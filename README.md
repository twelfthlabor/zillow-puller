# Property Scraper

Zillow-first browser collection for property listings research. A polite
SeleniumBase run attaches to (or launches) Chrome and writes normalized listings
to CSV; an Ontario queue runner walks 36 cities with per-region checkpoints;
a small read-only server exposes the collected CSVs over HTTP.

## What it does

1. `property-scraper` collects listings using the selectors and pacing in
   `config.json` (Zillow by default), with dedup, checkpoint/resume, optional
   gzipped page archive, and a run report.
2. `property-ontario` walks `regions.ontario.json`, generating one config per
   region under `data/regions/`, resuming partial regions, and waiting out
   human-verification challenges with a randomized cooldown.
3. `property-api` serves a collected CSV: `GET /health`, `GET /metadata`,
   `GET /listings`.

There is no unified package or command in this repo beyond the three listed
below, and none is planned here. If older notes describe a single
cross-source command, treat that as out of scope.

## Current commands

| Command | Purpose |
|---|---|
| `property-scraper` | SeleniumBase collector for one configured run (Zillow via `config.json`) |
| `property-ontario` | Region queue over Ontario cities with per-region state and resume |
| `property-api` | Read-only HTTP server over a collected CSV |

## Quickstart

### 1. Start the attach browser

```bash
scripts/launch_attach_chrome.sh
scripts/launch_attach_chrome.sh 9223 /path/to/other-profile
```

The helper takes `PORT` (default `9222`) then `PROFILE_DIR` (default
`$HOME/.chrome-zillow-scrape`). Manual equivalent:

```bash
open -na "Google Chrome" --args \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.chrome-zillow-scrape"
```

- Chrome 136 and newer refuse to open a remote-debugging port on the default
  profile, so the dedicated `--user-data-dir` is required.
- The debug port listens on localhost only. Close Chrome (or the port) when
  not scraping.
- Do not use a profile signed into a personal account; keep a dedicated
  scraping profile.

### 2. Configure

`config.json` is local and untracked, so a fresh clone only has
`config.example.json`:

```bash
cp config.example.json config.json   # fresh clone only
```

Set `"attach_address": "127.0.0.1:9222"` in `config.json` to drive the
browser from step 1. If it is `null` (as in `config.example.json`), the
scraper launches its own Chrome instead; set `user_data_dir` there if you
want that launch to use a persistent profile.

### 3. Run the Ontario queue

```bash
property-ontario --dry-run   # validate, generate per-region configs, run nothing
property-ontario             # walk the regions, skipping done, resuming partial
```

Use `--only toronto,ottawa` or `--skip` to narrow the queue, and
`--retry-failed` to retry regions marked failed. Per-region output lands in
`data/regions/<slug>/` (`config.json`, `listings.csv`, `checkpoint.json`,
`report.json`, plus `errors/` and `archive/` when generated).

### 4. Or run one region manually

```bash
property-scraper --config config.json --city toronto --state on
property-scraper --config config.json --city toronto --state on --log-level INFO
```

### 5. Serve the result

```bash
property-api --config config.json --host 127.0.0.1 --port 8000
property-api --csv data/regions/toronto-on/listings.csv --host 127.0.0.1 --port 8000
```

## Attach mode

Attach mode is the preferred workflow: with `attach_address` set, the scraper
connects to the running Chrome and never launches or quits a browser. The
attached browser stays open after the run, and a human solves any
verification page in it before the run resumes from its checkpoint. The
scraper drives the newest page tab, so keep the attached browser to a single
tab; when more than one page tab is open it logs a warning before running.
Launch flags (`headless`, `user_agent`, `user_data_dir`) and
`session_restart_pages` do not apply while attached. If the debug port is
not reachable, the scraper fails fast (exit 1) with a hint to restart Chrome
via the helper script instead of hanging; the Ontario queue checks the same
endpoint before running any region and exits 2. When the checkpoint is
already at `max_pages` there is nothing to fetch, so the run probes the
endpoint but does not connect.

## Ontario queue

`property-ontario` reads a base config plus `regions.ontario.json` (36
Ontario cities), generates and validates a config per region, and runs the
scraper one region at a time. Challenges (scraper exit 3) get a randomized
cooldown — 30–60 minutes by default — then the same region is retried; after
3 consecutive challenges the region is marked `challenged` and the queue
moves to the next city, resuming challenged regions automatically on a later
run. Fresh regions are attempted before challenged retries, and
`--max-total-challenges` (default 6, must be at least
`--max-consecutive-challenges`) stops the queue between regions if one
invocation racks up too many challenges. A scraper child that goes silent for
more than 10 minutes (`--stall-timeout-minutes`, `0` disables) is terminated
so its region can retry. The watchdog reads a sleep-inclusive clock on macOS
and Linux, so time spent asleep counts and a wake after a machine sleep is
caught at the next poll. Stall kills count against `--error-max-attempts` and
each one is logged; a region that exhausts the budget is skipped by later
runs until `--retry-failed` or `--reopen SLUG`. `--retry-failed` retries
failed regions, and `--reopen SLUG` resets any region (even `done`) for a
fresh attempt — a slug excluded by `--only`/`--skip` warns instead of
resetting.
A state-directory lock rejects a concurrent runner with exit 2. Exit codes:
`0` ok, `2` configuration error, `3` challenge stop or any region left
`challenged`, `4` failed regions, `130`/`143` interrupted. Full flags, state
layout, and resume rules are in
[docs/CLI_CONTRACT.md](docs/CLI_CONTRACT.md).

A run always records why it ended (`stop_reason`): a challenge, page cap,
empty-page threshold, or selector trouble is partial or unknown, never
complete. See [docs/SOURCE_COVERAGE.md](docs/SOURCE_COVERAGE.md).

## Weekly private runs

`scripts/weekly_run.sh` wraps the Ontario queue for a weekly unattended run.
It checks the attach endpoint first and launches Chrome via
`scripts/launch_attach_chrome.sh` when it is down, runs `property-ontario`
under `caffeinate -i`, and afterwards merges region CSVs into
`data/ontario-listings.csv` with `scripts/merge_listings.py`: one row per
listing (deduped by listing id/URL), the newest `scraped_at` wins when both
copies have one (otherwise the later-encountered row wins), and the row's
`region` is the newest copy's region. The merge receives the same
`--state-dir` value as the queue and is skipped for `--dry-run` and when the
queue exits 2, so it never reads a half-written tree. `caffeinate -i`
prevents idle sleep only — closing the lid or an explicit sleep can still
interrupt a long cooldown; the queue saves state and the next run resumes.
The weekly script passes no `--retry-failed`/`--reopen`, so a region marked
failed (for example by repeated stall kills against `--error-max-attempts`)
stays skipped until a run with one of those flags is started by hand.

Each run logs to `data/regions/logs/weekly-<UTC timestamp>.log`; launchd's
own output goes to `data/regions/logs/launchd.log`.

Install the schedule (Sunday 03:00 local time; installing does not start a
run):

```bash
scripts/install_weekly_schedule.sh
launchctl list | grep property-scraper
```

Uninstall:

```bash
launchctl bootout gui/$(id -u)/local.property-scraper.weekly
rm ~/Library/LaunchAgents/com.property-scraper.weekly.plist
```

Queue exit codes: `0` ok, `2` configuration/endpoint problem (for example
Chrome not reachable), `3` a challenge stopped the run or regions were left
`challenged`, `4` failed regions. Challenges are expected and are never
worked around — a challenged region is skipped and the next weekly run
resumes it automatically from its checkpoint.

`SKIP_CHROME_LAUNCH=1` fails fast instead of launching Chrome when the
endpoint is down. It is a manual-run switch only: launchd jobs do not inherit
your shell environment, so a scheduled run always launches Chrome.

Research use only. Listing data belongs to its source; respect its terms of use.

## Development setup

```bash
cd /path/to/property-scraper
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pytest -q -p no:rerunfailures
```

The attach-mode integration test launches a real headless Chrome and is
skipped when the Chrome binary is not installed.

Example configuration:

- `config.example.json` — tracked selector and pacing template
  (Zillow-shaped), with `max_pages: 0` (unlimited), `session_restart_pages:
  0` (no relaunches), incremental refresh stop enabled, and
  `attach_address: null`. Copy it to `config.json` and adapt the
  URL/selectors/pagination for your target site.
- `config.json` — your local, untracked working configuration (gitignored);
  in this checkout it attaches to `127.0.0.1:9222`.

Full option lists live in [docs/CLI_CONTRACT.md](docs/CLI_CONTRACT.md).

## Documentation

- [Product vision](docs/PRODUCT_VISION.md)
- [CLI contract](docs/CLI_CONTRACT.md)
- [Source and coverage model](docs/SOURCE_COVERAGE.md)
- [Coding-agent instructions](AGENTS.md)

## Licence and data rights

The software is MIT licensed. Listing data belongs to its source: Zillow's
terms of use and robots/access controls apply to every run. Only collect from
sources that permit automated access, keep the default courtesy delays, stop
when the site asks for human verification, and do not work around CAPTCHAs,
rate limits, fingerprinting, or identity checks. The Ontario queue waits a
bounded cooldown and resumes; it never solves a challenge. Research or
educational use does not grant extra access or redistribution rights.
