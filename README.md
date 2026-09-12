# Property Listings

Property Listings is intended to become an open-source terminal command and
Python package that returns normalized property listings from one call.

## Target experience

Install once:

```bash
pipx install property-listings
```

Get listings:

```bash
property-listings get --city Toronto --region ON
```

Or call the package:

```python
from property_listings import get_listings

result = get_listings(city="Toronto", region="ON")
```

The command should handle source selection, pagination, normalization,
deduplication, and coverage reporting. Results must say whether collection was
complete, partial, empty, or of unknown completeness. A first-page sample must
never be presented as all listings.

The unified `property-listings` command above is the product contract; it is not
fully implemented yet.

## Current building blocks

| Command | Current purpose |
|---|---|
| `property-scraper` | Generic Selenium collection for permitted sites |
| `property-scraper-sb` | SeleniumBase fixtures and explicitly authorized sites |
| `property-reso` | Authorized RESO/OData feed ingestion |
| `property-api` | Local HTTP access to a collected CSV snapshot |

These commands are compatibility tools. Future work should converge them behind
the single interface documented in [docs/CLI_CONTRACT.md](docs/CLI_CONTRACT.md).

## Development setup

```bash
cd /Users/daniel/Desktop/repo/property-scraper
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pytest -q -p no:rerunfailures
```

Example configurations:

- `config.example.json` — generic browser source.
- `config.seleniumbase.example.json` — controlled SeleniumBase fixture.
- `config.reso.example.json` — authorized RESO/OData provider.

## Documentation

- [Product vision](docs/PRODUCT_VISION.md)
- [CLI and package contract](docs/CLI_CONTRACT.md)
- [Source and coverage model](docs/SOURCE_COVERAGE.md)
- [Coding-agent instructions](AGENTS.md)

## Licence and data rights

The software is MIT licensed. Listing data retains the licence and access terms
of its source. Research or educational use does not automatically grant access
or redistribution rights, and the project does not include access-control
evasion.

