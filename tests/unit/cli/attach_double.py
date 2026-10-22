"""TEST DOUBLE of the CLI's attach seams (``attach.AttachBackend`` / ``attach.AttachedService``).

The real backend is ``indus.cli.attach_remote.DaemonBackend`` (P17-15 ensure, P17-16 channel,
P17-17 remote session service; tested over the daemon's real WebSocket endpoint in
``test_attach_remote.py`` and end to end against a real daemon in
``tests/integration/cli/test_attach_daemon.py``). This in-memory double lets ``test_attach.py``
drive every branch of ``attach`` (exit codes, refusals, Ctrl-C, ``/discard``) without a daemon.
It answers like ``RemoteAttached``:

- ``create(params, *, source, policy)`` / ``resume(session_id, *, policy)``; a changed frozen value
  raises an error whose ``reason`` is ``policy_conflict`` and whose ``fields`` lists the fields;
- ``submit(session_id, text, *, unattended=False, single_query=False)`` (``turn_id`` = the
  admission id);
- ``wait(session_id, turn_id)``; raises with ``reason`` ``outcome_unknown`` /
  ``blocked_by_unknown``;
- ``pending(session_id)`` rows (``admission_id``, ``state``, ``generation``, ``text``);
  ``discard_unknown(session_id, admission_id, generation)`` answers a ``stale_generation`` code for
  a stale generation; ``own_generation(session_id, turn_id)``;
- ``aclose()`` detaches only.

Nothing here is product code.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from indus.core.exit_reasons import Failure
from indus.core.turn_state import TurnResult
from indus.protocol.events import ExitReason, MessageDelta, TurnDone, TurnStarted
from indus.protocol.methods import (
    PromptSubmitResult,
    ServerRequestAnswerResult,
    SessionCreateParams,
    SessionCreateResult,
    SessionInterruptResult,
    SessionRedirectResult,
    SessionResumeResult,
    SessionSteerResult,
    SubmitStatus,
)


@dataclass(frozen=True)
class StubEndpoint:
    ws_url: str = "ws://127.0.0.1:47012/ws"
    profile_id: str = "work"
    instance_id: str = "inst-1"
    code_version: str = "0.0.0"


class RemoteFailure(Exception):
    """A refusal or loss as the remote host would raise it."""

    def __init__(self, reason: str, message: str = "", **extra: Any) -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or reason
        for key, value in extra.items():
            setattr(self, key, value)


@dataclass(frozen=True)
class Stale:
    """The fences' "aimed at another generation" answer."""

    code: str = "stale_generation"


def turn_result(
    session_id: str,
    turn_id: str,
    text: str | None = "done",
    *,
    reason: ExitReason | None = None,
    failure: str | None = None,
) -> TurnResult:
    return TurnResult(
        session_id=session_id,
        turn_id=turn_id,
        generation=1,
        exit_reason=reason or ExitReason.COMPLETED,
        final_text=text,
        rows=[],
        current_turn_user_index=0,
        api_calls=1,
        failure=Failure(failure, False) if failure else None,
    )


@dataclass
class StubRemote:
    """An in-memory remote session service (see the module notes)."""

    reply: str = "hello from the daemon"
    known: set[str] = field(default_factory=lambda: {"s-old"})
    pending_rows: list[dict[str, Any]] = field(default_factory=lambda: [])
    wait_with: Callable[[str, str], Any] | None = None
    conflict: tuple[str, ...] = ()
    calls: list[tuple[str, Any]] = field(default_factory=lambda: [])
    listeners: dict[str, list[Callable[[Any], None]]] = field(default_factory=lambda: {})
    askers: dict[str, list[Callable[[Any], None]]] = field(default_factory=lambda: {})
    answers: list[Any] = field(default_factory=lambda: [])
    answered: threading.Event = field(default_factory=threading.Event)
    closed: bool = False
    released: threading.Event = field(default_factory=threading.Event)
    generation: int = 0
    seq: int = 0
    queued_behind: int | None = None
    """When set, a submit is queued behind another surface's running turn of that generation
    (and never starts here)."""

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    async def create(
        self, params: SessionCreateParams, *, source: str, policy: Mapping[str, Any]
    ) -> SessionCreateResult:
        self.calls.append(("create", {"cwd": params.cwd, "source": source, "policy": dict(policy)}))
        sid = f"s-new-{len(self.calls)}"
        self.known.add(sid)
        return SessionCreateResult(session_id=sid, created=True)

    async def resume(
        self, session_id: str, *, policy: Mapping[str, Any] | None
    ) -> SessionResumeResult:
        self.calls.append(("resume", {"session_id": session_id, "policy": policy}))
        if self.conflict:
            raise RemoteFailure("policy_conflict", "frozen", fields=list(self.conflict))
        return SessionResumeResult(session_id=session_id, message_count=0, last_seq=0)

    async def submit(
        self, session_id: str, text: str, *, unattended: bool = False, single_query: bool = False
    ) -> PromptSubmitResult:
        self.generation += 1
        turn = f"adm-{self.generation}"
        self.calls.append(
            (
                "submit",
                {
                    "text": text,
                    "unattended": unattended,
                    "single_query": single_query,
                    "turn": turn,
                },
            )
        )
        for listener in self.listeners.get(session_id, []):
            listener(
                TurnStarted(
                    session_id=session_id,
                    seq=self._next(),
                    turn_id=turn,
                    generation=self.generation,
                )
            )
        if self.queued_behind is not None:
            return PromptSubmitResult(
                turn_id=turn, generation=self.queued_behind, status=SubmitStatus.QUEUED
            )
        return PromptSubmitResult(turn_id=turn, generation=self.generation)

    async def wait(self, session_id: str, turn_id: str) -> TurnResult:
        self.calls.append(("wait", turn_id))
        if self.wait_with is not None:
            got = self.wait_with(session_id, turn_id)
            while asyncio.iscoroutine(got) or isinstance(got, asyncio.Future):
                got = await got
            return got
        return turn_result(session_id, turn_id, self.reply)

    def interrupt(
        self, session_id: str, generation: int | None, *, hard: bool = False
    ) -> SessionInterruptResult:
        self.calls.append(("interrupt", generation))
        self.released.set()
        return SessionInterruptResult(interrupted=True)

    def steer(self, session_id: str, text: str) -> SessionSteerResult:
        self.calls.append(("steer", text))
        return SessionSteerResult(queued=True)

    def redirect(self, session_id: str, text: str) -> SessionRedirectResult:
        from indus.protocol.methods import RedirectOutcome

        self.calls.append(("redirect", text))
        return SessionRedirectResult(outcome=RedirectOutcome.REDIRECTED)

    def answer_server_request(
        self, session_id: str, request_id: str, answer: Any
    ) -> ServerRequestAnswerResult:
        self.calls.append(("answer", request_id))
        self.answers.append(answer)
        self.answered.set()
        return ServerRequestAnswerResult(accepted=True)

    def subscribe(self, session_id: str, listener: Callable[[Any], None]) -> Callable[[], None]:
        self.listeners.setdefault(session_id, []).append(listener)

        def off() -> None:
            self.calls.append(("unsubscribe", session_id))
            if listener in self.listeners.get(session_id, []):
                self.listeners[session_id].remove(listener)

        return off

    def on_request(self, session_id: str, listener: Callable[[Any], None]) -> Callable[[], None]:
        self.askers.setdefault(session_id, []).append(listener)
        return lambda: None

    def ask(self, session_id: str, request: Any) -> None:
        for listener in list(self.askers.get(session_id, [])):
            listener(request)

    def emit(self, session_id: str, turn_id: str, text: str) -> None:
        for listener in list(self.listeners.get(session_id, [])):
            listener(
                MessageDelta(session_id=session_id, seq=self._next(), turn_id=turn_id, text=text)
            )
            listener(
                TurnDone(
                    session_id=session_id,
                    seq=self._next(),
                    turn_id=turn_id,
                    reason=ExitReason.COMPLETED,
                )
            )

    async def pending(self, session_id: str) -> list[dict[str, Any]]:
        self.calls.append(("pending", session_id))
        return list(self.pending_rows)

    async def discard_unknown(self, session_id: str, admission_id: str, generation: int) -> Any:
        self.calls.append(("discard", (admission_id, generation)))
        row = next((r for r in self.pending_rows if r["admission_id"] == admission_id), None)
        if row is None or row.get("current_generation", row["generation"]) != generation:
            return Stale()
        self.pending_rows.remove(row)
        return {"state": "terminal"}

    def own_generation(self, session_id: str, turn_id: str) -> int | None:
        self.calls.append(("own_generation", turn_id))
        return None if self.queued_behind is not None else self.generation or None

    async def aclose(self) -> None:
        self.calls.append(("aclose", None))
        self.closed = True


@dataclass
class StubBackend:
    """``ensure`` + ``connect`` over a ``StubRemote`` (the double of ``DaemonBackend``)."""

    remote: StubRemote = field(default_factory=StubRemote)
    ensure_error: Exception | None = None
    ensured: list[Path] = field(default_factory=lambda: [])
    surfaces: list[str] = field(default_factory=lambda: [])

    def ensure(self, scope: Any, *, timeout: float) -> StubEndpoint:
        self.ensured.append(scope.home)
        if self.ensure_error is not None:
            raise self.ensure_error
        return StubEndpoint()

    connect_error: Exception | None = None

    async def connect(self, scope: Any, endpoint: Any, *, surface: str) -> StubRemote:
        self.surfaces.append(surface)
        if self.connect_error is not None:
            raise self.connect_error
        return self.remote
