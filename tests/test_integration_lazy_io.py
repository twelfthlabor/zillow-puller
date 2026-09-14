"""Real-Chrome guard for IO-only lazy hydration during fast scrolling.

``lazy_cards_fixture.html`` hydrates card details through an
IntersectionObserver but also sweeps the current scroll position as a
fallback, so it cannot fail when a collector scrolls so fast that Chrome
coalesces rendering frames and misses an intersection callback.
``lazy_cards_io_fixture.html`` removes that compensation: its cards hydrate
*only* when the observer reports them, and every card's brokerage arrives
300 ms after its hydration. This test drives the real collector with
``scrolling.enabled: true`` and the shipped default pacing against that
fixture and asserts every card and every field (including the delayed one)
is extracted. It skips when Chrome is not present, like the other
real-browser integration tests.
"""

from __future__ import annotations

import csv
import functools
import http.server
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

import property_scraper

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CHROME_BINARY = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CHROME_READY_TIMEOUT_SECONDS = 15.0
EXPECTED_CARD_COUNT = 36


@dataclass
class RunningChrome:
    port: int
    process: subprocess.Popen
    log_path: Path

    @property
    def attach_address(self) -> str:
        return f"127.0.0.1:{self.port}"


class _QuietRequestHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_debugger(port: int, process: subprocess.Popen, log_path: Path) -> None:
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + CHROME_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(
                f"Chrome exited with code {process.returncode} during startup; "
                f"log tail:\n{log_path.read_text(encoding='utf-8', errors='replace')}"
            )
        try:
            with urllib.request.urlopen(endpoint, timeout=1) as response:
                payload = json.load(response)
        except (OSError, ValueError):
            time.sleep(0.25)
            continue
        if payload.get("Browser"):
            return
    pytest.fail(f"Chrome DevTools endpoint {endpoint} was not ready in time")


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


@pytest.fixture()
def fixture_http_server():
    handler = functools.partial(_QuietRequestHandler, directory=str(FIXTURES_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def running_chrome(tmp_path: Path):
    if not (CHROME_BINARY.is_file() and os.access(CHROME_BINARY, os.X_OK)):
        pytest.skip(f"real Chrome is not available/executable at {CHROME_BINARY}")
    debug_port = _free_port()
    profile_dir = tmp_path / "chrome-profile"
    log_path = tmp_path / "chrome.log"
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
            yield RunningChrome(port=debug_port, process=process, log_path=log_path)
        finally:
            _terminate(process)


def _write_io_config(tmp_path: Path, attach_address: str, start_url: str) -> Path:
    config = {
        "start_url": start_url,
        "user_agent": "IOFixtureBot/1.0 (+mailto:test@example.com)",
        "output_csv": "listings.csv",
        "checkpoint_file": "checkpoint.json",
        "error_directory": "errors",
        "report_file": "report.json",
        "attach_address": attach_address,
        "page_wait_seconds": 15,
        "request_delay_seconds": 0,
        "request_jitter_seconds": 0,
        "max_pages": 1,
        "headless": True,
        "pagination": {
            "mode": "next_button",
            "next_selector": "a.next",
            "disabled_attribute": "aria-disabled",
        },
        # Production-like scroll settings with the shipped defaults for
        # settle_seconds / step_delay_seconds: this is the path under test.
        "scrolling": {
            "enabled": True,
            "max_rounds": 80,
            "stable_rounds": 3,
            "poll_seconds": 0.2,
            "scroll_fraction": 0.8,
        },
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
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def test_io_only_fixture_hydrates_every_card_and_late_field(
    running_chrome: RunningChrome,
    fixture_http_server: str,
    tmp_path: Path,
) -> None:
    """Fast scrolling must still deliver every IO-driven field, including the
    brokerage that arrives 300 ms after a card hydrates."""
    start_url = f"{fixture_http_server}/lazy_cards_io_fixture.html"
    config_path = _write_io_config(tmp_path, running_chrome.attach_address, start_url)

    scraper = property_scraper.PropertyScraper(property_scraper.Settings.load(config_path))
    scraper.run()

    with (tmp_path / "listings.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == EXPECTED_CARD_COUNT
    ids = {row["listing_id"] for row in rows}
    assert ids == {str(20000000 + index) for index in range(EXPECTED_CARD_COUNT)}

    missing: list[tuple[str, str]] = []
    for row in rows:
        for field in ("listing_id", "address", "price", "beds", "baths", "sqft", "agent", "url"):
            if not row.get(field):
                missing.append((row["listing_id"], field))
    assert not missing, f"fields dropped by fast scrolling: {missing}"

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["pages_completed"] == 1
    assert report["stop_reason"] == "pagination_exhausted"
