"""Tests for ``scripts/merge_listings.py`` — local merge/dedup export."""

from __future__ import annotations

import csv
import importlib.util
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MERGE_SCRIPT = REPO_ROOT / "scripts" / "merge_listings.py"

FIELDS = [
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

TS_EARLY = "2026-01-01T00:00:00+00:00"
TS_LATE = "2026-02-01T00:00:00+00:00"


def _load_merge_module():
    spec = importlib.util.spec_from_file_location("merge_listings", MERGE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses can resolve this module's annotations.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


merge = _load_merge_module()


def make_row(
    listing_id: str = "",
    url: str = "",
    address: str = "",
    scraped_at: str = "",
) -> dict[str, str]:
    return {
        "listing_id": listing_id,
        "address": address,
        "price": "1000000",
        "beds": "3",
        "baths": "2",
        "sqft": "",
        "agent": "",
        "url": url,
        "source_page": "https://www.zillow.com/toronto-on/",
        "scraped_at": scraped_at,
    }


def write_region(
    state_dir: Path,
    region: str,
    rows: list[dict[str, str]],
    fieldnames: list[str] | None = None,
) -> Path:
    region_dir = state_dir / region
    region_dir.mkdir(parents=True, exist_ok=True)
    path = region_dir / "listings.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fieldnames or FIELDS, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


def read_output(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def test_region_column_follows_listing_id(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_region(
        state,
        "toronto-on",
        [make_row("111", "https://example.test/1", scraped_at=TS_EARLY)],
    )

    stats = merge.merge_listings(state, tmp_path / "out.csv")

    header, rows = read_output(tmp_path / "out.csv")
    assert header[:2] == ["listing_id", "region"]
    assert rows[0]["region"] == "toronto-on"
    assert stats.unique_rows == 1
    assert stats.files_read == 1
    assert stats.rows_read == 1
    assert stats.duplicates_dropped == 0
    assert stats.fallback_decisions == 0


def test_dedup_across_two_regions_prefers_newer_scraped_at(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    # Same listing id in two regions; Toronto's copy is newer.
    write_region(
        state,
        "toronto-on",
        [make_row("111", "https://example.test/1", address="Toronto", scraped_at=TS_LATE)],
    )
    write_region(
        state,
        "ottawa-on",
        [make_row("111", "https://example.test/1", address="Ottawa", scraped_at=TS_EARLY)],
    )

    stats = merge.merge_listings(state, tmp_path / "out.csv")

    _, rows = read_output(tmp_path / "out.csv")
    assert len(rows) == 1
    assert rows[0]["region"] == "toronto-on"
    assert rows[0]["address"] == "Toronto"
    assert stats.unique_rows == 1
    assert stats.rows_read == 2
    assert stats.duplicates_dropped == 1
    assert stats.fallback_decisions == 0


def test_newest_row_in_second_file_wins(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_region(
        state,
        "ajax-on",
        [make_row("222", "https://example.test/2", address="Ajax", scraped_at=TS_EARLY)],
    )
    write_region(
        state,
        "barrie-on",
        [make_row("222", "https://example.test/2", address="Barrie", scraped_at=TS_LATE)],
    )

    merge.merge_listings(state, tmp_path / "out.csv")

    _, rows = read_output(tmp_path / "out.csv")
    assert len(rows) == 1
    assert rows[0]["region"] == "barrie-on"
    assert rows[0]["address"] == "Barrie"


def test_missing_or_blank_scraped_at_falls_back_to_later_row(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    # "ottawa-on" sorts before "toronto-on", so ottawa is the earlier file.
    write_region(
        state,
        "ottawa-on",
        [
            make_row("1", "https://example.test/1", address="valid-first", scraped_at=TS_EARLY),
            make_row("2", "https://example.test/2", address="blank-first"),
            make_row("3", "https://example.test/3", address="junk-first", scraped_at="not-a-date"),
        ],
    )
    write_region(
        state,
        "toronto-on",
        [
            make_row("1", "https://example.test/1", address="blank-later"),
            make_row("2", "https://example.test/2", address="valid-later", scraped_at=TS_LATE),
            make_row("3", "https://example.test/3", address="valid-later-3", scraped_at=TS_LATE),
        ],
    )

    stats = merge.merge_listings(state, tmp_path / "out.csv")

    _, rows = read_output(tmp_path / "out.csv")
    by_id = {row["listing_id"]: row for row in rows}
    # A later row with no comparable timestamp replaces an earlier valid one
    # (comparison is impossible, so encounter order decides).
    assert by_id["1"]["address"] == "blank-later"
    # Earlier blank / unparseable, later valid: the later row wins too.
    assert by_id["2"]["address"] == "valid-later"
    assert by_id["3"]["address"] == "valid-later-3"
    assert stats.unique_rows == 3
    assert stats.fallback_decisions == 3


def test_timezone_offsets_compare_by_instant(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    # "ottawa-on" sorts first, so its rows are the earlier-encountered copies.
    write_region(
        state,
        "ottawa-on",
        [
            # 23:00-05:00 is 04:00Z the next day: the later instant, even though
            # its string sorts before the +00:00 copy below.
            make_row(
                "1",
                "https://example.test/1",
                address="offset-later",
                scraped_at="2026-01-01T23:00:00-05:00",
            ),
            # A naive timestamp is treated as UTC, so 23:00Z beats 20:00Z.
            make_row(
                "2",
                "https://example.test/2",
                address="naive-later",
                scraped_at="2026-01-01T23:00:00",
            ),
        ],
    )
    write_region(
        state,
        "toronto-on",
        [
            make_row(
                "1",
                "https://example.test/1",
                address="plain-earlier",
                scraped_at="2026-01-02T00:00:00+00:00",
            ),
            make_row(
                "2",
                "https://example.test/2",
                address="aware-earlier",
                scraped_at="2026-01-01T20:00:00+00:00",
            ),
        ],
    )

    stats = merge.merge_listings(state, tmp_path / "out.csv")

    _, rows = read_output(tmp_path / "out.csv")
    by_id = {row["listing_id"]: row for row in rows}
    assert by_id["1"]["address"] == "offset-later"
    assert by_id["2"]["address"] == "naive-later"
    assert stats.fallback_decisions == 0


def test_logs_directory_is_ignored_silently(tmp_path: Path, caplog) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_region(state, "toronto-on", [make_row("1", "https://example.test/1")])
    # A logs/ dir with its own listings.csv must never be treated as a region
    # (and must not warn about the wrapper-log directory).
    write_region(state, "logs", [make_row("shadow", "https://example.test/shadow")])

    with caplog.at_level(logging.WARNING, logger="merge_listings"):
        stats = merge.merge_listings(state, tmp_path / "out.csv")

    assert stats.files_read == 1
    assert stats.unique_rows == 1
    assert stats.skipped_files == ()
    assert "logs" not in caplog.text

    _, rows = read_output(tmp_path / "out.csv")
    assert [row["listing_id"] for row in rows] == ["1"]


def test_bad_files_are_skipped_without_failing_the_run(
    tmp_path: Path, caplog
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_region(state, "good-on", [make_row("1", "https://example.test/1")])

    # Region with no listings.csv at all.
    (state / "missing-on").mkdir()

    empty = write_region(state, "empty-on", [])
    empty.write_bytes(b"")

    not_a_file = state / "dir-on" / "listings.csv"
    not_a_file.mkdir(parents=True)

    bad_utf8 = state / "bad-utf8-on" / "listings.csv"
    bad_utf8.parent.mkdir(parents=True)
    bad_utf8.write_bytes(b"listing_id,url\n\xff\xfe,https://example.test/x\n")

    oversized = state / "oversized-on" / "listings.csv"
    oversized.parent.mkdir(parents=True)
    huge = "x" * (csv.field_size_limit() + 10)
    oversized.write_text(f"listing_id,url\n{huge},https://example.test/y\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="merge_listings"):
        stats = merge.merge_listings(state, tmp_path / "out.csv")

    assert stats.files_read == 1
    assert stats.rows_read == 1
    assert stats.unique_rows == 1
    assert len(stats.skipped_files) == 5
    assert stats.skipped_by_reason == {"missing": 1, "empty": 1, "unreadable": 3}
    joined = "\n".join(stats.skipped_files)
    assert "missing" in joined
    assert "empty" in joined
    assert "not a regular file" in joined
    assert "unreadable" in joined
    assert "Skipping" in caplog.text

    summary = merge.format_summary(stats)
    assert "skipped files: 5 (missing=1, unreadable=3, empty=1)" in summary

    _, rows = read_output(tmp_path / "out.csv")
    assert [row["listing_id"] for row in rows] == ["1"]


def test_empty_state_dir_reports_zero(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    output = tmp_path / "out.csv"

    stats = merge.merge_listings(state, output)
    summary = merge.format_summary(stats)

    assert stats.files_read == 0
    assert stats.rows_read == 0
    assert stats.unique_rows == 0
    assert stats.duplicates_dropped == 0
    assert stats.rows_skipped_no_key == 0
    assert "unique rows written: 0" in summary
    assert "rows read: 0" in summary
    header, rows = read_output(output)
    assert rows == []
    assert header[:2] == ["listing_id", "region"]


def test_missing_state_dir_reports_zero(tmp_path: Path) -> None:
    stats = merge.merge_listings(tmp_path / "does-not-exist", tmp_path / "out.csv")

    assert stats.files_read == 0
    assert stats.unique_rows == 0
    assert "unique rows written: 0" in merge.format_summary(stats)


def test_refuses_to_replace_populated_output_without_allow_empty(
    tmp_path: Path, caplog
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_region(state, "toronto-on", [make_row("1", "https://example.test/1")])
    output = tmp_path / "out.csv"
    assert merge.main(["--state-dir", str(state), "--output", str(output)]) == 0
    before = output.read_bytes()
    assert before

    empty_state = tmp_path / "empty"
    empty_state.mkdir()

    with pytest.raises(merge.EmptyOverwriteRefused):
        merge.merge_listings(empty_state, output)
    assert output.read_bytes() == before

    with caplog.at_level(logging.ERROR, logger="merge_listings"):
        exit_code = merge.main(["--state-dir", str(empty_state), "--output", str(output)])

    assert exit_code == 1
    assert output.read_bytes() == before
    assert "refusing to overwrite" in caplog.text


def test_allow_empty_writes_header_only_output(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_region(state, "toronto-on", [make_row("1", "https://example.test/1")])
    output = tmp_path / "out.csv"
    assert merge.main(["--state-dir", str(state), "--output", str(output)]) == 0

    empty_state = tmp_path / "empty"
    empty_state.mkdir()
    exit_code = merge.main(
        ["--state-dir", str(empty_state), "--output", str(output), "--allow-empty"]
    )

    assert exit_code == 0
    header, rows = read_output(output)
    assert rows == []
    assert header[:2] == ["listing_id", "region"]


def test_fresh_output_written_when_nothing_readable(tmp_path: Path) -> None:
    state = tmp_path / "empty"
    state.mkdir()
    output = tmp_path / "fresh.csv"
    assert not output.exists()

    exit_code = merge.main(["--state-dir", str(state), "--output", str(output)])

    assert exit_code == 0
    header, rows = read_output(output)
    assert rows == []
    assert header[:2] == ["listing_id", "region"]


def test_rows_without_key_are_skipped(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_region(
        state,
        "toronto-on",
        [
            make_row("", "", address="no key"),
            make_row("", "https://example.test/url-only", address="url key"),
            make_row("", "https://example.test/URL-ONLY/", address="same url key"),
        ],
    )

    stats = merge.merge_listings(state, tmp_path / "out.csv")

    _, rows = read_output(tmp_path / "out.csv")
    assert stats.rows_skipped_no_key == 1
    assert stats.unique_rows == 1
    assert stats.duplicates_dropped == 1
    # URL keys normalize case and trailing slashes, so both URL rows collapse;
    # with no comparable scraped_at the later row's raw URL is kept.
    assert rows[0]["url"] == "https://example.test/URL-ONLY/"
    assert stats.fallback_decisions == 1


def test_output_ordering_is_region_then_key(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_region(
        state,
        "b-region-on",
        [
            make_row("9", "https://example.test/9"),
            make_row("1", "https://example.test/1"),
        ],
    )
    write_region(
        state,
        "a-region-on",
        [
            make_row("5", "https://example.test/5"),
            make_row("2", "https://example.test/2"),
        ],
    )

    merge.merge_listings(state, tmp_path / "out.csv")

    _, rows = read_output(tmp_path / "out.csv")
    assert [(row["region"], row["listing_id"]) for row in rows] == [
        ("a-region-on", "2"),
        ("a-region-on", "5"),
        ("b-region-on", "1"),
        ("b-region-on", "9"),
    ]


def test_reruns_are_byte_identical(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_region(
        state,
        "toronto-on",
        [
            make_row("2", "https://example.test/2", scraped_at=TS_EARLY),
            make_row("1", "https://example.test/1", scraped_at=TS_LATE),
        ],
    )
    write_region(
        state,
        "ottawa-on",
        [
            make_row("1", "https://example.test/1", scraped_at=TS_EARLY),
            make_row("3", "https://example.test/3"),
        ],
    )

    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    merge.merge_listings(state, first)
    merge.merge_listings(state, second)

    assert first.read_bytes() == second.read_bytes()


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_region(state, "toronto-on", [make_row("1", "https://example.test/1")])
    output = tmp_path / "nested" / "out.csv"

    merge.merge_listings(state, output)

    assert output.is_file()
    assert list(output.parent.glob("*.tmp")) == []


def test_main_prints_summary_and_returns_zero(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"
    state.mkdir()
    write_region(state, "toronto-on", [make_row("1", "https://example.test/1")])
    output = tmp_path / "out.csv"

    exit_code = merge.main(
        ["--state-dir", str(state), "--output", str(output), "--log-level", "warning"]
    )

    assert exit_code == 0
    printed = capsys.readouterr().out
    assert "unique rows written: 1" in printed
    assert "toronto-on: 1 -> 1" in printed
    assert str(output) in printed
