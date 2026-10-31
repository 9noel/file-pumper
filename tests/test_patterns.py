from __future__ import annotations

import tracemalloc

import pytest

from filepumper.exceptions import ValidationError
from filepumper.patterns import (
    MAX_PATTERN_BYTES,
    PatternKind,
    RandomPattern,
    RepeatPattern,
    ZeroPattern,
    make_pattern,
    parse_hex_pattern,
)


def collect(generator, total: int, chunk_size: int) -> bytes:
    return b"".join(bytes(chunk) for chunk in generator.chunks(total, chunk_size))


@pytest.mark.parametrize("total", [0, 1, 7, 4096, 4097, 10_000])
@pytest.mark.parametrize("chunk_size", [1, 3, 4096, 65536])
def test_every_pattern_yields_exactly_total_bytes(total: int, chunk_size: int) -> None:
    for generator in (ZeroPattern(), RandomPattern(), RepeatPattern(b"\xde\xad\xbe")):
        chunks = list(generator.chunks(total, chunk_size))
        assert sum(memoryview(c).nbytes for c in chunks) == total
        assert all(memoryview(c).nbytes > 0 for c in chunks), "empty chunks stall consumers"


# ------------------------------------------------------------------------------ zero


def test_zero_output_is_all_zero() -> None:
    assert collect(ZeroPattern(), 10_000, 4096) == bytes(10_000)


def test_zero_reuses_one_buffer() -> None:
    generator = ZeroPattern()
    owners = {id(chunk.obj) for chunk in generator.chunks(1_000_000, 4096)}  # type: ignore[union-attr]
    assert len(owners) == 1
    again = {id(chunk.obj) for chunk in generator.chunks(50_000, 4096)}  # type: ignore[union-attr]
    assert again == owners


def test_zero_with_nothing_requested_yields_nothing() -> None:
    assert list(ZeroPattern().chunks(0, 4096)) == []


# ---------------------------------------------------------------------------- random


def test_random_has_requested_length_and_is_not_constant() -> None:
    data = collect(RandomPattern(), 100_000, 4096)
    assert len(data) == 100_000
    assert len(set(data)) > 200  # 100k random bytes cover nearly every byte value


def test_random_calls_urandom_in_bounded_pieces() -> None:
    sizes: list[int] = []

    def fake(count: int) -> bytes:
        sizes.append(count)
        return b"\x07" * count

    data = collect(RandomPattern(urandom=fake), 10_000, 4096)
    assert data == b"\x07" * 10_000
    assert sizes == [4096, 4096, 1808]


def test_random_rejects_short_reads_from_source() -> None:
    generator = RandomPattern(urandom=lambda count: b"x" * (count - 1))
    with pytest.raises(ValueError, match="returned"):
        list(generator.chunks(100, 64))


def test_two_random_streams_differ() -> None:
    assert collect(RandomPattern(), 4096, 4096) != collect(RandomPattern(), 4096, 4096)


# --------------------------------------------------------------------------- repeat


def test_repeat_full_repetitions() -> None:
    assert collect(RepeatPattern(b"\xde\xad\xbe\xef"), 12, 4096) == b"\xde\xad\xbe\xef" * 3


def test_repeat_final_partial_pattern() -> None:
    assert (
        collect(RepeatPattern(b"\xde\xad\xbe\xef"), 10, 4096)
        == b"\xde\xad\xbe\xef" * 2 + b"\xde\xad"
    )


def test_repeat_output_smaller_than_pattern() -> None:
    assert collect(RepeatPattern(b"\x01\x02\x03\x04\x05"), 3, 4096) == b"\x01\x02\x03"


def test_repeat_single_byte_pattern() -> None:
    assert collect(RepeatPattern(b"\xab"), 9, 4) == b"\xab" * 9


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7, 4096])
def test_repeat_phase_is_continuous_across_chunk_boundaries(chunk_size: int) -> None:
    pattern = b"\x01\x02\x03"
    total = 100
    expected = (pattern * 40)[:total]
    assert collect(RepeatPattern(pattern), total, chunk_size) == expected


def test_repeat_rejects_empty_and_oversized_patterns() -> None:
    with pytest.raises(ValidationError, match="empty"):
        RepeatPattern(b"")
    with pytest.raises(ValidationError, match="maximum"):
        RepeatPattern(b"x" * (MAX_PATTERN_BYTES + 1))
    assert RepeatPattern(b"x" * MAX_PATTERN_BYTES)


# ------------------------------------------------------------------------------ hex


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("DEADBEEF", b"\xde\xad\xbe\xef"),
        ("deadbeef", b"\xde\xad\xbe\xef"),
        ("0xDEADBEEF", b"\xde\xad\xbe\xef"),
        ("0XAB", b"\xab"),
        ("  00ff  ", b"\x00\xff"),
        ("00", b"\x00"),
    ],
)
def test_parse_hex_pattern(text: str, expected: bytes) -> None:
    assert parse_hex_pattern(text) == expected


@pytest.mark.parametrize(
    "text", ["", "   ", "0x", "ABC", "GG", "DE AD", "DE:AD", "0xZZ", "xyz", "12 34", "--", "ÀB"]
)
def test_malformed_hex_rejected(text: str) -> None:
    with pytest.raises(ValidationError):
        parse_hex_pattern(text)


def test_oversized_hex_rejected() -> None:
    with pytest.raises(ValidationError, match="maximum"):
        parse_hex_pattern("AA" * (MAX_PATTERN_BYTES + 1))


# ----------------------------------------------------------------------------- factory


def test_make_pattern_kinds() -> None:
    assert isinstance(make_pattern("zero"), ZeroPattern)
    assert isinstance(make_pattern(PatternKind.RANDOM), RandomPattern)
    repeat = make_pattern("repeat", "ab")
    assert isinstance(repeat, RepeatPattern)
    assert repeat.pattern == b"\xab"


def test_make_pattern_validation() -> None:
    with pytest.raises(ValidationError, match="requires"):
        make_pattern("repeat")
    with pytest.raises(ValidationError, match="only be used with"):
        make_pattern("zero", "AB")
    with pytest.raises(ValidationError, match="unknown pattern"):
        make_pattern("sparse")
    with pytest.raises(ValidationError):
        make_pattern("repeat", "XYZ")


@pytest.mark.parametrize(
    "generator", [ZeroPattern(), RepeatPattern(b"\x01\x02\x03"), RandomPattern()]
)
def test_invalid_arguments(generator) -> None:
    with pytest.raises(ValueError, match="negative"):
        list(generator.chunks(-1, 10))
    with pytest.raises(ValueError, match="chunk_size"):
        list(generator.chunks(10, 0))


def test_descriptions_are_informative() -> None:
    assert "zero" in ZeroPattern().describe()
    assert "urandom" in RandomPattern().describe()
    assert "DEADBEEF" in RepeatPattern(b"\xde\xad\xbe\xef").describe()


# ------------------------------------------------------------------------------ memory


@pytest.mark.parametrize(
    "generator",
    [ZeroPattern(), RandomPattern(), RepeatPattern(b"\xde\xad\xbe\xef\x01")],
    ids=["zero", "random", "repeat"],
)
def test_streaming_uses_bounded_memory(generator) -> None:
    total = 256 * 1024 * 1024  # 256 MiB logical payload, never held in memory
    if isinstance(generator, RandomPattern):
        total = 16 * 1024 * 1024  # keep the entropy source test fast
    chunk_size = 64 * 1024
    produced = 0
    tracemalloc.start()
    try:
        for chunk in generator.chunks(total, chunk_size):
            produced += memoryview(chunk).nbytes
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert produced == total
    assert peak < 1024 * 1024, f"peak {peak} bytes is not independent of the payload size"
