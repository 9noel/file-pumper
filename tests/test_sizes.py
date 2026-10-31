from __future__ import annotations

import pytest

from filepumper.exceptions import InvalidSizeError
from filepumper.sizes import (
    GIB,
    KIB,
    MAX_SIZE_BYTES,
    MIB,
    TIB,
    format_size,
    format_size_exact,
    parse_size,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0B", 0),
        ("1B", 1),
        ("512B", 512),
        ("1KB", KIB),
        ("1KiB", KIB),
        ("100MB", 100 * MIB),
        ("100MiB", 100 * MIB),
        ("2GB", 2 * GIB),
        ("2GiB", 2 * GIB),
        ("1TB", TIB),
        ("1TiB", TIB),
        ("1.5KiB", 1536),
        ("0.5MB", MIB // 2),
        ("1.25GiB", GIB + GIB // 4),
        ("10 MB", 10 * MIB),
        ("  10MB  ", 10 * MIB),
        ("007MB", 7 * MIB),
    ],
)
def test_valid_sizes(text: str, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["10mb", "10Mb", "10mB", "10mib", "10MIB", "10kIb", "10b"])
def test_units_are_case_insensitive(text: str) -> None:
    assert parse_size(text) > 0


def test_decimal_and_binary_units_agree() -> None:
    for pair in (("1KB", "1KiB"), ("1MB", "1MiB"), ("1GB", "1GiB"), ("1TB", "1TiB")):
        assert parse_size(pair[0]) == parse_size(pair[1])


@pytest.mark.parametrize("text", ["", "   ", "\t\n"])
def test_empty_input(text: str) -> None:
    with pytest.raises(InvalidSizeError, match="empty"):
        parse_size(text)


@pytest.mark.parametrize("text", ["MB", "KiB", ".MB", "B"])
def test_missing_number(text: str) -> None:
    with pytest.raises(InvalidSizeError):
        parse_size(text)


@pytest.mark.parametrize("text", ["100", "0", "1.5"])
def test_unit_is_required(text: str) -> None:
    with pytest.raises(InvalidSizeError, match="no unit"):
        parse_size(text)


@pytest.mark.parametrize(
    "text", ["10XB", "10PB", "10EB", "10bytes", "10 megabytes", "10M", "10K", "10G"]
)
def test_unsupported_units(text: str) -> None:
    with pytest.raises(InvalidSizeError, match="unsupported unit"):
        parse_size(text)


@pytest.mark.parametrize("text", ["-1MB", "-0B", " -5KB"])
def test_negative_rejected(text: str) -> None:
    with pytest.raises(InvalidSizeError, match="negative"):
        parse_size(text)


def test_explicit_plus_rejected() -> None:
    with pytest.raises(InvalidSizeError):
        parse_size("+5MB")


@pytest.mark.parametrize("text", ["inf", "Infinity", "-inf", "nan", "NaN", "+inf"])
def test_non_finite_rejected(text: str) -> None:
    with pytest.raises(InvalidSizeError):
        parse_size(text)


@pytest.mark.parametrize(
    "text",
    [
        "1e3MB",
        "1,000MB",
        "1_000MB",
        "0x10MB",
        "1..5MB",
        "1.MB",
        "5 5MB",
        "1/2MB",
        "١٠MB",  # noqa: RUF001  (non-ASCII digits must be rejected)
        "10MB!",
        "1.5.2MB",
    ],
)
def test_malformed_rejected(text: str) -> None:
    with pytest.raises(InvalidSizeError):
        parse_size(text)


@pytest.mark.parametrize("text", ["0.5B", "1.5B", "0.0001KiB", "1.0001KB"])
def test_fractional_bytes_rejected(text: str) -> None:
    with pytest.raises(InvalidSizeError, match="whole number"):
        parse_size(text)


def test_fraction_that_is_whole_is_accepted() -> None:
    assert parse_size("0.0009765625MiB") == 1024
    assert parse_size("2.0KiB") == 2048


def test_maximum_is_accepted_and_one_more_is_rejected() -> None:
    assert parse_size(f"{MAX_SIZE_BYTES}B") == MAX_SIZE_BYTES
    with pytest.raises(InvalidSizeError, match="too large"):
        parse_size(f"{MAX_SIZE_BYTES + 1}B")


@pytest.mark.parametrize(
    "text", ["9999999999TB", "9223372036854775808B", "8192PB", "99999999999999999999999999GB"]
)
def test_overflow_rejected(text: str) -> None:
    with pytest.raises(InvalidSizeError):
        parse_size(text)


def test_overlong_input_rejected() -> None:
    with pytest.raises(InvalidSizeError, match="too long"):
        parse_size("1" * 100 + "B")


def test_non_string_rejected() -> None:
    with pytest.raises(InvalidSizeError):
        parse_size(100)  # type: ignore[arg-type]


def test_invalid_size_error_is_a_value_error() -> None:
    assert issubclass(InvalidSizeError, ValueError)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.00 KiB"),
        (1536, "1.50 KiB"),
        (MIB, "1.00 MiB"),
        (100 * MIB, "100.00 MiB"),
        (GIB, "1.00 GiB"),
        (TIB, "1.00 TiB"),
        (-2048, "-2.00 KiB"),
    ],
)
def test_format_size(value: int, expected: str) -> None:
    assert format_size(value) == expected


def test_format_size_exact() -> None:
    assert format_size_exact(10) == "10 B"
    assert format_size_exact(MIB) == "1.00 MiB (1,048,576 bytes)"


def test_format_size_handles_huge_values() -> None:
    assert format_size(MAX_SIZE_BYTES).endswith("PiB")


def test_add_versus_target_semantics_use_the_same_parser() -> None:
    """``--add 1MB`` and ``--target 1MB`` share one definition of 1MB."""
    assert parse_size("1MB") == parse_size("1MiB") == 1_048_576
