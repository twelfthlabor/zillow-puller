"""Site-neutral Selenium collector for authorized property-listing research."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from selenium import webdriver
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from field_utils import (
    KNOWN_TRANSFORMS,
    apply_validation,
    as_bool,
    backoff_seconds,
    normalize,
    normalize_record_key,
)


CSV_FIELDS = [
    "listing_id",
    "address",
    "price",
    "beds",
    "baths",
    "sqft",
    "agent",
    "url",
    "source_page",
    "scraped_at",
]


@dataclass(frozen=True)
class FieldRule:
    selector: str
    attribute: str | None = None
    transform: str = "text"
    validation: dict[str, Any] | None = None


@dataclass(frozen=True)
class Settings:
    start_url: str
    user_agent: str
    output_csv: Path
    checkpoint_file: Path
    error_directory: Path
    page_wait_seconds: float
    request_delay_seconds: float
    request_jitter_seconds: float
    max_pages: int
    headless: bool
    card_selector: str
    field_rules: dict[str, FieldRule]
    pagination: dict[str, Any]
    scrolling: dict[str, Any]
    challenge: dict[str, Any]
    max_attempts_per_page: int
    backoff_base_seconds: float
    empty_page_stop_threshold: int
    session_restart_pages: int
    listing_id_pattern: str

    @classmethod
    def load(cls, path: Path) -> "Settings":
        raw = json.loads(path.read_text(encoding="utf-8"))
        base = path.parent
        selectors = raw["selectors"]
        fields = {
            name: FieldRule(**rule)
            for name, rule in selectors.items()
            if name != "card"
        }
        return cls(
            start_url=raw["start_url"],
            user_agent=raw["user_agent"],
            output_csv=(base / raw["output_csv"]).resolve(),
            checkpoint_file=(base / raw["checkpoint_file"]).resolve(),
            error_directory=(base / raw["error_directory"]).resolve(),
            page_wait_seconds=float(raw.get("page_wait_seconds", 15)),
            request_delay_seconds=float(raw.get("request_delay_seconds", 3)),
            request_jitter_seconds=float(raw.get("request_jitter_seconds", 0)),
            max_pages=int(raw.get("max_pages", 100)),
            headless=as_bool(raw.get("headless"), True),
            card_selector=selectors["card"],
            field_rules=fields,
            pagination=raw["pagination"],
            scrolling=raw.get("scrolling", {}),
            challenge=raw.get("challenge", {}),
            max_attempts_per_page=int(raw.get("max_attempts_per_page", 3)),
            backoff_base_seconds=float(raw.get("backoff_base_seconds", 5)),
            empty_page_stop_threshold=int(raw.get("empty_page_stop_threshold", 3)),
            session_restart_pages=int(raw.get("session_restart_pages", 0)),
            listing_id_pattern=raw.get(
                "listing_id_pattern", r"/(\d+)_zpid/?(?:[?#].*)?$"
            ),
        )

    def validate(self) -> None:
        errors: list[str] = []
        if not self.start_url.lower().startswith(("http://", "https://")):
            errors.append("start_url must be an http(s) URL")
        if not self.card_selector:
            errors.append("selectors.card is required")
        if self.pagination.get("mode") not in {"next_button", "url_template"}:
            errors.append("pagination.mode must be 'next_button' or 'url_template'")
        if self.request_delay_seconds < 0 or self.request_jitter_seconds < 0:
            errors.append("request delays cannot be negative")
        if self.page_wait_seconds <= 0:
            errors.append("page_wait_seconds must be positive")
        if self.max_pages < 1:
            errors.append("max_pages must be at least 1")
        if self.max_attempts_per_page < 1:
            errors.append("max_attempts_per_page must be at least 1")
        if self.backoff_base_seconds < 0:
            errors.append("backoff_base_seconds cannot be negative")
        if self.empty_page_stop_threshold < 1:
            errors.append("empty_page_stop_threshold must be at least 1")
        if self.session_restart_pages < 0:
            errors.append("session_restart_pages cannot be negative")
        try:
            re.compile(self.listing_id_pattern)
        except re.error as exc:
            errors.append(f"listing_id_pattern is not a valid regex: {exc}")
        for name, rule in self.field_rules.items():
            if rule.transform not in KNOWN_TRANSFORMS:
                errors.append(f"field {name!r} uses unknown transform {rule.transform!r}")
        if errors:
            raise ValueError("; ".join(errors))


class StopRequested(Exception):
    """Raised after a graceful-stop signal."""


class AccessChallengeError(RuntimeError):
    """Raised when a site explicitly requires human verification."""


class PropertyScraper:
    def __init__(self, settings: Settings) -> None:
        settings.validate()
        self.settings = settings
        self.driver: webdriver.Chrome | None = None
        self.stop_requested = False
        self.seen_keys = self._load_existing_keys()
        self.checkpoint = self._load_checkpoint()

    def request_stop(self, *_: object) -> None:
        logging.warning("Stop requested; finishing the current operation.")
        self.stop_requested = True

    def _load_existing_keys(self) -> set[str]:
        path = self.settings.output_csv
        if not path.exists():
            return set()
        with path.open(newline="", encoding="utf-8") as handle:
            return {
                self._record_key(row)
                for row in csv.DictReader(handle)
                if self._record_key(row)
            }

    def _load_checkpoint(self) -> dict[str, Any]:
        path = self.settings.checkpoint_file
        if not path.exists():
            return {"next_url": self.settings.start_url, "pages_completed": 0}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logging.warning("Ignoring unreadable checkpoint: %s", path)
            return {"next_url": self.settings.start_url, "pages_completed": 0}

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

    def _build_driver(self) -> webdriver.Chrome:
        options = webdriver.ChromeOptions()
        if self.settings.headless:
            options.add_argument("--headless=new")
        options.add_argument(f"--user-agent={self.settings.user_agent}")
        options.add_argument("--window-size=1440,1200")
        options.add_argument("--disable-notifications")
        options.page_load_strategy = "eager"
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(self.settings.page_wait_seconds * 2)
        return driver

    def _courtesy_delay(self) -> None:
        delay = self.settings.request_delay_seconds
        jitter = self.settings.request_jitter_seconds
        time.sleep(max(0.0, delay + random.uniform(-jitter, jitter)))

    def _extract_field(self, card: WebElement, rule: FieldRule) -> str:
        try:
            child = card.find_element(By.CSS_SELECTOR, rule.selector)
            raw = child.get_attribute(rule.attribute) if rule.attribute else child.text
            value = normalize(raw or "", rule.transform)
            return apply_validation(value, rule.transform, rule.validation)
        except Exception as exc:
            # An absent optional child should not discard the entire listing.
            logging.debug("Field unavailable (%s): %s", rule.selector, exc)
            return ""

    def _extract_card(self, card: WebElement, page_url: str) -> dict[str, str]:
        record = {
            name: self._extract_field(card, rule)
            for name, rule in self.settings.field_rules.items()
        }
        record["url"] = urljoin(page_url, record.get("url", ""))
        if not record.get("listing_id") and record["url"]:
            pattern = self.settings.listing_id_pattern
            if pattern:
                match = re.search(pattern, record["url"])
                if match:
                    record["listing_id"] = match.group(1)
        record["source_page"] = page_url
        record["scraped_at"] = datetime.now(UTC).isoformat()
        return {field: record.get(field, "") for field in CSV_FIELDS}

    @staticmethod
    def _record_key(record: dict[str, str]) -> str:
        return normalize_record_key(
            str(record.get("listing_id") or ""),
            str(record.get("url") or ""),
        )

    def _append_records(self, records: list[dict[str, str]]) -> int:
        path = self.settings.output_csv
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        added = 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            for record in records:
                key = self._record_key(record)
                if not key:
                    logging.warning("Skipping record without listing_id or URL.")
                    continue
                if key in self.seen_keys:
                    continue
                writer.writerow(record)
                handle.flush()
                self.seen_keys.add(key)
                added += 1
        return added

    def _capture_error(self, label: str, page_url: str) -> None:
        if not self.driver:
            return
        self.settings.error_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = self.settings.error_directory / f"{stamp}-{label}"
        try:
            self.driver.save_screenshot(str(base.with_suffix(".png")))
            base.with_suffix(".html").write_text(
                self.driver.page_source, encoding="utf-8"
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
        except (OSError, WebDriverException) as exc:
            logging.warning("Could not save diagnostics: %s", exc)

    def _next_url(self, current_url: str, page_number: int) -> str | None:
        assert self.driver is not None
        pagination = self.settings.pagination
        mode = pagination["mode"]

        if mode == "url_template":
            template = pagination["url_template"]
            return template.format(page=page_number + 1)

        if mode != "next_button":
            raise ValueError(f"Unsupported pagination mode: {mode}")

        try:
            button = self.driver.find_element(
                By.CSS_SELECTOR, pagination["next_selector"]
            )
            disabled_attribute = pagination.get("disabled_attribute", "aria-disabled")
            if button.get_attribute(disabled_attribute) in {"true", "disabled"}:
                return None
            href = button.get_attribute("href")
            if href:
                return urljoin(current_url, href)

            fallback = pagination.get("url_template_fallback")
            if fallback:
                return fallback.format(page=page_number + 1)

            logging.warning("The next control has neither an href nor a URL fallback.")
            return None
        except Exception as exc:
            logging.info("No usable next-page control: %s", exc)
            return None

    def _collect_page_records(self, page_url: str) -> list[dict[str, str]]:
        """Collect cards while progressively scrolling a lazy-rendered result pane."""
        assert self.driver is not None
        try:
            cards = WebDriverWait(self.driver, self.settings.page_wait_seconds).until(
                EC.presence_of_all_elements_located(
                    (By.CSS_SELECTOR, self.settings.card_selector)
                )
            )
        except TimeoutException:
            if self._detect_challenge():
                raise AccessChallengeError(
                    "The site requested human verification; collection stopped."
                )
            raise
        scrolling = self.settings.scrolling
        if not scrolling.get("enabled", False):
            return [self._extract_card(card, page_url) for card in cards]

        scroll_target = self.driver.execute_script(
            """
            const card = arguments[0];
            for (let node = card.parentElement; node; node = node.parentElement) {
              const style = window.getComputedStyle(node);
              const canScroll = /(auto|scroll)/.test(style.overflowY);
              if (canScroll && node.scrollHeight > node.clientHeight + 20) {
                return node;
              }
            }
            return document.scrollingElement;
            """,
            cards[0],
        )
        if scroll_target is None:
            logging.warning("No scroll container found; using currently rendered cards.")
            return [self._extract_card(card, page_url) for card in cards]

        records_by_key: dict[str, dict[str, str]] = {}
        max_rounds = int(scrolling.get("max_rounds", 80))
        stable_rounds_required = int(scrolling.get("stable_rounds", 4))
        pause_seconds = float(scrolling.get("pause_seconds", 0.8))
        stable_rounds = 0
        previous_count = 0

        for round_number in range(1, max_rounds + 1):
            if self.stop_requested:
                raise StopRequested

            visible_cards = self.driver.find_elements(
                By.CSS_SELECTOR, self.settings.card_selector
            )
            for card in visible_cards:
                try:
                    record = self._extract_card(card, page_url)
                    key = self._record_key(record)
                    if key:
                        previous = records_by_key.get(key, {})
                        records_by_key[key] = {
                            field: record.get(field) or previous.get(field, "")
                            for field in CSV_FIELDS
                        }
                except StaleElementReferenceException:
                    logging.debug("A card rerendered during lazy-scroll extraction.")

            current_count = len(records_by_key)
            before = self.driver.execute_script(
                "return [arguments[0].scrollTop, arguments[0].scrollHeight, "
                "arguments[0].clientHeight];",
                scroll_target,
            )
            self.driver.execute_script(
                "arguments[0].scrollTop += Math.max("
                "arguments[0].clientHeight * 0.8, 500);",
                scroll_target,
            )
            time.sleep(pause_seconds)
            after = self.driver.execute_script(
                "return [arguments[0].scrollTop, arguments[0].scrollHeight, "
                "arguments[0].clientHeight];",
                scroll_target,
            )

            at_bottom = after[0] + after[2] >= after[1] - 5
            did_not_move = after[0] == before[0] and after[1] == before[1]
            if current_count == previous_count and (at_bottom or did_not_move):
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous_count = current_count

            if round_number % 10 == 0:
                logging.info(
                    "Lazy scroll: round %d, %d unique cards.", round_number, current_count
                )
            if stable_rounds >= stable_rounds_required:
                break

        # Capture any cards loaded by the final scroll before moving to pagination.
        for card in self.driver.find_elements(By.CSS_SELECTOR, self.settings.card_selector):
            try:
                record = self._extract_card(card, page_url)
                key = self._record_key(record)
                if key:
                    previous = records_by_key.get(key, {})
                    records_by_key[key] = {
                        field: record.get(field) or previous.get(field, "")
                        for field in CSV_FIELDS
                    }
            except StaleElementReferenceException:
                continue

        logging.info("Lazy scrolling collected %d unique cards.", len(records_by_key))
        return list(records_by_key.values())

    def _detect_challenge(self) -> bool:
        """Yes/no: is the page a real human-verification interstitial?

        DOM selectors come first — a challenge container (``#px-captcha``,
        a captcha iframe, reCAPTCHA widgets) is unambiguous. Text markers are
        deliberately strong strings only; the bare word ``captcha`` appears in
        ordinary Zillow markup and would mislabel a slow legitimate page.
        """
        assert self.driver is not None
        config = self.settings.challenge
        selectors = config.get(
            "selectors",
            [
                "#px-captcha",
                "#px-captcha-container",
                "iframe[src*='captcha']",
                "iframe[src*='recaptcha']",
                ".g-recaptcha",
                "[data-testid='captcha']",
            ],
        )
        try:
            for selector in selectors:
                if self.driver.find_elements(By.CSS_SELECTOR, selector):
                    return True
        except WebDriverException:
            return False
        text_markers = config.get(
            "text_markers",
            [
                "access to this page has been denied",
                "press &amp; hold",
                "press & hold",
                "press and hold",
                "confirm you are a human",
                "verify you are human",
            ],
        )
        page_text = self.driver.page_source.lower()
        return any(marker in page_text for marker in text_markers)

    def _load_page_with_retry(self, page_url: str) -> list[dict[str, str]]:
        """Fetch one page, retrying transient failures with exponential backoff.

        Access challenges and stop requests are fatal by design and never
        retried — a challenge means the run stops, it does not adapt.
        """
        assert self.driver is not None
        attempts = self.settings.max_attempts_per_page
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            if self.stop_requested:
                raise StopRequested
            try:
                self.driver.get(page_url)
                return self._collect_page_records(page_url)
            except AccessChallengeError:
                raise
            except (TimeoutException, WebDriverException) as exc:
                last_exc = exc
                if attempt < attempts:
                    delay = backoff_seconds(attempt, self.settings.backoff_base_seconds)
                    logging.warning(
                        "Transient failure on %s (attempt %d/%d); retrying in %.1fs: %s",
                        page_url,
                        attempt,
                        attempts,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def run(self) -> None:
        next_url = self.checkpoint.get("next_url") or self.settings.start_url
        pages_completed = int(self.checkpoint.get("pages_completed", 0))
        visited_page_urls: set[str] = set()
        consecutive_empty_pages = 0
        self.driver = self._build_driver()

        try:
            while next_url and pages_completed < self.settings.max_pages:
                if self.stop_requested:
                    raise StopRequested
                if next_url in visited_page_urls:
                    logging.warning("Pagination loop detected at %s", next_url)
                    break
                if (
                    self.settings.session_restart_pages
                    and pages_completed > 0
                    and pages_completed % self.settings.session_restart_pages == 0
                ):
                    logging.info(
                        "Restarting the browser session after %d pages.",
                        pages_completed,
                    )
                    self.driver.quit()
                    self.driver = self._build_driver()

                visited_page_urls.add(next_url)
                self._courtesy_delay()
                logging.info("Loading page %d: %s", pages_completed + 1, next_url)
                try:
                    records = self._load_page_with_retry(next_url)
                    added = self._append_records(records)
                    following_url = self._next_url(next_url, pages_completed + 1)
                    pages_completed += 1
                    self._save_checkpoint(following_url, pages_completed)
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
                    logging.info(
                        "Page complete: %d cards, %d new rows.", len(records), added
                    )
                    next_url = following_url
                except (AccessChallengeError, TimeoutException, WebDriverException) as exc:
                    logging.error("Page failed: %s (%s)", next_url, exc)
                    self._capture_error(f"page-{pages_completed + 1}", next_url)
                    self._save_checkpoint(next_url, pages_completed)
                    raise
        except StopRequested:
            self._save_checkpoint(next_url, pages_completed)
            logging.info("Stopped cleanly; resume by running the same command.")
        finally:
            self.driver.quit()


def _slug_component(value: str, label: str) -> str:
    """Normalize a location for a path component such as ``toronto-on``."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"{label} must contain at least one letter or number")
    return slug


def scrape_city(config_path: Path | str, city: str, state: str) -> None:
    """Run the configured collector for one city/state pair."""
    settings = Settings.load(Path(config_path).resolve())
    city_slug = _slug_component(city, "city")
    state_slug = _slug_component(state, "state")
    location_values = {"city": city_slug, "state": state_slug}

    try:
        start_url = settings.start_url.format(**location_values)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            "start_url may only contain {city}/{state} placeholders: " + str(exc)
        ) from exc

    pagination = dict(settings.pagination)
    for key in ("url_template", "url_template_fallback"):
        template = pagination.get(key)
        if template:
            try:
                # Preserve this placeholder for PropertyScraper._next_url().
                pagination[key] = template.format(page="{page}", **location_values)
            except (KeyError, IndexError, ValueError) as exc:
                raise ValueError(
                    f"pagination.{key} placeholder error: {exc}"
                ) from exc

    location_key = f"{city_slug}-{state_slug}"
    configured = replace(
        settings,
        start_url=start_url,
        output_csv=settings.output_csv.with_name(
            f"{settings.output_csv.stem}-{location_key}{settings.output_csv.suffix}"
        ),
        checkpoint_file=settings.checkpoint_file.with_name(
            f"{settings.checkpoint_file.stem}-{location_key}"
            f"{settings.checkpoint_file.suffix}"
        ),
        error_directory=settings.error_directory / location_key,
        pagination=pagination,
    )
    scraper = PropertyScraper(configured)
    signal.signal(signal.SIGINT, scraper.request_stop)
    signal.signal(signal.SIGTERM, scraper.request_stop)
    scraper.run()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect authorized property listings into CSV."
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the site-specific JSON configuration.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    parser.add_argument("--city", help="City used in the configured URL template.")
    parser.add_argument("--state", help="State/province used in the URL template.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if bool(args.city) != bool(args.state):
            raise ValueError("--city and --state must be provided together")
        if args.city and args.state:
            scrape_city(args.config, args.city, args.state)
        else:
            scraper = PropertyScraper(Settings.load(args.config.resolve()))
            signal.signal(signal.SIGINT, scraper.request_stop)
            signal.signal(signal.SIGTERM, scraper.request_stop)
            scraper.run()
        return 0
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        logging.error("Configuration error: %s", exc)
        return 2
    except WebDriverException as exc:
        logging.error("Browser error: %s", exc)
        return 1
    except AccessChallengeError as exc:
        logging.error("Access challenge: %s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
