#!/usr/bin/env python3
"""Real-Chrome benchmark for ``PropertyScraper._collect_page_records``.

Compares the pristine pre-change copy of ``property_scraper.py`` with the
working-tree module, in-process, against local fixtures served over loopback:

* ``tests/fixtures/lazy_cards_fixture.html`` — 42 Zillow-shaped cards in a
  scrollable pane whose details hydrate on view (IntersectionObserver +
  scroll-position sweep, 60–150 ms stagger) plus a late brokerage field at
  +2.0 s.
* ``tests/fixtures/lazy_cards_io_fixture.html`` — 36 Zillow-shaped cards
  whose details hydrate *only* through IntersectionObserver, with each
  card's brokerage arriving 300 ms after that hydration.
* ``tests/fixtures/attach_challenge_fixture.html`` — a ``#px-captcha`` page.

No external network is touched. The benchmark prints a per-side phase
breakdown (card wait, round JS time, module-level sleep time, rounds,
navigation count, total collect time) and medians, and writes the extracted
record sets to ``parity_<fixture>_before.json`` / ``parity_<fixture>_after.json``
so extraction parity can be inspected.

The module under test is driven through SeleniumBase Pure CDP Mode
(``seleniumbase.sb_cdp.Chrome``), matching the product's own session layer.
The pristine ``--baseline`` module predates the CDP migration and imports
Selenium directly, so the baseline side still attaches with raw Selenium;
that legacy attachment exists only to run pre-migration copies and is never
used for the module under test.

``--baseline`` is required: there is deliberately no committed dependency on
a machine-specific temp path, so a fresh checkout must be given the pristine
copy explicitly.

Usage::

    .venv313/bin/python scripts/bench_collect_page.py \
        --baseline /path/to/pristine/property_scraper.py

Options::

    --runs N            collect-phase runs per side (default 5; median reported)
    --challenge-runs N  challenge-path runs per side (default 3)
    --baseline PATH     pristine pre-change module copy (required)
    --module PATH       module under test (default ../property_scraper.py)
    --out-dir PATH      where parity JSON files are written
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import importlib.util
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from seleniumbase import sb_cdp

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
DEFAULT_OUT_DIR = Path(tempfile.gettempdir()) / "zillow-perf"
CHROME_BINARY = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CHROME_READY_TIMEOUT_SECONDS = 15.0
COLLECT_PAGE_WAIT_SECONDS = 15.0
CHALLENGE_PAGE_WAIT_SECONDS = 15.0
CHALLENGE_TARGET_SECONDS = 3.0
PANE_SETTLE_BETWEEN_RUNS_SECONDS = 0.3


@dataclass
class Chrome:
    port: int
    process: subprocess.Popen
    log_path: Path

    @property
    def attach_address(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def alive(self) -> bool:
        return self.process.poll() is None


class _QuietRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Serve fixtures with request logging silenced and caching disabled."""

    def log_message(self, *args: object) -> None:
        pass

    def end_headers(self) -> None:
        # Every run must fetch the fixture fresh; the browser cache would
        # otherwise skew the second side of an interleaved comparison.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_debugger(port: int, process: subprocess.Popen, log_path: Path) -> None:
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + CHROME_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Chrome exited with code {process.returncode} during startup; "
                f"log tail:\n{_log_tail(log_path)}"
            )
        try:
            with urllib.request.urlopen(endpoint, timeout=1) as response:
                payload = json.load(response)
        except (OSError, ValueError):
            time.sleep(0.25)
            continue
        if payload.get("Browser"):
            return
    raise RuntimeError(
        f"Chrome DevTools endpoint {endpoint} not ready in "
        f"{CHROME_READY_TIMEOUT_SECONDS:.0f}s; log tail:\n{_log_tail(log_path)}"
    )


def _log_tail(path: Path, limit: int = 2000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "<no log>"
    return text[-limit:]


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _launch_chrome(profile_dir: Path) -> Chrome:
    if not (CHROME_BINARY.is_file() and os.access(CHROME_BINARY, os.X_OK)):
        raise RuntimeError(f"real Chrome is not available at {CHROME_BINARY}")
    debug_port = _free_port()
    log_path = profile_dir.parent / "chrome.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log_handle:
        process = subprocess.Popen(
            [
                str(CHROME_BINARY),
                "--headless=new",
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_for_debugger(debug_port, process, log_path)
    except Exception:
        _terminate(process)
        raise
    return Chrome(port=debug_port, process=process, log_path=log_path)


class _FixtureServer:
    def __init__(self) -> None:
        handler = functools.partial(_QuietRequestHandler, directory=str(FIXTURES_DIR))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _TimeProxy:
    """Stand-in for a module's ``time`` global so its sleeps can be logged."""

    def __init__(self) -> None:
        self.sleeps: list[tuple[float, float]] = []

    def __getattr__(self, name: str):
        return getattr(time, name)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append((time.perf_counter(), float(seconds)))
        time.sleep(seconds)

    def reset(self) -> None:
        self.sleeps.clear()


class _DriverProxy:
    """Delegating raw-Selenium driver wrapper that records benchmark phases.

    Kept for the pristine ``--baseline`` module only; the module under test
    uses :class:`_CdpDriverProxy`.
    """

    def __init__(self, driver: object, card_selector: str) -> None:
        self._driver = driver
        self.card_selector = card_selector
        self.get_calls = 0
        self.nav_seconds = 0.0
        self.js_calls: list[tuple[str, float]] = []
        self.first_card_at: float | None = None

    def __getattr__(self, name: str):
        return getattr(self._driver, name)

    def reset(self) -> None:
        self.get_calls = 0
        self.nav_seconds = 0.0
        self.js_calls.clear()
        self.first_card_at = None

    def get(self, url: str) -> None:
        self.get_calls += 1
        started = time.perf_counter()
        self._driver.get(url)
        self.nav_seconds += time.perf_counter() - started

    def find_elements(self, by: str, selector: str) -> list[object]:
        result = self._driver.find_elements(by, selector)
        if selector == self.card_selector and result and self.first_card_at is None:
            self.first_card_at = time.perf_counter()
        return result

    def execute_script(self, script: str, *args: object) -> object:
        started = time.perf_counter()
        try:
            return self._driver.execute_script(script, *args)
        finally:
            self.js_calls.append((script, time.perf_counter() - started))


class _CdpDriverProxy:
    """Delegating Pure CDP session wrapper that records benchmark phases.

    Mirrors the ``_DriverProxy`` measurement surface while speaking the
    single-expression CDP session API the migrated module uses.
    """

    def __init__(self, session: object, card_selector: str) -> None:
        self._session = session
        self.card_selector = card_selector
        self.get_calls = 0
        self.nav_seconds = 0.0
        self.js_calls: list[tuple[str, float]] = []
        self.first_card_at: float | None = None

    def __getattr__(self, name: str):
        return getattr(self._session, name)

    def reset(self) -> None:
        self.get_calls = 0
        self.nav_seconds = 0.0
        self.js_calls.clear()
        self.first_card_at = None

    def get(self, url: str) -> None:
        self.get_calls += 1
        started = time.perf_counter()
        self._session.get(url)
        self.nav_seconds += time.perf_counter() - started

    def find_elements(self, selector: str) -> list[object]:
        result = self._session.find_elements(selector)
        if selector == self.card_selector and result and self.first_card_at is None:
            self.first_card_at = time.perf_counter()
        return result

    def execute_script(self, script: str) -> object:
        started = time.perf_counter()
        try:
            return self._session.execute_script(script)
        finally:
            self.js_calls.append((script, time.perf_counter() - started))


CARD_SELECTOR = "[data-testid='property-card']"


def _attach_selenium(chrome: Chrome):
    """Legacy raw-Selenium attachment for the pristine baseline module only.

    The baseline copy predates the CDP migration and itself imports
    ``selenium`` and expects a WebDriver, so it cannot run on a Pure CDP
    session. The module under test always uses :func:`_attach_cdp`.
    """
    try:
        from selenium import webdriver
    except ImportError as exc:  # pragma: no cover - environment problem
        raise SystemExit(f"the pristine --baseline module requires selenium: {exc}") from exc
    options = webdriver.ChromeOptions()
    options.debugger_address = chrome.attach_address
    options.page_load_strategy = "eager"
    # Selenium Manager runs in-process and prefers a PATH chromedriver, so
    # seleniumbase's bundled copy (which may not match the installed Chrome)
    # must not be on PATH for this legacy attach; a plain shell outside
    # pytest resolves a matching driver itself.
    original_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(
        entry for entry in original_path.split(os.pathsep) if entry and "seleniumbase" not in entry
    )
    try:
        driver = webdriver.Chrome(options=options)
    finally:
        os.environ["PATH"] = original_path
    driver.set_page_load_timeout(40)
    return driver


def _attach_cdp(chrome: Chrome):
    """Attach the module-under-test session with SeleniumBase Pure CDP Mode."""
    return sb_cdp.Chrome("about:blank", host="127.0.0.1", port=chrome.port)


def _write_config(
    directory: Path,
    *,
    start_url: str,
    page_wait_seconds: float,
    scrolling: dict[str, object],
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    config = {
        "start_url": start_url,
        "user_agent": "BenchFixtureBot/1.0 (+mailto:test@example.com)",
        "output_csv": "listings.csv",
        "checkpoint_file": "checkpoint.json",
        "error_directory": "errors",
        "report_file": "report.json",
        "page_wait_seconds": page_wait_seconds,
        "request_delay_seconds": 0,
        "request_jitter_seconds": 0,
        "max_pages": 1,
        "headless": True,
        "pagination": {"mode": "next_button", "next_selector": "a.next"},
        "scrolling": scrolling,
        "selectors": {
            "card": "[data-testid='property-card']",
            "address": {"selector": "[data-testid='property-card-link']"},
            "price": {
                "selector": "[data-testid='property-card-price']",
                "transform": "money",
                "validation": {"min": 0, "max": 500000000},
            },
            "beds": {
                "selector": "[data-testid='property-card-details']",
                "transform": "detail_beds",
            },
            "baths": {
                "selector": "[data-testid='property-card-details']",
                "transform": "detail_baths",
            },
            "sqft": {
                "selector": "[data-testid='property-card-details']",
                "transform": "detail_sqft",
            },
            "agent": {
                "selector": "[data-testid='property-card-brokerage']",
                "transform": "brokerage",
            },
            "url": {
                "selector": "[data-testid='property-card-link']",
                "attribute": "href",
            },
        },
    }
    path = directory / "config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


REQUIRED_FIELDS = ("listing_id", "address", "price", "beds", "baths", "sqft", "agent", "url")


def _canonical_records(records: list[dict[str, str]]) -> str:
    """Stable JSON for parity: every extracted field except scraped_at."""
    cleaned = [
        {key: value for key, value in record.items() if key != "scraped_at"} for record in records
    ]
    cleaned.sort(key=lambda record: (record.get("listing_id", ""), record.get("url", "")))
    return json.dumps(cleaned, indent=2, sort_keys=True)


@dataclass
class CollectRun:
    side: str
    run: int
    total_seconds: float = 0.0
    card_wait_seconds: float = 0.0
    wait_sleep_seconds: float = 0.0
    scroll_sleep_seconds: float = 0.0
    round_js_seconds: float = 0.0
    lookup_js_seconds: float = 0.0
    rounds: int = 0
    navigations: int = 0
    record_count: int = 0
    missing_fields: int = 0
    records_json: str = ""


@dataclass
class ChallengeRun:
    side: str
    run: int
    total_seconds: float = 0.0
    nav_seconds: float = 0.0
    raised: bool = False
    detector_seconds: float = 0.0
    detector_hit: bool = False


def _js_breakdown(js_calls: list[tuple[str, float]]) -> tuple[int, float, float]:
    rounds = 0
    round_seconds = 0.0
    lookup_seconds = 0.0
    for script, seconds in js_calls:
        # The migrated module folds the container lookup into the per-round
        # script, so querySelectorAll is the reliable round marker; the
        # baseline's standalone container lookup has no querySelectorAll.
        if "querySelectorAll" in script:
            rounds += 1
            round_seconds += seconds
        else:
            lookup_seconds += seconds
    return rounds, round_seconds, lookup_seconds


SCROLLING = {
    "enabled": True,
    "max_rounds": 80,
    "stable_rounds": 3,
    "poll_seconds": 0.2,
    "scroll_fraction": 0.8,
    # Explicitly mirrors the shipped step_delay_seconds default so the
    # measured pacing is visible in the printed configuration.
    "step_delay_seconds": 0.05,
}

# (slug, fixture URL (may carry a query), human label)
COLLECT_SCENARIOS = (
    (
        "lazy",
        "lazy_cards_fixture.html",
        "42 cards, viewport hydration + sweep, +2.0s field (capture gate)",
    ),
    (
        "io",
        "lazy_cards_io_fixture.html",
        "36 cards, IO-only, per-card +300ms field (capture gate)",
    ),
    (
        "lazy-fast",
        "lazy_cards_fixture.html?late_ms=0",
        "42 cards, fields settle during scrolling (typical case)",
    ),
)


def measure_collect(
    module: object,
    time_proxy: _TimeProxy,
    driver: object,
    start_url: str,
    config_dir: Path,
    run_index: int,
    attribute: str = "driver",
) -> CollectRun:
    config_path = _write_config(
        config_dir / f"collect-{run_index}",
        start_url=start_url,
        page_wait_seconds=COLLECT_PAGE_WAIT_SECONDS,
        scrolling=SCROLLING,
    )
    time_proxy.reset()
    driver.reset()
    settings = module.Settings.load(config_path)
    scraper = module.PropertyScraper(settings)
    setattr(scraper, attribute, driver)
    driver.get(start_url)  # exactly one navigation per measured page
    started = time.perf_counter()
    records = scraper._collect_page_records(start_url)
    total = time.perf_counter() - started

    run = CollectRun(side="", run=run_index)
    run.total_seconds = total
    run.navigations = driver.get_calls
    if driver.first_card_at is not None:
        run.card_wait_seconds = driver.first_card_at - started
    scroll_sleeps = [
        seconds
        for at, seconds in time_proxy.sleeps
        if driver.first_card_at is None or at >= driver.first_card_at
    ]
    wait_sleeps = [
        seconds
        for at, seconds in time_proxy.sleeps
        if driver.first_card_at is not None and at < driver.first_card_at
    ]
    run.scroll_sleep_seconds = sum(scroll_sleeps)
    run.wait_sleep_seconds = sum(wait_sleeps)
    run.rounds, run.round_js_seconds, run.lookup_js_seconds = _js_breakdown(driver.js_calls)
    run.record_count = len(records)
    run.missing_fields = sum(
        1 for record in records for name in REQUIRED_FIELDS if not record.get(name)
    )
    run.records_json = _canonical_records(records)
    return run


def measure_challenge(
    module: object,
    time_proxy: _TimeProxy,
    driver: object,
    start_url: str,
    config_dir: Path,
    run_index: int,
    attribute: str = "driver",
) -> ChallengeRun:
    config_path = _write_config(
        config_dir / f"challenge-{run_index}",
        start_url=start_url,
        page_wait_seconds=CHALLENGE_PAGE_WAIT_SECONDS,
        scrolling={"enabled": False},
    )
    time_proxy.reset()
    driver.reset()
    settings = module.Settings.load(config_path)
    scraper = module.PropertyScraper(settings)
    setattr(scraper, attribute, driver)
    started = time.perf_counter()
    driver.get(start_url)
    raised = False
    try:
        scraper._collect_page_records(start_url)
    except module.AccessChallengeError:
        raised = True
    total = time.perf_counter() - started

    run = ChallengeRun(side="", run=run_index)
    run.total_seconds = total
    run.nav_seconds = driver.nav_seconds
    run.raised = raised
    detector_started = time.perf_counter()
    run.detector_hit = bool(scraper._detect_challenge())
    run.detector_seconds = time.perf_counter() - detector_started
    return run


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def _print_collect_table(side: str, runs: list[CollectRun]) -> None:
    for run in runs:
        print(
            f"  {side:<6} run {run.run}: total={run.total_seconds * 1000:8.1f}ms  "
            f"card_wait={run.card_wait_seconds * 1000:7.1f}ms  "
            f"rounds={run.rounds:3d}  round_js={run.round_js_seconds * 1000:7.1f}ms  "
            f"sleep={run.scroll_sleep_seconds * 1000:8.1f}ms  "
            f"nav={run.navigations}  records={run.record_count}  "
            f"missing={run.missing_fields}"
        )
    print(
        f"  {side:<6} median: total={_median([r.total_seconds for r in runs]) * 1000:8.1f}ms  "
        f"card_wait={_median([r.card_wait_seconds for r in runs]) * 1000:7.1f}ms  "
        f"rounds={_median([float(r.rounds) for r in runs]):5.0f}  "
        f"round_js={_median([r.round_js_seconds for r in runs]) * 1000:7.1f}ms  "
        f"sleep={_median([r.scroll_sleep_seconds for r in runs]) * 1000:8.1f}ms  "
        f"nav={_median([float(r.navigations) for r in runs]):3.0f}  "
        f"records={_median([float(r.record_count) for r in runs]):3.0f}"
    )


def _compare_parity(before: list[CollectRun], after: list[CollectRun]) -> bool:
    identical = True
    for before_run, after_run in zip(before, after):
        if before_run.records_json != after_run.records_json:
            identical = False
            before_ids = before_run.records_json
            after_ids = after_run.records_json
            print(
                f"PARITY MISMATCH on run {before_run.run}: "
                f"before {before_run.record_count} records / "
                f"after {after_run.record_count} records"
            )
            if before_ids != after_ids:
                print("  before JSON:")
                print(before_ids[:2000])
                print("  after JSON:")
                print(after_ids[:2000])
    return identical


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--challenge-runs", type=int, default=3)
    parser.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help="pristine pre-change module copy to compare against",
    )
    parser.add_argument("--module", type=Path, default=REPO_ROOT / "property_scraper.py")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    sys.path.insert(0, str(REPO_ROOT))  # field_utils for both module copies
    if not args.baseline.is_file():
        raise SystemExit(f"baseline copy not found: {args.baseline}")
    if not args.module.is_file():
        raise SystemExit(f"module not found: {args.module}")

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / "bench-tmp"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    print("zillow collect-page benchmark (local headless Chrome, no external network)")
    print(f"  baseline module: {args.baseline}")
    print(f"  tested module:   {args.module}")
    print(f"  collect runs/side: {args.runs}   challenge runs/side: {args.challenge_runs}")
    print(f"  scrolling: {json.dumps(SCROLLING)}")
    print()

    baseline_module = _load_module("zillow_bench_baseline", args.baseline)
    edited_module = _load_module("zillow_bench_edited", args.module)
    baseline_time = _TimeProxy()
    edited_time = _TimeProxy()
    baseline_module.time = baseline_time
    edited_module.time = edited_time

    module_side = {
        "before": (baseline_module, baseline_time, args.baseline),
        "after": (edited_module, edited_time, args.module),
    }

    server = _FixtureServer()
    baseline_chrome = _launch_chrome(work_dir / "chrome-baseline" / "profile")
    edited_chrome = _launch_chrome(work_dir / "chrome-edited" / "profile")
    selenium_driver = None
    cdp_session = None
    try:
        selenium_driver = _attach_selenium(baseline_chrome)
        cdp_session = _attach_cdp(edited_chrome)
        proxies: dict[str, object] = {
            "before": _DriverProxy(selenium_driver, CARD_SELECTOR),
            "after": _CdpDriverProxy(cdp_session, CARD_SELECTOR),
        }
        # The pristine baseline module uses ``scraper.driver``; the migrated
        # module uses ``scraper.session``.
        attributes = {"before": "driver", "after": "session"}
        reset_pages = {
            "before": lambda: selenium_driver.get("about:blank"),
            "after": lambda: cdp_session.get("about:blank"),
        }

        print(f"  Chrome: {cdp_session.get_user_agent()}")
        print()

        # --- collect phase: interleaved sides, one scenario per fixture -----
        collect_parity: dict[str, bool] = {}
        collect_configs = work_dir / "collect"
        for slug, fixture, label in COLLECT_SCENARIOS:
            fixture_url = f"{server.base_url}/{fixture}"
            separator = "&" if "?" in fixture else "?"
            collect_runs: dict[str, list[CollectRun]] = {"before": [], "after": []}
            for run_index in range(1, args.runs + 1):
                for side in ("before", "after"):
                    module, time_proxy, _path = module_side[side]
                    result = measure_collect(
                        module,
                        time_proxy,
                        proxies[side],
                        f"{fixture_url}{separator}run={run_index}",
                        collect_configs / side / slug,
                        run_index,
                        attribute=attributes[side],
                    )
                    result.side = side
                    assert result.navigations == 1, (
                        f"{side} run {run_index}: expected exactly one navigation "
                        f"per page, saw {result.navigations}"
                    )
                    collect_runs[side].append(result)
                    reset_pages[side]()
                    time.sleep(PANE_SETTLE_BETWEEN_RUNS_SECONDS)

            before_runs = collect_runs["before"]
            after_runs = collect_runs["after"]
            print(f"{fixture} ({label})")
            _print_collect_table("before", before_runs)
            _print_collect_table("after", after_runs)
            before_total = _median([r.total_seconds for r in before_runs])
            after_total = _median([r.total_seconds for r in after_runs])
            reduction = (1 - after_total / before_total) * 100 if before_total else 0.0
            print(
                f"  median collect reduction: {reduction:.1f}% "
                f"({before_total * 1000:.1f}ms -> {after_total * 1000:.1f}ms)"
            )
            parity = _compare_parity(before_runs, after_runs)
            collect_parity[fixture] = parity
            print(
                f"  extraction parity (before vs after, all fields except scraped_at): "
                f"{'IDENTICAL' if parity else 'MISMATCH'}"
            )
            (out_dir / f"parity_{slug}_before.json").write_text(
                before_runs[0].records_json + "\n", encoding="utf-8"
            )
            (out_dir / f"parity_{slug}_after.json").write_text(
                after_runs[0].records_json + "\n", encoding="utf-8"
            )
            print()

        # --- challenge phase -------------------------------------------------
        challenge_url = f"{server.base_url}/attach_challenge_fixture.html"
        challenge_runs: dict[str, list[ChallengeRun]] = {"before": [], "after": []}
        challenge_configs = work_dir / "challenge"
        for run_index in range(1, args.challenge_runs + 1):
            for side in ("before", "after"):
                module, time_proxy, _path = module_side[side]
                result = measure_challenge(
                    module,
                    time_proxy,
                    proxies[side],
                    challenge_url,
                    challenge_configs / side,
                    run_index,
                    attribute=attributes[side],
                )
                result.side = side
                assert result.raised, f"{side} run {run_index}: no challenge raised"
                challenge_runs[side].append(result)
                reset_pages[side]()
                time.sleep(PANE_SETTLE_BETWEEN_RUNS_SECONDS)

        print("attach_challenge_fixture.html (#px-captcha, page_wait_seconds=15)")
        for side in ("before", "after"):
            runs = challenge_runs[side]
            for run in runs:
                print(
                    f"  {side:<6} run {run.run}: from-load={run.total_seconds:7.3f}s  "
                    f"nav={run.nav_seconds * 1000:6.1f}ms  "
                    f"raised={run.raised}  "
                    f"direct _detect_challenge={run.detector_seconds * 1000:6.1f}ms "
                    f"hit={run.detector_hit}"
                )
            print(
                f"  {side:<6} median: from-load="
                f"{_median([r.total_seconds for r in runs]):.3f}s  "
                f"target <= {CHALLENGE_TARGET_SECONDS:.0f}s: "
                f"{'PASS' if _median([r.total_seconds for r in runs]) <= CHALLENGE_TARGET_SECONDS else 'FAIL'}"
            )
        print()
        print(f"benchmark artifacts: {out_dir}")
        return 0 if all(collect_parity.values()) else 1
    finally:
        if selenium_driver is not None:
            with contextlib.suppress(Exception):
                selenium_driver.quit()
        if cdp_session is not None:
            with contextlib.suppress(Exception):
                cdp_session.quit()
        _terminate(baseline_chrome.process)
        _terminate(edited_chrome.process)
        server.close()


if __name__ == "__main__":
    sys.exit(main())
