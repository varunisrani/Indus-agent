"""Small text helpers shared by the REPL's renderers."""

from __future__ import annotations

import re

__all__ = ["clean", "one_line", "shorten"]

# CSI / OSC / other escape sequences a stored or streamed text might carry.
_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI ... final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC ... BEL or ST
    r"|\x1b[@-Z\\-_]"  # two-byte escapes
)
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: str) -> str:
    """``text`` without terminal escape sequences or control characters (newline and tab kept);
    a carriage return before a newline is dropped."""
    text = _ESCAPES.sub("", text.replace("\r\n", "\n"))
    return _CONTROLS.sub("", text)


def one_line(text: str) -> str:
    """``text`` cleaned, with every run of whitespace turned into one space."""
    return " ".join(clean(text).split())


def shorten(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, the last one an ellipsis when something was cut."""
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
