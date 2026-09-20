#!/usr/bin/env bash
# Sequential multi-province wrapper around the Ontario queue.
#
# - Resolves the repo root from this script's location, so it runs the same
#   from any working directory.
# - Runs `.venv313/bin/property-ontario` once per province (on + 12 new
#   regions files) with the shared `--state-dir data/regions`, forwarding
#   all extra args (including --dry-run) to each invocation.
# - Exit 3/4 (challenge / failed regions) continues to the next province;
#   exit 2 (config error) stops the loop and exits 2.
# - After the loop (non-dry-run), merges to data/canada-listings.csv via
#   scripts/merge_listings.py with the uniform schema.
set -euo pipefail

# Resolve paths from this script's location (independent of the caller's cwd).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"

ONTARIO_BIN="$REPO_ROOT/.venv313/bin/property-ontario"
PYTHON_BIN="$REPO_ROOT/.venv313/bin/python"

PROVS="on bc ab sk mb qc nb ns pe nl yt nt nu"

DRY_RUN=0
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then
    DRY_RUN=1
    break
  fi
done

OVERALL_RC=0

for PROV in $PROVS; do
  if [[ "$PROV" == "on" ]]; then
    REGIONS="regions.ontario.json"
  else
    REGIONS="regions.${PROV}.json"
  fi
  echo "== $PROV ($REGIONS) =="
  set +e
  ( cd "$REPO_ROOT" && "$ONTARIO_BIN" --regions "$REPO_ROOT/$REGIONS" --state-dir data/regions "$@" )
  rc=$?
  set -e
  echo "exit $rc: $PROV"
  if [[ "$rc" -eq 2 ]]; then
    echo "ERROR: config error in $PROV; stopping (exit 2)." >&2
    exit 2
  fi
  if [[ "$rc" -ne 0 && "$rc" -ne 3 && "$rc" -ne 4 ]]; then
    echo "ERROR: unexpected exit $rc in $PROV; stopping." >&2
    exit "$rc"
  fi
  if [[ "$OVERALL_RC" -eq 0 ]]; then
    OVERALL_RC="$rc"
  fi
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry run: skipping merge (would run: scripts/merge_listings.py --state-dir data/regions --output data/canada-listings.csv)"
  exit "$OVERALL_RC"
fi

( cd "$REPO_ROOT" && "$PYTHON_BIN" scripts/merge_listings.py --state-dir data/regions --output data/canada-listings.csv )
exit "$OVERALL_RC"
