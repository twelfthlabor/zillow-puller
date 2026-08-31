# AGENTS.md

Working notes for AI agents maintaining this Zillow scraper project.

## Goal

Scrape **live Zillow listings from anywhere** — any region — into deduped JSON, surviving PerimeterX anti-bot and Zillow's 24-page-per-search cap.

## Project map

- `zillow_pull.py` — **primary tool.** Tiling + Zillow internal search API via in-page fetch. Whole-region coverage. Region-agnostic: pass any slug/URL via `--region`.
- `zillow_pages.py` — simpler parallel page loader (was `scrape_oakville.py`). Working, but capped at 24 pages / ~984 listings per search.
- `zillow_puller/` — lightweight package wrapper so `python -m zillow_puller` invokes the primary tool.
- `local/` — ignored archive for research experiments, diagnostics, and raw HTML captures; never publish it.
- Outputs: checked-in examples live under `sample data pulls/`; new runs should use a uniquely named output and cache directory.
- Caches: `cache/<region>_pages/` (raw HTML pages), `cache/<slug>/` (per-tile JSON for `zillow_pull.py`), `~/.zillow-puller/profile` (persistent Chromium profile).

## CLI (both scripts are region-agnostic)

```bash
# full coverage — any region slug or full URL
python zillow_pull.py --region toronto-on
python zillow_pull.py --region https://www.zillow.com/austin-tx/
python zillow_pull.py --region new-york-ny --out nyc.json --cache-dir cache/nyc

# simple loader — capped at ~984; default fetcher is 'proxy' (whitelisted-IP, works from any IP)
python zillow_pages.py --region toronto-on --pages 24

# reliable long runs: use your own deployed Apps Script as the proxy
python zillow_pages.py --region toronto-on --proxy-base "https://script.google.com/macros/s/XXX/exec?url={url}"

# instant rerun from cache, no browser
python zillow_pull.py --region toronto-on --cache-only
```

Defaults: output `<slug>_all.json`, cache `cache/<slug>`, profile `~/.zillow-puller/profile`.

## Key technical facts (learned the hard way)

1. **The winning approach (confirmed 2026-08-09): nodriver + a copy of the user's real Chrome profile.** PerimeterX flags automation by *profile fingerprint*, NOT IP reputation. The local IP is clean (the user browses Zillow fine). Fresh/automated profiles (plain requests, curl_cffi, headless Playwright, nodriver with a fresh profile) all get denied; a copy of the real Chrome profile (with trusted `_px3` cookies + history) passes both the page **and** the internal API. This unlocked full-coverage tiling: **8,896 of 8,903 Toronto listings pulled live.** To make a profile copy: close Chrome, then copy `%LOCALAPPDATA%\Google\Chrome\User Data\Default` (only `Network/Cookies*`, `Preferences`, `Secure Preferences`, `History`, `Login Data`, `Web Data`, `Bookmarks`, `Network Persistent State`, `TransportSecurity`) into `~/.zillow-puller/real-profile-copy/Default/`. Use `--profile` to point at it.
2. **PerimeterX blocks:** plain `requests`, `curl_cffi`, and headless Playwright → 403 "Access to this page has been denied". Old note "the working combo is headed persistent Chromium" is superseded by finding #1.
3. **Internal API:** `PUT https://www.zillow.com/async-create-search-page-state` with JSON `{searchQueryState, wants, requestId, isDebugRequest}`. Response: `cat1.searchList.totalResultCount` / `totalPages` / `cat1.searchResults.listResults`. **Important:** a count-only `wants: {"cat2":["total"]}` returns `totalResultCount: 0` — Zillow only returns the total in the full `wants: {"cat1":["listResults","mapResults"],"cat2":["total"]}` request. `tile_search` therefore always issues the full request and reuses page 1 for collection.
4. **Must call the API from inside the page**: via in-page `fetch`. `NodriverPage.evaluate` injects the payload into a JS literal. Calling via an external HTTP client → 403.
5. **`?searchQueryState=` URL params → hard blocked.** Dead end; don't retry.
6. **24-page cap:** `totalPages` from the API is capped (~20–24, ~984 listings) per search regardless of true total. Toronto's true total is 8,903. **Tiling is required** for full coverage.
7. **Rate limits / throttle:** rapid browser launches or request bursts trigger an IP-level throttle that outlasts the session (observed >3 min; likely 10+). After throttle, even a good profile may be denied at warm-up. The `--rest-min`/`--launch-attempts` auto-retry loop rests between relaunches and resumes from the tile cache. Do NOT probe repeatedly — launch once, and if denied, wait (the tool does this automatically). **Mid-run throttle:** if warm-up succeeds but the API starts returning 403, `api_call` counts consecutive fully-blocked queries and raises `Throttled` after `THROTTLE_AFTER` (4); `run_once` then returns False so the rest/relaunch loop takes over. Large regions (e.g. NYC ~20k listings) may need several rest/relaunch cycles; each session caches completed tiles before the next resumes.
8. **`_px3` token expires in ~60s** — keep sessions short-lived and cache everything (per-tile JSON).
9. **PerimeterX challenge does NOT auto-resolve.** The 403 page is a `px-captcha` JS widget. It needs a human to click/hold the widget once, or a fresh launch after the IP has rested. Every browser launch re-triggers the throttle, so don't probe repeatedly. `warm_up` polls ~40s for auto-resolution, then waits up to ~2 min more (`solve_secs`) for a human to solve the widget before backing off — if a captcha appears in the launched window, solve it once.
10. **Google whitelisted-IP proxy (allorigins / Apps Script) is a working fallback** for the HTML-page path, but the free allorigins proxy is flaky (408/500/522 under load; Zillow's ~1.3MB pages exceed its upstream timeout). `zillow_pages.py --fetcher proxy` supports it, and `zillow_fetch_proxy.gs` is a deployable reliable endpoint. Only needed if you can't make a profile copy.
11. **curl_cffi gets an instant `403` + `x-px-blocked: 1` on every Zillow URL** including the homepage (0.1s, no cookies). It's a fake browser with no JS/fingerprint — irrelevant now that the profile approach works.

## How to validate `zillow_pull.py` (the full-coverage path)

Requires a trusted profile copy (see finding #1). The tile cache makes reruns resume instantly, and `--cache-only` merges cached tiles without launching a browser.

```bash
# first full run (or resume):
python zillow_pull.py --region toronto-on --tile-max 450 --delay 3 --max-pages 20 \
    --cache-dir cache/toronto_full --out "sample data pulls/toronto_full.json"

# instant merge of cached tiles, no browser:
python zillow_pull.py --region toronto-on --cache-only --cache-dir cache/toronto_full --out "sample data pulls/toronto_full.json"
```

Watch for, in order:
- warm-up success (no "denied" FATAL). If a px-captcha appears in the browser window, solve it once (the tool now waits up to ~2 min for a manual solve before backing off).
- `region: ... bounds: {...}`
- `box <key>: N listings` — then `collecting` (≤ tile-max) or `splitting` (> tile-max)
- `tile <key> page 1/N: ...` with API status implied by returned results
- `session checked N boxes, M blocked, K fresh this session` then
- `COVERAGE COMPLETE: no boxes blocked this session` — this is the real success signal
- `TOTAL <n> deduped listings` — expect ~8,900 for Toronto, ~20.7k for NYC.

Success criteria (met 2026-08-09): Toronto 8,896 from 45 tiles; NYC 20,755 from 169 boxes/115 tiles, each a single clean run with `COVERAGE COMPLETE` and 0 blocked.

## Mid-run throttle (large regions)

`run_once` counts consecutive blocked API queries (`THROTTLE_AFTER` = 4) and raises `Throttled`, which aborts the session so `run()`'s rest/relaunch loop takes over (default `--rest-min 20`, `--launch-attempts 10`). Cached tiles make each relaunch resume automatically. A run is only reported `COVERAGE COMPLETE` when a session checks every box with zero blocks — do not treat a partial `TOTAL` as done. If the IP is throttled at warm-up, every relaunch hits the challenge page; wait for the throttle to clear (it does, ~30+ min) and relaunch rather than probing repeatedly.

## Workflow conventions

- Never parallelize API calls; keep `--delay ≥ 3`.
- Reruns must hit the cache (fast, no browser).
- When a profile looks flagged, delete `.zillow-puller/profile` rather than reusing it.
- Update `README.md` when behavior/CLI changes.
