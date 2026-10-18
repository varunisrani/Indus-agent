"""Alternation repair on the live history (R01.2).

Providers accept a conversation only when it alternates: after the system prompt, user and
assistant turns take turns, and every assistant tool call is answered by exactly one tool result
in the run of tool rows right after it. Resumed, imported, interrupted or host-fed histories break
this in a few known ways; ``repair_transcript`` fixes them before a model call:

1. stored-parts content (``PARTS_PREFIX``) is decoded, so parts are merged as parts;
2. consecutive assistant rows become one (calls united in order, text joined);
3. tool results that answer no outstanding call of the run they sit in are dropped;
4. tool calls with no result in the run right after them are pruned (an emptied row goes);
5. consecutive user rows become one, without losing text.

A steer row (``display_kind="steer"``) is never merged with another user row, and a scaffold row
is never merged with a durable one. One pass can expose work for an earlier one (dropping an orphan
result between two assistant rows makes them neighbours), so the passes repeat until nothing
changes.

The input list and its rows are never mutated: a row that changes is a copy. The report says what
changed and where the persistence cursor (how many leading rows are already stored) lands.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from indus.core.transcript import TranscriptRow, call_id, content_has_payload, is_damaged_payload
from indus.state.messages import PARTS_PREFIX, decode_content

__all__ = ["PASS_NAMES", "RepairReport", "repair_transcript"]

PASS_NAMES = ("merged_assistants", "dropped_results", "pruned_calls", "merged_users")


@dataclass(frozen=True)
class RepairReport:
    """What ``repair_transcript`` did.

    ``counts``: repairs per pass (see ``PASS_NAMES``) plus ``decoded`` (rows whose stored-parts
    content was decoded; not a repair). ``flushed``: the new persistence cursor, ``None`` when no
    cursor was given. ``rewritten``: output indices below ``flushed`` whose stored row no longer
    matches (the persistence layer replaces them in place). ``sources``: for each output row, the
    input indices it stands for. ``dropped``: input indices that left the history entirely.
    """

    counts: Mapping[str, int] = field(default_factory=dict[str, int])
    flushed: int | None = None
    rewritten: tuple[int, ...] = ()
    sources: tuple[tuple[int, ...], ...] = ()
    dropped: tuple[int, ...] = ()

    @property
    def total(self) -> int:
        return sum(self.counts.get(name, 0) for name in PASS_NAMES)


class _Item:
    """One row being repaired: the row, the input rows it stands for, whether it is our copy."""

    __slots__ = ("changed", "owned", "row", "sources")

    def __init__(self, row: TranscriptRow, index: int) -> None:
        self.row = row
        self.sources = [index]
        self.owned = False
        self.changed = False

    def mine(self) -> TranscriptRow:
        """The row, copied first if it is still the caller's object."""
        if not self.owned:
            self.row = self.row.clone()
            self.owned = True
        return self.row


def _snapshot(row: TranscriptRow) -> tuple[object, ...]:
    return (
        row.content,
        row.tool_calls,
        row.reasoning,
        row.reasoning_details,
        row.api_content,
        row.display_kind,
    )


def _as_parts(content: object) -> list[Any]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return list(cast(list[Any], content))
    return [content]


# -- pass 0: decode --------------------------------------------------------------------------------


def _decode(items: list[_Item]) -> int:
    decoded = 0
    for item in items:
        content = item.row.content
        if isinstance(content, str) and content.startswith(PARTS_PREFIX):
            value = decode_content(content)
            if value is not content:
                item.mine().content = value
                decoded += 1
    return decoded


# -- pass 1: consecutive assistant rows ----------------------------------------------------------


def _join_assistant_content(first: object, second: object) -> object:
    if not content_has_payload(second):
        return first
    if not content_has_payload(first):
        return second
    if isinstance(first, str) and isinstance(second, str):
        return f"{first.strip()}\n{second.strip()}"
    return _as_parts(first) + _as_parts(second)


def _fold_assistant(into: _Item, other: _Item) -> None:
    survivor = into.mine()
    later = other.mine()
    before = _snapshot(survivor)
    content = _join_assistant_content(survivor.content, later.content)
    content_changed = content != survivor.content
    survivor.content = content
    calls = list(survivor.tool_calls or []) + list(later.tool_calls or [])
    survivor.tool_calls = calls or None
    if not survivor.reasoning and later.reasoning:
        survivor.reasoning = later.reasoning
    details = list(survivor.reasoning_details or []) + list(later.reasoning_details or [])
    survivor.reasoning_details = details or None
    if later.tool_call_uids:
        survivor.tool_call_uids = {**(survivor.tool_call_uids or {}), **later.tool_call_uids}
    survivor.finish_reason = later.finish_reason or survivor.finish_reason
    survivor.scaffold = survivor.scaffold and later.scaffold
    if content_changed:
        survivor.api_content = None
    into.sources.extend(other.sources)
    into.changed = into.changed or other.changed or _snapshot(survivor) != before


def _merge_assistants(items: list[_Item]) -> tuple[list[_Item], int]:
    out: list[_Item] = []
    merged = 0
    for item in items:
        prev = out[-1] if out else None
        if prev is not None and prev.row.role == "assistant" and item.row.role == "assistant":
            _fold_assistant(prev, item)
            merged += 1
            continue
        out.append(item)
    return out, merged


# -- pass 2: tool results that answer nothing ----------------------------------------------------


def _drop_stray_results(items: list[_Item]) -> tuple[list[_Item], int]:
    out: list[_Item] = []
    dropped = 0
    outstanding: Counter[str] = Counter()
    for item in items:
        row = item.row
        if row.role == "tool":
            tid = (row.tool_call_id or "").strip()
            if not tid or outstanding[tid] <= 0:
                dropped += 1
                continue
            outstanding[tid] -= 1
        elif row.role == "assistant":
            outstanding = Counter(cid for cid in row.call_ids() if cid)
        else:
            outstanding = Counter()
        out.append(item)
    return out, dropped


# -- pass 3: calls with no result right after them -----------------------------------------------


def _answers_after(items: list[_Item], index: int) -> Counter[str]:
    answers: Counter[str] = Counter()
    for follower in items[index + 1 :]:
        if follower.row.role != "tool":
            break
        tid = (follower.row.tool_call_id or "").strip()
        if tid:
            answers[tid] += 1
    return answers


def _prune_unanswered(items: list[_Item]) -> tuple[list[_Item], int]:
    out: list[_Item] = []
    pruned = 0
    for index, item in enumerate(items):
        row = item.row
        if row.role != "assistant" or not row.tool_calls:
            out.append(item)
            continue
        answers = _answers_after(items, index)
        kept: list[dict[str, Any]] = []
        for call in row.tool_calls:
            cid = call_id(call)
            if cid and answers[cid] > 0:
                answers[cid] -= 1
                kept.append(call)
        if len(kept) == len(row.tool_calls):
            out.append(item)
            continue
        pruned += 1
        survivor = item.mine()
        survivor.tool_calls = kept or None
        if survivor.tool_call_uids:
            ids = {call_id(c) for c in kept}
            survivor.tool_call_uids = {
                k: v for k, v in survivor.tool_call_uids.items() if k in ids
            } or None
        item.changed = True
        if not survivor.has_payload():
            continue
        out.append(item)
    return out, pruned


# -- pass 4: consecutive user rows ---------------------------------------------------------------


def _user_mergeable(row: TranscriptRow) -> bool:
    content = row.content
    if is_damaged_payload(content):
        return False
    return content is None or isinstance(content, (str, list))


def _join_user_content(first: object, second: object) -> object:
    if not isinstance(first, (str, type(None))) or not isinstance(second, (str, type(None))):
        return _as_parts(first) + _as_parts(second)
    a = first if isinstance(first, str) else ""
    b = second if isinstance(second, str) else ""
    if a and b:
        return f"{a}\n\n{b}"
    return a or b or first


def _fold_user(into: _Item, other: _Item) -> None:
    survivor = into.mine()
    later = other.mine()
    before = _snapshot(survivor)
    content = _join_user_content(survivor.content, later.content)
    if content != survivor.content:
        survivor.content = content
        survivor.api_content = None
    if survivor.display_kind != later.display_kind:
        survivor.display_kind = None
    into.sources.extend(other.sources)
    into.changed = into.changed or other.changed or _snapshot(survivor) != before


def _merge_users(items: list[_Item]) -> tuple[list[_Item], int]:
    out: list[_Item] = []
    merged = 0
    for item in items:
        prev = out[-1] if out else None
        if (
            prev is not None
            and prev.row.role == "user"
            and item.row.role == "user"
            and not prev.row.is_steer
            and not item.row.is_steer
            and prev.row.scaffold == item.row.scaffold
            and _user_mergeable(prev.row)
            and _user_mergeable(item.row)
        ):
            _fold_user(prev, item)
            merged += 1
            continue
        out.append(item)
    return out, merged


_PASSES: tuple[tuple[str, Callable[[list[_Item]], tuple[list[_Item], int]]], ...] = (
    ("merged_assistants", _merge_assistants),
    ("dropped_results", _drop_stray_results),
    ("pruned_calls", _prune_unanswered),
    ("merged_users", _merge_users),
)


def repair_transcript(
    rows: Sequence[TranscriptRow], *, flushed: int | None = None
) -> tuple[list[TranscriptRow], RepairReport]:
    """Repair ``rows`` (see the module notes); returns the new list and a report.

    ``flushed`` is the number of leading rows already stored. Afterwards an output row counts as
    stored when the first row it stands for was; such rows that changed are in ``rewritten``.
    """
    items = [_Item(row, index) for index, row in enumerate(rows)]
    counts: dict[str, int] = dict.fromkeys(PASS_NAMES, 0)
    counts["decoded"] = _decode(items)
    for _ in range(len(items) + 2):
        made = 0
        for name, run in _PASSES:
            items, n = run(items)
            counts[name] += n
            made += n
        if not made:
            break
    sources = tuple(tuple(item.sources) for item in items)
    kept = {index for group in sources for index in group}
    dropped = tuple(index for index in range(len(rows)) if index not in kept)
    new_flushed: int | None = None
    rewritten: tuple[int, ...] = ()
    if flushed is not None:
        new_flushed = sum(1 for group in sources if group[0] < flushed)
        rewritten = tuple(i for i in range(new_flushed) if items[i].changed)
    report = RepairReport(
        counts=counts,
        flushed=new_flushed,
        rewritten=rewritten,
        sources=sources,
        dropped=dropped,
    )
    return [item.row for item in items], report
