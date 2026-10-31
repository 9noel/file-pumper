from __future__ import annotations

import builtins
import os
import threading
import time
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import SAMPLE, directory_state, temp_files
from filepumper import engine
from filepumper.engine import FileStatus
from filepumper.exceptions import ValidationError
from filepumper.patterns import (
    PatternGenerator,
    PatternKind,
    RandomPattern,
    RepeatPattern,
    make_pattern,
)

# ------------------------------------------------------------------- exact amounts


@pytest.mark.parametrize("extra", [1, 100, 4095, 4096, 4097, 10_000, 123_457])
def test_add_appends_exact_amount_and_preserves_prefix(make_file, run, extra: int) -> None:
    path = make_file()
    result = run([path], add=extra)
    data = path.read_bytes()
    assert result.succeeded == 1
    assert len(data) == len(SAMPLE) + extra
    assert data[: len(SAMPLE)] == SAMPLE
    assert data[len(SAMPLE) :] == bytes(extra)
    assert result.results[0].bytes_added == extra
    assert result.results[0].final_size == len(SAMPLE) + extra


@pytest.mark.parametrize("target", [len(SAMPLE) + 1, 5000, 4096 * 3, 1_000_000])
def test_target_reaches_exact_size(make_file, run, target: int) -> None:
    path = make_file()
    result = run([path], target=target)
    assert result.succeeded == 1
    assert path.stat().st_size == target
    assert path.read_bytes()[: len(SAMPLE)] == SAMPLE
    assert result.results[0].bytes_added == target - len(SAMPLE)


def test_target_equal_to_current_size_is_a_skipped_noop(make_file, run) -> None:
    path = make_file()
    before = directory_state(path.parent)
    result = run([path], target=len(SAMPLE))
    assert result.skipped == 1 and result.succeeded == 0
    assert "target size" in (result.results[0].error or "")
    assert directory_state(path.parent) == before


def test_zero_byte_add_is_a_reported_noop(make_file, run) -> None:
    path = make_file()
    before = directory_state(path.parent)
    result = run([path], add=0)
    assert result.skipped == 1
    assert result.total_added == 0
    assert directory_state(path.parent) == before
    assert temp_files(path.parent) == []


def test_empty_file_can_be_padded(make_file, run) -> None:
    path = make_file("empty.bin", b"")
    assert run([path], add=5000).succeeded == 1
    assert path.read_bytes() == bytes(5000)


def test_empty_file_target(make_file, run) -> None:
    path = make_file("empty.bin", b"")
    assert run([path], target=777).succeeded == 1
    assert path.stat().st_size == 777


def test_never_truncates(make_file, make_options) -> None:
    path = make_file()
    before = path.read_bytes()
    with pytest.raises(ValidationError, match="never truncates"):
        engine.build_plan([str(path)], make_options(target=len(SAMPLE) - 1))
    assert path.read_bytes() == before


def test_one_oversized_target_blocks_the_whole_batch(make_file, make_options) -> None:
    small = make_file("small.bin", b"x" * 10)
    big = make_file("big.bin", b"y" * 5000)
    with pytest.raises(ValidationError) as caught:
        engine.build_plan([str(small), str(big)], make_options(target=1000))
    assert any("big.bin" in problem for problem in caught.value.problems)
    assert small.read_bytes() == b"x" * 10


# ----------------------------------------------------------------------- patterns


def test_repeat_pattern_is_written_with_final_partial_repetition(make_file, run) -> None:
    path = make_file("p.bin", b"HEAD")
    run([path], add=10, pattern=make_pattern("repeat", "DEADBEEF"))
    assert path.read_bytes() == b"HEAD" + b"\xde\xad\xbe\xef" * 2 + b"\xde\xad"


def test_repeat_pattern_across_many_chunks(make_file, run) -> None:
    path = make_file("p.bin", b"")
    run([path], add=50_001, pattern=RepeatPattern(b"\x01\x02\x03"), chunk_size=4096)
    assert path.read_bytes() == (b"\x01\x02\x03" * 20_000)[:50_001]


def test_random_pattern_has_exact_length(make_file, run) -> None:
    path = make_file()
    result = run([path], add=70_000, pattern=RandomPattern(), chunk_size=4096)
    assert result.succeeded == 1
    data = path.read_bytes()
    assert len(data) == len(SAMPLE) + 70_000
    assert data[: len(SAMPLE)] == SAMPLE
    assert data[len(SAMPLE) :] != bytes(70_000)


# ---------------------------------------------------------------- paths and batches


@pytest.mark.parametrize(
    "name",
    [
        "with space.bin",
        "ünïcödé.bin",
        "semi;colon,comma.bin",
        "quote'single.bin",
        "a b  c.tar.gz",
        "-dash.bin",
    ],
)
def test_unusual_file_names(make_file, run, name: str) -> None:
    path = make_file(name, subdir="sub dir (1)")
    assert run([path], add=64).succeeded == 1
    assert path.stat().st_size == len(SAMPLE) + 64


def test_multiple_files_are_reported_separately(make_file, run) -> None:
    paths = [make_file(f"f{i}.bin", bytes([i]) * (100 * (i + 1))) for i in range(4)]
    result = run(paths, add=1000)
    assert [item.status for item in result.results] == [FileStatus.SUCCESS] * 4
    assert result.total_added == 4000
    for index, path in enumerate(paths):
        assert path.read_bytes() == bytes([index]) * (100 * (index + 1)) + bytes(1000)
        assert result.results[index].path == str(path)


def test_target_applies_per_file(make_file, run) -> None:
    a, b = make_file("a.bin", b"a" * 10), make_file("b.bin", b"b" * 900)
    result = run([a, b], target=1000)
    assert a.stat().st_size == b.stat().st_size == 1000
    assert [item.bytes_added for item in result.results] == [990, 100]


def test_missing_file_is_rejected_before_anything_is_modified(
    make_file, make_options, tmp_path
) -> None:
    good = make_file("good.bin")
    before = directory_state(tmp_path)
    with pytest.raises(ValidationError) as caught:
        engine.build_plan([str(good), str(tmp_path / "missing.bin")], make_options(add=10))
    assert "missing.bin" in str(caught.value)
    assert "does not exist" in str(caught.value)
    assert directory_state(tmp_path) == before


def test_directory_is_rejected_and_not_scanned(make_file, make_options, tmp_path) -> None:
    make_file("inside.bin", subdir="folder")
    with pytest.raises(ValidationError, match="is a directory"):
        engine.build_plan([str(tmp_path / "folder")], make_options(add=10))
    assert (tmp_path / "folder" / "inside.bin").read_bytes() == SAMPLE


def test_all_problems_are_reported_together(make_file, make_options, tmp_path) -> None:
    (tmp_path / "d").mkdir()
    with pytest.raises(ValidationError) as caught:
        engine.build_plan([str(tmp_path / "d"), str(tmp_path / "nope")], make_options(add=1))
    assert len(caught.value.problems) == 2


def test_no_paths_is_rejected(make_options) -> None:
    with pytest.raises(ValidationError, match="no file paths"):
        engine.build_plan([], make_options(add=1))


def test_same_file_twice_is_rejected(make_file, make_options) -> None:
    path = make_file()
    with pytest.raises(ValidationError, match="same file"):
        engine.build_plan([str(path), str(path)], make_options(add=1))
    with pytest.raises(ValidationError, match="same file"):
        engine.build_plan([str(path), str(path.parent / "." / path.name)], make_options(add=1))


@pytest.mark.skipif(os.name == "nt", reason="special files are POSIX-specific")
def test_special_files_are_rejected(make_options) -> None:
    with pytest.raises(ValidationError, match="not a regular file"):
        engine.build_plan(["/dev/null"], make_options(add=1))


def test_max_add_limit(make_file, make_options) -> None:
    path = make_file()
    with pytest.raises(ValidationError, match="per-file limit"):
        engine.build_plan([str(path)], make_options(add=2000, max_add=1000))
    plan = engine.build_plan([str(path)], make_options(add=1000, max_add=1000))
    assert plan.total_add == 1000
    unlimited = engine.build_plan([str(path)], make_options(add=10**12, max_add=None))
    assert unlimited.total_add == 10**12


def test_resulting_size_overflow_is_rejected(make_file, make_options) -> None:
    path = make_file()
    with pytest.raises(ValidationError, match="exceed"):
        engine.build_plan([str(path)], make_options(add=2**63 - 1, max_add=None))


# ------------------------------------------------------------------------ dry run


def test_planning_and_reporting_perform_no_writes(
    make_file, make_options, tmp_path, monkeypatch
) -> None:
    from filepumper import reporting

    a, b = make_file("a.bin"), make_file("b.bin", b"zz")
    before = directory_state(tmp_path)
    atimes = {p: p.stat().st_atime_ns for p in (a, b)}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a dry run must not create files or write data")

    monkeypatch.setattr(engine.tempfile, "mkstemp", forbidden)
    monkeypatch.setattr(engine, "_atomic_replace", forbidden)
    monkeypatch.setattr(engine, "_run_transaction", forbidden)

    plan = engine.build_plan([str(a), str(b)], make_options(add=10_000_000))
    space = engine.plan_space(plan)
    text = reporting.format_plan(plan, space, dry_run=True)
    report = reporting.build_report(plan, None, space, dry_run=True)

    assert "DRY RUN" in text
    assert report["dry_run"] is True
    assert report["files"][0]["status"] == "planned"
    assert directory_state(tmp_path) == before
    assert {p: p.stat().st_atime_ns for p in (a, b)} == atimes


# ------------------------------------------------------------------ verification


def test_verification_succeeds(make_file, run) -> None:
    path = make_file()
    result = run([path], add=20_000, verify=True, pattern=RandomPattern())
    assert result.succeeded == 1
    assert result.results[0].verified is True


def test_verification_failure_keeps_original_and_cleans_up(make_file, run, monkeypatch) -> None:
    path = make_file()
    real_fsync = engine._fsync

    def corrupt_then_fsync(handle) -> None:
        real_fsync(handle)
        with open(next(path.parent.glob("*" + engine.TEMP_SUFFIX)), "r+b") as victim:
            victim.write(b"\xff")  # alter the first byte of the staged copy

    monkeypatch.setattr(engine, "_fsync", corrupt_then_fsync)
    result = run([path], add=5000, verify=True)
    item = result.results[0]
    assert item.status is FileStatus.FAILED
    assert item.error_type == "VerificationError"
    assert "prefix" in (item.error or "")
    assert path.read_bytes() == SAMPLE
    assert temp_files(path.parent) == []


def test_verification_detects_wrong_payload(make_file, run, monkeypatch) -> None:
    path = make_file()
    real_fsync = engine._fsync

    def corrupt_tail(handle) -> None:
        real_fsync(handle)
        with open(next(path.parent.glob("*" + engine.TEMP_SUFFIX)), "r+b") as victim:
            victim.seek(-1, os.SEEK_END)
            victim.write(b"\x01")

    monkeypatch.setattr(engine, "_fsync", corrupt_tail)
    result = run([path], add=5000, verify=True)
    assert result.results[0].status is FileStatus.FAILED
    assert "appended bytes differ" in (result.results[0].error or "")
    assert path.read_bytes() == SAMPLE


def test_verification_detects_wrong_size(make_file, run, monkeypatch) -> None:
    path = make_file()
    real_fsync = engine._fsync

    def grow(handle) -> None:
        real_fsync(handle)
        with open(next(path.parent.glob("*" + engine.TEMP_SUFFIX)), "ab") as victim:
            victim.write(b"extra")

    monkeypatch.setattr(engine, "_fsync", grow)
    result = run([path], add=5000, verify=True)
    assert result.results[0].status is FileStatus.FAILED
    assert "size mismatch" in (result.results[0].error or "")
    assert path.read_bytes() == SAMPLE


def test_without_verify_the_flag_is_not_reported(make_file, run) -> None:
    result = run([make_file()], add=10)
    assert result.results[0].verified is None


# ---------------------------------------------------------- failures and cleanup


class FailingPattern(PatternGenerator):
    """Emits some good chunks and then raises."""

    kind = PatternKind.ZERO

    def __init__(self, good_chunks: int, exc: Exception) -> None:
        self.good_chunks = good_chunks
        self.exc = exc

    def chunks(self, total: int, chunk_size: int) -> Iterator[bytes]:
        for _ in range(self.good_chunks):
            yield bytes(chunk_size)
        raise self.exc

    def describe(self) -> str:
        return "failing test pattern"


def test_generator_exception_preserves_original_and_removes_temp(make_file, run) -> None:
    path = make_file()
    result = run([path], add=100_000, pattern=FailingPattern(3, RuntimeError("boom")))
    item = result.results[0]
    assert item.status is FileStatus.FAILED
    assert "boom" in (item.error or "")
    assert "original file is unchanged" in (item.error or "")
    assert path.read_bytes() == SAMPLE
    assert temp_files(path.parent) == []
    assert result.failed == 1 and result.total_added == 0


def test_generator_that_stops_early_is_detected(make_file, run) -> None:
    class Short(FailingPattern):
        def chunks(self, total, chunk_size):
            yield bytes(10)

    path = make_file()
    result = run([path], add=5000, pattern=Short(0, RuntimeError()))
    assert result.results[0].status is FileStatus.FAILED
    assert "produced 10 bytes" in (result.results[0].error or "")
    assert path.read_bytes() == SAMPLE


def test_generator_that_overproduces_is_detected(make_file, run) -> None:
    class Greedy(FailingPattern):
        def chunks(self, total, chunk_size):
            yield bytes(total + 1)

    path = make_file()
    result = run([path], add=5000, pattern=Greedy(0, RuntimeError()))
    assert result.results[0].status is FileStatus.FAILED
    assert "more than" in (result.results[0].error or "")
    assert path.read_bytes() == SAMPLE


def test_generator_empty_chunk_is_detected(make_file, run) -> None:
    class Stalling(FailingPattern):
        def chunks(self, total, chunk_size):
            yield b""

    path = make_file()
    result = run([path], add=5000, pattern=Stalling(0, RuntimeError()))
    assert "empty chunk" in (result.results[0].error or "")
    assert path.read_bytes() == SAMPLE


def test_disk_full_during_write(make_file, run, monkeypatch) -> None:
    path = make_file()

    def full(_handle) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(engine, "_fsync", full)
    result = run([path], add=5000)
    item = result.results[0]
    assert item.status is FileStatus.FAILED
    assert "no space left" in (item.error or "")
    assert path.read_bytes() == SAMPLE
    assert temp_files(path.parent) == []


def test_replacement_failure_preserves_original(make_file, run, monkeypatch) -> None:
    path = make_file()

    def denied(_source, _destination) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(engine, "_atomic_replace", denied)
    result = run([path], add=5000)
    item = result.results[0]
    assert item.status is FileStatus.FAILED
    assert item.error_type == "WriteError"
    assert "could not replace" in (item.error or "")
    assert path.read_bytes() == SAMPLE
    assert temp_files(path.parent) == []


def test_failed_replacement_with_backup_leaves_no_backup(make_file, run, monkeypatch) -> None:
    path = make_file()

    def denied(_source, _destination) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(engine, "_atomic_replace", denied)
    result = run([path], add=100, backup=True)
    assert result.failed == 1
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]
    assert path.read_bytes() == SAMPLE


def test_unexpected_exception_is_contained_per_file(make_file, run, monkeypatch) -> None:
    a, b = make_file("a.bin"), make_file("b.bin")
    real = engine._atomic_replace

    def flaky(source: str, destination: str) -> None:
        if destination.endswith("a.bin"):
            raise ZeroDivisionError("simulated bug")
        real(source, destination)

    monkeypatch.setattr(engine, "_atomic_replace", flaky)
    result = run([a, b], add=100)
    assert [item.status for item in result.results] == [FileStatus.FAILED, FileStatus.SUCCESS]
    assert "ZeroDivisionError" in (result.results[0].error or "")
    assert a.read_bytes() == SAMPLE
    assert b.stat().st_size == len(SAMPLE) + 100
    assert temp_files(a.parent) == []


def test_partial_batch_failure_leaves_committed_files_committed(
    make_file, run, monkeypatch
) -> None:
    a, b, c = (make_file(f"{n}.bin") for n in "abc")
    real = engine._atomic_replace

    def fail_on_b(source: str, destination: str) -> None:
        if destination.endswith("b.bin"):
            raise OSError(5, "Input/output error")
        real(source, destination)

    monkeypatch.setattr(engine, "_atomic_replace", fail_on_b)
    result = run([a, b, c], add=10)
    assert [item.status for item in result.results] == [
        FileStatus.SUCCESS,
        FileStatus.FAILED,
        FileStatus.SUCCESS,
    ]
    assert a.stat().st_size == len(SAMPLE) + 10
    assert b.read_bytes() == SAMPLE
    assert c.stat().st_size == len(SAMPLE) + 10


def test_undeletable_temp_file_is_reported(make_file, run, monkeypatch) -> None:
    path = make_file()
    monkeypatch.setattr(
        engine, "_atomic_replace", lambda *_: (_ for _ in ()).throw(OSError(5, "io"))
    )
    real_unlink = os.unlink

    def stubborn(target, *args, **kwargs):
        if str(target).endswith(engine.TEMP_SUFFIX):
            raise OSError(16, "Device or resource busy")
        return real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(engine.os, "unlink", stubborn)
    result = run([path], add=10)
    assert result.results[0].leftover_temp is not None
    assert result.leftover_temps == [result.results[0].leftover_temp]
    monkeypatch.undo()
    for leftover in temp_files(path.parent):
        leftover.unlink()


# ------------------------------------------------------- concurrent modification


def test_source_modified_before_commit_aborts(make_file, run, monkeypatch) -> None:
    path = make_file()
    real_fsync = engine._fsync

    def modify_source_then_fsync(handle) -> None:
        real_fsync(handle)
        with open(path, "ab") as source:
            source.write(b"NEWER USER DATA")

    monkeypatch.setattr(engine, "_fsync", modify_source_then_fsync)
    result = run([path], add=500)
    item = result.results[0]
    assert item.status is FileStatus.FAILED
    assert item.error_type == "SourceModifiedError"
    assert "changed during the operation" in (item.error or "")
    assert path.read_bytes() == SAMPLE + b"NEWER USER DATA"  # the newer data survives
    assert temp_files(path.parent) == []


def test_source_touched_without_size_change_aborts(make_file, run, monkeypatch) -> None:
    path = make_file()
    real_fsync = engine._fsync

    def touch(handle) -> None:
        real_fsync(handle)
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    monkeypatch.setattr(engine, "_fsync", touch)
    result = run([path], add=500)
    assert result.results[0].error_type == "SourceModifiedError"
    assert "modification time" in (result.results[0].error or "")
    assert path.read_bytes() == SAMPLE


def test_source_replaced_by_another_file_aborts(make_file, run, monkeypatch) -> None:
    path = make_file()
    real_fsync = engine._fsync

    def swap(handle) -> None:
        real_fsync(handle)
        replacement = path.with_name("replacement.bin")
        replacement.write_bytes(b"someone else's file")
        os.replace(replacement, path)

    monkeypatch.setattr(engine, "_fsync", swap)
    result = run([path], add=500)
    assert result.results[0].status is FileStatus.FAILED
    assert path.read_bytes() == b"someone else's file"
    assert temp_files(path.parent) == []


def test_source_deleted_aborts(make_file, run, monkeypatch) -> None:
    path = make_file()
    real_fsync = engine._fsync

    def delete(handle) -> None:
        real_fsync(handle)
        path.unlink()

    monkeypatch.setattr(engine, "_fsync", delete)
    result = run([path], add=500)
    assert result.results[0].error_type == "SourceModifiedError"
    assert not path.exists()
    assert temp_files(path.parent) == []


# ---------------------------------------------------------- cancellation (Ctrl+C)


class SlowPattern(PatternGenerator):
    """Produces many small chunks slowly, signalling when the first one is out."""

    kind = PatternKind.ZERO

    def __init__(self, started: threading.Event, delay: float = 0.005) -> None:
        self.started = started
        self.delay = delay

    def chunks(self, total: int, chunk_size: int) -> Iterator[bytes]:
        produced = 0
        while produced < total:
            count = min(chunk_size, total - produced)
            yield bytes(count)
            produced += count
            self.started.set()
            time.sleep(self.delay)

    def describe(self) -> str:
        return "slow test pattern"


def test_cancel_event_stops_work_and_cleans_up(make_file, make_options) -> None:
    paths = [make_file(f"{i}.bin") for i in range(3)]
    started, cancel = threading.Event(), threading.Event()
    options = make_options(add=4096 * 2000, pattern=SlowPattern(started), max_add=None)
    plan = engine.build_plan([str(p) for p in paths], options)

    def trigger() -> None:
        started.wait(5)
        cancel.set()

    threading.Thread(target=trigger, daemon=True).start()
    result = engine.run_batch(plan, cancel=cancel)
    assert result.interrupted
    assert {item.status for item in result.results} == {FileStatus.CANCELLED}
    assert all(p.read_bytes() == SAMPLE for p in paths)
    assert temp_files(paths[0].parent) == []


def test_keyboard_interrupt_stops_scheduling_and_cleans_up(make_file, make_options) -> None:
    paths = [make_file(f"{i}.bin") for i in range(4)]
    started = threading.Event()
    options = make_options(add=4096 * 2000, pattern=SlowPattern(started), max_add=None)
    plan = engine.build_plan([str(p) for p in paths], options)

    def interrupt() -> None:
        if started.is_set():
            raise KeyboardInterrupt

    result = engine.run_batch(plan, on_tick=interrupt, tick_interval=0.01)
    assert result.interrupted
    assert len(result.results) == 4
    assert all(item.status is FileStatus.CANCELLED for item in result.results)
    assert all(p.read_bytes() == SAMPLE for p in paths)
    assert temp_files(paths[0].parent) == []
    snapshot = engine.ProgressTracker(plan).snapshot()
    assert len(snapshot.files) == 4


def test_interrupt_distinguishes_completed_from_interrupted(make_file, make_options) -> None:
    quick = make_file("quick.bin")
    slow = make_file("slow.bin")
    started = threading.Event()

    class Mixed(SlowPattern):
        def chunks(self, total, chunk_size):
            if total == 100:  # the fast file finishes immediately
                yield bytes(100)
                return
            yield from super().chunks(total, chunk_size)

    options = make_options(add=100, pattern=Mixed(started))
    plan = engine.build_plan([str(quick)], options)
    first = engine.run_batch(plan)
    assert first.succeeded == 1

    options = make_options(add=4096 * 2000, pattern=Mixed(started), max_add=None)
    plan = engine.build_plan([str(slow)], options)

    def interrupt() -> None:
        if started.is_set():
            raise KeyboardInterrupt

    second = engine.run_batch(plan, on_tick=interrupt, tick_interval=0.01)
    assert second.results[0].status is FileStatus.CANCELLED
    assert slow.read_bytes() == SAMPLE
    assert quick.stat().st_size == len(SAMPLE) + 100


# ------------------------------------------------------------------ concurrency


class CountingPattern(PatternGenerator):
    """Wraps zero padding and records how many files are being written at once."""

    kind = PatternKind.ZERO

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def chunks(self, total: int, chunk_size: int) -> Iterator[bytes]:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            produced = 0
            while produced < total:
                count = min(chunk_size, total - produced)
                time.sleep(0.002)
                yield bytes(count)
                produced += count
        finally:
            with self.lock:
                self.active -= 1

    def describe(self) -> str:
        return "counting test pattern"


def test_default_is_a_single_worker(make_file, run) -> None:
    paths = [make_file(f"{i}.bin") for i in range(3)]
    counter = CountingPattern()
    result = run(paths, add=4096 * 3, pattern=counter)
    assert result.succeeded == 3
    assert counter.peak == 1


def test_parallel_jobs_are_bounded_and_files_stay_correct(make_file, run) -> None:
    paths = [make_file(f"{i}.bin", bytes([i + 1]) * 3000) for i in range(6)]
    counter = CountingPattern()
    result = run(paths, add=4096 * 6, pattern=counter, jobs=3)
    assert result.succeeded == 6
    assert 1 <= counter.peak <= 3
    for index, path in enumerate(paths):
        assert path.read_bytes() == bytes([index + 1]) * 3000 + bytes(4096 * 6)


def test_effective_jobs_never_exceeds_files_to_write(make_file, make_options) -> None:
    path = make_file()
    plan = engine.build_plan([str(path)], make_options(add=10, jobs=8))
    assert plan.effective_jobs == 1
    plan = engine.build_plan([str(path)], make_options(add=0, jobs=8))
    assert plan.effective_jobs == 1 and plan.total_add == 0


def test_one_future_per_file_not_per_chunk(make_file, make_options, monkeypatch) -> None:
    submitted: list[int] = []
    real_submit = engine.ThreadPoolExecutor.submit

    def counting_submit(self, fn, *args, **kwargs):
        submitted.append(1)
        return real_submit(self, fn, *args, **kwargs)

    monkeypatch.setattr(engine.ThreadPoolExecutor, "submit", counting_submit)
    path = make_file()
    plan = engine.build_plan([str(path)], make_options(add=4096 * 500, chunk_size=4096))
    engine.run_batch(plan)
    assert len(submitted) == 1


# --------------------------------------------------------- progress and memory


def test_progress_reaches_total_without_and_with_verify(make_file, make_options) -> None:
    for verify, factor in ((False, 1), (True, 2)):
        path = make_file(f"v{verify}.bin")
        plan = engine.build_plan([str(path)], make_options(add=10_000, verify=verify))
        tracker = engine.ProgressTracker(plan)
        engine.run_batch(plan, tracker=tracker)
        snap = tracker.snapshot()
        expected = (len(SAMPLE) + 10_000) * factor
        assert snap.total == snap.done == expected
        assert snap.files[0].phase == engine.PHASE_DONE
        assert snap.files[0].payload_written == 10_000


def test_progress_is_not_done_before_commit(make_file, make_options, monkeypatch) -> None:
    path = make_file()
    plan = engine.build_plan([str(path)], make_options(add=5000))
    tracker = engine.ProgressTracker(plan)
    phases: list[str] = []
    real = engine._atomic_replace

    def watch(source: str, destination: str) -> None:
        phases.append(tracker.snapshot().files[0].phase)
        real(source, destination)

    monkeypatch.setattr(engine, "_atomic_replace", watch)
    engine.run_batch(plan, tracker=tracker)
    assert phases == [engine.PHASE_COMMITTING]
    assert tracker.snapshot().files[0].phase == engine.PHASE_DONE


def test_failed_file_never_reports_done(make_file, make_options) -> None:
    path = make_file()
    plan = engine.build_plan(
        [str(path)], make_options(add=5000, pattern=FailingPattern(0, RuntimeError("x")))
    )
    tracker = engine.ProgressTracker(plan)
    engine.run_batch(plan, tracker=tracker)
    assert tracker.snapshot().files[0].phase == engine.PHASE_FAILED


def test_memory_stays_bounded_for_a_large_payload(make_file, make_options) -> None:
    path = make_file()
    payload = 24 * 1024 * 1024
    plan = engine.build_plan([str(path)], make_options(add=payload, chunk_size=64 * 1024))
    tracemalloc.start()
    try:
        result = engine.run_batch(plan)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.succeeded == 1
    assert path.stat().st_size == len(SAMPLE) + payload
    assert peak < 2 * 1024 * 1024, f"peak {peak} bytes grows with the payload"
    path.unlink()


def test_large_files_are_copied_in_bounded_chunks(make_file, make_options, monkeypatch) -> None:
    path = make_file("big.bin", os.urandom(300_000))
    original = path.read_bytes()
    sizes: list[int] = []
    real = builtins.open

    class Spy:
        def __init__(self, handle):
            self._handle = handle

        def readinto(self, buffer):
            count = self._handle.readinto(buffer)
            sizes.append(count)
            return count

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

    def spying_open(file, mode="r", *args, **kwargs):
        handle = real(file, mode, *args, **kwargs)
        # Only the engine's unbuffered binary read of the source file is observed.
        return Spy(handle) if mode == "rb" and kwargs.get("buffering") == 0 else handle

    monkeypatch.setattr(engine, "open", spying_open, raising=False)
    plan = engine.build_plan([str(path)], make_options(add=10, chunk_size=4096))
    engine.run_batch(plan)
    assert len(sizes) > 300_000 // 4096  # the file really was read piecemeal
    assert max(sizes) <= 4096
    assert path.read_bytes()[:300_000] == original


# --------------------------------------------------------------------- metadata


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_permissions_are_preserved(make_file, run) -> None:
    path = make_file()
    path.chmod(0o640)
    assert run([path], add=100).succeeded == 1
    assert path.stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_executable_bit_is_preserved(make_file, run) -> None:
    path = make_file()
    path.chmod(0o755)
    run([path], add=100)
    assert path.stat().st_mode & 0o777 == 0o755


# ----------------------------------------------------------------------- backup


def test_backup_keeps_the_original_content(make_file, run) -> None:
    path = make_file()
    result = run([path], add=100, backup=True)
    backup = Path(result.results[0].backup_path or "")
    assert backup.name == "sample.bin.bak"
    assert backup.read_bytes() == SAMPLE
    assert path.stat().st_size == len(SAMPLE) + 100


@pytest.mark.skipif(os.name == "nt", reason="hard-link backup semantics are checked on POSIX")
def test_backup_names_never_collide(make_file, run) -> None:
    path = make_file()
    run([path], add=10, backup=True)
    second = run([path], add=10, backup=True)
    third = run([path], add=10, backup=True)
    assert Path(second.results[0].backup_path or "").name == "sample.bin.bak.1"
    assert Path(third.results[0].backup_path or "").name == "sample.bin.bak.2"
    assert (path.parent / "sample.bin.bak").read_bytes() == SAMPLE
    assert (path.parent / "sample.bin.bak.1").read_bytes() == SAMPLE + bytes(10)


def test_existing_backup_name_is_never_overwritten(make_file, run) -> None:
    path = make_file()
    (path.parent / "sample.bin.bak").write_bytes(b"precious")
    result = run([path], add=10, backup=True)
    assert (path.parent / "sample.bin.bak").read_bytes() == b"precious"
    assert Path(result.results[0].backup_path or "").read_bytes() == SAMPLE


def test_backup_space_estimate_counts_the_extra_copy(make_file, make_options) -> None:
    path = make_file()
    with_backup = engine.plan_space(
        engine.build_plan([str(path)], make_options(add=1000, backup=True))
    )
    without = engine.plan_space(engine.build_plan([str(path)], make_options(add=1000)))
    assert with_backup.total_required == len(SAMPLE) + 1000
    assert without.total_required == 1000 + len(SAMPLE)
    two = [make_file("x.bin"), make_file("y.bin")]
    many = engine.plan_space(
        engine.build_plan([str(p) for p in two], make_options(add=1000, backup=True))
    )
    assert many.total_required == 2 * (len(SAMPLE) + 1000)
