# pyright: strict
"""``todo_list``: the session's task list, kept by the agent loop (an inline tool).

The list is per agent session and lives in the process. Items are ``{id, content, status,
parent?}``; list order is priority; ``parent`` makes an item a subtask of another. A call without
``todos`` reads. With ``todos`` it either replaces the list (``merge`` false) or merges by id
(``merge`` true: only the fields given change, unknown ids are appended). Every call returns the
whole list, a revision that grows only when the list really changed, and counts per status.

Rules applied to what the model sends: at most ``MAX_ITEMS`` items (the first ones are kept);
content longer than ``MAX_CONTENT_CHARS`` is cut with a marker; an unknown status reads as
pending; a duplicate id keeps its last entry, in that entry's place; a parent that names the item
itself, a missing item or a cycle is dropped; in a flat list an ``in_progress`` item moves ahead of
earlier pending ones (nested lists keep their order). An item without content refuses the whole
write and leaves the list as it was.

The loop runs this tool itself (it needs the session's list), through ``TodoWiring.execute``; the
registry entry only carries the schema. On the first turn of a resumed session the prologue calls
``TodoWiring.hydrate`` with the stored transcript: the newest todo result that answers an earlier
assistant ``todo_list`` call (and is at most ``MAX_HYDRATE_CHARS`` long) rebuilds the list. After
compression P08 re-injects ``TodoList.render()``.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, cast

from indus.tools.calls import ToolCall, ToolResult
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "MAX_CONTENT_CHARS",
    "MAX_HYDRATE_CHARS",
    "MAX_ITEMS",
    "RENDER_HEADER",
    "STATUSES",
    "TODO_SCHEMA",
    "TOOL_NAME",
    "TodoInputError",
    "TodoList",
    "TodoWiring",
    "register_tools",
    "todo_wiring",
]

log = logging.getLogger(__name__)

TOOL_NAME = "todo_list"
TOOLSET = "todo"
STATUSES = ("pending", "in_progress", "completed", "cancelled")
ACTIVE = frozenset({"pending", "in_progress"})
MAX_ITEMS = 256
MAX_CONTENT_CHARS = 4_000
MAX_HYDRATE_CHARS = 512_000
CUT_MARK = " [cut]"
RENDER_HEADER = "[Task list carried over from before the conversation was compressed]"
_MARKERS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "cancelled": "[-]"}


class TodoInputError(ValueError):
    """The write was refused; the list is unchanged."""


@dataclass(frozen=True)
class _Item:
    id: str
    content: str
    status: str
    parent: str | None = None

    def as_dict(self) -> dict[str, str]:
        out = {"id": self.id, "content": self.content, "status": self.status}
        if self.parent:
            out["parent"] = self.parent
        return out


def _cap(text: str) -> str:
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    return text[: MAX_CONTENT_CHARS - len(CUT_MARK)] + CUT_MARK


def _status(value: object, fallback: str = "pending") -> str:
    text = str(value).strip().lower() if value is not None else ""
    return text if text in STATUSES else fallback


def _id_of(raw: object) -> str:
    if not isinstance(raw, Mapping):
        return ""
    value = cast(Mapping[str, Any], raw).get("id")
    return str(value).strip() if value is not None else ""


def _new_item(raw: object, position: int) -> _Item:
    """One item as sent, normalised; ``TodoInputError`` when it has no content."""
    if not isinstance(raw, Mapping):
        return _Item(f"item-{position + 1}", "(unreadable item)", "pending")
    entry = cast(Mapping[str, Any], raw)
    ident = _id_of(entry) or f"item-{position + 1}"
    content = str(entry.get("content") or "").strip()
    if not content:
        raise TodoInputError(
            f"todo item {ident!r} has no content; every item needs a short description"
        )
    parent = str(entry.get("parent") or "").strip() or None
    return _Item(
        ident, _cap(content), _status(entry.get("status")), parent if parent != ident else None
    )


def _last_wins(raw: Sequence[object]) -> list[tuple[int, object]]:
    """``raw`` with repeated ids collapsed to their last entry, in that entry's place."""
    last: dict[str, int] = {}
    for position, entry in enumerate(raw):
        last[_id_of(entry) or f"\x00{position}"] = position
    return [(p, raw[p]) for p in sorted(last.values())]


def _fix_parents(items: list[_Item]) -> list[_Item]:
    ids = {item.id for item in items}
    items = [i if i.parent is None or i.parent in ids else replace(i, parent=None) for i in items]
    parents = {item.id: item.parent for item in items}
    fixed: list[_Item] = []
    for item in items:
        seen = {item.id}
        node = item.parent
        while node is not None:
            if node in seen:
                item = replace(item, parent=None)  # a cycle: this item becomes a root
                parents[item.id] = None
                break
            seen.add(node)
            node = parents.get(node)
        fixed.append(item)
    return fixed


def _active_first(items: list[_Item]) -> list[_Item]:
    if any(item.parent for item in items):
        return items
    statuses = [item.status for item in items]
    if "in_progress" not in statuses:
        return items
    active = statuses.index("in_progress")
    if "pending" not in statuses[:active]:
        return items
    moved = list(items)
    moved.insert(statuses.index("pending"), moved.pop(active))
    return moved


def _settle(items: list[_Item]) -> list[_Item]:
    return _active_first(_fix_parents(items[:MAX_ITEMS]))


class TodoList:
    """One session's list. Thread-safe; every method returns copies."""

    def __init__(self) -> None:
        self._items: list[_Item] = []
        self._revision = 0
        self._lock = threading.Lock()

    @property
    def revision(self) -> int:
        return self._revision

    def read(self) -> list[dict[str, str]]:
        with self._lock:
            return [item.as_dict() for item in self._items]

    def __len__(self) -> int:
        return len(self._items)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"todos": [i.as_dict() for i in self._items], "revision": self._revision}

    def write(self, todos: Sequence[object], *, merge: bool = False) -> list[dict[str, str]]:
        """Replace or merge (see the module notes); ``TodoInputError`` leaves the list as it was."""
        with self._lock:
            before = list(self._items)
            after = self._merged(todos) if merge else self._fresh(todos)
            if after != before:
                self._items = after
                self._revision += 1
            return [item.as_dict() for item in self._items]

    def restore(self, todos: Sequence[object], *, revision: object = 0) -> None:
        """Adopt a stored list and its revision as they are (no new revision)."""
        try:
            items = self._fresh(todos)
        except TodoInputError:
            items = self._fresh([t for t in todos if str(_content_of(t)).strip()])
        try:
            number = max(0, int(cast(Any, revision) or 0))
        except (TypeError, ValueError):
            number = 0
        with self._lock:
            self._items = items
            self._revision = number

    @staticmethod
    def _fresh(todos: Sequence[object]) -> list[_Item]:
        return _settle([_new_item(raw, position) for position, raw in _last_wins(todos)])

    def _merged(self, todos: Sequence[object]) -> list[_Item]:
        items = list(self._items)
        where = {item.id: n for n, item in enumerate(items)}
        for position, raw in _last_wins(todos):
            ident = _id_of(raw)
            if not ident:
                continue  # a merge needs an id to know what to change
            entry = cast(Mapping[str, Any], raw)
            at = where.get(ident)
            if at is None:
                items.append(_new_item(raw, position))
                where[ident] = len(items) - 1
                continue
            current = items[at]
            content = str(entry.get("content") or "").strip()
            if content:
                current = replace(current, content=_cap(content))
            if entry.get("status") is not None:
                current = replace(current, status=_status(entry.get("status"), current.status))
            if "parent" in entry:
                parent = str(entry.get("parent") or "").strip() or None
                current = replace(current, parent=parent if parent != ident else None)
            items[at] = current
        return _settle(items)

    def result(self) -> dict[str, Any]:
        """What every call returns: the list, its revision and counts per status."""
        with self._lock:
            todos = [i.as_dict() for i in self._items]
            revision = self._revision
        summary: dict[str, int] = {"total": len(todos)}
        for status in STATUSES:
            summary[status] = sum(1 for t in todos if t["status"] == status)
        return {"todos": todos, "revision": revision, "summary": summary}

    def render(self) -> str | None:
        """The open work as text for re-injection after compression; ``None`` when nothing is open.

        Only pending and in-progress items are shown; a finished parent stays when one of its
        subtasks is still open, so the subtask keeps its context."""
        with self._lock:
            items = list(self._items)
        if not items:
            return None
        children: dict[str, list[_Item]] = {}
        for item in items:
            if item.parent:
                children.setdefault(item.parent, []).append(item)

        def walk(item: _Item, depth: int, out: list[str]) -> bool:
            below: list[str] = []
            open_below = False
            for child in children.get(item.id, []):
                open_below = walk(child, depth + 1, below) or open_below
            keep = item.status in ACTIVE or open_below
            if keep:
                marker = _MARKERS[item.status]
                out.append(f"{'  ' * depth}- {marker} {item.id}: {item.content} ({item.status})")
                out.extend(below)
            return keep

        lines = [RENDER_HEADER]
        for item in items:
            if not item.parent:
                walk(item, 0, lines)
        return "\n".join(lines) if len(lines) > 1 else None


def _content_of(raw: object) -> object:
    return cast(Mapping[str, Any], raw).get("content") if isinstance(raw, Mapping) else "x"


# -- the tool -----------------------------------------------------------------------------------


def _merge_flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def run_todo(todos: TodoList, args: Mapping[str, Any]) -> ToolResult:
    """One ``todo_list`` call against ``todos``."""
    raw = args.get("todos")
    if raw is not None:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return ToolResult.error("todos must be a list of items (got text that is not JSON)")
        if not isinstance(raw, list):
            return ToolResult.error(f"todos must be a list of items, not {type(raw).__name__}")
        try:
            todos.write(cast(list[object], raw), merge=_merge_flag(args.get("merge", False)))
        except TodoInputError as exc:
            return ToolResult.error(str(exc))
    return ToolResult.ok(json.dumps(todos.result(), ensure_ascii=False))


class HistoryRow(Protocol):
    """The parts of a stored row the hydrator reads (``core.transcript.TranscriptRow`` fits)."""

    role: str
    content: Any
    tool_calls: list[dict[str, Any]] | None
    tool_call_id: str | None


def _call_parts(call: object) -> tuple[str, str]:
    if not isinstance(call, Mapping):
        return "", ""
    entry = cast(Mapping[str, Any], call)
    ident = entry.get("id") or entry.get("call_id") or ""
    function = entry.get("function")
    name = cast(Mapping[str, Any], function).get("name") if isinstance(function, Mapping) else None
    return str(ident).strip(), name if isinstance(name, str) else ""


def latest_stored(rows: Sequence[HistoryRow]) -> tuple[list[object], object] | None:
    """``(todos, revision)`` of the newest todo result that answers an earlier todo call."""
    asked: dict[str, int] = {}
    for index, row in enumerate(rows):
        if row.role == "assistant":
            for call in row.tool_calls or ():
                ident, name = _call_parts(call)
                if ident and name == TOOL_NAME:
                    asked.setdefault(ident, index)
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        content = row.content
        if row.role != "tool" or not isinstance(content, str):
            continue
        at = asked.get(row.tool_call_id or "")
        if at is None or at >= index:
            continue  # not paired with an earlier todo call: a forged row cannot seed the list
        if len(content) > MAX_HYDRATE_CHARS:
            log.warning("a stored todo result of %d characters was skipped", len(content))
            continue
        if '"todos"' not in content:
            continue
        try:
            body: object = json.loads(content)
        except ValueError:
            continue
        if isinstance(body, dict):
            data = cast(dict[str, Any], body)
            items: object = data.get("todos")
            if isinstance(items, list):
                return cast(list[object], items), data.get("revision", 1)
    return None


@dataclass
class TodoWiring:
    """One session's list plus the two callables the agent loop takes."""

    todos: TodoList = field(default_factory=TodoList)

    async def execute(self, call: ToolCall, state: object) -> ToolResult:
        """The inline executor (``core.turn_state.InlineExecutor``)."""
        return run_todo(self.todos, call.args)

    def hydrate(self, rows: Sequence[HistoryRow]) -> None:
        """The history hydrator: rebuild the list from the stored transcript."""
        found = latest_stored(rows)
        if found is None:
            return
        items, revision = found
        try:
            number = int(cast(Any, revision) or 0)
        except (TypeError, ValueError):
            number = 1
        if number > self.todos.revision:
            self.todos.restore(items, revision=number)
            log.info("todo list restored from history: %d item(s)", len(self.todos))


def todo_wiring() -> TodoWiring:
    return TodoWiring()


TODO_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Keep a task list for work with several steps, or when the user asks for several "
        "things. Call it with no arguments to see the list. When a request covers many "
        "instances, give each one its own item so none is forgotten. The order of the list is "
        "the priority. Keep exactly one item in_progress at a time; split big steps into "
        "subtasks with parent. Mark an item completed only once the work is checked and done; "
        "if a step fails, cancel it and add a new item for the next attempt. Every call returns "
        "the whole list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Items to write. Leave out to read the list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Short unique id."},
                        "content": {"type": "string", "description": "What the task is."},
                        "status": {"type": "string", "enum": list(STATUSES)},
                        "parent": {
                            "type": "string",
                            "description": "Id of the item this one is a subtask of; leave out "
                            "for a top-level item.",
                        },
                    },
                    "required": ["id", "content", "status"],
                },
            },
            "merge": {
                "type": "boolean",
                "description": "true: change the items named by id and add new ones. false "
                "(default): the items sent become the whole new list.",
                "default": False,
            },
        },
        "required": [],
    },
}


def _outside_the_loop(args: Mapping[str, Any]) -> str:
    return ToolResult.error(
        "todo_list is run by the agent loop; it is not available in this context"
    ).text


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        TOOL_NAME, TOOLSET, TODO_SCHEMA, _outside_the_loop, traits=ToolTraits(mutating=True)
    )
