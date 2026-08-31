import argparse
import asyncio
import json
import os
import re
import sys
import time
from urllib.parse import quote

from playwright.async_api import async_playwright

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

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)
DENIED_RE = re.compile(r"Access to this page has been denied")


def parse_search_state(html):
    m = NEXT_DATA_RE.search(html)
    if not m:
        return None
    data = json.loads(m.group(1))
    ss = data.get("props", {}).get("pageProps", {}).get("searchPageState") or {}
    cat1 = ss.get("cat1") or {}
    sr = cat1.get("searchResults") or {}
    totals = (ss.get("categoryTotals") or {}).get("cat1") or {}
    return {
        "totalPages": ss.get("totalPages") or (totals.get("totalPages") if totals else None),
        "totalResultCount": totals.get("totalResultCount"),
        "listResults": sr.get("listResults") or [],
    }


def parse_page(html):
    state = parse_search_state(html)
    return state["listResults"] if state is not None else None


def extract(record, page):
    h = record.get("hdpData", {}).get("homeInfo", {})
    rec = {}
    for k, v in FIELD_MAP.items():
        rec[k] = v(record, h) if callable(v) else record.get(v)
    rec["zpid"] = record.get("zpid")
    rec["page"] = page
    return rec


async def warm_up(context, url):
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for _ in range(20):
            title = await page.title()
            content = await page.content()
            if not DENIED_RE.search(title) and NEXT_DATA_RE.search(content):
                return content
            await page.wait_for_timeout(2000)
        return None
    finally:
        await page.close()


async def fetch_page(sem, context, url, page_no, cache_path, retries=3, delay=1.0):
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8", errors="replace") as f:
            return page_no, f.read(), "cache"
    async with sem:
        for attempt in range(retries):
            if delay:
                await asyncio.sleep(delay)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                html = await page.content()
            except Exception as e:
                print(f"page {page_no} attempt {attempt+1} failed: {e!r}", file=sys.stderr)
                await asyncio.sleep(3)
                continue
            finally:
                await page.close()
            if DENIED_RE.search(html):
                print(f"page {page_no} attempt {attempt+1} hit captcha block, backing off",
                      file=sys.stderr)
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if cache_path:
                with open(cache_path, "w", encoding="utf-8") as f:
                    f.write(html)
            return page_no, html, "http"
    return page_no, None, "denied/failed"


def fetch_via_proxy(url, proxy_base, retries=5, delay=2.0, timeout=90):
    """Fetch a page through a whitelisted-IP proxy (e.g. Google Apps Script / allorigins).

    The local IP is hard-blocked by PerimeterX, but Google server IPs are whitelisted
    by Zillow, so proxying through them returns real 200 pages with __NEXT_DATA__.
    """
    from curl_cffi import requests as cr

    encoded = quote(url, safe="")
    proxy_url = proxy_base.format(url=encoded)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
    for attempt in range(retries):
        try:
            r = cr.get(proxy_url, impersonate="chrome", headers=headers, timeout=timeout)
        except Exception as e:
            print(f"proxy fetch attempt {attempt+1} failed: {e!r}", file=sys.stderr)
            time.sleep(min(30, 5 * 2 ** attempt))
            continue
        if r.status_code != 200:
            print(f"proxy fetch attempt {attempt+1}: status {r.status_code}", file=sys.stderr)
            time.sleep(min(30, 5 * 2 ** attempt))
            continue
        if DENIED_RE.search(r.text):
            print(f"proxy fetch attempt {attempt+1}: still blocked", file=sys.stderr)
            time.sleep(min(30, 5 * 2 ** attempt))
            continue
        return r.text
    return None


def run_proxy(args):
    """Fetch pages synchronously through a whitelisted-IP proxy (no browser)."""
    from curl_cffi import requests as cr

    os.makedirs(args.cache_dir, exist_ok=True)

    def cache_path(page):
        return os.path.join(args.cache_dir, f"page_{page:03d}.html")

    start = time.perf_counter()
    pages = list(range(args.start, args.start + args.pages))
    first_url = args.url_template.format(page=args.start)

    print(f"probing {first_url} via proxy ({args.proxy_base})", file=sys.stderr)
    warm = fetch_via_proxy(first_url, args.proxy_base, retries=args.retries, delay=args.delay)
    if warm is None:
        print("FATAL: proxy could not fetch the first page. Check the proxy or try again later.",
              file=sys.stderr)
        sys.exit(1)
    state = parse_search_state(warm)
    if state:
        print(f"server reports {state['totalResultCount']} listings across {state['totalPages']} pages",
              file=sys.stderr)
        if state["totalPages"] and args.pages > state["totalPages"]:
            print(f"capping --pages from {args.pages} to {state['totalPages']}", file=sys.stderr)
            pages = [p for p in pages if p <= state["totalPages"]]

    urls = {p: args.url_template.format(page=p) for p in pages}
    print(f"fetching {len(urls)} pages via proxy (delay={args.delay}s)", file=sys.stderr)

    results = []
    for p in pages:
        path = cache_path(p)
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                results.append((p, f.read(), "cache"))
            continue
        html = fetch_via_proxy(urls[p], args.proxy_base, retries=args.retries, delay=args.delay)
        if html is None:
            print(f"FAILED page {p}", file=sys.stderr)
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        results.append((p, html, "proxy"))
        pl = parse_page(html)
        print(f"page {p}: {len(pl) if pl else 0} listings (proxy)", file=sys.stderr)

    page_results = {}
    for page, html, src in results:
        list_results = parse_page(html)
        if list_results is None:
            print(f"NO NEXT_DATA page {page} ({src})", file=sys.stderr)
            continue
        page_results[page] = list_results

    seen = set()
    out = []
    for page in sorted(page_results):
        for r in page_results[page]:
            zpid = r.get("zpid")
            if zpid and zpid in seen:
                continue
            if zpid:
                seen.add(zpid)
            out.append(extract(r, page))

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    elapsed = time.perf_counter() - start
    print(f"TOTAL {len(out)} deduped listings written to {args.out} in {elapsed:.1f}s",
          file=sys.stderr)


async def run(args):
    os.makedirs(args.profile, exist_ok=True)
    if args.cache_dir:
        os.makedirs(args.cache_dir, exist_ok=True)

    def cache_path(page):
        return os.path.join(args.cache_dir, f"page_{page:03d}.html") if args.cache_dir else None

    start = time.perf_counter()

    pages = list(range(args.start, args.start + args.pages))
    all_cached = False
    if args.cache_dir:
        all_cached = all(
            os.path.exists(os.path.join(args.cache_dir, f"page_{p:03d}.html"))
            for p in pages
        )

    if all_cached:
        print("all pages cached; skipping browser", file=sys.stderr)
        results = []
        for p in pages:
            path = cache_path(p)
            assert path is not None
            with open(path, encoding="utf-8", errors="replace") as f:
                results.append((p, f.read(), "cache"))
    else:
        first_url = args.url_template.format(page=args.start)
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=args.profile,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            print(f"warming up {first_url}", file=sys.stderr)
            print("if a captcha appears in the browser window, solve it once", file=sys.stderr)
            warm = await warm_up(ctx, first_url)
            if warm is None:
                print("FATAL: browser is being denied (captcha/block). Waiting a few minutes or use a fresh --profile dir.", file=sys.stderr)
                await ctx.close()
                sys.exit(1)
            state = parse_search_state(warm)
            if state:
                print(f"server reports {state['totalResultCount']} listings across {state['totalPages']} pages",
                      file=sys.stderr)
                if state["totalPages"] and args.pages > state["totalPages"]:
                    print(f"capping --pages from {args.pages} to {state['totalPages']}", file=sys.stderr)

            pages = list(range(args.start, args.start + args.pages))
            if state and state["totalPages"]:
                pages = [p for p in pages if p <= state["totalPages"]]
            urls = {p: args.url_template.format(page=p) for p in pages}

            print(f"fetching {len(urls)} pages in parallel (workers={args.workers}, delay={args.delay}s)",
                  file=sys.stderr)

            sem = asyncio.Semaphore(args.workers)
            results = await asyncio.gather(
                *[fetch_page(sem, ctx, url, p, cache_path(p), args.retries, args.delay)
                  for p, url in urls.items()]
            )
            await ctx.close()

    page_results = {}
    denied = 0
    for page, html, src in results:
        if html is None:
            denied += 1
            print(f"FAILED page {page} ({src})", file=sys.stderr)
            continue
        list_results = parse_page(html)
        if list_results is None:
            print(f"NO NEXT_DATA page {page} ({src})", file=sys.stderr)
            continue
        print(f"page {page}: {len(list_results)} listings ({src})", file=sys.stderr)
        page_results[page] = list_results

    if denied and denied * 2 >= len(results):
        print("WARNING: more than half of pages were blocked; the session is likely flagged. "
              "Wait a few minutes or retry with a fresh --profile dir.", file=sys.stderr)

    seen = set()
    out = []
    for page in sorted(page_results):
        for r in page_results[page]:
            zpid = r.get("zpid")
            if zpid and zpid in seen:
                continue
            if zpid:
                seen.add(zpid)
            out.append(extract(r, page))

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    elapsed = time.perf_counter() - start
    print(f"TOTAL {len(out)} deduped listings written to {args.out} in {elapsed:.1f}s",
          file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(
        description="Fetch Zillow search listings for any region. "
                    "Simpler than zillow_pull.py but capped at 24 pages (~984 listings) per search.")
    ap.add_argument("--region", required=True,
                    help="Zillow region slug, e.g. 'toronto-on' or 'new-york-ny'")
    ap.add_argument("--url-template", default=None,
                    help="full Zillow page URL with a {page} placeholder (defaults to "
                         "https://www.zillow.com/{region}/{page}_p/)")
    ap.add_argument("--pages", type=int, default=20, help="number of result pages to fetch")
    ap.add_argument("--start", type=int, default=1, help="first page to fetch")
    ap.add_argument("--workers", type=int, default=3, help="concurrent page loads (browser fetcher only)")
    ap.add_argument("--retries", type=int, default=3, help="retries per page on failure")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds to wait before each page load")
    ap.add_argument("--fetcher", choices=["browser", "proxy"], default="proxy",
                    help="how to fetch pages: 'browser' (real Chromium, may be captcha-blocked) "
                         "or 'proxy' (via a whitelisted-IP proxy, default). "
                         "Note: the local IP is hard-blocked by PerimeterX, so browser rarely works.")
    ap.add_argument("--proxy-base", default="https://api.allorigins.win/raw?url={url}",
                    help="proxy URL template with a {url} placeholder. "
                         "Defaults to the free allorigins proxy (Google IPs, whitelisted by Zillow). "
                         "You can point this at your own Google Apps Script web app for better reliability.")
    ap.add_argument("--out", default=None, help="output JSON path (default: {region}_all.json)")
    ap.add_argument("--cache-dir", default=None,
                    help="dir to cache raw pages and skip re-downloading on reruns "
                         "(default: cache/{region}_pages)")
    ap.add_argument("--profile", default=os.path.join(os.path.expanduser("~"),
                                                      ".zillow-puller", "profile"),
                    help="persistent browser profile dir (browser fetcher only)")
    args = ap.parse_args()
    if not args.url_template:
        args.url_template = f"https://www.zillow.com/{args.region}/{{page}}_p/"
    if not args.out:
        args.out = f"{args.region}_all.json"
    if not args.cache_dir:
        args.cache_dir = os.path.join("cache", f"{args.region}_pages")
    if args.fetcher == "proxy":
        run_proxy(args)
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
