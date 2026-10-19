"""The turn loop (A1, R01.18).

``run_turn(state)`` runs the prologue, then iterations while
``(api_calls < max_iterations and budget.remaining > 0) or grace``, then the exit funnel
(``finalize_turn``), which every exit passes — success, failure, interrupt, exhaustion, a bug.

One iteration: interrupt check → ``api_calls`` and one budget unit go up → pre-request steer drain
→ request (system prompt + sanitized wire copy + tool array) → one provider call through the
``ModelCaller`` (P03 streaming and retries) → the answer: a cut-off one goes to the length ladder,
tool calls to the tool round, text to the empty ladder / intent-ack check / final answer (P04-17
verdicts are applied here).

Context (P08): before each request the pressure gate (post-tool right after a tool round) may
compress the transcript; the request's rows are announced to the context engine, whose meter
reads the answer's usage from the session's usage ledger (one path, ``core.usage``); a
provider-proven overflow (413, prompt too long, output cap too large) gets one bounded recovery
(``core.overflow``) instead of ending the turn.

Fallback (P08-13): a provider failure that outlived P03's retries and the credential pool, a
content-filter refusal with nothing shown, or the empty-answer ladder giving up moves the session
to the next fallback route (``core.fallback``) and the request is rebuilt for it.

Mixture of agents (P10-21): a turn the host armed (``core.moa``) asks the preset's reference
models before each request and the advice rides on the request's last user message.

Restarts that produced no answer (a redirect, a route change, a fallback, an overflow recovery)
give the iteration back and bump ``restart_count``; more than ``max_restarts`` end the turn. When
the budget runs out, one toolless grace request asks for a summary (retried once when empty, think
blocks removed), else a fixed limit reply is given.

An exception escaping an iteration counts against ``max_outer_errors``; one raised only inside
local processing modules (repair, wire) is a deterministic bug and ends the turn at once. Open
tool calls get error results first, so alternation holds on every path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from indus.core import loop_texts as texts
from indus.core import moa
from indus.core.compression.engine import expect_usage, run_gate
from indus.core.exit_reasons import ExitReason, Failure
from indus.core.fallback import fall_back
from indus.core.finalize import close_open_calls, finalize_turn
from indus.core.interrupts import redirect_rows, steer_row
from indus.core.overflow import is_overflow, recover_overflow
from indus.core.prologue import run_prologue
from indus.core.response_checks import (
    COPY,
    EmptyContext,
    LengthLimits,
    Verdict,
    check_empty,
    check_intent_ack,
    check_length,
    finish_continuation,
    is_repetition_dominated,
    scrubber_for,
)
from indus.core.tool_round import run_tool_round
from indus.core.transcript import TranscriptRow
from indus.core.turn_state import PersistFailed, TurnResult, TurnState
from indus.core.wire import prepare_wire
from indus.foundation.errors import ProviderError
from indus.protocol.events import (
    MessageDelta,
    Notice,
    NoticeLevel,
    ReasoningDelta,
    Status,
    StatusKind,
)
from indus.providers.classify import ClassifiedError
from indus.providers.retry import RetryFailed, RetryWait
from indus.providers.streaming import CallResult, RouteChanged
from indus.providers.types import ChatRequest, NormalizedResponse, TextDelta
from indus.providers.types import ReasoningDelta as ReasoningEvent

log = logging.getLogger(__name__)

__all__ = ["LOCAL_MODULES", "run_turn"]

# A failure raised only in these modules (never through a provider module) is a deterministic bug.
LOCAL_MODULES = frozenset({"indus.core.repair", "indus.core.wire", "indus.core.transcript"})
_PROVIDER_PREFIX = "indus.providers"


async def run_turn(state: TurnState) -> TurnResult:
    """Run one turn to its end and return the result (see the module notes)."""
    current = asyncio.current_task()
    try:
        await run_prologue(state)
        if not state.ended:
            state.moa = moa.claim(state)
            await _iterate(state)
    except PersistFailed as exc:
        _end_persist(state, exc)
    except asyncio.CancelledError:
        if current is not None:
            current.uncancel()
        log.warning(
            "turn %s of session %s was cancelled from outside", state.turn_id, state.session_id
        )
        state.control.interrupted = True
        state.control.by_system = True
        state.control.stop.set()
        state.end(ExitReason.INTERRUPTED_BY_SYSTEM)
    except Exception as exc:
        log.error("turn %s failed outside an iteration", state.turn_id, exc_info=exc)
        state.end(
            ExitReason.PROVIDER_FAILED,
            final_text=texts.LOOP_ERROR_REPLY.format(detail=_short(exc)),
            failure=Failure("loop_error", False, detail=_short(exc)),
        )
    return await finalize_turn(state)


# -- the loop ------------------------------------------------------------------------------------


def _may_iterate(state: TurnState) -> bool:
    cap = state.options.max_iterations
    under_cap = cap == 0 or state.api_calls < cap
    return under_cap and state.budget.remaining > 0


def _interrupted_exit(state: TurnState) -> None:
    reason = (
        ExitReason.INTERRUPTED_BY_SYSTEM
        if state.control.by_system
        else ExitReason.INTERRUPTED_BY_USER
    )
    state.end(reason)


async def _iterate(state: TurnState) -> None:
    while not state.ended and _may_iterate(state):
        if state.control.interrupted:
            _interrupted_exit(state)
            return
        try:
            await _iteration(state)
        except PersistFailed as exc:
            _end_persist(state, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _on_outer_error(state, exc)
    if not state.ended:
        if state.options.grace:
            await _grace(state)
        else:
            _no_grace(state)


def _no_grace(state: TurnState) -> None:
    cap = state.options.max_iterations
    reason = (
        ExitReason.MAX_ITERATIONS if cap and state.api_calls >= cap else ExitReason.BUDGET_EXHAUSTED
    )
    limit = str(cap) if cap else texts.LIMIT_UNLIMITED
    state.end(reason, final_text=texts.LIMIT_REPLY.format(limit=limit))


async def _iteration(state: TurnState) -> None:
    state.api_calls += 1
    if not state.budget.consume():
        state.api_calls -= 1
        return
    state.touch(f"model call {state.api_calls}")
    await _drain_steer_before_request(state)
    if state.runtime.context is not None:
        await run_gate(state, "post_tool" if state.tail_role() == "tool" else "pressure")
        if state.ended:
            return
    request = _request(state, with_tools=True)
    if state.moa is not None:
        request = await state.moa.before_request(state, request)
        if state.control.interrupted:
            _interrupted_exit(state)
            return
    expect_usage(state)  # the answer's usage reaches the engine through the ledger (P08-08)
    try:
        outcome = await _call_model(state, request)
    finally:
        expect_usage(state, done=True)
    if isinstance(outcome, ClassifiedError):
        if await recover_overflow(state, outcome):
            _refund(state, "overflow recovery")
        elif not state.ended:
            _end_provider(state, outcome)
        return
    if state.ended:
        return
    if outcome is None:
        if state.control.redirect_pending:
            await _apply_redirect(state)
            _refund(state, "redirect")
        else:
            _interrupted_exit(state)
        return
    if isinstance(outcome, RouteChanged):
        _refund(state, "route change")
        return
    await _handle_answer(state, outcome)


def _refund(state: TurnState, why: str) -> None:
    state.api_calls -= 1
    state.budget.refund()
    state.restart_count += 1
    if state.restart_count > state.options.max_restarts:
        log.warning(
            "turn %s restarted more than %d times (%s)",
            state.turn_id,
            state.options.max_restarts,
            why,
        )
        pending = state.control.take_redirect()
        if pending:
            state.control.steer(pending)
        state.end(
            ExitReason.PROVIDER_FAILED,
            final_text=texts.RESTART_LIMIT_REPLY,
            failure=Failure("restart_limit", True),
        )


async def _drain_steer_before_request(state: TurnState) -> None:
    """Steer text that arrived after the last batch lands right after its tool results."""
    if state.tail_role() != "tool" or not state.control.steer_pending:
        return
    text = state.control.drain_steer()
    if text:
        state.add(steer_row(text))
        await state.flush()


async def _apply_redirect(state: TurnState) -> None:
    text = state.control.take_redirect()
    if not text:
        return
    steer = state.control.drain_steer()
    if steer:
        state.add(steer_row(steer))
    state.add(*redirect_rows(state.streamed_text, text, tail_role=state.tail_role()))
    state.streamed_text = ""
    await state.flush()


# -- requests and calls ----------------------------------------------------------------------------


def _request(state: TurnState, *, with_tools: bool) -> ChatRequest:
    runtime, options = state.runtime, state.options
    wire = prepare_wire(state.rows, runtime.route, heals=runtime.heals, memo=runtime.memo)
    if runtime.heals.take_notice():
        state.emit(Notice, level=NoticeLevel.INFO, text=COPY["heal_notice"], key="wire_heal")
    messages: list[dict[str, Any]] = []
    if state.system_prompt:
        messages.append({"role": "system", "content": state.system_prompt})
    messages.extend(wire)
    cap = state.output_cap or options.max_tokens
    effort = None if state.reasoning_off else options.reasoning_effort
    state.output_cap = None
    state.reasoning_off = False
    state.last_cap = cap
    return ChatRequest(
        model=runtime.caller.model,
        messages=messages,
        tools=list(runtime.tools) if with_tools and runtime.tools else (),
        max_tokens=cap,
        reasoning_effort=effort,
    )


async def _call_model(
    state: TurnState, request: ChatRequest, *, soft: bool = False
) -> CallResult | RouteChanged | ClassifiedError | None:
    """One provider call. ``None`` when it was cancelled by the control (interrupt or redirect)
    or, with ``soft``, when it failed; a hard failure ends the turn ``provider_failed``, except a
    provider-proven overflow while a context engine is present: its verdict is returned for the
    overflow recovery to decide."""
    control = state.control
    control.phase = "model"
    state.streamed_text = ""
    state.scrubber.reset()

    def on_event(event: Any) -> None:
        if isinstance(event, TextDelta):
            visible, hidden = state.scrubber.feed(event.text)
            _shown(state, visible, hidden)
        elif isinstance(event, ReasoningEvent) and event.text:
            state.emit(ReasoningDelta, turn_id=state.turn_id, text=event.text)

    def on_wait(wait: RetryWait) -> None:
        state.emit(Status, kind=StatusKind.RETRYING, text=wait.describe())

    task: asyncio.Future[Any] = asyncio.ensure_future(
        state.runtime.caller.call(request, on_event=on_event, on_wait=on_wait)
    )
    control.bind_model_call(task)
    try:
        return await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        return None
    except RetryFailed as exc:
        verdict = exc.classified
        if _provider_cancelled(exc.error):
            return _end_cancelled(state, soft)
        if soft:
            log.warning("model call failed (%s); no retry here", verdict.reason)
            return None
        if state.runtime.context is not None and is_overflow(verdict):
            return verdict
        if await _try_fallback(state, verdict):
            return RouteChanged(None, 0, 0)
        _end_provider(state, verdict)
        return None
    except ProviderError as exc:
        if _provider_cancelled(exc):
            return _end_cancelled(state, soft)
        if soft:
            return None
        state.end(
            ExitReason.PROVIDER_FAILED,
            final_text=texts.PROVIDER_FAILED_REPLY.format(detail=_short(exc)),
            failure=Failure(exc.code or "provider", True, detail=_short(exc)),
        )
        return None
    finally:
        control.bind_model_call(None)
        visible, hidden = state.scrubber.flush()
        _shown(state, visible, hidden)


async def _try_fallback(
    state: TurnState, verdict: ClassifiedError | None, reason: str | None = None
) -> bool:
    """True when the request should be rebuilt and sent again: the session moved to a fallback
    route, or waited out a short rate limit on the main one."""
    decision = await fall_back(state, verdict, reason)
    if decision.action == "wait":
        await _wait(state, decision.seconds)
        return not state.control.interrupted
    return decision.action == "switched"


def _provider_cancelled(error: BaseException | None) -> bool:
    """The provider cancelled the response itself (Responses ``status: cancelled``, P08-09): the
    turn was interrupted, it did not fail."""
    return bool(getattr(error, "interrupted", False))


def _end_cancelled(state: TurnState, soft: bool) -> None:
    if soft:
        return None
    log.info("the provider cancelled the response; turn %s ends interrupted", state.turn_id)
    state.control.interrupted = True
    state.control.by_system = True
    state.end(ExitReason.INTERRUPTED_BY_SYSTEM)
    return None


def _end_provider(state: TurnState, verdict: ClassifiedError) -> None:
    state.end(
        ExitReason.PROVIDER_FAILED,
        final_text=texts.PROVIDER_FAILED_REPLY.format(detail=verdict.message or verdict.reason),
        failure=Failure(verdict.reason, verdict.retryable, verdict.reset_at, verdict.message),
    )


def _shown(state: TurnState, visible: str, hidden: str) -> None:
    if visible:
        state.streamed_text += visible
        state.emit(MessageDelta, turn_id=state.turn_id, text=visible)
    if hidden:
        state.emit(ReasoningDelta, turn_id=state.turn_id, text=hidden)


def _cleaned(state: TurnState, result: CallResult) -> tuple[CallResult, str]:
    """The answer with inline think blocks moved from the text into the reasoning."""
    response = result.response
    scrubber = scrubber_for(state.runtime.profile)
    visible, inner = scrubber.feed(response.content or "")
    tail_visible, tail_inner = scrubber.flush()
    visible += tail_visible
    inner += tail_inner
    reasoning = response.reasoning
    if inner:
        reasoning = f"{reasoning}\n{inner}" if reasoning else inner
    cleaned = replace(response, content=visible or None, reasoning=reasoning)
    return replace(result, response=cleaned), visible


# -- answers ---------------------------------------------------------------------------------------


async def _handle_answer(state: TurnState, raw: CallResult) -> None:
    result, visible = _cleaned(state, raw)
    response = result.response
    options = state.options
    refused = response.finish_reason == "content_filter" and not response.tool_calls
    if refused and not visible.strip() and await _try_fallback(state, None, "content_filter"):
        _refund(state, "fallback")
        return
    if result.stub_terminal or response.finish_reason == "length":
        limits = LengthLimits(
            context_window=options.context_length,
            requested_cap=state.last_cap,
            base_cap=options.max_tokens,
            model_output_limit=options.model_output_limit,
        )
        await _apply(state, check_length(result, state.checks, limits), response)
        return
    if response.tool_calls:
        await run_tool_round(state, response, visible)
        return
    if not visible.strip():
        ctx = EmptyContext(
            after_tool_round=state.tail_role() == "tool",
            model=state.runtime.caller.model,
            rng=state.rng,
        )
        await _apply(state, check_empty(result, state.checks, ctx), response)
        return
    tools_available = bool(state.runtime.tools or state.runtime.inline)
    ack = check_intent_ack(
        visible, state.checks, tools_available=tools_available, enabled=options.intent_ack
    )
    if ack is not None:
        await _apply(state, ack, response)
        return
    final = finish_continuation(state.checks, visible) if state.checks.fragments else visible
    final = final.strip()
    state.drop_scaffold()
    if is_repetition_dominated(final):
        state.end(
            ExitReason.PROVIDER_FAILED,
            final_text=COPY["repetition"],
            failure=Failure("repetition", True),
        )
        return
    state.add(
        TranscriptRow(
            role="assistant",
            content=final,
            reasoning=response.reasoning,
            reasoning_details=[dict(d) for d in response.reasoning_details] or None,
            finish_reason=response.finish_reason,
        )
    )
    await state.flush()
    state.end(ExitReason.COMPLETED, final_text=final)


async def _apply(state: TurnState, verdict: Verdict, response: NormalizedResponse) -> None:
    """Carry out a P04-17 verdict."""
    gave_up = verdict.action == "end" and verdict.failure_reason == "empty_response"
    if gave_up and await _try_fallback(state, None, "empty_response"):
        state.drop_scaffold()
        _refund(state, "fallback")
        return
    if verdict.notice:
        state.emit(Notice, level=NoticeLevel.WARNING, text=verdict.notice)
    if verdict.drop_scaffold:
        state.drop_scaffold()
    state.add(*(row.clone() for row in verdict.append))
    if verdict.action == "accept":
        final = (verdict.final_text or "").strip()
        state.add(TranscriptRow(role="assistant", content=final, finish_reason="partial"))
        await state.flush()
        state.end(ExitReason.COMPLETED, final_text=final)
    elif verdict.action == "continue":
        state.reasoning_off = verdict.reasoning_off
        await state.flush()
    elif verdict.action == "retry":
        if verdict.output_cap is not None:
            state.output_cap = verdict.output_cap
        await state.flush()
        if verdict.wait > 0:
            await _wait(state, verdict.wait)
    else:
        await state.flush()
        reason = verdict.failure_reason or "provider"
        exit_reason = (
            ExitReason.COMPRESSION_EXHAUSTED
            if reason == "context_overflow"
            else ExitReason.PROVIDER_FAILED
        )
        state.end(
            exit_reason,
            final_text=verdict.final_text,
            failure=Failure(reason, verdict.retryable),
        )
    log.debug(
        "verdict %s/%s applied (finish=%s)", verdict.action, verdict.rung, response.finish_reason
    )


async def _wait(state: TurnState, seconds: float) -> None:
    """Wait on the clock; an interrupt ends the wait early."""
    stop = state.control.stop
    sleeper = asyncio.ensure_future(state.clock.sleep(seconds))
    stopper = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        sleeper.cancel()
        stopper.cancel()


# -- exhaustion ------------------------------------------------------------------------------------


async def _grace(state: TurnState) -> None:
    """One toolless summary request after the budget ran out (retried once when empty)."""
    cap = state.options.max_iterations
    reason = (
        ExitReason.MAX_ITERATIONS if cap and state.api_calls >= cap else ExitReason.BUDGET_EXHAUSTED
    )
    log.warning(
        "turn %s used its %d model calls; asking for a summary", state.turn_id, state.api_calls
    )
    state.grace = True
    state.add(TranscriptRow(role="user", content=texts.GRACE_NUDGE, scaffold=True))
    summary = ""
    for _ in range(2):
        if state.control.interrupted:
            break
        state.api_calls += 1
        outcome = await _call_model(state, _request(state, with_tools=False), soft=True)
        if state.control.interrupted or state.ended:
            break
        if not isinstance(outcome, CallResult):
            continue
        _, visible = _cleaned(state, outcome)
        summary = visible.strip()
        if summary:
            break
    state.grace = False
    state.grace_used = True
    state.drop_scaffold()
    if state.control.interrupted:
        _interrupted_exit(state)
        return
    if state.ended:
        return
    if summary:
        state.add(TranscriptRow(role="assistant", content=summary, finish_reason="stop"))
        await state.flush()
        state.end(reason, final_text=summary)
        return
    limit = str(cap) if cap else texts.LIMIT_UNLIMITED
    state.end(reason, final_text=texts.LIMIT_REPLY.format(limit=limit))


# -- failures --------------------------------------------------------------------------------------


def _short(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 300 else text[:300] + "…"


def _end_persist(state: TurnState, exc: Exception) -> None:
    state.end(
        ExitReason.PERSISTENCE_FAILED,
        final_text=texts.PERSIST_FAILED_REPLY,
        failure=Failure("persistence", True, detail=str(exc)),
    )


def raised_locally(exc: BaseException) -> bool:
    """True when the traceback passes a local processing module and no provider module."""
    modules: set[str] = set()
    tb = exc.__traceback__
    while tb is not None:
        modules.add(str(tb.tb_frame.f_globals.get("__name__", "")))
        tb = tb.tb_next
    if any(m.startswith(_PROVIDER_PREFIX) for m in modules):
        return False
    return bool(modules & LOCAL_MODULES)


def _on_outer_error(state: TurnState, exc: Exception) -> None:
    state.outer_errors += 1
    detail = _short(exc)
    log.error(
        "turn %s: error escaped iteration %d (%d/%d)",
        state.turn_id,
        state.api_calls,
        state.outer_errors,
        state.options.max_outer_errors,
        exc_info=exc,
    )
    close_open_calls(state, texts.LOOP_ERROR_RESULT.format(detail=detail))
    if raised_locally(exc):
        state.end(
            ExitReason.PROVIDER_FAILED,
            final_text=texts.LOOP_ERROR_REPLY.format(detail=detail),
            failure=Failure("local_error", False, detail=detail),
        )
    elif state.outer_errors >= state.options.max_outer_errors:
        state.end(
            ExitReason.PROVIDER_FAILED,
            final_text=texts.LOOP_ERROR_REPLY.format(detail=detail),
            failure=Failure("loop_error", True, detail=detail),
        )
