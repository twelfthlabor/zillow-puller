# Zillow Region Puller

Collect public Zillow listings by region and save them as JSON. This is a small
research tool, not an official Zillow feed.

**Research use only.** Use it at low volume and only where you have permission
to collect the data. Follow Zillow's terms, privacy rules, and local law. Do not
use the results for housing, lending, insurance, employment, surveillance,
profiling, or unsolicited contact. Stop if Zillow blocks access; this project
does not bypass captchas or access controls.

## Install

Requires Python 3.10+, Google Chrome, and a Chrome profile that has visited
Zillow.

```bash
pip install -e .
```

Make a copy of the profile once, with Chrome closed:

```bash
python setup_profile.py
```

The copy is stored at `~/.zillow-puller/real-profile-copy` by default. Keep it
outside this repository.

## Pull listings

The main command tiles a region so it can collect more than one search page.

```bash
python zillow_pull.py --region ottawa-on \
  --cache-dir cache/ottawa --out "sample data pulls/ottawa.json"
```

The same command is available after installation:

```bash
zillow-puller --region toronto-on
```

Rerun from an existing cache without opening Chrome:

```bash
python zillow_pull.py --region ottawa-on --cache-only \
  --cache-dir cache/ottawa --out "sample data pulls/ottawa.json"
```

Use a Zillow region slug such as `toronto-on` or `new-york-ny`, or pass a full
region URL. Keep `--delay` at 3 seconds or higher. A visible browser window may
ask for a manual challenge.

`zillow_pages.py` is a simpler page-based collector. It is capped at Zillow's
public search-page limit and supports the optional proxy helper in
`zillow_fetch_proxy.gs`.

## Repository layout

- `zillow_pull.py` - primary tiled collector.
- `zillow_pages.py` - page-based collector.
- `setup_profile.py` - creates the local Chrome profile copy.
- `tests/test_pipeline.py` - offline tests for the primary collector.
- `sample data pulls/` - example JSON results from research runs.
- `cache/` - local tile cache; ignored by Git.
- `local/` - old experiments and raw captures; ignored by Git and never publish.

Each listing includes its Zillow ID, address, price, beds, baths, property type,
listing URL, and coordinates when Zillow provides them. Results can be stale,
duplicated across tiles, or missing properties at tile edges. Verify records
against the live source before using them.

## Tests

```bash
python -m pytest
```

The sample files are public listing data and are included for parser and output
examples only. They are not a complete or authoritative market dataset.
