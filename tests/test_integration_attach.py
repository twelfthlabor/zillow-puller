"""End-to-end attach-mode tests against a real local Chrome.

``tests/test_property_scraper.py`` stubs the driver, so it can only prove that
``attach_address`` is *configured*; it cannot prove the real browser contract.
These tests are the layer that does: they launch a dedicated headless Chrome
with a DevTools port,   attach to it through ``attach_address``, collect the
Zillow-shaped fixture cards into CSV + report, and assert the attached browser
process is still running afterwards (attach mode must never quit a browser it
did not launch). A second test drives the ``#px-captcha`` path to exit code 3
and checks the browser again. Because a Chrome process and its tabs survive
``session.quit()`` in attach mode, a third in-process test spies on the real
attached session's ``quit`` method — the only variant that actually fails if
the no-quit guard regresses. A fourth test attaches to a dead local URL and
proves the swallowed CDP navigation failure still ends as exit 1 /
``browser_error``. A final test needs no Chrome at all: a dead
``attach_address`` must fail fast (exit 1, no CSV) instead of hanging in the
session startup path.
"""

from __future__ import annotations

import csv
import functools
import http.server
import json
import os
import socket
import subprocess
import sys
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


@dataclass
class RunningChrome:
    """A Chrome process launched by the test on a dedicated debug port."""

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
    """Serve the fixtures directory without writing request logs to stderr."""

    def log_message(self, *args: object) -> None:
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _log_tail(path: Path, limit: int = 2000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "<no log>"
    return text[-limit:]


def _wait_for_debugger(port: int, process: subprocess.Popen, log_path: Path) -> None:
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + CHROME_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(
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
    pytest.fail(
        f"Chrome DevTools endpoint {endpoint} was not ready within "
        f"{CHROME_READY_TIMEOUT_SECONDS:.0f}s; log tail:\n{_log_tail(log_path)}"
    )


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
    """Serve ``tests/fixtures`` over a loopback HTTP server for the run."""
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
    """Launch a dedicated headless Chrome and yield its process handle."""
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


def _write_attach_config(
    tmp_path: Path,
    *,
    attach_address: str,
    start_url: str,
    page_wait_seconds: float = 0.5,
    max_attempts_per_page: int = 3,
    backoff_base_seconds: float = 5,
) -> Path:
    """Write a scraper config whose relative paths all resolve inside tmp_path."""
    config = {
        "start_url": start_url,
        "user_agent": "AttachFixtureBot/1.0 (+mailto:test@example.com)",
        "output_csv": "listings.csv",
        "checkpoint_file": "checkpoint.json",
        "error_directory": "errors",
        "report_file": "report.json",
        "attach_address": attach_address,
        "page_wait_seconds": page_wait_seconds,
        "request_delay_seconds": 0,
        "request_jitter_seconds": 0,
        "max_attempts_per_page": max_attempts_per_page,
        "backoff_base_seconds": backoff_base_seconds,
        "max_pages": 1,
        "headless": True,
        "pagination": {
            "mode": "next_button",
            "next_selector": "a.next",
            "disabled_attribute": "aria-disabled",
        },
        "scrolling": {"enabled": False},
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


def _run_scraper(config_path: Path) -> subprocess.CompletedProcess:
    """Run the real scraper subprocess against the tmp config.

    ``sys.executable`` is ``.venv313/bin/python`` whenever pytest runs from the
    project venv, and ``cwd`` keeps ``python -m property_scraper`` importing
    the checkout under test. The product attaches over the DevTools protocol,
    so no chromedriver/PATH workaround is needed.
    """
    return subprocess.run(
        [sys.executable, "-m", "property_scraper", "--config", str(config_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )


def test_attach_mode_collects_cards_and_leaves_browser_running(
    running_chrome: RunningChrome, fixture_http_server: str, tmp_path: Path
) -> None:
    start_url = f"{fixture_http_server}/attach_fixture.html"
    config_path = _write_attach_config(
        tmp_path,
        attach_address=running_chrome.attach_address,
        start_url=start_url,
    )
    assert running_chrome.alive, "fixture browser must be up before the run"

    result = _run_scraper(config_path)

    assert result.returncode == 0, result.stderr
    with (tmp_path / "listings.csv").open(newline="", encoding="utf-8") as handle:
        rows = {row["listing_id"]: row for row in csv.DictReader(handle)}
    assert set(rows) == {"12345678", "87654321"}

    first = rows["12345678"]
    assert first["address"] == "123 Maple St, Toronto, ON"
    assert first["price"] == "599900"
    assert first["beds"] == "3"
    assert first["baths"] == "2"
    assert first["sqft"] == "1500"
    assert first["agent"] == "Royal LePage Realty"
    assert first["url"].endswith("/12345678_zpid/")
    assert first["source_page"] == start_url

    second = rows["87654321"]
    assert second["price"] == "1250000"
    assert second["beds"] == "4"
    assert second["baths"] == "3.5"
    assert second["sqft"] == "2200"
    assert second["agent"] == "Sutton Group Realty Ltd."
    assert second["url"].endswith("/87654321_zpid/")

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["stop_reason"] == "pagination_exhausted"
    assert report["pages_completed"] == 1
    assert report["records_added"] == 2

    assert running_chrome.alive, "attach mode must not quit the attached browser"


def test_attach_mode_challenge_exits_three_and_leaves_browser_running(
    running_chrome: RunningChrome, fixture_http_server: str, tmp_path: Path
) -> None:
    start_url = f"{fixture_http_server}/attach_challenge_fixture.html"
    config_path = _write_attach_config(
        tmp_path,
        attach_address=running_chrome.attach_address,
        start_url=start_url,
        page_wait_seconds=0.5,
    )
    assert running_chrome.alive, "fixture browser must be up before the run"

    result = _run_scraper(config_path)

    assert result.returncode == 3, result.stderr
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["stop_reason"] == "challenge"
    assert report["challenges"] == 1
    assert report["pages_completed"] == 0
    assert not (tmp_path / "listings.csv").exists()

    assert running_chrome.alive, "attach mode must not quit the attached browser"


def test_attached_real_chrome_never_invokes_session_quit(
    running_chrome: RunningChrome,
    fixture_http_server: str,
    tmp_path: Path,
) -> None:
    """Spy on the real attached session: process aliveness cannot catch a quit.

    An attached Chrome survives ``session.quit()`` (its process and tab stay
    up), so the subprocess tests above would pass even if the no-quit guard
    regressed. Wrapping the real session's ``quit`` is the variant that fails
    when the guard is removed, while still driving a real browser, fixture
    server, and extraction pipeline in-process.
    """
    start_url = f"{fixture_http_server}/attach_fixture.html"
    config_path = _write_attach_config(
        tmp_path,
        attach_address=running_chrome.attach_address,
        start_url=start_url,
    )
    scraper = property_scraper.PropertyScraper(property_scraper.Settings.load(config_path))
    quit_calls: list[str] = []
    build_session = scraper._build_session

    def build_session_with_quit_spy(initial_url: str):
        session = build_session(initial_url)
        original_quit = session.quit

        def spy_quit() -> None:
            quit_calls.append("quit")
            original_quit()

        session.quit = spy_quit
        return session

    scraper._build_session = build_session_with_quit_spy
    scraper.run()

    assert quit_calls == []
    assert running_chrome.alive, "attach mode must not quit the attached browser"


def test_non_scrolling_extraction_uses_rendered_text(
    running_chrome: RunningChrome, fixture_http_server: str, tmp_path: Path
) -> None:
    """Non-attribute text fields must be rendered text (innerText), not raw
    text-node concatenation: fragments from a script tag or a hidden span
    inside the matched node must never reach the CSV."""
    start_url = f"{fixture_http_server}/rendered_text_fixture.html"
    config_path = _write_attach_config(
        tmp_path,
        attach_address=running_chrome.attach_address,
        start_url=start_url,
    )

    result = _run_scraper(config_path)

    assert result.returncode == 0, result.stderr
    csv_text = (tmp_path / "listings.csv").read_text(encoding="utf-8")
    assert "SCRIPTTEXT" not in csv_text, f"script text leaked into CSV:\n{csv_text}"
    assert "HIDDENTEXT" not in csv_text, f"hidden text leaked into CSV:\n{csv_text}"
    with (tmp_path / "listings.csv").open(newline="", encoding="utf-8") as handle:
        rows = {row["listing_id"]: row for row in csv.DictReader(handle)}
    assert rows["12345678"]["address"] == "123 Maple St, Toronto, ON"
    assert rows["12345678"]["price"] == "599900"


def test_attach_navigation_failure_retries_and_reports_browser_error(
    running_chrome: RunningChrome, tmp_path: Path
) -> None:
    """A dead target URL must still take the retry/backoff path.

    CDP ``get`` prints "Timeout loading ..." and returns on a timed-out
    navigation, so a failed navigation would otherwise look like a rendered
    empty page. The session must detect it and end as exit 1 with
    stop_reason ``browser_error`` (not ``empty_page_threshold``).
    """
    dead_address = f"127.0.0.1:{_free_port()}"
    config_path = _write_attach_config(
        tmp_path,
        attach_address=running_chrome.attach_address,
        start_url=f"http://{dead_address}/unreachable.html",
        max_attempts_per_page=2,
        backoff_base_seconds=0.01,
    )

    result = _run_scraper(config_path)

    assert result.returncode == 1, result.stderr
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["stop_reason"] == "browser_error"
    assert report["challenges"] == 0
    assert report["pages_completed"] == 0
    assert not (tmp_path / "listings.csv").exists()
    assert running_chrome.alive, "attach mode must not quit the attached browser"


def test_attach_dead_endpoint_exits_one_fast_without_writing_csv(tmp_path: Path) -> None:
    """A dead attach endpoint fails fast; no Chrome is launched for this test."""
    dead_address = f"127.0.0.1:{_free_port()}"
    config_path = _write_attach_config(
        tmp_path,
        attach_address=dead_address,
        start_url=f"http://{dead_address}/unused.html",
    )

    started = time.monotonic()
    result = _run_scraper(config_path)
    elapsed = time.monotonic() - started

    assert result.returncode == 1, result.stderr
    assert elapsed < 10.0, f"fast-fail took {elapsed:.1f}s; expected well under 10s"
    assert "unreachable" in result.stderr
    assert "launch_attach_chrome" in result.stderr
    assert not (tmp_path / "listings.csv").exists()
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["stop_reason"] == "browser_error"
