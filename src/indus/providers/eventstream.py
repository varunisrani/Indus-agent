"""The AWS event-stream binary framing (``application/vnd.amazon.eventstream``).

Each message is: a 12-byte prelude (total length, headers length, CRC32 of those 8 bytes, all
big-endian), the headers, the payload, and a CRC32 of everything before it. A header is a
one-byte name length, the name, a one-byte value type and the value (type 7 is a UTF-8 string
with a two-byte length). Facts from AWS's published "event stream encoding" description.

``EventStreamDecoder.feed`` accepts bytes in any chunking and returns whole messages in order; a
damaged frame raises ``ProviderStreamError`` (code ``bad_frame``) rather than guessing.
"""

from __future__ import annotations

import struct
import uuid
import zlib
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from indus.foundation.errors import ProviderStreamError

PRELUDE = 12
TRAILER = 4
MAX_MESSAGE = 16 * 1024 * 1024
MIN_MESSAGE = PRELUDE + TRAILER

type HeaderValue = bool | int | bytes | str | datetime | uuid.UUID


@dataclass(frozen=True)
class EventMessage:
    headers: Mapping[str, HeaderValue] = field(default_factory=dict[str, HeaderValue])
    payload: bytes = b""

    def header(self, name: str) -> str:
        value = self.headers.get(name)
        return value if isinstance(value, str) else ""


def _bad(message: str, code: str = "bad_frame") -> ProviderStreamError:
    text = f"damaged event-stream frame: {message}"
    return ProviderStreamError(text, body={"error": {"message": text, "code": code}})


def _crc(data: bytes | memoryview) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _headers(raw: bytes) -> dict[str, HeaderValue]:
    out: dict[str, HeaderValue] = {}
    pos = 0
    size = len(raw)
    while pos < size:
        name_len = raw[pos]
        pos += 1
        name = raw[pos : pos + name_len].decode("utf-8", errors="replace")
        pos += name_len
        if pos >= size:
            raise _bad("header without a value type")
        kind = raw[pos]
        pos += 1
        value: HeaderValue
        if kind == 0:
            value = True
        elif kind == 1:
            value = False
        elif kind in (2, 3, 4, 5):
            width = {2: 1, 3: 2, 4: 4, 5: 8}[kind]
            value = int.from_bytes(raw[pos : pos + width], "big", signed=True)
            pos += width
        elif kind in (6, 7):
            (length,) = struct.unpack_from(">H", raw, pos)
            pos += 2
            data = raw[pos : pos + length]
            pos += length
            value = data.decode("utf-8", errors="replace") if kind == 7 else bytes(data)
        elif kind == 8:
            (millis,) = struct.unpack_from(">q", raw, pos)
            pos += 8
            value = datetime.fromtimestamp(millis / 1000, tz=UTC)
        elif kind == 9:
            value = uuid.UUID(bytes=bytes(raw[pos : pos + 16]))
            pos += 16
        else:
            raise _bad(f"unknown header value type {kind}")
        if pos > size:
            raise _bad("header runs past the header block")
        out[name] = value
    return out


class EventStreamDecoder:
    """Feed bytes; get complete messages back."""

    def __init__(self) -> None:
        self._buf = bytearray()

    @property
    def pending(self) -> int:
        """Bytes held for a message that has not fully arrived yet."""
        return len(self._buf)

    def feed(self, chunk: bytes) -> list[EventMessage]:
        self._buf += chunk
        out: list[EventMessage] = []
        while len(self._buf) >= PRELUDE:
            total, header_len, prelude_crc = struct.unpack_from(">III", self._buf, 0)
            if _crc(memoryview(self._buf)[:8]) != prelude_crc:
                raise _bad("prelude checksum mismatch")
            if total < MIN_MESSAGE or total > MAX_MESSAGE:
                raise _bad(f"message length {total} out of range")
            if header_len > total - MIN_MESSAGE:
                raise _bad("headers longer than the message")
            if len(self._buf) < total:
                break
            frame = bytes(self._buf[:total])
            del self._buf[:total]
            (message_crc,) = struct.unpack_from(">I", frame, total - TRAILER)
            if _crc(frame[: total - TRAILER]) != message_crc:
                raise _bad("message checksum mismatch")
            headers = _headers(frame[PRELUDE : PRELUDE + header_len])
            payload = frame[PRELUDE + header_len : total - TRAILER]
            out.append(EventMessage(headers, payload))
        return out


def encode_message(headers: Mapping[str, str], payload: bytes) -> bytes:
    """One message with string headers (used by tests and fakes to build frames)."""
    block = bytearray()
    for name, value in headers.items():
        raw_name = name.encode("utf-8")
        raw_value = value.encode("utf-8")
        block += bytes([len(raw_name)]) + raw_name + b"\x07" + struct.pack(">H", len(raw_value))
        block += raw_value
    total = PRELUDE + len(block) + len(payload) + TRAILER
    prelude = struct.pack(">II", total, len(block))
    head = prelude + struct.pack(">I", _crc(prelude))
    body = head + bytes(block) + payload
    return body + struct.pack(">I", _crc(body))


async def aiter_messages(response: httpx.Response) -> AsyncGenerator[EventMessage]:
    """Decoded messages of an open response; leftover bytes at the end are a damaged stream."""
    decoder = EventStreamDecoder()
    async for chunk in response.aiter_bytes():
        for message in decoder.feed(chunk):
            yield message
    if decoder.pending:
        raise _bad(
            f"stream ended inside a frame ({decoder.pending} bytes left)", "incomplete_stream"
        )
