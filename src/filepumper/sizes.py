"""Parsing and formatting of human-readable binary sizes.

File Pumper uses binary units only: ``KB`` and ``KiB`` both mean 1024 bytes,
``MB`` and ``MiB`` both mean 1024**2 bytes, and so on. A unit is mandatory so
that ``100`` is never silently interpreted as bytes, kilobytes or anything else.
"""

from __future__ import annotations

import re
from decimal import Context, Decimal, InvalidOperation

from .exceptions import InvalidSizeError

KIB = 1024
MIB = KIB**2
GIB = KIB**3
TIB = KIB**4

#: Largest size accepted anywhere (the largest signed 64-bit offset).
MAX_SIZE_BYTES = 2**63 - 1

#: Longest accepted input text; keeps parsing cost trivially bounded.
MAX_INPUT_LENGTH = 64

UNIT_FACTORS: dict[str, int] = {
    "B": 1,
    "KB": KIB,
    "KIB": KIB,
    "MB": MIB,
    "MIB": MIB,
    "GB": GIB,
    "GIB": GIB,
    "TB": TIB,
    "TIB": TIB,
}

SUPPORTED_UNITS_TEXT = "B, KB, KiB, MB, MiB, GB, GiB, TB, TiB"

_SIZE_RE = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]*)", re.ASCII)
_NON_FINITE_RE = re.compile(r"[+-]?(?:inf(?:inity)?|nan)", re.IGNORECASE)
# 64 characters of input times a factor of at most 2**40 always fits in 200 digits,
# so the multiplication below is exact.
_EXACT = Context(prec=200)


def parse_size(text: str) -> int:
    """Convert text such as ``"100MB"`` or ``"1.5GiB"`` to a whole number of bytes.

    Raises :class:`InvalidSizeError` for empty, malformed, negative, non-finite,
    unit-less, unsupported-unit, fractional-byte or oversized input. Zero is
    accepted (``"0B"``); callers decide how to treat a zero-byte request.
    """
    if not isinstance(text, str):
        raise InvalidSizeError(f"size must be text, got {type(text).__name__}")
    value = text.strip()
    if not value:
        raise InvalidSizeError("size is empty; expected a number with a unit, e.g. 100MB")
    if len(value) > MAX_INPUT_LENGTH:
        raise InvalidSizeError("size text is too long to be valid")
    if _NON_FINITE_RE.fullmatch(value):
        raise InvalidSizeError(f"non-finite size {value!r} is not allowed")
    if value.startswith("-"):
        raise InvalidSizeError(f"negative sizes are not allowed: {value!r}")
    if value.startswith("+"):
        raise InvalidSizeError(f"explicit '+' sign is not allowed: {value!r}")

    match = _SIZE_RE.fullmatch(value)
    if match is None:
        raise InvalidSizeError(
            f"malformed size {value!r}; expected a number followed by a unit, e.g. 100MB or 1.5GiB"
        )
    unit = match["unit"].upper()
    if not unit:
        raise InvalidSizeError(
            f"size {value!r} has no unit; add one of {SUPPORTED_UNITS_TEXT} (e.g. {value}B)"
        )
    factor = UNIT_FACTORS.get(unit)
    if factor is None:
        raise InvalidSizeError(
            f"unsupported unit {match['unit']!r} in {value!r}; supported units: "
            f"{SUPPORTED_UNITS_TEXT}"
        )

    try:
        total = _EXACT.multiply(Decimal(match["number"]), Decimal(factor))
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex
        raise InvalidSizeError(f"malformed size {value!r}") from exc
    if total != total.to_integral_value():
        raise InvalidSizeError(f"size {value!r} does not resolve to a whole number of bytes")
    num_bytes = int(total)
    if num_bytes > MAX_SIZE_BYTES:
        raise InvalidSizeError(f"size {value!r} is too large (maximum is {MAX_SIZE_BYTES:,} bytes)")
    return num_bytes


def format_size(num_bytes: int) -> str:
    """Format a byte count using binary units, e.g. ``10.00 MiB``."""
    if num_bytes < 0:
        return "-" + format_size(-num_bytes)
    if num_bytes < KIB:
        return f"{num_bytes} B"
    units = ("KiB", "MiB", "GiB", "TiB", "PiB")
    value = num_bytes / KIB
    index = 0
    while value >= KIB and index < len(units) - 1:
        value /= KIB
        index += 1
    return f"{value:.2f} {units[index]}"


def format_size_exact(num_bytes: int) -> str:
    """Format a byte count with both the binary unit and the exact byte count."""
    if num_bytes < KIB:
        return f"{num_bytes} B"
    return f"{format_size(num_bytes)} ({num_bytes:,} bytes)"
