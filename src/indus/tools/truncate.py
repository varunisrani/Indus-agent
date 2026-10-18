"""Shared head/tail truncation for tools that cut their own output.

The kept text is exactly ``limit`` characters: the first ``head`` share (errors tend to show up
early) and the rest from the end (the latest lines matter most), joined by one marker that names
how much was left out and the total. Every tool uses the same marker shape.
"""

from __future__ import annotations

HEAD_SHARE = 0.4


def cut_marker(omitted: int, total: int, *, label: str = "output", unit: str = "chars") -> str:
    return f"\n\n[... {label} shortened: {omitted:,} of {total:,} {unit} left out ...]\n\n"


def split(limit: int, head: float = HEAD_SHARE) -> tuple[int, int]:
    """``(head chars, tail chars)`` for a ``limit``-character budget."""
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if not 0.0 <= head <= 1.0:
        raise ValueError("head must be between 0 and 1")
    first = int(limit * head)
    return first, limit - first


def head_tail(text: str, limit: int, head: float = HEAD_SHARE, *, label: str = "output") -> str:
    """``text`` as it is when it fits ``limit``; otherwise head + marker + tail."""
    if len(text) <= limit:
        return text
    first, last = split(limit, head)
    omitted = len(text) - first - last
    tail = text[len(text) - last :] if last else ""
    return text[:first] + cut_marker(omitted, len(text), label=label) + tail
