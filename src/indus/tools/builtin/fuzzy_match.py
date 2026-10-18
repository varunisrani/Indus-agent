# pyright: strict
"""Finding the text a model meant to replace, even when its copy drifted (P05-19).

``find_and_replace(content, old, new, replace_all)`` tries nine strategies in order and uses the
first one that finds anything:

1. ``exact`` — the text as given.
2. ``line_trimmed`` — every line compared without leading/trailing whitespace.
3. ``whitespace_normalized`` — runs of spaces and tabs count as one space.
4. ``indentation_flexible`` — leading indentation ignored.
5. ``escape_normalized`` — literal ``\\n``, ``\\t``, ``\\r`` in the search text read as the
   control characters.
6. ``trimmed_boundary`` — only the first and last line trimmed.
7. ``unicode_normalized`` — typographic characters (smart quotes, dashes, ellipsis, NBSP, minus,
   the other Zs spaces) read as their ASCII forms, then exact or line-trimmed.
8. ``block_anchor`` — first and last line equal (trimmed), the lines between similar.
9. ``context_similarity`` — every non-blank line similar (ratio ≥ 0.8).

The last two are *similarity* strategies: their span is located approximately, so they never apply
with ``replace_all`` and the result says so. More than one match without ``replace_all`` is an
error listing up to five locations. Before matching, escape drift from double-encoded tool
arguments is corrected only when the corrected search text exists verbatim in the file
(``\\'``/``\\"`` that should be plain quotes, backslash runs doubled); after a non-exact match,
drift the file does not have is refused instead of written. A non-exact line match re-indents the
replacement onto the file's indentation.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher

__all__ = [
    "SIMILARITY_STRATEGIES",
    "STRATEGIES",
    "Replacement",
    "already_applied",
    "closest_lines",
    "find_and_replace",
    "find_spans",
    "spans_for",
]

type Span = tuple[int, int]
type Strategy = Callable[[str, str], list[Span]]

MAX_LOCATIONS = 5
LINE_SIMILARITY = 0.80
BLOCK_SIMILARITY_ONE = 0.50
BLOCK_SIMILARITY_MANY = 0.70
ALREADY_APPLIED_MIN_CHARS = 8
# Upper bound on window-by-line similarity work before the last strategy gives up.
SIMILARITY_BUDGET = 4_000_000

IDENTICAL = (
    "old_string and new_string are the same, so nothing would change. Put the existing text in "
    "old_string and the changed text in new_string."
)
NO_MATCH = "Could not find old_string in the file"

TYPOGRAPHY: dict[str, str] = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "–": "-",
    "—": "--",
    "−": "-",
    "…": "...",
    " ": " ",
    " ": " ",
    " ": " ",
    "　": " ",
    **{chr(c): " " for c in range(0x2000, 0x200B)},
}


@dataclass(frozen=True)
class Replacement:
    """What ``find_and_replace`` decided. ``content`` is the new text (unchanged on failure)."""

    content: str
    count: int = 0
    strategy: str | None = None
    error: str | None = None
    notes: tuple[str, ...] = ()
    already_applied: bool = False


# -- text with a map back to the original ----------------------------------------------------------


class _Mapped:
    """A transformed text whose every character remembers the original span that produced it."""

    def __init__(self) -> None:
        self.chars: list[str] = []
        self.starts: list[int] = []
        self.ends: list[int] = []

    def add(self, text: str, start: int, end: int) -> None:
        for ch in text:
            self.chars.append(ch)
            self.starts.append(start)
            self.ends.append(end)

    @property
    def text(self) -> str:
        return "".join(self.chars)

    def back(self, spans: list[Span]) -> list[Span]:
        out: list[Span] = []
        for start, end in spans:
            if start >= end or end > len(self.starts):
                continue
            out.append((self.starts[start], self.ends[end - 1]))
        return out


def _collapse_blanks(text: str) -> _Mapped:
    mapped = _Mapped()
    i = 0
    while i < len(text):
        if text[i] in " \t":
            j = i
            while j < len(text) and text[j] in " \t":
                j += 1
            mapped.add(" ", i, j)
            i = j
        else:
            mapped.add(text[i], i, i + 1)
            i += 1
    return mapped


def _plain_typography(text: str) -> _Mapped:
    mapped = _Mapped()
    for i, ch in enumerate(text):
        mapped.add(TYPOGRAPHY.get(ch, ch), i, i + 1)
    return mapped


def _ascii(text: str) -> str:
    return "".join(TYPOGRAPHY.get(ch, ch) for ch in text)


# -- line windows ----------------------------------------------------------------------------------


def _line_starts(lines: list[str]) -> list[int]:
    starts: list[int] = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    return starts


def _pattern_lines(pattern: str) -> list[str]:
    lines = pattern.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()  # a final newline ends the last line; it is not one more line
    return lines


def _non_overlapping(spans: list[Span]) -> list[Span]:
    kept: list[Span] = []
    for span in sorted(spans):
        if not kept or span[0] >= kept[-1][1]:
            kept.append(span)
    return kept


def _windows(
    content: str, pattern: str, accept: Callable[[list[str], list[str]], bool]
) -> list[Span]:
    lines = content.split("\n")
    wanted = _pattern_lines(pattern)
    n = len(wanted)
    if n > len(lines):
        return []
    starts = _line_starts(lines)
    spans: list[Span] = []
    for i in range(len(lines) - n + 1):
        if accept(lines[i : i + n], wanted):
            spans.append((starts[i], starts[i + n - 1] + len(lines[i + n - 1])))
    return _non_overlapping(spans)


def _same_after(transform: Callable[[str], str]) -> Callable[[list[str], list[str]], bool]:
    def accept(block: list[str], wanted: list[str]) -> bool:
        return all(transform(a) == transform(b) for a, b in zip(block, wanted, strict=True))

    return accept


# -- the strategies --------------------------------------------------------------------------------


def _exact(content: str, pattern: str) -> list[Span]:
    spans: list[Span] = []
    start = content.find(pattern)
    while start != -1:
        spans.append((start, start + len(pattern)))
        start = content.find(pattern, start + len(pattern))
    return spans


def _line_trimmed(content: str, pattern: str) -> list[Span]:
    return _windows(content, pattern, _same_after(str.strip))


def _whitespace_normalized(content: str, pattern: str) -> list[Span]:
    mapped = _collapse_blanks(content)
    wanted = _collapse_blanks(pattern).text
    return mapped.back(_exact(mapped.text, wanted))


def _indentation_flexible(content: str, pattern: str) -> list[Span]:
    return _windows(content, pattern, _same_after(str.lstrip))


def _escape_normalized(content: str, pattern: str) -> list[Span]:
    unescaped = pattern.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
    return [] if unescaped == pattern else _exact(content, unescaped)


def _trimmed_boundary(content: str, pattern: str) -> list[Span]:
    def accept(block: list[str], wanted: list[str]) -> bool:
        if block[0].strip() != wanted[0].strip() or block[-1].strip() != wanted[-1].strip():
            return False
        return block[1:-1] == wanted[1:-1]

    return _windows(content, pattern, accept)


def _unicode_normalized(content: str, pattern: str) -> list[Span]:
    mapped = _plain_typography(content)
    plain_pattern = _ascii(pattern)
    if mapped.text == content and plain_pattern == pattern:
        return []
    found = _exact(mapped.text, plain_pattern) or _line_trimmed(mapped.text, plain_pattern)
    return mapped.back(found)


def _similar(a: str, b: str) -> float:
    if a == b:
        return 1.0
    matcher = SequenceMatcher(None, a, b)
    if matcher.real_quick_ratio() < LINE_SIMILARITY or matcher.quick_ratio() < LINE_SIMILARITY:
        return 0.0  # an upper bound already below the bar
    return matcher.ratio()


def _block_anchor(content: str, pattern: str) -> list[Span]:
    wanted = [_ascii(line) for line in _pattern_lines(pattern)]
    if len(wanted) < 2:
        return []
    plain = _ascii(content).split("\n")
    lines = content.split("\n")
    if len(plain) != len(lines):
        return []
    n = len(wanted)
    first, last = wanted[0].strip(), wanted[-1].strip()
    anchors = [
        i
        for i in range(len(lines) - n + 1)
        if plain[i].strip() == first and plain[i + n - 1].strip() == last
    ]
    if not anchors:
        return []
    floor = BLOCK_SIMILARITY_ONE if len(anchors) == 1 else BLOCK_SIMILARITY_MANY
    middle = "\n".join(wanted[1:-1])
    starts = _line_starts(lines)
    spans: list[Span] = []
    for i in anchors:
        if n > 2:
            got = "\n".join(plain[i + 1 : i + n - 1])
            if SequenceMatcher(None, got, middle).ratio() < floor:
                continue
        spans.append((starts[i], starts[i + n - 1] + len(lines[i + n - 1])))
    return _non_overlapping(spans)


def _context_similarity(content: str, pattern: str) -> list[Span]:
    wanted = _pattern_lines(pattern)
    lines = content.split("\n")
    n = len(wanted)
    if n > len(lines) or n * len(lines) > SIMILARITY_BUDGET:
        return []
    stripped = [w.strip() for w in wanted]

    def accept(block: list[str], _wanted: list[str]) -> bool:
        if _similar(stripped[0], block[0].strip()) < LINE_SIMILARITY:
            return False
        if _similar(stripped[-1], block[-1].strip()) < LINE_SIMILARITY:
            return False
        return all(
            not want or _similar(want, have.strip()) >= LINE_SIMILARITY
            for want, have in zip(stripped, block, strict=True)
        )

    return _windows(content, pattern, accept)


STRATEGIES: tuple[tuple[str, Strategy], ...] = (
    ("exact", _exact),
    ("line_trimmed", _line_trimmed),
    ("whitespace_normalized", _whitespace_normalized),
    ("indentation_flexible", _indentation_flexible),
    ("escape_normalized", _escape_normalized),
    ("trimmed_boundary", _trimmed_boundary),
    ("unicode_normalized", _unicode_normalized),
    ("block_anchor", _block_anchor),
    ("context_similarity", _context_similarity),
)
SIMILARITY_STRATEGIES = frozenset({"block_anchor", "context_similarity"})
_LINE_STRATEGIES = frozenset(
    {
        "line_trimmed",
        "indentation_flexible",
        "trimmed_boundary",
        "block_anchor",
        "context_similarity",
        "unicode_normalized",
    }
)
_BY_NAME: dict[str, Strategy] = dict(STRATEGIES)


def spans_for(name: str, content: str, pattern: str) -> list[Span]:
    """The spans one strategy finds (for tests and diagnostics)."""
    return _BY_NAME[name](content, pattern)


def find_spans(content: str, pattern: str) -> tuple[str, list[Span]] | None:
    """The first strategy that finds ``pattern`` and its spans, or None."""
    for name, strategy in STRATEGIES:
        spans = strategy(content, pattern)
        if spans:
            return name, spans
    return None


# -- escape drift ----------------------------------------------------------------------------------

_BACKSLASHES = re.compile(r"\\+")


class _Ambiguous(ValueError):
    pass


def _quote_drift_fix(old: str, new: str, content: str) -> tuple[str, str] | None:
    kinds = [q for q in ("'", '"') if "\\" + q in old and "\\" + q in new]
    if not kinds or old in content:
        return None
    for q in ("'", '"'):
        if not any("\\" + q in s for s in (old, new)):
            continue
        # The wire escapes a quote kind everywhere and by exactly one backslash.
        for s in (old, new):
            for match in re.finditer(r"(\\*)" + re.escape(q), s):
                if len(match.group(1)) != 1:
                    return None
    fixed_old = old.replace("\\'", "'").replace('\\"', '"')
    if fixed_old not in content:
        return None
    unproven = [q for q in ("'", '"') if "\\" + q in new and "\\" + q not in old]
    if unproven:
        raise _Ambiguous(
            "Escape drift: without the stray backslashes before quotes old_string matches the "
            f"file, but new_string also escapes {' and '.join(unproven)} which old_string gives "
            "no evidence for. Send old_string and new_string again with the escaping you mean."
        )
    return fixed_old, new.replace("\\'", "'").replace('\\"', '"')


def _backslash_drift_fix(old: str, new: str, content: str) -> tuple[str, str] | None:
    if "\\" not in old or old in content:
        return None
    runs = _BACKSLASHES.findall(old)
    if any(len(run) % 2 for run in runs):
        return None
    halved = _BACKSLASHES.sub(lambda m: "\\" * (len(m.group(0)) // 2), old)
    if halved not in content:
        return None
    if "\\" in new:
        raise _Ambiguous(
            "Escape drift: old_string matches the file once its backslashes are halved, but "
            "new_string has backslashes too and they may be meant. Send both again with the "
            "backslashes exactly as they should be in the file."
        )
    return halved, new


def _correct_drift(old: str, new: str, content: str) -> tuple[str, str, str | None]:
    fixed = _quote_drift_fix(old, new, content)
    if fixed is not None:
        return *fixed, (
            "Escape drift corrected: removed stray backslashes before quotes in old_string and "
            "new_string (the arguments were escaped twice)."
        )
    fixed = _backslash_drift_fix(old, new, content)
    if fixed is not None:
        return *fixed, (
            "Escape drift corrected: halved doubled backslashes in old_string; new_string had "
            "none and was used as given."
        )
    return old, new, None


def _regions(content: str, spans: list[Span]) -> str:
    return "".join(content[a:b] for a, b in spans)


def _drift_refusal(content: str, spans: list[Span], old: str, new: str) -> str | None:
    regions = _regions(content, spans)
    for quote in ("\\'", '\\"'):
        if quote in old and quote in new and quote not in regions:
            return (
                f"Escape drift: old_string and new_string contain {quote} but the matched text "
                f"in the file has a plain {quote[1]}. The arguments were escaped twice; send them "
                "again without the backslash."
            )
    if "\\n" in new:
        surplus = old.count("\\n")
        for a, b in spans:
            region = content[a:b]
            if "\n" in region and surplus > region.count("\\n"):
                return (
                    "Escape drift: old_string has literal \\n where the file has real line "
                    "breaks, so new_string would write a backslash and an n. Send the strings "
                    "again with real line breaks."
                )
    old_runs = [len(r) for r in _BACKSLASHES.findall(old)]
    file_runs = [len(r) for r in _BACKSLASHES.findall(regions)]
    if (
        old_runs
        and len(old_runs) == len(file_runs)
        and old_runs != file_runs
        and all(o == 2 * f for o, f in zip(old_runs, file_runs, strict=True))
        and (max(file_runs) >= 2 or len(file_runs) >= 2)
        and [len(r) for r in _BACKSLASHES.findall(new)] != file_runs
    ):
        return (
            "Escape drift: every backslash run in old_string is twice as long as in the file, so "
            "new_string would double the file's backslashes. Send the strings again with the "
            "backslashes as they are in the file."
        )
    return None


def _control_chars_in_new(new: str, regions: str) -> str:
    for literal, real in (("\\t", "\t"), ("\\r", "\r")):
        if literal in new and real in regions:
            new = new.replace(literal, real)
    return new


# -- re-indenting ----------------------------------------------------------------------------------


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _first_text_line(text: str) -> str | None:
    return next((line for line in text.split("\n") if line.strip()), None)


def _starts_mid_line(content: str, start: int) -> bool:
    line_start = content.rfind("\n", 0, start) + 1
    return bool(content[line_start:start].strip(" \t"))


def _reindent(region: str, old: str, new: str) -> str:
    old_first, file_first = _first_text_line(old), _first_text_line(region)
    if old_first is None or file_first is None:
        return new
    given, actual = _indent(old_first), _indent(file_first)
    if given == actual:
        return new
    out: list[str] = []
    for line in new.split("\n"):
        if not line.strip():
            out.append(line)
        elif line.startswith(given):
            out.append(actual + line[len(given) :])
        else:
            out.append(actual + line.lstrip(" \t"))
    return "\n".join(out)


# -- reporting -------------------------------------------------------------------------------------


def _locations(content: str, spans: list[Span]) -> str:
    rows: list[str] = []
    for start, _ in spans[:MAX_LOCATIONS]:
        number = content.count("\n", 0, start) + 1
        line_start = content.rfind("\n", 0, start) + 1
        line_end = content.find("\n", start)
        text = content[line_start : line_end if line_end != -1 else len(content)].strip()
        rows.append(f"  L{number}: {text if len(text) <= 80 else text[:77] + '...'}")
    if len(spans) > MAX_LOCATIONS:
        rows.append(f"  ... and {len(spans) - MAX_LOCATIONS} more")
    return "\n".join(rows)


def _visible(line: str) -> str:
    return _indent(line).replace("\t", "→").replace(" ", "·") + line.lstrip(" \t")


def already_applied(content: str, old: str, new: str) -> bool:
    """True when the edit is already in the file: ``new`` there verbatim, ``old`` gone."""
    if len(new.strip()) < ALREADY_APPLIED_MIN_CHARS or new not in content:
        return False
    return old not in content


def closest_lines(old: str, content: str, *, context: int = 2, limit: int = 3) -> str:
    """Numbered snippets of the file around the lines most like ``old``'s first line."""
    lines = content.splitlines()
    anchor = _first_text_line(old)
    if not lines or anchor is None:
        return ""
    target = anchor.strip()
    scored = sorted(
        ((SequenceMatcher(None, target, line.strip()).ratio(), i) for i, line in enumerate(lines)),
        key=lambda pair: (-pair[0], pair[1]),
    )
    picks = [i for ratio, i in scored[:limit] if ratio > 0.3]
    blocks: list[str] = []
    span = len(old.splitlines()) or 1
    for i in picks:
        first, stop = max(0, i - context), min(len(lines), i + span + context)
        blocks.append("\n".join(f"{n + 1}|{lines[n]}" for n in range(first, stop)))
    out = "\n---\n".join(blocks)
    if picks:
        best = lines[picks[0]]
        if best.strip() == target and best != anchor:
            out += (
                "\n\nOnly the indentation differs (→ tab, · space):\n"
                f"  file has: {_visible(best)}\n  you sent: {_visible(anchor)}"
            )
    return out


# -- the entry point -------------------------------------------------------------------------------


def find_and_replace(content: str, old: str, new: str, replace_all: bool = False) -> Replacement:
    if not old:
        return Replacement(
            content,
            error=(
                "old_string is empty. Put the exact text to replace in old_string (read the file "
                "first if unsure); to create or rewrite a whole file use write_file."
            ),
        )
    if not old.strip():
        return Replacement(
            content, error="old_string is only whitespace; give non-blank text that is in the file."
        )
    try:
        old, new, note = _correct_drift(old, new, content)
    except _Ambiguous as exc:
        return Replacement(content, error=str(exc))
    if old == new:
        return Replacement(content, error=IDENTICAL)
    found = find_spans(content, old)
    if found is None:
        if already_applied(content, old, new):
            return Replacement(content, already_applied=True)
        hint = closest_lines(old, content)
        text = NO_MATCH + (f".\n\nThe closest lines in the file:\n{hint}" if hint else ".")
        return Replacement(content, error=text)
    name, spans = found
    if len(spans) > 1 and not replace_all:
        return Replacement(
            content,
            error=(
                f"old_string matches {len(spans)} places. Add surrounding lines to make it "
                f"unique, or set replace_all to true.\nMatches:\n{_locations(content, spans)}"
            ),
        )
    if replace_all and len(spans) > 1 and name in SIMILARITY_STRATEGIES:
        return Replacement(
            content,
            error=(
                f"old_string only resembles {len(spans)} places (strategy {name}); replace_all "
                "needs the exact text, whitespace included."
            ),
        )
    if name != "exact":
        refused = _drift_refusal(content, spans, old, new)
        if refused:
            return Replacement(content, error=refused)
    new = _control_chars_in_new(new, _regions(content, spans))
    result = content
    for start, end in sorted(spans, reverse=True):
        piece = new
        if old.endswith("\n") and piece.endswith("\n") and not content[start:end].endswith("\n"):
            piece = piece[:-1]  # a line span stops before the newline old_string ended with
        if name in _LINE_STRATEGIES and not _starts_mid_line(content, start):
            piece = _reindent(content[start:end], old, piece)
        result = result[:start] + piece + result[end:]
    if result == content:
        return Replacement(content, already_applied=True)  # the span already reads as new_string
    notes = [note] if note else []
    if name in SIMILARITY_STRATEGIES:
        notes.append(
            f"Approximate match (strategy {name}): the edited span was located by similarity. "
            "Check the diff to be sure the change landed exactly where you meant."
        )
    return Replacement(result, len(spans), name, notes=tuple(notes))
