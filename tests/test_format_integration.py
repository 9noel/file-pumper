"""Engine-level behavior added for format-awareness, blend and --mark: plan-time
validation, FilePlan.format, exact sizing, per-file blend sampling, and verify."""

from __future__ import annotations

import pytest

from _format_samples import SAMPLES
from filepumper import engine, formats, marker
from filepumper.exceptions import ValidationError
from filepumper.patterns import make_pattern

# ----------------------------------------------------------------- plan-time: format


def test_plan_carries_recognized_format_info(make_file, make_options) -> None:
    path = make_file("a.png", SAMPLES["png"])
    options = make_options(add=1000)
    plan = engine.build_plan([str(path)], options)
    assert plan.files[0].format.kind is formats.FormatKind.PNG
    assert plan.files[0].format.append_is_structure_safe
    assert plan.files[0].warnings == ()  # no spurious "may be rejected" warning


def test_plan_warns_when_structure_is_inconsistent(make_file, make_options) -> None:
    # Claims a huge IHDR chunk length it does not actually have: PNG, but not confidently safe.
    broken = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x10\x00IHDR"
    path = make_file("bad.png", broken)
    options = make_options(add=100)
    plan = engine.build_plan([str(path)], options)
    item = plan.files[0]
    assert item.format.kind is formats.FormatKind.PNG
    assert not item.format.append_is_structure_safe
    assert any("cannot confirm appending stays safe" in w for w in item.warnings)


def test_plan_falls_back_to_extension_guess_for_unrecognized_content(
    make_file, make_options
) -> None:
    path = make_file("fake.pdf", b"not actually a pdf")
    options = make_options(add=100)
    plan = engine.build_plan([str(path)], options)
    item = plan.files[0]
    assert item.format.kind is formats.FormatKind.UNKNOWN
    assert any("suggests a PDF document" in w for w in item.warnings)


def test_plan_has_no_warning_for_a_plain_file_with_no_suggestive_extension(
    make_file, make_options
) -> None:
    path = make_file("data", b"just bytes")
    options = make_options(add=100)
    plan = engine.build_plan([str(path)], options)
    assert plan.files[0].warnings == ()


# ----------------------------------------------------------------------- plan: mark


def test_mark_rejected_when_padding_is_smaller_than_the_footer(make_file, make_options) -> None:
    path = make_file("a.bin", b"x" * 10)
    options = make_options(add=marker.SIZE - 1, mark=True)
    with pytest.raises(ValidationError, match="--mark needs at least"):
        engine.build_plan([str(path)], options)


def test_mark_exactly_at_the_footer_size_is_accepted(make_file, make_options) -> None:
    path = make_file("a.bin", b"x" * 10)
    options = make_options(add=marker.SIZE, mark=True)
    plan = engine.build_plan([str(path)], options)  # must not raise
    assert plan.files[0].add_bytes == marker.SIZE


def test_mark_with_zero_add_is_a_skip_not_an_error(make_file, make_options) -> None:
    path = make_file("a.bin", b"x" * 10)
    options = make_options(add=0, mark=True)
    plan = engine.build_plan([str(path)], options)
    assert plan.files[0].skip_reason is not None


def test_mark_plan_check_applies_per_file(make_file, make_options) -> None:
    big = make_file("big.bin", b"x" * 10, subdir="a")
    small = make_file("small.bin", b"x" * 10, subdir="b")
    options = make_options(add=marker.SIZE, mark=True)
    plan = engine.build_plan([str(big), str(small)], options)
    assert len(plan.files) == 2  # both at exactly SIZE: both fine


# ---------------------------------------------------------------------- plan: blend


def test_blend_rejected_for_an_empty_source_file(make_file, make_options) -> None:
    path = make_file("empty.bin", b"")
    options = make_options(add=100, pattern=make_pattern("blend"))
    with pytest.raises(ValidationError, match="blend pattern has no content"):
        engine.build_plan([str(path)], options)


def test_blend_accepted_for_a_non_empty_source_file(make_file, make_options) -> None:
    path = make_file("a.bin", b"some content")
    options = make_options(add=100, pattern=make_pattern("blend"))
    plan = engine.build_plan([str(path)], options)  # must not raise
    assert plan.files[0].add_bytes == 100


def test_blend_skipped_file_with_empty_source_is_not_an_error(make_file, make_options) -> None:
    # add=0 means the file is skipped before it would ever need blend content.
    path = make_file("empty.bin", b"")
    options = make_options(add=0, pattern=make_pattern("blend"))
    plan = engine.build_plan([str(path)], options)
    assert plan.files[0].skip_reason is not None


# --------------------------------------------------------------------- write: mark


def test_mark_consumes_the_requested_size_exactly(make_file, run) -> None:
    path = make_file("a.bin", b"original content")
    original_size = path.stat().st_size
    result = run([path], add=500, mark=True)
    assert result.succeeded == 1
    assert path.stat().st_size == original_size + 500  # exactly what was requested
    with path.open("rb") as handle:
        handle.seek(-marker.SIZE, 2)
        found = marker.unpack(handle.read())
    assert found is not None
    assert found.original_size == original_size
    assert found.added_bytes == 500


def test_mark_survives_verify(make_file, run) -> None:
    path = make_file("a.bin", b"content to keep")
    result = run([path], add=500, mark=True, verify=True)
    assert result.succeeded == 1
    assert result.results[0].verified is True


def test_mark_is_absent_when_not_requested(make_file, run) -> None:
    path = make_file("a.bin", b"content")
    run([path], add=200)
    with path.open("rb") as handle:
        handle.seek(-marker.SIZE, 2)
        assert marker.unpack(handle.read()) is None


def test_mark_with_target_mode(make_file, run) -> None:
    path = make_file("a.bin", b"x" * 50)
    result = run([path], target=1000, mark=True)
    assert result.succeeded == 1
    assert path.stat().st_size == 1000
    with path.open("rb") as handle:
        handle.seek(-marker.SIZE, 2)
        found = marker.unpack(handle.read())
    assert found is not None and found.added_bytes == 950


# -------------------------------------------------------------------- write: blend


def test_blend_fills_with_cycled_source_content(make_file, run) -> None:
    path = make_file("a.bin", b"AB")
    result = run([path], add=7, pattern=make_pattern("blend"))
    assert result.succeeded == 1
    assert path.read_bytes() == b"AB" + b"ABABABA"


def test_blend_samples_each_file_independently_in_one_batch(make_file, run) -> None:
    first = make_file("a.bin", b"11", subdir="a")
    second = make_file("b.bin", b"22", subdir="b")
    result = run([first, second], add=6, pattern=make_pattern("blend"))
    assert result.succeeded == 2
    assert first.read_bytes() == b"11" + b"111111"
    assert second.read_bytes() == b"22" + b"222222"


def test_blend_sample_is_capped_and_still_cycles_correctly(make_file, run) -> None:
    from filepumper.patterns import BLEND_SAMPLE_CAP

    content = (b"xy" * (BLEND_SAMPLE_CAP // 2 + 10))[: BLEND_SAMPLE_CAP + 20]
    path = make_file("big.bin", content)
    result = run([path], add=10, pattern=make_pattern("blend"))
    assert result.succeeded == 1
    tail = path.read_bytes()[-10:]
    assert tail == content[:BLEND_SAMPLE_CAP][:10]


# --------------------------------------------------------------- write: format-safe


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_padding_a_recognized_format_preserves_its_structural_bytes(
    make_file, run, kind: str
) -> None:
    path = make_file(f"a.{kind}", SAMPLES[kind])
    structural_size = formats.analyze(str(path)).structural_size
    result = run([path], add=777)
    assert result.succeeded == 1
    assert path.read_bytes()[:structural_size] == SAMPLES[kind]
    info_after = formats.analyze(str(path))
    assert info_after.structural_size == structural_size  # unmoved by the padding
