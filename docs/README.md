# Documentation

Read these documents in order:

1. [Product vision](PRODUCT_VISION.md) — the Zillow-first collector, the
   Ontario queue, and their success criteria.
2. [CLI contract](CLI_CONTRACT.md) — the actual `property-scraper`,
   `property-ontario`, and `property-api` interface, config keys, run report
   schema, and exit codes.
3. [Source and coverage model](SOURCE_COVERAGE.md) — how a run reports
   complete, partial, empty, or unknown coverage through `stop_reason`.

Repository-level coding-agent instructions are in [AGENTS.md](../AGENTS.md).
The root [README](../README.md) remains the user-facing installation and
current capabilities guide.

## Status language

- **Current** means a command or behavior that exists and is tested today.
- **Done** is the queue status for a region whose scraper exited 0 with
  `stop_reason` `pagination_exhausted` (at least one record collected) or
  `incremental_complete`; `done` regions are skipped on later runs.
- **Partial** means a run returned useful rows but did not finish its scope
  (cap, empty-page stop, zero-record pagination ending, interrupt, or
  missing report); partial regions are attempted again on the next queue
  run.
- **Challenged** means the site asked for human verification until the
  region hit its consecutive-challenge limit; the queue moved on and the
  region resumes automatically on a later invocation (it is not a failure).
- **Pending** means a region is queued but not yet attempted (or was reset
  by `--retry-failed`/`--reopen` for a retry).
- **Failed** means non-challenge errors exceeded the queue's attempt limit;
  failed regions are skipped until `--retry-failed` or `--reopen`.
- **Complete** means the run exhausted its configured scope through a
  documented terminal condition — never a sampled or capped run.
