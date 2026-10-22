"""One tool round: validate the batch, store the call row, run it, store one result per call.

Order (R01.2, R01.3, R01.14):

1. call ids are made unique against every id already in the session (``<id>_d<n>``, smallest free
   ``n``; never random, so the prompt cache prefix stays stable);
2. unknown names are repaired when an obvious variant exists (``repair_name``);
3. a batch made only of unknown names is a strike: the call row and one error result per call are
   stored and the model is asked again; ``max_invalid_strikes`` strikes in a row end the turn. In a
   mixed batch only the unknown calls get errors; the rest run;
4. arguments must be a JSON object. A malformed batch is asked for again without storing anything,
   ``max_bad_json_retries`` times; after that it is stored with error results so the model can
   recover;
5. ``delegate_task`` calls beyond ``max_child_calls`` are dropped, then repeated
   ``(name, canonical args)`` pairs (first wins); dropped calls never reach the stored row;
6. the assistant call row is stored **before** anything runs; a failed write ends the turn;
7. the batch runs through the planner and executor; each result is built (guardrail stub and
   guidance, spill, turn budget over results not stored yet, untrusted fence — by the tool's
   name, or because the call is tainted, ``core.untrusted``) and stored in call order; then
   buffered steer text is delivered as its own row, and a guardrail stop ends the turn.

Handlers may ask for these context keywords beyond the ``ToolContext`` fields (``extras``):
``broker`` (the session's server-request port), ``cancel`` (the turn's stop event) and
``environments`` (``SessionRuntime.environments``, the terminal backends), ``run_context``
(``SessionRuntime.run_context``: ``interactive``, ``single_query``, ``cron`` or ``unattended``, set
by the host) and ``approval_judge`` (``SessionRuntime.approval_judge``, the smart approval judge,
P08-14) and ``code_kernel_owner`` (``SessionRuntime.code_kernel_owner``, a subagent's kernel
owner, P10-06) and ``aux`` (``SessionRuntime.aux``, the bound auxiliary client), each offered
only when the turn has one; always ``tool_registry``, ``tool_hooks`` and ``enabled_tools`` (the
session's registry, hooks and tool names, for ``execute_code``), ``native_vision`` (the current
route shows the main model images a tool returns, ``providers.route.sees_tool_images``) and
``subagent`` (``SessionRuntime.subagent``: the session is a delegated child) and ``taint``
(``TurnState.taint``: a tool whose result carries outside text marks its call, SEC-4).
``delivery_origin`` (``{platform, chat_id, thread_id}`` of the chat a gateway session answers
in, asked of ``SessionRuntime.delivery_origin`` once per round) is offered when the host knows
one: the cron and board tools subscribe that chat.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from indus.backends.task_overrides import with_workdir
from indus.core import loop_texts as texts
from indus.core import untrusted
from indus.core.exit_reasons import ExitReason, Failure
from indus.core.guardrails import blocked_result, guidance
from indus.core.interrupts import steer_row
from indus.core.response_checks import DISPLAY_PREFILL
from indus.core.tool_exec import Executor, SlotOutcome
from indus.core.tool_plan import PlanCall, plan_segments, traits_for
from indus.core.transcript import TranscriptRow
from indus.core.turn_state import PersistFailed, TurnState
from indus.protocol.events import ToolCompleted, ToolStarted
from indus.providers.route import sees_tool_images
from indus.providers.types import NormalizedResponse
from indus.safety.redaction import redact_arguments
from indus.tools.calls import ToolCall, ToolContext, ToolResult
from indus.tools.dispatch import dispatch
from indus.tools.registry import NO_TRAITS
from indus.tools.spill import enforce_turn_budget, spill_if_needed, text_size

log = logging.getLogger(__name__)

__all__ = ["RoundAction", "handler_extras", "repair_name", "run_tool_round", "unique_ids"]

type RoundAction = Literal["continue", "retry", "end"]

_NAME_CUT = re.compile(r"[\"'<>]")
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")
_SUFFIXES = ("_tool", "-tool", "tool")


# -- names and ids -------------------------------------------------------------------------------


def _norm(name: str) -> str:
    return name.lower().replace("-", "_").replace(" ", "_")


def _drop_suffix(name: str) -> str | None:
    lowered = name.lower()
    for suffix in _SUFFIXES:
        if lowered.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)].rstrip("_-")
    return None


def repair_name(name: str, valid: frozenset[str]) -> str | None:
    """The valid tool ``name`` most likely meant, or ``None``.

    Tries: text before a stray quote or angle bracket, lower case, ``-``/space as ``_``,
    CamelCase as snake_case, and a trailing ``tool`` suffix removed (up to twice).
    """
    cut = _NAME_CUT.search(name)
    if cut is not None and cut.start() > 0:
        name = name[: cut.start()]
    name = name.strip()
    if not name:
        return None
    seen: list[str] = []
    frontier = [name]
    for _ in range(3):
        grown: list[str] = []
        for item in frontier:
            for variant in (item, item.lower(), _norm(item), _CAMEL.sub("_", item).lower()):
                variant = _norm(variant)
                if variant and variant not in seen:
                    seen.append(variant)
                    if variant in valid:
                        return variant
                    stripped = _drop_suffix(variant)
                    if stripped:
                        grown.append(stripped)
            stripped = _drop_suffix(item)
            if stripped:
                grown.append(stripped)
        frontier = grown
        if not frontier:
            break
    return None


def _free_id(base: str, taken: set[str]) -> str:
    n = 1
    while f"{base}_d{n}" in taken:
        n += 1
    return f"{base}_d{n}"


def unique_ids(ids: Sequence[str], taken: set[str], *, fallback: str = "call") -> list[str]:
    """``ids`` with repeats (within the batch or against ``taken``) renamed ``<id>_d<n>``."""
    out: list[str] = []
    used = set(taken)
    for position, raw in enumerate(ids):
        ident = raw.strip() or f"{fallback}_{position}"
        if ident in used:
            ident = _free_id(ident, used)
        used.add(ident)
        out.append(ident)
    return out


def _session_call_ids(rows: Sequence[TranscriptRow]) -> set[str]:
    return {cid for row in rows if row.role == "assistant" for cid in row.call_ids() if cid}


# -- the batch -------------------------------------------------------------------------------------


@dataclass
class _Call:
    id: str
    name: str
    raw: str
    args: dict[str, Any] | None = None
    error: str | None = None
    known: bool = True

    def wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw},
        }


def _parse(call: _Call) -> None:
    text = call.raw.strip()
    if not text:
        call.raw, call.args = "{}", {}
        return
    try:
        value = json.loads(text)
    except ValueError as exc:
        call.error = str(exc)
        return
    if not isinstance(value, dict):
        call.error = f"expected an object, got {type(value).__name__}"
        return
    call.args = cast(dict[str, Any], value)


def _canonical(call: _Call) -> str:
    if call.args is None:
        return call.raw
    return json.dumps(call.args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _unknown_text(name: str, valid: frozenset[str]) -> str:
    if not name.strip():
        return texts.BLANK_TOOL_NAME
    return texts.UNKNOWN_TOOL.format(name=name[:80], names=", ".join(sorted(valid)))


def _call_row(
    calls: Sequence[_Call], content: str | None, response: NormalizedResponse
) -> TranscriptRow:
    return TranscriptRow(
        role="assistant",
        content=content or None,
        tool_calls=[c.wire() for c in calls],
        reasoning=response.reasoning,
        reasoning_details=[dict(d) for d in response.reasoning_details] or None,
        finish_reason="tool_calls",
    )


def _result_row(call: _Call, content: Any) -> TranscriptRow:
    return TranscriptRow(role="tool", name=call.name, tool_call_id=call.id, content=content)


async def _store_rows_or_end(state: TurnState) -> bool:
    try:
        await state.flush()
    except PersistFailed as exc:
        state.end(
            ExitReason.PERSISTENCE_FAILED,
            final_text=texts.PERSIST_FAILED_REPLY,
            failure=Failure("persistence", True, detail=str(exc)),
        )
        return False
    return True


async def _answer_all(
    state: TurnState, calls: Sequence[_Call], text_for: Callable[[_Call], str]
) -> bool:
    for call in calls:
        state.add(_result_row(call, ToolResult.error(text_for(call)).content))
    return await _store_rows_or_end(state)


def _drop_trailing_prefill(state: TurnState) -> None:
    while state.rows and state.rows[-1].scaffold and state.rows[-1].display_kind == DISPLAY_PREFILL:
        state.rows.pop()


# -- entry point -----------------------------------------------------------------------------------


async def run_tool_round(
    state: TurnState, response: NormalizedResponse, visible_text: str
) -> RoundAction:
    """Handle the tool calls of ``response`` (see the module notes)."""
    runtime, options = state.runtime, state.options
    valid = runtime.tool_names()
    ids = unique_ids([c.id for c in response.tool_calls], _session_call_ids(state.rows))
    calls = [
        _Call(ident, c.name, c.arguments) for ident, c in zip(ids, response.tool_calls, strict=True)
    ]
    for call in calls:
        if call.name not in valid:
            fixed = repair_name(call.name, valid)
            if fixed is not None:
                log.info("tool name %r repaired to %r", call.name[:80], fixed)
                call.name = fixed
            else:
                call.known = False

    unknown = [c for c in calls if not c.known]
    _drop_trailing_prefill(state)
    if unknown and len(unknown) == len(calls):
        state.invalid_strikes += 1
        log.warning(
            "model called unknown tool %r (strike %d/%d)",
            unknown[0].name[:80],
            state.invalid_strikes,
            options.max_invalid_strikes,
        )
        if state.invalid_strikes >= options.max_invalid_strikes:
            state.end(
                ExitReason.INVALID_TOOL_STRIKES,
                final_text=texts.INVALID_TOOLS_REPLY.format(name=unknown[0].name[:80] or "?"),
                failure=Failure("invalid_tool", True),
            )
            return "end"
        state.add(_call_row(calls, visible_text, response))
        ok = await _store_rows_or_end(state) and await _answer_all(
            state, calls, lambda c: _unknown_text(c.name, valid)
        )
        return "continue" if ok else "end"
    state.invalid_strikes = 0

    for call in calls:
        if call.known:
            _parse(call)
    bad = [c for c in calls if c.known and c.error is not None]
    if bad:
        state.bad_json_retries += 1
        if state.bad_json_retries <= options.max_bad_json_retries:
            log.warning(
                "tool %s sent arguments that are not a JSON object; asking again (%d/%d)",
                bad[0].name,
                state.bad_json_retries,
                options.max_bad_json_retries,
            )
            return "retry"
        state.bad_json_retries = 0
        state.add(_call_row(calls, visible_text, response))

        def recovery(call: _Call) -> str:
            if not call.known:
                return _unknown_text(call.name, valid)
            if call.error is not None:
                return texts.BAD_JSON_ARGS.format(error=call.error)
            return texts.SIBLING_OF_BAD_JSON

        ok = await _store_rows_or_end(state) and await _answer_all(state, calls, recovery)
        return "continue" if ok else "end"
    state.bad_json_retries = 0

    calls = _cap_and_dedupe(calls, options.max_child_calls)
    state.add(_call_row(calls, visible_text, response))
    try:
        await state.flush()
    except PersistFailed as exc:
        for call in calls:
            state.add(_result_row(call, ToolResult.error(texts.PERSIST_FAILED_RESULT).content))
        state.end(
            ExitReason.PERSISTENCE_FAILED,
            final_text=texts.PERSIST_FAILED_REPLY,
            failure=Failure("persistence", True, detail=str(exc)),
        )
        return "end"

    await _execute(state, calls, valid)
    if state.ended:
        return "end"

    steer = None if state.control.interrupted else state.control.drain_steer()
    if steer:
        state.add(steer_row(steer))
        if not await _store_rows_or_end(state):
            return "end"

    halt = state.guard.halt
    if halt is not None:
        reply = texts.GUARD_HALT_REPLY.format(tool=halt.tool or "a tool", count=halt.count)
        state.add(TranscriptRow(role="assistant", content=reply, finish_reason="guardrail"))
        if await _store_rows_or_end(state):
            state.end(ExitReason.GUARDRAIL_HALT, final_text=reply)
        return "end"
    state.checks.post_tool_nudged = False
    state.checks.tool_retries = 0
    return "continue"


def _cap_and_dedupe(calls: list[_Call], max_children: int) -> list[_Call]:
    kept: list[_Call] = []
    children = 0
    seen: set[tuple[str, str]] = set()
    for call in calls:
        if call.name == "delegate_task" and call.known:
            if children >= max_children:
                log.warning(
                    "dropped a delegate_task call over the per-batch limit of %d", max_children
                )
                continue
            children += 1
        key = (call.name, _canonical(call))
        if key in seen:
            log.warning("dropped a repeated %s call with the same arguments", call.name)
            continue
        seen.add(key)
        kept.append(call)
    return kept


# -- execution -------------------------------------------------------------------------------------


async def _execute(state: TurnState, calls: list[_Call], valid: frozenset[str]) -> None:
    runtime, options = state.runtime, state.options
    entries = [runtime.registry.get(c.name, runtime.scope) for c in calls]
    traits = [
        traits_for(c.name, e) if c.known else NO_TRAITS for c, e in zip(calls, entries, strict=True)
    ]
    plan = [
        PlanCall(
            name=c.name,
            args=c.args,
            traits=t,
            deadline_s=e.timeout_s if e is not None else None,
            known=c.known and (e is not None or c.name in runtime.inline or t != NO_TRAITS),
        )
        for c, e, t in zip(calls, entries, traits, strict=True)
    ]
    cwd = runtime.prompt_context.cwd
    limits = options.exec_limits
    segments = plan_segments(plan, cwd=cwd, batch_deadline_s=limits.batch_deadline_s)
    budget = options.budget()
    extras = handler_extras(state)
    origin = await _delivery_origin(state)
    if origin is not None:
        extras["delivery_origin"] = origin
    scope = runtime.scope
    if scope is not None and runtime.workdir is not None:
        scope = with_workdir(scope, runtime.workdir)  # the session's own folder (W47-b)
    ctx = ToolContext(
        scope=scope,
        task_id=state.session_id,
        session_id=state.session_id,
        turn_id=state.turn_id,
        extras=extras,
    )

    async def run_one(index: int) -> ToolResult:
        call = calls[index]
        if not call.known:
            return ToolResult.error(_unknown_text(call.name, valid), error_type="unknown_tool")
        args = call.args or {}
        decision = state.guard.before(call.name, args, read_only=traits[index].read_only)
        if not decision.runs:
            return ToolResult(blocked_result(decision), is_error=True, error_type="guardrail")
        inline = runtime.inline.get(call.name)
        if inline is not None:
            return await inline(ToolCall(call.name, args, call.id), state)
        return await dispatch(
            ToolCall(call.name, args, call.id),
            ctx,
            registry=runtime.registry,
            hooks=runtime.hooks,
            inline=frozenset(runtime.inline),
        )

    def call_deadline(index: int) -> float | None:
        if traits[index].interactive or calls[index].name == "delegate_task":
            return None
        base = limits.call_deadline_s
        own = plan[index].deadline_s
        if base is None or own is None:
            return base
        return max(base, own)

    def on_start(index: int) -> None:
        call = calls[index]
        state.emit(
            ToolStarted,
            turn_id=state.turn_id,
            call_id=call.id,
            name=call.name,
            # Every surface shows these; the tool itself runs with the raw arguments.
            arguments=redact_arguments(call.args or {}, scope=runtime.scope),
        )

    executor = Executor(
        clock=state.clock,
        run_one=run_one,
        names=[c.name for c in calls],
        stop=state.control.stop,
        limits=limits,
        call_deadline=call_deadline,
        human_wait=state.control.human_wait_seconds,
        touch=state.touch,
        on_start=on_start,
    )
    state.control.phase = "tools"
    try:
        for segment in segments:
            if segment.kind == "parallel" and not state.control.stop.is_set():
                outcomes = await executor.run_parallel(segment.indices)
                if not await _commit(state, calls, traits, budget, outcomes):
                    _close_unanswered(state, calls)
                    return
                continue
            for index in segment.indices:
                outcome = await executor.run_single(index)
                if not await _commit(state, calls, traits, budget, [outcome]):
                    _close_unanswered(state, calls)
                    return
    finally:
        state.control.phase = "model"


_ORIGIN_KEYS = ("platform", "chat_id", "thread_id")


async def _delivery_origin(state: TurnState) -> dict[str, str] | None:
    """The session's chat as ``{platform, chat_id, thread_id?}``, or None (see the notes)."""
    lookup = state.runtime.delivery_origin
    if lookup is None:
        return None
    try:
        found = await lookup(state.session_id)
    except Exception:
        log.debug("the delivery origin of %s is unknown", state.session_id, exc_info=True)
        return None
    if not found:
        return None
    origin = {k: str(found[k]) for k in _ORIGIN_KEYS if found.get(k) not in (None, "")}
    return origin if origin.get("platform") and origin.get("chat_id") else None


def handler_extras(state: TurnState) -> dict[str, Any]:
    """The extra keywords a handler may ask for (see the module notes)."""
    extras: dict[str, Any] = {"cancel": state.control.stop}
    if state.control.broker is not None:
        extras["broker"] = state.control.broker
    if state.runtime.environments is not None:
        extras["environments"] = state.runtime.environments
    if state.runtime.run_context is not None:
        extras["run_context"] = state.runtime.run_context
    if state.runtime.approval_judge is not None:
        extras["approval_judge"] = state.runtime.approval_judge
    # execute_code (P10-08) dispatches in-script calls through the same registry and hooks.
    extras["tool_registry"] = state.runtime.registry
    extras["tool_hooks"] = state.runtime.hooks
    extras["enabled_tools"] = state.runtime.tool_names()
    if state.runtime.code_kernel_owner is not None:
        extras["code_kernel_owner"] = state.runtime.code_kernel_owner
    # Browser and media tools (P10-12..17): the aux client, vision through the main model, child.
    if state.runtime.aux is not None:
        extras["aux"] = state.runtime.aux
    extras["native_vision"] = sees_tool_images(state.runtime.route)
    extras["subagent"] = state.runtime.subagent
    # SEC-4: a tool that passes outside text on under its own name (execute_code) marks its call.
    extras["taint"] = state.taint
    return extras


def _content_of(result: ToolResult) -> Any:
    if isinstance(result.content, str):
        return result.content
    parts = result.content.get("content")
    return list(cast(list[Any], parts)) if isinstance(parts, list) else result.text


def _append_text(content: Any, suffix: str) -> Any:
    if not suffix:
        return content
    if isinstance(content, str):
        return content + suffix
    return [*cast(list[Any], content), {"type": "text", "text": suffix.strip()}]


async def _commit(
    state: TurnState,
    calls: Sequence[_Call],
    traits: Sequence[Any],
    budget: Any,
    outcomes: Sequence[SlotOutcome],
) -> bool:
    """Build, budget, fence and store the result rows for ``outcomes`` (call order)."""
    runtime = state.runtime
    built: list[tuple[SlotOutcome, dict[str, Any]]] = []
    for outcome in outcomes:
        call = calls[outcome.index]
        result = outcome.result
        content = _content_of(result)
        failed = result.is_error or outcome.status != "ok"
        decision, stub = state.guard.after(
            call.name,
            call.args,
            result.text if isinstance(result.content, str) else None,
            failed=failed,
            read_only=traits[outcome.index].read_only,
            mutating=traits[outcome.index].mutating,
            call_id=call.id,
        )
        if stub is not None and isinstance(content, str):
            content = stub
        content = _append_text(content, guidance(decision))
        if isinstance(content, str):
            entry = runtime.registry.get(call.name, runtime.scope)
            content = spill_if_needed(
                content,
                call.name,
                call.id,
                budget=budget,
                sink=runtime.spill_sink,
                registered_cap=entry.max_result_chars if entry is not None else None,
            )
        built.append((outcome, {"role": "tool", "tool_call_id": call.id, "content": content}))
    _apply_turn_budget(state, budget, [m for _, m in built])
    for outcome, message in built:
        call = calls[outcome.index]
        tainted = call.id in state.taint
        content = untrusted.fence(call.name, message["content"], tainted=tainted)
        if tainted or (outcome.started and untrusted.is_untrusted(call.name)):
            state.taint.read = True
        state.turn_tool_chars += text_size(content)
        state.add(_result_row(call, content))
        if runtime.run_budget is not None:
            runtime.run_budget.wrap_up(state.rows)  # P08-16: once, on this unstored row
        try:
            await state.flush()
        except PersistFailed as exc:
            state.end(
                ExitReason.PERSISTENCE_FAILED,
                final_text=texts.PERSIST_FAILED_REPLY,
                failure=Failure("persistence", True, detail=str(exc)),
            )
            return False
        shown = content if isinstance(content, str) else outcome.result.text
        state.emit(
            ToolCompleted,
            turn_id=state.turn_id,
            call_id=call.id,
            name=call.name,
            ok=outcome.ok,
            duration_ms=max(0, int(outcome.duration_s * 1000)),
            result=shown,
        )
    return True


def _apply_turn_budget(state: TurnState, budget: Any, messages: list[dict[str, Any]]) -> None:
    """Spill the largest new results first until this turn's tool text fits ``per_turn``."""
    room = max(0, budget.per_turn - state.turn_tool_chars)
    if sum(text_size(m["content"]) for m in messages) <= room:
        return
    fitted = enforce_turn_budget(
        messages, budget=replace(budget, per_turn=room), sink=state.runtime.spill_sink
    )
    for message, new in zip(messages, fitted, strict=True):
        message["content"] = new["content"]


def _close_unanswered(state: TurnState, calls: Sequence[_Call]) -> None:
    """After a failed store: in-memory results for calls that have none (not stored)."""
    answered = {r.tool_call_id for r in state.rows if r.role == "tool"}
    for call in calls:
        if call.id not in answered:
            row = _result_row(call, ToolResult.error(texts.PERSIST_FAILED_RESULT).content)
            row.row_id = -1  # never stored: the store is failing
            state.rows.append(row)
