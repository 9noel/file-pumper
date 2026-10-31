"""An opt-in provenance footer for padded files.

This exists for lab record-keeping: when ``--mark`` is used, File Pumper writes a small,
fixed-size footer identifying that *this tool* added the trailing padding, with the
original size, how much was added, when, and which pattern was used. ``filepumper
detect`` can then tell a fixture it created apart from one it only suspects (via
structural analysis) was padded by something else.

This is not meant to be hidden. The whole point of a provenance marker is that it is
reliably findable -- the opposite of disguising the fact that padding happened. It is
off by default and only ever written when explicitly requested.

The footer is written as the *last* bytes of the requested padding, so it consumes part
of the ``--add``/``--target`` budget rather than growing the file beyond the exact size
that was asked for.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .patterns import PatternKind

MAGIC = b"FPmpMRK1"
#: Total footer size in bytes: 8 (magic) + 8 + 8 + 8 (sizes/timestamp) + 1 (pattern)
#: + 27 (reserved) + 4 (CRC32) = 64.
SIZE = 64

_BODY = struct.Struct("<8sQQQB27s")
assert _BODY.size == SIZE - 4

_PATTERN_IDS = {kind.value: index for index, kind in enumerate(PatternKind)}
_PATTERN_NAMES = {index: kind.value for index, kind in enumerate(PatternKind)}


@dataclass(frozen=True)
class Marker:
    """The information a provenance footer carries."""

    original_size: int
    added_bytes: int
    timestamp: int  # Unix seconds, UTC
    pattern: str

    def pack(self) -> bytes:
        """Encode as the fixed :data:`SIZE`-byte footer."""
        body = _BODY.pack(
            MAGIC,
            self.original_size,
            self.added_bytes,
            self.timestamp,
            _PATTERN_IDS.get(self.pattern, 0xFF),
            b"",
        )
        return body + struct.pack("<I", zlib.crc32(body))


def unpack(data: bytes) -> Marker | None:
    """Decode a marker from the last :data:`SIZE` bytes of a file, or ``None`` if the
    magic or checksum does not match -- never raises on arbitrary/adversarial bytes.
    """
    if len(data) != SIZE:
        return None
    body, crc_bytes = data[:-4], data[-4:]
    if struct.unpack("<I", crc_bytes)[0] != zlib.crc32(body):
        return None
    try:
        magic, original_size, added_bytes, timestamp, pattern_id, _reserved = _BODY.unpack(body)
    except struct.error:
        return None
    if magic != MAGIC:
        return None
    return Marker(original_size, added_bytes, timestamp, _PATTERN_NAMES.get(pattern_id, "unknown"))
