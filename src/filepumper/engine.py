"""Planning and transactional execution of padding operations.

The write path for each file is:

1. snapshot the source file's identity (done while planning);
2. create a temporary file in the destination directory;
3. stream-copy the original bytes into it, then stream the payload after them;
4. flush and ``fsync`` the temporary file, then close it;
5. optionally verify it by streaming it back;
6. confirm the source file did not change in the meantime;
7. atomically replace the original pathname with the temporary file.

Any failure before step 7 removes the temporary file and leaves the original
untouched. A file is reported as successful only after step 7 has returned.

Planning (:func:`build_plan`) only reads metadata, so a dry run is simply a plan
that is never executed.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import StrEnum
from typing import BinaryIO

from . import formats, safety
from . import marker as marker_mod
from .config import DEFAULT_MAX_ADD, PerformanceSettings
from .exceptions import (
    FilePumperError,
    OperationCancelledError,
    SourceModifiedError,
    ValidationError,
    VerificationError,
    WriteError,
)
from .patterns import PatternGenerator, PatternKind
from .safety import FileIdentity, SpaceEntry, SpaceReport
from .sizes import MAX_SIZE_BYTES, format_size

log = logging.getLogger(__name__)

TEMP_SUFFIX = ".filepumper.tmp"

PHASE_QUEUED = "queued"
PHASE_COPYING = "copying"
PHASE_APPENDING = "appending"
PHASE_VERIFYING = "verifying"
PHASE_COMMITTING = "committing"
PHASE_DONE = "done"
PHASE_SKIPPED = "skipped"
PHASE_FAILED = "failed"
PHASE_CANCELLED = "cancelled"
TERMINAL_PHASES = frozenset({PHASE_DONE, PHASE_SKIPPED, PHASE_FAILED, PHASE_CANCELLED})


class Mode(StrEnum):
    """How the requested size is interpreted."""

    ADD = "add"
    TARGET = "target"


class FileStatus(StrEnum):
    """Final outcome of one file."""

    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ------------------------------------------------------------------------------ plan


@dataclass(frozen=True)
class PadOptions:
    """Everything that defines a padding run, independent of the command line."""

    mode: Mode
    size: int
    pattern: PatternGenerator
    settings: PerformanceSettings
    verify: bool = False
    backup: bool = False
    follow_symlinks: bool = False
    allow_hardlinks: bool = False
    #: Per-file cap on the addition; ``None`` disables the cap.
    max_add: int | None = DEFAULT_MAX_ADD
    #: Write a small provenance footer (see :mod:`filepumper.marker`), consuming part
    #: of the requested padding so the exact size requested is still what you get.
    mark: bool = False


@dataclass(frozen=True)
class FilePlan:
    """The planned outcome for one file."""

    path: str
    dest: str
    identity: FileIdentity
    original_size: int
    add_bytes: int
    final_size: int
    format: formats.FormatInfo
    skip_reason: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def will_write(self) -> bool:
        return self.skip_reason is None and self.add_bytes > 0

    @property
    def directory(self) -> str:
        return os.path.dirname(os.path.abspath(self.dest))


@dataclass(frozen=True)
class BatchPlan:
    """A validated plan for a whole invocation."""

    files: tuple[FilePlan, ...]
    options: PadOptions

    @property
    def effective_jobs(self) -> int:
        writers = sum(1 for item in self.files if item.will_write)
        return max(1, min(self.options.settings.jobs, writers))

    @property
    def total_add(self) -> int:
        return sum(item.add_bytes for item in self.files if item.will_write)

    @property
    def writes(self) -> tuple[FilePlan, ...]:
        return tuple(item for item in self.files if item.will_write)


def build_plan(paths: list[str], options: PadOptions) -> BatchPlan:
    """Validate every path and the requested size before anything is modified.

    Raises :class:`ValidationError` listing *all* problems found.
    """
    if not paths:
        raise ValidationError("no file paths were supplied")
    validated, problems = safety.inspect_files(
        paths, follow_symlinks=options.follow_symlinks, allow_hardlinks=options.allow_hardlinks
    )
    plans: list[FilePlan] = []
    for item in validated:
        original = item.identity.size
        if options.mode is Mode.ADD:
            add = options.size
        else:
            add = options.size - original
            if add < 0:
                problems.append(
                    f"{item.path}: current size {format_size(original)} is larger than the "
                    f"target {format_size(options.size)}; File Pumper never truncates files"
                )
                continue
        if original + add > MAX_SIZE_BYTES:
            problems.append(
                f"{item.path}: the resulting size would exceed {MAX_SIZE_BYTES:,} bytes"
            )
            continue
        if options.max_add is not None and add > options.max_add:
            problems.append(
                f"{item.path}: adding {format_size(add)} exceeds the per-file limit of "
                f"{format_size(options.max_add)} (raise it with --max-add or lift it with "
                "--allow-large)"
            )
            continue
        if options.mark and 0 < add < marker_mod.SIZE:
            problems.append(
                f"{item.path}: --mark needs at least {format_size(marker_mod.SIZE)} of "
                f"padding (requested {format_size(add)})"
            )
            continue
        if options.pattern.kind is PatternKind.BLEND and original == 0 and add > 0:
            problems.append(
                f"{item.path}: is empty, so the blend pattern has no content to draw from "
                "(use zero, random or repeat instead)"
            )
            continue
        skip_reason = None
        if add == 0:
            skip_reason = (
                "nothing to add (zero bytes requested)"
                if options.mode is Mode.ADD
                else "already at the requested target size"
            )
        format_info = formats.analyze(item.dest)
        extra_warnings: tuple[str, ...] = ()
        if format_info.kind is formats.FormatKind.UNKNOWN:
            # Content-sniffing found nothing recognized; fall back to the naive,
            # extension-only guess so a plain '.pdf' still gets a factual heads-up.
            guess = safety.format_warning(item.dest)
            if guess:
                extra_warnings = (guess,)
        elif not format_info.append_is_structure_safe:
            extra_warnings = (
                f"{item.path}: looks like a {format_info.label} but its structure "
                f"{format_info.note}; File Pumper cannot confirm appending stays safe for it",
            )
        plans.append(
            FilePlan(
                path=item.path,
                dest=item.dest,
                identity=item.identity,
                original_size=original,
                add_bytes=add,
                final_size=original + add,
                format=format_info,
                skip_reason=skip_reason,
                warnings=tuple(item.warnings) + extra_warnings,
            )
        )
    if problems:
        raise ValidationError(problems)
    return BatchPlan(files=tuple(plans), options=options)


def plan_space(plan: BatchPlan) -> SpaceReport:
    """Run the read-only free-space preflight for a plan."""
    entries = [
        SpaceEntry(item.directory, item.original_size, item.add_bytes) for item in plan.writes
    ]
    return safety.check_free_space(
        entries, jobs=plan.effective_jobs, keep_backup=plan.options.backup
    )


# ------------------------------------------------------------------------ progress


@dataclass(frozen=True)
class FileProgress:
    """Immutable view of one file's progress."""

    index: int
    path: str
    total: int
    done: int
    payload_written: int
    phase: str


@dataclass(frozen=True)
class ProgressSnapshot:
    """Immutable view of the progress of the whole batch."""

    files: tuple[FileProgress, ...]
    elapsed: float

    @property
    def total(self) -> int:
        return sum(item.total for item in self.files)

    @property
    def done(self) -> int:
        return sum(item.done for item in self.files)


class ProgressTracker:
    """Thread-safe progress aggregation. Workers write; the main thread reads."""

    def __init__(self, plan: BatchPlan, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._started = clock()
        factor = 2 if plan.options.verify else 1
        self._paths = [item.path for item in plan.files]
        self._totals = [item.final_size * factor if item.will_write else 0 for item in plan.files]
        self._done = [0] * len(plan.files)
        self._payload = [0] * len(plan.files)
        self._phases = [PHASE_QUEUED] * len(plan.files)

    def set_phase(self, index: int, phase: str) -> None:
        with self._lock:
            self._phases[index] = phase

    def advance(self, index: int, count: int, *, payload: bool = False) -> None:
        with self._lock:
            self._done[index] += count
            if payload:
                self._payload[index] += count

    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            files = tuple(
                FileProgress(
                    index=i,
                    path=self._paths[i],
                    total=self._totals[i],
                    done=self._done[i],
                    payload_written=self._payload[i],
                    phase=self._phases[i],
                )
                for i in range(len(self._paths))
            )
        return ProgressSnapshot(files=files, elapsed=self._clock() - self._started)


# ---------------------------------------------------------------------------- results


@dataclass
class FileResult:
    """What happened to one file."""

    path: str
    status: FileStatus
    original_size: int
    requested_add: int
    final_size: int | None = None
    bytes_added: int = 0
    elapsed: float = 0.0
    verified: bool | None = None
    backup_path: str | None = None
    leftover_temp: str | None = None
    error: str | None = None
    error_type: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class BatchResult:
    """Outcome of a whole run."""

    results: list[FileResult]
    elapsed: float
    interrupted: bool = False

    def count(self, status: FileStatus) -> int:
        return sum(1 for item in self.results if item.status is status)

    @property
    def succeeded(self) -> int:
        return self.count(FileStatus.SUCCESS)

    @property
    def skipped(self) -> int:
        return self.count(FileStatus.SKIPPED)

    @property
    def failed(self) -> int:
        return self.count(FileStatus.FAILED)

    @property
    def cancelled(self) -> int:
        return self.count(FileStatus.CANCELLED)

    @property
    def total_added(self) -> int:
        return sum(item.bytes_added for item in self.results)

    @property
    def leftover_temps(self) -> list[str]:
        return [item.leftover_temp for item in self.results if item.leftover_temp]


# ---------------------------------------------------------------- low-level helpers
# `_fsync` and `_atomic_replace` are separate functions so tests can simulate
# storage failures at exactly these two points.


def _fsync(handle: BinaryIO) -> None:
    os.fsync(handle.fileno())


def _atomic_replace(source: str, destination: str) -> None:
    os.replace(source, destination)


def _fsync_directory(directory: str) -> None:
    """Best-effort durability for the rename itself (POSIX only)."""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        log.debug("directory fsync failed for %s: %s", directory, exc)
    finally:
        os.close(fd)


def _raise_if_cancelled(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise OperationCancelledError("cancelled before the file was committed")


def _temp_prefix(dest: str) -> str:
    return f".{os.path.basename(dest)[:64]}."


def _describe_os_error(exc: OSError) -> str:
    if exc.errno == errno.ENOSPC:
        return "no space left on the device"
    if exc.errno == errno.EDQUOT:  # pragma: no cover - platform dependent
        return "disk quota exceeded"
    return exc.strerror or str(exc)


def _copy_original(
    source: str,
    out: BinaryIO,
    size: int,
    chunk_size: int,
    digest: hashlib._Hash | None,
    report: Callable[[int], None],
    cancel: threading.Event,
) -> None:
    """Stream exactly ``size`` bytes from ``source`` into ``out`` using one reused buffer."""
    buffer = bytearray(max(1, min(chunk_size, size)))
    view = memoryview(buffer)
    copied = 0
    with open(source, "rb", buffering=0) as handle:
        while copied < size:
            _raise_if_cancelled(cancel)
            wanted = min(len(buffer), size - copied)
            got = handle.readinto(view[:wanted])
            if not got:
                raise SourceModifiedError(f"{source} became shorter while it was being copied")
            chunk = view[:got]
            out.write(chunk)
            if digest is not None:
                digest.update(chunk)
            copied += got
            report(got)


def _write_payload(
    out: BinaryIO,
    total: int,
    pattern: PatternGenerator,
    chunk_size: int,
    digest: hashlib._Hash | None,
    report: Callable[[int], None],
    cancel: threading.Event,
) -> None:
    """Stream exactly ``total`` payload bytes from the pattern generator into ``out``."""
    iterator = iter(pattern.chunks(total, chunk_size))
    written = 0
    while True:
        try:
            chunk = next(iterator)
        except StopIteration:
            break
        except FilePumperError:
            raise
        except Exception as exc:  # a third-party generator may fail in any way
            raise WriteError(f"data generator failed: {type(exc).__name__}: {exc}") from exc
        _raise_if_cancelled(cancel)
        count = memoryview(chunk).nbytes
        if count == 0:
            raise WriteError("data generator produced an empty chunk")
        if written + count > total:
            raise WriteError(f"data generator produced more than the requested {total} bytes")
        out.write(chunk)
        if digest is not None:
            digest.update(chunk)
        written += count
        report(count)
    if written != total:
        raise WriteError(f"data generator produced {written} bytes but {total} were requested")


def _hash_range(
    handle: BinaryIO,
    count: int,
    chunk_size: int,
    report: Callable[[int], None],
    cancel: threading.Event,
) -> tuple[hashlib._Hash, int]:
    """Hash up to ``count`` bytes from the current position; return digest and bytes read."""
    digest = hashlib.sha256()
    buffer = bytearray(max(1, min(chunk_size, count)))
    view = memoryview(buffer)
    seen = 0
    while seen < count:
        _raise_if_cancelled(cancel)
        got = handle.readinto(view[: min(len(buffer), count - seen)])  # type: ignore[attr-defined]
        if not got:
            break
        digest.update(view[:got])
        seen += got
        report(got)
    return digest, seen


def _verify(
    temp_path: str,
    plan: FilePlan,
    source_digest: hashlib._Hash,
    payload_digest: hashlib._Hash,
    chunk_size: int,
    report: Callable[[int], None],
    cancel: threading.Event,
) -> None:
    """Stream the finished temporary file back and check size, prefix and payload."""
    actual = os.stat(temp_path).st_size
    if actual != plan.final_size:
        raise VerificationError(f"size mismatch: expected {plan.final_size} bytes, found {actual}")
    with open(temp_path, "rb", buffering=0) as handle:
        prefix, prefix_count = _hash_range(handle, plan.original_size, chunk_size, report, cancel)
        tail, tail_count = _hash_range(handle, plan.add_bytes, chunk_size, report, cancel)
        extra = handle.read(1)
    if prefix_count != plan.original_size or tail_count != plan.add_bytes or extra:
        raise VerificationError("the file is not the expected length when read back")
    if prefix.digest() != source_digest.digest():
        raise VerificationError("the original content is not preserved as a prefix")
    if tail.digest() != payload_digest.digest():
        raise VerificationError("the appended bytes differ from the bytes that were written")


def _ensure_unchanged(plan: FilePlan) -> None:
    """Abort if the source file was modified, replaced or moved since planning."""
    try:
        current = safety.snapshot(plan.dest)
    except OSError as exc:
        raise SourceModifiedError(
            f"{plan.path}: cannot be inspected any more ({exc.strerror or exc}); "
            "it may have been moved or deleted"
        ) from exc
    differences = plan.identity.differences(current)
    if differences:
        raise SourceModifiedError(
            f"{plan.path}: changed during the operation ({', '.join(differences)}); "
            "aborted without replacing it"
        )
    if plan.path != plan.dest and os.path.realpath(plan.path) != plan.dest:
        raise SourceModifiedError(f"{plan.path}: the symbolic link now points elsewhere")


def _copy_metadata(source: str, temp_path: str) -> list[str]:
    """Copy permissions (and ownership where permitted); return warnings for any failure."""
    warnings: list[str] = []
    st = os.stat(source)
    try:
        os.chmod(temp_path, stat.S_IMODE(st.st_mode))
    except OSError as exc:
        warnings.append(f"could not copy permissions: {exc.strerror or exc}")
    if hasattr(os, "chown") and hasattr(os, "geteuid"):
        uid = st.st_uid if os.geteuid() == 0 else -1
        try:
            os.chown(temp_path, uid, st.st_gid)
        except OSError as exc:
            log.debug("ownership not preserved for %s: %s", source, exc)
    return warnings


def _discard_temp(temp_path: str | None) -> str | None:
    """Delete an uncommitted temporary file. Return its path if it could not be removed."""
    if temp_path is None:
        return None
    for attempt in range(2):
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            return None
        except PermissionError:
            if attempt == 0:
                try:
                    os.chmod(temp_path, stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    return temp_path
                continue
            return temp_path
        except OSError as exc:
            log.warning("could not remove temporary file %s: %s", temp_path, exc)
            return temp_path
        return None
    return temp_path  # pragma: no cover - loop always returns


# ------------------------------------------------------------------- transaction


@dataclass
class _Attempt:
    """Mutable state of one file's transaction, used for cleanup."""

    temp_path: str | None = None
    backup_path: str | None = None
    committed: bool = False
    verified: bool | None = None
    warnings: list[str] = field(default_factory=list)


def _run_transaction(
    state: _Attempt,
    index: int,
    plan: FilePlan,
    options: PadOptions,
    tracker: ProgressTracker,
    cancel: threading.Event,
) -> None:
    chunk_size = options.settings.chunk_size
    dest = plan.dest

    def report(count: int) -> None:
        tracker.advance(index, count)

    def report_payload(count: int) -> None:
        tracker.advance(index, count, payload=True)

    _raise_if_cancelled(cancel)
    _ensure_unchanged(plan)
    # Resolved fresh for each file: for most patterns this is just ``options.pattern``,
    # but for ``blend`` it builds a generator sampled from this specific file's content.
    file_pattern = options.pattern.for_file(dest)

    descriptor, state.temp_path = tempfile.mkstemp(
        prefix=_temp_prefix(dest), suffix=TEMP_SUFFIX, dir=plan.directory
    )
    log.info("staging %s in %s", plan.path, state.temp_path)
    source_digest = hashlib.sha256() if options.verify else None
    payload_digest = hashlib.sha256() if options.verify else None

    tracker.set_phase(index, PHASE_COPYING)
    with os.fdopen(descriptor, "wb") as out:
        _copy_original(dest, out, plan.original_size, chunk_size, source_digest, report, cancel)
        tracker.set_phase(index, PHASE_APPENDING)
        fill_total = plan.add_bytes - (marker_mod.SIZE if options.mark else 0)
        _write_payload(
            out, fill_total, file_pattern, chunk_size, payload_digest, report_payload, cancel
        )
        if options.mark:
            _raise_if_cancelled(cancel)
            footer = marker_mod.Marker(
                original_size=plan.original_size,
                added_bytes=plan.add_bytes,
                timestamp=int(time.time()),
                pattern=options.pattern.kind.value,
            ).pack()
            out.write(footer)
            if payload_digest is not None:
                payload_digest.update(footer)
            report_payload(len(footer))
        out.flush()
        _fsync(out)

    # Both digests exist exactly when verification was requested.
    if source_digest is not None and payload_digest is not None:
        tracker.set_phase(index, PHASE_VERIFYING)
        _verify(state.temp_path, plan, source_digest, payload_digest, chunk_size, report, cancel)
        state.verified = True

    _raise_if_cancelled(cancel)
    _ensure_unchanged(plan)
    state.warnings.extend(_copy_metadata(dest, state.temp_path))

    tracker.set_phase(index, PHASE_COMMITTING)
    if options.backup:
        state.backup_path = safety.create_backup_link(dest)
    try:
        _atomic_replace(state.temp_path, dest)
    except OSError as exc:
        if state.backup_path:
            _discard_temp(state.backup_path)
            state.backup_path = None
        raise WriteError(f"could not replace {plan.path}: {_describe_os_error(exc)}") from exc
    state.committed = True
    _fsync_directory(plan.directory)
    log.info("committed %s (%d -> %d bytes)", plan.path, plan.original_size, plan.final_size)


def _pad_file(
    index: int,
    plan: FilePlan,
    options: PadOptions,
    tracker: ProgressTracker,
    cancel: threading.Event,
) -> FileResult:
    """Process one file and convert every outcome into a :class:`FileResult`.

    This is the worker boundary: an exception escaping into a ``Future`` would be
    invisible to the rest of the batch, so unexpected errors are caught here, logged
    with a traceback, and reported as a failure of this one file.
    """
    started = time.monotonic()
    result = FileResult(
        path=plan.path,
        status=FileStatus.FAILED,
        original_size=plan.original_size,
        requested_add=plan.add_bytes,
    )
    if plan.skip_reason is not None or plan.add_bytes == 0:
        tracker.set_phase(index, PHASE_SKIPPED)
        result.status = FileStatus.SKIPPED
        result.final_size = plan.original_size
        result.error = plan.skip_reason
        return result

    state = _Attempt()
    error: Exception | None = None
    try:
        _run_transaction(state, index, plan, options, tracker, cancel)
    except Exception as exc:
        error = exc
    finally:
        leftover = None if state.committed else _discard_temp(state.temp_path)

    result.elapsed = time.monotonic() - started
    result.warnings = list(state.warnings)
    result.leftover_temp = leftover
    if error is None:
        tracker.set_phase(index, PHASE_DONE)
        result.status = FileStatus.SUCCESS
        result.bytes_added = plan.add_bytes
        result.verified = state.verified
        result.backup_path = state.backup_path
        try:
            result.final_size = os.stat(plan.dest).st_size
        except OSError:
            result.final_size = plan.final_size
        return result

    result.error_type = type(error).__name__
    if isinstance(error, OperationCancelledError):
        tracker.set_phase(index, PHASE_CANCELLED)
        result.status = FileStatus.CANCELLED
        result.error = f"{error}; the original file is unchanged"
        return result

    tracker.set_phase(index, PHASE_FAILED)
    if isinstance(error, FilePumperError):
        message = str(error)
    elif isinstance(error, OSError):
        message = f"{_describe_os_error(error)} ({type(error).__name__})"
    else:
        log.error("unexpected error while processing %s", plan.path, exc_info=error)
        message = f"unexpected {type(error).__name__}: {error}"
    result.error = f"{message}; the original file is unchanged"
    return result


# ------------------------------------------------------------------------ batches


def _cancelled_result(plan: FilePlan) -> FileResult:
    return FileResult(
        path=plan.path,
        status=FileStatus.CANCELLED,
        original_size=plan.original_size,
        requested_add=plan.add_bytes,
        error="cancelled before it started; the original file is unchanged",
        error_type="OperationCancelledError",
    )


def run_batch(
    plan: BatchPlan,
    *,
    tracker: ProgressTracker | None = None,
    cancel: threading.Event | None = None,
    on_tick: Callable[[], None] | None = None,
    tick_interval: float = 0.1,
) -> BatchResult:
    """Execute a plan, one future per *file* (never per chunk).

    Files are independent, so at most ``jobs`` of them are processed at once and no two
    workers ever touch the same file (duplicates are rejected during planning). Work
    runs on worker threads while the calling thread only waits and calls ``on_tick``;
    that keeps Ctrl+C responsive on every platform. On ``KeyboardInterrupt`` no new
    files are started, running ones stop at the next chunk, and their temporary files
    are removed before this function returns with ``interrupted=True``.
    """
    tracker = tracker or ProgressTracker(plan)
    cancel = cancel or threading.Event()
    started = time.monotonic()
    results: dict[int, FileResult] = {}
    futures: dict[Future[FileResult], int] = {}

    executor = ThreadPoolExecutor(
        max_workers=plan.effective_jobs, thread_name_prefix="filepumper-worker"
    )
    try:
        for index, item in enumerate(plan.files):
            future = executor.submit(_pad_file, index, item, plan.options, tracker, cancel)
            futures[future] = index
        pending = set(futures)
        try:
            while pending:
                done, pending = wait(pending, timeout=tick_interval, return_when=FIRST_COMPLETED)
                for future in done:
                    results[futures[future]] = future.result()
                if on_tick is not None:
                    on_tick()
        except KeyboardInterrupt:
            log.warning("interrupted; stopping and cleaning up temporary files")
            cancel.set()
            for future in pending:
                future.cancel()
            while True:  # ignore further Ctrl+C while workers clean up
                try:
                    wait(pending)
                    break
                except KeyboardInterrupt:
                    continue
            for future in pending:
                index = futures[future]
                if future.cancelled():
                    tracker.set_phase(index, PHASE_CANCELLED)
                    results[index] = _cancelled_result(plan.files[index])
                else:
                    results[index] = future.result()
    finally:
        executor.shutdown(wait=True)
    if on_tick is not None:
        # Every file is already committed or cleaned up, so this last refresh is purely
        # cosmetic. A Ctrl+C landing here must not lose the results the caller needs
        # for the summary and exit code.
        try:
            on_tick()
        except KeyboardInterrupt:
            log.debug("interrupt during the final progress refresh ignored; work was finished")
    ordered = [results[i] for i in range(len(plan.files))]
    return BatchResult(
        results=ordered,
        elapsed=time.monotonic() - started,
        interrupted=cancel.is_set(),
    )
