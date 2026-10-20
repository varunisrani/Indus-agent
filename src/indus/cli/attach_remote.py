"""The CLI's attach backend over the shared-daemon client (P17-15..17 under P17-18).

``attach`` drives the daemon through two small seams, ``AttachBackend`` (ensure, then connect) and
``AttachedService`` (the calls a classic chat makes). ``DaemonBackend`` is the real one:

- ``ensure`` is P17-15's ``service.gateway_ensure.ensure(home, env, mode="start")``: a live daemon
  for this profile, started detached when none answers (launch-option env vars never reach it).
  A verdict that is not attachable, or a daemon without an attach endpoint, is
  ``AttachUnavailable`` with one credential-free line; nothing runs locally.
- ``connect`` builds the attach channel (``Channel`` over ``home_dialer``: a fresh single-use
  ticket per dial, automatic recoveries rationed by a ``RecoverBudget``) and the remote session
  service (``RemoteSessionService`` with the prepared-submission ``Journal`` of this home) on the
  caller's loop, and wraps it in ``RemoteAttached``.

``RemoteAttached`` maps the CLI's calls onto the remote service:

- ``create`` sends what the wire carries of the creation policy (``cwd``, ``model``,
  ``toolsets``; ``attach`` refuses a launch that names anything else before it connects).
  ``resume`` sends the id only: a changed frozen flag is refused by the read-only check in
  ``attach`` first. The surface is the channel's (``cli``).
- ``submit`` / ``wait``: the admission id a submit answers is waited on; the result carries the
  reply streamed after the turn's last tool call (the daemon sends deltas, not a final message).
  A lost transport is ``outcome_unknown`` (never resent; the journal keeps the entry). When the
  wait is lost while our input was still queued behind a lost turn of the session, the reason is
  ``blocked_by_unknown`` instead (the input stays queued in the daemon).
- ``pending`` lists the session's not-yet-finished admissions from the runtime ledger through a
  read-only store connection (it never creates or migrates anything, R03.16);
  ``discard_unknown`` is ``admission.discard`` (generation checked by the daemon).
- ``own_generation`` is the generation our admission's turn started with, so Ctrl-C interrupts
  exactly that turn and never another surface's.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from indus.cli.attach import AttachUnavailable
from indus.protocol.events import (
    EventBase,
    ExitReason,
    MessageDelta,
    ToolStarted,
    TurnStarted,
)
from indus.protocol.methods import (
    PromptSubmitResult,
    SessionCreateParams,
    SessionCreateResult,
    SessionResumeResult,
)

if TYPE_CHECKING:
    from indus.foundation.scope import Scope
    from indus.protocol.attach import AdmissionDiscardResult
    from indus.service.gateway_ensure import Endpoint, Ensured
    from indus.service.remote.channel import ChannelLimits, Dial
    from indus.service.remote.host import RemoteSessionService, RemoteTurn

__all__ = ["CARRIED_POLICY", "AttachedTurn", "DaemonBackend", "RemoteAttached", "RemoteRefusal"]

log = logging.getLogger(__name__)

CARRIED_POLICY: Final = frozenset({"cwd", "model", "toolsets"})
"""Creation-policy fields ``session.create`` carries today (the rest is P17-25's)."""
_KEPT_TURNS: Final = 64


class RemoteRefusal(Exception):
    """A remote outcome the CLI acts on by its ``reason`` (``outcome_unknown``,
    ``blocked_by_unknown``)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class AttachedTurn:
    """How a remote turn ended, in the shape the finite runs read."""

    session_id: str
    turn_id: str
    generation: int | None
    exit_reason: ExitReason
    final_text: str | None
    failure: Any = None
    api_calls: int = 0
    """Not reported by the daemon yet."""


@dataclass
class _Replies:
    """Reply text per turn of one session: deltas after the turn's last tool call."""

    text: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def __call__(self, event: EventBase) -> None:
        turn = getattr(event, "turn_id", None)
        if not isinstance(turn, str):
            return
        if isinstance(event, TurnStarted | ToolStarted):
            self.text[turn] = []
            while len(self.text) > _KEPT_TURNS:
                del self.text[next(iter(self.text))]
        elif isinstance(event, MessageDelta):
            self.text.setdefault(turn, []).append(event.text)

    def of(self, turn: str) -> str | None:
        parts = self.text.get(turn)
        return "".join(parts) if parts else None


def _verdict(found: Ensured) -> str:
    detail = (found.detail or "").splitlines()[0] if found.detail else ""
    reason = found.reason or found.state
    return f"the gateway daemon is not available ({reason}: {detail})" if detail else reason


class DaemonBackend:
    """``ensure`` + ``connect`` over P17-15..17 (see the module notes). ``ensure_fn``, ``dial``
    (instead of the home's control socket) and ``limits`` are seams for tests."""

    def __init__(
        self,
        *,
        ensure_fn: Callable[..., Ensured] | None = None,
        dial: Dial | None = None,
        limits: ChannelLimits | None = None,
    ) -> None:
        self._ensure_fn = ensure_fn
        self._dial = dial
        self._limits = limits

    def ensure(self, scope: Scope, *, timeout: float) -> Endpoint:
        from indus.service.gateway_ensure import ensure

        run = self._ensure_fn or ensure
        found = run(scope.home, scope.env.to_dict(), mode="start", timeout=timeout)
        if not found.attachable or found.endpoint is None:
            raise AttachUnavailable(_verdict(found))
        if not found.endpoint.ws_url:
            raise AttachUnavailable(
                "the gateway daemon has no attach endpoint (restart it with "
                "`indus gateway restart`)"
            )
        return found.endpoint

    async def connect(self, scope: Scope, endpoint: Any, *, surface: str) -> RemoteAttached:
        from indus.service.gateway_ensure import RecoverBudget
        from indus.service.remote.channel import Channel, DialRefused, home_dialer
        from indus.service.remote.host import RemoteSessionService
        from indus.service.remote.journal import Journal

        del endpoint  # every dial asks the control socket again (fresh ticket, current ids)
        dial = self._dial or home_dialer(
            scope.home, env=scope.env.to_dict(), budget=RecoverBudget()
        )
        channel = Channel(dial, surface=surface, features=("server_requests",), limits=self._limits)
        service = RemoteSessionService(channel, Journal(scope.home), surface=surface)
        try:
            await service.start()
        except DialRefused as refused:
            await service.aclose()
            raise AttachUnavailable(f"the attach connection was refused ({refused})") from None
        return RemoteAttached(service, scope)


class RemoteAttached:
    """``attach.AttachedService`` over a started ``RemoteSessionService``."""

    def __init__(self, service: RemoteSessionService, scope: Scope) -> None:
        self.service = service
        self.scope = scope
        self._replies: dict[str, _Replies] = {}

    def _follow(self, session_id: str) -> None:
        if session_id not in self._replies:
            replies = _Replies()
            self._replies[session_id] = replies
            self.service.subscribe(session_id, replies)

    async def create(
        self, params: SessionCreateParams, *, source: str, policy: Mapping[str, Any]
    ) -> SessionCreateResult:
        del source  # the daemon records the connection's surface
        carried = {k: v for k, v in policy.items() if k in CARRIED_POLICY and v is not None}
        wire = params.model_copy(update=carried)
        result = await self.service.create(wire)
        self._follow(result.session_id)
        return result

    async def resume(
        self, session_id: str, *, policy: Mapping[str, Any] | None
    ) -> SessionResumeResult:
        del policy  # checked read-only before connecting (P17-25 carries it on the wire)
        result = await self.service.resume(session_id)
        self._follow(session_id)
        return result

    async def submit(
        self, session_id: str, text: str, *, unattended: bool = False
    ) -> PromptSubmitResult:
        from indus.service.remote.host import OutcomeUnknown

        # ``unattended`` (``-z``): the daemon auto-approves this input's turn, as the in-process
        # run does; any other question still detaches the finite run (exit 3).
        try:
            return await self.service.submit(session_id, text, unattended=unattended)
        except OutcomeUnknown as lost:
            raise RemoteRefusal("outcome_unknown", lost.args[0] if lost.args else "") from None

    async def wait(self, session_id: str, turn_id: str) -> AttachedTurn:
        from indus.service.remote.host import OutcomeUnknown

        try:
            ended: RemoteTurn = await self.service.wait(session_id, turn_id)
        except OutcomeUnknown as lost:
            if await self._queued_behind_unknown(session_id, turn_id):
                raise RemoteRefusal(
                    "blocked_by_unknown", "the input waits behind a lost turn"
                ) from None
            raise RemoteRefusal("outcome_unknown", lost.args[0] if lost.args else "") from None
        replies = self._replies.get(session_id)
        return AttachedTurn(
            session_id=ended.session_id,
            turn_id=ended.turn_id,
            generation=ended.generation,
            exit_reason=ended.reason,
            final_text=replies.of(ended.turn_id) if replies is not None else None,
            failure=ended.failure,
        )

    def own_generation(self, session_id: str, turn_id: str) -> int | None:
        found = self.service.turn_of(session_id, turn_id)
        return found[1] if found is not None else None

    async def _queued_behind_unknown(self, session_id: str, admission_id: str) -> bool:
        rows = await self.pending(session_id)
        mine = next((row for row in rows if row["admission_id"] == admission_id), None)
        return (
            mine is not None
            and mine["state"] == "queued"
            and any(row["state"] == "unknown" for row in rows)
        )

    async def interrupt(
        self, session_id: str, generation: int | None, *, hard: bool = False
    ) -> Any:
        return await self.service.interrupt(session_id, generation, hard=hard)

    async def steer(self, session_id: str, text: str) -> Any:
        return await self.service.steer(session_id, text)

    async def redirect(self, session_id: str, text: str) -> Any:
        return await self.service.redirect(session_id, text)

    async def answer_server_request(self, session_id: str, request_id: str, answer: Any) -> Any:
        return await self.service.answer_server_request(session_id, request_id, answer)

    def subscribe(self, session_id: str, listener: Callable[[Any], None]) -> Callable[[], None]:
        return self.service.subscribe(session_id, listener)

    def on_request(self, session_id: str, listener: Callable[[Any], None]) -> Callable[[], None]:
        return self.service.on_request(session_id, listener)

    async def pending(self, session_id: str) -> list[dict[str, Any]]:
        """The session's queued, started and unknown admissions (read-only ledger read)."""
        from indus.foundation.blocking import run_blocking

        ids = {session_id}
        with contextlib.suppress(Exception):  # a session this client does not hold: its id only
            ids |= {self.service.key_of(session_id), self.service.current_id(session_id)}
        return await run_blocking(lambda: _pending_rows(self.scope, ids), label="admissions")

    async def discard_unknown(
        self, session_id: str, admission_id: str, generation: int
    ) -> AdmissionDiscardResult:
        return await self.service.discard_unknown(session_id, admission_id, generation)

    def on_gap(
        self, session_id: str, listener: Callable[[Mapping[str, Any]], object]
    ) -> Callable[[], None]:
        """``listener`` hears the session's replay gaps (a reconnect to a restarted daemon)."""
        return self.service.on_gap(session_id, listener)

    async def aclose(self) -> None:
        await self.service.aclose()


def _pending_rows(scope: Scope, ids: set[str]) -> list[dict[str, Any]]:
    import sqlite3

    from indus.foundation.errors import StoreUnavailable
    from indus.state import browse
    from indus.state import runtime_ledger as ledger

    try:
        store = browse.open_readonly(scope)
    except (StoreUnavailable, sqlite3.Error):
        return []
    try:
        view = _ReadOnly(store)
        rows: list[ledger.AdmissionRow] = []
        for sid in sorted(ids):
            try:
                rows.extend(ledger.pending(view.as_handle(), sid))
            except sqlite3.Error:
                log.warning("the runtime ledger could not be read", exc_info=True)
        rows.sort(key=lambda row: row.seq)
        return [
            {
                "admission_id": row.admission_id,
                "state": row.state,
                "generation": row.generation or 0,
                "text": ledger.payload_text(row),
            }
            for row in rows
        ]
    finally:
        store.close()


class _ReadOnly:
    """What ``runtime_ledger.pending`` reads through: a read-only connection."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def read(self) -> Any:
        return self._store.read()

    def as_handle(self) -> Any:
        return self
