#!/usr/bin/env bash
# Launch (or attach to) a Chrome instance with remote debugging enabled so the
# scraper can drive a real, human-verifiable browser profile.
#
# Usage: scripts/launch_attach_chrome.sh [PORT] [PROFILE_DIR]
#   PORT         remote debugging port (default: 9222)
#   PROFILE_DIR  Chrome user-data dir (default: $HOME/.chrome-zillow-scrape)
#
# Note: this only opens a browser with a debugging port. Solving any Zillow
# human-verification challenge remains a manual, human action.
set -euo pipefail

PORT="${1:-9222}"
PROFILE_DIR="${2:-$HOME/.chrome-zillow-scrape}"

mkdir -p "$PROFILE_DIR"

echo "Launching Google Chrome (remote debugging port $PORT, profile $PROFILE_DIR)..."
open -na "Google Chrome" --args \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE_DIR"

# Wait briefly for Chrome to bind the DevTools endpoint.
for _ in {1..20}; do
  if curl -fsS "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
    echo "Chrome DevTools endpoint is ready: http://127.0.0.1:$PORT/json/version"
    exit 0
  fi
  sleep 0.5
done

echo "ERROR: Chrome did not expose a DevTools endpoint on port $PORT" >&2
exit 1
