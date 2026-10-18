"""The turn prologue: everything that must happen before the first provider call (R01.3).

In order:

1. per-turn state starts fresh (the factory built it); the think-tag scrubber is reset;
2. lone UTF-16 surrogates are scrubbed from the message and from ``persist_text`` (R03.19);
3. the history is loaded (from the store unless the caller passed it) and repaired (P04-03); when
   a stored row changed or left, the store's live transcript is replaced to match (best effort);
4. on the first turn of this process with history, the hydrator table rebuilds process-local state
   (the todo list registers there);
5. the system prompt is restored or built (P04-07);
6. the session row is made to exist **with** that prompt;
7. the user row is staged: ``content`` is what the user typed (``persist_text`` when given),
   ``api_content`` is the exact text sent (the message, any API-only prefix, the one-shot notes);
8. every pending row, the user row last, is stored **before the first provider call**.

A store failure here raises ``PersistFailed``; the loop ends the turn ``persistence_failed`` without
calling the model.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, cast

from indus.core import loop_texts as texts
from indus.core.notes import attach_notes
from indus.core.repair import repair_transcript
from indus.core.transcript import TranscriptRow, call_id, call_name
from indus.core.turn_state import PersistFailed, TurnState
from indus.protocol.events import TurnStarted
from indus.state.messages import scrub_surrogates
from indus.tools.calls import ToolResult

log = logging.getLogger(__name__)

__all__ = ["run_prologue", "scrub_message"]


def scrub_message(value: Any) -> Any:
    """``value`` with lone surrogates replaced in its text (strings and text parts)."""
    if isinstance(value, str):
        return scrub_surrogates(value)
    if isinstance(value, list):
        out: list[Any] = []
        for part in cast(list[Any], value):
            if isinstance(part, dict) and isinstance(part.get("text"), str):  # pyright: ignore[reportUnknownMemberType]
                item = cast(dict[str, Any], part)
                out.append({**item, "text": scrub_surrogates(item["text"])})
            else:
                out.append(part)
        return out
    return value


async def _load_history(state: TurnState) -> list[TranscriptRow]:
    runtime = state.runtime
    if state.history is not None:
        return list(state.history)
    if runtime.store is None:
        return []
    try:
        return await runtime.store.load(state.session_id)
    except Exception as exc:
        raise PersistFailed(f"could not read the transcript: {exc}") from exc


def _close_crashed_batch(history: list[TranscriptRow]) -> int:
    """A stored tool-call row at the tail whose calls have no result: the process stopped while
    they ran. Each gets a new result row saying the outcome is unknown (stored with the user
    row), so the model is told instead of the call silently vanishing in the repair."""
    tail = len(history)
    while tail > 0 and history[tail - 1].role == "tool":
        tail -= 1
    if tail == 0:
        return 0
    row = history[tail - 1]
    if row.role != "assistant" or not row.tool_calls or row.row_id is None:
        return 0
    answered = {r.tool_call_id for r in history[tail:]}
    added = 0
    for call in row.tool_calls:
        ident = call_id(call)
        if ident and ident not in answered:
            name = call_name(call) or "the tool"
            text = texts.OUTCOME_UNKNOWN.format(name=name)
            history.append(
                TranscriptRow(
                    role="tool",
                    name=call_name(call),
                    tool_call_id=ident,
                    content=ToolResult.error(text, error_type="unknown_outcome").content,
                )
            )
            added += 1
    if added:
        log.warning("closed %d tool call(s) left open when the last process stopped", added)
    return added


async def _repair(state: TurnState, history: list[TranscriptRow]) -> list[TranscriptRow]:
    stored_rows = [r for r in history if r.row_id is not None]
    from_store = bool(stored_rows) and all(
        r.row_id is not None for r in history[: len(stored_rows)]
    )
    flushed = len(stored_rows) if from_store else None
    rows, report = repair_transcript(history, flushed=flushed)
    if not report.total:
        return rows
    log.info("repaired the history of session %s: %s", state.session_id, dict(report.counts))
    store = state.runtime.store
    if not from_store or store is None:
        return rows
    if not report.rewritten and not report.dropped:
        return rows
    ids = [r.row_id for r in stored_rows if r.row_id is not None]
    try:
        new_ids = await store.replace(state.session_id, rows, expect_ids=ids)
    except Exception as exc:
        log.warning(
            "could not store the repaired history of session %s (%s); using it for this turn only",
            state.session_id,
            exc,
        )
        for row in rows:
            row.row_id = row.row_id if row.row_id is not None else -1
        return rows
    for row, ident in zip(rows, new_ids, strict=False):
        row.row_id = ident
    return rows


def _hydrate(state: TurnState, history: list[TranscriptRow]) -> None:
    runtime = state.runtime
    if runtime.hydrated or not history:
        return
    runtime.hydrated = True
    for name, hydrator in runtime.hydrators.items():
        try:
            hydrator(history)
        except Exception:
            log.warning("history hydrator %s failed; its state starts empty", name, exc_info=True)


def _user_row(state: TurnState, notes: str) -> TranscriptRow:
    message = scrub_message(state.message)
    persist = scrub_message(state.persist_text) if state.persist_text is not None else None
    staged: dict[str, Any] = {"role": "user", "content": message}
    if isinstance(message, str) and isinstance(persist, str) and persist != message:
        staged = {"role": "user", "content": persist, "api_content": message}
    return TranscriptRow.from_message(attach_notes(staged, notes))


async def run_prologue(state: TurnState) -> None:
    runtime = state.runtime
    state.control.phase = "prologue"
    state.scrubber.reset()
    state.touch("turn started")
    state.emit(TurnStarted, turn_id=state.turn_id, generation=state.generation)

    loaded = await _load_history(state)
    _close_crashed_batch(loaded)
    history = await _repair(state, loaded)
    state.rows = history
    state.stored_at_start = sum(1 for r in history if r.row_id is not None)
    _hydrate(state, history)

    ctx = replace(runtime.prompt_context, session_id=state.session_id)
    state.system_prompt = await runtime.prompts.restore_or_build(
        ctx, history=[r.to_message() for r in history]
    )

    if runtime.store is not None:
        try:
            await runtime.store.ensure_session(
                state.session_id,
                source=runtime.source or runtime.options.platform,
                system_prompt=state.system_prompt,
                fields=runtime.session_fields,
            )
        except Exception as exc:
            raise PersistFailed(f"could not create the session row: {exc}") from exc

    state.add(_user_row(state, runtime.prompts.notes.pop()))
    state.user_index = len(state.rows) - 1
    await state.flush()
    runtime.hydrated = True
