"""Structural analysis of common file formats.

For each format recognized here, this answers one question: at what byte offset does the
format's *own* definition say its content ends? Bytes after that point ("overlay" or
"trailing data") are not interpreted by typical readers for that format -- which is why
File Pumper always appends new bytes at the literal end of the file: for every format
recognized here, the literal end already falls inside (or at) that tolerated region, so
appending does not move, resize or reinterpret anything the format itself defines.

Detection is a magic-byte match plus a hand-rolled, defensive parse of just the
structural metadata (never the whole payload, and never more than a bounded read for a
large file). Parsing never raises: a malformed, truncated or adversarial file is reported
as unrecognized or low-confidence instead of crashing the caller.

This module makes no claim about antivirus, EDR or any other security product's
detection of a file; it only reports where a format's own structure ends relative to the
file's actual size.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from enum import StrEnum
from typing import BinaryIO


class FormatKind(StrEnum):
    """Names of the formats this module can recognize and analyze."""

    PE = "pe"
    ZIP = "zip"
    PDF = "pdf"
    PNG = "png"
    JPEG = "jpeg"
    GIF = "gif"
    UNKNOWN = "unknown"


_LABELS: dict[FormatKind, str] = {
    FormatKind.PE: "PE executable/DLL",
    FormatKind.ZIP: "ZIP archive (also .docx/.xlsx/.pptx/.jar/.apk and similar)",
    FormatKind.PDF: "PDF document",
    FormatKind.PNG: "PNG image",
    FormatKind.JPEG: "JPEG image",
    FormatKind.GIF: "GIF image",
    FormatKind.UNKNOWN: "unrecognized format",
}


@dataclass(frozen=True)
class FormatInfo:
    """What was learned about one file's structure."""

    kind: FormatKind
    #: Byte offset where the format's own structure ends, or ``None`` if it could not
    #: be determined (unrecognized format, or a parse that could not be completed).
    structural_size: int | None
    file_size: int
    #: False when the parse found something it could not fully account for (a
    #: truncated or implausible structure); the result is still returned, but
    #: :attr:`append_is_structure_safe` is never true when this is false.
    confident: bool
    #: One short, human-readable sentence.
    note: str

    @property
    def label(self) -> str:
        return _LABELS[self.kind]

    @property
    def overlay_size(self) -> int:
        """Bytes already beyond the structural end, before any new padding is added."""
        if self.structural_size is None:
            return 0
        return max(0, self.file_size - self.structural_size)

    @property
    def append_is_structure_safe(self) -> bool:
        """True when appending at the literal end of the file is known, for this
        format, not to disturb anything the format itself defines.

        This is the common, expected case for every format :func:`analyze` recognizes.
        It is false for an unrecognized format (no basis to claim anything) and for a
        structure that looks truncated or inconsistent (``structural_size`` bigger than
        the file actually is), where File Pumper makes no safety claim either way.
        """
        return (
            self.kind is not FormatKind.UNKNOWN
            and self.confident
            and self.structural_size is not None
            and self.structural_size <= self.file_size
        )


def _unknown(file_size: int, note: str = "format not recognized") -> FormatInfo:
    return FormatInfo(FormatKind.UNKNOWN, None, file_size, False, note)


def sniff(header: bytes) -> FormatKind:
    """Identify a format from its first bytes (a magic-byte check only)."""
    if header[:2] == b"MZ":
        return FormatKind.PE
    if header[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return FormatKind.ZIP
    if header[:5] == b"%PDF-":
        return FormatKind.PDF
    if header[:8] == _PNG_SIGNATURE:
        return FormatKind.PNG
    if header[:3] == b"\xff\xd8\xff":
        return FormatKind.JPEG
    if header[:6] in _GIF_MAGICS:
        return FormatKind.GIF
    return FormatKind.UNKNOWN


def analyze(path: str) -> FormatInfo:
    """Learn what can be learned about ``path``'s structure without reading its whole
    content. Never raises: any problem opening, reading or parsing the file is reported
    as an unrecognized or low-confidence result instead.
    """
    try:
        file_size = os.path.getsize(path)
    except OSError as exc:
        return FormatInfo(
            FormatKind.UNKNOWN, None, 0, False, f"could not stat the file: {exc.strerror or exc}"
        )
    if file_size == 0:
        return _unknown(0, "empty file")
    kind = FormatKind.UNKNOWN
    try:
        with open(path, "rb", buffering=0) as handle:
            kind = sniff(handle.read(64))
            if kind is FormatKind.UNKNOWN:
                return _unknown(file_size)
            handle.seek(0)
            return _PARSERS[kind](handle, file_size)
    except OSError as exc:
        note = f"could not read the file: {exc.strerror or exc}"
        return FormatInfo(FormatKind.UNKNOWN, None, file_size, False, note)
    except Exception:  # a malformed or adversarial file must never crash planning
        return FormatInfo(kind, None, file_size, False, "the structure could not be parsed")


# -------------------------------------------------------------------------------- PE


def _parse_pe(handle: BinaryIO, file_size: int) -> FormatInfo:
    handle.seek(0)
    dos = handle.read(64)
    if len(dos) < 64 or dos[:2] != b"MZ":
        return FormatInfo(FormatKind.PE, None, file_size, False, "DOS header is too short")
    (e_lfanew,) = struct.unpack_from("<I", dos, 0x3C)
    if e_lfanew < 64 or e_lfanew + 24 > file_size:
        return FormatInfo(FormatKind.PE, None, file_size, False, "PE header offset is out of range")

    handle.seek(e_lfanew)
    if handle.read(4) != b"PE\x00\x00":
        return FormatInfo(FormatKind.PE, None, file_size, False, "PE signature not found")
    coff = handle.read(20)
    if len(coff) < 20:
        return FormatInfo(FormatKind.PE, None, file_size, False, "truncated COFF header")
    _machine, num_sections, _ts, _symtab, _numsym, opt_size, _chars = struct.unpack_from(
        "<HHIIIHH", coff, 0
    )
    opt_header_start = handle.tell()
    section_table_start = opt_header_start + opt_size
    if num_sections > 192 or section_table_start + num_sections * 40 > file_size + 4096:
        return FormatInfo(FormatKind.PE, None, file_size, False, "implausible section table")

    # The Certificate Table (Attribute Certificate Table, data directory entry 4) is
    # written after the section data and is meant to stay last; folding it into the
    # structural end means padding is placed after it, not inside it.
    cert_end = 0
    if opt_size >= 2:
        handle.seek(opt_header_start)
        optional = handle.read(opt_size)
        magic = struct.unpack_from("<H", optional, 0)[0] if len(optional) >= 2 else 0
        dir_base = {0x10B: 0x60, 0x20B: 0x70}.get(magic)
        entry_offset = None if dir_base is None else dir_base + 4 * 8
        if entry_offset is not None and entry_offset + 8 <= len(optional):
            cert_offset, cert_size = struct.unpack_from("<II", optional, entry_offset)
            cert_end = cert_offset + cert_size

    handle.seek(section_table_start)
    structural_end = max(section_table_start, cert_end)
    for _ in range(num_sections):
        raw = handle.read(40)
        if len(raw) < 40:
            note = "truncated section table"
            return FormatInfo(FormatKind.PE, structural_end, file_size, False, note)
        size_raw, ptr_raw = struct.unpack_from("<II", raw, 16)
        if size_raw:
            structural_end = max(structural_end, ptr_raw + size_raw)
    note = "overlay/certificate region" if structural_end < file_size else "no existing overlay"
    return FormatInfo(FormatKind.PE, structural_end, file_size, True, note)


# ------------------------------------------------------------------------------- ZIP

_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_FIXED_SIZE = 22


def _parse_zip(handle: BinaryIO, file_size: int) -> FormatInfo:
    window = min(file_size, _EOCD_FIXED_SIZE + 65535)
    if window < _EOCD_FIXED_SIZE:
        return FormatInfo(
            FormatKind.ZIP,
            None,
            file_size,
            False,
            "too short for an end-of-central-directory record",
        )
    handle.seek(file_size - window)
    tail = handle.read(window)
    index = tail.rfind(_EOCD_SIGNATURE)
    if index == -1:
        return FormatInfo(
            FormatKind.ZIP, None, file_size, False, "end-of-central-directory record not found"
        )
    comment_len = struct.unpack_from("<H", tail, index + 20)[0]
    structural_end = (file_size - window) + index + _EOCD_FIXED_SIZE + comment_len
    confident = structural_end <= file_size
    note = "after the central directory" if structural_end < file_size else "no existing overlay"
    if not confident:
        note = "end-of-central-directory record claims more than the file contains"
    return FormatInfo(FormatKind.ZIP, structural_end, file_size, confident, note)


# ------------------------------------------------------------------------------- PDF

_PDF_EOF = b"%%EOF"
_PDF_TAIL_WINDOW = 1 << 20  # 1 MiB: generous, but bounded regardless of file size


def _parse_pdf(handle: BinaryIO, file_size: int) -> FormatInfo:
    window = min(file_size, _PDF_TAIL_WINDOW)
    handle.seek(file_size - window)
    tail = handle.read(window)
    index = tail.rfind(_PDF_EOF)
    if index == -1:
        return FormatInfo(
            FormatKind.PDF, None, file_size, False, "no '%%EOF' trailer found near the end"
        )
    structural_end = (file_size - window) + index + len(_PDF_EOF)
    # A single trailing end-of-line after '%%EOF' is conventional, not overlay data.
    rest = tail[index + len(_PDF_EOF) : index + len(_PDF_EOF) + 2]
    if rest[:2] == b"\r\n":
        structural_end += 2
    elif rest[:1] in (b"\r", b"\n"):
        structural_end += 1
    has_overlay = structural_end < file_size
    state = "existing overlay after the trailer" if has_overlay else "no existing overlay"
    note = f"heuristic, based on the last '%%EOF' marker (not a full object parse): {state}"
    return FormatInfo(FormatKind.PDF, structural_end, file_size, True, note)


# ------------------------------------------------------------------------------- PNG

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _parse_png(handle: BinaryIO, file_size: int) -> FormatInfo:
    handle.seek(0)
    if handle.read(8) != _PNG_SIGNATURE:
        return FormatInfo(FormatKind.PNG, None, file_size, False, "PNG signature not found")
    pos = 8
    while pos + 8 <= file_size:
        header = handle.read(8)
        if len(header) < 8:
            break
        length, chunk_type = struct.unpack(">I4s", header)
        pos += 8
        if chunk_type == b"IEND":
            pos += 4  # IEND's (always-present, always-zero-length-data) trailing CRC
            note = "overlay after IEND" if pos < file_size else "no existing overlay"
            return FormatInfo(FormatKind.PNG, pos, file_size, True, note)
        skip = length + 4  # chunk data + CRC
        if length < 0 or pos + skip > file_size:
            return FormatInfo(FormatKind.PNG, None, file_size, False, "a chunk runs past the end")
        handle.seek(skip, 1)
        pos += skip
    return FormatInfo(FormatKind.PNG, None, file_size, False, "no IEND chunk found")


# ------------------------------------------------------------------------------ JPEG

_RESTART_RANGE = range(0xD0, 0xD8)
_NO_PAYLOAD_MARKERS = frozenset({0x01, 0xD8, 0xD9}) | set(_RESTART_RANGE)
_SOS, _EOI = 0xDA, 0xD9


def _parse_jpeg(handle: BinaryIO, file_size: int) -> FormatInfo:
    handle.seek(0)
    if handle.read(2) != b"\xff\xd8":
        return FormatInfo(FormatKind.JPEG, None, file_size, False, "JPEG SOI marker not found")
    pos = 2
    while pos < file_size:
        head = handle.read(2)
        if len(head) < 2 or head[0] != 0xFF:
            return FormatInfo(FormatKind.JPEG, None, file_size, False, "marker sync lost")
        pos += 2
        kind = head[1]
        while kind == 0xFF:  # any number of fill bytes may precede the real marker code
            extra = handle.read(1)
            if not extra:
                return FormatInfo(FormatKind.JPEG, None, file_size, False, "truncated marker")
            pos += 1
            kind = extra[0]
        if kind == _EOI:
            note = "overlay after EOI" if pos < file_size else "no existing overlay"
            return FormatInfo(FormatKind.JPEG, pos, file_size, True, note)
        if kind in _NO_PAYLOAD_MARKERS:
            continue
        length_bytes = handle.read(2)
        if len(length_bytes) < 2:
            return FormatInfo(FormatKind.JPEG, None, file_size, False, "truncated segment length")
        (length,) = struct.unpack(">H", length_bytes)
        if length < 2 or pos + length > file_size:
            note = "segment length out of range"
            return FormatInfo(FormatKind.JPEG, None, file_size, False, note)
        handle.seek(length - 2, 1)
        pos += length
        if kind == _SOS:
            next_pos = _skip_jpeg_entropy_data(handle, pos, file_size)
            if next_pos is None:
                return FormatInfo(
                    FormatKind.JPEG, None, file_size, False, "entropy-coded data never ends"
                )
            pos = next_pos
    return FormatInfo(FormatKind.JPEG, None, file_size, False, "no EOI marker found")


def _skip_jpeg_entropy_data(handle: BinaryIO, pos: int, file_size: int) -> int | None:
    """Advance past compressed scan data to just before the next real marker.

    Byte-stuffed ``FF 00`` and restart markers (``FFD0``-``FFD7``) are part of the
    entropy-coded data, not segment boundaries, and are skipped over.
    """
    while pos < file_size - 1:
        byte = handle.read(1)
        pos += 1
        if byte != b"\xff":
            continue
        nxt = handle.read(1)
        if not nxt:
            return None
        if nxt == b"\x00" or nxt[0] in _RESTART_RANGE:
            pos += 1
            continue
        handle.seek(-2, 1)  # put both marker bytes back for the caller to read
        return pos - 1
    return None


# -------------------------------------------------------------------------------- GIF

_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_GIF_TRAILER = 0x3B
_GIF_EXTENSION = 0x21
_GIF_IMAGE = 0x2C


def _color_table_size(packed_flags: int) -> int:
    return 3 * (2 ** ((packed_flags & 0x07) + 1))


def _parse_gif(handle: BinaryIO, file_size: int) -> FormatInfo:
    handle.seek(0)
    header = handle.read(13)  # 6-byte magic + 7-byte logical screen descriptor
    if len(header) < 13 or header[:6] not in _GIF_MAGICS:
        return FormatInfo(FormatKind.GIF, None, file_size, False, "GIF signature not found")
    pos = 13
    if header[10] & 0x80:  # global color table present
        table_size = _color_table_size(header[10])
        if pos + table_size > file_size:
            return FormatInfo(
                FormatKind.GIF, None, file_size, False, "global color table runs past the end"
            )
        handle.seek(table_size, 1)
        pos += table_size
    while pos < file_size:
        marker = handle.read(1)
        if not marker:
            break
        pos += 1
        if marker[0] == _GIF_TRAILER:
            note = "overlay after the trailer" if pos < file_size else "no existing overlay"
            return FormatInfo(FormatKind.GIF, pos, file_size, True, note)
        if marker[0] == _GIF_EXTENSION:
            if not handle.read(1):  # the extension's label byte
                break
            pos += 1
            pos = _skip_gif_subblocks(handle, pos, file_size)
        elif marker[0] == _GIF_IMAGE:
            pos = _parse_gif_image(handle, pos, file_size)
        else:
            return FormatInfo(
                FormatKind.GIF, None, file_size, False, "unrecognized block introducer"
            )
        if pos is None:
            return FormatInfo(FormatKind.GIF, None, file_size, False, "a block runs past the end")
    return FormatInfo(FormatKind.GIF, None, file_size, False, "no trailer found")


def _parse_gif_image(handle: BinaryIO, pos: int, file_size: int) -> int | None:
    desc = handle.read(9)
    if len(desc) < 9:
        return None
    pos += 9
    if desc[8] & 0x80:  # local color table present
        table_size = _color_table_size(desc[8])
        if pos + table_size > file_size:
            return None
        handle.seek(table_size, 1)
        pos += table_size
    if not handle.read(1):  # LZW minimum code size
        return None
    pos += 1
    return _skip_gif_subblocks(handle, pos, file_size)


def _skip_gif_subblocks(handle: BinaryIO, pos: int, file_size: int) -> int | None:
    while pos < file_size:
        size_byte = handle.read(1)
        if not size_byte:
            return None
        pos += 1
        size = size_byte[0]
        if size == 0:
            return pos
        if pos + size > file_size:
            return None
        handle.seek(size, 1)
        pos += size
    return None


_PARSERS = {
    FormatKind.PE: _parse_pe,
    FormatKind.ZIP: _parse_zip,
    FormatKind.PDF: _parse_pdf,
    FormatKind.PNG: _parse_png,
    FormatKind.JPEG: _parse_jpeg,
    FormatKind.GIF: _parse_gif,
}
