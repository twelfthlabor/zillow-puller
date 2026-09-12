"""SeleniumBase collector for controlled fixtures or authorized portals.

Active challenge handling is restricted to explicitly allow-listed hosts.
Only use these features where automated access and challenge interaction are
authorized by the site owner. Live hosts are disabled by default; the normal
production path is the RESO API client in :mod:`reso_ingest`.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from selenium.webdriver.common.by import By

from field_utils import (
    apply_validation,
    as_bool,
    backoff_seconds,
    normalize,
    normalize_record_key,
)


@dataclass(frozen=True)
class ChallengeSettings:
    mode: str
    marker_selector: str
    allowed_hosts: tuple[str, ...]
    answer_selector: str | None = None
    submit_selector: str | None = None
    success_selector: str | None = None
    answer_env: str = "PROPHUB_TEST_CHALLENGE_ANSWER"
    manual_timeout_seconds: float = 120.0
    seleniumbase_handler: str = "gui_handle_captcha"
    frame_selector: str = "iframe"
    retry: bool = False
    blind: bool = False
    completion_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class BrowserSettings:
    uc: bool = False
    cdp: bool = False
    stealth_clicks: bool = False
    reconnect_time_seconds: float = 4.0


@dataclass(frozen=True)
class Settings:
    start_url: str
    user_agent: str
    output_csv: Path
    checkpoint_file: Path
    headless: bool
    timeout_seconds: float
    page_delay_seconds: float
    max_pages: int
    card_selector: str
    fields: dict[str, dict[str, str]]
    pagination: dict[str, Any]
    dynamic_content: dict[str, Any]
    challenge: ChallengeSettings
    browser: BrowserSettings
    allow_live_browser: bool
    max_attempts_per_page: int
    backoff_base_seconds: float
    empty_page_stop_threshold: int
    listing_id_pattern: str

    @classmethod
    def load(cls, path: Path) -> "Settings":
        raw = json.loads(path.read_text(encoding="utf-8"))
        base = path.parent
        challenge = raw.get("challenge", {})
        browser = raw.get("browser", {})
        return cls(
            start_url=raw["start_url"],
            user_agent=raw.get(
                "user_agent", "AuthorizedPropertyAudit/1.0"
            ),
            output_csv=(
                base / raw.get("output_csv", "data/seleniumbase-listings.csv")
            ).resolve(),
            checkpoint_file=(
                base / raw.get(
                    "checkpoint_file", "data/seleniumbase-checkpoint.json"
                )
            ).resolve(),
            headless=as_bool(raw.get("headless"), True),
            timeout_seconds=float(raw.get("timeout_seconds", 15)),
            page_delay_seconds=max(0.0, float(raw.get("page_delay_seconds", 1))),
            max_pages=max(1, int(raw.get("max_pages", 20))),
            card_selector=raw["selectors"]["card"],
            fields={
                name: rule
                for name, rule in raw["selectors"].items()
                if name != "card"
            },
            pagination=raw["pagination"],
            dynamic_content=raw.get("dynamic_content", {}),
            challenge=ChallengeSettings(
                mode=challenge.get("mode", "stop"),
                marker_selector=challenge.get(
                    "marker_selector", "[data-test='challenge']"
                ),
                allowed_hosts=tuple(challenge.get("allowed_hosts", [])),
                answer_selector=challenge.get("answer_selector"),
                submit_selector=challenge.get("submit_selector"),
                success_selector=challenge.get("success_selector"),
                answer_env=challenge.get(
                    "answer_env", "PROPHUB_TEST_CHALLENGE_ANSWER"
                ),
                manual_timeout_seconds=float(
                    challenge.get("manual_timeout_seconds", 120)
                ),
                seleniumbase_handler=challenge.get(
                    "seleniumbase_handler", "gui_handle_captcha"
                ),
                frame_selector=challenge.get("frame_selector", "iframe"),
                retry=bool(challenge.get("retry", False)),
                blind=bool(challenge.get("blind", False)),
                completion_timeout_seconds=float(
                    challenge.get("completion_timeout_seconds", 30)
                ),
            ),
            browser=BrowserSettings(
                uc=bool(browser.get("uc", False)),
                cdp=bool(browser.get("cdp", False)),
                stealth_clicks=bool(browser.get("stealth_clicks", False)),
                reconnect_time_seconds=float(
                    browser.get("reconnect_time_seconds", 4)
                ),
            ),
            allow_live_browser=as_bool(raw.get("allow_live_browser"), False),
            max_attempts_per_page=int(raw.get("max_attempts_per_page", 3)),
            backoff_base_seconds=float(raw.get("backoff_base_seconds", 5)),
            empty_page_stop_threshold=int(raw.get("empty_page_stop_threshold", 3)),
            listing_id_pattern=raw.get(
                "listing_id_pattern", r"/(\d+)_zpid/?(?:[?#].*)?$"
            ),
        )

    def validate(self) -> None:
        host = (urlparse(self.start_url).hostname or "").lower()
        fixture_host = (
            host in {"localhost", "127.0.0.1", "::1"}
            or host.endswith((".test", ".example", ".invalid", ".localhost"))
        )
        if not fixture_host and not self.allow_live_browser:
            raise ValueError(
                "Live SeleniumBase collection is disabled by default. Use the "
                "RESO/API ingestion path, or set allow_live_browser=true only "
                "when the site owner has authorized browser automation."
            )
        challenge_modes = {"stop", "manual", "fixture", "seleniumbase"}
        if self.challenge.mode not in challenge_modes:
            raise ValueError(f"Unsupported challenge mode: {self.challenge.mode}")
        if self.browser.cdp and not self.browser.uc:
            raise ValueError("browser.cdp requires browser.uc=true.")
        if self.browser.stealth_clicks and not self.browser.uc:
            raise ValueError("browser.stealth_clicks requires browser.uc=true.")
        if self.browser.reconnect_time_seconds < 0:
            raise ValueError("browser.reconnect_time_seconds cannot be negative.")
        if self.challenge.completion_timeout_seconds <= 0:
            raise ValueError(
                "challenge.completion_timeout_seconds must be greater than zero."
            )
        if self.challenge.mode in {"manual", "fixture", "seleniumbase"}:
            if not self.challenge.allowed_hosts:
                raise ValueError(
                    f"challenge.mode={self.challenge.mode!r} requires "
                    "challenge.allowed_hosts."
                )
        if self.challenge.mode == "manual" and self.headless:
            raise ValueError("challenge.mode='manual' requires headless=false.")
        if self.challenge.mode == "fixture":
            if not self.challenge.answer_selector or not self.challenge.submit_selector:
                raise ValueError(
                    "Fixture mode requires answer_selector and submit_selector."
                )
        seleniumbase_handlers = {
            "gui_handle_captcha",
            "gui_click_captcha",
            "gui_handle_cf",
            "gui_click_cf",
            "gui_handle_rc",
            "gui_click_rc",
        }
        if self.challenge.mode == "seleniumbase":
            if not self.browser.uc:
                raise ValueError(
                    "challenge.mode='seleniumbase' requires browser.uc=true."
                )
            if self.headless:
                raise ValueError(
                    "SeleniumBase GUI CAPTCHA handlers require headless=false."
                )
            if self.challenge.seleniumbase_handler not in seleniumbase_handlers:
                raise ValueError(
                    "Unsupported SeleniumBase CAPTCHA handler: "
                    f"{self.challenge.seleniumbase_handler}"
                )
        if self.browser.stealth_clicks and self.headless:
            raise ValueError(
                "browser.stealth_clicks uses GUI clicks and requires headless=false."
            )
        if self.max_attempts_per_page < 1:
            raise ValueError("max_attempts_per_page must be at least 1.")
        if self.backoff_base_seconds < 0:
            raise ValueError("backoff_base_seconds cannot be negative.")
        if self.empty_page_stop_threshold < 1:
            raise ValueError("empty_page_stop_threshold must be at least 1.")
        try:
            re.compile(self.listing_id_pattern)
        except re.error as exc:
            raise ValueError(
                f"listing_id_pattern is not a valid regex: {exc}"
            ) from exc


class ChallengeDetected(RuntimeError):
    """A challenge was encountered and was not completed by an approved flow."""


class SeleniumBasePropertyScraper:
    def __init__(self, settings: Settings) -> None:
        settings.validate()
        self.settings = settings
        self.driver: Any = None
        self.seen = self._load_existing_keys()
        self.stop_requested = False
        self.checkpoint = self._load_checkpoint()

    def request_stop(self, *_: object) -> None:
        logging.warning("Stop requested; finishing the current operation.")
        self.stop_requested = True

    def _load_checkpoint(self) -> dict[str, Any]:
        path = self.settings.checkpoint_file
        if not path.exists():
            return {"next_url": None, "pages_completed": 0}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logging.warning("Ignoring unreadable checkpoint: %s", path)
            return {"next_url": None, "pages_completed": 0}

    def _save_checkpoint(self, next_url: str | None, pages_completed: int) -> None:
        path = self.settings.checkpoint_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "next_url": next_url,
                    "pages_completed": pages_completed,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _capture_error(self, label: str, page_url: str) -> None:
        if not self.driver:
            return
        error_directory = self.settings.output_csv.parent / "errors"
        error_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = error_directory / f"{stamp}-{label}"
        try:
            self.driver.save_screenshot(str(base.with_suffix(".png")))
            base.with_suffix(".html").write_text(
                self._page_source(), encoding="utf-8"
            )
            base.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "label": label,
                        "page_url": page_url,
                        "captured_at": datetime.now(UTC).isoformat(),
                        "checkpoint": self.checkpoint,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logging.warning("Could not save diagnostics: %s", exc)

    def _load_existing_keys(self) -> set[str]:
        path = self.settings.output_csv
        if not path.exists():
            return set()
        with path.open(newline="", encoding="utf-8") as handle:
            return {
                self._key(row)
                for row in csv.DictReader(handle)
                if self._key(row)
            }

    def _open_browser(self) -> None:
        # Import lazily so config validation and unit tests do not launch a browser.
        from seleniumbase import Driver

        self.driver = Driver(
            browser="chrome",
            headless=self.settings.headless,
            agent=self.settings.user_agent,
            uc=self.settings.browser.uc,
            page_load_strategy="eager",
        )

    @property
    def _page(self) -> Any:
        if self.settings.browser.cdp and getattr(self.driver, "cdp", None):
            return self.driver.cdp
        return self.driver

    def _current_url(self) -> str:
        if self.settings.browser.cdp:
            return self._page.get_current_url()
        return self.driver.current_url

    def _page_source(self) -> str:
        if self.settings.browser.cdp:
            return self._page.get_page_source()
        return self.driver.page_source

    def _find_elements(self, selector: str) -> list[Any]:
        if self.settings.browser.cdp:
            return list(self._page.find_elements(selector))
        return list(self.driver.find_elements(By.CSS_SELECTOR, selector))

    def _element_visible(self, element: Any) -> bool:
        if self.settings.browser.cdp:
            position = element.get_position()
            return bool(position.width or position.height)
        return bool(element.is_displayed())

    def _wait_for_present(self, selector: str, timeout: float) -> None:
        if self.settings.browser.cdp:
            self._page.wait_for_element_visible(selector, timeout=timeout)
        else:
            self.driver.wait_for_element_present(selector, timeout=timeout)

    def _wait_for_visible(self, selector: str, timeout: float) -> None:
        self._page.wait_for_element_visible(selector, timeout=timeout)

    def _wait_for_absent(self, selector: str, timeout: float) -> None:
        self._page.wait_for_element_absent(selector, timeout=timeout)

    def _open(self, url: str) -> None:
        browser = self.settings.browser
        if browser.cdp:
            if getattr(self.driver, "cdp", None):
                self.driver.cdp.open(url)
            else:
                self.driver.activate_cdp_mode(url)
            return
        if browser.uc:
            self.driver.uc_open_with_reconnect(
                url, reconnect_time=browser.reconnect_time_seconds
            )
            return
        self.driver.open(url)

    def _click(self, selector: str) -> None:
        browser = self.settings.browser
        if browser.cdp:
            if browser.stealth_clicks:
                self._page.gui_click_element(selector)
            else:
                self._page.click(selector)
            return
        if browser.uc:
            self.driver.uc_click(
                selector,
                reconnect_time=browser.reconnect_time_seconds,
            )
            return
        self.driver.click(selector)

    def _challenge_present(self) -> bool:
        selector_match = bool(
            self._find_elements(self.settings.challenge.marker_selector)
        )
        page_text = self._page_source().lower()
        text_match = any(
            marker in page_text
            for marker in (
                "press &amp; hold",
                "press & hold",
                "confirm you are a human",
                "verify you are human",
                "captcha",
                "access to this page has been denied",
            )
        )
        return selector_match or text_match

    def _handle_test_challenge(self) -> None:
        if not self._challenge_present():
            return

        challenge = self.settings.challenge
        host = (urlparse(self._current_url()).hostname or "").lower()
        allowed_hosts = {item.lower() for item in challenge.allowed_hosts}
        if host not in allowed_hosts:
            raise ChallengeDetected(
                f"Challenge found on non-allow-listed host {host!r}; "
                "no response was attempted."
            )

        if challenge.mode == "stop":
            raise ChallengeDetected("Challenge found; configured mode is 'stop'.")

        if challenge.mode == "manual":
            if self.settings.headless:
                raise ChallengeDetected("Manual challenge mode requires headless=false.")
            logging.warning("Waiting for a tester to complete the lab challenge.")
            self._wait_for_absent(
                challenge.marker_selector,
                timeout=challenge.manual_timeout_seconds,
            )
            return

        if challenge.mode == "seleniumbase":
            handler_name = f"uc_{challenge.seleniumbase_handler}"
            handler = getattr(self.driver, handler_name)
            logging.warning(
                "Running SeleniumBase %s on allow-listed host %s.",
                challenge.seleniumbase_handler,
                host,
            )
            kwargs: dict[str, Any] = {"frame": challenge.frame_selector}
            if challenge.seleniumbase_handler.startswith("gui_click_"):
                kwargs.update(retry=challenge.retry, blind=challenge.blind)
            handler(**kwargs)
            if challenge.success_selector:
                self._wait_for_visible(
                    challenge.success_selector,
                    timeout=challenge.completion_timeout_seconds,
                )
            else:
                self._wait_for_absent(
                    challenge.marker_selector,
                    timeout=challenge.completion_timeout_seconds,
                )
            return

        if challenge.mode != "fixture":
            raise ValueError(f"Unsupported challenge mode: {challenge.mode}")
        if not challenge.answer_selector or not challenge.submit_selector:
            raise ValueError(
                "Fixture mode requires answer_selector and submit_selector."
            )

        answer = os.environ.get(challenge.answer_env)
        if not answer:
            raise ChallengeDetected(
                f"Set {challenge.answer_env} to the answer supplied by the test fixture."
            )

        self._page.type(challenge.answer_selector, answer)
        self._click(challenge.submit_selector)
        if challenge.success_selector:
            self._wait_for_visible(
                challenge.success_selector,
                timeout=self.settings.timeout_seconds,
            )
        else:
            self._wait_for_absent(
                challenge.marker_selector,
                timeout=self.settings.timeout_seconds,
            )

    def _wait_for_cards(self) -> None:
        try:
            self._wait_for_present(
                self.settings.card_selector, self.settings.timeout_seconds
            )
        except Exception:
            self._handle_test_challenge()
            raise

    def _load_dynamic_content(self) -> None:
        config = self.settings.dynamic_content
        mode = config.get("mode", "none")
        if mode == "none":
            return
        if mode not in {"scroll", "load_more"}:
            raise ValueError(f"Unsupported dynamic_content mode: {mode}")

        stable_rounds = 0
        previous_count = -1
        for _ in range(max(1, int(config.get("max_rounds", 30)))):
            self._handle_test_challenge()
            count = len(
                self._find_elements(self.settings.card_selector)
            )
            stable_rounds = stable_rounds + 1 if count == previous_count else 0
            if stable_rounds >= max(1, int(config.get("stable_rounds", 2))):
                return
            previous_count = count

            if mode == "scroll":
                self._page.execute_script(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
            else:
                selector = config["load_more_selector"]
                buttons = self._find_elements(selector)
                if not buttons or not self._element_visible(buttons[0]):
                    return
                self._click(selector)
            time.sleep(max(0.0, float(config.get("pause_seconds", 0.5))))

    def _extract(self, card: Any, page_url: str) -> dict[str, str]:
        record: dict[str, str] = {}
        for name, rule in self.settings.fields.items():
            try:
                if self.settings.browser.cdp:
                    element = card.query_selector(rule["selector"])
                else:
                    element = card.find_element(By.CSS_SELECTOR, rule["selector"])
                attribute = rule.get("attribute")
                value = element.get_attribute(attribute) if attribute else element.text
                transform = rule.get("transform", "text")
                normalized = normalize(value or "", transform)
                record[name] = apply_validation(
                    normalized, transform, rule.get("validation")
                )
            except Exception:
                record[name] = ""
        if record.get("url"):
            record["url"] = urljoin(page_url, record["url"])
        if not record.get("listing_id") and record["url"]:
            pattern = self.settings.listing_id_pattern
            if pattern:
                match = re.search(pattern, record["url"])
                if match:
                    record["listing_id"] = match.group(1)
        record["source_page"] = page_url
        record["scraped_at"] = datetime.now(UTC).isoformat()
        return record

    @staticmethod
    def _key(record: dict[str, str]) -> str:
        return normalize_record_key(
            str(record.get("listing_id") or ""),
            str(record.get("url") or ""),
        )

    def _write(self, records: list[dict[str, str]]) -> int:
        fields = [*self.settings.fields]
        if "listing_id" not in fields:
            fields = ["listing_id", *fields]
        fields = [*fields, "source_page", "scraped_at"]
        self.settings.output_csv.parent.mkdir(parents=True, exist_ok=True)
        new_file = (
            not self.settings.output_csv.exists()
            or self.settings.output_csv.stat().st_size == 0
        )
        added = 0
        with self.settings.output_csv.open(
            "a", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(
                handle, fieldnames=fields, extrasaction="ignore"
            )
            if new_file:
                writer.writeheader()
            for record in records:
                key = self._key(record)
                if key and key not in self.seen:
                    writer.writerow(record)
                    self.seen.add(key)
                    added += 1
        return added

    def _next_url(self, current_url: str, page_number: int) -> str | None:
        pagination = self.settings.pagination
        mode = pagination["mode"]
        if mode == "url_template":
            return pagination["url_template"].format(page=page_number + 1)
        if mode != "next_button":
            raise ValueError(f"Unsupported pagination mode: {mode}")

        buttons = self._find_elements(pagination["next_selector"])
        if not buttons:
            return None
        button = buttons[0]
        disabled = button.get_attribute(
            pagination.get("disabled_attribute", "aria-disabled")
        )
        if disabled in {"true", "disabled"}:
            return None
        href = button.get_attribute("href")
        if href:
            return urljoin(current_url, href)

        # A click-only control is supported by clicking and returning the new URL.
        old_url = self._current_url()
        self._click(pagination["next_selector"])
        if not self.settings.browser.cdp:
            self.driver.wait_for_ready_state_complete(
                timeout=self.settings.timeout_seconds
            )
        if self._current_url() == old_url:
            logging.warning("Next control did not change the URL; stopping.")
            return None
        return self._current_url()

    def _open_with_retry(self, url: str) -> None:
        """Navigate once, retrying transient driver failures with backoff."""
        attempts = self.settings.max_attempts_per_page
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            if self.stop_requested:
                raise ChallengeDetected("Stop requested during navigation.")
            try:
                self._open(url)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < attempts:
                    delay = backoff_seconds(attempt, self.settings.backoff_base_seconds)
                    logging.warning(
                        "Transient failure opening %s (attempt %d/%d); "
                        "retrying in %.1fs: %s",
                        url,
                        attempt,
                        attempts,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def run(self) -> None:
        self._open_browser()
        next_url = self.checkpoint.get("next_url") or self.settings.start_url
        pages_completed = int(self.checkpoint.get("pages_completed", 0))
        visited: set[str] = set()
        consecutive_empty_pages = 0
        try:
            for page_number in range(pages_completed + 1, self.settings.max_pages + 1):
                if self.stop_requested:
                    logging.info("Stopped cleanly; resume by running the same command.")
                    self._save_checkpoint(next_url, pages_completed)
                    break
                if not next_url or next_url in visited:
                    break
                visited.add(next_url)
                logging.info("Opening page %d: %s", page_number, next_url)
                try:
                    self._open_with_retry(next_url)
                    self._handle_test_challenge()
                    self._wait_for_cards()
                    self._load_dynamic_content()
                    cards = self._find_elements(self.settings.card_selector)
                    records = [self._extract(card, next_url) for card in cards]
                    added = self._write(records)
                    following_url = self._next_url(next_url, page_number)
                    self._save_checkpoint(following_url, page_number)
                    if not records:
                        consecutive_empty_pages += 1
                        logging.warning(
                            "Page rendered no records (%d consecutive empty).",
                            consecutive_empty_pages,
                        )
                        if (
                            consecutive_empty_pages
                            >= self.settings.empty_page_stop_threshold
                        ):
                            logging.error(
                                "Stopping after %d consecutive empty pages — "
                                "selector drift or a block page. Resume is "
                                "checkpointed.",
                                consecutive_empty_pages,
                            )
                            break
                    else:
                        consecutive_empty_pages = 0
                    logging.info("Wrote %d new records.", added)
                    next_url = following_url
                    time.sleep(self.settings.page_delay_seconds)
                except ChallengeDetected:
                    self._capture_error(f"page-{page_number}", next_url)
                    raise
                except Exception as exc:
                    logging.error("Page failed: %s (%s)", next_url, exc)
                    self._capture_error(f"page-{page_number}", next_url)
                    self._save_checkpoint(next_url, page_number)
                    raise
        finally:
            if self.driver:
                self.driver.quit()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exercise an authorized property portal with SeleniumBase."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        settings = Settings.load(args.config.resolve())
        settings.validate()
        scraper = SeleniumBasePropertyScraper(settings)
        signal.signal(signal.SIGINT, scraper.request_stop)
        signal.signal(signal.SIGTERM, scraper.request_stop)
        scraper.run()
        return 0
    except Exception as exc:
        logging.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
