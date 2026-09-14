#!/usr/bin/env bash
# Install (or refresh) the weekly launchd schedule for scripts/weekly_run.sh.
#
# - Copies scripts/com.property-scraper.weekly.plist to
#   ~/Library/LaunchAgents/ after checking it points at this checkout.
# - Bootstraps it (launchctl bootstrap gui/<uid>, falling back to
#   launchctl load on older macOS).
# - Does not start a run: RunAtLoad is false and nothing is kickstarted.
# - Safe to re-run: the old label is unloaded before the fresh copy loads.
#
# Uninstall commands are printed at the end. This script only prepares the
# schedule; a human runs it when ready.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"

LABEL="local.property-scraper.weekly"
TEMPLATE="$SCRIPT_DIR/com.property-scraper.weekly.plist"
DEST="$HOME/Library/LaunchAgents/com.property-scraper.weekly.plist"
DOMAIN="gui/$(id -u)"
LOG_DIR="$REPO_ROOT/data/regions/logs"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "ERROR: template not found: $TEMPLATE" >&2
  exit 1
fi

# launchd does not create the log directory; it must exist before the job
# writes StandardOutPath/StandardErrorPath.
mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

PLIST_WRAPPER="$(plutil -extract ProgramArguments.0 raw "$TEMPLATE" 2>/dev/null || true)"
if [[ "$PLIST_WRAPPER" != "$REPO_ROOT/scripts/weekly_run.sh" ]]; then
  echo "ERROR: $TEMPLATE points at '$PLIST_WRAPPER', not this checkout ($REPO_ROOT/scripts/weekly_run.sh)." >&2
  echo "Update the plist paths before installing." >&2
  exit 1
fi

cp "$TEMPLATE" "$DEST"
plutil -lint "$DEST" >/dev/null

# Re-run safety: unload any previously loaded copy before loading the new one.
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true

if launchctl bootstrap "$DOMAIN" "$DEST" >/dev/null 2>&1; then
  LOADED="launchctl bootstrap"
elif launchctl load -w "$DEST" >/dev/null 2>&1; then
  LOADED="launchctl load (bootstrap fallback)"
else
  echo "ERROR: could not load $LABEL." >&2
  echo "Try manually: launchctl bootstrap $DOMAIN \"$DEST\"" >&2
  exit 1
fi

cat <<EOF
Installed $LABEL via $LOADED.
This did not start a run; the next run is Sunday 03:00 local time
(a missed run fires when the Mac is next awake).

Check:
  launchctl list | grep property-scraper
  launchctl print $DOMAIN/$LABEL
  tail -f $LOG_DIR/launchd.log
  ls $LOG_DIR/weekly-*.log

Uninstall:
  launchctl bootout $DOMAIN/$LABEL
  rm "$DEST"
EOF
