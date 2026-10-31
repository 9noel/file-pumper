"""Hand-built, minimal valid files for each format ``filepumper.formats`` recognizes.

These are not real, renderable images/executables -- just byte structures with correct
header/chunk/section layout, built from the public format specifications using only the
standard library (no third-party image/PE/PDF libraries), so the structural parsers can
be tested against known-correct offsets without any external tooling.
"""

from __future__ import annotations

import struct
import zlib


def png_bytes(width: int = 1, height: int = 1) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes((width * 3 + 1) * height))
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def gif_bytes() -> bytes:
    header = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0, 0, 0)  # no global color table
    image = b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0)
    image += bytes([2])  # LZW minimum code size
    image += bytes([2, 0x4C, 0x01]) + bytes([0])  # one sub-block, then the terminator
    return header + image + b"\x3b"


def jpeg_bytes(*, with_restart_and_stuffing: bool = True) -> bytes:
    soi = b"\xff\xd8"
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">H", 11) + b"\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    dht = b"\xff\xc4" + struct.pack(">H", 4) + b"\x00\x00"
    sos = b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x00\x3f\x00"
    entropy = b"\x12\x34"
    if with_restart_and_stuffing:
        # A stuffed FF00 and an embedded restart marker: both must be treated as part of
        # the entropy-coded data, not as segment boundaries.
        entropy += b"\xff\x00\x56\xff\xd0\x78\x9a"
    else:
        entropy += b"\x56\x78\x9a"
    eoi = b"\xff\xd9"
    return soi + app0 + sof0 + dht + sos + entropy + eoi


def pdf_bytes() -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
        b"xref\n0 1\n0000000000 65535 f \n"
        b"trailer\n<< /Size 1 /Root 1 0 R >>\n"
        b"startxref\n9\n%%EOF\n"
    )


def pe_bytes(num_sections: int = 2, *, pe32_plus: bool = True, with_cert: bool = False) -> bytes:
    e_lfanew = 64
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, e_lfanew)

    machine = 0x8664 if pe32_plus else 0x014C
    opt_size = 0xF0 if pe32_plus else 0xE0
    coff = struct.pack("<HHIIIHH", machine, num_sections, 0, 0, 0, opt_size, 0x0022)

    optional = bytearray(opt_size)
    struct.pack_into("<H", optional, 0, 0x20B if pe32_plus else 0x10B)
    dir_base = 0x70 if pe32_plus else 0x60

    section_table_start = e_lfanew + 4 + len(coff) + opt_size
    section_size = 512
    sections = b""
    end = section_table_start + num_sections * 40
    for i in range(num_sections):
        name = f".s{i}".encode().ljust(8, b"\x00")[:8]
        sections += name + struct.pack("<IIIIIIHHI", 0, 0, section_size, end, 0, 0, 0, 0, 0)
        end += section_size

    if with_cert:
        cert_offset, cert_size = end, 128
        struct.pack_into("<II", optional, dir_base + 4 * 8, cert_offset, cert_size)
        end = cert_offset + cert_size

    data = bytes(dos) + b"PE\x00\x00" + coff + bytes(optional) + sections
    return data + bytes(end - len(data))


def zip_bytes(names_and_contents: dict[str, bytes] | None = None) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in (names_and_contents or {"a.txt": b"hello", "b.txt": b"world"}).items():
            zf.writestr(name, content)
    return buffer.getvalue()


#: One pristine sample per recognized format, keyed by the ``FormatKind`` value.
SAMPLES: dict[str, bytes] = {
    "png": png_bytes(),
    "gif": gif_bytes(),
    "jpeg": jpeg_bytes(),
    "pdf": pdf_bytes(),
    "pe": pe_bytes(),
    "zip": zip_bytes(),
}
