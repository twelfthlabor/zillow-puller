# Source and Coverage Model

## Why coverage is part of the story

The collector aims to gather the listings available to its one configured
browser source — Zillow, driven by `config.json` — not a silent first-page
sample. Zillow exposes no public total to reconcile against here, so coverage
evidence is what a run actually recorded: the run report, checkpoints, error
diagnostics, the optional page archive, logs, and the queue state files. None
of those are a claim of market completeness.

## What each run records

1. Whether the query scope (source, city/state, start URL) was reachable.
2. `pages_completed`, `records_added`, and `records_total` (unique records
   known after the run, existing plus added), with dedup counts in the log.
3. Normalized rows in the shared CSV schema (`listing_id`, `address`,
   `price`, `beds`, `baths`, `sqft`, `agent`, `url`, `source_page`,
   `scraped_at`).
4. A `stop_reason` that names the terminal condition (enum below).
5. `challenges` (0 or 1) and `resumed_from_pages` (checkpoint depth at
   start).
6. Optional evidence: gzipped raw HTML for each successfully fetched page
   (`archive_directory`), failure screenshots/HTML/JSON in
   `error_directory`, and the queue's per-region status in
   `queue-state.json`.

The report is `report.json` per region when the Ontario queue generates the
configs, or the configured `report_file` for a manual run.

## Stop reasons and completeness

| `stop_reason` | The run ended because | Queue status | Coverage reading |
|---|---|---|---|
| `pagination_exhausted` | No next page with at least one record collected for the region | `done` | Complete for the configured scope. |
| `incremental_complete` | A refresh page was mostly already-known (`known_ratio` / `min_records`) | `done` | Snapshot treated as current; a deliberate early refresh stop, not an exhaustive pagination sweep. |
| `max_pages` | `max_pages` cap reached | `partial` | Partial — more pages likely existed. |
| `empty_page_threshold` | N consecutive pages rendered no cards (including card waits that timed out with no challenge), or pagination ended with zero records ever collected for the region | `partial` | Partial; suspect selector drift, a soft block, or an empty query. |
| `challenge` | Human verification detected (exit 3) | retried, then `challenged` after the per-region limit | Partial; the queue waits a cooldown and retries, then leaves the region `challenged` to resume on a later run. |
| `browser_error` | Browser/CDP failure, or the run ended without a classified reason | `partial` / `failed` | Partial or failed depending on whether rows were written. |
| `stop_requested` | SIGINT/SIGTERM graceful stop | `partial` | Partial; checkpoint resumes on the next run. |
| `pagination_loop` | The next URL was already visited this run | `partial` | Partial or unknown; inspect why pagination repeated. |

Challenge text markers are matched against visible page text
(`document.body.innerText`) first; `page_source` is only a fallback when
reading visible text fails or returns blank, so markers embedded in inline
scripts or hidden markup do not count as a challenge.

## Completeness rules

A run is complete for its scope only when all of these hold:

- The run exited 0 through `pagination_exhausted` (or
  `incremental_complete` for a refresh that is deliberately treated as up to
  date).
- No page cap, empty-page stop, challenge, authentication error, or transient
  failure cut the run short.
- Card and field selectors kept matching; a collapse in card detection makes
  the result unknown, not complete.
- The scope names its exact source and query (Zillow, city/state, start
  URL).

Anything else is partial (useful rows, unfinished scope, or a zero-record
pagination ending, which reports `empty_page_threshold`), or unknown (rows
exist but scope or termination cannot be established). The Ontario queue
stores `pending`, `done`,
`partial`, `failed`, or `challenged` per region; `done` requires exit 0 with
`pagination_exhausted` or `incremental_complete`, and no other status implies
complete. `challenged` means the site asked for human verification until the
per-region limit and the queue moved on; it is not a failure and it resumes
automatically. The queue exits 3 if any region is left `challenged` or the
total challenge bound ended the run, 4 if regions failed without any
challenged, and 0 otherwise. Dedup reduces the final count without reducing
completeness; logs record both raw and unique counts.

## Failure classification

| Event | `stop_reason` | Queue status | Can listings still be kept? |
|---|---|---|---|
| Documented last page reached, records collected | `pagination_exhausted` | `done` | Yes |
| Pagination terminates with zero records ever collected | `empty_page_threshold` | `partial` | Yes (empty), but not treated as complete |
| `max_pages` cap reached | `max_pages` | `partial` | Yes |
| Human-verification page | `challenge` | retried, then `challenged` | Yes, if earlier pages exist |
| Repeated empty pages (or card-wait timeouts) hit the threshold, or a zero-record ending | `empty_page_threshold` | `partial` | Yes, with a warning |
| Selector drift makes card detection unreliable | `empty_page_threshold` / `browser_error` | `partial` | Only with a warning; treat as unknown |
| Browser or CDP failure | `browser_error` | `partial`, then `failed` after `--error-max-attempts` | Yes, if earlier pages exist |
| Operator interrupt | `stop_requested` | `partial` | Yes; resume from checkpoint |
| Every page fails before records | `browser_error` | `failed` after `--error-max-attempts` | No useful result |

A region only becomes `failed` in the queue after `--error-max-attempts`
non-challenge failures. Challenges never mark a region failed: the queue
pauses between attempts with a cooldown, marks the region `challenged` after
the per-region consecutive limit, and moves on; `--max-total-challenges`
stops the whole invocation at a region boundary, after the in-flight region
has settled, if too many challenges accumulate.

## Deduplication

Prefer stable Zillow listing IDs parsed from card URLs, falling back to the
normalized URL. Within this single source there is no cross-provider merging;
the CSV keeps each unique card once and counts skipped duplicates in the log.
The `records_total` report field is the dedup set size after the run, not the
number of raw cards seen.

## Evidence on disk

- `report.json`: the per-run report (`report_file`; schema in
  `docs/CLI_CONTRACT.md`).
- `checkpoint.json`: `next_url` and `pages_completed`, updated after every
  completed page.
- `error_directory/`: `<stamp>-<label>.png`, `.html`, `.json` diagnostics
  captured on fatal page failures, including the checkpoint.
- `archive_directory/`: `<stamp>-page-NNN.html.gz` raw HTML per successfully
  fetched page. A challenge page is not archived, so the archive shows what
  was actually collected.
- `queue-state.json` and `last-run-summary.json` in the queue state
  directory: per-region status, attempts, challenge waits, and the run's exit
  code.

## Field quality

Coverage and field completeness are different. On current Zillow search
cards, `price`, `beds`, `baths`, `address`, `agent`, `listing_id`, and
`url` are populated from the card markup. Square footage is a known gap:
the card's `[data-testid='property-card-details']` element exposes only a
concatenated bed/bath line (for example `5 bds7 ba`), and there is no
card-level sqft element or testid, so `sqft` comes back empty on
search-page runs until a detail-page fetch exists. That is a coverage
limitation, not a parser failure. Beds and baths are parsed from that
concatenated detail line.

Watch the logs for empty-field warnings and treat a sudden drop in a
field's non-empty rate as a selector regression, not a market change.
Numeric `validation` bounds in the selectors config turn absurd
out-of-range values into empty cells, so a wrong number never reads as a
real one.
