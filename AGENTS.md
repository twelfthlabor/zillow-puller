# Project Instructions for Coding Agents

## Product intent

This repo collects Zillow property listings with a polite SeleniumBase run —
one browser session at a time, and one region at a time when the Ontario
queue is driving. The preferred workflow attaches to an already-running
Chrome (`attach_address`) so the scraper never launches or quits the
browser, and a human handles any verification page in that browser. A
challenged region is marked `challenged` and the queue moves on; it resumes
automatically on a later invocation, and a total challenge bound (checked
between regions) can stop one invocation early. Toronto is the proving
ground. The working loop is:

```bash
scripts/launch_attach_chrome.sh           # once per scraping session
property-ontario --dry-run
property-ontario
property-api --csv data/listings.csv --host 127.0.0.1 --port 8000
```

Collection is Zillow-first and honest about completeness: a run that hits a
challenge, a page cap, an empty-page threshold, or selector trouble is
partial or unknown, never complete. A first-page sample is never presented
as all listings, and `incremental_complete` means the refresh page was
mostly already known — not that pagination was exhausted.

## What work should optimize for

1. Reliable Zillow collection: selectors, bulk lazy-scroll extraction,
   pagination, checkpoint/resume, and queue state that survive ordinary page
   variation.
2. Polite behavior: one browser session at a time and one queue region at a
   time, courtesy delays, stop-on-challenge with a randomized cooldown, a
   per-region consecutive-challenge limit that marks the region `challenged`
   and moves on, and a bounded total number of challenges per invocation.
   Never add retries that hammer the site.
3. Coverage honesty: an explicit `stop_reason`, pages traversed, dedup
   counts, field-quality warnings, and a clear complete/partial/empty/unknown
   reading for every run.
4. Simple research workflow: CSV on disk plus a read-only HTTP server over
   that CSV.

## Meaning of "all listings"

"All" is an ambition, not a claim. A result is complete only for the named
query scope (source, city, pages) after pagination reaches a documented
terminal condition with no cap, challenge, or failure cutting it short. Each
run must say what was attempted, what completed, and what remains unknown.

## Repository map

- `property_scraper.py`: SeleniumBase collector (`property-scraper`). Attach
  mode (`attach_address`) connects to an existing browser and leaves it
  open; launch mode starts and quits its own. Writes CSV, checkpoint, error
  diagnostics, optional gzipped page archive, and a run report; stops on
  human verification (exit 3).
- `ontario_pull.py`: region queue (`property-ontario`). Generates and
  validates per-region configs under the state dir, walks
  `regions.ontario.json` in order, resumes `partial`/`challenged` regions,
  waits a cooldown after challenges, marks a region `challenged` after its
  per-region limit and continues, bounds total challenges per invocation
  (checked at region boundaries; fresh work before challenged retries),
  holds a state-dir lock against concurrent runs, and preflights
  `attach_address` before running regions.
- `regions.ontario.json`: 36 Ontario cities, province `on`.
- `scripts/launch_attach_chrome.sh`: starts Chrome with
  `--remote-debugging-port` and a dedicated `--user-data-dir`, then waits
  for the DevTools endpoint.
- `field_utils.py`: normalization, validation, dedup keys, backoff helpers.
- `listing_api.py`: read-only HTTP server (`property-api`) over the CSV.
  `GET /health`, `GET /metadata`, `GET /listings`.
- `config.json`: working Zillow configuration (local, untracked — copy from
  `config.example.json` on a fresh clone). It attaches to
  `127.0.0.1:9222`; the tracked example launches its own headless browser.
- `config.example.json`: selector and pacing template.
- `tests/test_property_scraper.py`, `tests/test_ontario_pull.py`,
  `tests/test_listing_api.py`, `tests/test_field_utils.py`: unit and fake
  coverage for the above.
- `tests/test_integration_attach.py`: end-to-end attach-mode run against a
  real headless Chrome (skipped when Chrome is absent); proves collection
  and that the attached browser is not quit.
- `tests/test_integration_runner.py` with `tests/fixtures/fake_scraper.py`:
  subprocess-level `property-ontario` coverage of challenge cooldown, retry,
  and done-region skipping.
- `tests/fixtures/`: Zillow-shaped HTML fixtures and the fake scraper.

## Boundaries

- Each build task should leave the collect-then-serve loop working, not add
  a new source framework or a unified multi-source package.

## Documentation map

- `docs/PRODUCT_VISION.md`: Zillow-first vision and success criteria.
- `docs/CLI_CONTRACT.md`: the actual `property-scraper` /
  `property-ontario` / `property-api` interface, config keys, run report
  schema, and exit codes.
- `docs/SOURCE_COVERAGE.md`: coverage semantics and `stop_reason` handling.
- `README.md`: installation, current capabilities, and user entry point.
