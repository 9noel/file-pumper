"""``filepumper.detect`` and the reporting helpers that build on it."""

from __future__ import annotations

from pathlib import Path

import pytest

from _format_samples import SAMPLES
from filepumper import config, detect, engine, marker, reporting
from filepumper.patterns import make_pattern


@pytest.fixture
def settings():
    return config.resolve_performance("conservative", 4096, 1)


def pad(path: Path, kind: str, add: int, *, settings, mark: bool = False) -> None:
    options = engine.PadOptions(
        mode=engine.Mode.ADD, size=add, pattern=make_pattern(kind), settings=settings, mark=mark
    )
    plan = engine.build_plan([str(path)], options)
    result = engine.run_batch(plan)
    assert result.succeeded == 1, result.results[0].error


# ----------------------------------------------------------------------- baseline


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_pristine_sample_is_not_reported_as_pumped(tmp_path: Path, kind: str) -> None:
    path = tmp_path / f"a.{kind}"
    path.write_bytes(SAMPLES[kind])
    report = detect.inspect_file(str(path))
    assert not report.looks_pumped
    assert report.overlay_size == 0
    assert report.marker is None
    assert report.tail_shape.value == "no trailing data"


def test_unrecognized_format_with_no_mark_cannot_be_detected(tmp_path: Path, settings) -> None:
    """Honest limitation: an unstructured blob padded without --mark has no ground
    truth to compare against, so detect correctly reports nothing rather than guessing.
    """
    path = tmp_path / "blob.bin"
    path.write_bytes(b"\x01\x02\x03\x04" * 50)
    pad(path, "zero", 500, settings=settings)
    report = detect.inspect_file(str(path))
    assert not report.looks_pumped
    assert report.format.kind.value == "unknown"


# ------------------------------------------------------------------------ overlay


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_padding_a_recognized_format_is_detected(tmp_path: Path, kind: str, settings) -> None:
    path = tmp_path / f"a.{kind}"
    path.write_bytes(SAMPLES[kind])
    pad(path, "zero", 1000, settings=settings)
    report = detect.inspect_file(str(path))
    assert report.looks_pumped
    assert report.overlay_size == 1000
    assert report.format.kind.value == kind
    assert report.marker is None


def test_zero_pattern_overlay_is_classified_all_same_byte(tmp_path: Path, settings) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(SAMPLES["png"])
    pad(path, "zero", 2000, settings=settings)
    report = detect.inspect_file(str(path))
    assert report.tail_shape.value == "all one byte value"
    assert report.overlay_entropy == 0.0


def test_random_pattern_overlay_is_classified_high_entropy(tmp_path: Path, settings) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(SAMPLES["png"])
    pad(path, "random", 4000, settings=settings)
    report = detect.inspect_file(str(path))
    assert report.tail_shape.value == "high entropy (random-looking)"
    assert report.overlay_entropy is not None and report.overlay_entropy > 7.0


def test_printable_filler_is_classified_as_text(tmp_path: Path, settings) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(SAMPLES["png"])
    ascii_hex = "48656c6c6f20576f726c6421"  # "Hello World!"
    options = engine.PadOptions(
        mode=engine.Mode.ADD,
        size=2000,
        pattern=make_pattern("repeat", ascii_hex),
        settings=settings,
    )
    plan = engine.build_plan([str(path)], options)
    engine.run_batch(plan)
    report = detect.inspect_file(str(path))
    assert report.tail_shape.value == "printable text"


def test_blend_pattern_is_still_detected_even_though_content_blends_in(
    tmp_path: Path, settings
) -> None:
    """The whole point of structural detection: it does not depend on what the padding
    looks like, only on where the format's own content ends versus the file's size."""
    path = tmp_path / "a.exe"
    path.write_bytes(SAMPLES["pe"])
    pad(path, "blend", 2000, settings=settings)
    report = detect.inspect_file(str(path))
    assert report.looks_pumped
    assert report.overlay_size == 2000


# ------------------------------------------------------------------------- marker


def test_mark_is_found_and_decoded(tmp_path: Path, settings) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(SAMPLES["png"])
    original_size = path.stat().st_size
    pad(path, "zero", 2000, settings=settings, mark=True)
    report = detect.inspect_file(str(path))
    assert report.looks_pumped
    assert report.marker is not None
    assert report.marker.original_size == original_size
    assert report.marker.added_bytes == 2000
    assert report.marker.pattern == "zero"


def test_mark_is_found_even_on_an_unrecognized_format(tmp_path: Path, settings) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"\x01\x02\x03\x04" * 50)
    pad(path, "zero", 500, settings=settings, mark=True)
    report = detect.inspect_file(str(path))
    assert report.looks_pumped
    assert report.marker is not None
    assert report.format.kind.value == "unknown"  # structural signal absent, marker present


def test_marker_like_bytes_that_do_not_match_the_crc_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "fake.bin"
    fake = marker.MAGIC + b"\x00" * (marker.SIZE - len(marker.MAGIC))
    path.write_bytes(fake[: marker.SIZE])
    report = detect.inspect_file(str(path))
    assert report.marker is None  # magic present but CRC wrong: correctly rejected


def test_file_shorter_than_the_marker_size(tmp_path: Path) -> None:
    path = tmp_path / "tiny.bin"
    path.write_bytes(b"x" * 10)
    report = detect.inspect_file(str(path))
    assert report.marker is None


# ------------------------------------------------------------------------ reporting


def test_build_pump_reports_json_shape(tmp_path: Path, settings) -> None:
    a = tmp_path / "a.png"
    a.write_bytes(SAMPLES["png"])
    pad(a, "zero", 1000, settings=settings, mark=True)
    b = tmp_path / "b.png"
    b.write_bytes(SAMPLES["png"])

    reports = reporting.build_pump_reports([str(a), str(b)])
    assert len(reports) == 2
    assert reports[0]["looks_pumped"] is True
    assert reports[0]["marker"]["pattern"] == "zero"
    assert reports[0]["overlay_size_bytes"] == 1000
    assert reports[1]["looks_pumped"] is False
    assert reports[1]["marker"] is None

    import json

    json.dumps(reports)  # must be JSON-serializable as-is


def test_build_pump_reports_handles_a_missing_file(tmp_path: Path) -> None:
    reports = reporting.build_pump_reports([str(tmp_path / "does-not-exist.bin")])
    assert len(reports) == 1
    assert "error" in reports[0]
    assert "looks_pumped" not in reports[0]


def test_format_pump_reports_text(tmp_path: Path, settings) -> None:
    a = tmp_path / "a.png"
    a.write_bytes(SAMPLES["png"])
    pad(a, "zero", 1000, settings=settings)
    reports = reporting.build_pump_reports([str(a)])
    text = reporting.format_pump_reports(reports)
    assert "PUMPED" in text
    assert "PNG image" in text
    assert "1/1 file(s) show signs of padding" in text


def test_format_pump_reports_text_for_a_clean_file(tmp_path: Path) -> None:
    a = tmp_path / "a.png"
    a.write_bytes(SAMPLES["png"])
    reports = reporting.build_pump_reports([str(a)])
    text = reporting.format_pump_reports(reports)
    assert "no sign of padding found" in text
    assert "0/1 file(s)" in text


def test_format_pump_reports_text_for_an_error(tmp_path: Path) -> None:
    reports = reporting.build_pump_reports([str(tmp_path / "missing.bin")])
    text = reporting.format_pump_reports(reports)
    assert "could not be inspected" in text
