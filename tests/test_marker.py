"""The opt-in provenance footer: pack/unpack round trip and corruption handling."""

from __future__ import annotations

import pytest

from filepumper import marker
from filepumper.patterns import PatternKind


def test_size_is_fixed() -> None:
    assert marker.SIZE == 64


@pytest.mark.parametrize("kind", [k.value for k in PatternKind])
def test_round_trips_every_pattern_kind(kind: str) -> None:
    original = marker.Marker(
        original_size=12345, added_bytes=67890, timestamp=1_700_000_000, pattern=kind
    )
    packed = original.pack()
    assert len(packed) == marker.SIZE
    assert marker.unpack(packed) == original


def test_round_trips_zero_and_large_values() -> None:
    m = marker.Marker(original_size=0, added_bytes=0, timestamp=0, pattern="zero")
    assert marker.unpack(m.pack()) == m
    big = marker.Marker(original_size=2**53, added_bytes=2**40, timestamp=2**32, pattern="random")
    assert marker.unpack(big.pack()) == big


def test_unknown_pattern_name_still_round_trips_as_unknown() -> None:
    m = marker.Marker(original_size=1, added_bytes=2, timestamp=3, pattern="something-new")
    back = marker.unpack(m.pack())
    assert back is not None
    assert back.pattern == "unknown"
    assert (back.original_size, back.added_bytes, back.timestamp) == (1, 2, 3)


@pytest.mark.parametrize("flip_byte", [0, 1, 7, 30, 59, 63])
def test_a_single_corrupted_byte_is_rejected(flip_byte: int) -> None:
    packed = bytearray(marker.Marker(100, 200, 300, "zero").pack())
    packed[flip_byte] ^= 0xFF
    assert marker.unpack(bytes(packed)) is None


@pytest.mark.parametrize("size", [0, 1, 63, 65, 128, 1000])
def test_wrong_length_is_rejected(size: int) -> None:
    assert marker.unpack(b"\x00" * size) is None


def test_random_bytes_are_not_mistaken_for_a_marker() -> None:
    import os

    for _ in range(50):
        assert marker.unpack(os.urandom(marker.SIZE)) is None


def test_magic_without_a_valid_crc_is_rejected() -> None:
    body = marker.MAGIC + b"\x00" * (marker.SIZE - 4 - len(marker.MAGIC))
    assert len(body) == marker.SIZE - 4
    bogus = body + b"\x00\x00\x00\x00"  # wrong CRC
    assert marker.unpack(bogus) is None
