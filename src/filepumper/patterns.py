"""Streaming byte generators for the padding payload.

Every generator yields the payload in bounded-size chunks, so memory use is
independent of the total payload size. Each generator yields *exactly* the
requested number of bytes and never yields an empty chunk (which guarantees
that consumers always make forward progress).

These are generic byte strategies. None of them is format-aware, and none of
them makes any promise that a padded file stays valid for its original format.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from enum import StrEnum

from .exceptions import ValidationError, WriteError

#: Longest accepted repeat pattern in bytes. Kept at the minimum chunk size so
#: a whole pattern always fits in one chunk.
MAX_PATTERN_BYTES = 4096

_HEX_RE = re.compile(r"[0-9a-fA-F]+")

Chunk = bytes | memoryview


class PatternKind(StrEnum):
    """Names of the supported padding strategies."""

    ZERO = "zero"
    RANDOM = "random"
    REPEAT = "repeat"
    BLEND = "blend"


def _check_arguments(total: int, chunk_size: int) -> None:
    if total < 0:
        raise ValueError("total must not be negative")
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")


class PatternGenerator(ABC):
    """Produces exactly ``total`` bytes as a stream of bounded chunks."""

    kind: PatternKind

    def for_file(self, path: str) -> PatternGenerator:
        """Return the generator to actually use when writing ``path``.

        Most patterns are independent of any particular file and return themselves.
        :class:`BlendPattern` overrides this to build a fresh generator sampled from
        that file's own existing bytes, so a batch of different files each get filler
        drawn from their own content rather than one file's content reused for all.
        """
        return self

    @abstractmethod
    def chunks(self, total: int, chunk_size: int) -> Iterator[Chunk]:
        """Yield non-empty chunks whose lengths sum to exactly ``total``."""

    @abstractmethod
    def describe(self) -> str:
        """Return a short human-readable description."""


class ZeroPattern(PatternGenerator):
    """Zero-valued bytes, served from one reused buffer."""

    kind = PatternKind.ZERO

    def __init__(self) -> None:
        self._buffer = b""

    def chunks(self, total: int, chunk_size: int) -> Iterator[Chunk]:
        _check_arguments(total, chunk_size)
        size = min(chunk_size, total)
        if size == 0:
            return
        if len(self._buffer) < size:
            self._buffer = bytes(size)
        view = memoryview(self._buffer)
        remaining = total
        while remaining > 0:
            count = min(size, remaining)
            yield view[:count]
            remaining -= count

    def describe(self) -> str:
        return "zero (all bytes 0x00)"


class RandomPattern(PatternGenerator):
    """Cryptographically strong random bytes from :func:`os.urandom`."""

    kind = PatternKind.RANDOM

    def __init__(self, urandom: Callable[[int], bytes] | None = None) -> None:
        self._urandom = urandom or os.urandom

    def chunks(self, total: int, chunk_size: int) -> Iterator[Chunk]:
        _check_arguments(total, chunk_size)
        remaining = total
        while remaining > 0:
            count = min(chunk_size, remaining)
            data = self._urandom(count)
            if len(data) != count:
                raise ValueError(f"random source returned {len(data)} bytes, expected {count}")
            yield data
            remaining -= count

    def describe(self) -> str:
        return "random (os.urandom)"


class _CyclicPattern(PatternGenerator):
    """Shared chunking for a pattern defined by one fixed byte buffer, repeated and cut
    off exactly at the requested length. Subclasses supply the buffer and ``describe``.
    """

    def __init__(self, buffer: bytes) -> None:
        self._buffer = buffer

    def chunks(self, total: int, chunk_size: int) -> Iterator[Chunk]:
        _check_arguments(total, chunk_size)
        if total == 0:
            return
        length = len(self._buffer)
        if length == 0:
            raise WriteError(f"{self.kind.value} pattern has no bytes to repeat")
        # Every full block is a whole number of repetitions, so the pattern phase
        # restarts at byte 0 for each block and only the last block is truncated.
        per_chunk = max(length, (chunk_size // length) * length)
        needed = -(-total // length) * length
        block_length = min(per_chunk, needed)
        view = memoryview(self._buffer * (block_length // length))
        remaining = total
        while remaining > 0:
            count = min(block_length, remaining)
            yield view[:count]
            remaining -= count


class RepeatPattern(_CyclicPattern):
    """A user-supplied byte sequence repeated until the payload is complete.

    The final repetition is truncated if the payload size is not a multiple of
    the pattern length.
    """

    kind = PatternKind.REPEAT

    def __init__(self, pattern: bytes) -> None:
        if not pattern:
            raise ValidationError("repeat pattern must not be empty")
        if len(pattern) > MAX_PATTERN_BYTES:
            raise ValidationError(
                f"repeat pattern is {len(pattern)} bytes; the maximum is {MAX_PATTERN_BYTES}"
            )
        super().__init__(bytes(pattern))

    @property
    def pattern(self) -> bytes:
        return self._buffer

    def describe(self) -> str:
        shown = self.pattern.hex().upper()
        if len(shown) > 32:
            shown = shown[:32] + "..."
        return f"repeat (0x{shown}, {len(self.pattern)} byte(s))"


#: How much of a file's existing content :class:`BlendPattern` samples to build its
#: repeating filler from. Bounded so building it never depends on the file's full size.
BLEND_SAMPLE_CAP = 65536


class BlendPattern(_CyclicPattern):
    """Filler cycled from a sample of the file's own existing bytes.

    This only changes what the padding *looks like* next to genuine content -- for
    example, it does not read as one obvious flat block the way ``zero`` does. It is
    generic, source-only reuse of bytes already in the file, not an attempt to mimic
    the file format or to hide the fact that bytes were added: ``filepumper detect``
    finds this padding exactly as reliably as any other pattern, because it compares
    the file's own structure to its size rather than recognizing particular content.

    The instance returned by :func:`make_pattern` is a template with no file sample
    yet; :meth:`for_file` resolves it per file, so each file in a batch is padded with
    filler drawn from its own content rather than one file's content reused for all.
    """

    kind = PatternKind.BLEND

    def __init__(self, sample: bytes = b"") -> None:
        super().__init__(sample)

    def for_file(self, path: str) -> PatternGenerator:
        try:
            with open(path, "rb") as handle:
                sample = handle.read(BLEND_SAMPLE_CAP)
        except OSError as exc:
            raise ValidationError(
                f"{path}: could not be read to build blend filler: {exc.strerror or exc}"
            ) from exc
        if not sample:
            raise ValidationError(
                f"{path}: is empty, so the blend pattern has no content to draw from "
                "(use zero, random or repeat instead)"
            )
        return BlendPattern(sample)

    def describe(self) -> str:
        if not self._buffer:
            return "blend (filler drawn from each file's own existing content)"
        return f"blend (filler drawn from a {len(self._buffer)}-byte sample of the file's content)"


def parse_hex_pattern(text: str) -> bytes:
    """Validate and decode a hexadecimal pattern such as ``DEADBEEF`` or ``0xDEADBEEF``."""
    value = text.strip()
    if value[:2].lower() == "0x":
        value = value[2:]
    if not value:
        raise ValidationError("hex pattern is empty")
    if _HEX_RE.fullmatch(value) is None:
        raise ValidationError(
            f"hex pattern {text!r} contains non-hexadecimal characters (use 0-9 and A-F only)"
        )
    if len(value) % 2:
        raise ValidationError(
            f"hex pattern {text!r} has an odd number of digits; whole bytes need two digits each"
        )
    pattern = bytes.fromhex(value)
    if len(pattern) > MAX_PATTERN_BYTES:
        raise ValidationError(
            f"hex pattern is {len(pattern)} bytes; the maximum is {MAX_PATTERN_BYTES}"
        )
    return pattern


def make_pattern(kind: PatternKind | str, repeat_hex: str | None = None) -> PatternGenerator:
    """Create a generator for ``kind``; ``repeat_hex`` is required for ``repeat`` only."""
    try:
        resolved = PatternKind(kind)
    except ValueError:
        choices = ", ".join(item.value for item in PatternKind)
        raise ValidationError(f"unknown pattern {kind!r}; choose one of: {choices}") from None
    if resolved is PatternKind.REPEAT:
        if repeat_hex is None:
            raise ValidationError("the repeat pattern requires a hex pattern")
        return RepeatPattern(parse_hex_pattern(repeat_hex))
    if repeat_hex is not None:
        raise ValidationError(
            f"a hex pattern can only be used with the repeat pattern, not {resolved.value}"
        )
    if resolved is PatternKind.ZERO:
        return ZeroPattern()
    if resolved is PatternKind.BLEND:
        return BlendPattern()
    return RandomPattern()
