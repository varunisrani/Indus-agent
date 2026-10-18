"""Pruning and boundaries: the model-free steps before a summary (P08-03).

Step 1 — ``prune``: a tool result longer than 200 characters that sits before the protected tail
becomes a one-line placeholder naming the tool and the size it had. The newest tool round the
model has not answered yet is never touched. A pruned skill load leaves ``[skill elided: <name>]``
and its name is collected (newest first, at most 20) so the summary can list what to reload.

Step 2 — ``window``: the head is the first three rows (the system row and the first exchange);
the tail is found by walking backwards over row estimates until a token budget is met:

- lean (default): ``2.5 % × window`` clamped to [10,000, 25,000] and to 20 % of the window;
- legacy: ``trigger × target_ratio`` (0.20), also held to 20 % of the window;

rows may overrun the budget by 1.5× (never past 20 % of the window) so whole rows are kept; the
tail keeps at least 8 rows when there are enough to spare and at least ``min_user`` real user
rows. A boundary never falls between an assistant tool call and its results: it moves back to the
parent assistant row. ``messages[head_end:tail_start]`` is the middle; empty means nothing can be
compressed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from indus.core.compression import texts
from indus.core.compression.anchor import DEFAULT_IMAGE_COST, IMAGE_PART_TYPES, estimate_message
from indus.core.transcript import TranscriptRow, call_arguments, call_id, call_name

__all__ = [
    "HEAD_ROWS",
    "MAX_SKILL_MARKERS",
    "MIN_TAIL_ROWS",
    "PRUNE_MIN_CHARS",
    "SKILL_TOOLS",
    "PruneResult",
    "TailPolicy",
    "Window",
    "align_back",
    "align_forward",
    "find_tail_start",
    "head_end",
    "is_real_user",
    "pending_round",
    "prune",
    "row_tokens",
    "window",
]

HEAD_ROWS = 3
PRUNE_MIN_CHARS = 200
MIN_TAIL_ROWS = 8
MAX_SKILL_MARKERS = 20
LEAN_SHARE = 0.025
LEAN_FLOOR = 10_000
LEAN_CAP = 25_000
TAIL_WINDOW_SHARE = 0.20
SOFT_OVERRUN = 1.5
SKILL_TOOLS = frozenset({"skill_view"})

Pricer = Callable[[TranscriptRow], int]


def row_tokens(row: TranscriptRow, image_cost: int = DEFAULT_IMAGE_COST) -> int:
    return estimate_message(row.to_message(), image_cost)


def is_real_user(row: TranscriptRow) -> bool:
    """A user row the person wrote: not scaffold, not our summary or carried-over task list."""
    if row.role != "user" or row.scaffold:
        return False
    text = row.content if isinstance(row.content, str) else ""
    return not text.startswith((texts.SUMMARY_OPEN, texts.TODO_PREFIX))


@dataclass(frozen=True)
class TailPolicy:
    """How large the protected tail is."""

    window: int
    trigger: int = 0
    mode: str = "lean"
    target_ratio: float = 0.20
    min_user: int = 1

    def budget(self) -> int:
        if self.mode == "legacy":
            budget = int(self.trigger * self.target_ratio)
        else:
            budget = max(LEAN_FLOOR, min(LEAN_CAP, int(self.window * LEAN_SHARE)))
        if self.window > 0:
            budget = min(budget, int(self.window * TAIL_WINDOW_SHARE))
        return max(1, budget)

    def soft_ceiling(self) -> int:
        budget = self.budget()
        ceiling = int(budget * SOFT_OVERRUN)
        if self.window > 0:
            ceiling = min(ceiling, int(self.window * TAIL_WINDOW_SHARE))
        return max(ceiling, budget)


# -- groups ---------------------------------------------------------------------------------------


def align_back(rows: Sequence[TranscriptRow], index: int) -> int:
    """Move ``index`` back off tool results onto the assistant row that called them."""
    while 0 < index < len(rows) and rows[index].role == "tool":
        index -= 1
    return index


def align_forward(rows: Sequence[TranscriptRow], index: int) -> int:
    """Move ``index`` forward past tool results (so the cut follows a whole group)."""
    while 0 <= index < len(rows) and rows[index].role == "tool":
        index += 1
    return index


def head_end(rows: Sequence[TranscriptRow]) -> int:
    return align_forward(rows, min(HEAD_ROWS, len(rows)))


def pending_round(rows: Sequence[TranscriptRow]) -> range:
    """The newest tool round the model has not answered: the calling assistant row and the tool
    rows after it at the very end; empty when the transcript does not end that way."""
    end = len(rows)
    index = end
    while index > 0 and rows[index - 1].role == "tool":
        index -= 1
    if index == end or index == 0:
        return range(0)
    caller = rows[index - 1]
    if caller.role != "assistant" or not caller.tool_calls:
        return range(0)
    return range(index - 1, end)


# -- the tail --------------------------------------------------------------------------------------


def _min_rows(available: int) -> int:
    if available <= 1:
        return 0
    return min(MIN_TAIL_ROWS, max(3, available - 2), available)


def find_tail_start(
    rows: Sequence[TranscriptRow],
    head: int,
    policy: TailPolicy,
    price: Pricer = row_tokens,
) -> int:
    """First index of the protected tail (``len(rows)`` when nothing is protected)."""
    count = len(rows)
    if count <= head:
        return count
    ceiling = policy.soft_ceiling()
    floor = _min_rows(count - head - 1)
    used = 0
    cut = count
    for index in range(count - 1, head - 1, -1):
        cost = price(rows[index])
        if used + cost > ceiling and (count - index - 1) >= floor:
            break
        used += cost
        cut = index
    if cut <= head:
        # Everything fits the tail budget: keep only the row floor so something can be condensed.
        cut = max(count - floor, head + 1)
    cut = min(cut, count - 1)  # the newest row never leaves the tail
    cut = align_back(rows, cut)
    cut = _keep_users(rows, cut, head, policy.min_user)
    return max(cut, head)


def _keep_users(rows: Sequence[TranscriptRow], cut: int, head: int, wanted: int) -> int:
    if wanted <= 0:
        return cut
    have = sum(1 for r in rows[cut:] if is_real_user(r))
    index = cut - 1
    while have < wanted and index >= head:
        if is_real_user(rows[index]):
            have += 1
            cut = index
        index -= 1
    return align_back(rows, cut)


@dataclass(frozen=True)
class Window:
    head_end: int
    tail_start: int

    @property
    def empty(self) -> bool:
        return self.tail_start <= self.head_end


def window(
    rows: Sequence[TranscriptRow],
    policy: TailPolicy,
    *,
    keep_last: int | None = None,
    price: Pricer = row_tokens,
) -> Window:
    """The compressible middle of ``rows``. ``keep_last`` (manual ``here N``) fixes the tail to
    the newest N rows (moved back to keep a tool group whole)."""
    head = head_end(rows)
    if keep_last is not None:
        start = align_back(rows, max(head, len(rows) - max(1, keep_last)))
        return Window(head, max(start, head))
    return Window(head, find_tail_start(rows, head, policy, price))


# -- pruning ---------------------------------------------------------------------------------------


@dataclass
class PruneResult:
    rows: list[TranscriptRow]
    pruned: int = 0
    skills: list[str] = field(default_factory=list[str])


def _tool_index(rows: Sequence[TranscriptRow]) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for row in rows:
        if row.role != "assistant":
            continue
        for call in row.tool_calls or ():
            ident = call_id(call)
            if ident:
                found[ident] = (call_name(call) or "tool", call_arguments(call) or "")
    return found


def _skill_name(arguments: str) -> str:
    try:
        parsed: object = json.loads(arguments) if arguments else None
    except ValueError:
        return ""
    if isinstance(parsed, Mapping):
        name = cast(Mapping[str, Any], parsed).get("name")
        return name.strip() if isinstance(name, str) else ""
    return ""


def _strip_images(content: list[Any]) -> list[Any] | None:
    kept: list[Any] = []
    dropped = 0
    for part in content:
        if (
            isinstance(part, Mapping)
            and cast(Mapping[str, Any], part).get("type") in IMAGE_PART_TYPES
        ):
            dropped += 1
        else:
            kept.append(part)
    if not dropped:
        return None
    kept.append({"type": "text", "text": texts.IMAGES_ELIDED.format(count=dropped)})
    return kept


def _is_placeholder(text: str) -> bool:
    return text.startswith((texts.PRUNED_PREFIX, texts.SKILL_MARKER_PREFIX))


def prune(rows: Sequence[TranscriptRow], boundary: int) -> PruneResult:
    """``rows`` with long tool results before ``boundary`` replaced by copies (see the module
    notes); the input rows are never modified and untouched rows are the same objects."""
    spared = pending_round(rows)
    calls = _tool_index(rows)
    out = list(rows)
    skills: list[str] = []
    pruned = 0
    for index in range(min(boundary, len(out))):
        row = out[index]
        if row.role != "tool" or index in spared:
            continue
        content = row.content
        if isinstance(content, list):
            stripped = _strip_images(cast(list[Any], content))
            if stripped is not None:
                out[index] = row.clone(content=stripped, api_content=None)
                pruned += 1
            continue
        if (
            not isinstance(content, str)
            or len(content) <= PRUNE_MIN_CHARS
            or _is_placeholder(content)
        ):
            continue
        name, arguments = calls.get(row.tool_call_id or "", (row.name or "tool", ""))
        skill = _skill_name(arguments) if name in SKILL_TOOLS else ""
        if skill:
            text = texts.SKILL_MARKER.format(name=skill)
            skills.append(skill)
        else:
            text = texts.PRUNED_TOOL.format(tool=name, chars=len(content))
        out[index] = row.clone(content=text, api_content=None)
        pruned += 1
    newest_first = list(dict.fromkeys(reversed(skills)))[:MAX_SKILL_MARKERS]
    return PruneResult(out, pruned, newest_first)
