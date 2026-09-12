# Project Instructions for Coding Agents

## Product intent

The primary product is a public, installable command/package that returns
property listings from one call. Toronto is the first proving ground.

Target command after one-time installation:

```bash
property-listings get --city Toronto --region ON
```

Target Python call:

```python
result = property_listings.get_listings(city="Toronto", region="ON")
```

The local HTTP server is an optional interface over the same engine. Do not
mistake "API" to mean that the project should primarily be a server, a CSV
viewer, or a credential-specific RESO client.

## What future work should optimize for

1. One obvious package and one primary executable: `property-listings`.
2. Useful defaults. A basic city query must not require users to edit selectors
   or understand the internal source adapters.
3. Source adapters behind one stable query/result contract.
4. Exhaust every available page, deduplicate across pages and sources, and
   return coverage evidence with the listings.
5. Make partial results unmistakable. Never label a stopped, challenged,
   sampled, capped, or selector-broken run as complete.
6. Keep installation lightweight. Browser dependencies should be an optional
   extra when non-browser sources do not need them.
7. Preserve a simple research workflow: JSON/NDJSON to stdout, CSV/JSON files
   on request, and a Python library returning typed data.

## Meaning of "all listings"

"All" is the product ambition, not permission to invent a total. A result is
complete only for explicitly named sources and query scope after pagination is
exhausted and source totals or termination conditions reconcile. The response
must say which sources were attempted, which completed, and what remains
unknown. `--require-complete` should make incomplete coverage a nonzero exit.

The long-term route to broader market coverage is multiple compliant source
adapters plus cross-source deduplication, not exposing one source-specific
script as the final product.

## Current repository and migration direction

The repository currently has several top-level scripts:

- `property_scraper.py`: generic Selenium collector.
- `seleniumbase_scraper.py`: controlled-fixture/authorized-browser collector.
- `reso_ingest.py`: authorized RESO/OData ingestion.
- `listing_api.py`: local read-only HTTP server over CSV.

These are implementation pieces, not the desired public interface. Evolve them
toward a package such as:

```text
src/property_listings/
  cli.py
  client.py
  models.py
  coverage.py
  sources/
    base.py
    reso.py
    browser.py
```

Keep old commands as compatibility wrappers during migration. Do not remove a
working path until the unified command covers it.

## Boundaries

- Do not add CAPTCHA solving, challenge bypass, fingerprint suppression,
  identity rotation, or other access-control evasion.
- Do not claim that research or education changes a source's access or data
  licence.
- Do not let those constraints erase the product goal. Continue improving the
  one-call package, permitted sources, multi-source coverage, result quality,
  and transparent completeness reporting.
- Do not pivot the project into documentation-only architecture. Each build
  task should leave the runnable one-call experience closer to the contract in
  `docs/CLI_CONTRACT.md`.

## Documentation map

- `docs/PRODUCT_VISION.md`: intended user experience and success criteria.
- `docs/CLI_CONTRACT.md`: stable target CLI/library contract.
- `docs/SOURCE_COVERAGE.md`: source adapters and completeness semantics.
- `README.md`: installation, current capabilities, and user entry point.

