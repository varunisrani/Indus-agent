"""Image bytes for the media tests, built here so no binary fixture is needed.

The PNG writer is independent of the code under test and can apply each of the five row filters,
so the decoder's unfiltering is exercised; the GIF, JPEG and WebP builders produce only the
structure the header readers look at.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable

Pixel = tuple[int, ...]


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _filter_row(kind: int, row: bytes, prev: bytes, bpp: int) -> bytes:
    out = bytearray()
    for i, value in enumerate(row):
        left = row[i - bpp] if i >= bpp else 0
        up = prev[i]
        up_left = prev[i - bpp] if i >= bpp else 0
        predictor = {
            0: 0,
            1: left,
            2: up,
            3: (left + up) >> 1,
            4: _paeth(left, up, up_left),
        }[kind]
        out.append((value - predictor) & 0xFF)
    return bytes(out)


def png(
    width: int,
    height: int,
    color: Callable[[int, int], Pixel] = lambda x, y: (x % 256, y % 256, (x + y) % 256),
    *,
    channels: int = 3,
    filters: tuple[int, ...] = (0, 1, 2, 3, 4),
) -> bytes:
    color_type = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    bpp = channels
    prev = bytes(width * bpp)
    raw = bytearray()
    for y in range(height):
        row = bytes(v for x in range(width) for v in color(x, y)[:channels])
        kind = filters[y % len(filters)]
        raw.append(kind)
        raw += _filter_row(kind, row, prev, bpp)
        prev = row
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _chunk(b"IEND", b"")
    )


def gif(width: int, height: int, frames: int) -> bytes:
    out = bytearray(b"GIF89a")
    out += struct.pack("<HHBBB", width, height, 0x80, 0, 0)  # global table of 2 colours
    out += b"\x00\x00\x00\xff\xff\xff"
    for _ in range(frames):
        out += b"\x21\xf9\x04\x00\x0a\x00\x00\x00"  # graphic control extension
        out += b"\x2c" + struct.pack("<HHHHB", 0, 0, width, height, 0)
        out += b"\x02\x02\x44\x01\x00"  # LZW min size 2, one tiny data block, terminator
    out += b"\x3b"
    return bytes(out)


def jpeg_header(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def webp_animated(width: int, height: int, frames: int) -> bytes:
    vp8x = b"VP8X" + struct.pack("<I", 10) + b"\x02\x00\x00\x00"
    vp8x += (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    body = b"WEBP" + vp8x
    for _ in range(frames):
        body += b"ANMF" + struct.pack("<I", 2) + b"\x00\x00"
    return b"RIFF" + struct.pack("<I", len(body)) + body
