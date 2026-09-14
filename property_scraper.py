"""SeleniumBase Pure CDP property-listing collector for Zillow-focused authorized research; selectors and pagination are configuration-driven."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import random
import re
import signal
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from seleniumbase import sb_cdp

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


def is_valid_attach_address(value: object) -> bool:
    """True for ``host:port`` strings whose port is a valid TCP port."""
    if not isinstance(value, str):
        return False
    host, separator, port_text = value.rpartition(":")
    if not separator or not host or not port_text:
        return False
    if not port_text.isdigit() or re.search(r"\s", host + port_text):
        return False
    return 1 <= int(port_text) <= 65535


# How often to re-check for cards (and challenge markers) while waiting out
# page_wait_seconds. Short enough to notice a server-rendered block page
# quickly, long enough to stay a light local poll.
_CARD_WAIT_POLL_SECONDS = 0.25

# Pure CDP scripts are single expressions, so each script is an IIFE with its
# arguments JSON-inlined; DOM node handles cannot cross the CDP boundary.
_SCROLL_COLLECT_SCRIPT = """
(() => {
  const fraction = __SCROLL_FRACTION__;
  const specs = __FIELD_SPECS__;
  const cardSelector = __CARD_SELECTOR__;
  const firstCard = document.querySelector(cardSelector);
  let container = null;
  if (firstCard) {
    for (let node = firstCard.parentElement; node; node = node.parentElement) {
      const style = window.getComputedStyle(node);
      const canScroll = /(auto|scroll)/.test(style.overflowY);
      if (canScroll && node.scrollHeight > node.clientHeight + 20) {
        container = node;
        break;
      }
    }
  }
  if (!container) {
    container = document.scrollingElement;
  }
  if (container) {
    const step = Math.max(container.clientHeight * fraction, 1);
    const maxScrollTop = Math.max(container.scrollHeight - container.clientHeight, 0);
    container.scrollTop = Math.min(container.scrollTop + step, maxScrollTop);
  }
  const cards = [];
  for (const card of document.querySelectorAll(cardSelector)) {
    const row = [];
    for (const spec of specs) {
      let value = null;
      try {
        const node = card.querySelector(spec.selector);
        if (node) {
          value = spec.attribute
            ? node.getAttribute(spec.attribute)
            : node.textContent;
        }
      } catch (err) {
        value = null;
      }
      row.push(value);
    }
    cards.push(row);
  }
  return {
    hasContainer: Boolean(container),
    scrollTop: container ? container.scrollTop : 0,
    scrollHeight: container ? container.scrollHeight : 0,
    clientHeight: container ? container.clientHeight : 0,
    cards: cards
  };
})()
"""


def _scroll_collect_script(
    scroll_fraction: float, field_specs: list[dict[str, Any]], card_selector: str
) -> str:
    """Inline the scroll arguments into one self-contained CDP script."""
    return (
        _SCROLL_COLLECT_SCRIPT.replace(
            "__SCROLL_FRACTION__", json.dumps(scroll_fraction)
        )
        .replace("__FIELD_SPECS__", json.dumps(field_specs))
        .replace("__CARD_SELECTOR__", json.dumps(card_selector))
    )


# Rendered-text extraction for the non-scrolling path (and the no-scroll-pane
# fallback). CDP ``element.text`` concatenates every text node, including
# script contents and hidden elements; ``innerText`` is the rendered text the
# old Selenium ``element.text`` returned. The returned shape is exactly what
# ``_records_from_card_data`` consumes.
_DOM_EXTRACT_SCRIPT = """
(() => {
  const specs = __FIELD_SPECS__;
  const cardSelector = __CARD_SELECTOR__;
  const cards = [];
  for (const card of document.querySelectorAll(cardSelector)) {
    const row = [];
    for (const spec of specs) {
      let value = null;
      try {
        const node = card.querySelector(spec.selector);
        if (node) {
          value = spec.attribute
            ? node.getAttribute(spec.attribute)
            : (typeof node.innerText === "string"
                ? node.innerText
                : node.textContent);
        }
      } catch (err) {
        value = null;
      }
      row.push(value);
    }
    cards.push(row);
  }
  return { cards: cards };
})()
"""


def _dom_extract_script(field_specs: list[dict[str, Any]], card_selector: str) -> str:
    """Inline the rendered-text extraction arguments into one CDP script."""
    return (
        _DOM_EXTRACT_SCRIPT.replace("__FIELD_SPECS__", json.dumps(field_specs))
        .replace("__CARD_SELECTOR__", json.dumps(card_selector))
    )


def _normalized_url(url: str | None) -> str:
    """Normalize a URL the way a browser does (trailing slash and case)."""
    if not url:
        return ""
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}?{parts.query}"


def _write_page_archive(archive_directory: Path, page_number: int, html: str) -> Path:
    """Gzip one page of raw HTML into the archive directory; return its path."""
    archive_directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = archive_directory / f"{stamp}-page-{page_number:03d}.html.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(html)
    return path


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
    attach_address: str | None
    user_data_dir: str | None
    incremental_stop: dict[str, Any]
    archive_directory: Path | None
    report_file: Path | None

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
        incremental_stop = raw.get("incremental_stop") or {}
        if not isinstance(incremental_stop, dict):
            raise TypeError("incremental_stop must be a JSON object")
        archive_directory = raw.get("archive_directory")
        report_file = raw.get("report_file")
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
            attach_address=raw.get("attach_address") or None,
            user_data_dir=raw.get("user_data_dir") or None,
            incremental_stop=dict(incremental_stop),
            archive_directory=(
                (base / archive_directory).resolve() if archive_directory else None
            ),
            report_file=(base / report_file).resolve() if report_file else None,
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
        if self.max_pages < 0:
            errors.append("max_pages cannot be negative (0 means unlimited)")
        if self.attach_address is not None and not is_valid_attach_address(
            self.attach_address
        ):
            errors.append(
                "attach_address must be 'host:port' with a port between 1 and 65535"
            )
        incremental_stop = self.incremental_stop
        if incremental_stop.get("enabled"):
            try:
                known_ratio = float(incremental_stop.get("known_ratio", 0.8))
                min_records = int(incremental_stop.get("min_records", 20))
            except (TypeError, ValueError):
                errors.append("incremental_stop values must be numeric")
            else:
                if not 0.0 <= known_ratio <= 1.0:
                    errors.append(
                        "incremental_stop.known_ratio must be between 0 and 1"
                    )
                if min_records < 0:
                    errors.append("incremental_stop.min_records cannot be negative")
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


class BrowserError(RuntimeError):
    """A browser/CDP session failure; retryable and mapped to exit code 1."""


class AccessChallengeError(RuntimeError):
    """Raised when a site explicitly requires human verification."""


class PropertyScraper:
    def __init__(self, settings: Settings) -> None:
        settings.validate()
        self.settings = settings
        self.session: Any | None = None
        self.attached = bool(settings.attach_address)
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

    def _check_attach_endpoint(self) -> None:
        """Fail fast when the configured DevTools endpoint is not listening."""
        address = self.settings.attach_address
        assert address is not None
        url = f"http://{address}/json/version"
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                response.read(64)
        except OSError as exc:
            raise BrowserError(
                f"Chrome DevTools endpoint {url} is unreachable: {exc}. "
                "Start (or restart) the browser with scripts/launch_attach_chrome.sh, "
                "then run the scraper again."
            ) from exc

    def _warn_on_multiple_page_targets(self, address: str) -> None:
        """Warn when the attached browser has more than one open page tab.

        SeleniumBase Pure CDP Mode drives the newest page target, so with
        several tabs open the scraper can drive the wrong one. This is a
        warning only; tab pinning is deliberately not implemented.
        """
        url = f"http://{address}/json/list"
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                targets = json.loads(response.read())
        except (OSError, ValueError):
            return
        if not isinstance(targets, list):
            return
        page_tabs = sum(
            1
            for target in targets
            if isinstance(target, dict) and target.get("type") == "page"
        )
        if page_tabs > 1:
            logging.warning(
                "Attached browser has %d open page tabs; SeleniumBase CDP Mode "
                "drives the newest tab, so close extra tabs (or keep a single "
                "tab) before running.",
                page_tabs,
            )

    def _build_session(self, initial_url: str) -> Any:
        """Create the Pure CDP session for this run (the one injectable seam).

        Attach mode connects to the configured DevTools endpoint and never
        launches or closes a browser. ``sb_cdp.Chrome`` navigates the session
        to its URL argument during construction, so attach mode must receive
        the first target URL -- ``about:blank`` would hijack the user's
        attached tab. Launch mode starts its own Chrome at ``about:blank``
        and lets the per-page retry loop do the real navigation.
        """
        if self.settings.attach_address:
            # A running browser ignores launch flags such as --headless or
            # --user-agent; connect to it through the DevTools endpoint instead.
            self._check_attach_endpoint()
            host, _, port_text = self.settings.attach_address.rpartition(":")
            try:
                return sb_cdp.Chrome(initial_url, host=host, port=int(port_text))
            except BrowserError:
                raise
            except Exception as exc:
                raise BrowserError(
                    f"Could not attach to Chrome at {self.settings.attach_address}: "
                    f"{exc}. Start (or restart) the browser with "
                    "scripts/launch_attach_chrome.sh, then run the scraper again."
                ) from exc
        kwargs: dict[str, Any] = {
            "browser_args": [
                "--window-size=1440,1200",
                "--disable-notifications",
            ]
        }
        if self.settings.headless:
            kwargs["headless"] = True
        if self.settings.user_agent:
            kwargs["agent"] = self.settings.user_agent
        if self.settings.user_data_dir:
            kwargs["user_data_dir"] = self.settings.user_data_dir
        try:
            return sb_cdp.Chrome("about:blank", **kwargs)
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError(f"Could not launch Chrome: {exc}") from exc

    def _courtesy_delay(self) -> None:
        delay = self.settings.request_delay_seconds
        jitter = self.settings.request_jitter_seconds
        time.sleep(max(0.0, delay + random.uniform(-jitter, jitter)))

    def _finalize_record(
        self, values: dict[str, str], page_url: str
    ) -> dict[str, str]:
        """Apply URL joining, id fallback, and provenance to raw field values."""
        record = dict(values)
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

    def _records_from_card_data(
        self, cards: list[list[Any]], page_url: str
    ) -> list[dict[str, str]]:
        """Map raw card rows from the bulk JS call into normalized records.

        ``cards`` holds one array of raw field values per card, ordered like
        ``self.settings.field_rules``; a missing element arrives as ``None``.
        This is pure so it can be tested without a browser.
        """
        field_names = list(self.settings.field_rules)
        records: list[dict[str, str]] = []
        for row in cards:
            values: dict[str, str] = {}
            for index, name in enumerate(field_names):
                raw = row[index] if index < len(row) else None
                rule = self.settings.field_rules[name]
                text = "" if raw is None else str(raw)
                value = normalize(text, rule.transform)
                values[name] = apply_validation(
                    value, rule.transform, rule.validation
                )
            records.append(self._finalize_record(values, page_url))
        return records

    def _extract_rendered_records(
        self, field_specs: list[dict[str, Any]], page_url: str
    ) -> list[dict[str, str]]:
        """Extract cards with one JS call using rendered text (``innerText``).

        Used when the lazy-scroll collector is off (or no scroll pane was
        found). Non-attribute fields deliberately come from ``innerText`` so
        script contents and hidden elements cannot leak into the CSV the way
        CDP ``element.text`` would.
        """
        assert self.session is not None
        try:
            result = self.session.execute_script(
                _dom_extract_script(field_specs, self.settings.card_selector)
            ) or {}
        except Exception as exc:
            raise BrowserError(f"Card extraction failed: {exc}") from exc
        return self._records_from_card_data(result.get("cards") or [], page_url)

    def _incremental_complete(
        self, records: list[dict[str, str]], added: int
    ) -> bool:
        """True when an incremental refresh has mostly rediscovered known rows."""
        config = self.settings.incremental_stop
        if not config.get("enabled") or not records:
            return False
        if len(records) < int(config.get("min_records", 20)):
            return False
        known_ratio = float(config.get("known_ratio", 0.8))
        already_known = (len(records) - added) / len(records)
        return already_known >= known_ratio

    def _archive_page(self, page_number: int) -> None:
        """Archive the raw HTML of a successfully fetched page."""
        archive_directory = self.settings.archive_directory
        if archive_directory is None or self.session is None:
            return
        try:
            path = _write_page_archive(
                archive_directory, page_number, self.session.get_page_source()
            )
        except OSError as exc:
            logging.warning("Could not archive page %d: %s", page_number, exc)
        except Exception as exc:
            raise BrowserError(f"Could not archive page {page_number}: {exc}") from exc
        else:
            logging.debug("Archived page %d to %s", page_number, path)

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
                self.seen_keys.add(key)
                added += 1
            handle.flush()
        return added

    def _capture_error(self, label: str, page_url: str) -> None:
        if not self.session:
            return
        self.settings.error_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = self.settings.error_directory / f"{stamp}-{label}"
        try:
            self.session.save_screenshot(str(base.with_suffix(".png")))
            base.with_suffix(".html").write_text(
                self.session.get_page_source(), encoding="utf-8"
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
            # Diagnostics are best-effort and must not mask the real failure.
            logging.warning("Could not save diagnostics: %s", exc)

    def _next_url(self, current_url: str, page_number: int) -> str | None:
        assert self.session is not None
        pagination = self.settings.pagination
        mode = pagination["mode"]

        if mode == "url_template":
            template = pagination["url_template"]
            return template.format(page=page_number + 1)

        if mode != "next_button":
            raise ValueError(f"Unsupported pagination mode: {mode}")

        try:
            # A missing next control is expected page-end, not a wait: check
            # presence first because CDP find_element waits out its timeout.
            if not self.session.is_element_present(pagination["next_selector"]):
                logging.info("No usable next-page control: not present")
                return None
            button = self.session.find_element(pagination["next_selector"])
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

    def _wait_for_cards(self) -> list[Any] | None:
        """Wait up to ``page_wait_seconds`` for cards, watching for a challenge.

        Returns the rendered cards, or ``None`` when the wait expires.
        Challenge markers on a page with no cards raise as soon as they are
        seen instead of waiting out the full ``page_wait_seconds`` before the
        diagnosis; a slow legitimate page still gets the full wait, and the
        marker semantics (strong strings only, visible text, DOM first) are
        exactly ``_detect_challenge``'s.
        """
        assert self.session is not None
        deadline = time.monotonic() + self.settings.page_wait_seconds
        poll_seconds = min(_CARD_WAIT_POLL_SECONDS, self.settings.page_wait_seconds)
        card_lookup = (
            "document.querySelector("
            f"{json.dumps(self.settings.card_selector)}) !== null"
        )
        while True:
            try:
                # CDP find_elements waits out a ~2s miss timeout; gate it with
                # a fast single-expression check so an empty or challenge page
                # still polls on the configured cadence (and raises on a
                # challenge) instead of stalling in a missing-element wait.
                if self.session.evaluate(card_lookup):
                    cards = self.session.find_elements(self.settings.card_selector)
                else:
                    cards = []
            except Exception as exc:
                raise BrowserError(f"Could not query the page for cards: {exc}") from exc
            if cards:
                return cards
            if self._detect_challenge():
                raise AccessChallengeError(
                    "The site requested human verification; collection stopped."
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(poll_seconds, remaining))

    def _collect_page_records(self, page_url: str) -> list[dict[str, str]]:
        """Collect cards while progressively scrolling a lazy-rendered result pane."""
        assert self.session is not None
        cards = self._wait_for_cards()
        if cards is None:
            # A rendered page with no cards is an empty page, not a failure;
            # the consecutive-empty-page logic decides when to stop.
            logging.warning(
                "No cards rendered within %.1fs; treating the page as empty.",
                self.settings.page_wait_seconds,
            )
            return []
        scrolling = self.settings.scrolling
        field_specs = [
            {"selector": rule.selector, "attribute": rule.attribute}
            for rule in self.settings.field_rules.values()
        ]
        if not scrolling.get("enabled", False):
            return self._extract_rendered_records(field_specs, page_url)
        max_rounds = int(scrolling.get("max_rounds", 80))
        stable_rounds_required = int(scrolling.get("stable_rounds", 3))
        poll_seconds = float(
            scrolling.get("poll_seconds", scrolling.get("pause_seconds", 0.2))
        )
        scroll_fraction = min(
            max(float(scrolling.get("scroll_fraction", 0.8)), 0.1), 1.0
        )
        # Card details can arrive seconds after the pane reaches the bottom (a
        # slow field response). Once at the bottom, the loop may not stop until
        # settle_seconds have passed with no change in any extracted value or
        # in the scroll geometry, anchored to the later of bottom-reached and
        # the last observed change; every change restarts that quiet period.
        # This bounds late hydration — it deliberately is not the old loop's
        # fixed per-round sleep band.
        settle_seconds = max(0.0, float(scrolling.get("settle_seconds", 2.5)))
        # A short dwell between moving rounds paces the pane's own lazy loads
        # (one viewport step at a time instead of a burst of steps). This is
        # not the request-level courtesy delay, which stays in _courtesy_delay.
        step_delay_seconds = max(
            0.0, float(scrolling.get("step_delay_seconds", 0.05))
        )
        records_by_key: dict[str, dict[str, str]] = {}
        stable_rounds = 0
        previous_cards: list[Any] | None = None
        previous_geometry: tuple[Any, ...] | None = None
        last_change_at = time.monotonic()
        bottom_reached_at: float | None = None

        for round_number in range(1, max_rounds + 1):
            if self.stop_requested:
                raise StopRequested

            # One round-trip finds the scroll pane, steps it, and reads every
            # card's raw fields; the script carries its own arguments because
            # CDP execute_script takes a single expression and no arguments.
            try:
                result = self.session.execute_script(
                    _scroll_collect_script(
                        scroll_fraction, field_specs, self.settings.card_selector
                    )
                ) or {}
            except Exception as exc:
                raise BrowserError(f"Scroll collection failed: {exc}") from exc
            if not result.get("hasContainer", True):
                logging.warning(
                    "No scroll container found; using currently rendered cards."
                )
                return self._extract_rendered_records(field_specs, page_url)
            cards_data = result.get("cards") or []
            for record in self._records_from_card_data(cards_data, page_url):
                key = self._record_key(record)
                if not key:
                    continue
                previous = records_by_key.get(key, {})
                records_by_key[key] = {
                    field: record.get(field) or previous.get(field, "")
                    for field in CSV_FIELDS
                }

            now = time.monotonic()
            geometry = (
                result.get("scrollTop", 0),
                result.get("scrollHeight", 0),
                result.get("clientHeight", 0),
            )
            scroll_top, scroll_height, client_height = geometry
            scrollable = scroll_height > client_height + 20
            at_bottom = scroll_top + client_height >= scroll_height - 5
            # Extracted values count as change too: late hydration must reset
            # the stability counter and the settle quiet period even when the
            # card count and the scroll position are already static.
            changed = cards_data != previous_cards or geometry != previous_geometry
            stable_rounds = 0 if changed else stable_rounds + 1
            previous_cards = cards_data
            previous_geometry = geometry
            if changed:
                last_change_at = now
            if at_bottom and bottom_reached_at is None:
                bottom_reached_at = now
            if at_bottom:
                quiet_anchor = last_change_at
                if bottom_reached_at is not None:
                    quiet_anchor = max(quiet_anchor, bottom_reached_at)
                quiet_seconds = now - quiet_anchor
            else:
                quiet_seconds = 0.0

            if round_number % 10 == 0:
                logging.info(
                    "Lazy scroll: round %d, %d unique cards.",
                    round_number,
                    len(records_by_key),
                )
            # The stop criterion needs the bottom, the stable-round count, and
            # (on a scrollable pane) a settle_seconds quiet period measured
            # from the later of bottom-reached and the last observed change.
            if (
                at_bottom
                and stable_rounds >= stable_rounds_required
                and (not scrollable or quiet_seconds >= settle_seconds)
            ):
                break
            if changed and not at_bottom:
                # Still moving or still growing: keep the next viewport step a
                # short dwell away instead of firing a burst of steps, and do
                # not fall back to the full poll interval while making progress.
                time.sleep(step_delay_seconds)
                continue
            time.sleep(poll_seconds)

        logging.info("Lazy scrolling collected %d unique cards.", len(records_by_key))
        return list(records_by_key.values())

    def _detect_challenge(self) -> bool:
        """Yes/no: is the page a real human-verification interstitial?

        DOM selectors come first — a challenge container (``#px-captcha``,
        a captcha iframe, reCAPTCHA widgets) is unambiguous. Text markers are
        deliberately strong strings only; the bare word ``captcha`` appears in
        ordinary Zillow markup and would mislabel a slow legitimate page.
        """
        assert self.session is not None
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
                # CDP find_elements waits out a ~2s miss; a single JS query is
                # immediate, so the challenge check stays as cheap as it was
                # with raw Selenium's non-waiting find_elements.
                expression = f"document.querySelector({json.dumps(selector)}) !== null"
                if self.session.evaluate(expression):
                    return True
        except Exception:
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
        # Prefer visible text: inline scripts and hidden markup can contain
        # challenge-sounding strings without the page actually being one.
        try:
            visible_text = self.session.evaluate(
                "document.body ? document.body.innerText : ''"
            )
        except Exception:
            visible_text = None
        if isinstance(visible_text, str) and visible_text.strip():
            page_text = visible_text.lower()
        else:
            # An empty/blank document body (common on block pages) carries no
            # visible markers; fall back to the raw source.
            try:
                page_text = self.session.get_page_source().lower()
            except Exception as exc:
                raise BrowserError(f"Could not read the page source: {exc}") from exc
        return any(marker in page_text for marker in text_markers)

    def _navigate(self, page_url: str) -> None:
        """Navigate, surfacing load failures that CDP ``get`` swallows.

        ``CDPMethods.get`` prints "Timeout loading ..." and returns when a
        navigation times out, which would otherwise look like a loaded empty
        page. A marker is injected into the current document before
        navigating: if it survives *and* the URL is unchanged, nothing
        committed and the attempt is a transient failure. If the marker is
        gone, a new document committed -- even when the site then redirected
        back to the URL the tab was already on -- so the navigation counts as
        successful. Landing on Chrome's network-error page is still a
        failure. A session already on the target (normalization-aware, which
        is the attach-constructor case) is not re-navigated.
        """
        assert self.session is not None
        try:
            before = self.session.get_current_url() or ""
            if _normalized_url(before) == _normalized_url(page_url):
                return
            marker = f"nav-{uuid.uuid4().hex}"
            self.session.evaluate(
                f"window.__property_scraper_nav_marker = {json.dumps(marker)}"
            )
            self.session.get(page_url)
            after = self.session.get_current_url() or ""
            marker_survives = bool(
                self.session.evaluate(
                    "window.__property_scraper_nav_marker || ''"
                )
            )
        except Exception as exc:
            raise BrowserError(f"Navigation to {page_url} failed: {exc}") from exc
        if after.startswith("chrome-error://") or (
            marker_survives and _normalized_url(after) == _normalized_url(before)
        ):
            raise BrowserError(
                f"Navigation to {page_url} did not complete (still at {after!r})"
            )

    def _load_page_with_retry(self, page_url: str) -> list[dict[str, str]]:
        """Fetch one page, retrying transient failures with exponential backoff.

        Access challenges and stop requests are fatal by design and never
        retried — a challenge means the run stops, it does not adapt.
        """
        assert self.session is not None
        attempts = self.settings.max_attempts_per_page
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            if self.stop_requested:
                raise StopRequested
            try:
                self._navigate(page_url)
                return self._collect_page_records(page_url)
            except AccessChallengeError:
                raise
            except BrowserError as exc:
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

    def _write_report(
        self,
        *,
        started_at: str,
        pages_completed: int,
        records_added: int,
        stop_reason: str,
        challenges: int,
        resumed_from_pages: int,
    ) -> None:
        """Atomically write the run report; a write failure only warns."""
        path = self.settings.report_file
        if path is None:
            return
        payload = {
            "schema_version": 1,
            "start_url": self.settings.start_url,
            "pages_completed": pages_completed,
            "records_added": records_added,
            "records_total": len(self.seen_keys),
            "stop_reason": stop_reason,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "challenges": challenges,
            "resumed_from_pages": resumed_from_pages,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            logging.warning("Could not write run report %s: %s", path, exc)

    def run(self) -> None:
        started_at = datetime.now(UTC).isoformat()
        next_url = self.checkpoint.get("next_url") or self.settings.start_url
        # Incremental stop is only sound for a fresh sweep: a run resumed
        # mid-pagination can land on a mostly-known page while later pages
        # still hold new listings (Zillow reorders between sessions).
        fresh_sweep = next_url == self.settings.start_url
        pages_completed = int(self.checkpoint.get("pages_completed", 0))
        resumed_from_pages = pages_completed
        records_added = 0
        challenges = 0
        stop_reason: str | None = None
        visited_page_urls: set[str] = set()
        consecutive_empty_pages = 0

        if self.attached and self.settings.session_restart_pages:
            logging.warning(
                "session_restart_pages=%d is ignored in attach mode; the "
                "connected browser is left running.",
                self.settings.session_restart_pages,
            )

        try:
            if self.attached:
                # Fail fast on a dead endpoint even when there is nothing to
                # fetch; keep it inside the report-writing try so a dead
                # endpoint still leaves a browser_error report.
                self._check_attach_endpoint()
                address = self.settings.attach_address
                assert address is not None
                self._warn_on_multiple_page_targets(address)
            will_fetch_first_page = bool(next_url) and (
                self.settings.max_pages <= 0
                or pages_completed < self.settings.max_pages
            )
            skip_first_courtesy_delay = False
            if will_fetch_first_page and self.attached:
                # The attach-mode session constructor navigates the attached
                # tab to the first target URL, so the first courtesy delay has
                # to happen before the session is built; the loop skips the
                # duplicate delay for that same first page.
                self._courtesy_delay()
                skip_first_courtesy_delay = True
            self.session = (
                self._build_session(next_url) if will_fetch_first_page else None
            )
            try:
                while next_url and (
                    self.settings.max_pages <= 0
                    or pages_completed < self.settings.max_pages
                ):
                    if self.stop_requested:
                        raise StopRequested
                    if next_url in visited_page_urls:
                        logging.warning("Pagination loop detected at %s", next_url)
                        stop_reason = "pagination_loop"
                        break
                    if (
                        self.settings.session_restart_pages
                        and not self.attached
                        and pages_completed > 0
                        and pages_completed % self.settings.session_restart_pages == 0
                    ):
                        logging.info(
                            "Restarting the browser session after %d pages.",
                            pages_completed,
                        )
                        self.session.quit()
                        self.session = self._build_session(next_url)

                    visited_page_urls.add(next_url)
                    if skip_first_courtesy_delay:
                        skip_first_courtesy_delay = False
                    else:
                        self._courtesy_delay()
                    logging.info("Loading page %d: %s", pages_completed + 1, next_url)
                    try:
                        records = self._load_page_with_retry(next_url)
                        if self.settings.archive_directory is not None:
                            self._archive_page(pages_completed + 1)
                        added = self._append_records(records)
                        records_added += added
                        pages_completed += 1
                        if fresh_sweep and self._incremental_complete(records, added):
                            self._save_checkpoint(None, pages_completed)
                            logging.info(
                                "Incremental refresh complete after %d page(s).",
                                pages_completed,
                            )
                            stop_reason = "incremental_complete"
                            break
                        following_url = self._next_url(next_url, pages_completed)
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
                                stop_reason = "empty_page_threshold"
                                break
                        else:
                            consecutive_empty_pages = 0
                        logging.info(
                            "Page complete: %d cards, %d new rows.",
                            len(records),
                            added,
                        )
                        next_url = following_url
                    except (AccessChallengeError, BrowserError) as exc:
                        if isinstance(exc, AccessChallengeError):
                            challenges = 1
                            stop_reason = "challenge"
                        else:
                            stop_reason = "browser_error"
                        logging.error("Page failed: %s (%s)", next_url, exc)
                        self._capture_error(f"page-{pages_completed + 1}", next_url)
                        self._save_checkpoint(next_url, pages_completed)
                        raise
                else:
                    if next_url is not None:
                        stop_reason = "max_pages"
                    elif self.seen_keys:
                        stop_reason = "pagination_exhausted"
                    else:
                        # Zero records ever collected for this region: a
                        # no-next-page ending is partial, never complete.
                        logging.warning(
                            "Pagination ended with no next page while the "
                            "region has zero records; refusing to treat the "
                            "region as exhausted (possible soft block or "
                            "maintenance page)."
                        )
                        stop_reason = "empty_page_threshold"
            except StopRequested:
                self._save_checkpoint(next_url, pages_completed)
                stop_reason = "stop_requested"
                logging.info("Stopped cleanly; resume by running the same command.")
            finally:
                if self.session is not None:
                    if self.attached:
                        logging.info(
                            "Attached browser left open; the session was not quit."
                        )
                    else:
                        self.session.quit()
        finally:
            if stop_reason is None:
                stop_reason = "browser_error"
            self._write_report(
                started_at=started_at,
                pages_completed=pages_completed,
                records_added=records_added,
                stop_reason=stop_reason,
                challenges=challenges,
                resumed_from_pages=resumed_from_pages,
            )


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
    report_file = settings.report_file
    if report_file is not None:
        report_file = report_file.with_name(
            f"{report_file.stem}-{location_key}{report_file.suffix}"
        )
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
        archive_directory=(
            settings.archive_directory / location_key
            if settings.archive_directory is not None
            else None
        ),
        report_file=report_file,
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
    except BrowserError as exc:
        logging.error("Browser error: %s", exc)
        return 1
    except AccessChallengeError as exc:
        logging.error("Access challenge: %s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
