"""One event sink per session: callbacks in, typed protocol events out (P04-20).

The turn loop, the broker and the agent report what happens through ``EventSink``:

- ``emit(event)`` takes a ready event model; ``next_seq()`` hands out the session's strictly
  growing ``seq`` (the loop and the broker draw from the same counter);
- small helpers build the typed event from a callback-shaped call (``text``, ``reasoning``,
  ``tool_started``, ``tool_progress``, ``tool_completed``, ``status``, ``notice``);
- ``request(model)`` delivers a server request (approval, clarify, sudo, secret) to the request
  listeners — on the wire it is its own notification, not an event.

Every event is kept in a bounded replay buffer so a client that reconnects can ask for what it
missed (``since(seq)``), then handed to each listener in subscription order. A listener that
raises is logged and skipped: a broken screen must never end a turn.
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from indus.protocol.base import ProtocolModel
from indus.protocol.events import (
    EventBase,
    MessageDelta,
    Notice,
    NoticeLevel,
    ReasoningDelta,
    Status,
    StatusKind,
    ToolCompleted,
    ToolProgress,
    ToolStarted,
)

__all__ = ["DEFAULT_KEEP", "EventListener", "EventSink", "RequestListener", "as_sink"]

log = logging.getLogger(__name__)

DEFAULT_KEEP = 2000

type EventListener = Callable[[EventBase], object]
type RequestListener = Callable[[ProtocolModel], object]


class EventSink:
    """The event stream of one session (see the module notes)."""

    def __init__(
        self,
        session_id: str,
        *,
        listeners: tuple[EventListener, ...] = (),
        keep: int = DEFAULT_KEEP,
        start_seq: int = 0,
    ) -> None:
        if keep < 1:
            raise ValueError("keep must be at least 1")
        self.session_id = session_id
        self._counter = itertools.count(start_seq)
        self._counter_guard = threading.Lock()
        self._last_seq = start_seq - 1
        self._buffer: deque[EventBase] = deque(maxlen=keep)
        self._listeners: list[EventListener] = list(listeners)
        self._request_listeners: list[RequestListener] = []
        self.failures = 0

    # -- sequence ---------------------------------------------------------------------------------

    def next_seq(self) -> int:
        with self._counter_guard:
            value = next(self._counter)
            self._last_seq = value
            return value

    @property
    def last_seq(self) -> int:
        """The newest seq handed out; -1 before the first."""
        return self._last_seq

    # -- listeners --------------------------------------------------------------------------------

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        """Add an event listener; the returned callable removes it again."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def on_request(self, listener: RequestListener) -> Callable[[], None]:
        """Add a server-request listener; the returned callable removes it again."""
        self._request_listeners.append(listener)

        def remove() -> None:
            if listener in self._request_listeners:
                self._request_listeners.remove(listener)

        return remove

    # -- delivery ---------------------------------------------------------------------------------

    def emit(self, event: EventBase) -> None:
        """Keep ``event`` for replay and hand it to every listener (failures are logged)."""
        self._buffer.append(event)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                self.failures += 1
                log.warning(
                    "an event listener failed on %s #%d of session %s",
                    type(event).__name__,
                    event.seq,
                    self.session_id,
                    exc_info=True,
                )

    def __call__(self, event: EventBase) -> None:
        self.emit(event)

    def request(self, request: ProtocolModel) -> None:
        """Deliver a server request to the request listeners (failures are logged)."""
        if not self._request_listeners:
            log.debug("server request for session %s has no listener yet", self.session_id)
        for listener in list(self._request_listeners):
            try:
                listener(request)
            except Exception:
                self.failures += 1
                log.warning(
                    "a server-request listener failed for session %s",
                    self.session_id,
                    exc_info=True,
                )

    def since(self, seq: int | None = None) -> list[EventBase]:
        """Kept events with a seq above ``seq`` (all kept events for ``None``), oldest first."""
        if seq is None:
            return list(self._buffer)
        return [e for e in self._buffer if e.seq > seq]

    # -- callback-shaped helpers ------------------------------------------------------------------

    def make(self, model: type[EventBase], **fields: Any) -> EventBase:
        """Build ``model`` for this session with the next seq."""
        return model(session_id=self.session_id, seq=self.next_seq(), **fields)

    def send(self, model: type[EventBase], **fields: Any) -> EventBase | None:
        """Build and emit ``model``; a field that does not fit the model is logged, never raised."""
        try:
            event = self.make(model, **fields)
        except Exception:
            self.failures += 1
            log.warning("could not build a %s event", model.__name__, exc_info=True)
            return None
        self.emit(event)
        return event

    def text(self, turn_id: str, text: str) -> EventBase | None:
        return self.send(MessageDelta, turn_id=turn_id, text=text) if text else None

    def reasoning(self, turn_id: str, text: str) -> EventBase | None:
        return self.send(ReasoningDelta, turn_id=turn_id, text=text) if text else None

    def tool_started(
        self, turn_id: str, call_id: str, name: str, arguments: Mapping[str, Any] | None = None
    ) -> EventBase | None:
        return self.send(
            ToolStarted,
            turn_id=turn_id,
            call_id=call_id,
            name=name,
            arguments=dict(arguments or {}),
        )

    def tool_progress(
        self, turn_id: str, call_id: str, message: str, fraction: float | None = None
    ) -> EventBase | None:
        return self.send(
            ToolProgress, turn_id=turn_id, call_id=call_id, message=message, fraction=fraction
        )

    def tool_completed(
        self,
        turn_id: str,
        call_id: str,
        name: str,
        *,
        ok: bool,
        duration_ms: int,
        result: str,
    ) -> EventBase | None:
        return self.send(
            ToolCompleted,
            turn_id=turn_id,
            call_id=call_id,
            name=name,
            ok=ok,
            duration_ms=max(0, duration_ms),
            result=result,
        )

    def status(self, kind: StatusKind, text: str = "") -> EventBase | None:
        return self.send(Status, kind=kind, text=text)

    def notice(self, level: NoticeLevel, text: str, *, key: str | None = None) -> EventBase | None:
        return self.send(Notice, level=level, text=text, key=key)


def as_sink(session_id: str, sink: EventSink | EventListener | None) -> EventSink:
    """``sink`` itself when it already is an ``EventSink`` for ``session_id``; otherwise a new
    sink for the session with ``sink`` (a plain callable) as its first listener."""
    if isinstance(sink, EventSink):
        if sink.session_id != session_id:
            raise ValueError(
                f"the event sink belongs to session {sink.session_id}, not {session_id}"
            )
        return sink
    return EventSink(session_id, listeners=(sink,) if sink is not None else ())
