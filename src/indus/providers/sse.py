"""Incremental ``text/event-stream`` decoder (framing per the WHATWG HTML "Server-sent events").

Lines are split on raw bytes: CR and LF are single bytes that never occur inside a UTF-8
multi-byte sequence, so a character split across network chunks is decoded only once its line is
complete. Comment lines are returned as ``KeepAlive`` so callers can see that the connection is
alive without counting it as content.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import httpx

from indus.foundation.errors import ProviderError
from indus.providers.types import KeepAlive

MAX_LINE_BYTES = 4 * 1024 * 1024
_BOM = b"\xef\xbb\xbf"
_LINE_END = re.compile(rb"[\r\n]")


class SSELineTooLong(ProviderError):
    """One event-stream line grew past ``MAX_LINE_BYTES``."""


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str
    id: str | None = None
    retry: int | None = None


type SSEItem = SSEEvent | KeepAlive


class SSEDecoder:
    """Feed bytes in any chunking; get complete events back in order."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._at_start = True
        self._skip_lf = False
        self._event = ""
        self._data: list[str] = []
        self._has_data = False
        self._last_id: str | None = None
        self._retry: int | None = None

    def feed(self, chunk: bytes) -> list[SSEItem]:
        out: list[SSEItem] = []
        if not chunk:
            return out
        self._buf += chunk
        if self._at_start:
            if len(self._buf) < len(_BOM) and _BOM.startswith(bytes(self._buf)):
                return out  # might still become a BOM
            if self._buf.startswith(_BOM):
                del self._buf[: len(_BOM)]
            self._at_start = False
        if self._skip_lf and self._buf[:1] == b"\n":
            del self._buf[:1]
        self._skip_lf = False
        start = 0
        buf = self._buf
        size = len(buf)
        while True:
            found = _LINE_END.search(buf, start)
            if found is None:
                break
            end = found.start()
            line = bytes(buf[start:end])
            self._check_length(len(line))
            if buf[end] == 0x0D:  # CR: a following LF belongs to the same line end
                if end + 1 < size:
                    start = end + 2 if buf[end + 1] == 0x0A else end + 1
                else:
                    start = end + 1
                    self._skip_lf = True
            else:
                start = end + 1
            self._line(line, out)
        del buf[:start]
        self._check_length(len(buf))
        return out

    def flush(self) -> list[SSEItem]:
        """End of stream: finish a trailing unterminated line and a pending event."""
        out: list[SSEItem] = []
        if self._at_start and self._buf and not _BOM.startswith(bytes(self._buf)):
            self._at_start = False
        if self._buf and not self._at_start:
            line = bytes(self._buf)
            self._buf.clear()
            self._line(line, out)
        self._buf.clear()
        self._dispatch(out)
        return out

    def _check_length(self, length: int) -> None:
        if length > MAX_LINE_BYTES:
            self._buf.clear()
            raise SSELineTooLong(f"an event-stream line is longer than {MAX_LINE_BYTES} bytes")

    def _line(self, raw: bytes, out: list[SSEItem]) -> None:
        if not raw:
            self._dispatch(out)
            return
        line = raw.decode("utf-8", errors="replace")
        if line.startswith(":"):
            out.append(KeepAlive(line[1:].removeprefix(" ")))
            return
        name, colon, value = line.partition(":")
        if colon and value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
            self._has_data = True
        elif name == "id":
            if "\x00" not in value:
                self._last_id = value
        elif name == "retry" and value and value.isascii() and value.isdigit():
            self._retry = int(value)
        # any other field name is ignored

    def _dispatch(self, out: list[SSEItem]) -> None:
        if self._has_data:
            out.append(
                SSEEvent(
                    event=self._event or "message",
                    data="\n".join(self._data),
                    id=self._last_id,
                    retry=self._retry,
                )
            )
        self._event = ""
        self._data = []
        self._has_data = False
        self._retry = None


async def aiter_sse(response: httpx.Response) -> AsyncGenerator[SSEItem]:
    """Decode an open streaming ``httpx.Response`` body as server-sent events."""
    decoder = SSEDecoder()
    async for chunk in response.aiter_bytes():
        for item in decoder.feed(chunk):
            yield item
    for item in decoder.flush():
        yield item
