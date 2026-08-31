import argparse
import asyncio
import json
import os
import random
import re
import sys
import time

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)
DENIED_RE = re.compile(r"Access to this page has been denied")

# Consecutive fully-blocked API queries (after per-query retries/backoff) that
# indicate the session is throttled. Abort the session so run() rests and
# relaunches; the tile cache resumes where it left off.
THROTTLE_AFTER = 4


class Throttled(Exception):
    pass

FIELD_MAP = {
    "address": "address",
    "price": "price",
    "beds": "beds",
    "baths": "baths",
    "sqft": "area",
    "type": lambda r, h: h.get("homeType"),
    "listing_url": "detailUrl",
    "zestimate": lambda r, h: h.get("zestimate"),
    "days_on_zillow": lambda r, h: h.get("daysOnZillow"),
    "lat": lambda r, h: (h.get("latitude") or (r.get("latLong") or {}).get("latitude")),
    "lon": lambda r, h: (h.get("longitude") or (r.get("latLong") or {}).get("longitude")),
}

# Playwright evaluate interface: evaluate(js, payload_str) -> {"status": int, "text": str}
FETCH_JS = """
async (payloadStr) => {
  const resp = await fetch('/async-create-search-page-state', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json', 'Accept': '*/*'},
    body: payloadStr,
  });
  const text = await resp.text();
  return {status: resp.status, text: text};
}
"""

# nodriver evaluate interface: evaluate(expression) -> raw JSON string.
# The payload is injected as a quoted JSON literal.
FETCH_JS_NODRIVER = """
(async () => {
  const payloadStr = __PAYLOAD__;
  try {
    const resp = await fetch('/async-create-search-page-state', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json', 'Accept': '*/*'},
      body: payloadStr,
    });
    const text = await resp.text();
    return JSON.stringify({status: resp.status, text: text});
  } catch (err) {
    return JSON.stringify({status: 0, text: String(err)});
  }
})()
"""


class NodriverPage:
    """Thin adapter so the pipeline (api_call/query_tile/tile_search) runs on nodriver.

    Mimics the small subset of Playwright's Page API that zillow_pull.py uses:
    goto / wait_for_timeout / title / content / evaluate / close.
    """

    def __init__(self, browser, tab):
        self.browser = browser
        self.tab = tab
        self.blocks = 0
        self.boxes_checked = 0
        self.boxes_blocked = 0

    async def goto(self, url, wait_until=None, timeout=None):
        self.tab = await self.browser.get(url, new_tab=False)
        return self

    async def wait_for_timeout(self, ms):
        await self.tab.sleep(ms / 1000)

    async def title(self):
        try:
            return await self.tab.evaluate("document.title", return_by_value=True) or ""
        except Exception:
            return ""

    async def content(self):
        try:
            return await self.tab.get_content()
        except Exception:
            return ""

    async def evaluate(self, js, payload_str=None):
        if payload_str is None:
            return await self.tab.evaluate(js, await_promise=True, return_by_value=True)
        expr = FETCH_JS_NODRIVER.replace("__PAYLOAD__", json.dumps(payload_str))
        raw = await self.tab.evaluate(expr, await_promise=True, return_by_value=True)
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw) if raw else None
        except (json.JSONDecodeError, TypeError):
            return {"status": 0, "text": str(raw)}

    async def close(self):
        try:
            await self.tab.close()
        except Exception:
            pass


async def make_page(args):
    import nodriver as uc
    browser = await uc.start(user_data_dir=args.profile, headless=False)
    tab = await browser.get(args.url)
    page = NodriverPage(browser, tab)
    return browser, page


def parse_region(html):
    m = NEXT_DATA_RE.search(html)
    if not m:
        return None
    data = json.loads(m.group(1))
    ss = data.get("props", {}).get("pageProps", {}).get("searchPageState") or {}
    qs = ss.get("queryState") or {}
    region = (qs.get("regionSelection") or [{}])[0]
    bounds = qs.get("mapBounds")
    term = qs.get("usersSearchTerm") or ss.get("usersSearchTerm") or ""
    return {
        "regionSelection": [region] if region.get("regionId") else [],
        "mapBounds": bounds,
        "usersSearchTerm": term,
    }


def tile_key(bounds):
    return ",".join(f"{bounds[k]:.4f}" for k in ("west", "east", "south", "north"))


def split_tile(bounds):
    west, east, south, north = bounds["west"], bounds["east"], bounds["south"], bounds["north"]
    mid_lon = (west + east) / 2
    mid_lat = (north + south) / 2
    return [
        {"west": west, "east": mid_lon, "south": mid_lat, "north": north},
        {"west": mid_lon, "east": east, "south": mid_lat, "north": north},
        {"west": west, "east": mid_lon, "south": south, "north": mid_lat},
        {"west": mid_lon, "east": east, "south": south, "north": mid_lat},
    ]


def build_payload(region, bounds, page_num, want_results):
    sqs = {
        "pagination": {"currentPage": page_num} if page_num > 1 else {},
        "usersSearchTerm": region.get("usersSearchTerm", ""),
        "mapBounds": bounds,
        "regionSelection": region.get("regionSelection", []),
        "filterState": {"sortSelection": {"value": "globalrelevanceex"}},
        "isMapVisible": True,
        "isListVisible": True,
    }
    wants = {"cat1": ["listResults", "mapResults"], "cat2": ["total"]} if want_results \
        else {"cat2": ["total"]}
    return {
        "searchQueryState": sqs,
        "wants": wants,
        "requestId": random.randint(2, 9999),
        "isDebugRequest": False,
    }


async def api_call(page, payload, retries=3, base_backoff=8, timeout=30):
    for attempt in range(retries):
        try:
            res = await asyncio.wait_for(
                page.evaluate(FETCH_JS, json.dumps(payload)), timeout=timeout
            )
        except asyncio.TimeoutError:
            print(f"API call timed out after {timeout}s, backing off", file=sys.stderr)
            res = None
        if res is not None and res["status"] == 200:
            try:
                return json.loads(res["text"])
            except json.JSONDecodeError:
                return None
        wait = base_backoff * (attempt + 1)
        status = res["status"] if res is not None else "timeout"
        print(f"API blocked ({status}), backing off {wait}s", file=sys.stderr)
        await asyncio.sleep(wait)
    page.blocks += 1
    if page.blocks >= THROTTLE_AFTER:
        print("throttled mid-run; aborting session so run() rests and relaunches",
              file=sys.stderr)
        raise Throttled()
    return None


async def query_tile(page, region, bounds, page_num, want_results, delay):
    data = await api_call(page, build_payload(region, bounds, page_num, want_results))
    await asyncio.sleep(delay + random.uniform(0, 1.0))
    if data is None:
        return None
    cat1 = data.get("cat1") or {}
    sl = cat1.get("searchList") or {}
    total = sl.get("totalResultCount")
    total_pages = sl.get("totalPages")
    if total is None:
        total = (data.get("cat2") or {}).get("total", {}).get("totalResultCount")
    if total is None:
        total = (data.get("categoryTotals") or {}).get("cat1", {}).get("totalResultCount")
    if total is None:
        total = (data.get("categoryTotals") or {}).get("cat2", {}).get("totalResultCount")
    if total_pages is None:
        total_pages = (data.get("categoryTotals") or {}).get("cat1", {}).get("totalPages")
    results = (cat1.get("searchResults") or {}).get("listResults") or []
    return {"total": total, "totalPages": total_pages, "results": results}


def extract(record, tile):
    h = record.get("hdpData", {}).get("homeInfo", {})
    rec = {}
    for k, v in FIELD_MAP.items():
        rec[k] = v(record, h) if callable(v) else record.get(v)
    rec["zpid"] = record.get("zpid")
    rec["tile"] = tile
    return rec


async def collect_tile(page, region, bounds, delay, max_pages, out, seen, cache_path,
                       first=None):
    key = tile_key(bounds)
    cached = None
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        added = 0
        for r in cached:
            zpid = r.get("zpid")
            if zpid and zpid not in seen:
                seen.add(zpid)
                out.append(r)
                added += 1
        print(f"  tile {key}: loaded {added} from cache", file=sys.stderr)
        return

    first = first if first is not None else await query_tile(page, region, bounds, 1, True, delay)
    if first is None or first["results"] is None:
        print(f"  tile {key}: API blocked/empty", file=sys.stderr)
        return
    pages = max(1, min(first["totalPages"] or 1, max_pages))
    collected = []
    for p in range(1, pages + 1):
        q = first if p == 1 else await query_tile(page, region, bounds, p, True, delay)
        if q is None:
            print(f"  tile {key} page {p}: blocked", file=sys.stderr)
            await asyncio.sleep(5)
            continue
        for r in q["results"]:
            zpid = r.get("zpid")
            if zpid and zpid in seen:
                continue
            if zpid:
                seen.add(zpid)
            collected.append(extract(r, key))
        print(f"  tile {key} page {p}/{pages}: {len(q['results'])} (total {len(collected)})",
              file=sys.stderr)
    if cache_path and collected:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(collected, f, ensure_ascii=False)
    out.extend(collected)


async def tile_search(page, region, bounds, delay, max_pages, tile_max, out, seen,
                      cache_dir, depth):
    key = tile_key(bounds)
    cp = os.path.join(cache_dir, f"tile_{key}.json") if cache_dir else None
    page.boxes_checked += 1
    if cp and os.path.exists(cp):
        print(f"  box {key}: tile cached, loading", file=sys.stderr)
        await collect_tile(page, region, bounds, delay, max_pages, out, seen, cp)
        return
    # Zillow's API returns totalResultCount: 0 for count-only wants; the total is only
    # present in the full listResults request. So always request results for the count.
    count = await query_tile(page, region, bounds, 1, True, delay)
    if count is None:
        print(f"  box {key}: API blocked", file=sys.stderr)
        page.boxes_blocked += 1
        return
    total = count["total"] or 0
    if total == 0:
        return
    if total <= tile_max or depth >= 8:
        print(f"  box {key}: {total} listings, collecting", file=sys.stderr)
        # pass the already-fetched first page so collect_tile doesn't re-query it
        await collect_tile(page, region, bounds, delay, max_pages, out, seen, cp,
                           first=count)
    else:
        print(f"  box {key}: {total} listings (over {tile_max}), splitting", file=sys.stderr)
        for sub in split_tile(bounds):
            await tile_search(page, region, sub, delay, max_pages, tile_max, out, seen,
                              cache_dir, depth + 1)


def load_cached(cache_dir):
    out, seen = [], set()
    for fn in sorted(os.listdir(cache_dir)):
        if not (fn.startswith("tile_") and fn.endswith(".json")):
            continue
        with open(os.path.join(cache_dir, fn), encoding="utf-8") as f:
            for r in json.load(f):
                if r.get("zpid") and r["zpid"] in seen:
                    continue
                if r.get("zpid"):
                    seen.add(r["zpid"])
                out.append(r)
    return out


async def warm_up(page, url, attempts=3, poll_secs=40, solve_secs=120):
    for attempt in range(1, attempts + 1):
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print(f"warm-up goto attempt {attempt} failed: {e!r}", file=sys.stderr)
            await asyncio.sleep(8 * attempt)
            continue
        # Phase 1: quick poll — the px challenge sometimes auto-resolves in seconds.
        for _ in range(int(poll_secs / 2)):
            await page.wait_for_timeout(2000)
            title = await page.title()
            content = await page.content()
            if DENIED_RE.search(title) or DENIED_RE.search(content):
                continue
            if NEXT_DATA_RE.search(content):
                print(f"warm-up attempt {attempt}: OK after challenge", file=sys.stderr)
                return content
        # Phase 2: if still challenged, give a human time to click/hold the
        # px-captcha widget ("solve it once"), since it does not auto-resolve.
        for _ in range(int(solve_secs / 5)):
            await page.wait_for_timeout(5000)
            title = await page.title()
            content = await page.content()
            if DENIED_RE.search(title) or DENIED_RE.search(content):
                print(f"warm-up attempt {attempt}: waiting for manual solve...",
                      file=sys.stderr)
                continue
            if NEXT_DATA_RE.search(content):
                print(f"warm-up attempt {attempt}: OK after manual solve",
                      file=sys.stderr)
                return content
        print(f"warm-up attempt {attempt}: still denied, backing off", file=sys.stderr)
        await asyncio.sleep(15 * attempt)
    return None


async def run_once(args):
    os.makedirs(args.profile, exist_ok=True)
    if args.cache_dir:
        os.makedirs(args.cache_dir, exist_ok=True)

    browser, page = await make_page(args)
    try:
        print(f"warming up {args.url}", file=sys.stderr)
        print("if a captcha appears in the browser window, solve it once", file=sys.stderr)
        html = await warm_up(page, args.url)
        if html is None:
            print("FATAL: denied at warm-up after retries. Session throttled.", file=sys.stderr)
            return False
        region = parse_region(html)
        if not region or not region["mapBounds"]:
            print("FATAL: could not read region/bounds from page", file=sys.stderr)
            return False
        print(f"region: {region['usersSearchTerm']} bounds: {region['mapBounds']}",
              file=sys.stderr)

        out, seen = [], set()
        try:
            await tile_search(page, region, region["mapBounds"], args.delay, args.max_pages,
                              args.tile_max, out, seen, args.cache_dir, 0)
        except Throttled:
            print("session aborted (throttled); cache has partial tiles, run() will rest "
                  "and resume", file=sys.stderr)
            return False
        print(f"session checked {page.boxes_checked} boxes, "
              f"{page.boxes_blocked} blocked, {len(out)} fresh this session",
              file=sys.stderr)
        if page.boxes_blocked == 0:
            print("COVERAGE COMPLETE: no boxes blocked this session", file=sys.stderr)
        return True
    finally:
        browser.stop()


async def run(args):
    start = time.perf_counter()
    if args.cache_only:
        if not args.cache_dir or not os.path.isdir(args.cache_dir):
            print("FATAL: --cache-only requires an existing --cache-dir", file=sys.stderr)
            sys.exit(1)
        out = load_cached(args.cache_dir)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        elapsed = time.perf_counter() - start
        print(f"TOTAL {len(out)} deduped listings written to {args.out} from cache "
              f"in {elapsed:.1f}s", file=sys.stderr)
        return

    attempt = 0
    while True:
        attempt += 1
        ok = await run_once(args)
        if ok:
            break
        if args.launch_attempts and attempt >= args.launch_attempts:
            print("GIVING UP: too many throttled launches.", file=sys.stderr)
            break
        print(f"throttled/denied; resting {args.rest_min} min before relaunch "
              f"(attempt {attempt})", file=sys.stderr)
        await asyncio.sleep(args.rest_min * 60)

    out = load_cached(args.cache_dir) if args.cache_dir else []
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    elapsed = time.perf_counter() - start
    print(f"TOTAL {len(out)} deduped listings written to {args.out} in {elapsed/60:.1f} min",
          file=sys.stderr)


def slug_from_url(url):
    parts = url.rstrip("/").split("/")
    for p in reversed(parts):
        if p and p not in ("homes", "for_sale", "for_rent", "b", "maps"):
            return p
    return "zillow"


def normalize_url(arg):
    arg = arg.strip()
    if arg.startswith(("http://", "https://")):
        return arg.rstrip("/")
    return f"https://www.zillow.com/{arg.lstrip('/').rstrip('/')}/"


def main():
    ap = argparse.ArgumentParser(
        description="Pull ALL Zillow listings for any region using the internal search API + square tiling.")
    ap.add_argument("--region", required=True,
                    help="Zillow region, e.g. 'toronto-on' or 'new-york-ny', or a full region page URL")
    ap.add_argument("--tile-max", type=int, default=450,
                    help="split tiles with more than this many listings")
    ap.add_argument("--max-pages", type=int, default=20,
                    help="max result pages to pull per tile")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="seconds between API calls")
    ap.add_argument("--out", default=None,
                    help="output JSON path (default: {slug}_all.json)")
    ap.add_argument("--cache-dir", default=None,
                    help="dir to cache per-tile results and resume (default: cache/{slug})")
    ap.add_argument("--cache-only", action="store_true",
                    help="merge cached tiles into --out without launching a browser")
    ap.add_argument("--profile", default=os.path.join(os.path.expanduser("~"),
                                                     ".zillow-puller", "real-profile-copy"),
                    help="Chrome profile dir to drive (default: ~/.zillow-puller/real-profile-copy, "
                         "a copy of your real Chrome profile that PerimeterX trusts). "
                         "Point it at your own profile copy for other machines.")
    ap.add_argument("--rest-min", type=float, default=15.0,
                    help="minutes to rest between relaunch attempts when throttled")
    ap.add_argument("--launch-attempts", type=int, default=8,
                    help="max browser relaunch attempts before giving up (throttle recovery)")
    args = ap.parse_args()
    args.url = normalize_url(args.region)
    slug = slug_from_url(args.url)
    if not args.out:
        args.out = f"{slug}_all.json"
    if not args.cache_dir:
        args.cache_dir = os.path.join("cache", slug)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
