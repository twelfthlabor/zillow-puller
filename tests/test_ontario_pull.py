from __future__ import annotations

import fcntl
import json
import logging
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import ontario_pull
from ontario_pull import (
    ConfigError,
    QueueInterrupted,
    QueueLimits,
    Region,
    RegionState,
    generate_region_config,
    load_queue_state,
    load_regions_file,
    main,
    run_queue,
    save_queue_state,
    select_regions,
)

PAGINATION_EXHAUSTED = "pagination_exhausted"
NO_REPORT = object()


def write_base_config(tmp_path: Path, **overrides: object) -> Path:
    raw: dict[str, object] = {
        "start_url": "https://www.zillow.com/homes/{city}-{state}/",
        "user_agent": "TestBot/1.0 (+mailto:test@example.com)",
        "output_csv": "data/listings.csv",
        "checkpoint_file": "data/checkpoint.json",
        "error_directory": "data/errors",
        "report_file": "data/report.json",
        "max_pages": 0,
        "pagination": {
            "mode": "next_button",
            "next_selector": "a.next",
            "url_template_fallback": "https://www.zillow.com/{city}-{state}/{page}_p/",
        },
        "selectors": {
            "card": "[data-test='property-card']",
            "price": {"selector": ".price", "transform": "money"},
        },
    }
    raw.update(overrides)
    path = tmp_path / "base-config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def write_regions_file(tmp_path: Path, regions: list[dict[str, str]]) -> Path:
    path = tmp_path / "regions.json"
    path.write_text(json.dumps({"province": "on", "regions": regions}), encoding="utf-8")
    return path


class FakeScraper:
    """Scripted ``run_region``: pops ``(exit_code, stop_reason)`` per call.

    ``stop_reason`` of ``NO_REPORT`` means no report.json is written; ``None``
    writes a report without a stop_reason. Only exit code 0 writes a report.
    """

    def __init__(self, results: list[tuple[int, object]]) -> None:
        self.results = list(results)
        self.calls: list[Path] = []

    def __call__(self, config_path: Path) -> int:
        self.calls.append(config_path)
        if not self.results:
            raise AssertionError(f"unexpected scraper call #{len(self.calls)}: {config_path}")
        exit_code, stop_reason = self.results.pop(0)
        if exit_code == 0 and stop_reason is not NO_REPORT:
            report = {} if stop_reason is None else {"stop_reason": stop_reason}
            (config_path.parent / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return exit_code


def test_limits() -> None:
    limits = QueueLimits(cooldown_min_minutes=1, cooldown_max_minutes=2)
    assert limits.cooldown_seconds == (60.0, 120.0)
    assert QueueLimits(reopen=["toronto"]).reopen == frozenset({"toronto"})
    with pytest.raises(ValueError):
        QueueLimits(cooldown_min_minutes=10, cooldown_max_minutes=1)
    with pytest.raises(ValueError):
        QueueLimits(max_consecutive_challenges=0)
    with pytest.raises(ValueError):
        QueueLimits(max_total_challenges=0)
    with pytest.raises(ValueError, match="max-total-challenges"):
        QueueLimits(max_consecutive_challenges=4, max_total_challenges=2)


def test_stall_timeout_limits_and_decision() -> None:
    assert QueueLimits().stall_timeout_seconds == 600.0
    assert QueueLimits(stall_timeout_minutes=0.05).stall_timeout_seconds == 3.0
    assert QueueLimits(stall_timeout_minutes=0).stall_timeout_seconds == 0.0
    with pytest.raises(ValueError, match="stall-timeout-minutes"):
        QueueLimits(stall_timeout_minutes=-1)

    # Strictly past the threshold triggers; a disabled watchdog (0) never does.
    assert ontario_pull._stall_exceeded(100.0, 159.0, 60.0) is False
    assert ontario_pull._stall_exceeded(100.0, 161.0, 60.0) is True
    assert ontario_pull._stall_exceeded(100.0, 10_000.0, 0.0) is False


def test_run_with_stall_watchdog_terminates_a_silent_child() -> None:
    started = time.monotonic()
    exit_code = ontario_pull._run_with_stall_watchdog(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stall_timeout_seconds=1.0,
        poll_seconds=0.1,
    )

    assert exit_code == -signal.SIGTERM
    assert time.monotonic() - started < 10


def test_run_with_stall_watchdog_forwards_output_and_can_be_disabled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = (
        "import sys, time;print('child-out');print('child-err', file=sys.stderr);time.sleep(0.2)"
    )

    exit_code = ontario_pull._run_with_stall_watchdog(
        [sys.executable, "-c", script],
        stall_timeout_seconds=0.0,
        poll_seconds=0.05,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    # Child stdout and stderr keep going to the queue's matching streams.
    assert "child-out" in captured.out
    assert "child-err" in captured.err


def test_terminate_process_escalates_to_sigkill() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print('ready', flush=True);"
            "time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        ontario_pull._terminate_process(process, grace_seconds=0.2)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_terminate_process_handles_normal_and_reaped_children() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ontario_pull._terminate_process(process, grace_seconds=5.0)
        assert process.returncode == -signal.SIGTERM
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    reaped = subprocess.Popen([sys.executable, "-c", "pass"])
    reaped.wait()
    ontario_pull._terminate_process(reaped, grace_seconds=0.1)  # must not raise


def test_stall_clock_prefers_sleep_inclusive_source() -> None:
    reading = ontario_pull._stall_clock()
    # The watchdog must use the sleep-inclusive helper, not time.monotonic.
    assert (
        ontario_pull._run_with_stall_watchdog.__kwdefaults__["clock"] is ontario_pull._stall_clock
    )
    if hasattr(time, "CLOCK_BOOTTIME"):
        assert reading == pytest.approx(time.clock_gettime(time.CLOCK_BOOTTIME), abs=5.0)
    elif sys.platform == "darwin":
        # Darwin's CLOCK_MONOTONIC counts time spent asleep, unlike
        # time.monotonic() (mach_absolute_time); the difference in the two
        # readings on a host that has slept is accumulated sleep time.
        assert reading == pytest.approx(time.clock_gettime(time.CLOCK_MONOTONIC), abs=5.0)
        assert time.clock_gettime(time.CLOCK_MONOTONIC) + 0.05 >= time.monotonic()
    else:
        assert reading == pytest.approx(time.monotonic(), abs=5.0)


def test_watchdog_counts_simulated_sleep_toward_timeout() -> None:
    """A clock leap while the child is silent trips the timeout immediately."""
    readings = [1000.0]
    calls = [0]

    def leaping_clock() -> float:
        calls[0] += 1
        if calls[0] > 1:
            readings[0] += 3600.0  # simulated wake after an hour asleep
        return readings[0]

    started = time.monotonic()
    exit_code = ontario_pull._run_with_stall_watchdog(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stall_timeout_seconds=600.0,
        clock=leaping_clock,
        poll_seconds=0.05,
    )

    assert exit_code == -signal.SIGTERM
    assert time.monotonic() - started < 30  # no real 10-minute wait


def test_run_with_stall_watchdog_counts_unterminated_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Partial lines (no newline) are output too and must reset the watchdog."""
    script = (
        "import sys, time\n"
        "deadline = time.monotonic() + 4.0\n"
        "while time.monotonic() < deadline:\n"
        "    sys.stdout.write('.')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.2)\n"
        "print()\n"
    )

    started = time.monotonic()
    exit_code = ontario_pull._run_with_stall_watchdog(
        [sys.executable, "-c", script],
        stall_timeout_seconds=1.0,
        poll_seconds=0.1,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert time.monotonic() - started >= 3.5  # ran past the timeout, not killed
    assert captured.out.count(".") >= 5


def test_run_with_stall_watchdog_reports_stall_via_callback() -> None:
    stalls: list[float] = []

    exit_code = ontario_pull._run_with_stall_watchdog(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stall_timeout_seconds=1.0,
        poll_seconds=0.1,
        on_stall=stalls.append,
    )

    assert exit_code == -signal.SIGTERM
    assert len(stalls) == 1
    assert stalls[0] >= 1.0


def test_watchdog_propagates_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("spawn failed")

    monkeypatch.setattr(ontario_pull.subprocess, "Popen", boom)

    with pytest.raises(OSError, match="spawn failed"):
        ontario_pull._run_with_stall_watchdog(["does-not-matter"], 1.0)


def test_watchdog_kills_child_interrupted_during_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    spawned: list[subprocess.Popen] = []

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen:
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    def interrupted(_self: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ontario_pull.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(ontario_pull._OutputForwarder, "start", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            ontario_pull._run_with_stall_watchdog(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                1.0,
            )
        # The already-spawned child must be killed and reaped even though the
        # interrupt landed before the watchdog loop started.
        assert spawned
        assert spawned[0].returncode == -signal.SIGKILL
    finally:
        if spawned and spawned[0].poll() is None:
            spawned[0].kill()
            spawned[0].wait()


class _StallingScraper:
    """Records calls and claims every return was a watchdog stall kill."""

    stalled = True

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _config_path: Path) -> int:
        self.calls += 1
        return -signal.SIGTERM


def test_stall_kill_attempt_accounting_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"
    limits = QueueLimits(error_max_attempts=2, error_backoff_seconds=5.0)
    scraper = _StallingScraper()

    with caplog.at_level(logging.WARNING, logger="ontario_pull"):
        result = run_queue(
            [Region(city="toronto")],
            base,
            state_dir,
            scraper,
            lambda _seconds: None,
            limits=limits,
        )

    assert scraper.calls == 2
    assert result.failed == ["toronto-on"]
    assert "stall kill counted as attempt 1/2 for toronto-on" in caplog.text
    assert "stall kill counted as attempt 2/2 for toronto-on" in caplog.text
    assert "--retry-failed or --reopen toronto-on" in caplog.text


def test_cli_rejects_non_finite_stall_timeout(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    base = write_base_config(tmp_path)
    regions_file = write_regions_file(tmp_path, [{"city": "toronto", "name": "Toronto"}])
    common = [
        "--base-config",
        str(base),
        "--regions",
        str(regions_file),
        "--state-dir",
        str(tmp_path / "state"),
        # Harmless stand-in so a regression cannot reach the real scraper.
        "--scraper-command",
        f"{sys.executable} -c pass",
    ]

    with caplog.at_level(logging.ERROR, logger="ontario_pull"):
        assert main([*common, "--stall-timeout-minutes", "nan"]) == 2
        assert main([*common, "--stall-timeout-minutes", "inf"]) == 2

    assert "stall-timeout-minutes" in caplog.text


def test_slow_page_wait_warns_but_still_runs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    base = write_base_config(tmp_path, page_wait_seconds=900)
    state_dir = tmp_path / "state"
    limits = QueueLimits(stall_timeout_minutes=5.0)  # 300s
    scraper = FakeScraper([(0, PAGINATION_EXHAUSTED)])

    with caplog.at_level(logging.WARNING, logger="ontario_pull"):
        result = run_queue(
            [Region(city="toronto")],
            base,
            state_dir,
            scraper,
            lambda _seconds: None,
            limits=limits,
        )

    assert result.exit_code == 0
    assert result.done == ["toronto-on"]
    assert "page_wait_seconds=900" in caplog.text
    assert "exceeds the stall timeout" in caplog.text


def test_regions_file_is_valid_and_curated() -> None:
    path = Path(ontario_pull.__file__).resolve().parent / "regions.ontario.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["province"] == "on"
    regions = load_regions_file(path)
    assert len(regions) >= 30
    slugs = [region.slug for region in regions]
    assert len(slugs) == len(set(slugs))
    assert "toronto" in {region.city for region in regions}
    assert all(region.state_slug == "on" for region in regions)


def test_select_regions_matches_city_or_slug_and_rejects_unknown() -> None:
    regions = [
        Region(city="toronto", name="Toronto"),
        Region(city="ottawa", name="Ottawa"),
        Region(city="st-catharines", name="St. Catharines"),
    ]
    assert [r.slug for r in select_regions(regions, only=["toronto"])] == ["toronto-on"]
    assert [r.slug for r in select_regions(regions, only=["toronto-on", "ottawa"])] == [
        "toronto-on",
        "ottawa-on",
    ]
    assert [r.slug for r in select_regions(regions, skip=["toronto"])] == [
        "ottawa-on",
        "st-catharines-on",
    ]
    with pytest.raises(ConfigError, match="unknown region slug"):
        select_regions(regions, only=["nowhere"])


def test_placeholder_validation_error(tmp_path: Path) -> None:
    bad_base = write_base_config(tmp_path, start_url="https://www.zillow.com/toronto/")
    region = Region.from_mapping({"city": "toronto"})

    with pytest.raises(ConfigError, match="placeholders"):
        generate_region_config(region, bad_base, tmp_path / "state")

    regions_file = write_regions_file(tmp_path, [{"city": "toronto", "name": "Toronto"}])
    exit_code = main(
        [
            "--dry-run",
            "--base-config",
            str(bad_base),
            "--regions",
            str(regions_file),
            "--state-dir",
            str(tmp_path / "state"),
        ]
    )
    assert exit_code == 2


def test_generated_config_substitution_and_per_region_paths(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    base_path = write_base_config(tmp_path, archive_directory="data/archive", max_pages=10)
    base_before = json.loads(base_path.read_text(encoding="utf-8"))
    state_dir = tmp_path / "state"

    with caplog.at_level(logging.WARNING, logger="ontario_pull"):
        config_path = generate_region_config(
            {"city": "St. Catharines", "name": "St. Catharines"}, base_path, state_dir
        )

    assert config_path == state_dir / "st-catharines-on" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["start_url"] == "https://www.zillow.com/homes/st-catharines-on/"
    assert (
        config["pagination"]["url_template_fallback"]
        == "https://www.zillow.com/st-catharines-on/{page}_p/"
    )
    assert config["output_csv"] == "listings.csv"
    assert config["checkpoint_file"] == "checkpoint.json"
    assert config["error_directory"] == "errors"
    assert config["report_file"] == "report.json"
    assert config["archive_directory"] == "archive"
    assert config["selectors"] == base_before["selectors"]
    assert config["user_agent"] == base_before["user_agent"]
    assert json.loads(base_path.read_text(encoding="utf-8")) == base_before
    assert "max_pages" in caplog.text

    no_archive = write_base_config(tmp_path / "plain", max_pages=0)
    plain_path = generate_region_config({"city": "ottawa"}, no_archive, state_dir)
    plain = json.loads(plain_path.read_text(encoding="utf-8"))
    assert "archive_directory" not in plain


def test_done_regions_skip_and_partial_regions_resume(tmp_path: Path) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"
    regions = [
        Region(city="toronto", name="Toronto"),
        Region(city="ottawa", name="Ottawa"),
    ]
    sleeps: list[float] = []
    limits = QueueLimits(cooldown_min_minutes=1, cooldown_max_minutes=1)

    first = FakeScraper([(0, PAGINATION_EXHAUSTED), (0, "max_pages_reached")])
    result = run_queue(regions, base, state_dir, first, sleeps.append, limits=limits)
    assert result.exit_code == 0
    assert result.done == ["toronto-on"]
    assert result.partial == ["ottawa-on"]
    assert first.calls[0] == state_dir / "toronto-on" / "config.json"
    assert first.calls[1] == state_dir / "ottawa-on" / "config.json"

    state = load_queue_state(state_dir)
    assert state["toronto-on"].status == "done"
    assert state["toronto-on"].last_stop_reason == PAGINATION_EXHAUSTED
    assert state["ottawa-on"].status == "partial"
    assert state["ottawa-on"].last_stop_reason == "max_pages_reached"

    second = FakeScraper([(0, PAGINATION_EXHAUSTED)])
    resumed = run_queue(regions, base, state_dir, second, sleeps.append, limits=limits)
    assert resumed.exit_code == 0
    assert resumed.done == ["ottawa-on", "toronto-on"]
    assert len(second.calls) == 1
    assert second.calls[0] == state_dir / "ottawa-on" / "config.json"


def test_challenge_waits_cooldown_then_retries_same_region(tmp_path: Path) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"
    sleeps: list[float] = []
    limits = QueueLimits(cooldown_min_minutes=1, cooldown_max_minutes=1)

    scraper = FakeScraper([(3, None), (0, "incremental_complete")])
    result = run_queue(
        [Region(city="toronto")], base, state_dir, scraper, sleeps.append, limits=limits
    )

    assert result.exit_code == 0
    assert sleeps == [60.0]
    assert scraper.calls == [
        state_dir / "toronto-on" / "config.json",
        state_dir / "toronto-on" / "config.json",
    ]
    state = load_queue_state(state_dir)["toronto-on"]
    assert state.status == "done"
    assert state.attempts == 2
    assert state.challenge_waits == 1
    assert state.last_exit_code == 0
    assert state.last_stop_reason == "incremental_complete"


def test_region_challenge_limit_marks_challenged_and_continues(tmp_path: Path) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"
    sleeps: list[float] = []
    limits = QueueLimits(
        cooldown_min_minutes=1, cooldown_max_minutes=1, max_consecutive_challenges=3
    )
    regions = [Region(city="toronto"), Region(city="ottawa")]

    scraper = FakeScraper([(3, None), (3, None), (3, None), (0, PAGINATION_EXHAUSTED)])
    result = run_queue(regions, base, state_dir, scraper, sleeps.append, limits=limits)

    assert result.exit_code == 3
    assert result.stopped_by_challenge is False
    assert result.challenged == ["toronto-on"]
    assert result.done == ["ottawa-on"]
    assert len(scraper.calls) == 4
    assert scraper.calls[3] == state_dir / "ottawa-on" / "config.json"
    assert sleeps == [60.0, 60.0]

    state = load_queue_state(state_dir)
    assert state["toronto-on"].status == "challenged"
    assert state["toronto-on"].attempts == 3
    assert state["toronto-on"].challenge_waits == 2
    assert state["toronto-on"].last_exit_code == 3
    assert state["ottawa-on"].status == "done"
    assert state["ottawa-on"].attempts == 1

    summary = json.loads((state_dir / "last-run-summary.json").read_text(encoding="utf-8"))
    assert summary["exit_code"] == 3
    assert summary["stopped_by_challenge"] is False
    assert summary["regions"]["toronto-on"]["status"] == "challenged"
    assert summary["counts"] == {
        "done": 1,
        "partial": 0,
        "failed": 0,
        "challenged": 1,
        "pending": 0,
    }
    assert sum(summary["counts"].values()) == len(regions)

    # A challenged region is resumable later without --retry-failed/--reopen.
    resumed = FakeScraper([(0, PAGINATION_EXHAUSTED)])
    second = run_queue(
        [Region(city="toronto")], base, state_dir, resumed, sleeps.append, limits=limits
    )
    assert second.exit_code == 0
    assert second.done == ["toronto-on"]
    assert len(resumed.calls) == 1


def test_total_challenge_bound_stops_between_regions(tmp_path: Path) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"
    sleeps: list[float] = []
    limits = QueueLimits(
        cooldown_min_minutes=1,
        cooldown_max_minutes=1,
        max_consecutive_challenges=3,
        max_total_challenges=4,
    )
    regions = [
        Region(city="toronto"),
        Region(city="ottawa"),
        Region(city="mississauga"),
    ]

    # Toronto burns 3 challenges, then Ottawa's first challenge reaches the
    # total bound (4). Ottawa must still settle (challenged) before the queue
    # stops at the region boundary; Mississauga is never started.
    scraper = FakeScraper([(3, None)] * 6)
    result = run_queue(regions, base, state_dir, scraper, sleeps.append, limits=limits)

    assert result.exit_code == 3
    assert result.stopped_by_challenge is True
    assert result.challenged == ["ottawa-on", "toronto-on"]
    assert len(scraper.calls) == 6
    assert [call.parent.name for call in scraper.calls] == [
        "toronto-on",
        "toronto-on",
        "toronto-on",
        "ottawa-on",
        "ottawa-on",
        "ottawa-on",
    ]
    assert sleeps == [60.0, 60.0, 60.0, 60.0]

    state = load_queue_state(state_dir)
    assert state["toronto-on"].status == "challenged"
    assert state["ottawa-on"].status == "challenged"
    assert state["ottawa-on"].attempts == 3
    assert state["mississauga-on"].status == "pending"
    assert state["mississauga-on"].attempts == 0

    summary = json.loads((state_dir / "last-run-summary.json").read_text(encoding="utf-8"))
    assert summary["exit_code"] == 3
    assert summary["stopped_by_challenge"] is True
    assert summary["counts"] == {
        "done": 0,
        "partial": 0,
        "failed": 0,
        "challenged": 2,
        "pending": 1,
    }
    assert sum(summary["counts"].values()) == len(regions)


def test_fresh_regions_processed_before_challenged(tmp_path: Path) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"
    save_queue_state(
        state_dir,
        {
            "toronto-on": RegionState(
                status="challenged", attempts=3, challenge_waits=2, last_exit_code=3
            ),
            "ottawa-on": RegionState(status="pending"),
            "mississauga-on": RegionState(status="partial", attempts=1, last_exit_code=0),
            "brampton-on": RegionState(status="challenged", attempts=1, last_exit_code=3),
        },
    )
    regions = [
        Region(city="toronto"),
        Region(city="ottawa"),
        Region(city="mississauga"),
        Region(city="brampton"),
    ]

    scraper = FakeScraper([(0, PAGINATION_EXHAUSTED)] * 4)
    result = run_queue(regions, base, state_dir, scraper, lambda _seconds: None)

    # File order within each group: fresh (pending/partial) then challenged.
    assert [call.parent.name for call in scraper.calls] == [
        "ottawa-on",
        "mississauga-on",
        "toronto-on",
        "brampton-on",
    ]
    assert result.done == ["brampton-on", "mississauga-on", "ottawa-on", "toronto-on"]
    assert result.exit_code == 0


def test_reopen_excluded_by_only_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"
    save_queue_state(
        state_dir,
        {"toronto-on": RegionState(status="challenged", attempts=3, last_exit_code=3)},
    )
    limits = QueueLimits(reopen=["toronto", "brampton"])
    scraper = FakeScraper([(0, PAGINATION_EXHAUSTED)])

    with caplog.at_level(logging.WARNING, logger="ontario_pull"):
        # Only Ottawa is selected, so both reopen tokens are filtered out.
        result = run_queue(
            [Region(city="ottawa")],
            base,
            state_dir,
            scraper,
            lambda _seconds: None,
            limits=limits,
        )

    assert result.exit_code == 0
    assert "--reopen toronto is excluded by --only/--skip; not reset" in caplog.text
    assert "--reopen brampton is excluded by --only/--skip; not reset" in caplog.text
    # The excluded region was not reset.
    assert load_queue_state(state_dir)["toronto-on"].status == "challenged"


def test_cli_rejects_total_below_consecutive(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    base = write_base_config(tmp_path)
    regions_file = write_regions_file(tmp_path, [{"city": "toronto", "name": "Toronto"}])

    with caplog.at_level(logging.ERROR, logger="ontario_pull"):
        exit_code = main(
            [
                "--base-config",
                str(base),
                "--regions",
                str(regions_file),
                "--state-dir",
                str(tmp_path / "state"),
                "--max-consecutive-challenges",
                "4",
                "--max-total-challenges",
                "2",
            ]
        )

    assert exit_code == 2
    assert "max-total-challenges" in caplog.text
    assert "max-consecutive-challenges" in caplog.text


def test_challenged_takes_exit_code_precedence_over_failed(tmp_path: Path) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"
    sleeps: list[float] = []
    limits = QueueLimits(
        cooldown_min_minutes=1,
        cooldown_max_minutes=1,
        max_consecutive_challenges=2,
        error_max_attempts=1,
    )
    regions = [Region(city="toronto"), Region(city="ottawa")]

    scraper = FakeScraper([(3, None), (3, None), (1, None)])
    result = run_queue(regions, base, state_dir, scraper, sleeps.append, limits=limits)

    assert result.challenged == ["toronto-on"]
    assert result.failed == ["ottawa-on"]
    assert result.exit_code == 3
    assert sleeps == [60.0]


def test_non_challenge_errors_retry_then_mark_region_failed(tmp_path: Path) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"
    sleeps: list[float] = []
    limits = QueueLimits(
        cooldown_min_minutes=1,
        cooldown_max_minutes=1,
        error_max_attempts=2,
        error_backoff_seconds=5.0,
    )
    regions = [Region(city="toronto"), Region(city="ottawa")]

    scraper = FakeScraper([(1, None), (1, None), (0, PAGINATION_EXHAUSTED)])
    result = run_queue(regions, base, state_dir, scraper, sleeps.append, limits=limits)

    assert result.exit_code == 4
    assert result.failed == ["toronto-on"]
    assert result.done == ["ottawa-on"]
    assert sleeps == [5.0]
    state = load_queue_state(state_dir)
    assert state["toronto-on"].status == "failed"
    assert state["toronto-on"].attempts == 2
    assert state["ottawa-on"].status == "done"


def test_non_challenge_error_then_success(tmp_path: Path) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"
    sleeps: list[float] = []
    limits = QueueLimits(error_max_attempts=2, error_backoff_seconds=5.0)

    scraper = FakeScraper([(2, None), (0, "incremental_complete")])
    result = run_queue(
        [Region(city="toronto")], base, state_dir, scraper, sleeps.append, limits=limits
    )

    assert result.exit_code == 0
    assert sleeps == [5.0]
    assert result.done == ["toronto-on"]
    assert load_queue_state(state_dir)["toronto-on"].attempts == 2


def test_failed_regions_require_retry_failed(tmp_path: Path) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"
    sleeps: list[float] = []
    limits = QueueLimits(error_max_attempts=1)

    failed = FakeScraper([(1, None)])
    first = run_queue(
        [Region(city="toronto")], base, state_dir, failed, sleeps.append, limits=limits
    )
    assert first.exit_code == 4
    assert load_queue_state(state_dir)["toronto-on"].status == "failed"

    def no_calls(_config_path: Path) -> int:
        raise AssertionError("failed region must be skipped without --retry-failed")

    second = run_queue(
        [Region(city="toronto")], base, state_dir, no_calls, sleeps.append, limits=limits
    )
    assert second.exit_code == 4
    assert second.failed == ["toronto-on"]

    retry_limits = QueueLimits(error_max_attempts=1, retry_failed=True)
    retried = FakeScraper([(0, PAGINATION_EXHAUSTED)])
    third = run_queue(
        [Region(city="toronto")], base, state_dir, retried, sleeps.append, limits=retry_limits
    )
    assert third.exit_code == 0
    assert third.done == ["toronto-on"]
    assert len(retried.calls) == 1


def test_cli_reopen_resets_any_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    base = write_base_config(tmp_path)
    regions_file = write_regions_file(tmp_path, [{"city": "toronto", "name": "Toronto"}])
    state_dir = tmp_path / "state"
    save_queue_state(
        state_dir,
        {"toronto-on": RegionState(status="failed", attempts=2, last_exit_code=1)},
    )
    run_calls: list[list[str]] = []

    def fake_run(command: list[str], stall_timeout_seconds: float, **_kwargs: object) -> int:
        run_calls.append(command)
        assert stall_timeout_seconds == 600.0
        config_path = Path(command[command.index("--config") + 1])
        (config_path.parent / "report.json").write_text(
            json.dumps({"stop_reason": PAGINATION_EXHAUSTED}), encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(ontario_pull, "_run_with_stall_watchdog", fake_run)
    common = [
        "--base-config",
        str(base),
        "--regions",
        str(regions_file),
        "--state-dir",
        str(state_dir),
        "--log-level",
        "WARNING",
    ]

    # A failed region stays skipped without --retry-failed or --reopen.
    assert main(common) == 4
    assert run_calls == []

    # --reopen resets the failed region and the stub now completes it.
    assert main([*common, "--reopen", "toronto"]) == 0
    assert len(run_calls) == 1
    assert load_queue_state(state_dir)["toronto-on"].status == "done"

    # Reopening a done region runs it again on request (full slug also works).
    assert main([*common, "--reopen", "toronto-on"]) == 0
    assert len(run_calls) == 2

    # Unknown slug is a usage error, before anything is generated or run.
    with caplog.at_level(logging.ERROR, logger="ontario_pull"):
        assert main([*common, "--reopen", "nowhere"]) == 2
    assert "unknown region slug" in caplog.text
    assert len(run_calls) == 2
    assert load_queue_state(state_dir)["toronto-on"].status == "done"


def test_missing_or_unfinished_report_marks_partial(tmp_path: Path) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"
    sleeps: list[float] = []
    regions = [
        Region(city="toronto"),
        Region(city="ottawa"),
        Region(city="mississauga"),
        Region(city="brampton"),
    ]

    scraper = FakeScraper(
        [
            (0, NO_REPORT),
            (0, "max_pages_reached"),
            (0, None),
            (0, "mystery_reason"),
        ]
    )
    result = run_queue(regions, base, state_dir, scraper, sleeps.append)

    assert result.exit_code == 0
    assert sorted(result.partial) == [
        "brampton-on",
        "mississauga-on",
        "ottawa-on",
        "toronto-on",
    ]
    assert result.done == []
    assert all(state.last_exit_code == 0 for state in result.regions.values())


def test_state_file_round_trip(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    assert load_queue_state(state_dir) == {}

    states = {
        "toronto-on": RegionState(
            status="done",
            attempts=2,
            challenge_waits=1,
            last_exit_code=0,
            last_stop_reason=PAGINATION_EXHAUSTED,
            updated_at="2026-09-12T00:00:00+00:00",
        ),
        "ottawa-on": RegionState(
            status="partial",
            attempts=1,
            challenge_waits=0,
            last_exit_code=0,
            last_stop_reason="max_pages_reached",
            updated_at="2026-09-12T00:01:00+00:00",
        ),
        "brampton-on": RegionState(
            status="challenged",
            attempts=3,
            challenge_waits=2,
            last_exit_code=3,
            last_stop_reason=None,
            updated_at="2026-09-12T00:02:00+00:00",
        ),
    }
    save_queue_state(state_dir, states, clock=lambda: 1234.0)

    raw = json.loads((state_dir / "queue-state.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["updated_at"] == "1970-01-01T00:20:34+00:00"
    assert set(raw["regions"]) == {"toronto-on", "ottawa-on", "brampton-on"}
    assert raw["regions"]["toronto-on"]["attempts"] == 2
    assert raw["regions"]["brampton-on"]["status"] == "challenged"

    assert load_queue_state(state_dir) == states


def test_interrupt_saves_state_and_stops_cleanly(tmp_path: Path) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"

    def interrupt(_config_path: Path) -> int:
        raise KeyboardInterrupt

    result = run_queue([Region(city="toronto")], base, state_dir, interrupt, lambda _s: None)

    assert result.exit_code == 130
    assert result.interrupted is True
    state = load_queue_state(state_dir)
    assert state["toronto-on"].attempts == 1
    assert (state_dir / "last-run-summary.json").exists()

    # The state lock is released on interrupt, so a later run can acquire it.
    resumed = FakeScraper([(0, PAGINATION_EXHAUSTED)])
    second = run_queue([Region(city="toronto")], base, state_dir, resumed, lambda _s: None)
    assert second.exit_code == 0
    assert len(resumed.calls) == 1


def test_queue_interrupted_exception_is_handled(tmp_path: Path) -> None:
    base = write_base_config(tmp_path)
    state_dir = tmp_path / "state"

    def interrupt(_config_path: Path) -> int:
        raise QueueInterrupted(15)

    result = run_queue([Region(city="toronto")], base, state_dir, interrupt, lambda _s: None)
    assert result.exit_code == 143
    assert result.interrupted is True


def test_dry_run_generates_configs_and_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = write_base_config(tmp_path)
    regions_file = write_regions_file(
        tmp_path,
        [
            {"city": "toronto", "name": "Toronto"},
            {"city": "ottawa", "name": "Ottawa"},
            {"city": "mississauga", "name": "Mississauga"},
        ],
    )
    state_dir = tmp_path / "state"

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry-run must not execute anything")

    monkeypatch.setattr(ontario_pull, "_run_with_stall_watchdog", unexpected)

    exit_code = main(
        [
            "--dry-run",
            "--base-config",
            str(base),
            "--regions",
            str(regions_file),
            "--state-dir",
            str(state_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Dry run: generated 3 region config(s); nothing executed." in captured.out
    assert str(state_dir / "toronto-on" / "config.json") in captured.out
    assert captured.out.count("--config") == 3
    assert (state_dir / "toronto-on" / "config.json").exists()
    assert (state_dir / "mississauga-on" / "config.json").exists()
    assert not (state_dir / "queue-state.json").exists()
    assert not (state_dir / "last-run-summary.json").exists()

    only_dir = tmp_path / "only-state"
    only_exit = main(
        [
            "--dry-run",
            "--base-config",
            str(base),
            "--regions",
            str(regions_file),
            "--state-dir",
            str(only_dir),
            "--only",
            "toronto,ottawa",
        ]
    )
    only_out = capsys.readouterr().out
    assert only_exit == 0
    assert "Dry run: generated 2 region config(s)" in only_out
    assert (only_dir / "toronto-on" / "config.json").exists()
    assert not (only_dir / "mississauga-on" / "config.json").exists()


def test_cli_runs_queue_with_fake_subprocess_and_retry_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = write_base_config(tmp_path)
    regions_file = write_regions_file(tmp_path, [{"city": "toronto", "name": "Toronto"}])
    state_dir = tmp_path / "state"
    save_queue_state(
        state_dir,
        {"toronto-on": RegionState(status="failed", attempts=2, last_exit_code=1)},
    )
    subprocess_calls: list[list[str]] = []

    def fake_run(command: list[str], stall_timeout_seconds: float, **_kwargs: object) -> int:
        subprocess_calls.append(command)
        config_path = Path(command[command.index("--config") + 1])
        (config_path.parent / "report.json").write_text(
            json.dumps({"stop_reason": PAGINATION_EXHAUSTED}), encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(ontario_pull, "_run_with_stall_watchdog", fake_run)

    args = [
        "--base-config",
        str(base),
        "--regions",
        str(regions_file),
        "--state-dir",
        str(state_dir),
        "--log-level",
        "WARNING",
    ]
    assert main(args) == 4
    assert subprocess_calls == []

    assert main([*args, "--retry-failed"]) == 0
    assert len(subprocess_calls) == 1
    assert subprocess_calls[0][-4:] == [
        "--config",
        str(state_dir / "toronto-on" / "config.json"),
        "--log-level",
        "WARNING",
    ]
    assert load_queue_state(state_dir)["toronto-on"].status == "done"
    captured = capsys.readouterr()
    assert "Ontario pull summary" in captured.out
    assert "DONE" in captured.out


def test_dry_run_reports_invalid_generated_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    base = write_base_config(
        tmp_path,
        selectors={"card": "", "price": {"selector": ".price", "transform": "money"}},
    )
    regions_file = write_regions_file(tmp_path, [{"city": "toronto", "name": "Toronto"}])

    with caplog.at_level(logging.ERROR, logger="ontario_pull"):
        exit_code = main(
            [
                "--dry-run",
                "--base-config",
                str(base),
                "--regions",
                str(regions_file),
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "selectors.card is required" in caplog.text
    assert "nothing executed" not in captured.out


def test_run_queue_rejects_invalid_generated_config(tmp_path: Path) -> None:
    base = write_base_config(
        tmp_path,
        selectors={"card": "", "price": {"selector": ".price", "transform": "money"}},
    )

    def must_not_run(_config_path: Path) -> int:
        raise AssertionError("an invalid generated config must never reach the scraper")

    with pytest.raises(ConfigError, match="selectors.card is required"):
        run_queue(
            [Region(city="toronto")],
            base,
            tmp_path / "state",
            must_not_run,
            lambda _seconds: None,
        )


def test_lock_blocks_concurrent_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    base = write_base_config(tmp_path)
    regions_file = write_regions_file(tmp_path, [{"city": "toronto", "name": "Toronto"}])
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    run_calls: list[list[str]] = []

    def fake_run(command: list[str], stall_timeout_seconds: float, **_kwargs: object) -> int:
        run_calls.append(command)
        config_path = Path(command[command.index("--config") + 1])
        (config_path.parent / "report.json").write_text(
            json.dumps({"stop_reason": PAGINATION_EXHAUSTED}), encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(ontario_pull, "_run_with_stall_watchdog", fake_run)
    common = [
        "--base-config",
        str(base),
        "--regions",
        str(regions_file),
        "--state-dir",
        str(state_dir),
        "--log-level",
        "WARNING",
    ]

    with (state_dir / ".lock").open("a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with caplog.at_level(logging.ERROR, logger="ontario_pull"):
            assert main(common) == 2
        assert "another run is active" in caplog.text
        assert run_calls == []

    assert main(common) == 0
    assert len(run_calls) == 1


def test_preflight_dead_attach_address_exits_2(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    dead_address = f"127.0.0.1:{port}"
    base = write_base_config(tmp_path, attach_address=dead_address)
    regions_file = write_regions_file(tmp_path, [{"city": "toronto", "name": "Toronto"}])

    with caplog.at_level(logging.ERROR, logger="ontario_pull"):
        exit_code = main(
            [
                "--base-config",
                str(base),
                "--regions",
                str(regions_file),
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )

    assert exit_code == 2
    assert dead_address in caplog.text
    assert "launch_attach_chrome.sh" in caplog.text


def test_missing_max_pages_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    base = write_base_config(tmp_path)
    raw = json.loads(base.read_text(encoding="utf-8"))
    del raw["max_pages"]
    base.write_text(json.dumps(raw), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="ontario_pull"):
        generate_region_config({"city": "toronto"}, base, tmp_path / "state")

    assert "no max_pages configured" in caplog.text
    assert "set max_pages: 0 for a full pull" in caplog.text


def test_default_base_config_is_module_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_dir = Path(ontario_pull.__file__).resolve().parent
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    resolved = ontario_pull.default_base_config()

    expected = module_dir / "config.json"
    if not expected.exists():
        expected = module_dir / "config.example.json"
    assert resolved == expected
    assert resolved != tmp_path / "config.json"
