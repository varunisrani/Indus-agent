"""``session/prompt`` and ``session/cancel`` (P19-09).

A prompt, in order:

1. the session must be held (else ``not_found``); a turn of it lost in a daemon restart (state
   ``unknown``) refuses the prompt with the ``unknown_execution`` text — nothing is sent, the user
   resolves it in a terminal (R09.11, R06.19, R03.21);
2. a text-only prompt that starts with a registered command runs it (``commands``); its reply ends
   the prompt, or it hands back text to run as the prompt; a skill command expands into its prompt;
3. otherwise the blocks are converted (``content``); the fingerprint is taken before that, and the
   retained identity of this session's previous submit — kept only when its acknowledgement never
   arrived — is reused when it matches, so the daemon answers with the same admission; any prompt
   retires the retained identity;
4. prompts of one session run one after another (a second waits for the first's answer);
5. submit with ``input_id``; a receipt for a turn that already settled shows its final text once;
   ``unknown`` refuses as in 1;
6. wait for the admission's turn, racing the session's lost-turn signal, a replay gap and the
   editor's cancel of a queued admission; then flush the projection up to the turn's
   ``message.complete`` so the answer is the last frame of the turn;
7. ``failed`` → JSON-RPC error with ``data.reason = admitted_turn_failed``; ``cancelled`` →
   ``stopReason: cancelled``; else ``end_turn`` with the session's usage.

``session/cancel`` (a notification): while a prompt is staging or submitting the cancel is held and
applied once the admission id is known (staging → the prompt ends ``cancelled`` without sending);
otherwise this editor's own admission is cancelled through the backend, which never interrupts a
turn that is not that admission's (R09.11).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from indus.acp import commands, content, store_view
from indus.acp.agent import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    RESOURCE_NOT_FOUND,
    AcpAgent,
    AcpError,
    HeldSession,
    handler,
)
from indus.acp.backend import Admission, BackendError, LostTurn, Settled
from indus.acp.history import lost_text
from indus.foundation.blocking import run_blocking
from indus.service.remote.host import OUTCOME_UNKNOWN

__all__ = [
    "FAILED_REASON",
    "KEPT_RETAINED",
    "PromptBook",
    "PromptState",
    "Retained",
    "acp_usage",
    "refusal",
]

log = logging.getLogger(__name__)

FAILED_REASON: Final = "admitted_turn_failed"
RECEIPT_GRACE_S: Final = 2.0
"""How long a settled admission's streamed reply is waited for before the receipt shows it."""
KEPT_RETAINED: Final = 64
_AMBIGUOUS: Final = frozenset({"outcome_unknown"})


@dataclass(frozen=True)
class Retained:
    """A submit whose acknowledgement never arrived: what the editor's retry may reuse."""

    fingerprint: str
    input_id: str
    text: str


@dataclass
class PromptState:
    """One held session's prompt bookkeeping."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    staging: bool = False
    submitting: bool = False
    pending_cancel: bool = False
    admission_id: str | None = None
    dropped: asyncio.Event = field(default_factory=asyncio.Event)
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled_text: str | None = None
    """The text of this session's last prompt the editor stopped (``/steer`` while idle)."""
    last_text: str | None = None


class PromptBook:
    """Per editor connection: prompt state per held session and the retained identities."""

    def __init__(self) -> None:
        self._states: dict[str, PromptState] = {}
        self._retained: OrderedDict[str, Retained] = OrderedDict()

    def state(self, key: str) -> PromptState:
        found = self._states.get(key)
        if found is None:
            found = self._states[key] = PromptState()
        return found

    def retain(self, key: str, retained: Retained) -> None:
        self._retained[key] = retained
        self._retained.move_to_end(key)
        while len(self._retained) > KEPT_RETAINED:
            self._retained.popitem(last=False)

    def take(self, key: str) -> Retained | None:
        """The session's retained identity, retired by this call (any prompt retires it)."""
        return self._retained.pop(key, None)

    def peek(self, key: str) -> Retained | None:
        return self._retained.get(key)

    def lost(self, key: str) -> None:
        """A lost turn was announced for the session: wake its waiting prompt."""
        self.state(key).lost.set()


# -- helpers ---------------------------------------------------------------------------------------


def refusal(session_id: str, lost: list[LostTurn]) -> AcpError:
    """The ``unknown_execution`` refusal (nothing is sent again from here)."""
    text = "unknown_execution: " + lost_text(session_id, lost).strip()
    return AcpError(INTERNAL_ERROR, text, {"reason": "unknown_execution"})


def acp_usage(numbers: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The session's usage counters as ACP ``Usage``."""
    if not numbers:
        return None

    def count(name: str) -> int:
        value = numbers.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    used_in, used_out = count("input_tokens"), count("output_tokens")
    usage: dict[str, Any] = {
        "inputTokens": used_in,
        "outputTokens": used_out,
        "totalTokens": used_in + used_out,
    }
    for ours, theirs in (
        ("reasoning_tokens", "thoughtTokens"),
        ("cache_read_tokens", "cachedReadTokens"),
        ("cache_write_tokens", "cachedWriteTokens"),
    ):
        if count(ours):
            usage[theirs] = count(ours)
    return usage


def _held(agent: AcpAgent, editor_id: str) -> HeldSession:
    held = agent.sessions.by_editor(editor_id)
    if held is None:
        raise AcpError(RESOURCE_NOT_FOUND, "no such session", {"reason": "not_found"})
    return held


def _answer(stop: str, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    from acp.schema import PromptResponse

    body: dict[str, Any] = {"stopReason": stop}
    if usage is not None:
        body["usage"] = usage
    response = PromptResponse.model_validate(body)
    return response.model_dump(mode="json", by_alias=True, exclude_none=True)


async def _refresh_title(agent: AcpAgent, held: HeldSession) -> None:
    """The stored title (an automatic title lands after the first reply) → the editor."""
    try:
        row = await run_blocking(
            store_view.session_row, agent.scope, held.key, label="acp session row"
        )
    except Exception:
        return
    title = row.get("title") if row else None
    agent.projection.title(held.key, title if isinstance(title, str) else None)


async def cancel_admission(agent: AcpAgent, held: HeldSession, admission_id: str) -> None:
    """Cancel this editor's own admission (never a successor's turn)."""
    backend = await agent.backend()
    try:
        outcome = await backend.cancel(held.key, admission_id)
    except BackendError as exc:
        log.info("cancel of %s not applied: %s", admission_id, exc.message)
        return
    if outcome == "cancelled":
        agent.prompts.state(held.key).dropped.set()  # dropped from the queue: no turn will come


# -- waiting ---------------------------------------------------------------------------------------


async def _first(*waits: asyncio.Future[Any]) -> asyncio.Future[Any]:
    done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    return next(iter(done))


async def _settle(
    agent: AcpAgent, held: HeldSession, state: PromptState, admission: Admission
) -> Settled:
    """Wait for the admission's turn (see the module notes, step 6)."""
    backend = await agent.backend()
    broken = agent.projection.broken(held.key)
    waiting = asyncio.ensure_future(backend.wait(held.key, admission.admission_id))
    try:
        while True:
            signals = [
                asyncio.ensure_future(broken.wait()),
                asyncio.ensure_future(state.dropped.wait()),
                asyncio.ensure_future(state.lost.wait()),
            ]
            try:
                first = await _first(waiting, *signals)
            finally:
                for signal in signals:
                    signal.cancel()
            if first is waiting:
                return waiting.result()
            if broken.is_set():
                raise BackendError("outcome_unknown", OUTCOME_UNKNOWN)
            if state.dropped.is_set():
                return Settled(admission.admission_id, "cancelled")
            state.lost.clear()
            lost = await backend.lost(held.key)
            receipt = await backend.receipt(held.key, admission.admission_id)
            if lost and receipt.state in ("queued", "unknown"):
                raise refusal(held.key, lost)  # blocked behind a lost turn, or lost itself
    finally:
        if not waiting.done():
            waiting.cancel()
            with contextlib.suppress(BaseException):
                await waiting


# -- the handlers ----------------------------------------------------------------------------------


@handler("session/prompt")
async def prompt(agent: AcpAgent, params: Mapping[str, Any]) -> dict[str, Any]:
    from acp.schema import PromptRequest

    request = PromptRequest.model_validate(params)
    held = _held(agent, request.session_id)
    blocks: list[Mapping[str, Any]] = [
        block.model_dump(mode="json", by_alias=True, exclude_none=True) for block in request.prompt
    ]
    backend = await agent.backend()
    lost = await backend.lost(held.key)
    if lost:
        raise refusal(held.key, lost)
    book = agent.prompts
    retained = book.take(held.key)
    state = book.state(held.key)
    text = content.plain_text(blocks)
    if text is not None:
        target = commands.quick_target(agent.scope, text)
        found = commands.command_of(target or text)
        if found is not None:
            outcome = await commands.run(agent, held, *found)
            if outcome.reply:
                agent.projection.say(held.key, outcome.reply.rstrip() + "\n")
            if outcome.submit is None:
                await agent.projection.flush()
                return _answer("end_turn")
            return await _run(agent, held, state, outcome.submit, None)
        expanded = await commands.skill_expansion(agent, held, text)
        if expanded is not None:
            return await _run(agent, held, state, expanded, None)
    fingerprint = content.fingerprint(blocks)
    if retained is not None and retained.fingerprint == fingerprint:
        return await _run(agent, held, state, retained.text, retained)
    state.staging = True
    try:
        prepared = await content.prepare(blocks, agent.scope)
    finally:
        state.staging = False
    if state.pending_cancel:
        state.pending_cancel = False
        return _answer("cancelled")
    return await _run(
        agent, held, state, prepared.text, Retained(fingerprint, uuid.uuid4().hex, prepared.text)
    )


async def _run(
    agent: AcpAgent,
    held: HeldSession,
    state: PromptState,
    text: str,
    identity: Retained | None,
) -> dict[str, Any]:
    """Submit ``text`` and wait for its turn (steps 4 to 7)."""
    if not text.strip():
        raise AcpError(INVALID_PARAMS, "the prompt is empty", {"reason": "empty_prompt"})
    if identity is None:
        identity = Retained("", uuid.uuid4().hex, text)
    async with state.lock:
        backend = await agent.backend()
        state.dropped.clear()
        state.lost.clear()
        state.submitting = True
        try:
            admission = await backend.submit(held.key, text, input_id=identity.input_id)
        except BackendError as exc:
            if exc.code in _AMBIGUOUS and identity.fingerprint:
                agent.prompts.retain(held.key, identity)  # the editor's retry may reuse it
            state.pending_cancel = False
            raise
        except asyncio.CancelledError:
            if identity.fingerprint:
                agent.prompts.retain(held.key, identity)
            raise
        finally:
            state.submitting = False
        state.last_text = text
        if admission.state == "unknown":
            lost = await backend.lost(held.key)
            raise refusal(held.key, lost or [LostTurn(admission.admission_id, 0)])
        if admission.state == "terminal":
            # Settled already (a retry of a turn that ran, or a very quick turn): its reply is
            # shown once — from the stream when it arrives, else from the receipt.
            state.pending_cancel = False
            generation = admission.generation
            shown = generation is not None and await agent.projection.reply_shown(
                held.key, generation, grace=RECEIPT_GRACE_S
            )
            if not shown:
                agent.projection.show_receipt(held.key, generation, admission.final_text or "")
                await agent.projection.flush()
            return _finish(agent, held, state, text, admission.outcome or "completed", None)
        state.admission_id = admission.admission_id
        try:
            if state.pending_cancel:
                state.pending_cancel = False
                await cancel_admission(agent, held, admission.admission_id)
            settled = await _settle(agent, held, state, admission)
            await agent.projection.settled(held.key, settled.turn_id)
        finally:
            if state.admission_id == admission.admission_id:
                state.admission_id = None
        await _refresh_title(agent, held)
        await agent.projection.flush()
        return _finish(agent, held, state, text, settled.outcome, settled.failure)


def _finish(
    agent: AcpAgent,
    held: HeldSession,
    state: PromptState,
    text: str,
    outcome: str,
    failure: str | None,
) -> dict[str, Any]:
    if outcome in ("cancelled", "interrupted", "discarded", "replaced"):
        state.cancelled_text = text
        return _answer("cancelled")
    state.cancelled_text = None
    if outcome == "failed":
        message = f"the turn failed: {failure}" if failure else "the turn failed"
        raise AcpError(INTERNAL_ERROR, message, {"reason": FAILED_REASON})
    return _answer("end_turn", acp_usage(agent.projection.usage_of(held.key)))


@handler("session/cancel")
async def cancel(agent: AcpAgent, params: Mapping[str, Any]) -> None:
    from acp.schema import CancelNotification

    request = CancelNotification.model_validate(params)
    held = agent.sessions.by_editor(request.session_id)
    if held is None:
        return None
    state = agent.prompts.state(held.key)
    if state.staging or state.submitting:
        state.pending_cancel = True
        return None
    if state.admission_id is not None:
        await cancel_admission(agent, held, state.admission_id)
    return None
