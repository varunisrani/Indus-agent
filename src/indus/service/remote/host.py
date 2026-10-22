"""The remote session service: the session API over the attach channel (P17-17).

``RemoteSessionService`` has the calls a surface makes on the in-process ``SessionService`` —
create, resume, submit, wait, interrupt, steer, redirect, answer a server request, events, live
listeners, close — and sends each as a JSON-RPC call to the daemon over a ``Channel`` (P17-16).
Events come back as the same event models (``TurnStarted``, ``MessageDelta``, ``TurnDone``, …)
and server requests as the same request models, so a renderer cannot tell where the agent runs.

Rules:

- **Create is idempotent** (R05.21): every create carries an ``idempotency_key`` (the caller's or a
  fresh one). When the transport goes before the answer, the same create is sent once more after
  the reconnect; the daemon returns the session it already made.
- **Submit is journalled** (``journal.Journal``): the entry is written before the submit leaves and
  carries the admission request id. Accepted → retired. Refused by the daemon, or never sent →
  retired. Transport lost after it left → kept as ``unknown`` and ``OutcomeUnknown`` raised: it is
  never resent on its own (R09.11). ``retry(request_id)`` resends it with the same id when the
  person asks (the daemon's ledger answers an exact retry with the first admission); ``discard``
  drops it. A daemon answer ``OUTCOME_UNKNOWN`` (the retried input's turn was lost in a restart)
  keeps the entry as ``unknown`` too and raises ``OutcomeUnknown`` (P17-25).
- **A running turn whose transport is lost**: every ``wait`` on that connection's turns fails
  ``OutcomeUnknown`` at once; nothing is resubmitted.
- **Reconnect**: before callers' requests flow again, each open session is subscribed again with
  its replay cursor (``session.subscribe {epoch, cursor}``): missed events arrive once (events at
  or below the cursor are dropped), open server requests are offered again (the same id is passed
  on once), and a ``session.replay_gap`` reaches ``on_gap`` listeners so the surface reloads. A
  session the restarted daemon does not host any more is resumed.
- Controls name the generation of the turn they target: the caller's, else the one of the last
  ``turn.started`` seen; an answer names the generation its offer carried.
- A server request another viewer answered (``request.settled`` with ``answered``) reaches the
  session's listeners as ``server_request.cancelled`` (``answered_elsewhere``), so a card still
  open here closes as it does for a withdrawn request (P17-27).
- ``discard_unknown`` resolves a turn the daemon lost in a restart (``admission.discard``, by
  that turn's generation).
- Presence and queue events (``session.viewers``, ``admission.state``) advance the cursor but are
  not handed to the session's renderer.

``RemoteHost`` is the blocking facade the classic REPL uses: the same method names as
``InProcessHost`` (``create``, ``resume``, ``submit``, ``wait``, ``interrupt``, ``steer``,
``redirect``, ``answer_server_request``, ``events``, ``close_session``, ``feed``, ``call``,
``run``), with the channel and the service on a loop thread of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import secrets
import threading
from collections import deque
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final, Self

from pydantic import TypeAdapter, ValidationError

from indus.core.events import EventListener, RequestListener
from indus.foundation.blocking import run_blocking
from indus.protocol.attach import AdmissionDiscardResult
from indus.protocol.base import ProtocolModel
from indus.protocol.errors import ERROR_TABLE, INTERNAL_ERROR, SESSION_NOT_FOUND, ErrorCode
from indus.protocol.errors import OUTCOME_UNKNOWN as PROTOCOL_OUTCOME_UNKNOWN
from indus.protocol.events import (
    EVENTS,
    CancelReason,
    EventBase,
    ExitReason,
    ServerRequestCancelled,
    TurnDone,
    TurnFailure,
    TurnStarted,
)
from indus.protocol.methods import (
    PromptSubmitResult,
    ProtocolError,
    ServerRequestAnswerResult,
    SessionCreateParams,
    SessionCreateResult,
    SessionInterruptResult,
    SessionRedirectResult,
    SessionResumeResult,
    SessionSteerResult,
    SubmitStatus,
)
from indus.protocol.requests_port import AnswerModel
from indus.protocol.server_requests import SERVER_REQUEST_METHOD, ServerRequest
from indus.service.remote.channel import (
    Channel,
    NotSent,
    RpcError,
    TransportLost,
)
from indus.service.remote.journal import Journal, Prepared

__all__ = [
    "GAP_METHOD",
    "OUTCOME_UNKNOWN",
    "OUTCOME_UNKNOWN_CODE",
    "GapListener",
    "OutcomeUnknown",
    "RemoteHost",
    "RemoteSessionService",
    "RemoteTurn",
]

log = logging.getLogger(__name__)

GAP_METHOD: Final = "session.replay_gap"
SETTLED_METHOD: Final = "request.settled"
ANSWERED: Final = "answered"
"""The ``request.settled`` outcome of a request some viewer answered."""
OUTCOME_UNKNOWN_CODE: Final = "runtime.outcome_unknown"
PRESENCE_METHODS: Final = frozenset({"session.viewers", "admission.state"})
"""Numbered events that are not for the session renderer (presence and queue bookkeeping)."""
OUTCOME_UNKNOWN: Final = (
    "The connection to the gateway was lost; the outcome is unknown. Do not resend it: resume "
    "the session to see whether it completed."
)
KEPT_EVENTS: Final = 512
"""Events kept per session for ``events(since)``."""

_REQUESTS: TypeAdapter[Any] = TypeAdapter(ServerRequest)
_BY_NUMBER: Final[dict[int, ErrorCode]] = {row.code: row for row in ERROR_TABLE}

type GapListener = Callable[[Mapping[str, Any]], object]


class OutcomeUnknown(TransportLost):
    """The daemon may or may not have run this; it must not be sent again on its own.
    ``request_id`` names the kept journal entry (``retry`` / ``discard``) when there is one."""

    code = OUTCOME_UNKNOWN_CODE

    def __init__(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        super().__init__(OUTCOME_UNKNOWN)
        self.session_id = session_id
        self.request_id = request_id
        self.turn_id = turn_id


@dataclass(frozen=True)
class RemoteTurn:
    """How a turn ended (from its ``turn.done``)."""

    session_id: str
    turn_id: str
    generation: int | None
    reason: ExitReason
    failure: TurnFailure | None = None


def protocol_error(error: RpcError) -> ProtocolError:
    """The daemon's JSON-RPC error as the ``ProtocolError`` an in-process call would raise."""
    row = _BY_NUMBER.get(error.code, INTERNAL_ERROR)
    path = error.data.get("path")
    return ProtocolError(row, error.message, path=path if isinstance(path, str) else None)


@dataclass
class _Session:
    """What this client knows about one attached session."""

    key: str
    current: str
    epoch: str | None = None
    cursor: int = 0
    generation: int | None = None
    running: str | None = None
    kept: deque[EventBase] = field(default_factory=lambda: deque[EventBase](maxlen=KEPT_EVENTS))
    listeners: list[EventListener] = field(default_factory=list[EventListener])
    requesters: list[RequestListener] = field(default_factory=list[RequestListener])
    gaps: list[GapListener] = field(default_factory=list[GapListener])
    offers: dict[str, int | None] = field(default_factory=dict[str, int | None])
    ended: dict[str, RemoteTurn] = field(default_factory=dict[str, RemoteTurn])
    by_generation: dict[int, str] = field(default_factory=dict[int, str])
    """Turn id of each recently started generation."""
    admissions: dict[str, str] = field(default_factory=dict[str, str])
    """Admission id (``prompt.submit``'s ``turn_id``) → the turn it became."""
    unassigned: deque[tuple[str, int, bool]] = field(default_factory=deque[tuple[str, int, bool]])
    """Submitted admissions whose turn has not started: (id, generation, exact)."""
    waiters: dict[str, list[asyncio.Future[RemoteTurn]]] = field(
        default_factory=dict[str, list[asyncio.Future[RemoteTurn]]]
    )


def _call(listener: Callable[[Any], object], value: object) -> None:
    try:
        listener(value)
    except Exception:
        log.exception("a remote session listener failed")


class RemoteSessionService:
    """The session service over the attach channel (see the module notes)."""

    def __init__(self, channel: Channel, journal: Journal, *, surface: str = "cli") -> None:
        self.channel = channel
        self.journal = journal
        self.surface = surface
        self._sessions: dict[str, _Session] = {}
        self._alias: dict[str, str] = {}
        self._undo: list[Callable[[], None]] = []
        self._closed = False

    # -- lifetime ---------------------------------------------------------------------------------

    async def start(self) -> Self:
        """Wire the channel and make its first connection (its dial failure is raised)."""
        if not self._undo:
            self._undo = [
                self.channel.on_notification(self._on_notification),
                self.channel.on_ready(self._resubscribe),
                self.channel.on_state(self._on_state),
            ]
        await self.channel.start()
        return self

    async def aclose(self) -> None:
        """Detach from the daemon (turns keep running there) and close the channel."""
        self._closed = True
        for undo in self._undo:
            undo()
        self._undo.clear()
        self._fail_waiters(None)
        await self.channel.close()

    @property
    def attached(self) -> list[str]:
        return [s.current for s in self._sessions.values()]

    def current_id(self, session_id: str) -> str:
        return self._session(session_id).current

    def generation(self, session_id: str) -> int | None:
        return self._session(session_id).generation

    def key_of(self, session_id: str) -> str:
        """The id this client first knew the session by (the daemon's ledger key for it)."""
        return self._session(session_id).key

    def turn_of(self, session_id: str, turn_id: str) -> tuple[str, int] | None:
        """``(turn id, generation)`` of the turn ``turn_id`` names (a turn id, or the admission id a
        submit answered) once it has started here; None before (or when it is too old)."""
        found = self._session(session_id)
        turn = found.admissions.get(turn_id, turn_id)
        for generation, name in found.by_generation.items():
            if name == turn:
                return turn, generation
        return None

    def _session(self, session_id: str) -> _Session:
        key = self._alias.get(session_id, session_id)
        found = self._sessions.get(key)
        if found is None:
            raise ProtocolError(SESSION_NOT_FOUND, path="session_id")
        return found

    def _adopt(self, session_id: str, *, cursor: int) -> _Session:
        key = self._alias.get(session_id, session_id)
        found = self._sessions.get(key)
        epoch = self.channel.ready_params.get("replay_epoch")
        if found is None:
            found = _Session(key, session_id)
            self._sessions[key] = found
        found.current = session_id
        self._alias[session_id] = key
        found.epoch = epoch if isinstance(epoch, str) else None
        found.cursor = max(0, cursor)
        return found

    async def _rpc(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return await self.channel.request(method, params)
        except RpcError as error:
            raise protocol_error(error) from None

    # -- sessions ---------------------------------------------------------------------------------

    async def create(self, params: SessionCreateParams | None = None) -> SessionCreateResult:
        """Create a session (idempotent by its key; see the module notes)."""
        params = params or SessionCreateParams()
        if params.idempotency_key is None:
            params = params.model_copy(
                update={"idempotency_key": f"create-{secrets.token_hex(16)}"}
            )
        wire = params.to_wire()
        try:
            answer = await self._rpc("session.create", wire)
        except TransportLost:
            answer = await self._rpc("session.create", wire)  # same key: never a second session
        result = SessionCreateResult.model_validate(answer)
        self._adopt(result.session_id, cursor=0)
        return result

    async def resume(self, session_id: str) -> SessionResumeResult:
        result = SessionResumeResult.model_validate(
            await self._rpc("session.resume", {"session_id": session_id})
        )
        self._adopt(result.session_id, cursor=result.last_seq)
        if result.session_id != session_id:
            self._alias[session_id] = self._alias[result.session_id]
        return result

    async def close(self, session_id: str) -> None:
        """Stop watching a session from here (its turns keep running in the daemon)."""
        key = self._alias.get(session_id, session_id)
        found = self._sessions.pop(key, None)
        if found is None:
            return
        for alias in [a for a, k in self._alias.items() if k == key]:
            del self._alias[alias]
        self._fail_session(found, closing=True)
        with contextlib.suppress(ProtocolError, NotSent, TransportLost):
            await self._rpc("session.close", {"session_id": found.current})

    # -- turns ------------------------------------------------------------------------------------

    async def submit(
        self, session_id: str, text: str, *, unattended: bool = False, single_query: bool = False
    ) -> PromptSubmitResult:
        """Admit ``text`` (see the module notes for the journal and ``OutcomeUnknown``).
        ``unattended``: a finite run nobody watches (``-z``); the daemon auto-approves its turn.
        ``single_query``: a finite run (``chat -q``, ``-z``); the daemon runs its turn one-shot."""
        found = self._session(session_id)
        entry = await run_blocking(
            lambda: self.journal.prepare(
                found.current, text, unattended=unattended, single_query=single_query
            ),
            label="journal",
        )
        return await self._send(found, entry)

    async def retry(self, request_id: str) -> PromptSubmitResult:
        """Send a kept submission again, with its own request id (the person asked for it)."""
        entry = await run_blocking(self.journal.get, request_id, label="journal")
        if entry is None:
            raise ProtocolError(INTERNAL_ERROR, "no kept submission with that id")
        return await self._send(self._session(entry.session_id), entry)

    async def discard(self, request_id: str) -> bool:
        """Drop a kept submission without sending it."""
        return await run_blocking(self.journal.retire, request_id, label="journal")

    async def pending_submissions(self, session_id: str | None = None) -> list[Prepared]:
        return await run_blocking(self.journal.pending, session_id, label="journal")

    async def _send(self, found: _Session, entry: Prepared) -> PromptSubmitResult:
        params: dict[str, Any] = {
            "session_id": found.current,
            "text": entry.text,
            "request_id": entry.request_id,
        }
        if entry.unattended:  # kept with the entry, so an explicit retry is an exact retry
            params["unattended"] = True
        if entry.single_query:
            params["single_query"] = True
        floor = max(found.by_generation, default=0)
        try:
            answer = await self.channel.request("prompt.submit", params)
        except RpcError as error:
            if error.code == PROTOCOL_OUTCOME_UNKNOWN.code:
                # An exact retry of an input the daemon lost in a restart: never run again, so
                # the entry stays as unknown for an explicit user decision.
                await run_blocking(self.journal.mark_unknown, entry.request_id, label="journal")
                raise OutcomeUnknown(found.current, request_id=entry.request_id) from None
            await run_blocking(self.journal.retire, entry.request_id, label="journal")
            raise protocol_error(error) from None
        except NotSent:
            await run_blocking(self.journal.retire, entry.request_id, label="journal")
            raise
        except TransportLost:
            await run_blocking(self.journal.mark_unknown, entry.request_id, label="journal")
            raise OutcomeUnknown(found.current, request_id=entry.request_id) from None
        await run_blocking(self.journal.retire, entry.request_id, label="journal")
        result = PromptSubmitResult.model_validate(answer)
        self._track(found, result, floor)
        return result

    def _track(self, found: _Session, result: PromptSubmitResult, floor: int) -> None:
        """Remember which turn an admission becomes (see ``wait``)."""
        answered = result.generation or 0  # left out while the daemon has not claimed it
        exact = result.status is SubmitStatus.STREAMING and answered > 0
        if exact:
            found.generation = answered
            turn = found.by_generation.get(answered)
            if turn is not None:
                found.admissions[result.turn_id] = turn
                return
        else:
            # The turn may have started before this answer arrived: the first later generation
            # that no other admission of ours took.
            taken = set(found.admissions.values())
            bound = max(floor, answered)
            later = sorted(g for g in found.by_generation if g > bound)
            for generation in later:
                turn = found.by_generation[generation]
                if turn not in taken:
                    found.admissions[result.turn_id] = turn
                    return
        found.unassigned.append((result.turn_id, max(floor, answered), exact))

    def _assign(self, found: _Session, started: TurnStarted) -> None:
        found.by_generation[started.generation] = started.turn_id
        while len(found.by_generation) > 64:
            del found.by_generation[next(iter(found.by_generation))]
        for index, (admission, generation, exact) in enumerate(found.unassigned):
            fits = generation == started.generation if exact else started.generation > generation
            if fits:
                del found.unassigned[index]
                found.admissions[admission] = started.turn_id
                while len(found.admissions) > 256:
                    del found.admissions[next(iter(found.admissions))]
                return

    async def wait(self, session_id: str, turn_id: str | None = None) -> RemoteTurn:
        """How a turn ended; ``OutcomeUnknown`` when the transport goes first. ``turn_id`` is a
        turn id from ``turn.started``, or the ``turn_id`` a submit answered (its admission id):
        a submit that started at once is matched by its generation, a queued one by the first
        later turn to start, in submit order (until P17-25's ``admission.state`` names it).
        Default: the running turn."""
        found = self._session(session_id)
        target = turn_id or found.running
        if target is None:
            raise ProtocolError(INTERNAL_ERROR, "no turn to wait for")
        target = found.admissions.get(target, target)
        if target in found.ended:
            return found.ended[target]
        future: asyncio.Future[RemoteTurn] = asyncio.get_running_loop().create_future()
        found.waiters.setdefault(target, []).append(future)
        return await future

    async def interrupt(
        self, session_id: str, generation: int | None = None, *, hard: bool = False
    ) -> SessionInterruptResult:
        found = self._session(session_id)
        target = generation if generation is not None else found.generation
        if target is None:
            return SessionInterruptResult(interrupted=False)
        answer = await self._rpc(
            "session.interrupt",
            {"session_id": found.current, "generation": target, "hard": hard},
        )
        return SessionInterruptResult.model_validate(answer)

    async def steer(
        self, session_id: str, text: str, *, generation: int | None = None
    ) -> SessionSteerResult:
        found = self._session(session_id)
        target = generation if generation is not None else found.generation
        if target is None or found.running is None:
            return SessionSteerResult(queued=False)
        answer = await self._rpc(
            "session.steer", {"session_id": found.current, "text": text, "generation": target}
        )
        return SessionSteerResult.model_validate(answer)

    async def redirect(self, session_id: str, text: str) -> SessionRedirectResult:
        found = self._session(session_id)
        answer = await self._rpc("session.redirect", {"session_id": found.current, "text": text})
        return SessionRedirectResult.model_validate(answer)

    async def answer_server_request(
        self, session_id: str, request_id: str, answer: Mapping[str, Any] | AnswerModel
    ) -> ServerRequestAnswerResult:
        found = self._session(session_id)
        generation = found.offers.get(request_id, found.generation)
        body = answer.to_wire() if isinstance(answer, ProtocolModel) else dict(answer)
        params: dict[str, Any] = {
            "session_id": found.current,
            "request_id": request_id,
            "answer": body,
        }
        if generation is not None:
            params["generation"] = generation
        result = ServerRequestAnswerResult.model_validate(
            await self._rpc("server_request.answer", params)
        )
        found.offers.pop(request_id, None)
        return result

    async def discard_unknown(
        self, session_id: str, admission_id: str, generation: int
    ) -> AdmissionDiscardResult:
        """Resolve a turn the daemon lost in a restart (``admission.discard``): never run again,
        the queue moves on. A stale generation changes nothing (``reason``)."""
        found = self._session(session_id)
        answer = await self._rpc(
            "admission.discard",
            {"session_id": found.current, "admission_id": admission_id, "generation": generation},
        )
        return AdmissionDiscardResult.model_validate(answer)

    async def decline_server_request(self, session_id: str, request_id: str) -> bool:
        found = self._session(session_id)
        answer = await self._rpc(
            "server_request.decline", {"session_id": found.current, "request_id": request_id}
        )
        return answer.get("accepted") is True

    # -- events -----------------------------------------------------------------------------------

    def events(self, session_id: str, since: int | None = None) -> list[EventBase]:
        """Kept events of the session with a seq above ``since`` (all kept ones for None)."""
        kept = self._session(session_id).kept
        return [e for e in kept if since is None or e.seq > since]

    def subscribe(self, session_id: str, listener: EventListener) -> Callable[[], None]:
        found = self._session(session_id)
        found.listeners.append(listener)
        return lambda: _remove(found.listeners, listener)

    def on_request(self, session_id: str, listener: RequestListener) -> Callable[[], None]:
        found = self._session(session_id)
        found.requesters.append(listener)
        return lambda: _remove(found.requesters, listener)

    def on_gap(self, session_id: str, listener: GapListener) -> Callable[[], None]:
        found = self._session(session_id)
        found.gaps.append(listener)
        return lambda: _remove(found.gaps, listener)

    def _on_notification(self, method: str, params: Mapping[str, Any]) -> None:
        if method == SERVER_REQUEST_METHOD:
            self._offer(params)
            return
        session_id = params.get("session_id")
        if not isinstance(session_id, str):
            return
        key = self._alias.get(session_id, session_id)
        found = self._sessions.get(key)
        if found is None:
            return
        if method == GAP_METHOD:
            self._gap(found, params)
            return
        seq = params.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return
        epoch = params.get("replay_epoch")
        if isinstance(epoch, str) and epoch != found.epoch:
            found.epoch, found.cursor = epoch, 0
        if seq <= found.cursor:
            return  # already applied (a replay overlapping what we saw): exactly once
        found.cursor = seq
        if found.current != session_id:
            found.current = session_id
            self._alias[session_id] = key
        if method == SETTLED_METHOD:
            request_id = params.get("request_id")
            if isinstance(request_id, str):
                found.offers.pop(request_id, None)
                if params.get("outcome") == ANSWERED:
                    closed = ServerRequestCancelled(
                        session_id=session_id,
                        seq=seq,
                        request_id=request_id,
                        reason=CancelReason.ANSWERED_ELSEWHERE,
                    )
                    self._apply(found, closed)
            return
        model = EVENTS.get(method)
        if model is None or method in PRESENCE_METHODS:
            return  # presence and list updates: not for the session renderer
        try:
            event = model.model_validate({k: v for k, v in params.items() if k != "replay_epoch"})
        except ValidationError:
            log.warning("dropped a %s event that does not match its model", method)
            return
        self._apply(found, event)

    def _apply(self, found: _Session, event: EventBase) -> None:
        if isinstance(event, TurnStarted):
            found.generation = event.generation
            found.running = event.turn_id
            self._assign(found, event)
        elif isinstance(event, TurnDone):
            ended = RemoteTurn(
                event.session_id,
                event.turn_id,
                found.generation if found.running == event.turn_id else None,
                event.reason,
                event.failure,
            )
            found.ended[event.turn_id] = ended
            while len(found.ended) > 64:
                del found.ended[next(iter(found.ended))]
            if found.running == event.turn_id:
                found.running = None
            names = [event.turn_id]
            names += [a for a, turn in found.admissions.items() if turn == event.turn_id]
            for name in names:
                for future in found.waiters.pop(name, []):
                    if not future.done():
                        future.set_result(ended)
        found.kept.append(event)
        for listener in list(found.listeners):
            _call(listener, event)

    def _offer(self, params: Mapping[str, Any]) -> None:
        try:
            request = _REQUESTS.validate_python(params.get("request"))
        except ValidationError:
            log.warning("dropped a server request that does not match its model")
            return
        request_id = str(request.request_id)
        key = self._alias.get(str(request.session_id), str(request.session_id))
        found = self._sessions.get(key)
        if found is None:
            return
        generation = params.get("generation")
        seen = request_id in found.offers
        found.offers[request_id] = generation if isinstance(generation, int) else None
        if seen:
            return  # offered again after a reconnect: the card is already up
        for listener in list(found.requesters):
            _call(listener, request)

    def _gap(self, found: _Session, params: Mapping[str, Any]) -> None:
        epoch, last = params.get("replay_epoch"), params.get("last_seq")
        if isinstance(epoch, str):
            found.epoch = epoch
        if isinstance(last, int) and not isinstance(last, bool):
            found.cursor = last
        for listener in list(found.gaps):
            _call(listener, dict(params))

    # -- loss and reconnect -----------------------------------------------------------------------

    def _on_state(self, state: str, info: Mapping[str, Any]) -> None:
        if state in ("reconnecting", "closed", "failed"):
            self._fail_waiters(None)

    def _fail_waiters(self, only: _Session | None) -> None:
        for found in [only] if only is not None else list(self._sessions.values()):
            self._fail_session(found)

    def _fail_session(self, found: _Session, *, closing: bool = False) -> None:
        waiters, found.waiters = found.waiters, {}
        for turn_id, futures in waiters.items():
            for future in futures:
                if future.done():
                    continue
                if closing:
                    future.cancel()
                else:
                    future.set_exception(OutcomeUnknown(found.current, turn_id=turn_id))

    async def _resubscribe(self, channel: Channel) -> None:
        epoch = channel.ready_params.get("replay_epoch")
        for found in list(self._sessions.values()):
            params: dict[str, Any] = {"session_id": found.current}
            if found.epoch is not None:
                params["epoch"] = found.epoch
                params["cursor"] = found.cursor
            try:
                answer = await channel.request("session.subscribe", params, before_open=True)
            except RpcError as error:
                if error.code != SESSION_NOT_FOUND.code:
                    raise
                await self._rehost(channel, found)
                continue
            last = answer.get("last_seq")
            if isinstance(epoch, str):
                found.epoch = epoch
            if isinstance(last, int) and not isinstance(last, bool):
                found.cursor = max(found.cursor, last)

    async def _rehost(self, channel: Channel, found: _Session) -> None:
        """The daemon does not host the session any more (it restarted): resume it there."""
        try:
            answer = await channel.request(
                "session.resume", {"session_id": found.current}, before_open=True
            )
        except RpcError as error:
            if error.code != SESSION_NOT_FOUND.code:
                raise
            log.info("session %s is gone after the reconnect", found.current)
            self._sessions.pop(found.key, None)
            return
        result = SessionResumeResult.model_validate(answer)
        epoch = channel.ready_params.get("replay_epoch")
        gap = {
            "type": GAP_METHOD,
            "session_id": result.session_id,
            "replay_epoch": epoch if isinstance(epoch, str) else "",
            "last_seq": result.last_seq,
            "reason": "epoch_changed",
        }
        self._gap(found, gap)
        found.running = None
        found.generation = None


def _remove(items: list[Any], item: object) -> None:
    with contextlib.suppress(ValueError):
        items.remove(item)


# -- the blocking facade --------------------------------------------------------------------------

type Feed = queue.Queue[EventBase | ProtocolModel]


class RemoteHost:
    """``InProcessHost``'s blocking calls over a ``RemoteSessionService`` on its own loop thread.

    ``build`` runs on that loop and returns the started service (it makes the channel there).
    """

    def __init__(
        self,
        build: Callable[[], Coroutine[Any, Any, RemoteSessionService]],
        *,
        call_timeout: float | None = 60.0,
    ) -> None:
        self._build = build
        self.call_timeout = call_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._service: RemoteSessionService | None = None
        self._started = threading.Event()
        self._failure: BaseException | None = None
        self._stop: asyncio.Event | None = None

    # -- lifetime ---------------------------------------------------------------------------------

    def start(self) -> Self:
        thread = threading.Thread(target=self._run, name="indus-remote-host", daemon=True)
        self._thread = thread
        thread.start()
        self._started.wait()
        if self._failure is not None:
            raise self._failure
        return self

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        try:
            self._service = await self._build()
        except BaseException as exc:
            self._failure = exc
            self._started.set()
            return
        self._started.set()
        await self._stop.wait()
        with contextlib.suppress(Exception):
            await self._service.aclose()

    def stop(self, timeout: float = 10.0) -> None:
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(stop.set)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    @property
    def service(self) -> RemoteSessionService:
        if self._service is None:
            raise RuntimeError("the remote host is not started")
        return self._service

    # -- calls ------------------------------------------------------------------------------------

    def call[T](self, coro: Coroutine[Any, Any, T], *, timeout: float | None = -1.0) -> T:
        """Run ``coro`` on the host loop and wait (``-1``: the host's default timeout)."""
        loop = self._loop
        if loop is None or self._thread is None:
            coro.close()
            raise RuntimeError("the remote host is not started")
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError("a blocking call on the remote host's own loop would wait on itself")
        wait = self.call_timeout if timeout == -1.0 else timeout
        return asyncio.run_coroutine_threadsafe(coro, loop).result(wait)

    def run[T](self, fn: Callable[[], T], *, timeout: float | None = -1.0) -> T:
        async def step() -> T:
            return fn()

        return self.call(step(), timeout=timeout)

    def create(self, params: SessionCreateParams | None = None) -> SessionCreateResult:
        return self.call(self.service.create(params))

    def resume(self, session_id: str) -> SessionResumeResult:
        return self.call(self.service.resume(session_id))

    def submit(
        self, session_id: str, text: str, *, unattended: bool = False, single_query: bool = False
    ) -> PromptSubmitResult:
        return self.call(
            self.service.submit(session_id, text, unattended=unattended, single_query=single_query),
            timeout=None,
        )

    def wait(
        self, session_id: str, turn_id: str | None = None, *, timeout: float | None = -1.0
    ) -> RemoteTurn:
        return self.call(self.service.wait(session_id, turn_id), timeout=timeout)

    def interrupt(
        self, session_id: str, generation: int | None = None, *, hard: bool = False
    ) -> SessionInterruptResult:
        return self.call(self.service.interrupt(session_id, generation, hard=hard))

    def steer(self, session_id: str, text: str) -> SessionSteerResult:
        return self.call(self.service.steer(session_id, text))

    def redirect(self, session_id: str, text: str) -> SessionRedirectResult:
        return self.call(self.service.redirect(session_id, text))

    def answer_server_request(
        self, session_id: str, request_id: str, answer: Mapping[str, Any] | AnswerModel
    ) -> ServerRequestAnswerResult:
        return self.call(self.service.answer_server_request(session_id, request_id, answer))

    def events(self, session_id: str, since: int | None = None) -> list[EventBase]:
        return self.run(lambda: self.service.events(session_id, since))

    def close_session(self, session_id: str) -> None:
        self.call(self.service.close(session_id))

    def feed(self, session_id: str) -> tuple[Feed, Callable[[], None]]:
        """A queue that receives the session's events and server requests from now on, and the
        callable that stops it."""
        feed: Feed = queue.Queue()

        def attach() -> Callable[[], None]:
            off_events = self.service.subscribe(session_id, feed.put)
            off_requests = self.service.on_request(session_id, feed.put)

            def detach() -> None:
                off_events()
                off_requests()

            return detach

        detach = self.run(attach)
        return feed, lambda: self.run(detach)
