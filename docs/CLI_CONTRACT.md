# CLI Contract

This document describes the commands that actually exist in this repo:
`property-scraper`, `property-ontario`, and `property-api`. There is no
unified cross-source command or package; do not document or assume one.

## `property-scraper`

Collects listings with SeleniumBase into a CSV, following a JSON configuration.
`config.json` is a local, untracked configuration — on a fresh clone, copy
`config.example.json` to `config.json` and adapt the URL/selectors/pagination
for your target site (the Zillow setup uses data-testid-based card selectors).

```bash
property-scraper --config config.json
property-scraper --config config.json --city Toronto --state ON
property-scraper --config config.json --city Toronto --state ON --log-level DEBUG
```

Options:

- `--config PATH` (required): site-specific JSON configuration.
- `--city NAME`, `--state NAME`: optional location pair. Must be given
  together or not at all. They fill the `{city}` / `{state}` placeholders in
  `start_url` and pagination templates, and suffix the output, checkpoint,
  and report filenames with `-<city>-<state>` plus a `<city>-<state>/`
  subdirectory for errors and archives (for example
  `data/listings-toronto-on.csv`).
- `--log-level DEBUG|INFO|WARNING|ERROR` (default `INFO`).

Without `--city`/`--state`, the run uses `start_url` and the file paths
exactly as written in the config.

### Browser modes

**Attach mode** (`attach_address` set): the scraper connects to an
already-running Chrome DevTools endpoint and never launches a browser. The
attached browser is left open when the run exits — the browser session is not
quit. Launch flags (`headless`, `user_agent`, `user_data_dir`) and
`session_restart_pages` do not apply; a warning is logged if a restart
interval is configured.

The scraper drives the newest page tab (SeleniumBase Pure CDP Mode behavior);
keep the attached browser to a single tab. When more than one `type=="page"`
target is open, attach mode logs a warning naming the tab count before
running. Tab pinning is deliberately not implemented.

Before building the browser session, attach mode probes
`http://<attach_address>/json/version` with a 2-second timeout. A dead port
fails fast with exit 1 and a message pointing at
`scripts/launch_attach_chrome.sh`, instead of waiting out the normal session
timeout. When the checkpoint is already at `max_pages`, there is nothing to
fetch: the run still probes the endpoint (a dead port still exits 1) but does
not connect to the browser.

Setup (once per scraping session):

```bash
scripts/launch_attach_chrome.sh              # port 9222, profile $HOME/.chrome-zillow-scrape
scripts/launch_attach_chrome.sh 9223 /path/to/other-profile
```

The helper takes `PORT` (default `9222`) then `PROFILE_DIR` (default
`$HOME/.chrome-zillow-scrape`), launches Chrome with
`--remote-debugging-port` and `--user-data-dir`, waits up to 10 seconds for
`http://127.0.0.1:<port>/json/version`, and exits nonzero if the endpoint
never answers. Manual equivalent:

```bash
open -na "Google Chrome" --args \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.chrome-zillow-scrape"
```

Then set `"attach_address": "127.0.0.1:9222"` in `config.json`.

- Chrome 136 and newer refuse to open a remote-debugging port on the default
  profile, so a dedicated `--user-data-dir` is required.
- The debug port listens on localhost only. Close Chrome (or the port) when
  not scraping: any local process that can reach the port can drive the
  browser.
- Do not point this at a browser profile signed into a personal account; use
  the dedicated scraping profile.
- The scraper never solves a challenge. A human solves the verification
  page in the attached browser, and the run resumes from its checkpoint.

**Launch mode** (`attach_address` null): the scraper starts and quits its own
Chrome. `headless` (default `true`), `user_agent`, and the optional
`user_data_dir` profile apply. `session_restart_pages` restarts the browser
every N pages to bound memory; `0` (the shipped value) disables restarts.

### Configuration keys

Paths in the config are read relative to the config file's directory.

| Key | Default | Meaning |
|---|---|---|
| `start_url` | required | http(s) URL; may contain `{city}` / `{state}` placeholders. |
| `user_agent` | required | User-Agent used in launch mode only. |
| `output_csv` | required | CSV destination, appended across runs. |
| `checkpoint_file` | required | Resume state (`next_url`, `pages_completed`, `updated_at`). |
| `error_directory` | required | Failure diagnostics: `<stamp>-<label>.png`, `.html`, `.json`. |
| `report_file` | `null` | Run report JSON written at exit (see schema below); `null` disables. |
| `archive_directory` | `null` | Gzipped raw HTML for each successfully fetched page (`<stamp>-page-NNN.html.gz`); `null` disables. |
| `attach_address` | `null` | `host:port` of a running Chrome DevTools endpoint (for example `127.0.0.1:9222`). |
| `user_data_dir` | `null` | Launch mode: Chrome profile directory (`--user-data-dir`). |
| `headless` | `true` | Launch mode: run Chrome headless. |
| `page_wait_seconds` | `15` | Card wait (and challenge poll) per page. Navigation waits are SeleniumBase CDP internal and not configurable: ≈0.9 s settle plus up to 50 s network-idle per attempt, with an ≈30 s cap on the attach-constructor navigation. |
| `request_delay_seconds` | `3` | Courtesy delay before each page. |
| `request_jitter_seconds` | `0` | Random +/- jitter added to the delay. |
| `max_pages` | `100` | Page cap; `0` means unlimited. Must not be negative. |
| `max_attempts_per_page` | `3` | Attempts for transient page failures (at least 1). |
| `backoff_base_seconds` | `5` | Base for retry backoff (`base * 2^(attempt-1)`, capped at 120 s, +/-25% jitter). |
| `empty_page_stop_threshold` | `3` | Consecutive pages with zero cards before stopping. |
| `session_restart_pages` | `0` | Launch mode: quit and relaunch Chrome every N pages; `0` disables. Ignored in attach mode. |
| `incremental_stop` | `{}` | Refresh early-stop object (below). |
| `listing_id_pattern` | Zillow `_zpid` regex | Regex whose first group is the listing ID when no ID field matched. |
| `pagination` | required | Pagination object (below). |
| `scrolling` | `{}` | Lazy-scroll extraction object (below). |
| `challenge` | `{}` | Human-verification detection markers (below). |
| `selectors` | required | `card` plus one rule per field (below). |

Validation failures (unknown pagination mode, negative delays, bad
`attach_address`, unknown field transform, invalid regex, and so on) stop the
run with exit code 2.

#### `incremental_stop`

Used on refresh runs to stop early when a page is mostly records already in
the CSV/dedup set:

- `enabled` (default `false`).
- `known_ratio` (default `0.8`, must be 0–1).
- `min_records` (default `20`).

After a page has been appended, if it produced at least `min_records` and at
least `known_ratio` of them were already known, the run stops with
`stop_reason` `incremental_complete` and sets checkpoint `next_url` to
`null`, so the next run starts at `start_url` instead of resuming
mid-refresh. A page below either bound continues normally.

This applies only to a fresh sweep — a run whose starting URL is
`start_url` (no checkpoint, or a checkpoint with `next_url` null). A run
resumed mid-pagination ignores `incremental_stop` and keeps going, because
later pages can still hold new listings even when the resumed page is
mostly known.

#### `pagination`

- `mode`: `next_button` or `url_template` (required).
- `next_button`: `next_selector` (required) locates the control;
  `disabled_attribute` (default `aria-disabled`) with value `true` or
  `disabled` means the last page; the control's `href` is followed, and
  `url_template_fallback` is used when there is no `href`.
- `url_template`: `url_template` is formatted with the next page number
  (`template.format(page=N)`).
- `url_template` / `url_template_fallback` may also contain `{city}` /
  `{state}` when the run was started with `--city`/`--state`.

#### `scrolling`

Bulk card extraction for lazy-rendered result panes:

- `enabled` (default `false`).
- `max_rounds` (default `80`).
- `stable_rounds` (default `3`): stop once the pane is at the bottom and this
  many consecutive rounds show no change in extracted card values, card
  count, or scroll geometry.
- `settle_seconds` (default `2.5`): the quiet period that must pass at the
  bottom of a scrollable pane before the stop criterion may fire. It is
  anchored to the later of reaching the bottom and the last observed change
  in any extracted value or in the scroll geometry, and every change restarts
  it. A field that hydrates within `settle_seconds` of the last change is
  captured and extends polling further; a change after a fully quiet
  `settle_seconds` window is not waited for. This bounds late hydration — it
  deliberately does not restore the old loop's fixed ~4.8 s of blind polling.
  Non-scrollable panes are not held for the quiet period.
- `step_delay_seconds` (default `0.05`): dwell between scrolling rounds while
  the pane is still moving. It paces the page's own lazy loading (one
  viewport step at a time instead of a burst of steps) and gives the browser
  rendering frames so IntersectionObserver hydration is not skipped. This is
  a browser-local pacing, separate from the request-level
  `request_delay_seconds`; it never changes navigation count.
- `poll_seconds` (default `0.2`; a legacy `pause_seconds` key is accepted as
  a fallback): the wait between rounds once the pane has stopped moving and
  while the settle period is running.
- `scroll_fraction` (default `0.8`, clamped to 0.1–1.0): fraction of the pane
  height scrolled per round; a round that would pass the bottom lands on it
  exactly.

Each round is a single browser call that scrolls the closest scrollable
ancestor of the first card and reads every configured field from every card.
Values missing in one round are filled in by later rounds, and records merge
on the dedup key. With `enabled` false, the scraper reads the rendered cards
once.

#### `challenge`

Detection runs while waiting for cards. A page with no cards is polled for
challenge markers about every 0.25 s, so markers appearing before
`page_wait_seconds` expires raise the challenge stop immediately instead of
after the full card wait; a slow legitimate page still gets the full wait,
and the marker rules below are unchanged:

- `selectors` (default: `#px-captcha`, `#px-captcha-container`,
  `iframe[src*='captcha']`, `iframe[src*='recaptcha']`, `.g-recaptcha`,
  `[data-testid='captcha']`).
- `text_markers` (default: `access to this page has been denied`,
  `press &amp; hold`, `press & hold`, `press and hold`,
  `confirm you are a human`, `verify you are human`).

A DOM match raises the challenge stop. Text markers are matched against
visible text (`document.body.innerText`) first, with `page_source` only as a
fallback when reading visible text fails or returns blank — challenge-
sounding strings in inline scripts or hidden markup do not trigger a stop.
The bare word `captcha` in ordinary markup is not treated as a challenge.

A challenge stop is exit code 3, `stop_reason` `challenge`, diagnostics
saved, checkpoint pointing at the failed page so a rerun retries it. When
the card wait times out and no challenge is detected, the page is recorded
as an empty page (no cards) and feeds the `empty_page_stop_threshold`
counter. The same reason covers the end of pagination when the region still
has zero records: a no-next-page ending with an empty CSV is
`empty_page_threshold` (partial), never `pagination_exhausted` — a normal
exhaustion with at least one record is `pagination_exhausted`. Transient
(non-challenge) failures are retried up to `max_attempts_per_page` before
the run exits 1.

#### `selectors`

`card` (required) is the listing-card selector. Every other key is a field
rule with `selector` (required), optional `attribute` (else element text),
optional `transform` (default `text`), and optional numeric `validation`
(`min`/`max`, out-of-range values become empty). Known transforms:
`text`, `money`, `integer`, `number`, `detail_beds`, `detail_baths`,
`detail_sqft`, `brokerage`. Fields are written in this order:
`listing_id`, `address`, `price`, `beds`, `baths`, `sqft`, `agent`, `url`,
`source_page`, `scraped_at`.

On current Zillow search cards the card-level detail element
(`[data-testid='property-card-details']`) only carries a concatenated
bed/bath line (for example `5 bds7 ba`): `price`, `beds`, `baths`, `address`,
`agent`, `listing_id`, and `url` populate from those cards, while `sqft` is
expected empty because there is no card-level sqft element or testid. The
`detail_beds` / `detail_baths` transforms parse that concatenated line;
`detail_sqft` exists for configs that point at markup carrying footage, and
a populated `sqft` column requires a detail-page fetch this tool does not
perform.

### Run report

When `report_file` is set, an atomic JSON report is written at exit, even
after a challenge or browser error:

| Field | Meaning |
|---|---|
| `schema_version` | `1`. |
| `start_url` | The URL run this invocation (after `--city`/`--state` substitution). |
| `pages_completed` | Pages completed, including pages resumed from the checkpoint. |
| `records_added` | New unique rows written to the CSV by this run. |
| `records_total` | Unique records known after the run (existing plus added). |
| `stop_reason` | Why the run ended (enum below). |
| `started_at`, `finished_at` | ISO 8601 UTC timestamps. |
| `challenges` | `1` if this run ended on a human-verification page, else `0`. |
| `resumed_from_pages` | `pages_completed` at start, taken from the checkpoint. |

`stop_reason` values:

| Value | Meaning |
|---|---|
| `pagination_exhausted` | No next page with at least one record collected for the region (documented terminal condition). |
| `max_pages` | Configured `max_pages` cap reached. |
| `incremental_complete` | Refresh stopped because a page was mostly already known. |
| `empty_page_threshold` | `empty_page_stop_threshold` consecutive empty pages, or pagination ended with zero records ever collected for the region. |
| `challenge` | Human verification detected (exit 3). |
| `browser_error` | Browser/CDP failure, or a run that ended without a classified reason. |
| `stop_requested` | SIGINT/SIGTERM graceful stop; checkpoint saved for resume. |
| `pagination_loop` | The next URL was already visited this run. |

A failed report write logs a warning and does not change the outcome. With
`--city`/`--state`, the report name is suffixed like the other paths
(for example `report-toronto-on.json`).

Exit codes:

- `0`: run finished, including a graceful SIGINT/SIGTERM stop (check
  `stop_reason` for how it ended).
- `1`: browser error.
- `2`: configuration or argument error (including `--city` without
  `--state`).
- `3`: the site requested human verification; the run stopped and the
  checkpoint was saved.

## `property-ontario`

Region-by-region queue runner for the Zillow scraper. It reads a base config
and a regions file, generates one config per region inside the state
directory, and runs the scraper once per unfinished region (sequentially, one
subprocess at a time). After a challenge (scraper exit 3) it waits a
randomized cooldown and retries the same region; once a region reaches its
per-region consecutive-challenge limit the region is marked `challenged` and
the queue moves to the next one. Fresh work (`pending`/`partial`) is
attempted before `challenged` retries, and `--max-total-challenges` is
checked at region boundaries: the in-flight region always settles to a
terminal status before the queue stops, so the bound never leaves a region
`pending`. It never solves, bypasses, or suppresses a challenge.

Real runs hold a `<state-dir>/.lock` file, so a second `property-ontario`
pointed at the same state directory exits 2 with "another run is active".
Before any region runs, a real run also probes the base config's
`attach_address` (when set) and exits 2 if that DevTools endpoint is dead.

```bash
property-ontario --dry-run
property-ontario
property-ontario --only toronto,ottawa
property-ontario --skip toronto --retry-failed
```

Options:

| Flag | Default | Meaning |
|---|---|---|
| `--base-config PATH` | `config.json` in the module directory if present, else `config.example.json` there | Template config; `start_url` must contain `{city}` and `{state}`. |
| `--regions PATH` | `regions.ontario.json` next to the module | Regions file. |
| `--state-dir PATH` | `data/regions` | Per-region configs and queue state. |
| `--scraper-command CMD` | `<python> -m property_scraper` | Scraper command, shlex-split; `--config <region config> --log-level <level>` is appended. |
| `--only SLUGS` | all | Comma-separated city slugs or full slugs. |
| `--skip SLUGS` | none | Comma-separated slugs to exclude. |
| `--cooldown-min-minutes F` | `30` | Minimum cooldown after a challenge. |
| `--cooldown-max-minutes F` | `60` | Maximum cooldown after a challenge. |
| `--max-consecutive-challenges N` | `3` | Mark a region `challenged` after N consecutive challenges, then continue with the next region (at least 1). |
| `--max-total-challenges N` | `6` | Stop the whole queue after N challenges in one invocation; checked between regions and must be at least `--max-consecutive-challenges` (at least 1). |
| `--error-max-attempts N` | `2` | Total attempts for non-challenge failures before a region is marked failed. |
| `--stall-timeout-minutes F` | `10` | Terminate a scraper child that has produced no output for this many minutes so the region can retry (SIGTERM, then SIGKILL after ~10s); `0` disables. Must be a finite, non-negative number. |
| `--retry-failed` | off | Reset failed regions to pending for this run. |
| `--reopen SLUGS` | none | Comma-separated region slugs to reset to `pending` before the run, whatever their status (including `done`, `failed`, and `challenged`). |
| `--dry-run` | off | Validate, generate region configs, print commands, run nothing. |
| `--log-level DEBUG\|INFO\|WARNING\|ERROR` | `INFO` | Queue log level. |

The default `--base-config` is looked up next to `ontario_pull.py`, not
relative to the current working directory; `--state-dir` is still relative
to the working directory.

### Regions file

```json
{
  "province": "on",
  "regions": [
    {"city": "toronto", "name": "Toronto"},
    {"city": "st-catharines", "name": "St. Catharines"}
  ]
}
```

`regions.ontario.json` ships 36 Ontario cities. Each entry needs `city`;
`name` defaults to the city, and a per-entry `state`/`province` defaults to
the file-level `province` (`on`). Slugs are lowercased city-state pairs
(`toronto-on`, `st-catharines-on`). `--only`/`--skip` accept either the city
slug (`toronto`) or the full slug (`toronto-on`); an unknown slug is a
configuration error (exit 2).

### Generated configs and state layout

The base config is never modified. For each region, `start_url` and the
pagination templates are filled with that region's `{city}`/`{state}`, and
the output paths are reset to bare filenames so everything lands in the
region directory:

```text
data/regions/
  .lock                     # flock held for the duration of a real run
  queue-state.json          # per-region status, schema_version 1
  last-run-summary.json     # counts, exit code, per-region state
  toronto-on/
    config.json             # generated from the base config
    listings.csv            # output_csv
    checkpoint.json         # scraper resume state
    report.json             # scraper run report
    errors/                 # created when a page fails
    archive/                # created when archive_directory is set in the base config
```

The generated config always sets `report_file` to `report.json`; the queue
warns and marks the region partial if that file is missing. If the base
config names an `archive_directory`, the region config points at `archive/`
inside the region directory; if it is `null`, archiving stays off.

Every generated config is validated with the scraper's own
`Settings.load().validate()` before it is used or reported ready — in real
runs and in `--dry-run` — and an invalid config exits 2. A base `max_pages`
that is missing (the scraper default of 100 would silently cap the pull) or
nonzero logs a warning; `max_pages: 0` is the full-pull setting.

### Resume semantics

`queue-state.json` stores per region: `status`, `attempts`,
`challenge_waits`, `last_exit_code`, `last_stop_reason`, `updated_at`.

| Status | Meaning | Next invocation |
|---|---|---|
| `pending` | Queued or resumed mid-run. | Attempted. |
| `done` | Scraper exited 0 with `pagination_exhausted` or `incremental_complete`. | Skipped. |
| `partial` | Scraper exited 0 with any other `stop_reason` (including a missing or unreadable report). | Attempted again; the scraper checkpoint resumes pagination. |
| `challenged` | Hit `--max-consecutive-challenges`; the queue moved on. | Attempted again automatically (no `--retry-failed` needed). |
| `failed` | Non-challenge errors exhausted `--error-max-attempts`. | Skipped unless `--retry-failed` (failed only) or `--reopen` (any status). |

`--reopen toronto,ottawa` resets the named regions to `pending` before the
skip checks, whatever their current status; reopened regions count as fresh
work. Unknown slugs exit 2. A reopen slug that is valid but excluded by
`--only`/`--skip` cannot be reset; the runner logs a warning that it was
excluded instead of silently ignoring it.

Regions are attempted in this order: fresh work (`pending`/`partial`, plus
any `--reopen` resets) in regions-file order first, then `challenged`
retries. `done` regions are skipped, and `failed` regions are skipped unless
`--retry-failed` or `--reopen` applies.

`schema_version` is `1` for both state files. `last-run-summary.json` adds
`started_at`, `finished_at`, `exit_code`, `stopped_by_challenge`,
`interrupted`, a `counts` object (`done`, `partial`, `failed`, `pending`,
`challenged`) covering every region in the run, and a `regions` map with
each region's full state. `stopped_by_challenge` is `true` only when
`--max-total-challenges` ended the run early.

### Challenge handling

- Scraper exit 3 counts as a challenge. The runner increments both the
  region's consecutive-challenge count and the invocation-wide total, waits
  a random delay between the cooldown minimum and maximum (default 30–60
  minutes), then retries the same region.
- When a region reaches `--max-consecutive-challenges` consecutive
  challenges (default 3), it is marked `challenged`, no further cooldown is
  spent on it in this invocation, and the queue continues with the next
  region.
- `--max-total-challenges` (default 6) bounds all exit-3 events in one
  invocation and must be at least `--max-consecutive-challenges` (otherwise
  the run exits 2). It is checked at region boundaries: the current region
  settles to `done`/`partial`/`failed`/`challenged` first, then the queue
  stops, saves state, and exits 3. Challenged regions resume automatically
  on the next run, and the bound never leaves a region `pending`.
- A region that exits 0 resets its own consecutive-challenge count; the
  invocation-wide total is never reset.
- Non-challenge failures retry within the region up to
  `--error-max-attempts` (default 2 total attempts) with a fixed 60-second
  backoff, then the region is marked `failed` and the queue continues.

### Stall detection

While a scraper child runs, the queue pipes its stdout/stderr and forwards
output to the queue's own streams as it arrives, so scraper logs keep their
formatting and stay interleaved live with queue logging. Partial output
counts as activity: a child that keeps flushing chunks without newlines is
not considered stalled. If the child produces no output for longer than
`--stall-timeout-minutes` (default 10; `0` disables), the queue logs
`stall detected: ...` and terminates the child with SIGTERM, escalating to
SIGKILL after a short grace period.

The watchdog reads the platform's sleep-inclusive clock where one exists:
`CLOCK_BOOTTIME` on Linux, and Darwin's `CLOCK_MONOTONIC` on macOS (it counts
time asleep, unlike `time.monotonic()`). Time spent asleep therefore counts
toward the timeout, and a machine sleep longer than the timeout is detected
at the first poll (about one second) after wake. Output still buffered in the
pipe when sleep started is read after wake and counts as fresh activity, so
that case can take one more timeout of awake silence to detect. Where only a
sleep-excluding monotonic clock is available, the watchdog falls back to
`time.monotonic()` and a wake is detected only after up to the timeout of
additional awake silence. A base config whose `page_wait_seconds` exceeds the
stall timeout logs a warning when the region config is built, because a
legitimately slow card wait could otherwise trip the watchdog.

The child's non-zero exit takes the ordinary failure path: it is retried
after the fixed backoff, and marked `failed` after `--error-max-attempts`
(default 2). Stall kills count against that budget, and each one logs
`stall kill counted as attempt N/M for <slug>; ...`. A region that reaches
the budget is skipped by later runs until `--retry-failed` or
`--reopen <slug>`, so an unattended queue needs that explicit retry after
repeated stalls. Checkpoint resume and dedup make a mid-region kill safe.

Processes that inherit the child's pipes and outlive it (for example an
orphaned grandchild) can delay reader shutdown by the bounded join, but the
queue never signals grandchildren and never waits past that bound.

### Exit codes

- `0`: queue processed; no region left `challenged`, no challenge stop, and
  no failed regions (some regions may still be `partial` or `pending` and
  resume on a later run).
- `2`: configuration or usage error, including an unreachable
  `attach_address`, an invalid generated region config,
  `--max-total-challenges` below `--max-consecutive-challenges`, or another
  run holding the state lock.
- `3`: the total challenge bound was reached, or one or more regions were
  left `challenged`; this takes precedence when regions both challenged and
  failed.
- `4`: queue finished with one or more failed regions and none challenged.
  `--retry-failed` or `--reopen` retries them on a later run.
- `130`/`143`: interrupted by SIGINT/SIGTERM; state is saved and the summary
  notes the interruption.

### `--dry-run`

Validates the base config and regions file, writes each selected region's
`config.json` (validated with the scraper's own `Settings`), prints the
exact scraper command per region, and executes nothing. It does not take the
state lock, probe `attach_address`, or write `queue-state.json` /
`last-run-summary.json`. Exit code is 0 on success, 2 on a configuration
error.

## `property-api`

Read-only HTTP server over a collected CSV. Stdlib only. Loads the CSV into
memory at startup; restart (or send SIGHUP) to pick up a new run.

```bash
property-api --csv data/listings.csv --host 127.0.0.1 --port 8000
property-api --config config.json --host 127.0.0.1 --port 8000
property-api --csv data/listings.csv --metadata data/metadata.json --port 8000
```

Options:

- `--csv PATH`: listings CSV to serve. For queue output, point it at a region
  CSV such as `data/regions/toronto-on/listings.csv`.
- `--config PATH`: alternative to `--csv`; uses `output_csv` from the
  scraper config (relative to the config's directory), plus its
  `metadata_file` when set. Provide `--csv` or `--config`.
- `--metadata PATH`: optional provenance/completeness JSON exposed at
  `GET /metadata`. Without it, `/metadata` reports unverified completeness.
- `--host ADDR` (default `127.0.0.1`), `--port N` (default `8000`).
- `--log-level LEVEL` (default `INFO`).

Endpoints:

- `GET /health` returns `{"status": "ok", "records": N}`.
- `GET /metadata` returns the provenance/coverage JSON.
- `GET /listings` returns filtered, paginated records. Filters:
  `listing_id`, `address`, `source` (substring match), `price_min`,
  `price_max`, `beds_min`, `beds_max`, `sqft_min`, `sqft_max`, plus `limit`
  (default 100, max 1000) and `offset` (default 0).
