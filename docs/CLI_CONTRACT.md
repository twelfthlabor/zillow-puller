# CLI and Package Contract

This is the target public contract. Existing commands remain compatibility
tools until the unified package implements the full contract.

## Installation

```bash
pipx install property-listings
```

Browser support, when a permitted source requires it, should be optional:

```bash
pipx install 'property-listings[browser]'
```

## Primary command

```bash
property-listings get --city Toronto --region ON
```

Default behavior:

- Query all enabled sources suitable for the location.
- Traverse every available page unless the user supplies an explicit limit.
- Normalize and deduplicate records.
- Emit JSON to stdout.
- Put diagnostics and progress on stderr so stdout remains pipeable.
- Include a coverage object in the response.

Useful forms:

```bash
property-listings get --city Toronto --region ON --format ndjson
property-listings get --city Toronto --region ON --format csv --output toronto.csv
property-listings get --city Toronto --region ON --source reso
property-listings get --city Toronto --region ON --require-complete
property-listings get --url 'https://permitted.example/search?city=Toronto'
```

## Python package

```python
from property_listings import get_listings

result = get_listings(
    city="Toronto",
    region="ON",
    require_complete=False,
)

for listing in result.listings:
    print(listing.address, listing.price)

print(result.coverage.status)
```

The synchronous one-call API is required. An async/streaming API may be added
without replacing it.

## Result envelope

```json
{
  "query": {
    "city": "Toronto",
    "region": "ON"
  },
  "listings": [],
  "coverage": {
    "status": "complete",
    "scope": "enabled sources for Toronto, ON",
    "sources_attempted": 2,
    "sources_completed": 2,
    "records_seen": 1200,
    "unique_listings": 1044,
    "duplicates_removed": 156,
    "warnings": []
  },
  "generated_at": "2026-08-04T00:00:00Z"
}
```

Coverage status is one of:

- `complete`: every selected source reconciled and exhausted its query scope.
- `partial`: useful listings were returned but at least one selected source did
  not complete.
- `empty`: selected sources completed successfully and returned no listings.
- `unknown`: listings may exist, but the package cannot establish source scope
  or termination well enough to claim completeness.

## Exit behavior

- `0`: command succeeded; partial results are allowed unless
  `--require-complete` was set.
- `2`: invalid arguments or configuration.
- `3`: no source is configured or permitted for the query.
- `4`: `--require-complete` was requested and coverage was not complete.
- `5`: every selected source failed and no useful result could be returned.

## Optional HTTP interface

```bash
property-listings serve --host 127.0.0.1 --port 8000
```

The HTTP interface must call the same engine and expose the same listing and
coverage semantics. It is not the primary product.

## Current compatibility commands

- `property-scraper`: generic Selenium collector.
- `property-scraper-sb`: SeleniumBase fixture/authorized-browser collector.
- `property-reso`: RESO/OData ingestion.
- `property-api`: HTTP server over an existing CSV snapshot.

Future implementations should converge these behind `property-listings` while
keeping compatibility wrappers until migration is tested.

