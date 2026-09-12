# Source and Coverage Model

## Why coverage is part of the API

The project is intended to retrieve all listings available for a query, not a
silent first-page sample. Different sources expose different totals, pagination,
permissions, and failure modes. Therefore coverage is returned alongside the
listings instead of being left as a log message.

## Source adapter contract

Each adapter should provide:

1. `supports(query)` — whether the source can serve the requested location and
   listing type.
2. `collect(query, cursor)` — one page or stream segment plus the next cursor.
3. `normalize(record)` — conversion to the shared listing schema.
4. `coverage()` — observed totals, pages, termination reason, and warnings.
5. `provenance()` — source name, timestamp, licence/terms reference, and adapter
   version without exposing credentials.

Adapters may represent:

- Public or open-data sources with no credentials.
- Authorized feeds or APIs using user-supplied environment credentials.
- Browser collection from sources that permit automation.
- Local fixtures or user-supplied exports for reproducible research.

## Completeness rules

An adapter is complete for a query only when all applicable conditions hold:

- Pagination ended through a documented terminal condition.
- Any advertised total reconciles with records seen, after accounting for
  documented filtering.
- No page cap, time cap, challenge, authentication error, or transient failure
  stopped the run.
- Required selectors or fields did not collapse in a way that invalidates the
  result set.
- The adapter can name its exact source and query scope.

Cross-source coverage is complete only when every selected adapter is complete.
Deduplication can reduce the final count without reducing completeness; the
manifest records both raw and unique counts.

## Failure classification

| Event | Adapter status | Can listings still be returned? |
|---|---|---|
| Documented last page reached | `complete` | Yes |
| Valid query returns zero and terminates | `empty` | Yes |
| Page or record limit reached | `partial` | Yes |
| Human-verification page | `partial` | Yes, if earlier pages exist |
| One source fails while another completes | `partial` | Yes |
| Selector drift makes card detection unreliable | `unknown` | Only with a warning |
| All sources fail before records | `failed` internally | No useful result |

## Deduplication

Prefer stable provider listing IDs. Across providers, use conservative matching
based on normalized address, unit, listing type, brokerage identifier, and
price/time proximity. Keep source-specific IDs and provenance even when records
merge so researchers can audit the decision.

## Field quality

Coverage and field completeness are different. A source can expose every card
while beds or square footage fail to parse. Every result should report non-empty
rates for core fields and flag suspicious values or sudden schema regressions.

## Current Toronto evidence

The existing Zillow browser audit collected 41 unique first-page records and
then encountered human verification on page 2. That run is useful evidence and
a partial adapter result; it is not complete Toronto coverage. Future models
must preserve that distinction while continuing to improve the unified package.

