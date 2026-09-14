# Project Instructions for Coding Agents

## Product intent

Zillow-first listing collector: one browser session at a time, one region at a
time when the Ontario queue is driving. The preferred workflow attaches to an
already-running Chrome (`attach_address`), so the scraper never launches or
quits that browser; a human handles any verification page in it.

Working loop:

```bash
scripts/launch_attach_chrome.sh           # once per scraping session
property-ontario --dry-run
property-ontario
property-api --csv data/ontario-listings.csv --host 127.0.0.1 --port 8000
```

Coverage honesty is the product. A run cut short by a challenge, page cap,
empty-page threshold, or selector trouble is partial or unknown, never
complete; "all" means the named query scope (source, city, pages) with a
documented terminal `stop_reason`. A first-page sample is never presented as
all listings, and `incremental_complete` means the refresh page was mostly
already known, not that pagination was exhausted. Stay polite: courtesy
delays, stop on challenges with a cooldown, bounded challenges per invocation;
never add retries that hammer the site or work around a challenge.

## Commands

- Interpreter: `.venv313/bin/python` (3.13, editable install of this repo).
  There is no global `python`; the console scripts come from this venv.
- Full suite: `.venv313/bin/python -m pytest -q` (~151 tests, ~50 s).
- Single test: `.venv313/bin/python -m pytest tests/test_ontario_pull.py -k stall -q`
- Real-Chrome integration (their own throwaway Chrome; skipped if missing):
  `.venv313/bin/python -m pytest tests/test_integration_attach.py tests/test_integration_lazy_io.py -q`
- Queue subprocess tests (fake scraper fixture, no browser):
  `.venv313/bin/python -m pytest tests/test_integration_runner.py -q`
- Queue dry run: `.venv313/bin/python -m ontario_pull --dry-run`
- Merge region CSVs:
  `.venv313/bin/python scripts/merge_listings.py --state-dir data/regions --output data/ontario-listings.csv`

## Testing rules

- Integration tests launch a throwaway headless Chrome on a free port with a
  temp profile. Never aim tests at the user's live Chrome on 127.0.0.1:9222.
  Product code must never quit/close an attached browser; the no-quit proof in
  `tests/test_integration_attach.py` must keep passing.
- New behavior needs fails-before/passes-after evidence; a green suite alone is
  not enough. The stall-watchdog tests are the model.
- Reuse `tests/fixtures/` (Zillow-shaped HTML, `fake_scraper.py` hang/chatty/
  challenge modes) instead of new scaffolds.

## Repository map

- `property_scraper.py`: collector. SeleniumBase Pure CDP (`sb_cdp.Chrome`)
  for attach and launch; `_build_session` opens the session, `_navigate` owns
  navigation/timeout/redirect classification, `_wait_for_cards` +
  `_detect_challenge` own the challenge-aware wait, and the scrolling
  collector bulk-extracts every card per JS round-trip. Writes CSV,
  checkpoint, error diagnostics, optional page archive, and a run report;
  exits 3 on human verification.
- `ontario_pull.py`: region queue. Generates per-region configs under the
  state dir, walks `regions.ontario.json`, resumes `partial`/`challenged`
  regions, cools down after challenges, bounds total challenges, holds a
  state-dir lock, and preflights the attach endpoint. Stall watchdog: a child
  silent past `--stall-timeout-minutes` (default 10, 0 disables) is SIGTERM'd,
  then SIGKILL'd after grace, and the region retries from checkpoint. The
  clock is sleep-inclusive (macOS CLOCK_MONOTONIC / Linux CLOCK_BOOTTIME), so
  a lid-close hang is caught right after wake. Stall kills consume
  `--error-max-attempts` (default 2); a failed region is skipped until
  `--retry-failed` or `--reopen <slug>`.
- `listing_api.py`: read-only HTTP server (`property-api`): `GET /health`,
  `/metadata`, `/listings`.
- `field_utils.py`: normalization, validation, dedup keys, backoff helpers.
- `regions.ontario.json`: 36 Ontario cities; slugs are `<city>-<state>`
  (`toronto-on`) and `{city}`/`{state}` fill `start_url` templates.
- `scripts/`: `launch_attach_chrome.sh` (debug port + dedicated profile;
  Chrome 136+ refuses debugging on the default profile), `weekly_run.sh` +
  `install_weekly_schedule.sh` (launchd weekly run under `caffeinate -i`;
  lid close still pauses it), `merge_listings.py`, and
  `bench_collect_page.py` (fixture benchmark; its legacy Selenium attach only
  runs pre-migration baseline copies).
- `config.example.json`: tracked selector/pacing template; `config.json` is
  local, untracked.

## SeleniumBase CDP quirks

- Use `sb_cdp.Chrome` directly; do not use the `SB()`/`Driver()` managers
  (they launch their own browser and reintroduce chromedriver). Never add
  captcha-solving or stealth helpers.
- Attach needs an existing page target and drives the newest tab: keep the
  attached browser to one tab. The constructor navigates its `url` argument,
  so pass the real target URL (never `about:blank`) when attaching.
- `CDPMethods` has no element handles and no positional JS arguments; inline
  values into self-contained scripts. Find calls block for seconds on a miss
  (~2.2 s for `find_elements`), which is why cheap JS presence gates precede
  them.
- A dead CDP socket does not raise; calls can hang forever. The queue
  watchdog exists for that; don't add per-page liveness probes in the scraper.

## Data and git rules

- `data/` is gitignored except the committed `data/ontario-listings.csv`;
  `config.json` stays untracked. Never commit browser profiles, logs,
  screenshots, checkpoints, or per-region configs.
- Do not merge, push, or deploy without an explicit human instruction.

## Boundaries

- Keep the collect-then-serve loop working; no new source frameworks or
  unified multi-source package.
- Update `docs/CLI_CONTRACT.md` when flags, config keys, the report schema,
  or exit codes change.

## Documentation map

- `docs/CLI_CONTRACT.md`: commands, config keys, report schema, exit codes
  (scraper 0/1/2/3; queue 0/2/3/4; 130/143 signals).
- `docs/SOURCE_COVERAGE.md`: coverage semantics and `stop_reason` handling.
- `docs/PRODUCT_VISION.md`: vision and success criteria.
- `README.md`: installation, capabilities, user entry point.
