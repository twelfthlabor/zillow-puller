#!/usr/bin/env python3
"""Merge per-region scraper CSVs into one deduped, region-tagged research CSV.

Reads ``<state-dir>/<region>/listings.csv`` for every immediate subdirectory of
``--state-dir`` (a ``logs/`` directory is skipped silently — it holds wrapper
logs, not listings) and writes a single CSV ordered by
``(region, listing_id/url)`` so reruns are byte-stable.

Dedup semantics: exactly one row per listing across the whole run, keyed by
``field_utils.normalize_record_key`` (listing id first, normalized URL as the
fallback). A boundary listing that surfaces in two region queries — or the
same page reached via two URL spellings — collapses to one row, so the
``region`` column names the region of the newest copy, not every region that
saw it.

* rows without a listing id or URL are skipped and counted;
* when both copies carry a parseable ``scraped_at``, the newest instant wins
  (timezone-aware comparison; naive timestamps are treated as UTC);
* when either ``scraped_at`` is missing or unparseable there is nothing to
  compare, so the later-encountered row wins — files are visited in sorted
  region order, rows in file order — and the fallback is counted in the
  summary.

Safety guard: when no region CSV is readable (state dir missing/moved, or
every file empty/unreadable), an existing populated ``--output`` is left
byte-for-byte untouched and the CLI exits non-zero unless ``--allow-empty``
is passed.

The export is atomic: rows go to a sibling temp file and are moved into place
with ``os.replace``. Only stdlib data modules are imported — no Selenium, no
browser, no network.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from field_utils import normalize_record_key

LOGGER = logging.getLogger("merge_listings")

LISTINGS_FILENAME = "listings.csv"
LOGS_DIRNAME = "logs"
DEFAULT_STATE_DIR = Path("data/regions")
DEFAULT_OUTPUT = Path("data/ontario-listings.csv")
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Shape the scraper writes; used when no input file contributes a header.
DEFAULT_FIELDS = [
    "listing_id",
    "region",
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


class EmptyOverwriteRefused(RuntimeError):
    """Raised when a merge would replace a populated export with no data."""


@dataclass(frozen=True)
class MergeStats:
    """Counters and per-region results for one merge run."""

    state_dir: Path
    output_path: Path
    files_read: int
    rows_read: int
    unique_rows: int
    duplicates_dropped: int
    rows_skipped_no_key: int
    fallback_decisions: int
    skipped_files: tuple[str, ...]
    skipped_by_reason: dict[str, int] = field(default_factory=dict)
    region_rows_read: dict[str, int] = field(default_factory=dict)
    region_rows_written: dict[str, int] = field(default_factory=dict)


@dataclass
class _Candidate:
    """A row competing for a dedup key; kept only if it wins the comparison."""

    key: str
    region: str
    row: dict[str, str]
    timestamp: datetime | None
    sequence: int


def _parse_scraped_at(value: object) -> datetime | None:
    """Parse an ISO-8601 ``scraped_at`` into a timezone-aware instant.

    Naive timestamps are treated as UTC so mixed-format copies still compare
    by instant rather than by string shape. Returns ``None`` when the value
    is missing or unparseable; callers then fall back to encounter order.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a listings CSV fully; malformed files raise before any row is used.

    Reading eagerly keeps a bad file atomic: a ``csv.Error`` halfway through
    cannot leave half of that file's rows in the merge.
    """
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, restval="")
        rows = [{name: value for name, value in row.items() if name is not None} for row in reader]
        return list(reader.fieldnames or []), rows


def _ordered_fieldnames(discovered: list[str]) -> list[str]:
    """Union of input columns in first-seen order, with ``region`` after id."""
    ordered: list[str] = []
    for name in discovered:
        if name and name not in ordered:
            ordered.append(name)
    rest = [name for name in ordered if name not in ("listing_id", "region")]
    if "listing_id" in ordered:
        return ["listing_id", "region", *rest]
    return ["region", *rest]


def _skip_reason(path: Path) -> str | None:
    """Why a ``listings.csv`` cannot be read, or ``None`` when it is usable."""
    try:
        if not path.exists():
            return "missing"
        if not path.is_file():
            return "not a regular file"
        if path.stat().st_size == 0:
            return "empty"
    except OSError as exc:
        return f"unreadable ({exc})"
    return None


def _skip_category(reason: str) -> str:
    """Collapse a detailed skip reason into a summary bucket."""
    if reason == "missing":
        return "missing"
    if reason == "empty":
        return "empty"
    return "unreadable"


def _existing_output_has_rows(path: Path) -> bool:
    """True when an existing output holds data that an empty run must not clobber.

    A missing, empty, or header-only file is safe to replace. A file that
    cannot be parsed is treated as populated — when in doubt, do not destroy.
    """
    if not path.exists():
        return False
    try:
        if path.stat().st_size == 0:
            return False
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, restval="")
            return next(reader, None) is not None
    except (OSError, UnicodeDecodeError, csv.Error):
        return True


def _region_dirs(state_dir: Path) -> list[Path]:
    """Immediate subdirectories of the state dir, name-sorted for determinism.

    ``logs/`` is not a region; it holds wrapper logs and is skipped silently.
    """
    try:
        return sorted(
            (
                entry
                for entry in state_dir.iterdir()
                if entry.is_dir() and not entry.name.startswith(".") and entry.name != LOGS_DIRNAME
            ),
            key=lambda entry: entry.name,
        )
    except OSError as exc:
        LOGGER.warning("Cannot scan state dir %s: %s", state_dir, exc)
        return []


def merge_listings(
    state_dir: Path | str, output: Path | str, allow_empty: bool = False
) -> MergeStats:
    """Merge every readable region CSV under ``state_dir`` into ``output``.

    One bad region file is skipped with a warning; it never aborts the run.
    Raises ``EmptyOverwriteRefused`` when nothing was readable and the
    existing output is populated, unless ``allow_empty`` is set. Returns
    counters for the caller to print and for tests to assert on.
    """
    state_dir = Path(state_dir)
    output = Path(output)

    files_read = 0
    rows_read = 0
    rows_skipped_no_key = 0
    fallback_decisions = 0
    skipped_files: list[str] = []
    skipped_by_reason: dict[str, int] = {}
    region_rows_read: dict[str, int] = {}
    discovered: list[str] = []
    best: dict[str, _Candidate] = {}
    sequence = 0

    for region_dir in _region_dirs(state_dir):
        region = region_dir.name
        csv_path = region_dir / LISTINGS_FILENAME
        reason = _skip_reason(csv_path)
        if reason is not None:
            category = _skip_category(reason)
            LOGGER.warning("Skipping %s: %s", csv_path, reason)
            skipped_files.append(f"{csv_path}: {reason}")
            skipped_by_reason[category] = skipped_by_reason.get(category, 0) + 1
            continue

        try:
            header, rows = _read_csv(csv_path)
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            LOGGER.warning("Skipping unreadable %s: %s", csv_path, exc)
            skipped_files.append(f"{csv_path}: unreadable ({exc})")
            skipped_by_reason["unreadable"] = skipped_by_reason.get("unreadable", 0) + 1
            continue

        files_read += 1
        for name in header:
            if name and name not in discovered:
                discovered.append(name)
        region_rows_read[region] = region_rows_read.get(region, 0) + len(rows)
        rows_read += len(rows)

        for row in rows:
            sequence += 1
            for name in row:
                if name not in discovered:
                    discovered.append(name)
            key = normalize_record_key(str(row.get("listing_id") or ""), str(row.get("url") or ""))
            if not key:
                rows_skipped_no_key += 1
                LOGGER.debug("Skipping row without listing_id or URL in %s", csv_path)
                continue

            timestamp = _parse_scraped_at(row.get("scraped_at"))
            candidate = _Candidate(key, region, row, timestamp, sequence)
            current = best.get(key)
            if current is None:
                best[key] = candidate
                continue

            if current.timestamp is not None and candidate.timestamp is not None:
                if candidate.timestamp >= current.timestamp:
                    best[key] = candidate
                continue

            fallback_decisions += 1
            LOGGER.debug(
                "No comparable scraped_at for %s; keeping later row %d from %s",
                key,
                sequence,
                region,
            )
            best[key] = candidate

    kept = sorted(best.values(), key=lambda item: (item.region, item.key))
    fieldnames = _ordered_fieldnames(discovered) if discovered else list(DEFAULT_FIELDS)

    region_rows_written: dict[str, int] = {}
    for candidate in kept:
        region_rows_written[candidate.region] = region_rows_written.get(candidate.region, 0) + 1

    if files_read == 0 and not allow_empty and _existing_output_has_rows(output):
        raise EmptyOverwriteRefused(
            f"refusing to overwrite {output}: no readable region CSV found under "
            f"{state_dir} (pass --allow-empty to write an empty export)"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output.with_name(f"{output.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=fieldnames, restval="", extrasaction="ignore"
            )
            writer.writeheader()
            for candidate in kept:
                record = dict(candidate.row)
                record["region"] = candidate.region
                writer.writerow(record)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, output)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return MergeStats(
        state_dir=state_dir,
        output_path=output,
        files_read=files_read,
        rows_read=rows_read,
        unique_rows=len(kept),
        duplicates_dropped=rows_read - rows_skipped_no_key - len(kept),
        rows_skipped_no_key=rows_skipped_no_key,
        fallback_decisions=fallback_decisions,
        skipped_files=tuple(skipped_files),
        skipped_by_reason=skipped_by_reason,
        region_rows_read=region_rows_read,
        region_rows_written=region_rows_written,
    )


def format_summary(stats: MergeStats) -> str:
    """Render the human-readable run summary printed by the CLI."""
    lines = [
        "Merge summary",
        f"  state dir: {stats.state_dir}",
        f"  output: {stats.output_path}",
        f"  files read: {stats.files_read}",
        f"  rows read: {stats.rows_read}",
        f"  unique rows written: {stats.unique_rows}",
        f"  duplicates dropped: {stats.duplicates_dropped}",
        f"  rows skipped (no key): {stats.rows_skipped_no_key}",
        f"  scraped_at fallback decisions: {stats.fallback_decisions}",
        f"  skipped files: {len(stats.skipped_files)} "
        f"(missing={stats.skipped_by_reason.get('missing', 0)}, "
        f"unreadable={stats.skipped_by_reason.get('unreadable', 0)}, "
        f"empty={stats.skipped_by_reason.get('empty', 0)})",
        "  per-region counts (read -> written):",
    ]
    regions = sorted(set(stats.region_rows_read) | set(stats.region_rows_written))
    if not regions:
        lines.append("    (none)")
    for region in regions:
        lines.append(
            f"    {region}: "
            f"{stats.region_rows_read.get(region, 0)} -> "
            f"{stats.region_rows_written.get(region, 0)}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """CLI contract for the merge export."""
    parser = argparse.ArgumentParser(
        description="Merge per-region listings.csv files into one deduped CSV.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"Region state directory (default: {DEFAULT_STATE_DIR}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Merged CSV destination (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=LOG_LEVELS,
        default="INFO",
        help="Logging verbosity (default: INFO).",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help=(
            "Allow replacing an existing populated output with a header-only "
            "file when no region CSV is readable."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse args, merge, print the summary. Returns exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s: %(message)s",
    )
    try:
        stats = merge_listings(args.state_dir, args.output, allow_empty=args.allow_empty)
    except EmptyOverwriteRefused as exc:
        LOGGER.error("%s", exc)
        return 1
    print(format_summary(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
