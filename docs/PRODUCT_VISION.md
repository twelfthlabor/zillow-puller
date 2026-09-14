# Product Vision

## The idea

A small, honest Zillow collector for property listings research. One
configured SeleniumBase run gathers listing cards into a CSV; an optional queue
runner walks Ontario regions with per-region checkpoints; a tiny read-only
server makes the CSVs queryable. Built for students and researchers who need
a reproducible snapshot, not a market feed.

```bash
scripts/launch_attach_chrome.sh
property-ontario --dry-run
property-ontario
property-api --csv data/regions/toronto-on/listings.csv --host 127.0.0.1 --port 8000
```

## How it should behave

- **Attach first, single session.** The preferred workflow connects to an
  already-running Chrome via `attach_address`; the scraper never launches a
  new browser and never quits the attached one. Launch mode (no
  `attach_address`) starts its own Chrome, optionally with a `user_data_dir`
  profile. Either way: one browser session at a time, one queue region at a
  time, courtesy delays with a little jitter, lazy-scroll settling before
  pagination — no parallel sessions, no identity rotation, no delay
  skipping.
- **Checkpoint and resume.** Every completed page updates the region
  checkpoint, and the queue stores `pending`/`done`/`partial`/`failed`/
  `challenged` per region. A stopped, partial, or challenged region resumes
  where it left off by re-running the queue; `done` regions are skipped.
- **Stop on challenge, keep moving.** A human-verification page ends the
  scraper run with a saved checkpoint and diagnostics — exit 3 — instead of
  retrying or working around it. A human handles the page in the attached
  browser; the queue waits a randomized cooldown (default 30–60 minutes) and
  retries the same region. After the per-region consecutive limit the region
  is marked `challenged` and the queue continues with the next city, and a
  total challenge bound stops the invocation between regions if too many
  accrue rather than hammering the site. Challenged regions resume
  automatically on a later run, and fresh regions are attempted before
  challenged retries.
- **Dedup by default.** Records merge on stable listing IDs (Zillow IDs from
  card URLs) with URL fallback, so re-runs and overlapping pages do not
  inflate counts.
- **Coverage honesty.** Every run reports what it actually did through the
  `report.json` `stop_reason`, page and record counts, and the queue status.
  A partial or sampled region is never presented as complete; the queue only
  marks a region `done` on `pagination_exhausted` or `incremental_complete`.
- **Incremental refresh.** On a fresh sweep, `incremental_stop` ends a run at
  a page that is mostly records already collected (`known_ratio`,
  `min_records`), so a regular refresh stops early instead of re-walking the
  whole result set. A mid-pagination resume ignores it and keeps paginating.

## Config, honestly

`config.json` is the whole plan: start URL, card and field selectors,
pagination mode, scroll settings, challenge markers, pacing, retry limits,
and output paths. The shipped configs set `max_pages` to `0` (unlimited) and
`session_restart_pages` to `0` (no browser restarts). Unlimited here does not
mean unbounded effort: runs still stop at a terminal pagination condition, on
the empty-page threshold, on a challenge, or on the incremental refresh
stop. Set a positive `max_pages` when a cost-bounded sample is wanted — a
capped run's `stop_reason` is `max_pages`, so it is labeled partial, never
"collected everything". In attach mode the browser lifecycle belongs to the
human, so `session_restart_pages` does not apply; launch mode can restart its
own Chrome to bound memory. `--city`/`--state` only fill the URL and path
templates the config already defines; they do not add new sources or pages.

## Success criteria

1. `property-ontario --dry-run` validates the base config, generates one
   config per selected region, prints the scraper command for each, and runs
   nothing.
2. `property-ontario` walks the selected regions (fresh work before
   challenged retries, regions-file order within each group), skips `done`
   regions, resumes `partial` ones from their checkpoints, retries
   same-region challenges after a cooldown, marks a region `challenged`
   after its per-region limit and continues, and exits `0`, `2`, `3`, or `4`
   for success, configuration error, challenge (total bound reached or
   regions left challenged), or failed regions — `130`/`143` when
   interrupted. `--reopen` resets any region for a fresh attempt.
3. A challenged region exits the scraper with code 3, `stop_reason`
   `challenge`, diagnostics on disk, and partial — not complete — coverage;
   the queue resumes it automatically on a later invocation.
4. `property-scraper --config config.json --city Toronto --state ON`
   completes a single-region run that ends in a documented terminal
   condition with CSV, checkpoint, and report to show for it.
5. Re-running the same command resumes from the checkpoint without
   duplicating rows.
6. `property-api` serves a resulting CSV with working filters and a
   `/metadata` answer that matches the run's actual completeness.
7. Selectors and pacing stay in config, not in code, so a markup change is
   a config edit with a test run, not a rewrite.
8. Two runners pointed at the same state directory cannot interleave: the
   second exits 2 while the first holds the lock, and a dead
   `attach_address` is caught before any region starts.

## Non-goals

- A unified cross-source package or multi-source adapter framework.
- Unlimited paging or full-market coverage claims from a capped run.
- Server-first workflows that hide the CSV snapshot behind another layer.
- Any form of access-control evasion as a collection strategy; the queue
  waits and resumes, it never solves or bypasses a challenge.
