"""Cutting a reply into platform-sized pieces (P15-14).

``split_message`` returns pieces that each fit ``limit`` under the platform's length function
(code points, or UTF-16 units for Telegram). Cuts prefer the last line break in the budget, then
the last space, then a hard cut; a piece that ends inside a fenced code block is closed and the
next piece reopens the fence with the same language; a cut is moved back so it never leaves an
inline code span half open, never separates a backslash from the character it escapes and never
lands inside a fence marker line. Multi-piece output carries an ``(i/n)`` marker after each piece,
outside any fence (on its own line after a closing fence, which must stand alone).

Each piece remembers the source offsets of the text it carries, so a send that stopped part way
can resume with ``Chunked.remainder`` instead of re-sending the whole reply (R06.4).

Limits below ``BARE_LIMIT`` leave no room for markers or fence repair: those cut by code point
only. A single code point wider than the limit (an emoji at a UTF-16 limit of 1) still goes out
alone; that is the only case a piece exceeds the limit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "BARE_LIMIT",
    "Chunk",
    "Chunked",
    "LenFn",
    "Marker",
    "close_fences",
    "code_points",
    "fence_state",
    "plain_marker",
    "prefix_within",
    "split_message",
    "utf16_units",
]

type LenFn = Callable[[str], int]
type Marker = Callable[[int, int], str]

BARE_LIMIT = 32
"""Below this many units a limit is cut bare: no markers, no fence repair."""

_FENCE = "```"
_CLOSER = "\n```"
_FIRST_GUESS_COUNT = 99


def code_points(text: str) -> int:
    """Length in code points (Python's ``len``)."""
    return len(text)


def utf16_units(text: str) -> int:
    """Length in UTF-16 code units: astral characters (most emoji) count two."""
    return len(text.encode("utf-16-le")) // 2


def plain_marker(index: int, total: int) -> str:
    return f"({index}/{total})"


def prefix_within(text: str, budget: int, len_fn: LenFn = code_points) -> int:
    """The largest code-point offset ``k`` with ``len_fn(text[:k]) <= budget``."""
    if budget <= 0 or not text:
        return 0
    if len_fn is code_points:
        return min(len(text), budget)
    if len_fn(text) <= budget:
        return len(text)
    low, high = 0, min(len(text), budget)  # every code point costs at least one unit
    while low < high:
        middle = (low + high + 1) // 2
        if len_fn(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return low


# -- fences and inline code ----------------------------------------------------------------------


def _fence_tag(line: str) -> str | None:
    """The info word of a fence marker line ("" when bare), or None when ``line`` is not one."""
    stripped = line.strip()
    if not stripped.startswith(_FENCE):
        return None
    words = stripped[len(_FENCE) :].lstrip("`").split()
    return words[0] if words else ""


def fence_state(text: str, open_lang: str | None = None) -> str | None:
    """The language of the fence still open after ``text`` ("" for a bare fence), else None.

    ``open_lang`` is the state before ``text`` (a piece that starts inside a reopened fence)."""
    lang = open_lang
    for line in text.split("\n"):
        tag = _fence_tag(line)
        if tag is not None:
            lang = None if lang is not None else tag
    return lang


def _open_inline_tick(text: str, open_lang: str | None) -> int:
    """Offset of a backtick run that opens an inline code span left unclosed at the end of
    ``text`` (outside fences, escaped backticks ignored), or -1."""
    lang = open_lang
    opened = -1
    opened_run = 0
    offset = 0
    for line in text.split("\n"):
        tag = _fence_tag(line)
        if tag is not None:
            lang = None if lang is not None else tag
            opened = -1
        elif lang is None:
            i = 0
            while i < len(line):
                char = line[i]
                if char == "\\":
                    i += 2
                    continue
                if char == "`":
                    run = len(line) - i - len(line[i:].lstrip("`"))
                    if opened < 0:
                        opened, opened_run = offset + i, run
                    elif run == opened_run:
                        opened = -1
                    i += run
                    continue
                i += 1
        offset += len(line) + 1
    return opened


def close_fences(text: str) -> str:
    """``text`` plus whatever closes a fence or inline code span it leaves open (interim frames
    of a message still being written)."""
    if fence_state(text) is not None:
        text = text.rstrip("\n") + _CLOSER
    if _open_inline_tick(text, None) >= 0:
        text += "`"
    return text


# -- pieces --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    """One piece as sent, and the ``source[start:end]`` slice of the input it carries."""

    text: str
    start: int
    end: int


@dataclass(frozen=True)
class Chunked:
    source: str
    chunks: tuple[Chunk, ...]

    @property
    def texts(self) -> list[str]:
        return [chunk.text for chunk in self.chunks]

    def remainder(self, delivered: int) -> str:
        """The source text not yet delivered when the first ``delivered`` pieces landed."""
        if delivered <= 0:
            return self.source
        if delivered >= len(self.chunks):
            return ""
        return self.source[self.chunks[delivered].start :]


def _reopen(lang: str | None) -> str:
    return "" if lang is None else f"{_FENCE}{lang}\n"


def _trailing_backslashes(text: str) -> int:
    return len(text) - len(text.rstrip("\\"))


def _line_bounds(text: str, at: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, at) + 1
    end = text.find("\n", at)
    return start, len(text) if end < 0 else end


def _choose_split(rest: str, cut: int, open_lang: str | None) -> int:
    region = rest[:cut]
    newline = region.rfind("\n")
    if newline >= max(1, cut // 2):
        split = newline
    else:
        space = region.rfind(" ")
        split = space if space >= 1 else cut
    if split != newline:
        line_start, line_end = _line_bounds(rest, split)
        if (
            line_start > 1
            and split < line_end
            and _fence_tag(rest[line_start:line_end]) is not None
        ):
            split = line_start - 1  # never inside a fence marker line
    if split == cut and split > 1 and _trailing_backslashes(rest[:split]) % 2 == 1:
        split -= 1  # keep an escape and the character it escapes together
    tick = _open_inline_tick(rest[:split], open_lang)
    if tick > 0:
        safe = max(rest.rfind(" ", 0, tick), rest.rfind("\n", 0, tick))
        if safe > cut // 4:
            split = safe
        elif tick > cut // 4:
            split = tick  # no break before the span: cut right in front of its backtick
    return max(1, split)


def _absorb_closing_line(rest: str, split: int, room: int, len_fn: LenFn) -> int:
    """When the line right after the cut closes the open fence and fits, keep it in this piece
    (instead of a synthetic closer followed by an empty reopened block)."""
    if rest[split : split + 1] != "\n":
        return split
    line_end = rest.find("\n", split + 1)
    line_end = len(rest) if line_end < 0 else line_end
    if _fence_tag(rest[split + 1 : line_end]) != "":
        return split
    if len_fn(rest[:line_end]) <= room:
        return line_end
    return split


def _pieces(text: str, limit: int, len_fn: LenFn, reserve: int) -> list[Chunk]:
    out: list[Chunk] = []
    pos = 0
    carry: str | None = None
    closer_len = len_fn(_CLOSER)
    while pos < len(text):
        rest = text[pos:]
        prefix = _reopen(carry)
        room = limit - reserve - len_fn(prefix)
        if carry and room - closer_len < limit // 4:
            prefix = _reopen("")  # a huge info word would starve the piece
            room = limit - reserve - len_fn(prefix)
        end_state = fence_state(rest, carry)
        if len_fn(rest) + (closer_len if end_state is not None else 0) <= room:
            closer = _CLOSER if end_state is not None else ""
            out.append(Chunk(prefix + rest + closer, pos, len(text)))
            break
        budget = max(1, room - closer_len)
        cut = max(1, prefix_within(rest, budget, len_fn))
        split = _choose_split(rest, cut, carry)
        if fence_state(rest[:split], carry) is not None:
            split = _absorb_closing_line(rest, split, room, len_fn)
        body = rest[:split]
        state = fence_state(body, carry)
        out.append(Chunk(prefix + body + (_CLOSER if state is not None else ""), pos, pos + split))
        pos += split
        if state is None:
            while pos < len(text) and text[pos].isspace():
                pos += 1
        elif text[pos : pos + 1] == "\n":
            pos += 1  # the cut's own line break; code indentation after it is kept
        carry = state
    return out


def _bare(text: str, limit: int, len_fn: LenFn) -> Chunked:
    chunks: list[Chunk] = []
    pos = 0
    while pos < len(text):
        cut = max(1, prefix_within(text[pos:], limit, len_fn))
        chunks.append(Chunk(text[pos : pos + cut], pos, pos + cut))
        pos += cut
    return Chunked(text, tuple(chunks))


def split_message(
    text: str,
    limit: int,
    *,
    len_fn: LenFn = code_points,
    marker: Marker = plain_marker,
) -> Chunked:
    """Cut ``text`` into pieces of at most ``limit`` units (see the module docstring)."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if len_fn(text) <= limit:
        return Chunked(text, (Chunk(text, 0, len(text)),))
    if limit < BARE_LIMIT:
        return _bare(text, limit, len_fn)
    reserve = 1 + len_fn(marker(_FIRST_GUESS_COUNT, _FIRST_GUESS_COUNT))
    while True:
        pieces = _pieces(text, limit, len_fn, reserve)
        total = len(pieces)
        needed = 1 + len_fn(marker(total, total))
        if needed <= reserve:
            break
        reserve = needed
    if total == 1:
        return Chunked(text, tuple(pieces))
    marked = tuple(
        Chunk(
            piece.text + ("\n" if piece.text.endswith(_CLOSER) else " ") + marker(i, total),
            piece.start,
            piece.end,
        )
        for i, piece in enumerate(pieces, start=1)
    )
    return Chunked(text, marked)
