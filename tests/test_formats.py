"""Structural analysis: pristine files, overlay detection, and defensive handling of
truncated/malformed/adversarial input (must never raise)."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from _format_samples import SAMPLES, jpeg_bytes, pdf_bytes, pe_bytes, zip_bytes
from filepumper import formats


@pytest.fixture(params=sorted(SAMPLES))
def sample_kind(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def sample_file(tmp_path: Path, sample_kind: str) -> Path:
    path = tmp_path / f"sample.{sample_kind}"
    path.write_bytes(SAMPLES[sample_kind])
    return path


# ----------------------------------------------------------------------------- sniff


def test_sniff_identifies_every_sample(sample_kind: str) -> None:
    assert formats.sniff(SAMPLES[sample_kind][:64]).value == sample_kind


def test_sniff_handles_short_and_empty_input() -> None:
    assert formats.sniff(b"") is formats.FormatKind.UNKNOWN
    assert formats.sniff(b"M") is formats.FormatKind.UNKNOWN
    assert formats.sniff(b"not a known format") is formats.FormatKind.UNKNOWN


# --------------------------------------------------------------------------- analyze


def test_pristine_sample_has_no_overlay(sample_file: Path, sample_kind: str) -> None:
    size = sample_file.stat().st_size
    info = formats.analyze(str(sample_file))
    assert info.kind.value == sample_kind
    assert info.structural_size == size
    assert info.overlay_size == 0
    assert info.confident
    assert info.append_is_structure_safe
    assert "no existing overlay" in info.note


@pytest.mark.parametrize("extra", [1, 37, 4096])
def test_appended_bytes_are_detected_as_overlay(sample_file: Path, extra: int) -> None:
    size = sample_file.stat().st_size
    with sample_file.open("ab") as handle:
        handle.write(b"\xaa" * extra)
    info = formats.analyze(str(sample_file))
    assert info.structural_size == size
    assert info.overlay_size == extra
    assert info.append_is_structure_safe


def test_unrecognized_file_makes_no_claim(tmp_path: Path) -> None:
    path = tmp_path / "plain.txt"
    path.write_bytes(b"just some ordinary text content, nothing special here\n" * 3)
    info = formats.analyze(str(path))
    assert info.kind is formats.FormatKind.UNKNOWN
    assert info.structural_size is None
    assert info.overlay_size == 0
    assert not info.append_is_structure_safe


def test_empty_and_missing_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    info = formats.analyze(str(empty))
    assert info.kind is formats.FormatKind.UNKNOWN
    assert not info.append_is_structure_safe

    missing = formats.analyze(str(tmp_path / "does-not-exist"))
    assert missing.kind is formats.FormatKind.UNKNOWN
    assert not missing.confident


def test_truncation_never_raises_and_is_not_claimed_safe(tmp_path: Path, sample_kind: str) -> None:
    data = SAMPLES[sample_kind]
    for cut in (1, len(data) // 2):  # a truncated-by-1 PDF can still end cleanly at '%%EOF'
        path = tmp_path / f"truncated.{sample_kind}"
        path.write_bytes(data[:cut])
        info = formats.analyze(str(path))  # must not raise
        assert not info.append_is_structure_safe


def test_garbage_with_valid_magic_bytes_never_raises(tmp_path: Path, sample_kind: str) -> None:
    # The first bytes sniff as a real format, but everything after is adversarial noise.
    path = tmp_path / f"garbage.{sample_kind}"
    path.write_bytes(SAMPLES[sample_kind][:8] + bytes(range(256)) * 20)
    info = formats.analyze(str(path))  # must not raise regardless of the verdict
    assert info.kind.value == sample_kind or info.kind is formats.FormatKind.UNKNOWN


def test_unreadable_file_is_reported_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "locked.bin"
    path.write_bytes(b"x" * 10)
    path.chmod(0)
    try:
        info = formats.analyze(str(path))
        assert info.kind is formats.FormatKind.UNKNOWN
    finally:
        path.chmod(0o644)


# ------------------------------------------------------------------------------- PE


def test_pe_overlay_includes_certificate_table(tmp_path: Path) -> None:
    data = pe_bytes(with_cert=True)
    path = tmp_path / "signed.exe"
    path.write_bytes(data)
    info = formats.analyze(str(path))
    assert info.kind is formats.FormatKind.PE
    assert info.structural_size == len(data)  # cert table is the last thing, correctly included
    assert info.overlay_size == 0


def test_pe_pe32_and_pe32_plus_both_parse(tmp_path: Path) -> None:
    for plus in (True, False):
        data = pe_bytes(pe32_plus=plus)
        path = tmp_path / f"a{plus}.exe"
        path.write_bytes(data)
        info = formats.analyze(str(path))
        assert info.structural_size == len(data)


def test_pe_zero_sections(tmp_path: Path) -> None:
    data = pe_bytes(num_sections=0)
    path = tmp_path / "nosections.exe"
    path.write_bytes(data)
    info = formats.analyze(str(path))
    assert info.kind is formats.FormatKind.PE
    assert info.structural_size == len(data)


def test_pe_implausible_section_count_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.exe"
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 64)
    coff = struct.pack("<HHIIIHH", 0x8664, 60000, 0, 0, 0, 0, 0)  # implausible section count
    path.write_bytes(bytes(dos) + b"PE\x00\x00" + coff)
    info = formats.analyze(str(path))
    assert not info.append_is_structure_safe


def test_non_mz_header_is_not_pe(tmp_path: Path) -> None:
    path = tmp_path / "notpe.exe"
    path.write_bytes(b"MZ" + b"\x00" * 10)  # too short for a real DOS header
    info = formats.analyze(str(path))
    assert not info.append_is_structure_safe


# ------------------------------------------------------------------------------ ZIP


def test_zip_with_a_comment_is_included_in_structural_size(tmp_path: Path) -> None:
    import zipfile

    path = tmp_path / "commented.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.txt", "hi")
        zf.comment = b"a trailing comment"
    info = formats.analyze(str(path))
    assert info.structural_size == path.stat().st_size
    assert info.overlay_size == 0


def test_zip_without_eocd_is_not_safe(tmp_path: Path) -> None:
    path = tmp_path / "broken.zip"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 50)  # local header but no EOCD anywhere
    info = formats.analyze(str(path))
    assert not info.append_is_structure_safe


def test_office_style_zip_is_recognized_as_zip(tmp_path: Path) -> None:
    path = tmp_path / "doc.docx"
    path.write_bytes(zip_bytes({"word/document.xml": b"<xml/>"}))
    info = formats.analyze(str(path))
    assert info.kind is formats.FormatKind.ZIP
    assert info.overlay_size == 0


# ------------------------------------------------------------------------------- PDF


def test_pdf_trailing_eol_after_eof_is_not_overlay(tmp_path: Path) -> None:
    for eol in (b"\n", b"\r\n", b"\r", b""):
        data = pdf_bytes().rstrip(b"\n") + eol
        path = tmp_path / "x.pdf"
        path.write_bytes(data)
        info = formats.analyze(str(path))
        assert info.overlay_size == 0, eol


def test_pdf_uses_the_last_eof_for_incremental_updates(tmp_path: Path) -> None:
    first = pdf_bytes()
    second_update = b"\n2 0 obj\n<< >>\nendobj\nstartxref\n0\n%%EOF\n"
    data = first + second_update
    path = tmp_path / "updated.pdf"
    path.write_bytes(data)
    info = formats.analyze(str(path))
    assert info.structural_size == len(data)


def test_pdf_without_eof_marker_is_not_safe(tmp_path: Path) -> None:
    path = tmp_path / "noeof.pdf"
    path.write_bytes(b"%PDF-1.4\nsome content with no proper trailer at all")
    info = formats.analyze(str(path))
    assert not info.append_is_structure_safe


# ------------------------------------------------------------------------------- PNG


def test_png_chunk_running_past_the_end_is_not_safe(tmp_path: Path) -> None:
    path = tmp_path / "bad.png"
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I4s", 1000, b"IHDR")  # claims 1000 bytes, has 0
    path.write_bytes(header)
    info = formats.analyze(str(path))
    assert not info.append_is_structure_safe


def test_png_without_iend_is_not_safe(tmp_path: Path) -> None:
    path = tmp_path / "noiend.png"
    path.write_bytes(SAMPLES["png"][:30])  # cut before IEND
    info = formats.analyze(str(path))
    assert not info.append_is_structure_safe


# ------------------------------------------------------------------------------ JPEG


def test_jpeg_stuffed_and_restart_bytes_are_not_mistaken_for_markers(tmp_path: Path) -> None:
    data = jpeg_bytes(with_restart_and_stuffing=True)
    path = tmp_path / "x.jpg"
    path.write_bytes(data)
    info = formats.analyze(str(path))
    assert info.structural_size == len(data)
    assert info.overlay_size == 0


def test_jpeg_without_eoi_is_not_safe(tmp_path: Path) -> None:
    data = jpeg_bytes()[:-2]  # drop the EOI marker
    path = tmp_path / "noeoi.jpg"
    path.write_bytes(data)
    info = formats.analyze(str(path))
    assert not info.append_is_structure_safe


def test_jpeg_fill_bytes_before_a_marker_are_skipped(tmp_path: Path) -> None:
    # Any number of extra 0xFF bytes may precede a real marker code per the JPEG spec.
    data = b"\xff\xd8\xff\xff\xff\xd9"  # SOI, then padded EOI
    path = tmp_path / "fill.jpg"
    path.write_bytes(data)
    info = formats.analyze(str(path))
    assert info.structural_size == len(data)


# ------------------------------------------------------------------------------- GIF


def test_gif_with_global_color_table(tmp_path: Path) -> None:
    header = b"GIF89a" + struct.pack("<HHBBB", 2, 2, 0x80, 0, 0)  # global color table, 2 entries
    table = bytes(3 * 2)
    data = header + table + b"\x3b"
    path = tmp_path / "gct.gif"
    path.write_bytes(data)
    info = formats.analyze(str(path))
    assert info.structural_size == len(data)
    assert info.overlay_size == 0


def test_gif_with_extension_block(tmp_path: Path) -> None:
    header = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0, 0, 0)
    # Graphic control extension: label 0xF9, one 4-byte sub-block, then the terminator.
    ext = b"\x21\xf9" + bytes([4, 0, 0, 0, 0]) + bytes([0])
    data = header + ext + b"\x3b"
    path = tmp_path / "ext.gif"
    path.write_bytes(data)
    info = formats.analyze(str(path))
    assert info.structural_size == len(data)


def test_gif_without_trailer_is_not_safe(tmp_path: Path) -> None:
    data = SAMPLES["gif"][:-1]  # drop the trailer byte
    path = tmp_path / "notrailer.gif"
    path.write_bytes(data)
    info = formats.analyze(str(path))
    assert not info.append_is_structure_safe


def test_gif_unrecognized_block_introducer_is_not_safe(tmp_path: Path) -> None:
    header = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0, 0, 0)
    path = tmp_path / "bad.gif"
    path.write_bytes(header + b"\x99" + b"\x3b")  # 0x99 is not a valid block introducer
    info = formats.analyze(str(path))
    assert not info.append_is_structure_safe
