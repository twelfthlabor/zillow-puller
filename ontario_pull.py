"""Region-by-region Ontario pull queue for the Zillow property scraper.

The scraper exits with code 3 when Zillow asks for human verification. This
runner walks the configured cities in file order, keeps per-region checkpoints
and state, and on a challenge waits a randomized cooldown before resuming the
same region. A region that hits ``--max-consecutive-challenges`` is marked
``challenged`` and the queue moves on to the next region; fresh regions
(pending/partial) are processed before challenged retries, and the whole queue
stops early only when the total number of exit-3 events in the invocation
reaches ``--max-total-challenges``, checked at region boundaries so the
in-flight region always settles first. It never solves, bypasses, or suppresses
a challenge -- it only pauses and resumes.

Exit codes used by ``property-ontario``:

* ``0`` -- queue processed, no challenged/failed regions and no challenge stop
* ``2`` -- configuration or usage error (including a held state lock or an
  unreachable ``attach_address``)
* ``3`` -- stopped by the total challenge bound, or one or more regions were
  left ``challenged``
* ``4`` -- queue finished with one or more failed regions
* ``130``/``143`` -- interrupted by SIGINT/SIGTERM (state saved)

A scraper child that produces no output for longer than
``--stall-timeout-minutes`` (default 10) is terminated so the ordinary retry
path resumes the region from its checkpoint. The watchdog uses the platform's
sleep-inclusive clock where one exists (CLOCK_BOOTTIME on Linux, CLOCK_MONOTONIC
on macOS), so time spent asleep counts toward the timeout and a run frozen by
machine sleep recovers at the first poll after wake.
"""

from __future__ import annotations

import argparse
import codecs
import contextlib
import copy
import fcntl
import json
import logging
import math
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

LOGGER = logging.getLogger("ontario_pull")

SCHEMA_VERSION = 1
STATE_FILENAME = "queue-state.json"
SUMMARY_FILENAME = "last-run-summary.json"
REGION_CONFIG_FILENAME = "config.json"
REPORT_FILENAME = "report.json"
LOCK_FILENAME = ".lock"

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_CHALLENGE_STOP = 3
EXIT_FAILED_REGIONS = 4

CHALLENGE_EXIT_CODE = 3
DONE_STOP_REASONS = frozenset({"pagination_exhausted", "incremental_complete"})
VALID_STATUSES = ("pending", "done", "partial", "failed", "challenged")

DEFAULT_STALL_TIMEOUT_MINUTES = 10.0
STALL_TERMINATE_GRACE_SECONDS = 10.0
STALL_POLL_SECONDS = 1.0
STALL_READER_JOIN_SECONDS = 5.0
STALL_OUTPUT_CHUNK_BYTES = 65536

DEFAULT_REGIONS_PATH = Path(__file__).resolve().parent / "regions.ontario.json"


class ConfigError(Exception):
    """User-facing configuration problem (CLI maps this to exit code 2)."""


class QueueInterrupted(Exception):
    """Raised from signal handlers so the queue can save state and stop."""

    def __init__(self, signum: int):
        super().__init__(f"received signal {signum}")
        self.signum = signum


def slugify(value: str) -> str:
    """Normalize a city/state for URLs, directory names, and slugs."""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _iso_now(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), tz=UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class QueueLimits:
    """Bounded retry/cooldown policy for a queue run."""

    cooldown_min_minutes: float = 30.0
    cooldown_max_minutes: float = 60.0
    max_consecutive_challenges: int = 3
    max_total_challenges: int = 6
    error_max_attempts: int = 2
    error_backoff_seconds: float = 60.0
    stall_timeout_minutes: float = DEFAULT_STALL_TIMEOUT_MINUTES
    retry_failed: bool = False
    reopen: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.cooldown_min_minutes < 0 or self.cooldown_max_minutes < 0:
            raise ValueError("cooldown minutes cannot be negative")
        if self.cooldown_min_minutes > self.cooldown_max_minutes:
            raise ValueError("cooldown-min-minutes cannot exceed cooldown-max-minutes")
        if self.max_consecutive_challenges < 1:
            raise ValueError("max-consecutive-challenges must be at least 1")
        if self.max_total_challenges < 1:
            raise ValueError("max-total-challenges must be at least 1")
        if self.max_total_challenges < self.max_consecutive_challenges:
            raise ValueError(
                "max-total-challenges cannot be less than max-consecutive-challenges "
                f"({self.max_total_challenges} < {self.max_consecutive_challenges}); "
                "a single region could never reach its own challenge limit"
            )
        if self.error_max_attempts < 1:
            raise ValueError("error-max-attempts must be at least 1")
        if self.error_backoff_seconds < 0:
            raise ValueError("error backoff seconds cannot be negative")
        if not math.isfinite(self.stall_timeout_minutes):
            raise ValueError("stall-timeout-minutes must be a finite number")
        if self.stall_timeout_minutes < 0:
            raise ValueError("stall-timeout-minutes cannot be negative")
        if not isinstance(self.reopen, frozenset):
            object.__setattr__(self, "reopen", frozenset(self.reopen))

    @property
    def cooldown_seconds(self) -> tuple[float, float]:
        """Cooldown range in seconds, as ``sleep`` expects it."""
        return (self.cooldown_min_minutes * 60.0, self.cooldown_max_minutes * 60.0)

    @property
    def stall_timeout_seconds(self) -> float:
        """Stall watchdog window in seconds; 0 disables the watchdog."""
        return self.stall_timeout_minutes * 60.0


@dataclass(frozen=True)
class Region:
    """One city to pull, with the province/state used in URLs and slugs."""

    city: str
    name: str = ""
    state: str = "on"

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any], default_state: str = "on") -> Region:
        city = str(mapping.get("city", "")).strip()
        if not city:
            raise ConfigError("region entry is missing a city")
        if not slugify(city):
            raise ConfigError(f"region city {city!r} has no usable letters or numbers")
        name = str(mapping.get("name") or city)
        state = str(mapping.get("state") or mapping.get("province") or default_state)
        return cls(city=city, name=name, state=state)

    @property
    def city_slug(self) -> str:
        return slugify(self.city)

    @property
    def state_slug(self) -> str:
        return slugify(self.state) or "on"

    @property
    def slug(self) -> str:
        return f"{self.city_slug}-{self.state_slug}"

    @property
    def tokens(self) -> frozenset[str]:
        """Accepted ``--only``/``--skip`` identifiers (city or full slug)."""
        return frozenset({self.slug, self.city_slug})


@dataclass
class RegionState:
    """Persisted per-region progress.

    Status meanings:

    * ``pending`` -- queued, not finished yet
    * ``done`` -- completed (report stop_reason says pagination/scan finished)
    * ``partial`` -- stopped short of done; resumable on a later invocation
    * ``failed`` -- non-challenge errors exhausted; needs ``--retry-failed``
      (or ``--reopen``)
    * ``challenged`` -- hit the per-region consecutive-challenge limit; the
      queue moved on, and a later invocation resumes it automatically
    """

    status: str = "pending"
    attempts: int = 0
    challenge_waits: int = 0
    last_exit_code: int | None = None
    last_stop_reason: str | None = None
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "attempts": self.attempts,
            "challenge_waits": self.challenge_waits,
            "last_exit_code": self.last_exit_code,
            "last_stop_reason": self.last_stop_reason,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RegionState:
        status = str(data.get("status", "pending"))
        if status not in VALID_STATUSES:
            status = "pending"
        last_exit = data.get("last_exit_code")
        stop_reason = data.get("last_stop_reason")
        return cls(
            status=status,
            attempts=int(data.get("attempts", 0) or 0),
            challenge_waits=int(data.get("challenge_waits", 0) or 0),
            last_exit_code=int(last_exit) if last_exit is not None else None,
            last_stop_reason=str(stop_reason) if stop_reason is not None else None,
            updated_at=str(data.get("updated_at", "")),
        )


@dataclass(frozen=True)
class QueueResult:
    """Outcome of one ``run_queue`` invocation."""

    exit_code: int
    regions: dict[str, RegionState] = field(default_factory=dict)
    stopped_by_challenge: bool = False
    interrupted: bool = False

    def _by_status(self, status: str) -> list[str]:
        return sorted(slug for slug, state in self.regions.items() if state.status == status)

    @property
    def done(self) -> list[str]:
        return self._by_status("done")

    @property
    def partial(self) -> list[str]:
        return self._by_status("partial")

    @property
    def failed(self) -> list[str]:
        return self._by_status("failed")

    @property
    def challenged(self) -> list[str]:
        return self._by_status("challenged")

    @property
    def pending(self) -> list[str]:
        return self._by_status("pending")


# ---------------------------------------------------------------------------
# Regions file and config generation
# ---------------------------------------------------------------------------


def load_regions_file(path: str | Path) -> list[Region]:
    """Load ``regions.ontario.json``-style files into :class:`Region` objects."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"could not read regions file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"regions file {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping) or not isinstance(raw.get("regions"), list):
        raise ConfigError(f"regions file {path} must be an object with a 'regions' list")
    province = str(raw.get("province") or "on")
    regions = [Region.from_mapping(entry, province) for entry in raw["regions"]]
    if not regions:
        raise ConfigError(f"regions file {path} contains no regions")
    return regions


def known_region_tokens(regions: Sequence[Region]) -> set[str]:
    """All accepted identifiers (city slug or full slug) for the given regions."""
    return {token for region in regions for token in region.tokens}


def select_regions(
    regions: Sequence[Region],
    only: Sequence[str] | None = None,
    skip: Sequence[str] | None = None,
) -> list[Region]:
    """Apply ``--only`` then ``--skip`` filters; unknown slugs are an error."""
    selected = list(regions)
    known = known_region_tokens(regions)
    for label, tokens in (("--only", only), ("--skip", skip)):
        if not tokens:
            continue
        requested = [token.strip() for token in tokens if token.strip()]
        unknown = [token for token in requested if token not in known]
        if unknown:
            raise ConfigError(
                f"{label} has unknown region slug(s): {', '.join(unknown)}; "
                f"known slugs: {', '.join(sorted(known))}"
            )
        wanted = set(requested)
        if label == "--only":
            selected = [region for region in selected if region.tokens & wanted]
        else:
            selected = [region for region in selected if not (region.tokens & wanted)]
    return selected


def validate_base_config(base_config: Mapping[str, Any]) -> None:
    """Require ``{city}`` and ``{state}`` placeholders in ``start_url``."""
    start_url = base_config.get("start_url")
    if not isinstance(start_url, str) or not start_url:
        raise ConfigError("base config must define a non-empty string start_url")
    missing = [placeholder for placeholder in ("{city}", "{state}") if placeholder not in start_url]
    if missing:
        raise ConfigError(
            "base config start_url must contain {city} and {state} placeholders "
            f"(missing {', '.join(missing)} in {start_url!r}); per-region configs "
            "cannot be generated without them"
        )


def load_base_config(path: str | Path) -> dict[str, Any]:
    """Read and validate the template config used for every region."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"could not read base config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"base config {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"base config {path} must be a JSON object")
    validate_base_config(raw)
    return raw


def _format_location(
    template: str,
    label: str,
    city_slug: str,
    state_slug: str,
    *,
    preserve_page: bool = False,
) -> str:
    try:
        if preserve_page:
            # Keep {page} for the scraper's own pagination handling.
            return template.format(page="{page}", city=city_slug, state=state_slug)
        return template.format(city=city_slug, state=state_slug)
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(f"{label} contains an unsupported placeholder: {exc}") from exc


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def validate_region_config(config_path: str | Path) -> None:
    """Validate a generated region config with the scraper's own Settings.

    ``property_scraper`` is imported lazily so that importing this module does
    not pull in Selenium; every generated config goes through this validation
    (dry runs included) before it is used or reported as ready.
    """
    config_path = Path(config_path)
    try:
        from property_scraper import Settings
    except ImportError as exc:  # pragma: no cover - environment problem
        raise ConfigError(
            f"cannot import property_scraper to validate {config_path}: {exc}"
        ) from exc
    try:
        Settings.load(config_path).validate()
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ConfigError(f"generated config {config_path} is invalid: {exc}") from exc


def build_region_config(
    base_config: Mapping[str, Any],
    region: Region,
    state_dir: str | Path,
    stall_timeout_seconds: float = 0.0,
) -> Path:
    """Write the per-region config from ``base_config`` and return its path.

    ``stall_timeout_seconds`` (0 disables) is only used to warn when the
    scraper's own ``page_wait_seconds`` exceeds the watchdog window.
    """
    state_dir = Path(state_dir)
    config = copy.deepcopy(dict(base_config))
    city_slug, state_slug = region.city_slug, region.state_slug

    config["start_url"] = _format_location(
        str(config.get("start_url", "")), "start_url", city_slug, state_slug
    )

    pagination = config.get("pagination")
    if pagination is not None:
        if not isinstance(pagination, Mapping):
            raise ConfigError("base config pagination must be a JSON object")
        pagination = dict(pagination)
        for key in ("url_template", "url_template_fallback"):
            template = pagination.get(key)
            if template:
                pagination[key] = _format_location(
                    str(template),
                    f"pagination.{key}",
                    city_slug,
                    state_slug,
                    preserve_page=True,
                )
        config["pagination"] = pagination

    # Bare filenames so the scraper resolves everything inside the region dir.
    config["output_csv"] = "listings.csv"
    config["checkpoint_file"] = "checkpoint.json"
    config["error_directory"] = "errors"
    config["report_file"] = REPORT_FILENAME
    if config.get("archive_directory") is not None:
        config["archive_directory"] = "archive"

    if "max_pages" not in config:
        LOGGER.warning(
            "%s: no max_pages configured; the scraper default (100 pages) will "
            "cap the pull; set max_pages: 0 for a full pull",
            region.slug,
        )
    elif int(config.get("max_pages") or 0):
        LOGGER.warning(
            "%s: max_pages=%s is nonzero, regions will be capped; set max_pages: 0 for a full pull",
            region.slug,
            config.get("max_pages"),
        )

    if stall_timeout_seconds > 0 and config.get("page_wait_seconds") is not None:
        try:
            page_wait_seconds = float(config["page_wait_seconds"])
        except (TypeError, ValueError):
            pass
        else:
            if page_wait_seconds > stall_timeout_seconds:
                LOGGER.warning(
                    "%s: page_wait_seconds=%s exceeds the stall timeout (%.0fs); "
                    "a legitimately slow page could be killed and retried",
                    region.slug,
                    config["page_wait_seconds"],
                    stall_timeout_seconds,
                )

    config_path = state_dir / region.slug / REGION_CONFIG_FILENAME
    _atomic_write_json(config_path, config)
    validate_region_config(config_path)
    return config_path


def generate_region_config(
    region: Region | Mapping[str, Any],
    base_config_path: str | Path,
    state_dir: str | Path,
    stall_timeout_seconds: float = 0.0,
) -> Path:
    """Load the base config and write one region's config file.

    Used by ``--dry-run``; :func:`run_queue` loads the base config once and
    calls :func:`build_region_config` per region.
    """
    if not isinstance(region, Region):
        region = Region.from_mapping(region)
    base_config = load_base_config(base_config_path)
    return build_region_config(base_config, region, state_dir, stall_timeout_seconds)


# ---------------------------------------------------------------------------
# Queue state and reporting
# ---------------------------------------------------------------------------


def load_queue_state(
    state_dir: str | Path, clock: Callable[[], float] = time.time
) -> dict[str, RegionState]:
    """Load ``queue-state.json``; missing file means a fresh queue."""
    path = Path(state_dir) / STATE_FILENAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"could not read state file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"state file {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError(f"state file {path} must be a JSON object")
    version = raw.get("schema_version")
    if version is not None and int(version) != SCHEMA_VERSION:
        raise ConfigError(
            f"state file {path} has unsupported schema_version {version}; expected {SCHEMA_VERSION}"
        )
    raw_regions = raw.get("regions", {})
    if not isinstance(raw_regions, Mapping):
        raise ConfigError(f"state file {path} regions must be a JSON object")
    return {
        str(slug): RegionState.from_dict(entry if isinstance(entry, Mapping) else {})
        for slug, entry in raw_regions.items()
    }


def save_queue_state(
    state_dir: str | Path,
    states: Mapping[str, RegionState],
    clock: Callable[[], float] = time.time,
) -> None:
    """Atomically persist the per-region state file."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _iso_now(clock),
        "regions": {slug: state.to_dict() for slug, state in states.items()},
    }
    _atomic_write_json(Path(state_dir) / STATE_FILENAME, payload)


def build_summary(
    result: QueueResult,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    counts = {
        "done": len(result.done),
        "partial": len(result.partial),
        "failed": len(result.failed),
        "challenged": len(result.challenged),
        "pending": len(result.pending),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": result.exit_code,
        "stopped_by_challenge": result.stopped_by_challenge,
        "interrupted": result.interrupted,
        "counts": counts,
        "regions": {slug: state.to_dict() for slug, state in result.regions.items()},
    }


def _read_stop_reason(region_dir: Path) -> str | None:
    """Return ``report.json``'s stop_reason, or None with a warning."""
    report_path = region_dir / REPORT_FILENAME
    if not report_path.exists():
        LOGGER.warning(
            "%s: report missing at %s; treating as partial", region_dir.name, report_path
        )
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("%s: unreadable report (%s); treating as partial", region_dir.name, exc)
        return None
    if not isinstance(report, Mapping):
        LOGGER.warning("%s: report is not a JSON object; treating as partial", region_dir.name)
        return None
    stop_reason = report.get("stop_reason")
    if not isinstance(stop_reason, str) or not stop_reason:
        LOGGER.warning("%s: report has no stop_reason; treating as partial", region_dir.name)
        return None
    return stop_reason


# ---------------------------------------------------------------------------
# Core queue
# ---------------------------------------------------------------------------


def _coerce_regions(
    regions: Iterable[Region | Mapping[str, Any]] | Mapping[str, Any],
    default_state: str = "on",
) -> list[Region]:
    if isinstance(regions, Mapping):
        default_state = str(regions.get("province") or default_state)
        regions = regions.get("regions", [])
    coerced: list[Region] = []
    for entry in regions:
        if isinstance(entry, Region):
            coerced.append(entry)
        else:
            coerced.append(Region.from_mapping(entry, default_state))
    return coerced


def _processing_order(regions: Sequence[Region], states: Mapping[str, RegionState]) -> list[Region]:
    """Fresh work (pending/partial) before challenged retries.

    Regions that are already ``challenged`` are deferred so a repeatedly
    challenged city cannot starve fresh regions across invocations. The sort
    is stable, so file order is preserved within each group; ``done``/``failed``
    stay in the fresh group and are filtered by the main loop (failed only runs
    with ``--retry-failed``/``--reopen``).
    """

    def rank(region: Region) -> int:
        return 1 if states[region.slug].status == "challenged" else 0

    return sorted(regions, key=rank)


def _touch(state: RegionState, clock: Callable[[], float]) -> None:
    state.updated_at = _iso_now(clock)


@contextlib.contextmanager
def _queue_lock(state_dir: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock on ``<state-dir>/.lock``.

    A second runner pointed at the same state directory fails fast with a
    :class:`ConfigError` (exit 2) instead of interleaving state writes. The
    lock is released on every exit path, including interrupts.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / LOCK_FILENAME
    handle = lock_path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ConfigError(
                f"another run is active (could not lock {lock_path}); "
                "wait for it to finish before starting a new run"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def preflight_attach(base_config: Mapping[str, Any], timeout: float = 2.0) -> None:
    """Fail fast when ``attach_address`` points at a dead DevTools endpoint."""
    address = base_config.get("attach_address")
    if not address:
        return
    address = str(address)
    url = address if "://" in address else f"http://{address}"
    url = url.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(1)
    except (OSError, ValueError) as exc:
        raise ConfigError(
            f"attach_address {address!r} is not reachable at {url}: {exc}; "
            "start Chrome with scripts/launch_attach_chrome.sh, or remove "
            "attach_address from the base config"
        ) from exc
    LOGGER.info("Attach endpoint reachable: %s", url)


def run_queue(
    regions: Iterable[Region | Mapping[str, Any]] | Mapping[str, Any],
    base_config_path: str | Path,
    state_dir: str | Path,
    run_region: Callable[[Path], int],
    sleep: Callable[[float], None],
    clock: Callable[[], float] = time.time,
    limits: QueueLimits | None = None,
) -> QueueResult:
    """Process regions in order, persisting progress and surviving challenges.

    Holds ``<state-dir>/.lock`` for the duration, so a concurrent run fails
    with exit 2. A region that hits its consecutive-challenge limit becomes
    ``challenged`` and the queue continues with the next region; the whole
    queue stops early only when the total challenge count in this invocation
    reaches ``limits.max_total_challenges``.
    ``run_region`` returns the scraper exit code for one generated region
    config; tests inject fakes and the CLI injects a subprocess runner.
    ``sleep`` is injected so tests never wait through real cooldowns.
    """
    limits = limits if limits is not None else QueueLimits()
    state_dir = Path(state_dir)
    with _queue_lock(state_dir):
        return _run_queue(
            regions,
            base_config_path,
            state_dir,
            run_region,
            sleep,
            clock=clock,
            limits=limits,
        )


def _run_queue(
    regions: Iterable[Region | Mapping[str, Any]] | Mapping[str, Any],
    base_config_path: str | Path,
    state_dir: Path,
    run_region: Callable[[Path], int],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    limits: QueueLimits,
) -> QueueResult:
    region_list = _coerce_regions(regions)
    base_config = load_base_config(base_config_path)
    preflight_attach(base_config)
    states = load_queue_state(state_dir, clock=clock)
    started_at = _iso_now(clock)

    for region in region_list:
        if region.slug not in states:
            states[region.slug] = RegionState(status="pending", updated_at=_iso_now(clock))

    # Reopen before ordering/processing so reopened regions count as fresh work.
    reopened_tokens: set[str] = set()
    for region in region_list:
        state = states[region.slug]
        if region.tokens & limits.reopen:
            LOGGER.info("Reopening %s (was %s)", region.slug, state.status)
            state.status = "pending"
            _touch(state, clock)
            reopened_tokens |= region.tokens & limits.reopen
    for token in sorted(set(limits.reopen) - reopened_tokens):
        LOGGER.warning("--reopen %s is excluded by --only/--skip; not reset", token)
    save_queue_state(state_dir, states, clock)

    ordered_regions = _processing_order(region_list, states)
    total_challenges = 0
    stopped_by_challenge = False
    interrupted_signum: int | None = None

    try:
        for region in ordered_regions:
            state = states[region.slug]
            if state.status == "done":
                LOGGER.info("Skipping %s (already done)", region.slug)
                continue
            if state.status == "failed" and not limits.retry_failed:
                LOGGER.info("Skipping %s (failed; use --retry-failed to retry)", region.slug)
                continue
            if state.status == "failed":
                LOGGER.info("Retrying %s (--retry-failed)", region.slug)
                state.status = "pending"
                _touch(state, clock)

            config_path = build_region_config(
                base_config, region, state_dir, limits.stall_timeout_seconds
            )
            LOGGER.info("Processing %s (%s)", region.slug, region.name)
            error_attempts = 0
            region_challenges = 0

            while True:
                state.attempts += 1
                _touch(state, clock)
                save_queue_state(state_dir, states, clock)

                exit_code = int(run_region(config_path))
                stall_killed = bool(getattr(run_region, "stalled", False))
                state.last_exit_code = exit_code

                if exit_code == 0:
                    error_attempts = 0
                    region_challenges = 0
                    stop_reason = _read_stop_reason(state_dir / region.slug)
                    state.last_stop_reason = stop_reason
                    if stop_reason in DONE_STOP_REASONS:
                        state.status = "done"
                        LOGGER.info("%s complete (stop_reason=%s)", region.slug, stop_reason)
                    else:
                        state.status = "partial"
                        LOGGER.warning(
                            "%s partial (stop_reason=%s); it will resume on a later run",
                            region.slug,
                            stop_reason or "missing",
                        )
                    _touch(state, clock)
                    save_queue_state(state_dir, states, clock)
                    break

                if exit_code == CHALLENGE_EXIT_CODE:
                    total_challenges += 1
                    region_challenges += 1
                    _touch(state, clock)
                    save_queue_state(state_dir, states, clock)
                    if region_challenges >= limits.max_consecutive_challenges:
                        state.status = "challenged"
                        _touch(state, clock)
                        save_queue_state(state_dir, states, clock)
                        LOGGER.error(
                            "%s hit %s consecutive challenge(s); marking it "
                            "challenged and continuing with the next region",
                            region.slug,
                            region_challenges,
                        )
                        break
                    wait_seconds = random.uniform(*limits.cooldown_seconds)
                    state.challenge_waits += 1
                    _touch(state, clock)
                    save_queue_state(state_dir, states, clock)
                    LOGGER.warning(
                        "Challenge on %s (exit 3); waiting %.0f seconds before resuming",
                        region.slug,
                        wait_seconds,
                    )
                    sleep(wait_seconds)
                    continue

                error_attempts += 1
                _touch(state, clock)
                save_queue_state(state_dir, states, clock)
                if stall_killed:
                    if error_attempts >= limits.error_max_attempts:
                        LOGGER.error(
                            "stall kill counted as attempt %s/%s for %s; region "
                            "marked failed and skipped until --retry-failed or "
                            "--reopen %s",
                            error_attempts,
                            limits.error_max_attempts,
                            region.slug,
                            region.slug,
                        )
                    else:
                        LOGGER.warning(
                            "stall kill counted as attempt %s/%s for %s; region "
                            "will retry; after %s attempts it is marked failed "
                            "and skipped until --retry-failed or --reopen %s",
                            error_attempts,
                            limits.error_max_attempts,
                            region.slug,
                            limits.error_max_attempts,
                            region.slug,
                        )
                if error_attempts >= limits.error_max_attempts:
                    state.status = "failed"
                    _touch(state, clock)
                    save_queue_state(state_dir, states, clock)
                    LOGGER.error(
                        "%s failed after %s non-challenge error(s) (last exit %s); "
                        "continuing with the next region",
                        region.slug,
                        error_attempts,
                        exit_code,
                    )
                    break
                LOGGER.warning(
                    "%s exited %s (attempt %s/%s); retrying in %.0f seconds",
                    region.slug,
                    exit_code,
                    error_attempts,
                    limits.error_max_attempts,
                    limits.error_backoff_seconds,
                )
                sleep(limits.error_backoff_seconds)

            if total_challenges >= limits.max_total_challenges:
                LOGGER.error(
                    "Stopping queue at a region boundary: %s total challenge(s) "
                    "reached limit %s (%s settled as %s)",
                    total_challenges,
                    limits.max_total_challenges,
                    region.slug,
                    state.status,
                )
                stopped_by_challenge = True
                break
    except QueueInterrupted as exc:
        interrupted_signum = exc.signum
        LOGGER.warning("Interrupted (%s); saving state and stopping", exc)
    except KeyboardInterrupt:
        interrupted_signum = signal.SIGINT
        LOGGER.warning("Interrupted (KeyboardInterrupt); saving state and stopping")

    region_states = {region.slug: states[region.slug] for region in region_list}
    interrupted = interrupted_signum is not None
    any_challenged = any(state.status == "challenged" for state in region_states.values())
    if interrupted_signum is not None:
        exit_code = 128 + interrupted_signum
    elif stopped_by_challenge or any_challenged:
        exit_code = EXIT_CHALLENGE_STOP
    elif any(state.status == "failed" for state in region_states.values()):
        exit_code = EXIT_FAILED_REGIONS
    else:
        exit_code = EXIT_OK

    result = QueueResult(
        exit_code=exit_code,
        regions=region_states,
        stopped_by_challenge=stopped_by_challenge,
        interrupted=interrupted,
    )
    finished_at = _iso_now(clock)
    save_queue_state(state_dir, states, clock)
    _atomic_write_json(
        state_dir / SUMMARY_FILENAME,
        build_summary(result, started_at, finished_at),
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def default_base_config(module_dir: str | Path | None = None) -> Path:
    """``config.json`` next to this module when present, else its example.

    Resolved relative to the module directory (not the working directory) so
    the entry point behaves the same from any CWD.
    """
    base = Path(module_dir) if module_dir is not None else Path(__file__).resolve().parent
    local = base / "config.json"
    return local if local.exists() else base / "config.example.json"


def command_for_region(scraper_command: str, log_level: str, config_path: Path) -> list[str]:
    argv = shlex.split(scraper_command)
    if not argv:
        raise ConfigError("--scraper-command must not be empty")
    return [*argv, "--config", str(config_path), "--log-level", log_level]


def _stall_clock() -> float:
    """Clock that counts system sleep where the platform offers one.

    CLOCK_BOOTTIME (Linux) and Darwin's CLOCK_MONOTONIC include suspend time,
    so a wake after a long sleep immediately shows up as a large silence gap.
    Falls back to ``time.monotonic()`` where only a sleep-excluding clock
    exists; there a wake is detected after up to the timeout of extra awake
    silence.
    """
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    if sys.platform == "darwin":
        return time.clock_gettime(time.CLOCK_MONOTONIC)
    return time.monotonic()


# No scraper-side CDP liveness check is used to complement this watchdog:
# Tab.closed and the websocket state do not reflect a dead peer (they stayed
# OPEN after the browser was killed) and pending CDP transactions never fail,
# so mid-call hangs remain possible; the queue-side watchdog is the recovery
# layer.
class _OutputForwarder(threading.Thread):
    """Forward one child stream to the queue's matching stream.

    The stream is read in raw chunks (not lines), so output that never sends a
    newline still counts as activity, while complete lines are forwarded
    verbatim and flushed as they arrive. Every raw read refreshes the shared
    last-output clock the stall watchdog reads. Daemon + joined after the
    child exits; a pipe-holding grandchild can delay that join by the bounded
    timeout but never blocks queue exit.
    """

    def __init__(
        self,
        stream: TextIO,
        target: TextIO,
        on_output: Callable[[], None],
    ) -> None:
        super().__init__(name="scraper-output-forwarder", daemon=True)
        self._stream = stream
        self._target = target
        self._on_output = on_output

    def run(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = os.read(self._stream.fileno(), STALL_OUTPUT_CHUNK_BYTES)
                if not chunk:
                    break
                self._on_output()
                text = decoder.decode(chunk)
                if text:
                    self._target.write(text)
                    self._target.flush()
            tail = decoder.decode(b"", final=True)
            if tail:
                self._target.write(tail)
                self._target.flush()
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):
                self._stream.close()


def _stall_exceeded(last_output_at: float, now: float, timeout_seconds: float) -> bool:
    """True when the child has been silent past the timeout; 0 disables."""
    return timeout_seconds > 0 and (now - last_output_at) > timeout_seconds


def _terminate_process(
    process: subprocess.Popen,
    grace_seconds: float = STALL_TERMINATE_GRACE_SECONDS,
) -> None:
    """SIGTERM a child, escalating to SIGKILL if it outlives the grace period.

    Tolerates the child exiting at any point in the sequence, including
    before the first signal.
    """
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        return
    # Reap the killed child so the caller observes its exit code and no
    # zombie is left behind.
    with contextlib.suppress(OSError):
        process.wait()


def _run_with_stall_watchdog(
    command: Sequence[str],
    stall_timeout_seconds: float,
    *,
    clock: Callable[[], float] = _stall_clock,
    sleep: Callable[[float], None] = time.sleep,
    grace_seconds: float = STALL_TERMINATE_GRACE_SECONDS,
    poll_seconds: float = STALL_POLL_SECONDS,
    on_stall: Callable[[float], None] | None = None,
) -> int:
    """Run one scraper child with live output and a silence watchdog.

    The child's stdout/stderr are piped and forwarded to the queue's own
    streams as they arrive, so scraper log formatting and interleaving are
    preserved and nothing waits for end-of-run. When no output arrives for
    longer than ``stall_timeout_seconds`` (0 disables), the child is SIGTERM'd
    -- SIGKILL'd after ``grace_seconds`` -- and its exit code is returned so
    the caller's ordinary failure/retry path takes over; checkpoint resume
    makes the mid-region kill safe. The watchdog reads ``clock``, which counts
    system sleep where the platform has a sleep-inclusive clock (see
    :func:`_stall_clock`), so a wake after a long sleep is caught at the first
    poll. ``on_stall`` receives the silent seconds when a kill is triggered.
    """
    process: subprocess.Popen | None = None
    readers: list[_OutputForwarder] = []
    last_output_at = clock()

    def note_output() -> None:
        nonlocal last_output_at
        last_output_at = clock()

    try:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        readers.append(_OutputForwarder(process.stdout, sys.stdout, note_output))
        readers.append(_OutputForwarder(process.stderr, sys.stderr, note_output))
        for reader in readers:
            reader.start()

        while True:
            if process.poll() is not None:
                break
            now = clock()
            if _stall_exceeded(last_output_at, now, stall_timeout_seconds):
                stall_seconds = now - last_output_at
                LOGGER.warning(
                    "stall detected: no output for %.0fs from child pid %s; "
                    "terminating so the region can retry",
                    stall_seconds,
                    process.pid,
                )
                if on_stall is not None:
                    on_stall(stall_seconds)
                _terminate_process(process, grace_seconds=grace_seconds)
                break
            sleep(poll_seconds)
        return int(process.wait())
    except BaseException:
        # Mirror subprocess.run: never leave an orphaned scraper behind when
        # spawning or reader setup is interrupted.
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        for reader in readers:
            # A thread whose start() raised was never registered; joining it
            # would be an error.
            if reader.ident is not None:
                reader.join(timeout=STALL_READER_JOIN_SECONDS)


class _ScraperRunner:
    """Production ``run_region``: one scraper child per call.

    ``stalled`` is True when the last call ended with a watchdog kill, so the
    queue can report how that kill counts against the attempt budget. Injected
    test callables lack the attribute; the queue reads it with
    ``getattr(..., False)``.
    """

    def __init__(self, scraper_command: str, log_level: str, stall_timeout_seconds: float) -> None:
        self._scraper_command = scraper_command
        self._log_level = log_level
        self._stall_timeout_seconds = stall_timeout_seconds
        self.stalled = False

    def _note_stall(self, _seconds: float) -> None:
        self.stalled = True

    def __call__(self, config_path: Path) -> int:
        command = command_for_region(self._scraper_command, self._log_level, config_path)
        LOGGER.info("Running scraper: %s", shlex.join(command))
        self.stalled = False
        return _run_with_stall_watchdog(
            command,
            self._stall_timeout_seconds,
            on_stall=self._note_stall,
        )


def subprocess_region_runner(
    scraper_command: str,
    log_level: str,
    stall_timeout_seconds: float = DEFAULT_STALL_TIMEOUT_MINUTES * 60.0,
) -> _ScraperRunner:
    """Build the production ``run_region`` that shells out to the scraper."""
    return _ScraperRunner(scraper_command, log_level, stall_timeout_seconds)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="property-ontario",
        description=(
            "Run the Zillow scraper across Ontario regions, waiting out "
            "human-verification challenges and resuming from checkpoints."
        ),
    )
    parser.add_argument(
        "--base-config",
        type=Path,
        default=None,
        help="Base scraper config with {city}/{state} placeholders "
        "(default: config.json if present, else config.example.json)",
    )
    parser.add_argument(
        "--regions",
        type=Path,
        default=DEFAULT_REGIONS_PATH,
        help="Regions JSON file (default: regions.ontario.json next to this module)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("data/regions"),
        help="Directory for per-region configs and progress state (default: data/regions)",
    )
    parser.add_argument(
        "--scraper-command",
        default=None,
        help="Scraper command line, parsed with shlex (default: '<python> -m property_scraper')",
    )
    parser.add_argument("--only", default=None, help="Comma-separated region slugs to run")
    parser.add_argument("--skip", default=None, help="Comma-separated region slugs to skip")
    parser.add_argument(
        "--cooldown-min-minutes",
        type=float,
        default=30.0,
        help="Minimum cooldown after a challenge (default: 30)",
    )
    parser.add_argument(
        "--cooldown-max-minutes",
        type=float,
        default=60.0,
        help="Maximum cooldown after a challenge (default: 60)",
    )
    parser.add_argument(
        "--max-consecutive-challenges",
        type=int,
        default=3,
        help="Mark a region challenged after this many consecutive challenges, "
        "then continue with the next region (default: 3)",
    )
    parser.add_argument(
        "--max-total-challenges",
        type=int,
        default=6,
        help="Stop the whole queue after this many challenges in one invocation; "
        "checked between regions and must be >= --max-consecutive-challenges "
        "(default: 6)",
    )
    parser.add_argument(
        "--error-max-attempts",
        type=int,
        default=2,
        help="Attempts allowed for non-challenge failures before marking a region failed "
        "(default: 2)",
    )
    parser.add_argument(
        "--stall-timeout-minutes",
        type=float,
        default=DEFAULT_STALL_TIMEOUT_MINUTES,
        help="Terminate a scraper child that produces no output for this many "
        "minutes so the region can retry (0 disables; default: 10). The "
        "platform's sleep-inclusive clock is used where available, so time "
        "spent asleep counts and a longer sleep is detected at the first poll "
        "after wake.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Reset failed regions to pending for this run",
    )
    parser.add_argument(
        "--reopen",
        default=None,
        help="Comma-separated region slugs to reset to pending before this run "
        "(any status, including done/failed/challenged)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate, generate region configs, print commands, and run nothing",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    return parser.parse_args(argv)


def _split_slugs(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [token.strip() for token in value.split(",") if token.strip()]


def _install_signal_handlers() -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def handler(signum: int, frame: Any) -> None:
        raise QueueInterrupted(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, handler)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            continue
    return previous


def _restore_signal_handlers(previous: Mapping[int, Any]) -> None:
    for signum, original in previous.items():
        try:
            signal.signal(signum, original)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            continue


def print_summary(result: QueueResult, state_dir: Path) -> None:
    print(
        "Ontario pull summary: "
        f"{len(result.done)} done, {len(result.partial)} partial, "
        f"{len(result.challenged)} challenged, {len(result.failed)} failed, "
        f"{len(result.pending)} pending "
        f"(exit {result.exit_code})"
    )
    for slug in sorted(result.regions):
        state = result.regions[slug]
        print(
            f"  {state.status.upper():8} {slug} "
            f"(attempts={state.attempts}, challenge_waits={state.challenge_waits}, "
            f"last_exit={state.last_exit_code}, stop_reason={state.last_stop_reason})"
        )
    if result.stopped_by_challenge:
        print("  Stopped by the total challenge bound; resume later to continue.")
    elif result.challenged:
        print("  Some regions were left challenged; re-run to resume them.")
    if result.interrupted:
        print("  Interrupted; progress saved. Re-run to resume.")
    print(f"  State: {state_dir / STATE_FILENAME}")
    print(f"  Summary: {state_dir / SUMMARY_FILENAME}")


def _dry_run(
    selected: Sequence[Region],
    base_config_path: Path,
    state_dir: Path,
    scraper_command: str,
    log_level: str,
    stall_timeout_seconds: float = 0.0,
) -> int:
    try:
        base_config = load_base_config(base_config_path)
        for region in selected:
            config_path = build_region_config(base_config, region, state_dir, stall_timeout_seconds)
            print(f"region={region.slug} config={config_path}")
            print("  " + shlex.join(command_for_region(scraper_command, log_level, config_path)))
    except ConfigError as exc:
        LOGGER.error("Configuration error: %s", exc)
        return EXIT_CONFIG_ERROR
    print(f"Dry run: generated {len(selected)} region config(s); nothing executed.")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        regions = load_regions_file(args.regions)
        selected = select_regions(regions, _split_slugs(args.only), _split_slugs(args.skip))
        if not selected:
            raise ConfigError("no regions selected after applying --only/--skip")
        reopen_tokens = _split_slugs(args.reopen) or []
        if reopen_tokens:
            known = known_region_tokens(regions)
            unknown = [token for token in reopen_tokens if token not in known]
            if unknown:
                raise ConfigError(
                    f"--reopen has unknown region slug(s): {', '.join(unknown)}; "
                    f"known slugs: {', '.join(sorted(known))}"
                )
        base_config_path = args.base_config if args.base_config else default_base_config()
        scraper_command = args.scraper_command or f"{sys.executable} -m property_scraper"
        limits = QueueLimits(
            cooldown_min_minutes=args.cooldown_min_minutes,
            cooldown_max_minutes=args.cooldown_max_minutes,
            max_consecutive_challenges=args.max_consecutive_challenges,
            max_total_challenges=args.max_total_challenges,
            error_max_attempts=args.error_max_attempts,
            stall_timeout_minutes=args.stall_timeout_minutes,
            retry_failed=args.retry_failed,
            reopen=frozenset(reopen_tokens),
        )
    except (ConfigError, ValueError) as exc:
        LOGGER.error("Configuration error: %s", exc)
        return EXIT_CONFIG_ERROR

    if args.dry_run:
        return _dry_run(
            selected,
            base_config_path,
            args.state_dir,
            scraper_command,
            args.log_level,
            limits.stall_timeout_seconds,
        )

    run_region = subprocess_region_runner(
        scraper_command, args.log_level, limits.stall_timeout_seconds
    )
    previous_handlers = _install_signal_handlers()
    try:
        result = run_queue(
            selected,
            base_config_path,
            args.state_dir,
            run_region,
            time.sleep,
            limits=limits,
        )
    except ConfigError as exc:
        LOGGER.error("Configuration error: %s", exc)
        return EXIT_CONFIG_ERROR
    finally:
        _restore_signal_handlers(previous_handlers)

    print_summary(result, Path(args.state_dir))
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
