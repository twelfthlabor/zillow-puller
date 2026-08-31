import asyncio
import json
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zillow_pull as zp
import zillow_pages as zpg

FAILS = []


def check(name, cond, detail: object = ""):
    if cond:
        print(f"  PASS {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {detail}")


class FakePage:
    def __init__(self, bounds, total, per_page=40):
        self.bounds = bounds
        self.total = total
        self.per_page = per_page
        self.calls = []
        self.payloads = []
        self.responses = self._build_responses()
        self.blocks = 0
        self.boxes_checked = 0
        self.boxes_blocked = 0

    def _results_for(self, bounds, page):
        n = self.per_page
        start = (page - 1) * n
        out = []
        for i in range(start, start + n):
            if i >= self.total:
                break
            out.append({
                "zpid": i + 1,
                "address": f"{i + 1} Fake St",
                "price": 100000 + i,
                "beds": 2,
                "baths": 1,
                "area": 800,
                "detailUrl": f"/homedetails/x/{i + 1}_zpid/",
                "latitude": bounds["south"],
                "longitude": bounds["west"],
                "hdpData": {"homeInfo": {"homeType": "CONDO",
                                         "zestimate": 500000,
                                         "daysOnZillow": 5}},
            })
        return out

    def _build_responses(self):
        key = zp.tile_key(self.bounds)
        w, e, s, n = (self.bounds[k] for k in ("west", "east", "south", "north"))
        mid_lon = (w + e) / 2
        mid_lat = (n + s) / 2
        quads = [
            {"west": w, "east": mid_lon, "south": mid_lat, "north": n},
            {"west": mid_lon, "east": e, "south": mid_lat, "north": n},
            {"west": w, "east": mid_lon, "south": s, "north": mid_lat},
            {"west": mid_lon, "east": e, "south": s, "north": mid_lat},
        ]
        # responses keyed by tile_key -> list of pages
        all_responses = {key: []}
        for page in range(1, 5):
            all_responses[key].append(self._results_for(self.bounds, page))
        return all_responses

    async def evaluate(self, js, payload_str):
        self.calls.append(1)
        self.payloads.append(json.loads(payload_str))
        payload = json.loads(payload_str)
        sqs = payload["searchQueryState"]
        bounds = sqs["mapBounds"]
        key = zp.tile_key(bounds)
        page = sqs.get("pagination", {}).get("currentPage", 1)
        wants = payload.get("wants", {})
        if key not in self.responses:
            self.responses[key] = [self._results_for(bounds, p) for p in range(1, 5)]
        results = self.responses[key][page - 1] if page <= len(self.responses[key]) else []
        want_results = "listResults" in wants.get("cat1", [])
        total = len(self.responses[key]) * self.per_page
        data: dict = {
            "cat2": {"total": {"totalResultCount": total}},
        }
        if want_results:
            data["cat1"] = {
                "searchList": {"totalResultCount": total, "totalPages": 4},
                "searchResults": {"listResults": results},
            }
        return {"status": 200, "text": json.dumps(data)}


async def _check_tile_key_split():
    print("tile_key/split_tile")
    b = {"west": -80.0, "east": -79.0, "south": 43.0, "north": 44.0}
    k = zp.tile_key(b)
    check("tile_key format", k == "-80.0000,-79.0000,43.0000,44.0000", k)
    subs = zp.split_tile(b)
    check("split -> 4", len(subs) == 4, len(subs))
    keys = {zp.tile_key(s) for s in subs}
    check("sub-tiles unique", len(keys) == 4, keys)


async def _check_payload_build():
    print("build_payload")
    region = {"regionSelection": [{"regionId": 1, "regionType": 6}],
              "mapBounds": {"west": -80, "east": -79, "south": 43, "north": 44},
              "usersSearchTerm": "test"}
    p1 = zp.build_payload(region, region["mapBounds"], 1, True)
    check("page1 empty pagination", p1["searchQueryState"]["pagination"] == {})
    check("page1 wants results", "listResults" in p1["wants"]["cat1"])
    p2 = zp.build_payload(region, region["mapBounds"], 2, False)
    check("page2 has pagination", p2["searchQueryState"]["pagination"]["currentPage"] == 2)
    check("count-only wants", p2["wants"] == {"cat2": ["total"]})


async def _check_end_to_end():
    print("end-to-end tiling + collect + cache")
    bounds = {"west": -80.0, "east": -79.0, "south": 43.0, "north": 44.0}
    region = {"regionSelection": [{"regionId": 1, "regionType": 6}],
              "mapBounds": bounds, "usersSearchTerm": "test"}
    page = FakePage(bounds, total=600)  # > tile_max, forces a split
    out, seen = [], set()
    tmp = tempfile.mkdtemp()

    await zp.tile_search(page, region, bounds, delay=0, max_pages=10, tile_max=450,
                         out=out, seen=seen, cache_dir=tmp, depth=0)

    check("collected > 0", len(out) > 0, len(out))
    check("all have zpid", all(r.get("zpid") for r in out))
    check("no dup zpids", len({r["zpid"] for r in out}) == len(out))
    check("zestimate is zestimate not rent", out[0]["zestimate"] == 500000)
    check("cache files written", len([f for f in os.listdir(tmp) if f.startswith("tile_")]) > 0)

    # cache-only merge
    out2 = zp.load_cached(tmp)
    check("cache-only merge matches", len(out2) == len(out), (len(out2), len(out)))

    # resume: second run with same cache should not add new API calls
    page2 = FakePage(bounds, total=600)
    out3, seen3 = [], set()
    await zp.tile_search(page2, region, bounds, delay=0, max_pages=10, tile_max=450,
                         out=out3, seen=seen3, cache_dir=tmp, depth=0)
    check("resume uses cache (no API calls)", len(page2.calls) == 0, len(page2.calls))


async def _check_count_only_splitting():
    print("count-only splitting decisions")
    bounds = {"west": -80.0, "east": -79.0, "south": 43.0, "north": 44.0}
    region = {"regionSelection": [{"regionId": 1, "regionType": 6}],
              "mapBounds": bounds, "usersSearchTerm": "test"}
    page = FakePage(bounds, total=100)  # <= tile_max, should collect directly
    out, seen = [], set()
    tmp = tempfile.mkdtemp()
    await zp.tile_search(page, region, bounds, delay=0, max_pages=10, tile_max=450,
                         out=out, seen=seen, cache_dir=tmp, depth=0)
    check("small region collected directly", len(out) == 100, len(out))


def test_proxy_fetch():
    failures_before = len(FAILS)
    print("proxy fetch URL building + retry on non-200")
    from unittest.mock import patch

    url = "https://www.zillow.com/toronto-on/2_p/"
    base = "https://api.allorigins.win/raw?url={url}"
    html = "<html><script id=\"__NEXT_DATA__\" type=\"application/json\">{\"x\":1}</script></html>"

    class FakeResp:
        def __init__(self, code, text):
            self.status_code = code
            self.text = text

    with patch("curl_cffi.requests.get") as m:
        m.return_value = FakeResp(200, html)
        got = zpg.fetch_via_proxy(url, base, retries=2, delay=0)
        check("proxy 200 returns html", got == html)
        check("url urlencoded", "url=https%3A" in m.call_args.args[0])

    with patch("curl_cffi.requests.get") as m:
        m.side_effect = [FakeResp(500, ""), FakeResp(403, "Access to this page has been denied")]
        got = zpg.fetch_via_proxy(url, base, retries=2, delay=0)
        check("proxy retries then gives up", got is None)
        check("proxy called twice", m.call_count == 2)
    assert len(FAILS) == failures_before, FAILS[failures_before:]


def test_proxy_parse_roundtrip():
    failures_before = len(FAILS)
    print("proxy HTML parses into listings via existing parser")
    html = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({
            "props": {"pageProps": {"searchPageState": {
                "categoryTotals": {"cat1": {"totalResultCount": 1, "totalPages": 1}},
                "cat1": {"searchResults": {"listResults": [{
                    "zpid": 42,
                    "address": "4 Oak St",
                    "price": 999000,
                    "beds": 3,
                    "baths": 2,
                    "area": 1400,
                    "detailUrl": "/homedetails/x/42_zpid/",
                    "hdpData": {"homeInfo": {"homeType": "HOUSE",
                                             "zestimate": 1000000,
                                             "daysOnZillow": 3}},
                }]}},
            }}}})
        + '</script></html>'
    )
    state = zpg.parse_search_state(html)
    check("proxy parse total", state["totalResultCount"] == 1, state)
    rec = zpg.extract(state["listResults"][0], 1)
    check("proxy extract zestimate", rec["zestimate"] == 1000000, rec)
    assert len(FAILS) == failures_before, FAILS[failures_before:]


def _run_async_check(check_fn):
    failures_before = len(FAILS)
    asyncio.run(check_fn())
    assert len(FAILS) == failures_before, FAILS[failures_before:]


def test_tile_key_split():
    _run_async_check(_check_tile_key_split)


def test_payload_build():
    _run_async_check(_check_payload_build)


def test_end_to_end():
    _run_async_check(_check_end_to_end)


def test_count_only_splitting():
    _run_async_check(_check_count_only_splitting)


async def main():
    await _check_tile_key_split()
    await _check_payload_build()
    await _check_count_only_splitting()
    await _check_end_to_end()
    test_proxy_fetch()
    test_proxy_parse_roundtrip()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURES: {FAILS}")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
