"""Subprocess-level end-to-end coverage for the ``property-ontario`` CLI.

``tests/test_ontario_pull.py`` exercises ``run_queue`` with injected fakes and
the CLI with a monkeypatched scraper executor. That leaves the real process
boundary untested. This module closes the gap: the console script actually
spawns ``tests/fixtures/fake_scraper.py`` (per-region behavior is selected
through environment variables, see that fixture). Covered here:

* a challenged region is retried after the cooldown until it completes, then
  skipped by a second invocation;
* a region that always challenges is marked ``challenged`` after
  ``--max-consecutive-challenges``, the queue moves on to the next region
  (which completes), the process exits 3, and ``--reopen`` later completes it;
* a silently hung child is killed by the stall watchdog and the queue
  continues, while a chatty slow child is left alone.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FAKE_SCRAPER = FIXTURES_DIR / "fake_scraper.py"
VENV_ONTARIO = REPO_ROOT / ".venv313" / "bin" / "property-ontario"


def _ontario_command() -> list[str]:
    """Prefer the installed console script; fall back to the module entry point."""
    if os.access(VENV_ONTARIO, os.X_OK):
        return [str(VENV_ONTARIO)]
    return [sys.executable, "-m", "ontario_pull"]


def _prepare_queue(
    tmp_path: Path, regions: list[dict[str, str]] | None = None
) -> tuple[Path, Path, Path]:
    """Write a base config + regions file; return paths to use."""
    base_config = tmp_path / "base-config.json"
    base_config.write_text(
        json.dumps(
            {
                "start_url": "https://www.zillow.com/homes/{city}-{state}/",
                "user_agent": "TestBot/1.0 (+mailto:test@example.com)",
                "output_csv": "listings.csv",
                "checkpoint_file": "checkpoint.json",
                "error_directory": "errors",
                "max_pages": 0,
                "pagination": {"mode": "next_button", "next_selector": "a.next"},
                "selectors": {"card": "[data-testid='property-card']"},
            }
        ),
        encoding="utf-8",
    )
    if regions is None:
        regions = [{"city": "toronto", "name": "Toronto"}]
    regions_file = tmp_path / "regions.json"
    regions_file.write_text(
        json.dumps({"province": "on", "regions": regions}),
        encoding="utf-8",
    )
    return tmp_path / "state", base_config, regions_file


def _run_ontario(
    state_dir: Path,
    base_config: Path,
    regions_file: Path,
    *extra_args: str,
    env_overrides: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [
            *_ontario_command(),
            "--base-config",
            str(base_config),
            "--regions",
            str(regions_file),
            "--state-dir",
            str(state_dir),
            "--scraper-command",
            shlex.join([sys.executable, str(FAKE_SCRAPER)]),
            "--cooldown-min-minutes",
            "0",
            "--cooldown-max-minutes",
            "0",
            "--log-level",
            "INFO",
            *extra_args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=120,
    )


def _stub_calls(state_dir: Path, slug: str = "toronto-on") -> list[dict[str, str]]:
    counter = state_dir / slug / "fake-scraper-calls.json"
    return json.loads(counter.read_text(encoding="utf-8"))


def _region_states(state_dir: Path) -> dict[str, dict[str, object]]:
    payload = json.loads((state_dir / "queue-state.json").read_text(encoding="utf-8"))
    return payload["regions"]


def _region_state(state_dir: Path, slug: str = "toronto-on") -> dict[str, object]:
    return _region_states(state_dir)[slug]


def test_cli_challenge_cooldown_retries_same_region_until_done(tmp_path: Path) -> None:
    state_dir, base_config, regions_file = _prepare_queue(tmp_path)

    result = _run_ontario(state_dir, base_config, regions_file)

    assert result.returncode == 0, result.stderr
    # The cooldown branch ran, then the same region was retried in place.
    assert "Challenge on toronto-on (exit 3); waiting 0 seconds before resuming" in result.stderr
    calls = _stub_calls(state_dir)
    expected_config = str(state_dir / "toronto-on" / "config.json")
    assert [call["config"] for call in calls] == [expected_config, expected_config]

    # The stub's terminal report is what promotes the region to done.
    report = json.loads((state_dir / "toronto-on" / "report.json").read_text(encoding="utf-8"))
    assert report["stop_reason"] == "pagination_exhausted"

    state = _region_state(state_dir)
    assert state["status"] == "done"
    assert state["attempts"] == 2
    assert state["challenge_waits"] == 1
    assert state["last_exit_code"] == 0
    assert state["last_stop_reason"] == "pagination_exhausted"

    summary = json.loads((state_dir / "last-run-summary.json").read_text(encoding="utf-8"))
    assert summary["exit_code"] == 0
    assert summary["stopped_by_challenge"] is False
    assert summary["counts"]["done"] == 1
    assert "DONE" in result.stdout


def test_second_cli_invocation_skips_done_region(tmp_path: Path) -> None:
    state_dir, base_config, regions_file = _prepare_queue(tmp_path)
    first = _run_ontario(state_dir, base_config, regions_file)
    assert first.returncode == 0, first.stderr
    assert len(_stub_calls(state_dir)) == 2

    second = _run_ontario(state_dir, base_config, regions_file)

    assert second.returncode == 0, second.stderr
    assert "Skipping toronto-on (already done)" in second.stderr
    # The stub was not run again on the second pass.
    assert len(_stub_calls(state_dir)) == 2
    assert _region_state(state_dir)["status"] == "done"
    summary = json.loads((state_dir / "last-run-summary.json").read_text(encoding="utf-8"))
    assert summary["counts"] == {
        "done": 1,
        "partial": 0,
        "failed": 0,
        "challenged": 0,
        "pending": 0,
    }


def test_cli_marks_region_challenged_moves_on_then_reopen_completes(
    tmp_path: Path,
) -> None:
    """A per-region challenge limit marks the region and continues the queue."""
    state_dir, base_config, regions_file = _prepare_queue(
        tmp_path,
        regions=[
            {"city": "toronto", "name": "Toronto"},
            {"city": "ottawa", "name": "Ottawa"},
        ],
    )

    first = _run_ontario(
        state_dir,
        base_config,
        regions_file,
        "--max-consecutive-challenges",
        "3",
        env_overrides={
            "FAKE_SCRAPER_ALWAYS_CHALLENGE": "toronto-on",
            "FAKE_SCRAPER_NEVER_CHALLENGE": "ottawa-on",
        },
    )

    assert first.returncode == 3, first.stderr
    assert "marking it challenged and continuing with the next region" in first.stderr
    # Both regions were attempted: toronto three times, ottawa once.
    toronto_config = str(state_dir / "toronto-on" / "config.json")
    assert [call["config"] for call in _stub_calls(state_dir, "toronto-on")] == [
        toronto_config,
        toronto_config,
        toronto_config,
    ]
    assert len(_stub_calls(state_dir, "ottawa-on")) == 1

    state = _region_states(state_dir)
    assert state["toronto-on"]["status"] == "challenged"
    assert state["toronto-on"]["attempts"] == 3
    assert state["toronto-on"]["challenge_waits"] == 2
    assert state["toronto-on"]["last_exit_code"] == 3
    assert state["ottawa-on"]["status"] == "done"
    assert state["ottawa-on"]["last_stop_reason"] == "pagination_exhausted"

    summary = json.loads((state_dir / "last-run-summary.json").read_text(encoding="utf-8"))
    assert summary["exit_code"] == 3
    assert summary["stopped_by_challenge"] is False  # moved on, not stopped
    assert summary["regions"]["toronto-on"]["status"] == "challenged"
    assert summary["regions"]["ottawa-on"]["status"] == "done"
    counts = summary["counts"]
    assert counts == {"done": 1, "partial": 0, "failed": 0, "pending": 0, "challenged": 1}
    assert sum(counts.values()) == 2  # every region is accounted for
    assert "1 done" in first.stdout
    assert "1 challenged" in first.stdout

    # Reopen only the challenged region; it now completes, ottawa stays skipped.
    second = _run_ontario(
        state_dir,
        base_config,
        regions_file,
        "--max-consecutive-challenges",
        "3",
        "--reopen",
        "toronto-on",
        env_overrides={"FAKE_SCRAPER_NEVER_CHALLENGE": "toronto-on"},
    )

    assert second.returncode == 0, second.stderr
    assert "Reopening toronto-on (was challenged)" in second.stderr
    assert "Skipping ottawa-on (already done)" in second.stderr
    assert len(_stub_calls(state_dir, "toronto-on")) == 4
    assert len(_stub_calls(state_dir, "ottawa-on")) == 1  # not rerun

    state = _region_states(state_dir)
    assert state["toronto-on"]["status"] == "done"
    assert state["toronto-on"]["last_stop_reason"] == "pagination_exhausted"
    assert state["ottawa-on"]["status"] == "done"
    summary = json.loads((state_dir / "last-run-summary.json").read_text(encoding="utf-8"))
    assert summary["exit_code"] == 0
    assert summary["counts"]["done"] == 2


def test_cli_stall_watchdog_kills_silent_child_and_continues(tmp_path: Path) -> None:
    """A child that goes silent forever is killed and the queue moves on.

    Before the watchdog existed this call never returned: the hung child kept
    the queue blocked with no output.
    """
    state_dir, base_config, regions_file = _prepare_queue(
        tmp_path,
        regions=[
            {"city": "toronto", "name": "Toronto"},
            {"city": "ottawa", "name": "Ottawa"},
        ],
    )
    started = time.monotonic()

    result = _run_ontario(
        state_dir,
        base_config,
        regions_file,
        "--stall-timeout-minutes",
        "0.05",  # 3 seconds
        "--error-max-attempts",
        "1",
        env_overrides={
            "FAKE_SCRAPER_HANG": "toronto-on",
            "FAKE_SCRAPER_NEVER_CHALLENGE": "ottawa-on",
        },
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 4, result.stderr
    assert "stall detected: no output for" in result.stderr
    assert "terminating so the region can retry" in result.stderr
    assert "stall kill counted as attempt 1/1 for toronto-on" in result.stderr
    assert "toronto-on failed after 1 non-challenge error(s)" in result.stderr
    assert "(last exit -15)" in result.stderr or "(last exit -9)" in result.stderr
    assert elapsed < 60  # bounded: the pre-watchdog queue blocked indefinitely

    calls = _stub_calls(state_dir, "toronto-on")
    assert len(calls) == 1
    state = _region_states(state_dir)
    assert state["toronto-on"]["status"] == "failed"
    assert state["toronto-on"]["last_exit_code"] in (-15, -9)
    # The queue proceeded to the next region after the kill.
    assert state["ottawa-on"]["status"] == "done"
    assert state["ottawa-on"]["last_stop_reason"] == "pagination_exhausted"


def test_cli_stall_watchdog_spares_chatty_child(tmp_path: Path) -> None:
    """Output resets the watchdog: a slow child is not killed while it talks."""
    state_dir, base_config, regions_file = _prepare_queue(tmp_path)
    started = time.monotonic()

    result = _run_ontario(
        state_dir,
        base_config,
        regions_file,
        "--stall-timeout-minutes",
        "0.1",  # 6 seconds, shorter than the child's total runtime
        env_overrides={
            "FAKE_SCRAPER_NEVER_CHALLENGE": "toronto-on",
            "FAKE_SCRAPER_CHATTY_SECONDS": "7",
        },
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert "stall detected" not in result.stderr
    assert elapsed >= 6.0  # ran longer than the stall timeout without a kill
    # The child's own lines were forwarded live through the queue's streams.
    assert "fake-scraper: still working" in result.stderr
    assert _region_state(state_dir)["status"] == "done"
    assert len(_stub_calls(state_dir)) == 1
