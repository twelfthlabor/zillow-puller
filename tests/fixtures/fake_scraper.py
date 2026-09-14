"""Stub scraper for the property-ontario subprocess integration tests.

The region slug is the name of the directory holding ``--config``. Behavior:

* default: the first call for a region exits 3 (human-verification challenge)
  and later calls write ``report.json`` (``stop_reason:
  pagination_exhausted``) and exit 0 -- the retry-until-success path;
* ``FAKE_SCRAPER_ALWAYS_CHALLENGE=slug,...``: those regions always exit 3;
* ``FAKE_SCRAPER_NEVER_CHALLENGE=slug,...``: those regions always succeed;
* ``FAKE_SCRAPER_HANG=slug,...``: those regions emit nothing at all and sleep
  forever, so the queue's stall watchdog has to kill them;
* ``FAKE_SCRAPER_CHATTY_SECONDS=<seconds>``: emit a line every 0.25s for that
  long, then succeed, so a slow-but-alive child is provably not mistaken for a
  stall.

Every invocation appends to ``fake-scraper-calls.json`` in the region
directory, so tests can prove retries, skips, and which regions ran.

The CLI accepts the flags ``ontario_pull.command_for_region`` appends:
``--config <path> --log-level <level>``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

COUNTER_FILENAME = "fake-scraper-calls.json"
CHALLENGE_EXIT_CODE = 3
STOP_REASON = "pagination_exhausted"


def _slug_set(env_name: str) -> set[str]:
    raw = os.environ.get(env_name, "")
    return {token.strip() for token in raw.split(",") if token.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(prog="fake-scraper")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    region_dir = args.config.resolve().parent
    slug = region_dir.name
    counter_path = region_dir / COUNTER_FILENAME
    calls = json.loads(counter_path.read_text(encoding="utf-8")) if counter_path.exists() else []
    calls.append({"config": str(args.config.resolve()), "slug": slug})
    counter_path.write_text(json.dumps(calls, indent=2), encoding="utf-8")

    if slug in _slug_set("FAKE_SCRAPER_HANG"):
        while True:
            time.sleep(3600)

    chatty_seconds = os.environ.get("FAKE_SCRAPER_CHATTY_SECONDS")
    if chatty_seconds:
        deadline = time.monotonic() + float(chatty_seconds)
        while time.monotonic() < deadline:
            print(
                f"fake-scraper: still working ({time.monotonic():.3f})",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(0.25)
        (region_dir / "report.json").write_text(
            json.dumps({"stop_reason": STOP_REASON}, indent=2),
            encoding="utf-8",
        )
        return 0

    if slug in _slug_set("FAKE_SCRAPER_ALWAYS_CHALLENGE"):
        return CHALLENGE_EXIT_CODE
    if slug not in _slug_set("FAKE_SCRAPER_NEVER_CHALLENGE") and len(calls) == 1:
        return CHALLENGE_EXIT_CODE

    (region_dir / "report.json").write_text(
        json.dumps({"stop_reason": STOP_REASON}, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
