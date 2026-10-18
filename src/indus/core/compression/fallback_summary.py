"""Model-free summary parts (P08-04): the anchor index, the user messages, the fallback notes.

``anchor_index`` harvests exact identifiers from the condensed turns with fixed patterns (issue
and PR numbers, commit hashes, file paths, error lines, URLs), most frequent first, within 7,000
characters. ``user_messages`` keeps every real user message verbatim, newest first, within a
24,000-character budget and 4,000 characters per message. ``fallback_summary`` builds handoff
notes with the same headings as a model summary from those parts alone; the engine commits it
when the summary call keeps failing (R01.9). Every text is redacted before it is kept.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, cast

from indus.core.compression import texts
from indus.core.compression.boundaries import is_real_user
from indus.core.transcript import TranscriptRow, call_arguments, call_name
from indus.foundation.redact import redact_text

__all__ = [
    "ANCHOR_BUDGET",
    "USER_BUDGET",
    "USER_MESSAGE_MAX",
    "anchor_index",
    "fallback_summary",
    "file_mentions",
    "row_text",
    "user_messages",
]

ANCHOR_BUDGET = 7_000
USER_BUDGET = 24_000
USER_MESSAGE_MAX = 4_000
FALLBACK_MAX = 8_000
PREVIOUS_MAX = 3_000
TURN_MAX = 600
MAX_LISTED = 12

_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("issues and PRs", re.compile(r"(?<![\w&/])#\d{2,6}\b"), 100),
    ("commits", re.compile(r"\b[0-9a-f]{7,40}\b"), 40),
    ("files", re.compile(r"(?:~|\.{1,2})?/?(?:[\w.@-]+/)+[\w.@-]+\.[A-Za-z0-9]{1,8}\b"), 80),
    ("errors", re.compile(r"\b[A-Za-z_]*(?:Error|Exception)\b[^\n]{0,100}"), 40),
    ("urls", re.compile(r"https?://[^\s)\"'<>]{6,160}"), 30),
)
_PATH = _PATTERNS[2][1]


def row_text(row: TranscriptRow) -> str:
    """The text of a row as a reader would see it (parts joined, tool calls listed)."""
    content = row.content
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for part in cast(list[Any], content):
            if isinstance(part, dict):
                value = cast(dict[str, Any], part).get("text")
                if isinstance(value, str):
                    parts.append(value)
        text = "\n".join(parts)
    else:
        text = ""
    for call in row.tool_calls or ():
        text += f"\n{call_name(call) or 'tool'}({call_arguments(call) or ''})"
    return text


def _clean(text: str) -> str:
    return redact_text(text)


def _is_commit(value: str) -> bool:
    return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def anchor_index(rows: Sequence[TranscriptRow], budget: int = ANCHOR_BUDGET) -> str:
    """Lines ``<label>: a, b(×3), …`` for each category found; "" when nothing was found."""
    corpus = "\n".join(_clean(row_text(r)) for r in rows if r.role != "system")
    if not corpus:
        return ""
    lines: list[str] = []
    used = 0
    for label, pattern, cap in _PATTERNS:
        counts: dict[str, int] = {}
        last: dict[str, int] = {}
        for order, match in enumerate(pattern.finditer(corpus)):
            value = match.group(0).strip().rstrip(".,;:")
            if label == "commits" and not _is_commit(value):
                continue
            counts[value] = counts.get(value, 0) + 1
            last[value] = order
        if not counts:
            continue
        ranked = sorted(counts, key=lambda v: (-counts[v], -last[v]))[:cap]
        line = f"{label}: " + ", ".join(
            f"{v} (×{counts[v]})" if counts[v] > 1 else v for v in ranked
        )
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def file_mentions(rows: Sequence[TranscriptRow], limit: int = MAX_LISTED) -> list[str]:
    """File paths mentioned in ``rows``, newest mention first."""
    found: list[str] = []
    for row in reversed(rows):
        for match in _PATH.finditer(row_text(row)):
            value = match.group(0).rstrip(".,;:")
            if value not in found:
                found.append(value)
            if len(found) >= limit:
                return found
    return found


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def user_messages(
    rows: Sequence[TranscriptRow], budget: int = USER_BUDGET, per_message: int = USER_MESSAGE_MAX
) -> list[str]:
    """Real user messages, newest first, redacted and cut to the budgets (the message that
    straddles the budget is cut to fit; older ones are left out)."""
    out: list[str] = []
    left = budget
    for row in reversed(rows):
        if not is_real_user(row):
            continue
        text = _clean(row_text(row).strip())
        if not text:
            continue
        text = _cut(text, min(per_message, left))
        if not text:
            break
        out.append(text)
        left -= len(text)
        if left <= 0:
            break
    return out


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else texts.FALLBACK_NONE


def _done_lines(rows: Sequence[TranscriptRow]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        if row.role == "assistant" and row.tool_calls:
            for call in row.tool_calls:
                args = _clean(call_arguments(call) or "")
                lines.append(_cut(f"called {call_name(call) or 'a tool'}({args})", TURN_MAX))
    return lines[-MAX_LISTED:]


def _blocked_lines(rows: Sequence[TranscriptRow]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        if row.role != "tool":
            continue
        text = row_text(row)
        lowered = text.lower()
        if "error" in lowered or "failed" in lowered or "traceback" in lowered:
            lines.append(_cut(_clean(text.strip().splitlines()[0] if text.strip() else text), 200))
    return lines[-5:]


def fallback_summary(
    rows: Sequence[TranscriptRow], *, previous: str | None = None, reason: str | None = None
) -> str:
    """Handoff notes for ``rows`` built without a model (the body; the engine adds the shared
    sections and the open/close markers)."""
    asks = user_messages(rows, budget=TURN_MAX, per_message=TURN_MAX)
    goal = texts.FALLBACK_GOAL.format(text=asks[0]) if asks else texts.FALLBACK_NO_GOAL
    body = texts.FALLBACK_BODY.format(
        goal=goal,
        done=_bullets(_done_lines(rows)),
        blocked=_bullets(_blocked_lines(rows)),
        files=_bullets(file_mentions(rows)),
        count=len(rows),
        reason=texts.FALLBACK_REASON.format(reason=reason) if reason else "",
    )
    if previous and previous.strip():
        body += texts.FALLBACK_PREVIOUS.format(text=_cut(_clean(previous.strip()), PREVIOUS_MAX))
    return _cut(body, FALLBACK_MAX)
