#!/usr/bin/env bash
# Weekly private-research wrapper around the Ontario queue.
#
# - Resolves the repo root from this script's location, so it runs the same
#   from launchd, a terminal, or any working directory.
# - Checks the attach-mode Chrome DevTools endpoint and launches the attach
#   browser via scripts/launch_attach_chrome.sh when it is down. Set
#   SKIP_CHROME_LAUNCH=1 to fail fast instead of launching Chrome (a manual
#   switch; launchd jobs do not inherit shell environment).
# - Runs `.venv313/bin/property-ontario "$@"` under `caffeinate -i` (when
#   available) so idle sleep does not cut a challenge cooldown short, then
#   runs scripts/merge_listings.py if that helper exists.
# - Merge is skipped for --dry-run and when the queue exits 2 (configuration,
#   endpoint, or lock error) so a concurrent run's CSVs are never touched;
#   otherwise it receives the same --state-dir value as the queue.
# - Logs the whole run to data/regions/logs/weekly-<UTC timestamp>.log,
#   teeing to stdout.
#
# Polite private research only: no CAPTCHA solving, bypass, or identity
# tricks; challenges stop the run and the next invocation resumes.
# Scheduled via scripts/install_weekly_schedule.sh (nothing is deployed).
set -euo pipefail

# Resolve paths from this script's location (independent of the caller's cwd).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"

ONTARIO_BIN="$REPO_ROOT/.venv313/bin/property-ontario"
PYTHON_BIN="$REPO_ROOT/.venv313/bin/python"
LAUNCH_HELPER="$REPO_ROOT/scripts/launch_attach_chrome.sh"
MERGE_SCRIPT="$REPO_ROOT/scripts/merge_listings.py"
LOG_DIR="$REPO_ROOT/data/regions/logs"

# Attach endpoint. Overridable for testing (e.g. ATTACH_PORT=9223); the local
# config attaches to 127.0.0.1:9222.
ATTACH_HOST="${ATTACH_HOST:-127.0.0.1}"
ATTACH_PORT="${ATTACH_PORT:-9222}"
ATTACH_URL="http://${ATTACH_HOST}:${ATTACH_PORT}/json/version"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/weekly-$(date -u +%Y%m%dT%H%M%SZ).log"

endpoint_up() {
  curl -fsS --max-time 2 "$ATTACH_URL" >/dev/null 2>&1
}

endpoint_error() {
  echo "ERROR: attach endpoint $ATTACH_URL is unreachable." >&2
  echo "       Start Chrome with: $LAUNCH_HELPER $ATTACH_PORT" >&2
  echo "       (or set SKIP_CHROME_LAUNCH=1 to fail fast without launching Chrome)." >&2
}

# Effective queue options parsed from the forwarded args; last occurrence wins,
# matching argparse. Defaults mirror property-ontario / merge_listings.py.
DRY_RUN=0
STATE_DIR="data/regions"

parse_forwarded_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        ;;
      --state-dir)
        if [[ $# -ge 2 ]]; then
          STATE_DIR="$2"
          shift
        fi
        ;;
      --state-dir=*)
        STATE_DIR="${1#--state-dir=}"
        ;;
    esac
    shift
  done
}

merge_command() {
  echo "$PYTHON_BIN scripts/merge_listings.py --state-dir $STATE_DIR"
}

main() {
  echo "=== weekly_run $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  echo "repo root:  $REPO_ROOT"
  echo "log file:   $LOG_FILE"
  echo "attach url: $ATTACH_URL"
  echo "arguments:  ${*:-(none)}"
  echo "state dir:  $STATE_DIR"
  echo "dry run:    $DRY_RUN"

  local queue_rc=0

  if ! endpoint_up; then
    if [[ "${SKIP_CHROME_LAUNCH:-}" == "1" ]]; then
      endpoint_error
      queue_rc=2
    else
      echo "Attach endpoint is down; launching Chrome via $LAUNCH_HELPER ..."
      "$LAUNCH_HELPER" "$ATTACH_PORT" || echo "launch helper reported failure; waiting for the endpoint anyway..."
      local i
      for i in $(seq 1 20); do
        endpoint_up && break
        sleep 1
      done
      if ! endpoint_up; then
        echo "ERROR: attach endpoint did not come up within ~20s." >&2
        endpoint_error
        queue_rc=2
      fi
    fi
  fi

  if [[ "$queue_rc" -eq 0 && ! -x "$ONTARIO_BIN" ]]; then
    echo "ERROR: $ONTARIO_BIN is missing or not executable; create the .venv313 virtualenv first." >&2
    queue_rc=2
  fi

  if [[ "$queue_rc" -eq 0 ]]; then
    if command -v caffeinate >/dev/null 2>&1; then
      # caffeinate -i prevents idle sleep only; it forwards the child's exit
      # code. Closing the lid or an explicit sleep can still interrupt.
      ( cd "$REPO_ROOT" && caffeinate -i "$ONTARIO_BIN" "$@" ) || queue_rc=$?
    else
      echo "WARNING: caffeinate not found; long cooldowns may be interrupted by system sleep." >&2
      ( cd "$REPO_ROOT" && "$ONTARIO_BIN" "$@" ) || queue_rc=$?
    fi
  fi
  echo "queue exit code: $queue_rc"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "dry run: skipping merge (would run: $(merge_command))"
  elif [[ "$queue_rc" -eq 2 ]]; then
    echo "queue exit code 2 (configuration/endpoint/lock error); skipping merge - a concurrent run may be mid-write in the same CSVs."
  elif [[ -f "$MERGE_SCRIPT" ]]; then
    echo "merge command: $(merge_command)"
    local merge_rc=0
    ( cd "$REPO_ROOT" && "$PYTHON_BIN" scripts/merge_listings.py --state-dir "$STATE_DIR" ) || merge_rc=$?
    if [[ "$merge_rc" -eq 0 ]]; then
      echo "merge_listings.py: ok"
    else
      echo "WARNING: merge_listings.py failed (exit $merge_rc); queue exit code $queue_rc is unchanged." >&2
    fi
  else
    echo "merge_listings.py not present; skipping merge."
  fi

  return "$queue_rc"
}

parse_forwarded_args "$@"

set +e
main "$@" 2>&1 | tee -a "$LOG_FILE"
rc="${PIPESTATUS[0]}"
set -e
echo "=== weekly_run finished: exit $rc (log: $LOG_FILE) ===" | tee -a "$LOG_FILE"
exit "$rc"
