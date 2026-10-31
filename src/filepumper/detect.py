"""Look at a file and report whether it carries trailing data beyond what its own
format defines -- the same region File Pumper's own padding goes into.

This works on any file, regardless of what added the trailing data or whether File
Pumper was involved at all. The primary signal is structural: it compares the file's
own declared end (:mod:`filepumper.formats`) to its actual size, not a search for any
particular byte pattern. Changing what padding *looks like* (zero, random, a blend of
the file's own content) changes the entropy/content classification this module also
reports, but not whether the overlay itself is detected -- a hidden flat block is still
exactly as much a size mismatch as a blended one.

This module makes no claim about, and does not attempt to predict, how any antivirus,
EDR or other security product would treat a file.
"""

from __future__ import annotations

import math
import os
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from . import formats
from . import marker as marker_mod
from .formats import FormatInfo

#: Bound how much of the trailing region is read to classify it, independent of how
#: large that region actually is.
TAIL_SAMPLE_CAP = 1 << 20  # 1 MiB

_PRINTABLE = frozenset(range(32, 127)) | {ord("\t"), ord("\n"), ord("\r")}


class TailShape(StrEnum):
    """A rough, human-readable description of the trailing region's content."""

    NONE = "no trailing data"
    ALL_SAME_BYTE = "all one byte value"
    LOW_VARIETY = "few distinct byte values"
    PRINTABLE_TEXT = "printable text"
    HIGH_ENTROPY = "high entropy (random-looking)"
    MIXED = "mixed content"


@dataclass(frozen=True)
class PumpReport:
    """What :func:`inspect_file` learned about one file."""

    path: str
    file_size: int
    format: FormatInfo
    overlay_size: int
    #: Shannon entropy of the sampled trailing region, in bits per byte (0.0-8.0).
    #: ``None`` when there is no trailing region to sample.
    overlay_entropy: float | None
    tail_shape: TailShape
    #: Set only when File Pumper's own provenance footer (``--mark``) was found.
    marker: marker_mod.Marker | None

    @property
    def looks_pumped(self) -> bool:
        """A practical verdict: File Pumper's own marker was found, or the format's
        declared structure ends meaningfully before the file's actual end."""
        return self.marker is not None or self.overlay_size > 0


def _shannon_entropy(counts: Counter[int], total: int) -> float:
    if total == 0:
        return 0.0
    entropy = 0.0
    for count in counts.values():
        probability = count / total
        entropy -= probability * math.log2(probability)
    return entropy


def _classify_tail(path: str, start: int, file_size: int) -> tuple[float | None, TailShape]:
    size = file_size - start
    if size <= 0:
        return None, TailShape.NONE
    sampled = min(size, TAIL_SAMPLE_CAP)
    counts: Counter[int] = Counter()
    printable = 0
    with open(path, "rb", buffering=0) as handle:
        handle.seek(start)
        remaining = sampled
        while remaining > 0:
            chunk = handle.read(min(65536, remaining))
            if not chunk:
                break
            counts.update(chunk)
            printable += sum(1 for byte in chunk if byte in _PRINTABLE)
            remaining -= len(chunk)
    entropy = _shannon_entropy(counts, sampled)
    distinct = len(counts)
    if distinct <= 1:
        shape = TailShape.ALL_SAME_BYTE
    elif printable / sampled > 0.95:
        shape = TailShape.PRINTABLE_TEXT
    elif entropy > 7.5:
        shape = TailShape.HIGH_ENTROPY
    elif distinct <= 8:
        shape = TailShape.LOW_VARIETY
    else:
        shape = TailShape.MIXED
    return entropy, shape


def _find_marker(path: str, file_size: int) -> marker_mod.Marker | None:
    if file_size < marker_mod.SIZE:
        return None
    with open(path, "rb", buffering=0) as handle:
        handle.seek(file_size - marker_mod.SIZE)
        tail = handle.read(marker_mod.SIZE)
    return marker_mod.unpack(tail)


def inspect_file(path: str) -> PumpReport:
    """Inspect one file. Never raises for problems reading or parsing it; the file-size
    lookup failing is the only thing that propagates, matching ``os.path.getsize``.
    """
    file_size = os.path.getsize(path)
    info = formats.analyze(path)
    if info.structural_size is not None and info.structural_size <= file_size:
        start = info.structural_size
    else:
        start = file_size  # unrecognized or inconsistent structure: nothing to call overlay
    overlay_size = file_size - start
    entropy, shape = _classify_tail(path, start, file_size)
    found = _find_marker(path, file_size)
    return PumpReport(path, file_size, info, overlay_size, entropy, shape, found)
