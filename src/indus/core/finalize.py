"""The single exit funnel: every turn ends here (R01.3, R01.18).

Each step is guarded on its own, so one failing step never loses the answer:

1. the exit reason is settled (an interrupt wins over "no reason recorded");
2. scaffold rows of this turn leave the live list (they were never stored);
3. an empty final answer is recovered from the text already streamed;
4. the tail is closed: an open tool-call row gets one result per unanswered call, an interrupt's
   streamed text is sealed as an assistant row, delivered final text always has its assistant row;
5. every pending row is stored;
6. leftover steer text becomes ``pending_steer`` (the next turn's message), unless the stop
   was hard;
7. open server requests are withdrawn (turn ended);
8. the result is stamped with ``{turn_id, current_turn_user_index}``, the exit reason and, for a
   failure, its reason / retryable flag / reset time; one log line is written (WARNING when
   abnormal) and ``turn.done`` is sent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from indus.core import loop_texts as texts
from indus.core.exit_reasons import ABNORMAL, FAILING, INTERRUPTED, ExitReason, Failure
from indus.core.transcript import TranscriptRow, call_id, call_name
from indus.core.turn_state import PersistFailed, TurnResult, TurnState
from indus.protocol.events import CancelReason, TurnDone, TurnFailure
from indus.tools.calls import ToolResult

if TYPE_CHECKING:
    from collections.abc import Awaitable

log = logging.getLogger(__name__)

__all__ = ["close_open_calls", "finalize_turn"]


def close_open_calls(state: TurnState, text: str) -> int:
    """Give every unanswered call of the newest tool-call row a result row; returns how many."""
    for position in range(len(state.rows) - 1, -1, -1):
        row = state.rows[position]
        if row.scaffold or row.role == "tool":
            continue
        if row.role != "assistant" or not row.tool_calls:
            return 0
        answered = {
            r.tool_call_id
            for r in state.rows[position + 1 :]
            if r.role == "tool" and not r.scaffold
        }
        added = 0
        for call in row.tool_calls:
            ident = call_id(call)
            if ident and ident not in answered:
                state.add(
                    TranscriptRow(
                        role="tool",
                        name=call_name(call),
                        tool_call_id=ident,
                        content=ToolResult.error(text).content,
                    )
                )
                added += 1
        return added
    return 0


def _interrupt_reason(state: TurnState) -> ExitReason:
    if state.control.by_system:
        return ExitReason.INTERRUPTED_BY_SYSTEM
    return ExitReason.INTERRUPTED_BY_USER


def _settle_reason(state: TurnState) -> None:
    if state.exit is not None:
        return
    if state.control.interrupted:
        state.end(_interrupt_reason(state))
    elif state.final_text:
        state.end(ExitReason.COMPLETED)
    else:
        state.end(
            ExitReason.PROVIDER_FAILED,
            final_text=texts.LOOP_ERROR_REPLY.format(detail="the turn ended without a reply"),
            failure=Failure("loop_error", True),
        )


def _recover_from_stream(state: TurnState) -> None:
    if state.control.interrupted or state.exit in INTERRUPTED or state.failure is not None:
        return
    if not (state.final_text or "").strip() and state.streamed_text.strip():
        state.final_text = state.streamed_text.strip()


def _close_tail(state: TurnState) -> None:
    interrupted = state.exit in INTERRUPTED
    reason = (
        texts.TOOL_CANCELLED.format(name="this tool")
        if interrupted
        else (
            texts.LOOP_ERROR_RESULT.format(detail=state.failure.reason if state.failure else "stop")
        )
    )
    close_open_calls(state, reason)
    if interrupted:
        partial = state.streamed_text.strip()
        if partial and state.tail_role() != "assistant":
            state.add(TranscriptRow(role="assistant", content=partial, finish_reason="interrupted"))
            state.final_text = state.final_text or partial
        return
    final = state.final_text
    if not final or state.failure is not None:
        return
    tail = next((r for r in reversed(state.rows) if not r.scaffold), None)
    if (
        tail is not None
        and tail.role == "assistant"
        and not tail.tool_calls
        and final in (tail.content, tail.api_content)
    ):
        return
    state.add(TranscriptRow(role="assistant", content=final))


async def _persist(state: TurnState) -> None:
    try:
        await state.flush()
    except PersistFailed as exc:
        if state.exit not in FAILING:
            state.exit = ExitReason.PERSISTENCE_FAILED
            state.failure = Failure("persistence", True, detail=str(exc))
        raise


def _reasoning(state: TurnState) -> str | None:
    for row in reversed(state.turn_rows()):
        if row.role == "assistant" and row.reasoning and row.reasoning.strip():
            return row.reasoning
    return None


def _log_exit(state: TurnState) -> None:
    exit_reason = state.exit or ExitReason.PROVIDER_FAILED
    tail = state.tail_role()
    tool_rounds = sum(1 for r in state.turn_rows() if r.role == "assistant" and r.tool_calls)
    stuck = tail == "tool" and exit_reason not in INTERRUPTED
    level = logging.WARNING if exit_reason in ABNORMAL or stuck else logging.INFO
    log.log(
        level,
        "turn ended: session=%s turn=%s reason=%s failure=%s api_calls=%d tool_rounds=%d "
        "last_role=%s reply_chars=%d",
        state.session_id,
        state.turn_id,
        exit_reason.value,
        state.failure.reason if state.failure else "-",
        state.api_calls,
        tool_rounds,
        tail,
        len(state.final_text or ""),
    )


async def _guarded(
    state: TurnState, label: str, step: Callable[[], Awaitable[None] | None]
) -> None:
    try:
        outcome = step()
        if outcome is not None:
            await outcome
    except Exception as exc:
        state.cleanup_errors.append(f"{label}: {exc}")
        log.error("finalize step %s failed: %s", label, exc, exc_info=exc)


async def finalize_turn(state: TurnState) -> TurnResult:
    """Close the turn (see the module notes) and return its result."""
    control = state.control
    control.phase = "finalize"
    control.stop.set()
    await _guarded(state, "drop_scaffold", lambda: state.drop_scaffold())
    await _guarded(state, "recover_stream", lambda: _recover_from_stream(state))
    await _guarded(state, "settle", lambda: _settle_reason(state))
    await _guarded(state, "close_tail", lambda: _close_tail(state))
    await _guarded(state, "persist", lambda: _persist(state))
    state.pending_steer = control.leftover_steer()
    await _guarded(state, "withdraw", lambda: _withdraw(state))
    exit_reason = state.exit or ExitReason.PROVIDER_FAILED
    failure = state.failure
    if failure is None and exit_reason in FAILING:
        failure = state.failure = Failure(exit_reason.value, True)
    _log_exit(state)
    turn_failure = (
        TurnFailure(reason=failure.reason, retryable=failure.retryable, resets_at=failure.resets_at)
        if failure is not None
        else None
    )
    state.emit(TurnDone, turn_id=state.turn_id, reason=exit_reason, failure=turn_failure)
    control.phase = "done"
    return TurnResult(
        session_id=state.session_id,
        turn_id=state.turn_id,
        generation=state.generation,
        exit_reason=exit_reason,
        final_text=state.final_text,
        rows=list(state.rows),
        current_turn_user_index=state.user_index,
        api_calls=state.api_calls,
        failure=failure,
        reasoning=_reasoning(state),
        pending_steer=state.pending_steer,
        guardrail=state.guard.halt,
        intervened=control.intervened,
        cleanup_errors=tuple(state.cleanup_errors),
    )


def _withdraw(state: TurnState) -> None:
    state.control.withdraw_requests(CancelReason.TURN_ENDED)
