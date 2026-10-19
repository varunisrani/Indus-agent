# pyright: strict
"""The ``memory`` model tool (P09-02): add, patch and remove entries of one store per call.

Shapes. Single: ``{action: add|patch|remove, target, content?, old_text?}`` (``new_text`` stands in
for ``content`` when ``content`` is missing). Batch: ``{target, operations: [{action, content?,
old_text?}, ...]}``, all or nothing, the limit checked on the final list. ``target`` (``memory`` or
``user``) is required at the top level; one call writes one store. There is no read action, and
``replace`` is refused with a pointer to ``patch``.

Answers are JSON text. Every refusal says what to send next. Missing ``old_text`` and a store over
budget carry the live entries; a failed batch op does not (the store did not change and the model
holds the list already); no match carries the three closest entries.

Per turn, failed attempts are counted (argument slips such as an unknown action are not). After
``MAX_FAILURES_PER_TURN`` the next failure answers with a terminal result telling the model to stop
and answer the user, and every later memory call in that turn gets the same answer without touching
the files — a failed side effect must never eat the reply. A success resets the count.

Safety. New and patched text passes the strict threat scan (scope ``memory``); a hit is refused by
category name only. In an unattended review fork, ``patch`` and ``remove`` (alone or inside a batch)
are staged as pending writes pinned to the full entry they target, never applied (R07.4); a failure
to stage refuses the call. With ``memory.write_approval`` on, a foreground write in the interactive
CLI asks the person (approve / reject) and everything else is staged (P09-03).

The loop runs this tool itself (``MemoryWiring.execute`` in the inline table, P04-09); the registry
entry carries the schema, the availability probe (off when both stores are disabled) and the schema
narrowing when only one store is enabled. A foreground success calls ``on_success`` hooks (the
review counter of P09-14 resets there). Writes reach the disk at once; the session's prompt keeps
the block it was built with (P09-04).
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from indus.core.turn_state import TurnState
from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.learning import pending
from indus.learning.memory.store import (
    CONFIG_ENABLE_KEYS,
    STORE_LABELS,
    TARGETS,
    MemoryStore,
    Op,
    Result,
    Target,
    coerce_target,
)
from indus.protocol.requests_port import RequestNotAnswered
from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice
from indus.safety import threat_scan
from indus.tools.calls import ToolCall, ToolResult
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "MAX_FAILURES_PER_TURN",
    "MEMORY_SCHEMA",
    "TOOL_NAME",
    "TOOLSET",
    "Asker",
    "MemoryTool",
    "MemoryWiring",
    "apply_pending",
    "enabled_targets",
    "memory_wiring",
    "probe",
    "register_tools",
    "schema_overrides",
]

log = logging.getLogger(__name__)

TOOL_NAME = "memory"
TOOLSET = "memory"
MAX_FAILURES_PER_TURN = 3
ACTIONS = ("add", "patch", "remove")
DESTRUCTIVE = frozenset({"patch", "remove"})
# Failures that count toward the per-turn cap: the attempt reached the store and did not land.
COUNTED = frozenset(
    {
        "no_match",
        "over_limit",
        "stale",
        "span_repeated",
        "no_op",
        "blocked",
        "drift",
        "unreadable",
        "lock_busy",
        "lock_refused",
    }
)

type Asker = Callable[[str, str], Awaitable[bool | None]]
"""Ask a person about one memory write: ``(summary, detail)`` → approved, refused, or None when
nobody could be asked (the write is then staged)."""

_TARGET_TEXT = (
    "'memory' = your own notes (environment facts, conventions, tool quirks, lessons that hold in "
    "every session). 'user' = who the user is (name, role, preferences, how they like to work)."
)

MEMORY_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Keep short facts that should still be true in every future session. Saved entries "
        "appear at the start of each new session, so keep them few and dense.\n\n"
        "Write one store per call. For several changes send one call with 'operations' (each "
        "{action, content?, old_text?}): they apply together and the size limit is checked only "
        "on the result, so one call can drop or shorten old entries and add a new one. Use the "
        "single action/content/old_text fields for one lone change. A successful result means "
        "the change is saved; do not send it again.\n\n"
        "Actions: 'add' saves a new entry (content). 'patch' changes the text old_text inside "
        "one entry to content and keeps the rest of that entry. 'remove' deletes the whole "
        "entry that contains old_text.\n\n"
        "Save: stable facts about the user and their environment, standing corrections. Do not "
        "save: task progress, one-off details, things easy to look up again, or how-to steps "
        "(those belong in a skill).\n\n"
        f"Stores: {_TARGET_TEXT}"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "enum": list(TARGETS),
                "description": "Which store this call writes: 'memory' (your notes) or 'user' "
                "(the user's profile).",
            },
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": "One change: add, patch or remove. Leave out when sending "
                "'operations'.",
            },
            "content": {
                "type": "string",
                "description": "add: the new entry. patch: the text that takes old_text's place.",
            },
            "old_text": {
                "type": "string",
                "description": "patch and remove: text copied from one saved entry. patch "
                "changes exactly this text (it must occur once in the entry); remove deletes the "
                "entry that contains it.",
            },
            "new_text": {"type": "string", "description": "Same as content."},
            "operations": {
                "type": "array",
                "description": "Several changes to the same store, applied together.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": list(ACTIONS)},
                        "content": {"type": "string"},
                        "new_text": {"type": "string"},
                        "old_text": {"type": "string"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["target"],
    },
}

_ONE_STORE_TEXT: Mapping[Target, str] = {
    "memory": "Only your notes store is on here: always send target 'memory' (environment facts, "
    "conventions, tool quirks, lessons).",
    "user": "Only the user profile store is on here: always send target 'user' (name, role, "
    "preferences, how they like to work).",
}


# -- availability and schema ----------------------------------------------------------------------


def _flag(scope: Scope | None, key: str) -> bool:
    if scope is None or scope.config is None:
        return True
    try:
        value = scope.config.get(key)
    except Exception:
        log.debug("could not read %s; treating the store as enabled", key, exc_info=True)
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "off", "0")
    return True


def enabled_targets(scope: Scope | None) -> tuple[Target, ...]:
    """The stores ``scope``'s config leaves on."""
    return tuple(t for t in TARGETS if _flag(scope, CONFIG_ENABLE_KEYS[t]))


def probe(scope: Scope) -> bool:
    """The tool is offered when at least one store is on."""
    return bool(enabled_targets(scope))


def schema_overrides(scope: Scope | None) -> Mapping[str, Any] | None:
    """With one store off, advertise only the other (description and target enum)."""
    live = enabled_targets(scope)
    if len(live) != 1:
        return None
    parameters = copy.deepcopy(MEMORY_SCHEMA["parameters"])
    target = parameters["properties"]["target"]
    target["enum"] = list(live)
    target["description"] = _ONE_STORE_TEXT[live[0]]
    description = cast(str, MEMORY_SCHEMA["description"]).replace(
        f"Stores: {_TARGET_TEXT}", _ONE_STORE_TEXT[live[0]]
    )
    return {"description": description, "parameters": parameters}


def _outside_the_loop(args: Mapping[str, Any]) -> str:
    return ToolResult.error(
        "memory is run by the agent loop; it is not available in this context"
    ).text


def register_tools(registry: ToolRegistry) -> None:
    """The schema-only registry entry (the loop runs the tool through ``MemoryWiring``)."""
    registry.register(
        TOOL_NAME,
        TOOLSET,
        MEMORY_SCHEMA,
        _outside_the_loop,
        probe=probe,
        schema_overrides=schema_overrides,
        traits=ToolTraits(mutating=True),
    )


# -- parsing --------------------------------------------------------------------------------------


class _Refusal(Exception):
    def __init__(self, body: dict[str, Any], *, counted: bool = False) -> None:
        super().__init__(str(body.get("error", "")))
        self.body = body
        self.counted = counted


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": message, **extra}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _op_from(raw: Mapping[str, Any], label: str) -> Op:
    action = raw.get("action")
    if action == "replace":
        raise _Refusal(
            _error(
                f"{label}'replace' is not an action here; use 'patch': old_text is the exact "
                "text to change and content its new text (the rest of the entry stays). To "
                "rewrite a whole entry, use the whole entry as old_text. Nothing was written."
            )
        )
    if action not in ACTIONS:
        raise _Refusal(
            _error(
                f"{label}unknown action {action!r}; use add, patch or remove. Nothing was written."
            )
        )
    content = _text(raw.get("content")) or _text(raw.get("new_text"))
    return Op(cast(str, action), content, _text(raw.get("old_text")))


@dataclass(frozen=True)
class _Request:
    target: Target
    ops: tuple[Op, ...]
    batch: bool


def _parse(args: Mapping[str, Any], live: Sequence[Target]) -> _Request:
    raw_target = args.get("target")
    target = coerce_target(raw_target)
    if raw_target is None or raw_target == "":
        raise _Refusal(
            _error(
                "target is required: send target 'memory' or 'user' at the top level of the call "
                "(one store per call). Nothing was written."
            )
        )
    if target is None:
        raise _Refusal(_error(f"unknown target {raw_target!r}; use 'memory' or 'user'."))
    if target not in live:
        key = CONFIG_ENABLE_KEYS[target]
        raise _Refusal(
            _error(
                f"the {STORE_LABELS[target]} store is turned off ({key}: false); nothing was "
                "written.",
                target=target,
            )
        )
    operations = args.get("operations")
    if isinstance(operations, str) and operations.strip():
        try:
            operations = json.loads(operations)
        except ValueError:
            raise _Refusal(
                _error("operations must be a list of {action, content?, old_text?}")
            ) from None
    if operations not in (None, [], ""):
        if not isinstance(operations, list):
            raise _Refusal(_error("operations must be a list of {action, content?, old_text?}"))
        ops: list[Op] = []
        for index, item in enumerate(cast(list[object], operations), start=1):
            if not isinstance(item, Mapping):
                raise _Refusal(_error(f"operation {index} is not an object; nothing was written."))
            raw = cast(Mapping[str, Any], item)
            named = raw.get("target")
            if named not in (None, "", target):
                raise _Refusal(
                    _error(
                        f"operation {index} names target {named!r} but the call writes "
                        f"{target!r}; one call writes one store, so send two calls. Nothing was "
                        "written."
                    )
                )
            ops.append(_op_from(raw, f"operation {index}: "))
        return _Request(target, tuple(ops), True)
    if not args.get("action"):
        raise _Refusal(_error("send 'action' (add, patch or remove) or a list of 'operations'."))
    return _Request(target, (_op_from(args, ""),), False)


# -- the tool -------------------------------------------------------------------------------------


def _scan(text: str) -> str | None:
    found = threat_scan.scan(text, scope="memory")
    if not found:
        return None
    kinds = sorted({f.split(":", 1)[0] for f in found})
    return (
        "refused: the text matches a blocked pattern ("
        + ", ".join(kinds)
        + "). Memory reaches every future session, so it may not hold instructions of that kind."
    )


def _describe_op(op: Op) -> str:
    if op.action == "add":
        return f"add: {op.content}"
    if op.action == "remove":
        return f"remove the entry containing: {op.old_text}"
    return f"patch: '{op.old_text}' -> '{op.content}' (the rest of the entry stays)"


def _summary(request: _Request) -> tuple[str, str]:
    label = STORE_LABELS[request.target]
    if request.batch:
        head = f"{len(request.ops)} change(s) to {label}"
    else:
        head = f"{request.ops[0].action} in {label}"
    return head, "\n".join(_describe_op(op) for op in request.ops)


@dataclass
class MemoryTool:
    """One session's memory tool: the store, the pending store, the per-turn failure count."""

    store: MemoryStore
    pending: pending.PendingStore
    gate: Callable[[], bool] = lambda: False
    on_success: list[Callable[[], object]] = field(default_factory=list[Callable[[], object]])
    _turn: str = ""
    _failures: int = 0
    _capped: bool = False

    # -- per turn ---------------------------------------------------------------------------------

    def _enter_turn(self, turn_id: str) -> None:
        if turn_id != self._turn:
            self._turn, self._failures, self._capped = turn_id, 0, False

    def _terminal(self) -> dict[str, Any]:
        return {
            "success": False,
            "done": True,
            "error": (
                f"Memory was left unchanged after {self._failures} failed attempts in this turn. "
                "Stop calling the memory tool for now and give the user your answer; the fact can "
                "be saved in a later turn."
            ),
        }

    def _failed(self, body: dict[str, Any]) -> dict[str, Any]:
        self._failures += 1
        if self._failures > MAX_FAILURES_PER_TURN:
            self._capped = True
            return self._terminal()
        return body

    # -- running ----------------------------------------------------------------------------------

    async def run(
        self, args: Mapping[str, Any], *, turn_id: str = "", asker: Asker | None = None
    ) -> dict[str, Any]:
        self._enter_turn(turn_id)
        if self._capped:
            return self._terminal()
        try:
            request = _parse(args, self.store.live_targets)
            body = await self._run(request, asker)
        except _Refusal as refusal:
            return self._failed(refusal.body) if refusal.counted else refusal.body
        if body.get("success"):
            if not body.get("staged"):
                self._failures = 0
                if pending.current_origin() == "foreground":
                    for hook in list(self.on_success):
                        try:
                            hook()
                        except Exception:
                            log.warning("memory success hook failed", exc_info=True)
            return body
        return body

    async def _run(self, request: _Request, asker: Asker | None) -> dict[str, Any]:
        target = request.target
        for op in request.ops:
            if op.action == "add" and not op.content.strip():
                raise _Refusal(_error("add needs content: the entry to save."))
            if op.action in DESTRUCTIVE and not op.old_text.strip():
                if request.batch:
                    raise _Refusal(_error(f"every {op.action} operation needs old_text."))
                entries = await self._blocking(self.store.entries, target)
                raise _Refusal(
                    _error(
                        f"{op.action} needs old_text: a short piece of text copied from the one "
                        "entry to change. Send the call again with old_text taken from "
                        "current_entries.",
                        current_entries=entries,
                        usage=self.store.usage(target).text,
                    )
                )
            if op.action == "patch" and not op.content.strip():
                raise _Refusal(
                    _error(
                        "patch needs content: the text that takes old_text's place. To delete a "
                        "whole entry use remove; to cut text out of an entry, patch a longer span "
                        "that contains it."
                    )
                )
            if op.action in ("add", "patch") and (reason := _scan(op.content)) is not None:
                raise _Refusal(_error(reason), counted=True)

        destructive = any(op.action in DESTRUCTIVE for op in request.ops)
        if pending.unattended_review() and destructive:
            return await self._stage(request, background=True)
        decision = pending.decide(
            "memory",
            gate=self.gate(),
            origin=pending.current_origin(),
            can_ask=asker is not None,
        )
        if decision == "stage":
            return await self._stage(request, background=False)
        if decision == "ask" and asker is not None:
            head, detail = _summary(request)
            answer = await asker(head, detail)
            if answer is None:
                return await self._stage(request, background=False)
            if not answer:
                return _error("the user declined this memory change; nothing was saved.")
        result = await self._blocking(self._apply, target, request.ops)
        return self._answer(request, result)

    async def _blocking[T](self, fn: Callable[..., T], *args: Any) -> T:
        return await run_blocking(fn, *args, label="memory store")

    def _apply(self, target: Target, ops: Sequence[Op]) -> Result:
        return self.store.apply(target, ops, check=_scan)

    # -- staging ----------------------------------------------------------------------------------

    async def _stage(self, request: _Request, *, background: bool) -> dict[str, Any]:
        preview = await self._blocking(
            lambda: self.store.preview(request.target, request.ops, check=_scan)
        )
        if not preview.ok:
            return self._answer(request, preview)
        ops: list[dict[str, Any]] = []
        for op, change in zip(request.ops, preview.changes, strict=True):
            item: dict[str, Any] = {
                "action": op.action,
                "content": op.content,
                "old_text": op.old_text,
            }
            if op.action in DESTRUCTIVE and change.before is not None:
                item["pinned_entry"] = change.before
            ops.append(item)
        head, detail = _summary(request)
        pinned = ops[0].get("pinned_entry") if len(ops) == 1 else None
        payload = {"target": request.target, "operations": ops}
        origin = pending.current_origin()
        try:
            record = await self._blocking(
                lambda: self.pending.stage(
                    "memory",
                    payload,
                    summary=f"{head}: {detail}"[:300],
                    origin=origin,
                    pinned_entry=pinned,
                )
            )
        except Exception:
            log.warning("could not stage a memory write; refusing it", exc_info=True)
            if background:
                return _error(
                    "a background review may not patch or remove memory entries on its own, and "
                    "the change could not be held for approval; it was not applied. 'add' is "
                    "still available."
                )
            return _error("the change could not be held for approval, so it was not saved.")
        if background:
            message = (
                "A background review may not patch or remove saved entries on its own; the "
                "change is held for the user's approval (/memory pending) and is not saved yet."
            )
        else:
            message = pending.staged_message("memory")
        return {"success": True, "staged": True, "pending_id": record.id, "message": message}

    # -- answers ----------------------------------------------------------------------------------

    def _answer(self, request: _Request, result: Result) -> dict[str, Any]:
        target = request.target
        if result.ok:
            body: dict[str, Any] = {"success": True, "target": target}
            if result.wrote:
                body["message"] = (
                    "Saved." if not request.batch else (f"Applied {len(request.ops)} change(s).")
                )
            else:
                body["message"] = "Already saved; not added again."
            dups = [c for c in result.changes if c.duplicate]
            if dups and result.wrote:
                body["note"] = f"{len(dups)} entry(ies) were already saved and not added again."
            replaced = [
                {"operation": c.index, "action": c.action, "before": c.before, "after": c.after}
                for c in result.changes
                if c.before is not None
            ]
            if replaced:
                body["changed_entries"] = replaced
            body["usage"] = self.store.usage(target).text
            body["entry_count"] = len(result.entries)
            body["done"] = True
            return body
        return self._failure(request, result)

    def _failure(self, request: _Request, result: Result) -> dict[str, Any]:
        target = request.target
        where = f"operation {result.op_index}: " if request.batch and result.op_index else ""
        kind = result.kind
        details = result.details
        body: dict[str, Any]
        if kind == "over_limit":
            limit = int(details.get("limit", self.store.limit(target)))
            body = _error(
                f"{STORE_LABELS[target]} holds {int(details.get('used', 0)):,} of {limit:,} chars; "
                f"this change ({int(details.get('added_chars', 0)):,} new chars) would end at "
                f"{int(details.get('new_size', 0)):,}, "
                f"{int(details.get('overflow', 0)):,} over the limit. Send ONE call with "
                "'operations' that removes or shortens (patch) entries "
                "from current_entries and adds the new text in the same call; the limit is "
                "checked on the result. Nothing was written.",
                usage=f"{int(details.get('used', 0)):,}/{limit:,}",
                new_entry_chars=int(details.get("added_chars", 0)),
                overflow_chars=int(details.get("overflow", 0)),
                current_entries=list(result.entries),
            )
        elif kind == "no_match":
            closest = list(details.get("closest_entries") or [])
            body = _error(
                f"{where}{result.message}. Copy old_text exactly from one saved entry and send "
                "the call again"
                + (" (closest_entries lists the likeliest)." if closest else ".")
                + (" Nothing in this batch was applied." if request.batch else ""),
            )
            if closest:
                body["closest_entries"] = closest
        elif kind == "ambiguous":
            body = _error(
                f"{where}{result.message}; use a longer piece of text so only one entry matches."
                + (" Nothing in this batch was applied." if request.batch else ""),
                matches=[str(m)[:120] for m in cast(list[object], details.get("matches") or [])],
            )
        elif kind in ("drift", "unreadable", "lock_refused", "lock_busy"):
            body = _error(result.message)
            if details.get("backup"):
                body["backup"] = details["backup"]
        else:
            body = _error(
                f"{where}{result.message}."
                + (
                    " Nothing in this batch was applied."
                    if request.batch
                    else " Nothing was written."
                )
            )
        body["usage_now"] = self.store.usage(target).text
        if kind in COUNTED:
            return self._failed(body)
        return body


def apply_pending(store: MemoryStore, record: pending.PendingWrite) -> pending.Applied:
    """Replay a staged memory write (``/memory approve``): each patch/remove applies only to its
    pinned entry; if that entry changed since staging, nothing applies and the record stays."""
    payload = record.payload
    target = coerce_target(payload.get("target"))
    raw_ops = payload.get("operations")
    if target is None or not isinstance(raw_ops, list):
        return pending.Applied(False, "the staged record is malformed")
    if not store.enabled(target):
        return pending.Applied(False, f"the {STORE_LABELS[target]} store is turned off")
    ops: list[Op] = []
    for item in cast(list[object], raw_ops):
        if not isinstance(item, Mapping):
            return pending.Applied(False, "the staged record is malformed")
        raw = cast(Mapping[str, Any], item)
        action = _text(raw.get("action"))
        pinned = raw.get("pinned_entry")
        if action in DESTRUCTIVE and not isinstance(pinned, str):
            return pending.Applied(False, "the staged change does not name the entry it targets")
        ops.append(
            Op(
                action,
                _text(raw.get("content")),
                _text(raw.get("old_text")),
                pinned if isinstance(pinned, str) else None,
            )
        )
    result = store.apply(target, ops, check=_scan)
    if not result.ok:
        if result.kind == "stale":
            return pending.Applied(
                False,
                "the entry it targets changed after it was staged, so nothing was applied; "
                "reject it, or make the change again",
            )
        return pending.Applied(False, result.message or result.kind)
    overwritten = tuple(c.before for c in result.changes if c.before is not None)
    return pending.Applied(True, f"saved to {STORE_LABELS[target]}", overwritten)


# -- the loop's executor --------------------------------------------------------------------------


def _broker_asker(state: TurnState) -> Asker | None:
    broker = state.control.broker
    runtime = state.runtime
    interactive = runtime.run_context in (None, "", "interactive")
    if broker is None or runtime.options.platform != "cli" or not interactive:
        return None

    async def ask(summary: str, detail: str) -> bool | None:
        payload = {
            "command": detail or summary,
            "description": f"Save to memory: {summary}",
            "choices": [ApprovalChoice.ONCE.value, ApprovalChoice.DENY.value],
        }
        try:
            answer = await broker.ask("approval", payload, turn_id=state.turn_id)
        except RequestNotAnswered:
            return None
        if not isinstance(answer, ApprovalAnswer):
            return None
        return answer.choice != ApprovalChoice.DENY

    return ask


@dataclass
class MemoryWiring:
    """The session's memory tool plus the inline executor the agent loop takes."""

    tool: MemoryTool

    async def execute(self, call: ToolCall, state: TurnState) -> ToolResult:
        body = await self.tool.run(call.args, turn_id=state.turn_id, asker=_broker_asker(state))
        text = json.dumps(body, ensure_ascii=False)
        return ToolResult(text, is_error=not body.get("success"))


def memory_wiring(scope: Scope, *, store: MemoryStore | None = None) -> MemoryWiring:
    """A session's wiring over ``scope``'s stores and pending area (config read now)."""
    memory = store if store is not None else MemoryStore.for_scope(scope)
    staged = pending.PendingStore(scope.home, scope.clock)
    tool = MemoryTool(memory, staged, gate=lambda: pending.gate_on(scope, "memory"))
    return MemoryWiring(tool)
