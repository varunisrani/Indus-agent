"""Generation fences: every control names the turn it means (P17-05).

Each turn of a session runs under a generation number that only grows. In the shared daemon a
session has several viewers, so a control must say which turn it targets: ``interrupt`` and
``steer`` name a generation, an approval or clarify answer names its request and generation,
``stop`` names an admission. When the named generation is not the one running, the control is
refused with ``StaleGeneration`` carrying the generation that does run (or None). A control is never
re-aimed at "the latest turn", which may belong to another surface (R01.4).

Turn bookkeeping is keyed by generation too (R06.2): ``begin`` records the claimed generation,
``adopt`` attaches the running turn's controls, ``release`` clears the session only for the
generation that is running, so a displaced turn finishing late cannot clear its successor.

Between ``begin`` and ``adopt`` (the turn is claimed but its agent is not ready) an interrupt or a
steer for that generation is latched and delivered at ``adopt`` (steers first, then the stop); it
is dropped when that generation is released, and never reaches a later one. A ``stop`` for an
admission whose claim has not reached the fence yet is kept for that generation's ``begin``.

The fence holds the running generation in memory, fed by whoever claims turns: the admission
service (``service.admissions``, ``AdmissionService(fences=...)``) calls ``begin`` at each claim,
``adopt`` at once with the claim's ticket (which keeps an early stop itself, so a stop also ends a
lease wait) and ``release`` when the turn has ended; the durable copy is the runtime ledger.
``stop`` reads and cancels through the same service; an admission of another session is
``not_found`` (a stop never reaches a session it does not name). Decisions are taken under one
lock; the turn's own methods are called outside it and receive the generation again, so a turn
that ended in between ignores the call. Results are values: the RPC layer maps
``StaleGeneration`` to the protocol's ``stale_generation`` error with the running generation in
its data.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol, cast

from indus.protocol.methods import ServerRequestAnswerResult
from indus.protocol.requests_port import AnswerModel
from indus.state.runtime_ledger import LedgerRefusal

__all__ = [
    "STALE_GENERATION",
    "AdmissionPort",
    "AdmissionReceipt",
    "Answerer",
    "ControlApplied",
    "ControlStatus",
    "GenerationFences",
    "OpenRequestInfo",
    "RequestLookup",
    "RequestNotOpen",
    "Responded",
    "StaleGeneration",
    "StopOutcome",
    "StopResult",
    "TurnControls",
]

STALE_GENERATION = "stale_generation"
"""The refusal reason of a control aimed at a generation that is not running (the ledger's
``LedgerRefusal`` reason of the same name; product code ``runtime.stale_generation``)."""


# -- ports ------------------------------------------------------------------------------------


class TurnControls(Protocol):
    """What a running turn exposes to the fence (the agent, through a thin adapter)."""

    def interrupt(self, generation: int, *, hard: bool = False, by_system: bool = False) -> bool:
        """Stop the turn if it still is ``generation``; False when nothing was stopped."""
        ...

    def steer(self, text: str) -> bool:
        """Hand guidance to the running turn; False when it no longer takes any."""
        ...


class OpenRequestInfo(Protocol):
    """An open server request as the fence needs it."""

    @property
    def generation(self) -> int | None:
        """The generation of the turn that asked (None when no turn asked)."""
        ...


class RequestLookup(Protocol):
    """The session's open server requests (``RequestBook`` of P17-07)."""

    def lookup(self, request_id: str) -> OpenRequestInfo | None: ...


type Answerer = Callable[[str, Mapping[str, Any] | AnswerModel], ServerRequestAnswerResult]
"""Hands an answer to the waiting request (``ServerRequestBroker.answer``)."""


class AdmissionReceipt(Protocol):
    """An admission's receipt (P17-02): state ``queued``/``started``/``terminal``/``unknown``."""

    @property
    def admission_id(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def state(self) -> str: ...

    @property
    def generation(self) -> int | None: ...

    @property
    def outcome(self) -> str | None: ...


class AdmissionPort(Protocol):
    """The admission service calls ``stop`` needs (``AdmissionService``; plain or awaitable).

    An id the service does not know raises ``LedgerRefusal("not_found")``; ``cancel`` of an
    admission that has started meanwhile raises ``admission_started``, of an unknown one
    ``unknown_execution``."""

    def receipt(self, admission_id: str) -> AdmissionReceipt | Awaitable[AdmissionReceipt]: ...

    def cancel(self, admission_id: str) -> AdmissionReceipt | Awaitable[AdmissionReceipt]: ...


# -- results ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StaleGeneration:
    """The control named a generation that is not running; nothing was done."""

    session_id: str
    requested: int | None
    current: int | None
    """The generation running now, or None when the session is idle."""
    code: ClassVar[str] = STALE_GENERATION
    error_code: ClassVar[str] = f"runtime.{STALE_GENERATION}"
    """The product error code (``foundation.errors.ERROR_CODES``), shared with the ledger."""

    def to_data(self) -> dict[str, Any]:
        """The ``error.data`` of the protocol refusal."""
        data: dict[str, Any] = {"reason": self.code, "running": self.current is not None}
        if self.current is not None:
            data["generation"] = self.current
        return data


class ControlStatus(StrEnum):
    """What happened to an accepted control."""

    DELIVERED = "delivered"
    """The running turn took it."""
    LATCHED = "latched"
    """The turn is claimed but not ready; it gets the control when it adopts its controls."""
    REJECTED = "rejected"
    """The turn took nothing: it is ending, or not ready for guidance yet (a steer before the
    runner bound its steer hook)."""


@dataclass(frozen=True, slots=True)
class ControlApplied:
    """An interrupt or steer that reached (or is waiting for) its generation."""

    session_id: str
    generation: int
    status: ControlStatus


@dataclass(frozen=True, slots=True)
class Responded:
    """The answer reached its request."""

    request_id: str
    accepted: bool


@dataclass(frozen=True, slots=True)
class RequestNotOpen:
    """The request was already answered, withdrawn or never sent: the answer did nothing."""

    request_id: str


class StopOutcome(StrEnum):
    """What a ``stop(admission_id)`` did."""

    CANCELLED = "cancelled"
    """The admission was still queued and is now cancelled."""
    INTERRUPTED = "interrupted"
    """The admission's turn runs (or is claimed) and was told to stop."""
    ALREADY_SETTLED = "already_settled"
    """The admission's turn already ended."""
    UNKNOWN = "unknown"
    """The admission's outcome is unknown; only an operator discard resolves it."""
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class StopResult:
    admission_id: str
    outcome: StopOutcome
    generation: int | None = None
    status: ControlStatus | None = None


# -- the fence --------------------------------------------------------------------------------


@dataclass(slots=True)
class _Stop:
    hard: bool
    by_system: bool


@dataclass(slots=True)
class _Session:
    running: int | None = None
    latest: int | None = None
    controls: TurnControls | None = None
    latched_stop: _Stop | None = None
    latched_steers: list[str] = field(default_factory=list[str])
    early_stop: int | None = None
    requests: RequestLookup | None = None
    answer: Answerer | None = None

    def clear_turn(self) -> None:
        self.running = None
        self.controls = None
        self.latched_stop = None
        self.latched_steers.clear()


async def _resolved[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return cast(T, await value)
    return cast(T, value)


class GenerationFences:
    """Generation checks for the controls of every session in one daemon."""

    def __init__(self, *, admissions: AdmissionPort | None = None) -> None:
        self._admissions = admissions
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}

    def _state(self, session_id: str) -> _Session:
        state = self._sessions.get(session_id)
        if state is None:
            state = self._sessions[session_id] = _Session()
        return state

    # -- wiring -------------------------------------------------------------------------------

    def bind_admissions(self, admissions: AdmissionPort) -> None:
        """Give ``stop`` its admission service (built after the fence, which it is handed)."""
        self._admissions = admissions

    def bind_requests(self, session_id: str, requests: RequestLookup, answer: Answerer) -> None:
        """Route approval/clarify answers of ``session_id`` to its request book and broker."""
        with self._lock:
            state = self._state(session_id)
            state.requests = requests
            state.answer = answer

    def forget(self, session_id: str) -> None:
        """Drop everything known about a closed session."""
        with self._lock:
            self._sessions.pop(session_id, None)

    # -- turn lifecycle -----------------------------------------------------------------------

    def begin(self, session_id: str, generation: int) -> None:
        """``generation`` was claimed and now runs (a still-running older one is displaced)."""
        with self._lock:
            state = self._state(session_id)
            if state.latest is not None and generation <= state.latest:
                raise ValueError(
                    f"generation {generation} of {session_id} is not above {state.latest}"
                )
            state.clear_turn()
            state.running = state.latest = generation
            if state.early_stop is not None and state.early_stop <= generation:
                if state.early_stop == generation:
                    state.latched_stop = _Stop(hard=True, by_system=False)
                state.early_stop = None

    def adopt(self, session_id: str, generation: int, controls: TurnControls) -> bool:
        """The running turn attaches its controls; latched steers and stop are delivered now."""
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None or state.running != generation:
                return False
            state.controls = controls
            steers, state.latched_steers = state.latched_steers, []
            stop, state.latched_stop = state.latched_stop, None
        for text in steers:
            controls.steer(text)
        if stop is not None:
            controls.interrupt(generation, hard=stop.hard, by_system=stop.by_system)
        return True

    def release(self, session_id: str, generation: int) -> bool:
        """The turn ``generation`` ended; only the running generation clears the session."""
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None or state.running != generation:
                return False
            state.clear_turn()
            return True

    def running(self, session_id: str) -> int | None:
        """The generation running now, or None."""
        with self._lock:
            state = self._sessions.get(session_id)
            return state.running if state is not None else None

    def latest(self, session_id: str) -> int | None:
        """The newest generation this fence has seen begin, or None."""
        with self._lock:
            state = self._sessions.get(session_id)
            return state.latest if state is not None else None

    # -- controls -----------------------------------------------------------------------------

    def interrupt(
        self, session_id: str, generation: int, *, hard: bool = False, by_system: bool = False
    ) -> ControlApplied | StaleGeneration:
        """Stop the turn ``generation`` of ``session_id``, and only that turn."""
        with self._lock:
            state = self._state(session_id)
            if state.running is None or state.running != generation:
                return StaleGeneration(session_id, generation, state.running)
            controls = state.controls
            if controls is None:
                previous = state.latched_stop
                state.latched_stop = _Stop(
                    hard=hard or (previous is not None and previous.hard),
                    by_system=by_system and (previous is None or previous.by_system),
                )
                return ControlApplied(session_id, generation, ControlStatus.LATCHED)
        done = controls.interrupt(generation, hard=hard, by_system=by_system)
        status = ControlStatus.DELIVERED if done else ControlStatus.REJECTED
        return ControlApplied(session_id, generation, status)

    def steer(
        self, session_id: str, generation: int, text: str
    ) -> ControlApplied | StaleGeneration:
        """Hand ``text`` to the turn ``generation``; blank text is a caller error."""
        if not text.strip():
            raise ValueError("steer text must not be empty")
        with self._lock:
            state = self._state(session_id)
            if state.running is None or state.running != generation:
                return StaleGeneration(session_id, generation, state.running)
            controls = state.controls
            if controls is None:
                state.latched_steers.append(text)
                return ControlApplied(session_id, generation, ControlStatus.LATCHED)
        done = controls.steer(text)
        status = ControlStatus.DELIVERED if done else ControlStatus.REJECTED
        return ControlApplied(session_id, generation, status)

    def respond(
        self,
        session_id: str,
        request_id: str,
        generation: int,
        answer: Mapping[str, Any] | AnswerModel,
    ) -> Responded | RequestNotOpen | StaleGeneration:
        """Answer an approval or clarify request asked by turn ``generation``.

        A request that is no longer open (answered elsewhere, withdrawn, timed out) is a quiet
        no-op. An open request answered with another generation, or whose turn no longer runs,
        is stale. A malformed answer raises the broker's ``ProtocolError``.
        """
        with self._lock:
            state = self._state(session_id)
            requests, answer_fn, running = state.requests, state.answer, state.running
        if requests is None or answer_fn is None:
            return RequestNotOpen(request_id)
        info = requests.lookup(request_id)
        if info is None:
            return RequestNotOpen(request_id)
        asked_by = info.generation
        if asked_by is not None and (asked_by != generation or running != generation):
            return StaleGeneration(session_id, generation, running)
        result = answer_fn(request_id, answer)
        return Responded(request_id, result.accepted)

    async def stop(self, session_id: str, admission_id: str) -> StopResult:
        """Stop one admission: cancel it while queued, else interrupt exactly its generation."""
        if self._admissions is None:
            raise RuntimeError("stop needs the admission service")
        receipt = await self._receipt(admission_id)
        if receipt is None or receipt.session_id != session_id:
            return StopResult(admission_id, StopOutcome.NOT_FOUND)
        if receipt.state == "queued":
            try:
                receipt = await _resolved(self._admissions.cancel(admission_id))
            except LedgerRefusal as refusal:
                if refusal.reason not in ("admission_started", "unknown_execution"):
                    raise
                # Claimed (or steered) between our read and the cancel: read it again and stop
                # that exact claim below.
                receipt = await self._receipt(admission_id)
                if receipt is None:
                    return StopResult(admission_id, StopOutcome.NOT_FOUND)
            else:
                if receipt.state == "terminal" and receipt.outcome == "cancelled":
                    return StopResult(admission_id, StopOutcome.CANCELLED, receipt.generation)
                if receipt.state == "queued":
                    raise RuntimeError(f"admission {admission_id} stayed queued after cancel")
        if receipt.state == "unknown":
            return StopResult(admission_id, StopOutcome.UNKNOWN, receipt.generation)
        if receipt.state != "started" or receipt.generation is None:
            return StopResult(admission_id, StopOutcome.ALREADY_SETTLED, receipt.generation)
        return self._stop_generation(session_id, admission_id, receipt.generation)

    async def _receipt(self, admission_id: str) -> AdmissionReceipt | None:
        assert self._admissions is not None
        try:
            return await _resolved(self._admissions.receipt(admission_id))
        except LedgerRefusal as refusal:
            if refusal.reason == "not_found":
                return None
            raise

    def _stop_generation(self, session_id: str, admission_id: str, generation: int) -> StopResult:
        with self._lock:
            state = self._state(session_id)
            if state.running != generation:
                if state.latest is None or generation > state.latest:
                    # The claim has not reached the fence yet: stop it when it begins.
                    state.early_stop = generation
                    return StopResult(
                        admission_id, StopOutcome.INTERRUPTED, generation, ControlStatus.LATCHED
                    )
                return StopResult(admission_id, StopOutcome.ALREADY_SETTLED, generation)
        applied = self.interrupt(session_id, generation, hard=True)
        if isinstance(applied, StaleGeneration):  # released in between
            return StopResult(admission_id, StopOutcome.ALREADY_SETTLED, generation)
        return StopResult(admission_id, StopOutcome.INTERRUPTED, generation, applied.status)
