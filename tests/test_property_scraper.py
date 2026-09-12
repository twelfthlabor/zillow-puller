from __future__ import annotations

import json
from pathlib import Path

import pytest
from selenium.common.exceptions import TimeoutException

from property_scraper import PropertyScraper, Settings


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


class FakeElement:
    def __init__(self, text: str = "", attributes: dict[str, str | None] | None = None):
        self._text = text
        self._attributes = attributes or {}

    @property
    def text(self) -> str:
        return self._text

    def get_attribute(self, name: str) -> str | None:
        return self._attributes.get(name)

    def find_element(self, by: str, selector: str) -> "FakeElement":
        return self


class EmptyPageDriver:
    def __init__(self) -> None:
        self.page_source = "<html><body></body></html>"
        self.calls: list[str] = []

    def get(self, url: str) -> None:
        self.calls.append(url)

    def find_elements(self, by: str, selector: str) -> list[object]:
        return []

    def save_screenshot(self, path: str) -> None:
        self.calls.append(f"shot:{path}")

    def quit(self) -> None:
        self.calls.append("quit")


class ChallengeDriver:
    """Returns a matching element for a configured selector, else nothing."""

    def __init__(self, selector_hits: set[str] = frozenset(), page_source: str = "") -> None:
        self.selector_hits = selector_hits
        self.page_source = page_source

    def find_elements(self, by: str, selector: str) -> list[object]:
        return [object()] if selector in self.selector_hits else []


class RetryScraper(PropertyScraper):
    def _build_driver(self) -> EmptyPageDriver:
        return EmptyPageDriver()


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
        scraper._record_key({"listing_id": "", "url": "HTTPS://X.COM/A/"})
        == "url:https://x.com/a"
    )


def test_field_validation_bounds_drop_absurd_values(tmp_path: Path) -> None:
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    price_rule = settings.field_rules["price"]

    assert scraper._extract_field(FakeElement("$2,000,000,000"), price_rule) == ""
    assert scraper._extract_field(FakeElement("$1,234,567"), price_rule) == "1234567"


def test_listing_id_fallback_extracts_from_url(tmp_path: Path) -> None:
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    card = FakeElement(
        attributes={"href": "/homedetails/Example-St-Toronto-ON/464240149_zpid/"}
    )
    record = scraper._extract_card(card, "https://example.com/properties?page=1")
    assert record["listing_id"] == "464240149"


def test_invalid_settings_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pagination.mode"):
        Settings.load(
            make_config(tmp_path, pagination={"mode": "magic"})
        ).validate()
    with pytest.raises(ValueError, match="cannot be negative"):
        Settings.load(
            make_config(tmp_path, request_delay_seconds=-1)
        ).validate()
    with pytest.raises(ValueError, match="not a valid regex"):
        Settings.load(
            make_config(tmp_path, listing_id_pattern="([unclosed")
        ).validate()


def test_transient_failure_retries_then_preserves_checkpoint(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        max_attempts_per_page=2,
        backoff_base_seconds=0.01,
    )
    settings = Settings.load(config)
    scraper = RetryScraper(settings)

    with pytest.raises(TimeoutException):
        scraper.run()

    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["next_url"] == settings.start_url
    assert checkpoint["pages_completed"] == 0
    assert not (tmp_path / "checkpoint.json.tmp").exists()
    error_files = list((tmp_path / "errors").glob("*.json"))
    assert error_files, "expected a diagnostic sidecar after the fatal failure"


def test_challenge_detected_by_dom_selector(tmp_path: Path) -> None:
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    scraper.driver = ChallengeDriver(selector_hits={"#px-captcha"})
    assert scraper._detect_challenge() is True


def test_challenge_detected_by_strong_text_marker(tmp_path: Path) -> None:
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    scraper.driver = ChallengeDriver(
        page_source="<html>Access to this page has been denied</html>"
    )
    assert scraper._detect_challenge() is True


def test_weak_captcha_string_is_not_a_challenge(tmp_path: Path) -> None:
    """The bare word 'captcha' appears in ordinary Zillow markup; a slow
    legitimate page must not be mislabeled as an access challenge."""
    settings = Settings.load(make_config(tmp_path))
    scraper = PropertyScraper(settings)
    scraper.driver = ChallengeDriver(
        page_source="<html>window.__captcha_config__ = {}; recaptcha.js loading</html>"
    )
    assert scraper._detect_challenge() is False
