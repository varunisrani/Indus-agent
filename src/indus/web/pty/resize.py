"""The in-band resize frame of the chat terminal (P18-07, R09.7).

The browser cannot send a terminal size out of band over a plain byte socket, so it sends a frame
that is exactly ``ESC [ RESIZE:<cols>;<rows> ]``. The server consumes such a frame and applies the
size to the PTY; it is never written to the child. Anything else — the escape with bytes before or
after it, two escapes in one frame, more than nine digits — is ordinary input.

Sizes are clamped to 1..2000 columns and 1..1000 rows: a broken probe reports values like 131072
columns or 0 rows, and passing those on blanks the TUI or overflows the kernel's unsigned short.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "MAX_COLS",
    "MAX_ROWS",
    "MIN_SIZE",
    "TermSize",
    "clamp",
    "message_bytes",
    "parse_resize",
    "resize_frame",
]

MIN_SIZE: Final = 1
MAX_COLS: Final = 2000
MAX_ROWS: Final = 1000

_FRAME: Final = re.compile(rb"\x1b\[RESIZE:(\d{1,9});(\d{1,9})\]")


@dataclass(frozen=True, slots=True)
class TermSize:
    """A terminal size, always inside the clamps when made through ``TermSize.of``."""

    cols: int
    rows: int

    @classmethod
    def of(cls, cols: object, rows: object) -> TermSize:
        return cls(clamp(cols, MAX_COLS), clamp(rows, MAX_ROWS))


def clamp(value: object, maximum: int) -> int:
    """``value`` as an int inside ``[1, maximum]``; anything that is not a whole number is 1."""
    if isinstance(value, bool):
        return MIN_SIZE
    try:
        number = int(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError, OverflowError):
        return MIN_SIZE
    return max(MIN_SIZE, min(number, maximum))


def parse_resize(frame: bytes) -> TermSize | None:
    """The size a frame asks for, or None when the frame is input."""
    found = _FRAME.fullmatch(frame)
    if found is None:
        return None
    return TermSize.of(int(found.group(1)), int(found.group(2)))


def resize_frame(cols: int, rows: int) -> bytes:
    """The frame a client sends for ``cols`` × ``rows`` (tests and fakes)."""
    return f"\x1b[RESIZE:{cols};{rows}]".encode("ascii")


def message_bytes(message: Mapping[str, Any]) -> bytes:
    """The payload of an ASGI ``websocket.receive`` message as bytes (text is UTF-8)."""
    raw: object = message.get("bytes")
    if isinstance(raw, bytes | bytearray):
        return bytes(raw)
    text: object = message.get("text")
    if isinstance(text, str):
        return text.encode("utf-8", "replace")
    return b""
