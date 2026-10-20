"""P18-07: the in-band resize frame and its clamps (R09.7)."""

from __future__ import annotations

import pytest

from indus.web.pty.resize import (
    MAX_COLS,
    MAX_ROWS,
    TermSize,
    clamp,
    message_bytes,
    parse_resize,
    resize_frame,
)


def test_an_exact_frame_is_a_resize() -> None:
    assert parse_resize(b"\x1b[RESIZE:120;40]") == TermSize(120, 40)
    assert parse_resize(resize_frame(80, 24)) == TermSize(80, 24)


@pytest.mark.parametrize(
    "frame",
    [
        b"x\x1b[RESIZE:80;24]",
        b"\x1b[RESIZE:80;24]x",
        b"\x1b[RESIZE:80;24]\x1b[RESIZE:80;24]",
        b"\x1b[RESIZE:80;24",
        b"\x1b[RESIZE:-1;24]",
        b"\x1b[RESIZE:80,24]",
        b"\x1b[resize:80;24]",
        b"\x1b[RESIZE:1234567890;24]",
        b"\x1b[RESIZE:;24]",
        b"",
        b"hello",
    ],
)
def test_only_an_exact_frame_is_a_resize(frame: bytes) -> None:
    assert parse_resize(frame) is None


@pytest.mark.parametrize(
    ("cols", "rows", "want"),
    [
        (5000, 5000, TermSize(MAX_COLS, MAX_ROWS)),
        (0, 0, TermSize(1, 1)),
        (131072, 1, TermSize(MAX_COLS, 1)),
        (2000, 1000, TermSize(2000, 1000)),
        (2001, 1001, TermSize(2000, 1000)),
        (1, 1, TermSize(1, 1)),
    ],
)
def test_sizes_are_clamped(cols: int, rows: int, want: TermSize) -> None:
    assert parse_resize(resize_frame(cols, rows)) == want
    assert TermSize.of(cols, rows) == want


def test_clamp_turns_garbage_into_one() -> None:
    assert clamp("x", 10) == 1
    assert clamp(None, 10) == 1
    assert clamp(float("inf"), 10) == 1
    assert clamp(True, 10) == 1
    assert clamp(-5, 10) == 1
    assert clamp("7", 10) == 7


def test_text_and_bytes_messages_become_bytes() -> None:
    assert message_bytes({"type": "websocket.receive", "bytes": b"\x03"}) == b"\x03"
    assert message_bytes({"type": "websocket.receive", "text": "é"}) == "é".encode()
    assert message_bytes({"type": "websocket.receive", "text": "\x1b[RESIZE:9;9]"}) == (
        b"\x1b[RESIZE:9;9]"
    )
    assert message_bytes({"type": "websocket.receive"}) == b""
