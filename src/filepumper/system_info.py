"""Best-effort inspection of the machine and its storage.

Anything that cannot be determined is reported as ``None`` (shown as
"unknown"); values are never guessed.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

#: Machines with less memory than this get the conservative preset.
LOW_MEMORY_BYTES = 1024**3


@dataclass(frozen=True)
class StorageInfo:
    """Disk usage for the filesystem that contains ``path``."""

    path: str
    probed: str | None
    free: int | None
    total: int | None

    @property
    def used(self) -> int | None:
        if self.total is None or self.free is None:
            return None
        return self.total - self.free


def cpu_count() -> int | None:
    """Return the number of logical CPUs, or ``None`` if unknown."""
    return os.cpu_count()


def total_memory() -> int | None:
    """Return physical memory in bytes where the platform exposes it, else ``None``."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return pages * page_size


def free_space(path: str | os.PathLike[str]) -> int | None:
    """Return bytes available to this user on the filesystem holding ``path``."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def storage_info(path: str | os.PathLike[str]) -> StorageInfo:
    """Inspect storage for ``path`` or, if it does not exist, its nearest existing parent."""
    original = os.fspath(path)
    probe = Path(original).absolute()
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return StorageInfo(path=original, probed=None, free=None, total=None)
    return StorageInfo(path=original, probed=str(probe), free=usage.free, total=usage.total)


def recommend_preset(cpus: int | None, memory: int | None) -> str:
    """Suggest a preset name. Informational only, not a benchmark result.

    Falls back to ``conservative`` when the hardware cannot be detected or has
    very little memory; otherwise suggests ``balanced``.
    """
    if cpus is None:
        return "conservative"
    if memory is not None and memory < LOW_MEMORY_BYTES:
        return "conservative"
    return "balanced"
