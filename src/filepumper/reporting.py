"""Human-readable output and JSON reports.

Formatting helpers are pure functions so they can be tested without a terminal.
Progress goes to stderr; summaries and plans go to stdout.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import platform
import shutil
import tempfile
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from . import __version__, config, detect, system_info
from .engine import (
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_QUEUED,
    TERMINAL_PHASES,
    BatchPlan,
    BatchResult,
    FileResult,
    FileStatus,
    ProgressSnapshot,
    ProgressTracker,
)
from .safety import SpaceReport
from .sizes import format_size, format_size_exact

# ------------------------------------------------------------------ small formatters


def format_duration(seconds: float | None) -> str:
    """Format a duration; ``--:--`` when it is unknown or not finite."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "--:--"
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_rate(bytes_per_second: float | None) -> str:
    if bytes_per_second is None or not math.isfinite(bytes_per_second) or bytes_per_second <= 0:
        return "-- /s"
    return f"{format_size(int(bytes_per_second))}/s"


# ------------------------------------------------------------------------- progress


def format_progress_line(
    snapshot: ProgressSnapshot, rate: float | None, columns: int | None = None
) -> str:
    """Build the single-line progress text for an interactive terminal."""
    total, done = snapshot.total, snapshot.done
    active = [
        f for f in snapshot.files if f.phase not in TERMINAL_PHASES and f.phase != PHASE_QUEUED
    ]
    if active:
        label = os.path.basename(active[0].path) or active[0].path
        if len(active) > 1:
            label += f" (+{len(active) - 1} more)"
        phase = active[0].phase
    else:
        label, phase = "-", "waiting"
    parts = [f"{label} [{phase}]"]
    if total > 0:
        parts.append(f"{format_size(done)} / {format_size(total)} ({100 * done / total:.0f}%)")
    parts.append(format_rate(rate))
    parts.append(f"elapsed {format_duration(snapshot.elapsed)}")
    eta = estimate_eta(done, total, rate, snapshot.elapsed)
    parts.append(f"ETA {format_duration(eta)}")
    finished = sum(1 for f in snapshot.files if f.phase == PHASE_DONE)
    failed = sum(1 for f in snapshot.files if f.phase == PHASE_FAILED)
    files = f"files {finished}/{len(snapshot.files)}"
    if failed:
        files += f", {failed} failed"
    parts.append(files)
    line = " | ".join(parts)
    if columns and columns > 10 and len(line) >= columns:
        line = line[: columns - 4] + "..."
    return line


class RateMeter:
    """Throughput over a short sliding window (bytes per second)."""

    WINDOW_SECONDS = 5.0

    def __init__(self) -> None:
        self._samples: deque[tuple[float, int]] = deque()

    def update(self, now: float, done: int) -> float | None:
        """Record ``done`` bytes at time ``now``; return the current rate, or ``None``."""
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > self.WINDOW_SECONDS:
            self._samples.popleft()
        oldest_time, oldest_done = self._samples[0]
        span = now - oldest_time
        if span <= 0:
            return None
        return (done - oldest_done) / span


def estimate_eta(done: int, total: int, rate: float | None, elapsed: float) -> float | None:
    """Seconds remaining, or ``None`` when there is not enough information to say."""
    if rate and rate > 0 and done > 0 and elapsed >= 1.0 and total >= done:
        return (total - done) / rate
    return None


class ProgressRenderer:
    """Throttled progress output.

    On a terminal it rewrites one status line a few times per second. When output is
    redirected it prints one plain line per file as it is first seen and when it
    finishes, plus a heartbeat line every 30 seconds, and never uses carriage returns.
    """

    HEARTBEAT_SECONDS = 30.0

    def __init__(
        self,
        tracker: ProgressTracker,
        stream: TextIO,
        *,
        enabled: bool = True,
        interval: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
        columns: int | None = None,
    ) -> None:
        self._tracker = tracker
        self._stream = stream
        self._enabled = enabled
        self._tty = bool(getattr(stream, "isatty", lambda: False)())
        self._interval = interval
        self._clock = clock
        self._columns = columns
        self._meter = RateMeter()
        self._last_render = -math.inf
        self._last_length = 0
        self._last_heartbeat = clock()
        self._seen: set[int] = set()
        self._finished: set[int] = set()
        self._wrote_line = False

    def _width(self) -> int:
        return self._columns or shutil.get_terminal_size((100, 24)).columns

    def tick(self, *, force: bool = False) -> None:
        """Called regularly by the main thread; cheap when nothing needs printing."""
        if not self._enabled:
            return
        now = self._clock()
        snapshot = self._tracker.snapshot()
        rate = self._meter.update(now, snapshot.done)
        if self._tty:
            if force or now - self._last_render >= self._interval:
                self._render_tty(snapshot, rate)
                self._last_render = now
            return
        self._announce(snapshot)
        if now - self._last_heartbeat >= self.HEARTBEAT_SECONDS:
            self._write_line(format_progress_line(snapshot, rate))
            self._last_heartbeat = now

    def finish(self) -> None:
        """Print the final state and end the status line."""
        if not self._enabled:
            return
        snapshot = self._tracker.snapshot()
        if self._tty:
            if self._wrote_line:
                self._render_tty(snapshot, self._meter.update(self._clock(), snapshot.done))
                self._stream.write("\n")
                self._stream.flush()
        else:
            self._announce(snapshot)

    def _render_tty(self, snapshot: ProgressSnapshot, rate: float | None) -> None:
        if snapshot.total == 0 and all(f.phase in TERMINAL_PHASES for f in snapshot.files):
            return
        line = format_progress_line(snapshot, rate, self._width() - 1)
        padding = max(0, self._last_length - len(line))
        self._stream.write("\r" + line + " " * padding)
        self._stream.flush()
        self._last_length = len(line)
        self._wrote_line = True

    def _announce(self, snapshot: ProgressSnapshot) -> None:
        count = len(snapshot.files)
        for item in snapshot.files:
            if item.phase == PHASE_QUEUED:
                continue
            tag = f"[{item.index + 1}/{count}] {item.path}"
            if item.index not in self._seen and item.phase not in TERMINAL_PHASES:
                self._seen.add(item.index)
                self._write_line(f"{tag}: {item.phase}")
            if item.phase in TERMINAL_PHASES and item.index not in self._finished:
                self._seen.add(item.index)
                self._finished.add(item.index)
                self._write_line(f"{tag}: {item.phase}")

    def _write_line(self, text: str) -> None:
        self._stream.write(text + "\n")
        self._stream.flush()


# ----------------------------------------------------------------------- plan output


def _mode_text(plan: BatchPlan) -> str:
    size = format_size_exact(plan.options.size)
    if plan.options.mode.value == "add":
        return f"add {size} to each file"
    return f"expand each file to a total size of {size}"


def format_plan(plan: BatchPlan, space: SpaceReport | None, *, dry_run: bool) -> str:
    """Describe the planned operation. ``dry_run`` selects the detailed per-file layout."""
    options = plan.options
    lines: list[str] = []
    if dry_run:
        lines.append("DRY RUN: no files, names, timestamps or permissions will be changed.")
        lines.append("")
    lines.append(f"Operation  : {_mode_text(plan)}")
    lines.append(f"Pattern    : {options.pattern.describe()}")
    jobs_note = ""
    if plan.effective_jobs != options.settings.jobs:
        jobs_note = f" (effective {plan.effective_jobs}: limited by the number of files to write)"
    lines.append(f"Performance: {options.settings.describe()}{jobs_note}")
    lines.append(f"Verify     : {'yes (reads each result back)' if options.verify else 'no'}")
    lines.append(
        f"Backup     : {'yes (hard-link kept beside the file)' if options.backup else 'no'}"
    )
    if options.mark:
        lines.append("Mark       : yes (a provenance footer is written into the padding)")
    lines.append(f"Files      : {len(plan.files)} ({len(plan.writes)} to modify)")
    lines.append("")
    for number, item in enumerate(plan.files, start=1):
        lines.append(f"[{number}/{len(plan.files)}] {item.path}")
        lines.append(f"    current size : {format_size_exact(item.original_size)}")
        lines.append(f"    additional   : {format_size_exact(item.add_bytes)}")
        lines.append(f"    final size   : {format_size_exact(item.final_size)}")
        if item.format.kind.value != "unknown":
            safe = (
                "appending stays within the format's own structure"
                if (item.format.append_is_structure_safe)
                else "appending safety could not be confirmed, see WARNING below"
            )
            lines.append(f"    format       : {item.format.label} ({safe})")
        if item.skip_reason:
            lines.append(f"    SKIPPED      : {item.skip_reason}")
        else:
            lines.append(
                f"    temp space   : ~{format_size(item.final_size)} for the staged copy "
                "(original bytes + payload)"
            )
        for warning in item.warnings:
            lines.append(f"    WARNING      : {warning}")
    if space is not None and space.devices:
        lines.append("")
        lines.append("Disk space (estimate for staging, not a guarantee):")
        for device in space.devices:
            free = "unknown" if device.free is None else format_size(device.free)
            verdict = (
                "OK"
                if device.sufficient
                else f"INSUFFICIENT (short by {format_size(device.shortfall)})"
            )
            need = format_size(device.required)
            lines.append(f"    {device.directory}: need ~{need}, free {free} -> {verdict}")
        lines.append(
            "    Other programs, quotas or filesystem overhead can still cause a "
            "runtime out-of-space failure; the original file is kept if that happens."
        )
    if len(plan.writes) > 1:
        lines.append("")
        lines.append(
            "Note: files are committed one at a time. If a runtime failure occurs part-way "
            "through, files committed earlier stay modified."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- summary


def _result_line(item: FileResult) -> str:
    if item.status is FileStatus.SUCCESS:
        tags = []
        if item.verified:
            tags.append("verified")
        if item.backup_path:
            tags.append(f"backup: {item.backup_path}")
        extra = f" [{'; '.join(tags)}]" if tags else ""
        return (
            f"  [OK]        {item.path}: {format_size(item.original_size)} -> "
            f"{format_size(item.final_size or 0)} (+{format_size(item.bytes_added)}){extra}"
        )
    if item.status is FileStatus.SKIPPED:
        return f"  [SKIPPED]   {item.path}: {item.error or 'nothing to do'}"
    label = "[CANCELLED]" if item.status is FileStatus.CANCELLED else "[FAILED]   "
    return f"  {label} {item.path}: {item.error}"


def format_summary(result: BatchResult) -> str:
    """Render the end-of-run summary."""
    lines = [
        "",
        "Summary",
        "-------",
        f"Files requested : {len(result.results)}",
        f"  succeeded     : {result.succeeded}",
        f"  skipped       : {result.skipped}",
        f"  failed        : {result.failed}",
        f"  cancelled     : {result.cancelled}",
        f"Total bytes added: {format_size_exact(result.total_added)}",
        f"Elapsed          : {format_duration(result.elapsed)}",
        "",
        "Files:",
    ]
    lines.extend(_result_line(item) for item in result.results)
    for item in result.results:
        for warning in item.warnings:
            lines.append(f"  note ({item.path}): {warning}")
    if result.leftover_temps:
        lines.append("")
        lines.append("WARNING: these temporary files could not be removed; delete them manually:")
        lines.extend(f"  {path}" for path in result.leftover_temps)
    if result.succeeded and (result.failed or result.cancelled):
        lines.append("")
        lines.append(
            f"Batch only partly completed: {result.succeeded} file(s) were already committed and "
            "are NOT rolled back. Failed or cancelled files were left unchanged."
        )
    elif result.interrupted:
        lines.append("")
        lines.append(
            "Interrupted: files that were still in progress were left unchanged and their "
            "temporary files were removed."
        )
    return "\n".join(lines)


# -------------------------------------------------------------------- JSON reporting


def build_report(
    plan: BatchPlan,
    result: BatchResult | None,
    space: SpaceReport | None,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Build a JSON-serialisable report for a dry run (``result=None``) or a real run."""
    options = plan.options
    files: list[dict[str, Any]] = []
    for index, item in enumerate(plan.files):
        entry: dict[str, Any] = {
            "path": item.path,
            "resolved_path": item.dest,
            "original_size_bytes": item.original_size,
            "requested_add_bytes": item.add_bytes,
            "planned_final_size_bytes": item.final_size,
            "warnings": list(item.warnings),
        }
        if result is None:
            entry.update(
                status="skipped" if item.skip_reason else "planned",
                bytes_added=0,
                final_size_bytes=None,
                elapsed_seconds=None,
                verified=None,
                backup_path=None,
                error=None,
                reason=item.skip_reason,
            )
        else:
            outcome = result.results[index]
            entry.update(
                status=outcome.status.value,
                bytes_added=outcome.bytes_added,
                final_size_bytes=outcome.final_size,
                elapsed_seconds=round(outcome.elapsed, 6),
                verified=outcome.verified,
                backup_path=outcome.backup_path,
                error=None if outcome.status is FileStatus.SKIPPED else outcome.error,
                reason=outcome.error if outcome.status is FileStatus.SKIPPED else None,
                leftover_temp=outcome.leftover_temp,
            )
            entry["warnings"].extend(outcome.warnings)
        files.append(entry)

    summary: dict[str, Any] = {"requested": len(plan.files)}
    if result is None:
        summary.update(
            would_modify=len(plan.writes),
            would_skip=len(plan.files) - len(plan.writes),
            total_planned_add_bytes=plan.total_add,
        )
    else:
        summary.update(
            succeeded=result.succeeded,
            skipped=result.skipped,
            failed=result.failed,
            cancelled=result.cancelled,
            total_bytes_added=result.total_added,
            elapsed_seconds=round(result.elapsed, 6),
            interrupted=result.interrupted,
        )
    return {
        "tool": "filepumper",
        "version": __version__,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "operation": {"mode": options.mode.value, "size_bytes": options.size},
        "pattern": {
            "kind": options.pattern.kind.value,
            "description": options.pattern.describe(),
        },
        "performance": {
            "preset": options.settings.preset,
            "resolved_preset": options.settings.resolved_preset,
            "chunk_size_bytes": options.settings.chunk_size,
            "jobs": options.settings.jobs,
            "effective_jobs": plan.effective_jobs,
        },
        "options": {
            "verify": options.verify,
            "backup": options.backup,
            "follow_symlinks": options.follow_symlinks,
            "allow_hardlinks": options.allow_hardlinks,
            "max_add_bytes": options.max_add,
        },
        "space_check": (
            None
            if space is None
            else [
                {
                    "directory": d.directory,
                    "required_bytes": d.required,
                    "free_bytes": d.free,
                    "sufficient": d.sufficient,
                }
                for d in space.devices
            ]
        ),
        "summary": summary,
        "files": files,
    }


def write_report(path: str | os.PathLike[str], report: dict[str, Any]) -> None:
    """Write a JSON report atomically (temporary file + replace)."""
    target = Path(path)
    if target.is_dir():
        raise IsADirectoryError(f"{target} is a directory")
    directory = target.parent if str(target.parent) else Path(".")
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        os.replace(temp_name, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


# ------------------------------------------------------------------------ info output


def build_info(paths: list[str]) -> dict[str, Any]:
    """Collect machine, configuration and storage information; unknown values are ``None``."""
    cpus = system_info.cpu_count()
    memory = system_info.total_memory()
    recommended = system_info.recommend_preset(cpus, memory)
    settings = config.resolve_performance(recommended)
    config_path = config.default_config_path()
    return {
        "filepumper_version": __version__,
        "platform": platform.platform(),
        "machine": platform.machine() or None,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "cpu_count": cpus,
        "total_memory_bytes": memory,
        "config": {
            "path": None if config_path is None else str(config_path),
            "exists": bool(config_path and config_path.is_file()),
        },
        "recommended": {
            "preset": recommended,
            "chunk_size_bytes": settings.chunk_size,
            "jobs": settings.jobs,
            "note": "informational suggestion, not a benchmark result",
        },
        "limits": {
            "min_chunk_size_bytes": config.MIN_CHUNK_SIZE,
            "max_chunk_size_bytes": config.MAX_CHUNK_SIZE,
            "max_jobs": config.MAX_JOBS,
            "default_max_add_bytes": config.DEFAULT_MAX_ADD,
            "confirm_threshold_bytes": config.CONFIRM_THRESHOLD,
        },
        "storage": [
            {
                "path": info.path,
                "probed_path": info.probed,
                "free_bytes": info.free,
                "total_bytes": info.total,
                "used_bytes": info.used,
            }
            for info in map(system_info.storage_info, paths)
        ],
    }


def build_pump_reports(paths: list[str]) -> list[dict[str, Any]]:
    """Run :func:`filepumper.detect.inspect_file` on every path; unreadable paths get
    an ``error`` field instead of raising, so one bad path does not stop the rest.
    """
    reports: list[dict[str, Any]] = []
    for path in paths:
        try:
            report = detect.inspect_file(path)
        except OSError as exc:
            reports.append({"path": path, "error": exc.strerror or str(exc)})
            continue
        reports.append(
            {
                "path": report.path,
                "file_size_bytes": report.file_size,
                "format": report.format.kind.value,
                "format_label": report.format.label,
                "format_note": report.format.note,
                "structural_size_bytes": report.format.structural_size,
                "overlay_size_bytes": report.overlay_size,
                "overlay_entropy_bits_per_byte": report.overlay_entropy,
                "overlay_shape": report.tail_shape.value,
                "marker": None
                if report.marker is None
                else {
                    "original_size_bytes": report.marker.original_size,
                    "added_bytes": report.marker.added_bytes,
                    "timestamp_unix": report.marker.timestamp,
                    "timestamp": datetime.fromtimestamp(
                        report.marker.timestamp, tz=UTC
                    ).isoformat(),
                    "pattern": report.marker.pattern,
                },
                "looks_pumped": report.looks_pumped,
            }
        )
    return reports


def format_pump_reports(reports: list[dict[str, Any]]) -> str:
    """Render :func:`build_pump_reports` output as text."""
    lines: list[str] = []
    for number, report in enumerate(reports, start=1):
        lines.append(f"[{number}/{len(reports)}] {report['path']}")
        if "error" in report:
            lines.append(f"    could not be inspected: {report['error']}")
            lines.append("")
            continue
        lines.append(f"    size         : {format_size_exact(report['file_size_bytes'])}")
        lines.append(f"    format       : {report['format_label']} ({report['format_note']})")
        verdict = "PUMPED" if report["looks_pumped"] else "no sign of padding found"
        style_note = ""
        if report["overlay_size_bytes"]:
            entropy = report["overlay_entropy_bits_per_byte"]
            style_note = (
                f", {format_size(report['overlay_size_bytes'])} beyond the format's own "
                f"structure ({report['overlay_shape']}, {entropy:.2f} bits/byte)"
            )
        lines.append(f"    verdict      : {verdict}{style_note}")
        marker = report["marker"]
        if marker is not None:
            lines.append(
                "    File Pumper mark: original "
                f"{format_size_exact(marker['original_size_bytes'])}, added "
                f"{format_size_exact(marker['added_bytes'])}, pattern {marker['pattern']}, "
                f"at {marker['timestamp']}"
            )
        lines.append("")
    pumped = sum(1 for r in reports if r.get("looks_pumped"))
    errors = sum(1 for r in reports if "error" in r)
    lines.append(
        f"{pumped}/{len(reports)} file(s) show signs of padding"
        + (f" ({errors} could not be inspected)" if errors else "")
    )
    return "\n".join(lines)


def _or_unknown(value: int | None) -> str:
    return "unknown" if value is None else format_size(value)


def format_info(info: dict[str, Any]) -> str:
    """Render :func:`build_info` output as text."""
    recommended = info["recommended"]
    limits = info["limits"]
    cpus = info["cpu_count"]
    config_info = info["config"]
    lines = [
        f"File Pumper {info['filepumper_version']}",
        f"Platform     : {info['platform']}",
        f"Python       : {info['python']['version']} ({info['python']['implementation']})",
        f"Logical CPUs : {'unknown' if cpus is None else cpus}",
        f"Memory       : {_or_unknown(info['total_memory_bytes'])}",
        "Config file  : "
        + (
            "unknown"
            if config_info["path"] is None
            else f"{config_info['path']} ({'found' if config_info['exists'] else 'not found'})"
        ),
        f"Suggested    : preset {recommended['preset']} (chunk "
        f"{format_size(recommended['chunk_size_bytes'])}, jobs {recommended['jobs']}); "
        "informational, not a benchmark",
        f"Limits       : chunk size {format_size(limits['min_chunk_size_bytes'])} to "
        f"{format_size(limits['max_chunk_size_bytes'])}; jobs 1 to {limits['max_jobs']}; "
        f"default max addition {format_size(limits['default_max_add_bytes'])} per file; "
        f"confirmation from {format_size(limits['confirm_threshold_bytes'])} total",
        "Storage:",
    ]
    for entry in info["storage"]:
        if entry["free_bytes"] is None:
            lines.append(f"  {entry['path']}: unknown")
            continue
        probed = ""
        if entry["probed_path"] and entry["probed_path"] != os.path.abspath(entry["path"]):
            probed = f" (measured at {entry['probed_path']})"
        lines.append(
            f"  {entry['path']}: free {format_size(entry['free_bytes'])} of "
            f"{_or_unknown(entry['total_bytes'])}, used {_or_unknown(entry['used_bytes'])}{probed}"
        )
    return "\n".join(lines)
