# Product Vision

## The idea

Property Listings should be an open-source terminal command and Python package
for research and education. After installing it once, a person asks for a
location and receives normalized listings from one call.

```bash
property-listings get --city Toronto --region ON
```

```python
from property_listings import get_listings

result = get_listings(city="Toronto", region="ON")
```

Users should not need to choose between Selenium, RESO, CSV checkpoints, or a
local HTTP server for an ordinary query. Those are source and delivery details
behind the package.

## Intended users

- Students exploring housing availability and pricing.
- Researchers building reproducible datasets and analyses.
- Developers who need normalized listing data without rebuilding pagination,
  deduplication, and source adapters.
- Data providers who want their permitted feed available through a common open
  client.

## Product principles

### One call first

The default path should return or stream listings immediately. Advanced source
configuration remains available, but it must not dominate the basic experience.

### Listings plus evidence

A listing collection without coverage evidence is not enough. Every result must
include source names, collection time, pages traversed, deduplication counts,
field-quality warnings, and completeness status.

### Multiple sources, one schema

The package should normalize permitted browser sources, public datasets,
authorized APIs, feeds, and user-supplied fixtures through adapters. The public
result schema stays stable while adapters evolve independently.

### Honest completeness

The package aims to collect all listings available to the configured sources.
It must never turn a page-one sample into an "all Toronto listings" claim. A
challenge, page cap, failed source, missing result total, or selector failure
produces a partial or unknown status.

### Research-friendly output

JSON should be the default machine-readable output. NDJSON supports streaming;
CSV is an export option. Each run should be reproducible from its query,
provider versions, timestamps, and coverage manifest.

## Success criteria

The first meaningful release is successful when:

1. `pipx install property-listings` installs one executable.
2. `property-listings get --city Toronto --region ON` is the primary workflow.
3. The same operation is available as one Python function call.
4. At least one real source adapter can complete an end-to-end query.
5. Pagination, retries, deduplication, normalized fields, and coverage reporting
   are shared infrastructure rather than separate scripts.
6. `--require-complete` reliably distinguishes complete source coverage from a
   partial dataset.
7. Optional `property-listings serve` exposes the same results over HTTP.

## Non-goals

- A server-only API that requires a separate manual scrape before every use.
- A collection of unrelated commands requiring users to understand internals.
- Claiming universal market coverage from a single partial source.
- Shipping access-control evasion as a collection strategy.

