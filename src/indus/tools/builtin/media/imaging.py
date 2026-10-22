"""Image facts and the few pixel operations the media tools need.

``inspect(data)`` reads only headers: the type from the first bytes (never the file name), the
canvas size and the frame count of animated GIF, PNG (APNG) and WebP files. ``check_limits`` turns
those facts into a refusal before anything decodes the pixels.

Pixel work (``crop``, ``fit``) uses Pillow when it is installed and our own small PNG codec
otherwise. The fallback reads 8- and 16-bit, non-interlaced PNG in every colour type and writes
8-bit PNG, which covers screenshots and generated images; anything else without Pillow raises
``NeedsPillow`` so the caller can say what to install.
"""

from __future__ import annotations

import base64
import importlib
import io
import struct
import zlib
from dataclasses import dataclass
from types import ModuleType
from typing import Any

__all__ = [
    "MAX_FRAMES",
    "MAX_TOTAL_PIXELS",
    "BadImage",
    "Fitted",
    "ImageInfo",
    "NeedsPillow",
    "Raster",
    "b64_len",
    "check_limits",
    "crop",
    "data_url",
    "decode_png",
    "encode_png",
    "fit",
    "inspect",
    "pillow",
]

MAX_FRAMES = 100
MAX_TOTAL_PIXELS = 100_000_000
MIN_SIDE = 64
PILLOW_EXTRA = "vision"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class BadImage(ValueError):
    """The bytes are not a usable image (the message says why, in plain words)."""


class NeedsPillow(BadImage):
    """This operation on this image type needs Pillow."""

    def __init__(self, what: str) -> None:
        super().__init__(
            f"{what} needs the Pillow library, which is not installed "
            f"(install the `{PILLOW_EXTRA}` extra: pip install 'indus-agent[{PILLOW_EXTRA}]')."
        )


@dataclass(frozen=True)
class ImageInfo:
    mime: str
    width: int
    height: int
    frames: int = 1

    @property
    def longest(self) -> int:
        return max(self.width, self.height)


def pillow() -> ModuleType | None:
    """``PIL.Image`` when Pillow is installed, else None (it is an optional extra)."""
    try:
        return importlib.import_module("PIL.Image")
    except ImportError:
        return None


# -- sniffing -----------------------------------------------------------------------------------


def _png_info(data: bytes) -> ImageInfo:
    if len(data) < 33 or data[12:16] != b"IHDR":
        raise BadImage("the PNG header is damaged")
    width, height = struct.unpack(">II", data[16:24])
    frames = 1
    for kind, body in _png_chunks(data, verify=False):
        if kind == b"acTL" and len(body) >= 4:
            frames = max(1, struct.unpack(">I", body[:4])[0])
        if kind == b"IDAT":
            break
    return ImageInfo("image/png", width, height, frames)


_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _jpeg_info(data: bytes) -> ImageInfo:
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        (length,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        if marker in _SOF_MARKERS and pos + 9 <= len(data):
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            return ImageInfo("image/jpeg", width, height)
        if marker == 0xDA or length < 2:
            break
        pos += 2 + length
    raise BadImage("the JPEG has no frame header (damaged or truncated)")


def _skip_sub_blocks(data: bytes, pos: int) -> int:
    while pos < len(data):
        size = data[pos]
        pos += 1
        if size == 0:
            return pos
        pos += size
    raise BadImage("the GIF is truncated")


def _gif_info(data: bytes) -> ImageInfo:
    if len(data) < 13:
        raise BadImage("the GIF header is damaged")
    width, height = struct.unpack("<HH", data[6:10])
    flags = data[10]
    pos = 13
    if flags & 0x80:
        pos += 3 * (2 ** ((flags & 0x07) + 1))
    frames = 0
    while pos < len(data):
        block = data[pos]
        if block == 0x3B:  # trailer
            break
        if block == 0x21:  # extension: label, then sub-blocks
            pos = _skip_sub_blocks(data, pos + 2)
        elif block == 0x2C:  # image descriptor
            frames += 1
            if frames > MAX_FRAMES:
                break  # enough to refuse; no need to walk the rest
            local = data[pos + 9] if pos + 9 < len(data) else 0
            pos += 10
            if local & 0x80:
                pos += 3 * (2 ** ((local & 0x07) + 1))
            pos = _skip_sub_blocks(data, pos + 1)  # LZW minimum code size, then the data
        else:
            raise BadImage("the GIF has an unknown block (damaged)")
    if frames == 0:
        raise BadImage("the GIF holds no image")
    return ImageInfo("image/gif", width, height, frames)


def _webp_info(data: bytes) -> ImageInfo:
    pos = 12
    width = height = 0
    frames = 0
    while pos + 8 <= len(data):
        kind = data[pos : pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        body = data[pos + 8 : pos + 8 + size]
        if kind == b"VP8X" and len(body) >= 10:
            width = 1 + int.from_bytes(body[4:7], "little")
            height = 1 + int.from_bytes(body[7:10], "little")
        elif kind == b"VP8 " and len(body) >= 10 and not width:
            width = struct.unpack("<H", body[6:8])[0] & 0x3FFF
            height = struct.unpack("<H", body[8:10])[0] & 0x3FFF
        elif kind == b"VP8L" and len(body) >= 5 and not width:
            bits = int.from_bytes(body[1:5], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
        elif kind == b"ANMF":
            frames += 1
        pos += 8 + size + (size & 1)
    if not width or not height:
        raise BadImage("the WebP has no readable size (damaged)")
    return ImageInfo("image/webp", width, height, max(frames, 1))


def _bmp_info(data: bytes) -> ImageInfo:
    if len(data) < 26:
        raise BadImage("the BMP header is damaged")
    width, height = struct.unpack("<ii", data[18:26])
    return ImageInfo("image/bmp", abs(width), abs(height))


def sniff(data: bytes) -> str | None:
    """The image type the first bytes announce, or None."""
    if data.startswith(PNG_SIGNATURE):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM") and len(data) >= 26:
        return "image/bmp"
    return None


def inspect(data: bytes) -> ImageInfo:
    """Type, size and frame count from the headers; ``BadImage`` when it is not an image."""
    mime = sniff(data)
    if mime is None:
        hint = "text" if _looks_textual(data[:512]) else "unknown binary data"
        raise BadImage(
            f"the content is not a supported image (it looks like {hint}); "
            "PNG, JPEG, GIF and WebP are accepted"
        )
    try:
        info = {
            "image/png": _png_info,
            "image/jpeg": _jpeg_info,
            "image/gif": _gif_info,
            "image/webp": _webp_info,
            "image/bmp": _bmp_info,
        }[mime](data)
    except (struct.error, IndexError) as exc:
        raise BadImage(f"the {mime.split('/')[1].upper()} header is damaged") from exc
    if info.width <= 0 or info.height <= 0:
        raise BadImage("the image reports a zero size")
    return info


def _looks_textual(head: bytes) -> bool:
    if not head:
        return False
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(c.isprintable() or c in "\r\n\t" for c in text)


def check_limits(
    info: ImageInfo, *, max_frames: int = MAX_FRAMES, max_pixels: int = MAX_TOTAL_PIXELS
) -> None:
    """Refuse oversized animations (frames, or frames times canvas) before decoding."""
    if info.frames > max_frames:
        raise BadImage(
            f"the animation has more than {max_frames} frames; send a still image or a shorter clip"
        )
    total = info.frames * info.width * info.height
    if total > max_pixels:
        raise BadImage(
            f"the image is too big to decode safely ({info.width}x{info.height} px x "
            f"{info.frames} frame(s) = {total:,} pixels; the limit is {max_pixels:,})"
        )


# -- our PNG codec ------------------------------------------------------------------------------

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_MODES = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}


@dataclass(frozen=True)
class Raster:
    """Packed 8-bit pixels, row after row; ``mode`` is L, LA, RGB or RGBA."""

    width: int
    height: int
    mode: str
    pixels: bytes

    @property
    def channels(self) -> int:
        return len(self.mode)

    def pixel(self, x: int, y: int) -> tuple[int, ...]:
        start = (y * self.width + x) * self.channels
        return tuple(self.pixels[start : start + self.channels])


def _png_chunks(data: bytes, *, verify: bool = True):
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if len(body) < length:
            raise BadImage("the PNG is truncated")
        if verify:
            crc = data[pos + 8 + length : pos + 12 + length]
            if len(crc) < 4 or struct.unpack(">I", crc)[0] != zlib.crc32(kind + body):
                raise BadImage("the PNG is damaged (a chunk checksum does not match)")
        yield kind, body
        if kind == b"IEND":
            return
        pos += 12 + length


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, height: int, stride: int, bpp: int) -> bytearray:
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    for row in range(height):
        kind = raw[pos]
        line = bytearray(raw[pos + 1 : pos + 1 + stride])
        pos += 1 + stride
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up_left = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], up_left)) & 0xFF
        elif kind != 0:
            raise BadImage("the PNG uses an unknown row filter (damaged)")
        out[row * stride : (row + 1) * stride] = line
        prev = line
    return out


def png_stream_complete(data: bytes) -> None:
    """Raise ``BadImage`` unless the PNG's pixel stream inflates to its full declared size."""
    width, height, depth, color, interlace = _png_header(data)
    raw = _png_inflate(data)
    if interlace:
        return  # the size check below does not apply to Adam7; Pillow validates those
    stride = (width * _CHANNELS[color] * depth + 7) // 8
    if len(raw) < height * (stride + 1):
        raise BadImage("the PNG pixel data is truncated")


def _png_header(data: bytes) -> tuple[int, int, int, int, int]:
    if not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise BadImage("not a PNG")
    width, height, depth, color, _comp, _filt, interlace = struct.unpack(">IIBBBBB", data[16:29])
    if color not in _CHANNELS:
        raise BadImage("the PNG colour type is invalid")
    return width, height, depth, color, interlace


_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2),
          (0, 1, 1, 2))  # fmt: skip


def _png_raw_size(width: int, height: int, depth: int, color: int, interlace: int) -> int:
    """The exact size of the inflated pixel stream the header declares (filter bytes included)."""
    bits = _CHANNELS[color] * depth
    if not interlace:
        return height * ((width * bits + 7) // 8 + 1)
    total = 0
    for x0, y0, dx, dy in _ADAM7:
        cols = (width - x0 + dx - 1) // dx if width > x0 else 0
        rows = (height - y0 + dy - 1) // dy if height > y0 else 0
        if cols and rows:
            total += rows * ((cols * bits + 7) // 8 + 1)
    return total


def _png_inflate(data: bytes) -> bytes:
    """The pixel stream, inflated no further than the size the header declares: a small file
    whose stream inflates to gigabytes (a decompression bomb) is cut there, never held whole."""
    limit = _png_raw_size(*_png_header(data))
    idat = b"".join(body for kind, body in _png_chunks(data) if kind == b"IDAT")
    try:
        return zlib.decompressobj().decompress(idat, max(1, limit))
    except zlib.error as exc:
        raise BadImage("the PNG pixel data is damaged or truncated") from exc


def decode_png(data: bytes) -> Raster:
    """Decode an 8/16-bit non-interlaced PNG to 8-bit pixels (palette becomes RGB or RGBA)."""
    width, height, depth, color, interlace = _png_header(data)
    if interlace or depth not in (8, 16):
        raise NeedsPillow("reading an interlaced or low-bit-depth PNG")
    palette = b""
    alpha = b""
    for kind, body in _png_chunks(data):
        if kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            alpha = body
    channels = _CHANNELS[color]
    sample = depth // 8
    stride = width * channels * sample
    raw = _png_inflate(data)
    if len(raw) < height * (stride + 1):
        raise BadImage("the PNG pixel data is truncated")
    flat = _unfilter(raw, height, stride, channels * sample)
    if sample == 2:
        flat = flat[0::2]  # keep the high byte of each 16-bit sample
    if color == 3:
        if not palette:
            raise BadImage("the PNG has no palette")
        with_alpha = bool(alpha)
        out = bytearray()
        for index in flat:
            out += palette[index * 3 : index * 3 + 3]
            if with_alpha:
                out.append(alpha[index] if index < len(alpha) else 255)
        return Raster(width, height, "RGBA" if with_alpha else "RGB", bytes(out))
    return Raster(width, height, _MODES[channels], bytes(flat))


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


_COLOR_TYPES = {"L": 0, "LA": 4, "RGB": 2, "RGBA": 6}


def encode_png(raster: Raster) -> bytes:
    stride = raster.width * raster.channels
    rows = bytearray()
    for row in range(raster.height):
        rows.append(0)
        rows += raster.pixels[row * stride : (row + 1) * stride]
    header = struct.pack(
        ">IIBBBBB", raster.width, raster.height, 8, _COLOR_TYPES[raster.mode], 0, 0, 0
    )
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + _chunk(b"IEND", b"")
    )


def _crop_raster(raster: Raster, box: tuple[int, int, int, int]) -> Raster:
    x1, y1, x2, y2 = box
    ch = raster.channels
    stride = raster.width * ch
    out = bytearray()
    for row in range(y1, y2):
        start = row * stride + x1 * ch
        out += raster.pixels[start : start + (x2 - x1) * ch]
    return Raster(x2 - x1, y2 - y1, raster.mode, bytes(out))


def _shrink_raster(raster: Raster, width: int, height: int) -> Raster:
    """Area-average downscale (every source pixel counts once)."""
    ch = raster.channels
    src = raster.pixels
    stride = raster.width * ch
    xs = [x * raster.width // width for x in range(width + 1)]
    ys = [y * raster.height // height for y in range(height + 1)]
    out = bytearray()
    for ty in range(height):
        y0, y1 = ys[ty], max(ys[ty + 1], ys[ty] + 1)
        for tx in range(width):
            x0, x1 = xs[tx], max(xs[tx + 1], xs[tx] + 1)
            count = (y1 - y0) * (x1 - x0)
            sums = [0] * ch
            for sy in range(y0, y1):
                base = sy * stride
                for sx in range(x0, x1):
                    at = base + sx * ch
                    for c in range(ch):
                        sums[c] += src[at + c]
            out += bytes((s + count // 2) // count for s in sums)
    return Raster(width, height, raster.mode, bytes(out))


# -- operations ---------------------------------------------------------------------------------


def clamp_box(region: Any, info: ImageInfo) -> tuple[int, int, int, int]:
    """``[x1, y1, x2, y2]`` in original pixels, clamped to the image; ``BadImage`` when unusable."""
    if not (
        isinstance(region, list | tuple)
        and len(region) == 4
        and all(isinstance(v, int | float) and not isinstance(v, bool) for v in region)
    ):
        raise BadImage("region must be four numbers [x1, y1, x2, y2] in original image pixels")
    x1, y1, x2, y2 = (int(v) for v in region)
    cx1, cx2 = (max(0, min(v, info.width)) for v in (x1, x2))
    cy1, cy2 = (max(0, min(v, info.height)) for v in (y1, y2))
    if cx2 <= cx1 or cy2 <= cy1:
        raise BadImage(
            f"region [{x1}, {y1}, {x2}, {y2}] has no area inside the image, which is "
            f"{info.width}x{info.height} px; use x1 < x2 and y1 < y2 within those bounds"
        )
    return cx1, cy1, cx2, cy2


def crop(data: bytes, info: ImageInfo, box: tuple[int, int, int, int]) -> bytes:
    """The box (already clamped) at original resolution, as PNG."""
    image_mod = pillow()
    if image_mod is not None:
        with image_mod.open(io.BytesIO(data)) as img:
            part = img.crop(box)
            if part.mode not in ("RGB", "RGBA", "L", "LA"):
                part = part.convert("RGBA" if "A" in part.getbands() else "RGB")
            buf = io.BytesIO()
            part.save(buf, format="PNG")
            return buf.getvalue()
    if info.mime != "image/png":
        raise NeedsPillow(f"cutting a region out of a {info.mime.split('/')[1].upper()} image")
    return encode_png(_crop_raster(decode_png(data), box))


@dataclass(frozen=True)
class Fitted:
    data: bytes
    mime: str
    width: int
    height: int
    scaled_from: tuple[int, int] | None = None  # original size when it was shrunk


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def b64_len(size: int) -> int:
    """Length of the base64 text for ``size`` bytes."""
    return 4 * ((size + 2) // 3)


def _target_size(width: int, height: int, longest: int) -> tuple[int, int]:
    if max(width, height) <= longest:
        return width, height
    if width >= height:
        return longest, max(1, round(height * longest / width))
    return max(1, round(width * longest / height)), longest


def fit(data: bytes, info: ImageInfo, *, max_side: int, max_b64_bytes: int) -> Fitted:
    """The image with its longest side ≤ ``max_side`` and its base64 form ≤ ``max_b64_bytes``.

    An image already inside both limits comes back unchanged. Otherwise it is shrunk; PNG output
    keeps exact pixels, Pillow may also use JPEG quality steps to reach the byte target. Each pass
    that still misses the target halves the size again, never below ``MIN_SIDE``. ``BadImage``
    when the target cannot be reached.
    """
    if info.longest <= max_side and b64_len(len(data)) <= max_b64_bytes:
        return Fitted(data, info.mime, info.width, info.height)
    image_mod = pillow()
    if image_mod is not None:
        return _fit_pillow(image_mod, data, info, max_side, max_b64_bytes)
    if info.mime != "image/png":
        raise NeedsPillow(f"shrinking a {info.mime.split('/')[1].upper()} image")
    raster = decode_png(data)
    width, height = _target_size(info.width, info.height, max_side)
    while True:
        small = (
            _shrink_raster(raster, width, height)
            if (width, height)
            != (
                raster.width,
                raster.height,
            )
            else raster
        )
        encoded = encode_png(small)
        if b64_len(len(encoded)) <= max_b64_bytes:
            return Fitted(encoded, "image/png", width, height, (info.width, info.height))
        if max(width, height) <= MIN_SIDE:
            raise BadImage("the image cannot be made small enough to embed")
        width, height = _target_size(width, height, max(MIN_SIDE, max(width, height) // 2))


def _fit_pillow(
    image_mod: ModuleType, data: bytes, info: ImageInfo, max_side: int, max_b64_bytes: int
) -> Fitted:
    with image_mod.open(io.BytesIO(data)) as opened:
        img = opened.convert("RGBA") if opened.mode in ("P", "PA", "LA") else opened.copy()
    keep_png = info.mime == "image/png" and img.mode == "RGBA"
    width, height = _target_size(img.width, img.height, max_side)
    lanczos = getattr(image_mod, "LANCZOS", None) or image_mod.Resampling.LANCZOS
    while True:
        small = img.resize((width, height), lanczos) if (width, height) != img.size else img
        attempts: list[tuple[str, dict[str, Any]]] = [("PNG", {})]
        if not keep_png:
            attempts = [("JPEG", {"quality": q}) for q in (85, 70, 50)]
        for fmt, options in attempts:
            out = small if fmt == "PNG" or small.mode in ("RGB", "L") else small.convert("RGB")
            buf = io.BytesIO()
            out.save(buf, format=fmt, **options)
            encoded = buf.getvalue()
            if b64_len(len(encoded)) <= max_b64_bytes:
                scaled = (
                    (info.width, info.height)
                    if (width, height)
                    != (
                        info.width,
                        info.height,
                    )
                    else None
                )
                mime = "image/png" if fmt == "PNG" else "image/jpeg"
                return Fitted(encoded, mime, width, height, scaled)
        if max(width, height) <= MIN_SIDE:
            raise BadImage("the image cannot be made small enough to embed")
        width, height = _target_size(width, height, max(MIN_SIDE, max(width, height) // 2))


def validate_decodable(data: bytes, info: ImageInfo) -> None:
    """A deeper check than the headers: Pillow loads every frame; without it PNG streams are
    inflated to their declared size (a cut-off download is caught either way for PNG)."""
    image_mod = pillow()
    if image_mod is None:
        if info.mime == "image/png":
            png_stream_complete(data)
        return
    try:
        with image_mod.open(io.BytesIO(data)) as img:
            for index in range(info.frames):
                img.seek(index)
                img.load()
    except EOFError:
        return
    except Exception as exc:
        raise BadImage(f"the image could not be decoded ({type(exc).__name__})") from exc


def to_png(data: bytes) -> bytes:
    """Re-encode (first frame) as PNG with Pillow; used for types providers do not take (BMP)."""
    image_mod = pillow()
    if image_mod is None:
        raise NeedsPillow("converting a BMP image")
    with image_mod.open(io.BytesIO(data)) as img:
        buf = io.BytesIO()
        img.convert("RGBA" if "A" in img.getbands() else "RGB").save(buf, format="PNG")
        return buf.getvalue()
