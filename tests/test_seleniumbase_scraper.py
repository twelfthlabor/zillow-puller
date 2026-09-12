from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from seleniumbase_scraper import (
    ChallengeDetected,
    SeleniumBasePropertyScraper,
    Settings,
)


def make_config(tmp_path: Path, **overrides: object) -> Path:
    raw: dict[str, object] = {
        "start_url": "https://authorized.example/properties",
        "output_csv": "listings.csv",
        "headless": False,
        "selectors": {
            "card": ".card",
            "url": {"selector": "a", "attribute": "href"},
        },
        "pagination": {
            "mode": "next_button",
            "next_selector": "a.next",
        },
        "browser": {
            "uc": True,
            "cdp": False,
            "stealth_clicks": False,
            "reconnect_time_seconds": 2.5,
        },
        "challenge": {
            "mode": "stop",
            "marker_selector": ".captcha",
            "allowed_hosts": ["authorized.example"],
        },
    }
    raw.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


class FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.current_url = "https://authorized.example/properties"
        self.page_source = "<html></html>"
        self.cdp = None

    def uc_open_with_reconnect(self, url: str, reconnect_time: float) -> None:
        self.calls.append(("uc_open_with_reconnect", url, reconnect_time))

    def uc_click(self, selector: str, reconnect_time: float) -> None:
        self.calls.append(("uc_click", selector, reconnect_time))

    def find_elements(self, by: str, selector: str) -> list[object]:
        return [object()] if selector == ".captcha" else []

    def uc_gui_handle_captcha(self, frame: str) -> None:
        self.calls.append(("uc_gui_handle_captcha", frame))

    def uc_gui_click_cf(
        self, frame: str, retry: bool, blind: bool
    ) -> None:
        self.calls.append(("uc_gui_click_cf", frame, retry, blind))

    def wait_for_element_absent(self, selector: str, timeout: float) -> None:
        self.calls.append(("wait_for_element_absent", selector, timeout))


def test_uc_navigation_and_click_use_configured_reconnect_time(
    tmp_path: Path,
) -> None:
    settings = Settings.load(make_config(tmp_path))
    settings.validate()
    scraper = SeleniumBasePropertyScraper(settings)
    scraper.driver = FakeDriver()

    scraper._open(settings.start_url)
    scraper._click("button.more")

    assert scraper.driver.calls == [
        ("uc_open_with_reconnect", settings.start_url, 2.5),
        ("uc_click", "button.more", 2.5),
    ]


def test_cdp_mode_activates_once_then_reuses_cdp(tmp_path: Path) -> None:
    settings = Settings.load(
        make_config(
            tmp_path,
            browser={
                "uc": True,
                "cdp": True,
                "stealth_clicks": True,
                "reconnect_time_seconds": 4,
            },
        )
    )
    settings.validate()
    scraper = SeleniumBasePropertyScraper(settings)
    calls: list[tuple[str, str]] = []
    cdp = SimpleNamespace(
        open=lambda url: calls.append(("cdp.open", url)),
        gui_click_element=lambda selector: calls.append(("gui_click", selector)),
    )
    driver = SimpleNamespace(
        cdp=None,
        activate_cdp_mode=lambda url: (
            calls.append(("activate_cdp_mode", url)),
            setattr(driver, "cdp", cdp),
        ),
    )
    scraper.driver = driver

    scraper._open("https://authorized.example/one")
    scraper._open("https://authorized.example/two")
    scraper._click("button.more")

    assert calls == [
        ("activate_cdp_mode", "https://authorized.example/one"),
        ("cdp.open", "https://authorized.example/two"),
        ("gui_click", "button.more"),
    ]


def test_seleniumbase_captcha_handler_is_dispatched_on_allowed_host(
    tmp_path: Path,
) -> None:
    settings = Settings.load(
        make_config(
            tmp_path,
            challenge={
                "mode": "seleniumbase",
                "marker_selector": ".captcha",
                "allowed_hosts": ["authorized.example"],
                "seleniumbase_handler": "gui_handle_captcha",
                "frame_selector": "iframe.challenge",
                "completion_timeout_seconds": 12,
            },
        )
    )
    settings.validate()
    scraper = SeleniumBasePropertyScraper(settings)
    scraper.driver = FakeDriver()

    scraper._handle_test_challenge()

    assert scraper.driver.calls == [
        ("uc_gui_handle_captcha", "iframe.challenge"),
        ("wait_for_element_absent", ".captcha", 12.0),
    ]


def test_click_captcha_handler_receives_retry_and_blind_options(
    tmp_path: Path,
) -> None:
    settings = Settings.load(
        make_config(
            tmp_path,
            challenge={
                "mode": "seleniumbase",
                "marker_selector": ".captcha",
                "allowed_hosts": ["authorized.example"],
                "seleniumbase_handler": "gui_click_cf",
                "frame_selector": "iframe.turnstile",
                "retry": True,
                "blind": True,
                "completion_timeout_seconds": 8,
            },
        )
    )
    scraper = SeleniumBasePropertyScraper(settings)
    scraper.driver = FakeDriver()

    scraper._handle_test_challenge()

    assert scraper.driver.calls == [
        ("uc_gui_click_cf", "iframe.turnstile", True, True),
        ("wait_for_element_absent", ".captcha", 8.0),
    ]


def test_active_challenge_handler_refuses_unlisted_host(tmp_path: Path) -> None:
    settings = Settings.load(
        make_config(
            tmp_path,
            challenge={
                "mode": "seleniumbase",
                "marker_selector": ".captcha",
                "allowed_hosts": ["different.example"],
            },
        )
    )
    settings.validate()
    scraper = SeleniumBasePropertyScraper(settings)
    scraper.driver = FakeDriver()

    with pytest.raises(ChallengeDetected, match="non-allow-listed host"):
        scraper._handle_test_challenge()


@pytest.mark.parametrize(
    ("browser", "headless", "message"),
    [
        (
            {"uc": False, "cdp": True},
            False,
            "browser.cdp requires browser.uc=true",
        ),
        (
            {"uc": True, "cdp": False, "stealth_clicks": True},
            True,
            "stealth_clicks uses GUI clicks",
        ),
    ],
)
def test_invalid_browser_combinations_are_rejected(
    tmp_path: Path,
    browser: dict[str, object],
    headless: bool,
    message: str,
) -> None:
    settings = Settings.load(
        make_config(tmp_path, browser=browser, headless=headless)
    )
    with pytest.raises(ValueError, match=message):
        settings.validate()


def test_live_browser_collection_requires_explicit_authorization_flag(
    tmp_path: Path,
) -> None:
    settings = Settings.load(
        make_config(tmp_path, start_url="https://listings.example.org/properties")
    )
    with pytest.raises(ValueError, match="disabled by default"):
        settings.validate()

    allowed = Settings.load(
        make_config(
            tmp_path,
            start_url="https://listings.example.org/properties",
            allow_live_browser=True,
        )
    )
    allowed.validate()
