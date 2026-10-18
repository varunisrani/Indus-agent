"""A fake session host for REPL and slash-command unit tests (P06-04).

``FakeHost`` has the blocking methods of ``InProcessHost`` the classic CLI uses. It records every
call in ``calls`` (name and arguments), answers from settable fields, and lets the test push
events and server requests into the session's feed with ``emit`` (sequence numbers are filled in).
``wait_for(name)`` blocks until a call with that name has been made, so a test on another thread
can synchronise without sleeping.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from indus.protocol.events import (
    EventBase,
    ExitReason,
    MessageDelta,
    TurnDone,
    TurnStarted,
)
from indus.protocol.methods import (
    PromptSubmitResult,
    ProtocolError,
    RedirectOutcome,
    ServerRequestAnswerResult,
    SessionCreateParams,
    SessionCreateResult,
    SessionInterruptResult,
    SessionRedirectResult,
    SessionResumeResult,
    SessionSteerResult,
)
from indus.protocol.requests_port import AnswerModel

SESSION = "20260101_090000_abcdef"
WAIT = 5.0


@dataclass
class Call:
    name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any] = field(default_factory=dict[str, Any])


class FakeHost:
    """Records calls; answers from its fields; feeds what the test emits."""

    def __init__(self, session_id: str = SESSION) -> None:
        self.session_id = session_id
        self.calls: list[Call] = []
        self.steer_accepts = True
        self.redirect_outcome = RedirectOutcome.REDIRECTED
        self.interrupted = True
        self.submit_error: ProtocolError | None = None
        self.answer_error: ProtocolError | None = None
        self.on_submit: Callable[[str, str], None] | None = None
        self._turns = 0
        self._seq = 0
        self._feeds: dict[str, queue.Queue[Any]] = {}
        self._cond = threading.Condition()

    # -- recording ------------------------------------------------------------------------------

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        with self._cond:
            self.calls.append(Call(name, args, kwargs))
            self._cond.notify_all()

    def named(self, name: str) -> list[Call]:
        with self._cond:
            return [c for c in self.calls if c.name == name]

    def wait_for(self, name: str, count: int = 1, timeout: float = WAIT) -> list[Call]:
        """Block until ``count`` calls named ``name`` were made; returns them."""
        with self._cond:
            ok = self._cond.wait_for(
                lambda: sum(1 for c in self.calls if c.name == name) >= count, timeout
            )
            if not ok:
                raise AssertionError(f"no {count} call(s) to {name}; calls: {self.calls}")
            return [c for c in self.calls if c.name == name]

    # -- feed -----------------------------------------------------------------------------------

    def feed(self, session_id: str) -> tuple[queue.Queue[Any], Callable[[], None]]:
        self._record("feed", session_id)
        feed: queue.Queue[Any] = queue.Queue()
        self._feeds[session_id] = feed

        def detach() -> None:
            self._record("detach", session_id)
            if self._feeds.get(session_id) is feed:
                del self._feeds[session_id]

        return feed, detach

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def emit(self, item: Any, session_id: str | None = None) -> None:
        """Put ``item`` (an event, a request, or a dict to build an event from) in the feed."""
        sid = session_id or self.session_id
        self._feeds[sid].put(item)

    def started(self, turn_id: str = "t1", generation: int = 1) -> TurnStarted:
        return TurnStarted(
            session_id=self.session_id, seq=self.next_seq(), turn_id=turn_id, generation=generation
        )

    def delta(self, text: str, turn_id: str = "t1") -> MessageDelta:
        return MessageDelta(
            session_id=self.session_id, seq=self.next_seq(), turn_id=turn_id, text=text
        )

    def done(self, turn_id: str = "t1", reason: ExitReason = ExitReason.COMPLETED) -> TurnDone:
        return TurnDone(
            session_id=self.session_id, seq=self.next_seq(), turn_id=turn_id, reason=reason
        )

    def event(self, model: type[EventBase], **fields: Any) -> EventBase:
        fields.setdefault("session_id", self.session_id)
        fields.setdefault("seq", self.next_seq())
        return model(**fields)

    # -- the host API ---------------------------------------------------------------------------

    def create(self, params: SessionCreateParams | None = None) -> SessionCreateResult:
        self._record("create", params)
        return SessionCreateResult(session_id=self.session_id, created=True)

    def resume(self, session_id: str) -> SessionResumeResult:
        self._record("resume", session_id)
        return SessionResumeResult(session_id=session_id, message_count=0, last_seq=0)

    def submit(self, session_id: str, text: str) -> PromptSubmitResult:
        self._record("submit", session_id, text)
        if self.submit_error is not None:
            error, self.submit_error = self.submit_error, None
            raise error
        self._turns += 1
        if self.on_submit is not None:
            self.on_submit(session_id, text)
        return PromptSubmitResult(turn_id=f"t{self._turns}", generation=self._turns)

    def interrupt(
        self, session_id: str, generation: int | None = None, *, hard: bool = False
    ) -> SessionInterruptResult:
        self._record("interrupt", session_id, generation, hard=hard)
        return SessionInterruptResult(interrupted=self.interrupted)

    def steer(self, session_id: str, text: str) -> SessionSteerResult:
        self._record("steer", session_id, text)
        if self.steer_accepts:
            return SessionSteerResult(queued=True, ticket="st_1")
        return SessionSteerResult(queued=False)

    def redirect(self, session_id: str, text: str) -> SessionRedirectResult:
        self._record("redirect", session_id, text)
        return SessionRedirectResult(outcome=self.redirect_outcome)

    def answer_server_request(
        self, session_id: str, request_id: str, answer: Mapping[str, Any] | AnswerModel
    ) -> ServerRequestAnswerResult:
        self._record("answer", session_id, request_id, answer)
        if self.answer_error is not None:
            raise self.answer_error
        return ServerRequestAnswerResult(accepted=True)

    def events(self, session_id: str, since: int | None = None) -> list[EventBase]:
        self._record("events", session_id, since)
        return []

    def close_session(self, session_id: str) -> None:
        self._record("close_session", session_id)
