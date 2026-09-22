from __future__ import annotations

import csv
import gzip
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

import property_scraper
from property_scraper import (
    CSV_FIELDS,
    AccessChallengeError,
    BrowserError,
    PropertyScraper,
    Settings,
    _write_page_archive,
)


def make_config(tmp_path: Path, **overrides: object) -> Path:
    raw: dict[str, object] = {
        "start_url": "https://example.com/properties?page=1",
        "user_agent": "TestBot/1.0 (+mailto:test@example.com)",
        "output_csv": "listings.csv",
        "checkpoint_file": "checkpoint.json",
        "error_directory": "errors",
        "page_wait_seconds": 0.1,
        "request_delay_seconds": 0,
        "request_jitter_seconds": 0,
        "max_pages": 5,
        "headless": True,
        "pagination": {"mode": "next_button", "next_selector": "a.next"},
        "scrolling": {"enabled": False},
        "selectors": {
            "card": "[data-test='property-card']",
            "listing_id": {
                "selector": "[data-listing-id]",
                "attribute": "data-listing-id",
            },
            "url": {"selector": "a", "attribute": "href"},
            "price": {
                "selector": ".price",
                "transform": "money",
                "validation": {"min": 0, "max": 50000000},
            },
        },
    }
    raw.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


class ChallengeSession:
    """CDP-session stub for the challenge detector.

    ``selector_hits`` are the CSS selectors whose ``document.querySelector``
    check should report a match. ``visible_text`` is what the
    ``document.body.innerText`` expression returns; it defaults to
    ``page_source`` so DOM/text-marker tests can keep using one string.
    ``text_error=True`` makes the innerText script raise while the selector
    checks keep working (the raw-Selenium fake's ``script_error`` behavior).
    """

    def __init__(
        self,
        selector_hits: set[str] = frozenset(),
        page_source: str = "",
        visible_text: str | None = None,
        text_error: bool = False,
    ) -> None:
        self.selector_hits = selector_hits
        self.page_source = page_source
        self.visible_text = page_source if visible_text is None else visible_text
        self.text_error = text_error

    def find_elements(self, selector: str) -> list[object]:
        return []

    def evaluate(self, expression: str) -> object:
        if "document.body" in expression:
            if self.text_error:
                raise BrowserError("script execution failed")
            return self.visible_text
        return any(json.dumps(selector) in expression for selector in self.selector_hits)

    def get_page_source(self) -> str:
        return self.page_source


class FailingSession:
    """Navigation always fails transiently, to exercise retry/backoff."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_current_url(self) -> str:
        return "about:blank"

    def evaluate(self, expression: str) -> object:
        return "stub-marker"  # the document is never replaced

    def get(self, url: str) -> None:
        self.calls.append(url)
        raise BrowserError("navigation failed")

    def find_elements(self, selector: str) -> list[object]:
        return []

    def get_page_source(self) -> str:
        return "<html><body></body></html>"

    def save_screenshot(self, path: str) -> None:
        self.calls.append(f"shot:{path}")

    def quit(self) -> None:
        self.calls.append("quit")


class RetryScraper(PropertyScraper):
    def _build_session(self, initial_url: str) -> FailingSession:
        return FailingSession()


class SilentNavigationSession:
    """CDP ``get`` swallowed the load timeout: the URL never changed."""

    def __init__(self, current_url: str = "about:blank") -> None:
        self.current_url = current_url
        self.calls: list[str] = []

    def get_current_url(self) -> str:
        return self.current_url

    def evaluate(self, expression: str) -> object:
        if "window.__property_scraper_nav_marker" in expression:
            return "stub-marker"  # same document: the marker survives
        return ""

    def get(self, url: str) -> None:
        self.calls.append(url)  # no navigation commits

    def find_elements(self, selector: str) -> list[object]:
        return []

    def get_page_source(self) -> str:
        return "<html><body></body></html>"

    def save_screenshot(self, path: str) -> None:
        self.calls.append(f"shot:{path}")

    def quit(self) -> None:
        self.calls.append("quit")


class SilentNavigationScraper(PropertyScraper):
    def __init__(self, settings: Settings, current_url: str = "about:blank") -> None:
        super().__init__(settings)
        self.current_url = current_url

    def _build_session(self, initial_url: str) -> SilentNavigationSession:
        return SilentNavigationSession(self.current_url)


def test_checkpoint_round_trip_and_atomic_write(tmp_path: Path) -> None:
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)

    scraper._save_checkpoint("https://example.com/properties?page=2", 2)
    loaded = scraper._load_checkpoint()

    assert loaded["next_url"] == "https://example.com/properties?page=2"
    assert loaded["pages_completed"] == 2
    assert not (tmp_path / "checkpoint.json.tmp").exists()


def test_record_key_prefers_id(tmp_path: Path) -> None:
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    assert scraper._record_key({"listing_id": " 42 ", "url": "https://x/"}) == "id:42"
    assert (
        scraper._record_key({"listing_id": "", "url": "HTTPS://X.COM/A/"}) == "url:https://x.com/a"
    )


def test_field_validation_bounds_drop_absurd_values(tmp_path: Path) -> None:
    """Rows are ordered like the field rules: listing_id, url, price."""
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)

    records = scraper._records_from_card_data(
        [
            ["1", "/homedetails/1_zpid/", "$2,000,000,000"],
            ["2", "/homedetails/2_zpid/", "$1,234,567"],
        ],
        "https://example.com/properties?page=1",
    )

    assert records[0]["price"] == ""
    assert records[1]["price"] == "1234567"


def test_listing_id_fallback_extracts_from_url(tmp_path: Path) -> None:
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)

    records = scraper._records_from_card_data(
        [[None, "/homedetails/Example-St-Toronto-ON/464240149_zpid/", None]],
        "https://example.com/properties?page=1",
    )

    assert records[0]["listing_id"] == "464240149"


def test_invalid_settings_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pagination.mode"):
        Settings.load(make_config(tmp_path, pagination={"mode": "magic"})).validate()
    with pytest.raises(ValueError, match="cannot be negative"):
        Settings.load(make_config(tmp_path, request_delay_seconds=-1)).validate()
    with pytest.raises(ValueError, match="not a valid regex"):
        Settings.load(make_config(tmp_path, listing_id_pattern="([unclosed")).validate()


def test_transient_failure_retries_then_preserves_checkpoint(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        max_attempts_per_page=2,
        backoff_base_seconds=0.01,
    )
    settings = Settings.load(config)
    scraper = RetryScraper(settings)

    with pytest.raises(BrowserError):
        scraper.run()

    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["next_url"] == settings.start_url
    assert checkpoint["pages_completed"] == 0
    assert not (tmp_path / "checkpoint.json.tmp").exists()
    error_files = list((tmp_path / "errors").glob("*.json"))
    assert error_files, "expected a diagnostic sidecar after the fatal failure"


def test_silent_navigation_failure_is_browser_error_not_empty_page(
    tmp_path: Path,
) -> None:
    """CDP ``get`` prints "Timeout loading ..." and returns on a timed-out
    navigation. A session that never left its URL must be classified as a
    transient browser_error (and retried), never as a loaded empty page."""
    config = make_config(
        tmp_path,
        max_attempts_per_page=2,
        backoff_base_seconds=0.01,
        report_file="report.json",
    )
    scraper = SilentNavigationScraper(Settings.load(config))

    with pytest.raises(BrowserError, match="did not complete"):
        scraper.run()

    report = read_report(tmp_path)
    assert report["stop_reason"] == "browser_error"
    assert report["pages_completed"] == 0


def test_navigation_to_chrome_error_page_is_retryable(tmp_path: Path) -> None:
    """A navigation that lands on Chrome's network-error page is a browser
    error, not a rendered empty page."""
    config = make_config(tmp_path, backoff_base_seconds=0.01)
    scraper = SilentNavigationScraper(
        Settings.load(config), current_url="chrome-error://chromewebdata/"
    )
    scraper.session = scraper._build_session("https://example.com/properties?page=1")

    with pytest.raises(BrowserError, match="did not complete"):
        scraper._navigate("https://example.com/properties?page=1")


def test_navigation_already_on_target_is_not_a_failure(tmp_path: Path) -> None:
    """The attach-mode constructor already navigated; _navigate must accept a
    session that is sitting on the target URL without re-fetching it."""
    config = make_config(tmp_path)
    url = "https://example.com/properties?page=1"
    scraper = SilentNavigationScraper(Settings.load(config), current_url=url)
    scraper.session = scraper._build_session(url)

    scraper._navigate(url)

    assert scraper.session.calls == []


class RedirectBackSession:
    """A committed navigation whose final URL is where the tab already was.

    The attach tab sits on Zillow's canonical URL (with query parameters);
    navigating to the clean target commits a new document, but the site
    redirects back to the canonical URL, so the visible URL is unchanged.
    The pre-navigation document marker cannot survive a new document.
    """

    def __init__(self, canonical: str) -> None:
        self.canonical = canonical
        self.current_url = canonical
        self.navigations: list[str] = []
        self.marker_survives = False

    def get_current_url(self) -> str:
        return self.current_url

    def evaluate(self, expression: str) -> object:
        if "window.__property_scraper_nav_marker" in expression:
            if "=" in expression:
                # marker injection; a document still holds it for now
                self.marker_survives = True
                return "stub-marker"
            return "stub-marker" if self.marker_survives else ""
        return False

    def get(self, url: str) -> None:
        self.navigations.append(url)
        self.current_url = self.canonical  # redirect back to the same URL
        self.marker_survives = False  # ... but a new document committed

    def goto_if_not_url(self, url: str) -> None:
        self.get(url)


class RecordingNavigationSession:
    """Records navigation calls; never changes the URL or the document."""

    def __init__(self, current_url: str) -> None:
        self.current_url = current_url
        self.navigations: list[str] = []

    def get_current_url(self) -> str:
        return self.current_url

    def evaluate(self, expression: str) -> object:
        return "stub-marker"  # same document: a marker would survive

    def get(self, url: str) -> None:
        self.navigations.append(url)

    def goto_if_not_url(self, url: str) -> None:
        self.navigations.append(url)


def test_redirect_back_to_canonical_is_not_a_browser_error(tmp_path: Path) -> None:
    """A navigation that commits and redirects back to the starting URL is
    not a swallowed timeout: the URL is unchanged but the document is new."""
    canonical = "https://www.zillow.com/toronto-on/?searchQueryState=%7B%7D"
    target = "https://www.zillow.com/toronto-on/"
    config = make_config(tmp_path)
    scraper = PropertyScraper(Settings.load(config))
    scraper.session = RedirectBackSession(canonical)

    scraper._navigate(target)

    assert scraper.session.navigations == [target]


def test_navigate_skips_redundant_navigation_on_normalized_match(
    tmp_path: Path,
) -> None:
    """Raw differ, normalized equal: the attach constructor already loaded
    this URL, so _navigate must not issue a second request."""
    current = "https://example.com/properties/?page=1"
    target = "https://example.com/properties?page=1"
    config = make_config(tmp_path)
    scraper = PropertyScraper(Settings.load(config))
    scraper.session = RecordingNavigationSession(current)

    scraper._navigate(target)

    assert scraper.session.navigations == []


def test_challenge_detected_by_dom_selector(tmp_path: Path) -> None:
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    scraper.session = ChallengeSession(selector_hits={"#px-captcha"})
    assert scraper._detect_challenge() is True


def test_challenge_detected_by_strong_text_marker(tmp_path: Path) -> None:
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    scraper.session = ChallengeSession(
        page_source="<html>Access to this page has been denied</html>"
    )
    assert scraper._detect_challenge() is True


def test_weak_captcha_string_is_not_a_challenge(tmp_path: Path) -> None:
    """The bare word 'captcha' appears in ordinary Zillow markup; a slow
    legitimate page must not be mislabeled as an access challenge."""
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    scraper.session = ChallengeSession(
        page_source="<html>window.__captcha_config__ = {}; recaptcha.js loading</html>"
    )
    assert scraper._detect_challenge() is False


def test_challenge_markers_in_hidden_markup_are_ignored(tmp_path: Path) -> None:
    """Markers only present in page source (inline JS/templates) are not a
    challenge when the visible text is an ordinary page."""
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    scraper.session = ChallengeSession(
        page_source='<html><script>var msg = "press &amp; hold";</script></html>',
        visible_text="Toronto homes for sale",
    )
    assert scraper._detect_challenge() is False


def test_challenge_falls_back_to_page_source_when_script_fails(
    tmp_path: Path,
) -> None:
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    scraper.session = ChallengeSession(
        page_source="<html>Access to this page has been denied</html>",
        visible_text="",
        text_error=True,
    )
    assert scraper._detect_challenge() is True


def test_challenge_detected_when_innertext_is_blank(tmp_path: Path) -> None:
    """Block pages can render blank visible text while the source carries the
    marker; the page source must still be scanned."""
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    scraper.session = ChallengeSession(
        page_source="<html>Access to this page has been denied</html>",
        visible_text="   \n\t ",
    )
    assert scraper._detect_challenge() is True


class ScriptedSession:
    """Browser stub for run() paths that never touch a page."""

    def __init__(self) -> None:
        self.page_source = "<html><body>stub</body></html>"
        self.calls: list[str] = []

    def get_page_source(self) -> str:
        return self.page_source

    def quit(self) -> None:
        self.calls.append("quit")

    def save_screenshot(self, path: str) -> None:
        self.calls.append(f"shot:{path}")


class ScriptedScraper(PropertyScraper):
    """Drive run() with scripted pages instead of a real browser."""

    def __init__(
        self,
        settings: Settings,
        pages: list[list[dict[str, str]]] | None = None,
        failures: list[Exception | None] | None = None,
        next_urls: list[str] | None = None,
        loop_url: str | None = None,
    ) -> None:
        super().__init__(settings)
        self.pages = list(pages or [])
        self.failures: list[Exception | None] = list(failures or [])
        self.next_urls = list(next_urls or [])
        self.loop_url = loop_url
        self.loaded: list[str] = []
        self.sessions: list[ScriptedSession] = []

    def _build_session(self, initial_url: str) -> ScriptedSession:
        session = ScriptedSession()
        self.sessions.append(session)
        return session

    def _load_page_with_retry(self, page_url: str) -> list[dict[str, str]]:
        self.loaded.append(page_url)
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        return self.pages.pop(0) if self.pages else []

    def _next_url(self, current_url: str, page_number: int) -> str | None:
        if self.loop_url is not None:
            return self.loop_url
        if self.next_urls:
            return self.next_urls.pop(0)
        return None


class BulkDriver:
    """Fake session returning scripted results for the bulk scroll script."""

    page_source = "<html><body>bulk</body></html>"

    def __init__(self, rounds: list[dict[str, object]]) -> None:
        self.rounds = list(rounds)
        self.scripts: list[str] = []

    def evaluate(self, expression: str) -> bool:
        return True  # cards are present, so the poll proceeds to find_elements

    def find_elements(self, selector: str) -> list[object]:
        return [object()]

    def execute_script(self, script: str) -> object:
        self.scripts.append(script)
        return self.rounds.pop(0)

    def get_page_source(self) -> str:
        return self.page_source

    def quit(self) -> None:
        pass


class FakeClock:
    """Deterministic ``time`` stand-in for the settle-window logic."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_record(listing_id: str, price: str = "$100,000") -> dict[str, str]:
    return {
        "listing_id": listing_id,
        "url": f"https://example.com/homedetails/{listing_id}_zpid/",
        "price": price,
    }


def seed_csv(path: Path, records: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(record)


def read_report(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))


# --- max_pages 0 = unlimited -------------------------------------------------


def test_max_pages_zero_means_unlimited(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_pages=0, report_file="report.json")
    scraper = ScriptedScraper(
        Settings.load(config),
        pages=[[make_record("1")], [make_record("2")], [make_record("3")]],
        next_urls=[
            "https://example.com/properties?page=2",
            "https://example.com/properties?page=3",
        ],
    )
    scraper.run()

    assert len(scraper.loaded) == 3
    report = read_report(tmp_path)
    assert report["pages_completed"] == 3
    assert report["records_added"] == 3
    assert report["stop_reason"] == "pagination_exhausted"


def test_max_pages_zero_valid_negative_rejected(tmp_path: Path) -> None:
    Settings.load(make_config(tmp_path, max_pages=0)).validate()
    with pytest.raises(ValueError, match="max_pages cannot be negative"):
        Settings.load(make_config(tmp_path, max_pages=-1)).validate()


# --- incremental refresh stop ------------------------------------------------


def test_incremental_stop_trigger_sets_null_checkpoint(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        max_pages=0,
        report_file="report.json",
        incremental_stop={"enabled": True, "known_ratio": 0.5, "min_records": 2},
    )
    seed_csv(tmp_path / "listings.csv", [make_record("1"), make_record("2")])
    scraper = ScriptedScraper(
        Settings.load(config),
        pages=[[make_record("1"), make_record("2"), make_record("3")]],
        next_urls=["https://example.com/properties?page=2"],
    )
    scraper.run()

    assert len(scraper.loaded) == 1  # stopped before fetching the next page
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["next_url"] is None
    assert checkpoint["pages_completed"] == 1
    report = read_report(tmp_path)
    assert report["stop_reason"] == "incremental_complete"
    assert report["records_added"] == 1
    assert report["records_total"] == 3


def test_incremental_stop_skipped_when_page_is_mostly_new(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        max_pages=0,
        incremental_stop={"enabled": True, "known_ratio": 0.8, "min_records": 2},
    )
    seed_csv(tmp_path / "listings.csv", [make_record("1")])
    scraper = ScriptedScraper(
        Settings.load(config),
        pages=[[make_record("1"), make_record("2"), make_record("3")], [make_record("4")]],
        next_urls=["https://example.com/properties?page=2"],
    )
    scraper.run()

    # Only 1 of 3 records was already known (< 0.8), so page 2 was still fetched.
    assert len(scraper.loaded) == 2
    assert scraper.loaded[1] == "https://example.com/properties?page=2"


def test_incremental_stop_skipped_below_min_records(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        max_pages=0,
        incremental_stop={"enabled": True, "known_ratio": 0.8, "min_records": 5},
    )
    seed_csv(tmp_path / "listings.csv", [make_record("1")])
    scraper = ScriptedScraper(
        Settings.load(config),
        pages=[[make_record("1")], [make_record("2")]],
        next_urls=["https://example.com/properties?page=2"],
    )
    scraper.run()

    # 100% known but only 1 record < min_records: page 2 was still fetched.
    assert len(scraper.loaded) == 2
    assert scraper.loaded[1] == "https://example.com/properties?page=2"


def test_incremental_stop_only_applies_to_fresh_sweeps(tmp_path: Path) -> None:
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh_config = make_config(
        fresh_dir,
        max_pages=0,
        report_file="report.json",
        incremental_stop={"enabled": True, "known_ratio": 0.5, "min_records": 2},
    )
    seed_csv(fresh_dir / "listings.csv", [make_record("1"), make_record("2")])
    fresh = ScriptedScraper(
        Settings.load(fresh_config),
        pages=[[make_record("1"), make_record("2"), make_record("3")]],
        next_urls=["https://example.com/properties?page=2"],
    )
    fresh.run()
    assert len(fresh.loaded) == 1
    assert read_report(fresh_dir)["stop_reason"] == "incremental_complete"

    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    resume_config = make_config(
        resume_dir,
        max_pages=0,
        report_file="report.json",
        incremental_stop={"enabled": True, "known_ratio": 0.5, "min_records": 2},
    )
    seed_csv(resume_dir / "listings.csv", [make_record("1"), make_record("2")])
    (resume_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "next_url": "https://example.com/properties?page=5",
                "pages_completed": 4,
            }
        ),
        encoding="utf-8",
    )
    resumed = ScriptedScraper(
        Settings.load(resume_config),
        pages=[
            [make_record("1"), make_record("2"), make_record("3")],
            [make_record("9")],
        ],
        next_urls=["https://example.com/properties?page=6"],
    )
    resumed.run()

    # The resumed page is mostly known, but a mid-sweep resume must keep going.
    assert len(resumed.loaded) == 2
    assert resumed.loaded[0] == "https://example.com/properties?page=5"
    assert resumed.loaded[1] == "https://example.com/properties?page=6"
    report = read_report(resume_dir)
    assert report["stop_reason"] == "pagination_exhausted"
    assert report["resumed_from_pages"] == 4


# --- bulk card extraction ----------------------------------------------------


def test_records_from_card_data_maps_missing_and_fallbacks(tmp_path: Path) -> None:
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    cards = [
        [None, "/homedetails/Example-St/464240149_zpid/", "$1,200,000"],
        ["ID-9", "/homedetails/X/9_zpid/", None],
    ]

    records = scraper._records_from_card_data(cards, "https://example.com/properties?page=1")

    first, second = records
    # Missing listing_id is recovered from the URL via the configured regex.
    assert first["listing_id"] == "464240149"
    assert first["url"] == ("https://example.com/homedetails/Example-St/464240149_zpid/")
    assert first["price"] == "1200000"
    assert first["source_page"] == "https://example.com/properties?page=1"
    # An explicit id wins; missing fields stay empty without raising.
    assert second["listing_id"] == "ID-9"
    assert second["price"] == ""
    assert set(second) == set(CSV_FIELDS)


def test_collect_page_records_bulk_script_and_merge(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        scrolling={
            "enabled": True,
            "max_rounds": 5,
            "stable_rounds": 2,
            "poll_seconds": 0,
            "settle_seconds": 0,
            "step_delay_seconds": 0,
        },
    )
    scraper = PropertyScraper(Settings.load(config))
    driver = BulkDriver(
        [
            {
                "scrollTop": 0,
                "scrollHeight": 1000,
                "clientHeight": 500,
                "cards": [["1", "/a", None]],
            },
            {
                "scrollTop": 500,
                "scrollHeight": 1000,
                "clientHeight": 500,
                "cards": [["1", "/a", "$200"], ["2", "/b", "$50"]],
            },
            {
                "scrollTop": 500,
                "scrollHeight": 1000,
                "clientHeight": 500,
                "cards": [["1", "/a", "$200"], ["2", "/b", "$50"]],
            },
            {
                "scrollTop": 500,
                "scrollHeight": 1000,
                "clientHeight": 500,
                "cards": [["1", "/a", "$200"], ["2", "/b", "$50"]],
            },
        ]
    )
    scraper.session = driver

    records = scraper._collect_page_records("https://example.com/properties")

    by_id = {record["listing_id"]: record for record in records}
    assert set(by_id) == {"1", "2"}
    # The empty price from round 1 is filled in when round 2 provides it.
    assert by_id["1"]["price"] == "200"
    # One self-contained scroll/collect script per poll round, no element
    # handles or positional arguments crossing the CDP boundary.
    assert len(driver.scripts) == 4
    assert all("querySelectorAll" in script for script in driver.scripts)
    assert all("arguments[" not in script for script in driver.scripts)


def test_collect_page_records_falls_back_to_rendered_text_without_container(
    tmp_path: Path,
) -> None:
    """No scrollable pane: extract via the rendered-text (innerText) script,
    never via raw element text."""
    config = make_config(
        tmp_path,
        scrolling={"enabled": True, "max_rounds": 5, "stable_rounds": 1},
    )
    scraper = PropertyScraper(Settings.load(config))

    class NoContainerDriver(BulkDriver):
        def __init__(self) -> None:
            super().__init__([{"hasContainer": False, "cards": []}])

        def execute_script(self, script: str) -> object:
            self.scripts.append(script)
            if "innerText" in script:
                return {"cards": [["9", "/homedetails/9_zpid/", "$99"]]}
            return self.rounds.pop(0)

    driver = NoContainerDriver()
    scraper.session = driver

    records = scraper._collect_page_records("https://example.com/properties")

    assert [record["listing_id"] for record in records] == ["9"]
    assert records[0]["price"] == "99"
    assert any("innerText" in script for script in driver.scripts)


def test_poll_seconds_falls_back_to_pause_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(
        tmp_path,
        scrolling={
            "enabled": True,
            "max_rounds": 2,
            "stable_rounds": 1,
            "pause_seconds": 0.25,
            "settle_seconds": 0,
        },
    )
    sleeps: list[float] = []
    monkeypatch.setattr(property_scraper.time, "sleep", sleeps.append)
    scraper = PropertyScraper(Settings.load(config))
    at_bottom = {
        "scrollTop": 500,
        "scrollHeight": 1000,
        "clientHeight": 500,
        "cards": [],
    }
    scraper.session = BulkDriver([dict(at_bottom), dict(at_bottom)])

    scraper._collect_page_records("https://example.com/properties")

    # The first round waits out pause_seconds; the second is stable and stops.
    assert sleeps == [0.25]


def test_collect_page_records_stops_early_when_values_settle(tmp_path: Path) -> None:
    """A stable pane stops after stable_rounds; a late field change resets it."""
    config = make_config(
        tmp_path,
        scrolling={
            "enabled": True,
            "max_rounds": 10,
            "stable_rounds": 2,
            "poll_seconds": 0,
            "settle_seconds": 0,
        },
    )
    scraper = PropertyScraper(Settings.load(config))

    def round_data(price: str | None) -> dict[str, object]:
        return {
            "scrollTop": 500,
            "scrollHeight": 1000,
            "clientHeight": 500,
            "cards": [["1", "/homedetails/1_zpid/", price]],
        }

    driver = BulkDriver(
        [
            round_data(None),  # first read: details not hydrated yet
            round_data("$100"),  # hydration resets stability
            round_data("$100"),
            round_data("$100"),
        ]
    )
    scraper.session = driver

    records = scraper._collect_page_records("https://example.com/properties")

    assert records[0]["price"] == "100"
    # Round 1 changed (empty price), round 2 changed (price arrived), rounds
    # 3-4 are stable -> stop. max_rounds=10 must not be reached.
    assert len(driver.scripts) == 4


class ScriptedPriceDriver(BulkDriver):
    """Stable at-bottom pane whose price follows a per-round script.

    The last scripted price sticks once the list is exhausted, so a test can
    express "nothing changes after round N" without dropping the value again.
    """

    def __init__(self, prices: list[str | None]) -> None:
        super().__init__([])
        self.prices = list(prices)
        self.rounds_run = 0

    def execute_script(self, script: str) -> object:
        self.scripts.append(script)
        price = self.prices[min(self.rounds_run, len(self.prices) - 1)]
        self.rounds_run += 1
        return {
            "scrollTop": 500,
            "scrollHeight": 1000,
            "clientHeight": 500,
            "cards": [["1", "/homedetails/1_zpid/", price]],
        }


def test_settle_quiet_period_captures_change_after_old_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change at t=+3.5s — after a 2.5s-from-loop-start floor would have
    stopped — is captured because the quiet period is anchored to the last
    observed change (t=+1.0s), so polling continues until t=+3.5s."""
    clock = FakeClock()
    monkeypatch.setattr(property_scraper, "time", clock)
    config = make_config(
        tmp_path,
        scrolling={
            "enabled": True,
            "max_rounds": 20,
            "stable_rounds": 1,
            "poll_seconds": 0.5,
            "settle_seconds": 2.5,
        },
    )
    scraper = PropertyScraper(Settings.load(config))
    scraper.session = ScriptedPriceDriver(
        [None, None, "$100", "$100", "$100", "$100", "$100", "$250"]
    )

    records = scraper._collect_page_records("https://example.com/properties")

    assert records[0]["price"] == "250"
    # The change at round 8 (t=3.5s) restarts the quiet period, so the loop
    # polls until t=6.0s instead of stopping around t=2.5s.
    assert scraper.session.rounds_run == 13
    assert clock.now == pytest.approx(6.0)


def test_settle_window_stops_about_settle_seconds_after_a_quiet_bottom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing arriving after the bottom, the loop stops at the first
    stable poll at/after settle_seconds instead of the old ~4.8s band."""
    clock = FakeClock()
    monkeypatch.setattr(property_scraper, "time", clock)
    config = make_config(
        tmp_path,
        scrolling={
            "enabled": True,
            "max_rounds": 20,
            "stable_rounds": 1,
            "poll_seconds": 0.5,
            "settle_seconds": 2.5,
        },
    )
    scraper = PropertyScraper(Settings.load(config))
    scraper.session = ScriptedPriceDriver(["$100"])

    records = scraper._collect_page_records("https://example.com/properties")

    assert records[0]["price"] == "100"
    assert scraper.session.rounds_run == 6
    assert clock.now == pytest.approx(2.5)


def test_step_delay_paces_moving_rounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Moving rounds sleep step_delay_seconds (not the full poll interval),
    and a stable bottom round sleeps poll_seconds."""
    clock = FakeClock()
    monkeypatch.setattr(property_scraper, "time", clock)
    config = make_config(
        tmp_path,
        scrolling={
            "enabled": True,
            "max_rounds": 5,
            "stable_rounds": 1,
            "poll_seconds": 0.2,
            "settle_seconds": 0,
            "step_delay_seconds": 0.03,
        },
    )

    class TwoStepDriver(BulkDriver):
        def __init__(self) -> None:
            super().__init__([])
            self.rounds_run = 0

        def execute_script(self, script: str) -> object:
            self.scripts.append(script)
            self.rounds_run += 1
            return {
                "scrollTop": 0 if self.rounds_run == 1 else 500,
                "scrollHeight": 1000,
                "clientHeight": 500,
                "cards": [["1", "/homedetails/1_zpid/", "$100"]],
            }

    scraper = PropertyScraper(Settings.load(config))
    scraper.session = TwoStepDriver()

    scraper._collect_page_records("https://example.com/properties")

    # Round 1 is still moving (dwell), round 2 reaches the bottom (poll).
    assert clock.sleeps == [0.03, 0.2]


# --- empty pages --------------------------------------------------------------


class ZeroCardDriver:
    """Renders no cards at all; the card wait always expires."""

    def __init__(self) -> None:
        self.page_source = "<html><body>plain page, no cards</body></html>"
        self.calls: list[str] = []
        self.current_url = "about:blank"

    def get_current_url(self) -> str:
        return self.current_url

    def get(self, url: str) -> None:
        self.calls.append(url)
        self.current_url = url

    def find_elements(self, selector: str) -> list[object]:
        return []

    def evaluate(self, expression: str) -> object:
        if "document.body" in expression:
            return ""
        return False

    def get_page_source(self) -> str:
        return self.page_source

    def save_screenshot(self, path: str) -> None:
        self.calls.append(f"shot:{path}")

    def quit(self) -> None:
        self.calls.append("quit")


class EmptyThenGoodDriver:
    """The first page renders no cards, later pages render one card."""

    def __init__(self) -> None:
        self.gets = 0
        self.calls: list[str] = []
        self.current_url = "about:blank"

    @property
    def page_source(self) -> str:
        return "<html><body>plain page</body></html>"

    def get_current_url(self) -> str:
        return self.current_url

    def get(self, url: str) -> None:
        self.gets += 1
        self.calls.append(url)
        self.current_url = url

    def find_elements(self, selector: str) -> list[object]:
        if self.gets <= 1:
            return []
        return [object()]

    def execute_script(self, script: str) -> object:
        # Rendered-text extraction rows, ordered like the field rules.
        return {"cards": [["42", "/homedetails/42_zpid/", "$100,000"]]}

    def evaluate(self, expression: str) -> object:
        if "document.body" in expression:
            return "<html><body>plain page</body></html>"
        return self.gets > 1

    def get_page_source(self) -> str:
        return self.page_source

    def save_screenshot(self, path: str) -> None:
        self.calls.append(f"shot:{path}")

    def quit(self) -> None:
        self.calls.append("quit")


class LivePageScraper(PropertyScraper):
    """Uses the real page pipeline with an injected session."""

    def __init__(
        self,
        settings: Settings,
        session: object,
        next_urls: list[str] | None = None,
    ) -> None:
        super().__init__(settings)
        self.session = session
        self.next_urls = list(next_urls or [])

    def _build_session(self, initial_url: str) -> object:
        return self.session

    def _next_url(self, current_url: str, page_number: int) -> str | None:
        return self.next_urls.pop(0) if self.next_urls else None


def test_zero_card_page_counts_as_empty_not_browser_error(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        max_pages=0,
        empty_page_stop_threshold=3,
        report_file="report.json",
        page_wait_seconds=0.05,
    )
    driver = ZeroCardDriver()
    scraper = LivePageScraper(
        Settings.load(config),
        driver,
        next_urls=[
            "https://example.com/properties?page=2",
            "https://example.com/properties?page=3",
        ],
    )
    scraper.run()

    fetched = [call for call in driver.calls if call.startswith("http")]
    assert len(fetched) == 3
    report = read_report(tmp_path)
    assert report["pages_completed"] == 3
    assert report["records_added"] == 0
    assert report["stop_reason"] == "empty_page_threshold"
    # An empty page is not a failure, so no diagnostic capture happened.
    assert not (tmp_path / "errors").exists()


def test_challenge_detected_while_waiting_for_cards_without_full_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A challenge page raises as soon as markers are seen, not after the
    whole page_wait_seconds card timeout has elapsed."""
    config = make_config(tmp_path, page_wait_seconds=5.0)
    scraper = PropertyScraper(Settings.load(config))
    scraper.session = ChallengeSession(selector_hits={"#px-captcha"})
    clock = FakeClock()
    monkeypatch.setattr(property_scraper, "time", clock)

    started = time.monotonic()
    with pytest.raises(AccessChallengeError):
        scraper._collect_page_records("https://example.com/properties")
    elapsed = time.monotonic() - started

    # The DOM marker is found on the first poll, before any waiting.
    assert clock.sleeps == []
    assert elapsed < 1.0, f"challenge took {elapsed:.3f}s; expected no card wait"


def test_slow_legitimate_page_still_gets_the_full_card_wait(tmp_path: Path) -> None:
    """No challenge markers means the page gets its whole card wait; the empty
    classification is never made early."""
    config = make_config(tmp_path, page_wait_seconds=0.3)
    scraper = PropertyScraper(Settings.load(config))
    scraper.session = ZeroCardDriver()

    started = time.monotonic()
    records = scraper._collect_page_records("https://example.com/properties")
    elapsed = time.monotonic() - started

    assert records == []
    assert elapsed >= 0.25, f"returned after {elapsed:.3f}s; expected the full wait"


def test_cards_present_suppress_challenge_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A challenge marker on a page that did render cards is not a stop; the
    early challenge check only runs while no cards are present."""
    config = make_config(tmp_path)
    settings = Settings.load(config)

    class CardsAndChallengeDriver:
        page_source = "<html><body>cards plus a captcha widget</body></html>"

        def find_elements(self, selector: str) -> list[object]:
            if selector == settings.card_selector:
                return [object()]
            return []

        def execute_script(self, script: str) -> object:
            # Rendered-text extraction rows, ordered like the field rules.
            return {"cards": [["7", "/homedetails/7_zpid/", "$100,000"]]}

        def evaluate(self, expression: str) -> object:
            if "document.body" in expression:
                return self.page_source
            return True  # the card-presence gate

    scraper = PropertyScraper(settings)
    scraper.session = CardsAndChallengeDriver()
    calls: list[int] = []
    monkeypatch.setattr(scraper, "_detect_challenge", lambda: calls.append(1) or True)

    records = scraper._collect_page_records("https://example.com/properties")

    assert [record["listing_id"] for record in records] == ["7"]
    assert calls == []


def test_good_page_after_empty_page_resets_counter(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        max_pages=0,
        empty_page_stop_threshold=2,
        report_file="report.json",
        page_wait_seconds=0.05,
    )
    driver = EmptyThenGoodDriver()
    scraper = LivePageScraper(
        Settings.load(config),
        driver,
        next_urls=["https://example.com/properties?page=2"],
    )
    scraper.run()

    report = read_report(tmp_path)
    assert report["pages_completed"] == 2
    assert report["records_added"] == 1
    assert report["stop_reason"] == "pagination_exhausted"


def test_zero_record_exhaustion_is_partial_not_complete(tmp_path: Path) -> None:
    """A timeout page with no next control must not retire the region as done."""
    config = make_config(
        tmp_path,
        max_pages=0,
        empty_page_stop_threshold=3,
        report_file="report.json",
        page_wait_seconds=0.05,
    )
    scraper = LivePageScraper(Settings.load(config), ZeroCardDriver())
    scraper.run()

    report = read_report(tmp_path)
    assert report["pages_completed"] == 1
    assert report["records_total"] == 0
    # The run really ended (no next control) but with zero records ever.
    assert report["stop_reason"] == "empty_page_threshold"
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["next_url"] is None


def test_exhaustion_with_records_stays_pagination_exhausted(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_pages=0, report_file="report.json")
    scraper = ScriptedScraper(Settings.load(config), pages=[[make_record("1")]])
    scraper.run()

    report = read_report(tmp_path)
    assert report["records_total"] == 1
    assert report["stop_reason"] == "pagination_exhausted"


# --- page archive ------------------------------------------------------------


def test_write_page_archive_gzips_html(tmp_path: Path) -> None:
    path = _write_page_archive(tmp_path / "archive", 7, "<html>raw</html>")

    assert path.parent == tmp_path / "archive"
    assert re.fullmatch(r"\d{8}T\d{6}Z-page-007\.html\.gz", path.name)
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        assert handle.read() == "<html>raw</html>"


def test_run_archives_successful_pages_only(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        max_pages=1,
        report_file="report.json",
        archive_directory="archive",
    )
    scraper = ScriptedScraper(Settings.load(config), pages=[[make_record("1")]])
    scraper.run()

    archived = list((tmp_path / "archive").glob("*-page-001.html.gz"))
    assert len(archived) == 1
    with gzip.open(archived[0], "rt", encoding="utf-8") as handle:
        assert handle.read() == scraper.sessions[0].page_source

    challenge_dir = tmp_path / "challenge"
    challenge_dir.mkdir()
    challenge_config = make_config(
        challenge_dir,
        max_pages=1,
        report_file="report.json",
        archive_directory="archive-challenge",
    )
    challenged = ScriptedScraper(
        Settings.load(challenge_config),
        failures=[AccessChallengeError("human verification")],
    )
    with pytest.raises(AccessChallengeError):
        challenged.run()
    assert not (challenge_dir / "archive-challenge").exists()


# --- run report --------------------------------------------------------------


def test_run_report_schema_and_resumed_pages(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_pages=0, report_file="report.json")
    (tmp_path / "checkpoint.json").write_text(
        json.dumps(
            {
                "next_url": "https://example.com/properties?page=4",
                "pages_completed": 3,
            }
        ),
        encoding="utf-8",
    )
    scraper = ScriptedScraper(Settings.load(config), pages=[[make_record("1")]])
    scraper.run()

    report = read_report(tmp_path)
    assert set(report) == {
        "schema_version",
        "start_url",
        "pages_completed",
        "records_added",
        "records_total",
        "stop_reason",
        "started_at",
        "finished_at",
        "challenges",
        "resumed_from_pages",
    }
    assert report["schema_version"] == 1
    assert report["start_url"] == "https://example.com/properties?page=1"
    assert report["pages_completed"] == 4
    assert report["records_added"] == 1
    assert report["records_total"] == 1
    assert report["stop_reason"] == "pagination_exhausted"
    assert report["challenges"] == 0
    assert report["resumed_from_pages"] == 3
    started = datetime.fromisoformat(str(report["started_at"]))
    finished = datetime.fromisoformat(str(report["finished_at"]))
    assert started <= finished
    assert started.tzinfo is not None
    assert not (tmp_path / "report.json.tmp").exists()


def test_stop_reason_max_pages(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_pages=1, report_file="report.json")
    scraper = ScriptedScraper(
        Settings.load(config),
        pages=[[make_record("1")], [make_record("2")]],
        next_urls=["https://example.com/properties?page=2"],
    )
    scraper.run()

    assert len(scraper.loaded) == 1
    assert read_report(tmp_path)["stop_reason"] == "max_pages"


def test_stop_reason_empty_page_threshold(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        max_pages=0,
        empty_page_stop_threshold=2,
        report_file="report.json",
    )
    scraper = ScriptedScraper(
        Settings.load(config),
        pages=[[], []],
        next_urls=["https://example.com/properties?page=2"],
    )
    scraper.run()

    assert len(scraper.loaded) == 2
    assert read_report(tmp_path)["stop_reason"] == "empty_page_threshold"


def test_stop_reason_pagination_loop(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_pages=0, report_file="report.json")
    scraper = ScriptedScraper(
        Settings.load(config),
        pages=[[make_record("1")]],
        loop_url="https://example.com/properties?page=1",
    )
    scraper.run()

    assert read_report(tmp_path)["stop_reason"] == "pagination_loop"


def test_stop_reason_stop_requested(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_pages=0, report_file="report.json")
    scraper = ScriptedScraper(Settings.load(config), pages=[[make_record("1")]])
    scraper.stop_requested = True
    scraper.run()

    assert scraper.loaded == []
    assert read_report(tmp_path)["stop_reason"] == "stop_requested"


def test_stop_reason_challenge_and_error_paths(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_pages=0, report_file="report.json")
    challenged = ScriptedScraper(
        Settings.load(config),
        failures=[AccessChallengeError("human verification")],
    )
    with pytest.raises(AccessChallengeError):
        challenged.run()
    report = read_report(tmp_path)
    assert report["stop_reason"] == "challenge"
    assert report["challenges"] == 1

    error_config = make_config(tmp_path, max_pages=0, report_file="report.json")
    errored = ScriptedScraper(
        Settings.load(error_config),
        failures=[BrowserError("browser crashed")],
    )
    with pytest.raises(BrowserError):
        errored.run()
    report = read_report(tmp_path)
    assert report["stop_reason"] == "browser_error"
    assert report["challenges"] == 0


def test_report_write_failure_warns_without_masking_outcome(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "blocker").write_text("not a directory", encoding="utf-8")
    config = make_config(tmp_path, max_pages=1, report_file="blocker/report.json")
    scraper = ScriptedScraper(Settings.load(config), pages=[[make_record("1")]])

    with caplog.at_level(logging.WARNING):
        scraper.run()

    assert "Could not write run report" in caplog.text
    assert len(scraper.loaded) == 1


# --- attach mode -------------------------------------------------------------


class RecordingSession:
    """Captures the arguments the scraper passes to ``sb_cdp.Chrome``."""

    def __init__(self, url: str | None = None, **kwargs: object) -> None:
        self.url = url
        self.kwargs = kwargs


class FakeHttpResponse:
    def __init__(self, payload: bytes = b'{"Browser": "Chrome/152.0"}') -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._payload


def _attach_devtools_payloads(
    monkeypatch: pytest.MonkeyPatch, targets: list[dict[str, str]]
) -> None:
    """Serve /json/version and /json/list payloads for attach preflight."""

    def reachable(url: str, timeout: float | None = None) -> FakeHttpResponse:
        if url.endswith("/json/list"):
            return FakeHttpResponse(json.dumps(targets).encode())
        return FakeHttpResponse()

    monkeypatch.setattr(property_scraper.urllib.request, "urlopen", reachable)


def test_attach_multi_tab_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Several page targets: warn that the newest tab will be driven."""
    _attach_devtools_payloads(
        monkeypatch,
        [
            {"type": "page", "url": "https://example.com/a"},
            {"type": "page", "url": "https://example.com/b"},
            {"type": "service_worker", "url": "https://example.com/sw.js"},
        ],
    )
    scraper = PropertyScraper(Settings.load(make_config(tmp_path, attach_address="127.0.0.1:9222")))

    with caplog.at_level(logging.WARNING):
        scraper._warn_on_multiple_page_targets("127.0.0.1:9222")

    assert "2 open page tabs" in caplog.text
    assert "newest tab" in caplog.text


def test_attach_single_tab_logs_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _attach_devtools_payloads(monkeypatch, [{"type": "page", "url": "https://example.com/a"}])
    scraper = PropertyScraper(Settings.load(make_config(tmp_path, attach_address="127.0.0.1:9222")))

    with caplog.at_level(logging.WARNING):
        scraper._warn_on_multiple_page_targets("127.0.0.1:9222")

    assert "page tabs" not in caplog.text


def test_attach_multi_tab_probe_failure_is_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(url: str, timeout: float | None = None) -> FakeHttpResponse:
        if url.endswith("/json/list"):
            raise OSError("endpoint vanished")
        return FakeHttpResponse()

    monkeypatch.setattr(property_scraper.urllib.request, "urlopen", broken)
    scraper = PropertyScraper(Settings.load(make_config(tmp_path, attach_address="127.0.0.1:9222")))

    scraper._warn_on_multiple_page_targets("127.0.0.1:9222")  # must not raise


def test_attach_mode_connects_without_launch_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed: list[str] = []

    def reachable(url: str, timeout: float | None = None) -> FakeHttpResponse:
        probed.append(url)
        assert timeout == 2
        return FakeHttpResponse()

    monkeypatch.setattr(property_scraper.urllib.request, "urlopen", reachable)
    monkeypatch.setattr(property_scraper.sb_cdp, "Chrome", RecordingSession)
    settings = Settings.load(
        make_config(
            tmp_path,
            attach_address="127.0.0.1:9222",
            user_data_dir="/tmp/should-be-ignored",
        )
    )

    session = PropertyScraper(settings)._build_session("https://example.com/properties?page=1")

    assert probed == ["http://127.0.0.1:9222/json/version"]
    # The attach-mode session navigates during construction, so it gets the
    # first target URL -- never about:blank, which would hijack the tab.
    assert session.url == "https://example.com/properties?page=1"
    # Launch flags are ignored in attach mode; only the endpoint is passed.
    assert session.kwargs == {"host": "127.0.0.1", "port": 9222}


def test_attach_dead_endpoint_fails_fast_without_building_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed: list[tuple[str, float | None]] = []

    def unreachable(url: str, timeout: float | None = None) -> FakeHttpResponse:
        probed.append((url, timeout))
        raise OSError("Connection refused")

    chrome_built: list[bool] = []

    class ExplodingSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            chrome_built.append(True)

    monkeypatch.setattr(property_scraper.urllib.request, "urlopen", unreachable)
    monkeypatch.setattr(property_scraper.sb_cdp, "Chrome", ExplodingSession)
    settings = Settings.load(make_config(tmp_path, attach_address="127.0.0.1:9222"))
    scraper = PropertyScraper(settings)

    with pytest.raises(BrowserError, match="launch_attach_chrome"):
        scraper._build_session("https://example.com/properties?page=1")

    assert chrome_built == []
    assert probed == [("http://127.0.0.1:9222/json/version", 2)]


def test_main_returns_one_when_attach_endpoint_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(url: str, timeout: float | None = None) -> FakeHttpResponse:
        raise OSError("Connection refused")

    monkeypatch.setattr(property_scraper.urllib.request, "urlopen", unreachable)
    monkeypatch.setattr(property_scraper.sb_cdp, "Chrome", RecordingSession)
    config = make_config(tmp_path, attach_address="127.0.0.1:9222")
    monkeypatch.setattr(sys, "argv", ["property-scraper", "--config", str(config)])

    assert property_scraper.main() == 1


def test_main_returns_one_when_idle_attach_endpoint_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to fetch (checkpoint at max_pages) must still preflight the
    attach endpoint: a dead port exits 1 with a browser_error report."""

    def unreachable(url: str, timeout: float | None = None) -> FakeHttpResponse:
        raise OSError("Connection refused")

    monkeypatch.setattr(property_scraper.urllib.request, "urlopen", unreachable)
    monkeypatch.setattr(property_scraper.sb_cdp, "Chrome", RecordingSession)
    config = make_config(
        tmp_path,
        attach_address="127.0.0.1:9222",
        max_pages=1,
        report_file="report.json",
    )
    (tmp_path / "checkpoint.json").write_text(
        json.dumps({"next_url": "https://example.com/properties?page=2", "pages_completed": 1}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["property-scraper", "--config", str(config)])

    assert property_scraper.main() == 1

    assert not (tmp_path / "listings.csv").exists()
    assert read_report(tmp_path)["stop_reason"] == "browser_error"


def test_launch_mode_passes_browser_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(property_scraper.sb_cdp, "Chrome", RecordingSession)
    settings = Settings.load(make_config(tmp_path, user_data_dir="/tmp/chrome-profile"))

    session = PropertyScraper(settings)._build_session("https://example.com/properties?page=1")

    # Launch mode starts a fresh browser at about:blank; the per-page retry
    # loop performs the real navigation.
    assert session.url == "about:blank"
    assert session.kwargs["headless"] is True
    assert session.kwargs["agent"] == settings.user_agent
    assert session.kwargs["user_data_dir"] == "/tmp/chrome-profile"
    assert "--window-size=1440,1200" in session.kwargs["browser_args"]


def test_attached_run_does_not_quit_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        property_scraper.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeHttpResponse(),
    )
    config = make_config(
        tmp_path,
        attach_address="127.0.0.1:9222",
        max_pages=2,
        report_file="report.json",
    )
    (tmp_path / "checkpoint.json").write_text(
        json.dumps({"next_url": "https://example.com/properties?page=2", "pages_completed": 1}),
        encoding="utf-8",
    )
    scraper = ScriptedScraper(
        Settings.load(config),
        pages=[[make_record("1")]],
        next_urls=["https://example.com/properties?page=3"],
    )

    with caplog.at_level(logging.INFO):
        scraper.run()

    assert len(scraper.sessions) == 1
    assert scraper.sessions[0].calls == []
    assert "left open" in caplog.text
    assert read_report(tmp_path)["stop_reason"] == "max_pages"


def test_attached_run_skips_session_when_page_cap_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing left to fetch: do not connect and never navigate the tab."""
    monkeypatch.setattr(
        property_scraper.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeHttpResponse(),
    )
    config = make_config(
        tmp_path,
        attach_address="127.0.0.1:9222",
        max_pages=1,
        report_file="report.json",
    )
    (tmp_path / "checkpoint.json").write_text(
        json.dumps({"next_url": "https://example.com/properties?page=2", "pages_completed": 1}),
        encoding="utf-8",
    )
    scraper = ScriptedScraper(Settings.load(config))

    scraper.run()

    assert scraper.sessions == []
    assert read_report(tmp_path)["stop_reason"] == "max_pages"


def test_launch_session_restarts_relaunch_the_browser(tmp_path: Path) -> None:
    """session_restart_pages quits the old session and builds a new one."""
    config = make_config(
        tmp_path,
        session_restart_pages=1,
        max_pages=3,
        report_file="report.json",
    )
    scraper = ScriptedScraper(
        Settings.load(config),
        pages=[[make_record("1")], [make_record("2")], [make_record("3")]],
        next_urls=[
            "https://example.com/properties?page=2",
            "https://example.com/properties?page=3",
            "https://example.com/properties?page=4",
        ],
    )

    scraper.run()

    # Initial session plus one relaunch per completed page, then the final
    # launch-mode quit in run()'s cleanup.
    assert len(scraper.sessions) == 3
    assert [session.calls for session in scraper.sessions] == [
        ["quit"],
        ["quit"],
        ["quit"],
    ]
    assert len(scraper.loaded) == 3
    assert read_report(tmp_path)["stop_reason"] == "max_pages"


def test_attached_run_ignores_session_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        property_scraper.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeHttpResponse(),
    )
    config = make_config(
        tmp_path,
        attach_address="127.0.0.1:9222",
        session_restart_pages=1,
        max_pages=2,
    )
    (tmp_path / "checkpoint.json").write_text(
        json.dumps({"next_url": "https://example.com/properties?page=2", "pages_completed": 1}),
        encoding="utf-8",
    )
    scraper = ScriptedScraper(Settings.load(config), pages=[[make_record("1")]])

    with caplog.at_level(logging.WARNING):
        scraper.run()

    assert len(scraper.sessions) == 1
    assert "session_restart_pages" in caplog.text


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1",
        "127.0.0.1:notaport",
        "127.0.0.1:0",
        "127.0.0.1:70000",
        ":9222",
        "127.0.0.1:9222:extra",
        " 127.0.0.1:9222",
    ],
)
def test_attach_address_validation_rejects_malformed(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="attach_address"):
        Settings.load(make_config(tmp_path, attach_address=value)).validate()


def test_attach_address_validation_accepts_host_port(tmp_path: Path) -> None:
    for value in ("127.0.0.1:9222", "localhost:1", "192.168.1.10:65535"):
        Settings.load(make_config(tmp_path, attach_address=value)).validate()
