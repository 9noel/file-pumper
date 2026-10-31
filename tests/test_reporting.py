from __future__ import annotations

import io
import json
import math

import pytest

from conftest import SAMPLE
from filepumper import config, engine, reporting, system_info
from filepumper.engine import FileResult, FileStatus
from filepumper.exceptions import ConfigError, UsageError
from filepumper.reporting import (
    ProgressRenderer,
    format_duration,
    format_progress_line,
    format_rate,
    format_summary,
)


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------------- formatters


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0.0s"), (1.234, "1.2s"), (59.9, "59.9s"), (60, "1:00"), (125, "2:05"), (3725, "1:02:05"),
     (None, "--:--"), (-1, "--:--"), (math.inf, "--:--"), (math.nan, "--:--")],
)  # fmt: skip
def test_format_duration(seconds, expected) -> None:
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        (None, "-- /s"),
        (0, "-- /s"),
        (-3, "-- /s"),
        (math.inf, "-- /s"),
        (math.nan, "-- /s"),
        (2**20, "1.00 MiB/s"),
    ],
)
def test_format_rate(rate, expected) -> None:
    assert format_rate(rate) == expected


# ---------------------------------------------------------------------- progress


@pytest.fixture
def small_plan(make_file, make_options):
    files = [make_file("one.bin"), make_file("two.bin")]
    return engine.build_plan([str(p) for p in files], make_options(add=10_000))


def test_progress_line_contents(small_plan) -> None:
    tracker = engine.ProgressTracker(small_plan)
    tracker.set_phase(0, engine.PHASE_APPENDING)
    tracker.advance(0, 5000)
    line = format_progress_line(tracker.snapshot(), rate=2.0 * 2**20)
    for expected in ("one.bin", "[appending]", "%", "MiB/s", "elapsed", "ETA", "files 0/2"):
        assert expected in line


def test_eta_is_unknown_when_not_meaningful(small_plan) -> None:
    tracker = engine.ProgressTracker(small_plan, clock=lambda: 0.0)
    assert "ETA --:--" in format_progress_line(tracker.snapshot(), rate=None)
    tracker.advance(0, 1000)
    assert "ETA --:--" in format_progress_line(tracker.snapshot(), rate=0.0)


def test_progress_line_is_truncated_to_the_terminal(small_plan) -> None:
    tracker = engine.ProgressTracker(small_plan)
    tracker.set_phase(0, engine.PHASE_COPYING)
    line = format_progress_line(tracker.snapshot(), rate=1.0, columns=40)
    assert len(line) <= 40 and line.endswith("...")


def test_progress_line_shows_failures(small_plan) -> None:
    tracker = engine.ProgressTracker(small_plan)
    tracker.set_phase(0, engine.PHASE_FAILED)
    assert "1 failed" in format_progress_line(tracker.snapshot(), rate=None)


def test_tty_renderer_is_throttled_and_uses_one_line(small_plan) -> None:
    clock, stream = Clock(), FakeTTY()
    renderer = ProgressRenderer(
        engine.ProgressTracker(small_plan, clock=clock),
        stream,
        interval=0.2,
        clock=clock,
        columns=120,
    )
    renderer.tick()
    clock.now = 0.05
    renderer.tick()
    clock.now = 0.10
    renderer.tick()
    assert stream.getvalue().count("\r") == 1
    clock.now = 0.30
    renderer.tick()
    assert stream.getvalue().count("\r") == 2
    renderer.finish()
    assert stream.getvalue().endswith("\n")
    assert stream.getvalue().count("\n") == 1


def test_plain_renderer_for_redirected_output(small_plan) -> None:
    clock, stream = Clock(), io.StringIO()
    tracker = engine.ProgressTracker(small_plan, clock=clock)
    renderer = ProgressRenderer(tracker, stream, clock=clock)
    renderer.tick()
    assert stream.getvalue() == ""  # nothing has started yet
    tracker.set_phase(0, engine.PHASE_COPYING)
    renderer.tick()
    renderer.tick()  # no duplicate line for an unchanged file
    tracker.set_phase(0, engine.PHASE_DONE)
    renderer.tick()
    lines = stream.getvalue().splitlines()
    assert lines == [
        f"[1/2] {small_plan.files[0].path}: copying",
        f"[1/2] {small_plan.files[0].path}: done",
    ]
    assert "\r" not in stream.getvalue()


def test_plain_renderer_heartbeat(small_plan) -> None:
    clock, stream = Clock(), io.StringIO()
    tracker = engine.ProgressTracker(small_plan, clock=clock)
    tracker.set_phase(0, engine.PHASE_COPYING)
    renderer = ProgressRenderer(tracker, stream, clock=clock)
    renderer.tick()
    clock.now = 31.0
    renderer.tick()
    assert any("elapsed" in line for line in stream.getvalue().splitlines())


def test_disabled_renderer_prints_nothing(small_plan) -> None:
    stream = FakeTTY()
    renderer = ProgressRenderer(engine.ProgressTracker(small_plan), stream, enabled=False)
    renderer.tick()
    renderer.finish()
    assert stream.getvalue() == ""


# ----------------------------------------------------------------------- summary


def _result(status, **kwargs) -> FileResult:
    defaults = {"path": "f.bin", "original_size": 10, "requested_add": 5}
    return FileResult(status=status, **{**defaults, **kwargs})


def test_summary_counts_and_details() -> None:
    result = engine.BatchResult(
        results=[
            _result(FileStatus.SUCCESS, path="ok.bin", final_size=15, bytes_added=5, verified=True),
            _result(FileStatus.SKIPPED, path="skip.bin", error="nothing to add"),
            _result(
                FileStatus.FAILED, path="bad.bin", error="disk full; the original file is unchanged"
            ),
            _result(FileStatus.CANCELLED, path="stop.bin", error="cancelled"),
        ],
        elapsed=2.5,
    )
    text = format_summary(result)
    for expected in (
        "Files requested : 4", "succeeded     : 1", "skipped       : 1",
        "failed        : 1", "cancelled     : 1", "Total bytes added: 5 B", "2.5s", "[OK]",
        "ok.bin: 10 B -> 15 B (+5 B) [verified]", "[SKIPPED]", "[FAILED]", "disk full",
        "[CANCELLED]", "partly completed", "NOT rolled back",
    ):  # fmt: skip
        assert expected in text


def test_summary_lists_leftover_temporary_files() -> None:
    result = engine.BatchResult(
        results=[_result(FileStatus.FAILED, error="x", leftover_temp="/d/.f.tmp")], elapsed=0.1
    )
    text = format_summary(result)
    assert "could not be removed" in text and "/d/.f.tmp" in text
    assert "partly completed" not in text


def test_all_success_summary_has_no_partial_warning() -> None:
    result = engine.BatchResult(
        results=[_result(FileStatus.SUCCESS, final_size=15, bytes_added=5)], elapsed=0.1
    )
    assert "partly" not in format_summary(result)


# -------------------------------------------------------------------- JSON report


def test_report_is_valid_json_and_complete(make_file, make_options, tmp_path) -> None:
    path = make_file()
    plan = engine.build_plan([str(path)], make_options(add=100, verify=True))
    result = engine.run_batch(plan)
    report = reporting.build_report(plan, result, engine.plan_space(plan), dry_run=False)
    target = tmp_path / "r.json"
    reporting.write_report(target, report)
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == json.loads(json.dumps(report))
    assert loaded["files"][0]["bytes_added"] == 100
    assert loaded["performance"]["chunk_size_bytes"] == 4096
    assert list(tmp_path.glob(".r.json*")) == []


def test_report_for_skipped_file_uses_reason(make_file, make_options) -> None:
    path = make_file()
    plan = engine.build_plan([str(path)], make_options(add=0))
    result = engine.run_batch(plan)
    entry = reporting.build_report(plan, result, None, dry_run=False)["files"][0]
    assert entry["status"] == "skipped" and entry["error"] is None and entry["reason"]


def test_write_report_cleans_up_when_serialisation_fails(tmp_path) -> None:
    with pytest.raises(TypeError):
        reporting.write_report(tmp_path / "r.json", {"bad": object()})
    assert list(tmp_path.iterdir()) == []


def test_write_report_replaces_existing_file_atomically(tmp_path) -> None:
    target = tmp_path / "r.json"
    target.write_text("old")
    reporting.write_report(target, {"ok": True})
    assert json.loads(target.read_text()) == {"ok": True}


# ------------------------------------------------------------------ config module


def test_presets_and_overrides() -> None:
    conservative = config.resolve_performance("conservative")
    balanced = config.resolve_performance("balanced")
    fast = config.resolve_performance("fast")
    assert conservative.chunk_size < balanced.chunk_size < fast.chunk_size
    assert conservative.jobs == balanced.jobs == 1
    overridden = config.resolve_performance("balanced", chunk_size=64 * 1024, jobs=4)
    assert (overridden.chunk_size, overridden.jobs) == (64 * 1024, 4)


def test_default_is_one_worker() -> None:
    assert config.resolve_performance().jobs == 1
    assert config.resolve_performance(None, None, None).preset == "auto"


def test_auto_falls_back_to_conservative_without_hardware_info(monkeypatch) -> None:
    monkeypatch.setattr(system_info, "cpu_count", lambda: None)
    settings = config.resolve_performance("auto")
    assert settings.resolved_preset == "conservative" and settings.jobs == 1


def test_fast_preset_never_exceeds_cpu_count(monkeypatch) -> None:
    monkeypatch.setattr(system_info, "cpu_count", lambda: 1)
    assert config.resolve_performance("fast").jobs == 1


@pytest.mark.parametrize("chunk", [0, 1, config.MIN_CHUNK_SIZE - 1, config.MAX_CHUNK_SIZE + 1])
def test_chunk_size_bounds(chunk: int) -> None:
    with pytest.raises(UsageError, match="outside the allowed range"):
        config.resolve_performance("balanced", chunk_size=chunk)


@pytest.mark.parametrize("chunk", [config.MIN_CHUNK_SIZE, config.MAX_CHUNK_SIZE])
def test_chunk_size_limits_are_inclusive(chunk: int) -> None:
    assert config.resolve_performance("balanced", chunk_size=chunk).chunk_size == chunk


@pytest.mark.parametrize("jobs", [0, -1, config.MAX_JOBS + 1])
def test_jobs_bounds(jobs: int) -> None:
    with pytest.raises(UsageError, match="jobs must be between"):
        config.resolve_performance("balanced", jobs=jobs)


def test_unknown_preset() -> None:
    with pytest.raises(UsageError, match="unknown preset"):
        config.resolve_performance("warp")


def test_load_config_values(tmp_path) -> None:
    path = tmp_path / "c.toml"
    path.write_text(
        'preset = "fast"\nchunk_size = "2MiB"\njobs = 3\n'
        'pattern = "random"\nverify = true\nmax_add = 5000\n',
        encoding="utf-8",
    )
    user = config.load_user_config(path, explicit=True)
    assert (user.preset, user.chunk_size, user.jobs, user.pattern, user.verify, user.max_add) == (
        "fast", 2 * 1024**2, 3, "random", True, 5000,
    )  # fmt: skip
    assert user.source == path


def test_default_config_location_honours_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(config.CONFIG_ENV_VAR)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert config.default_config_path() == tmp_path / "filepumper" / "config.toml"


def test_unreadable_config_is_a_config_error(tmp_path) -> None:
    with pytest.raises(ConfigError):
        config.load_user_config(tmp_path, explicit=True)  # a directory cannot be read as a file


# -------------------------------------------------------------------- info report


def test_build_info_structure(tmp_path) -> None:
    info = reporting.build_info([str(tmp_path)])
    json.dumps(info)  # must be serialisable
    assert (
        info["storage"][0]["used_bytes"]
        == info["storage"][0]["total_bytes"] - info["storage"][0]["free_bytes"]
    )
    assert info["python"]["version"]
    assert reporting.format_info(info).startswith("File Pumper ")


def test_sample_constant_is_what_tests_expect() -> None:
    assert len(SAMPLE) == 1280
