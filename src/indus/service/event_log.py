"""The daemon's event log: per-session sequence numbers, a replay epoch and bounded replay (P17-06).

Every event the daemon sends for a session passes through ``EventLog.append``. The log numbers it
(``seq``: 1, 2, 3 … per session, never reused while the daemon lives) and stamps the daemon's
``replay_epoch``, a random token made once per daemon start. The newest events of each session are
kept as frozen JSON so a client that lost its socket can ask ``since(session, epoch, cursor)`` and
receive exactly the events after ``cursor``, in order. When that cannot be answered truthfully the
answer is a ``ReplayGap`` and the client reloads a snapshot instead:

- ``epoch_changed``: the client's epoch belongs to another daemon run (its counters restarted);
- ``evicted``: some event after the cursor was dropped from memory (or was too big to keep);
- ``ahead``: the cursor is past anything this daemon numbered.

Memory bounds (``ReplayLimits``): per session at most 512 events and 4 MiB of payload, at most 64
sessions, at most 64 MiB in the process. The process cap takes events from the session that has
been quiet longest; a 65th session drops the quietest session's whole buffer. A session's counter
and "evicted through" watermark outlive its buffer, so a returning session keeps counting up and a
client holding an old cursor sees a gap, never a silent hole.

Streaming text (``message.delta``, ``reasoning.delta``) is joined before numbering: pieces of the
same kind and turn that arrive within ``coalesce_window`` (33 ms) of the first held piece become
one event whose text is their concatenation. Any other event for the session flushes the held
piece first, so order is kept. ``flush_due`` (driven by ``run_flusher`` in the daemon) flushes a
held piece once its window is over.

Listeners added with ``add_listener`` receive each numbered event (``LoggedEvent``) after it is
kept; deliveries follow seq order when appends come from one thread (the daemon loop). A listener
that raises is logged and skipped. Listeners must not append to the log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from indus.foundation.clock import Clock
from indus.protocol.events import EventBase

__all__ = [
    "COALESCE_WINDOW_S",
    "STREAMING_TYPES",
    "EventLog",
    "LoggedEvent",
    "Replay",
    "ReplayGap",
    "ReplayLimits",
    "new_replay_epoch",
]

log = logging.getLogger(__name__)

COALESCE_WINDOW_S = 0.033
"""How long a streamed text piece may wait for the next one before it is numbered."""

STREAMING_TYPES = frozenset({"message.delta", "reasoning.delta"})
"""Event types whose consecutive pieces are joined."""

_MIB = 1024 * 1024

type GapReason = Literal["epoch_changed", "evicted", "ahead"]
type LogListener = Callable[[LoggedEvent], object]


def new_replay_epoch() -> str:
    """A fresh opaque replay epoch (one per daemon start)."""
    return secrets.token_hex(16)


@dataclass(frozen=True, slots=True)
class ReplayLimits:
    """How much replay the daemon keeps in memory."""

    events_per_session: int = 512
    bytes_per_session: int = 4 * _MIB
    sessions: int = 64
    bytes_process: int = 64 * _MIB

    def __post_init__(self) -> None:
        for name in ("events_per_session", "bytes_per_session", "sessions", "bytes_process"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


@dataclass(frozen=True, slots=True)
class LoggedEvent:
    """One numbered event as the log keeps it (payload frozen as JSON bytes)."""

    session_id: str
    seq: int
    replay_epoch: str
    payload: bytes = field(repr=False)

    @property
    def size(self) -> int:
        """Bytes this event counts against the caps."""
        return len(self.payload)

    @property
    def type(self) -> str:
        return str(self.to_wire().get("type", ""))

    def to_wire(self) -> dict[str, Any]:
        """The event as a client receives it: its fields plus ``seq`` and ``replay_epoch``."""
        event = _thaw(self.payload)
        event["seq"] = self.seq
        event["replay_epoch"] = self.replay_epoch
        return event


@dataclass(frozen=True, slots=True)
class Replay:
    """The events after a cursor, oldest first."""

    session_id: str
    replay_epoch: str
    events: tuple[dict[str, Any], ...]
    last_seq: int
    """The newest seq the session has (the cursor to hold after applying ``events``)."""


@dataclass(frozen=True, slots=True)
class ReplayGap:
    """The log cannot replay from the cursor; the client reloads a snapshot."""

    session_id: str
    replay_epoch: str
    last_seq: int
    reason: GapReason

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "session.replay_gap",
            "session_id": self.session_id,
            "replay_epoch": self.replay_epoch,
            "last_seq": self.last_seq,
            "reason": self.reason,
        }


@dataclass(slots=True)
class _Held:
    """A streamed text piece waiting for its neighbours."""

    type: str
    turn_id: object
    base: dict[str, Any]
    parts: list[str]
    started: float


@dataclass(slots=True)
class _Ring:
    events: deque[LoggedEvent] = field(default_factory=deque[LoggedEvent])
    bytes: int = 0


def _freeze(event: Mapping[str, Any]) -> bytes:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8", errors="surrogatepass"
    )


def _thaw(payload: bytes) -> dict[str, Any]:
    value = json.loads(payload.decode("utf-8", errors="surrogatepass"))
    assert isinstance(value, dict)
    return dict(value)  # pyright: ignore[reportUnknownArgumentType]


def _as_mapping(session_id: str, event: EventBase | Mapping[str, Any]) -> dict[str, Any]:
    data = event.to_wire() if isinstance(event, EventBase) else dict(event)
    if not isinstance(data.get("type"), str) or not data["type"]:
        raise ValueError("an event needs a non-empty string 'type'")
    data.pop("seq", None)  # the log numbers events itself
    data.pop("replay_epoch", None)
    data.setdefault("session_id", session_id)
    return data


class EventLog:
    """Numbering, coalescing and bounded replay for every session of one daemon."""

    def __init__(
        self,
        *,
        clock: Clock,
        epoch: str | None = None,
        limits: ReplayLimits | None = None,
        coalesce_window: float = COALESCE_WINDOW_S,
    ) -> None:
        if coalesce_window < 0:
            raise ValueError("coalesce_window must be >= 0")
        self._clock = clock
        self.epoch = epoch or new_replay_epoch()
        self.limits = limits or ReplayLimits()
        self.coalesce_window = coalesce_window
        self._lock = threading.Lock()
        self._rings: OrderedDict[str, _Ring] = OrderedDict()  # least recently active first
        self._last: dict[str, int] = {}
        self._evicted: dict[str, int] = {}
        self._held: dict[str, _Held] = {}
        self._total = 0
        self._listeners: list[LogListener] = []
        self._wake: asyncio.Event | None = None
        self._wake_loop: asyncio.AbstractEventLoop | None = None

    # -- listeners ----------------------------------------------------------------------------

    def add_listener(self, listener: LogListener) -> Callable[[], None]:
        """Hear every numbered event; the returned callable removes the listener."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _tell(self, entries: list[LoggedEvent]) -> None:
        for entry in entries:
            for listener in list(self._listeners):
                try:
                    listener(entry)
                except Exception:
                    log.warning(
                        "an event-log listener failed on #%d of session %s",
                        entry.seq,
                        entry.session_id,
                        exc_info=True,
                    )

    # -- appending ----------------------------------------------------------------------------

    def append(self, session_id: str, event: EventBase | Mapping[str, Any]) -> list[LoggedEvent]:
        """Number and keep ``event`` (a held text piece may wait; see the module notes).

        Returns the events numbered by this call, oldest first: none when the piece is held, the
        flushed held piece before this event when one was waiting.
        """
        if not session_id:
            raise ValueError("session_id must not be empty")
        data = _as_mapping(session_id, event)
        kind = data["type"]
        streaming = kind in STREAMING_TYPES and isinstance(data.get("text"), str)
        payload = None if streaming else _freeze(data)  # encoded outside the lock
        armed = False
        with self._lock:
            now = self._clock.monotonic()
            done: list[LoggedEvent] = []
            held = self._held.get(session_id)
            if streaming:
                text = str(data.pop("text"))
                if (
                    held is not None
                    and held.type == kind
                    and held.turn_id == data.get("turn_id")
                    and now - held.started < self.coalesce_window
                ):
                    held.parts.append(text)
                else:
                    if held is not None:
                        done.append(self._flush_held(session_id))
                    self._held[session_id] = _Held(kind, data.get("turn_id"), data, [text], now)
                    armed = True
            else:
                if held is not None:
                    done.append(self._flush_held(session_id))
                assert payload is not None
                done.append(self._keep(session_id, payload))
        if armed:
            self._poke()
        self._tell(done)
        return done

    def _flush_held(self, session_id: str) -> LoggedEvent:
        held = self._held.pop(session_id)
        data = dict(held.base)
        data["text"] = "".join(held.parts)
        return self._keep(session_id, _freeze(data))

    def _keep(self, session_id: str, payload: bytes) -> LoggedEvent:
        """Number ``payload`` and store it within the caps (lock held)."""
        seq = self._last.get(session_id, 0) + 1
        self._last[session_id] = seq
        entry = LoggedEvent(session_id, seq, self.epoch, payload)
        ring = self._ring(session_id)
        limits = self.limits
        if entry.size > limits.bytes_per_session or entry.size > limits.bytes_process:
            self._evicted[session_id] = seq  # never kept: a cursor before it has a gap
            return entry
        ring.events.append(entry)
        ring.bytes += entry.size
        self._total += entry.size
        while len(ring.events) > limits.events_per_session or ring.bytes > limits.bytes_per_session:
            self._drop_oldest(session_id, ring)
        while self._total > limits.bytes_process:
            victim = next((sid for sid, r in self._rings.items() if r.events), None)
            if victim is None:  # pragma: no cover - the total counts only kept events
                break
            self._drop_oldest(victim, self._rings[victim])
        return entry

    def _ring(self, session_id: str) -> _Ring:
        ring = self._rings.get(session_id)
        if ring is not None:
            self._rings.move_to_end(session_id)
            return ring
        ring = self._rings[session_id] = _Ring()
        while len(self._rings) > self.limits.sessions:
            quiet, dropped = self._rings.popitem(last=False)
            self._total -= dropped.bytes
            self._evicted[quiet] = max(self._evicted.get(quiet, 0), self._last.get(quiet, 0))
        return ring

    def _drop_oldest(self, session_id: str, ring: _Ring) -> None:
        entry = ring.events.popleft()
        ring.bytes -= entry.size
        self._total -= entry.size
        self._evicted[session_id] = max(self._evicted.get(session_id, 0), entry.seq)

    # -- flushing held text -------------------------------------------------------------------

    def flush(self, session_id: str | None = None) -> list[LoggedEvent]:
        """Number held text now (one session, or every session for ``None``)."""
        with self._lock:
            ids = [session_id] if session_id is not None else list(self._held)
            done = [self._flush_held(sid) for sid in ids if sid in self._held]
        self._tell(done)
        return done

    def flush_due(self) -> list[LoggedEvent]:
        """Number every held piece whose coalescing window is over."""
        with self._lock:
            now = self._clock.monotonic()
            due = [sid for sid, h in self._held.items() if now - h.started >= self.coalesce_window]
            done = [self._flush_held(sid) for sid in due]
        self._tell(done)
        return done

    def next_flush_at(self) -> float | None:
        """The monotonic time the earliest held piece is due, or None when nothing is held."""
        with self._lock:
            if not self._held:
                return None
            return min(h.started for h in self._held.values()) + self.coalesce_window

    async def run_flusher(self) -> None:
        """Flush held text when its window ends; runs until cancelled (on the daemon loop)."""
        self._wake = asyncio.Event()
        self._wake_loop = asyncio.get_running_loop()
        try:
            while True:
                due = self.next_flush_at()
                if due is None:
                    self._wake.clear()
                    if self.next_flush_at() is None:
                        await self._wake.wait()
                    continue
                delay = due - self._clock.monotonic()
                if delay > 0:
                    await self._clock.sleep(delay)
                self.flush_due()
        finally:
            self._wake = None
            self._wake_loop = None

    def _poke(self) -> None:
        wake, loop = self._wake, self._wake_loop
        if wake is None or loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            wake.set()
        else:
            loop.call_soon_threadsafe(wake.set)

    # -- replay -------------------------------------------------------------------------------

    def since(self, session_id: str, epoch: str | None, cursor: int) -> Replay | ReplayGap:
        """The kept events of ``session_id`` after ``cursor`` (the last seq the client saw)."""
        if cursor < 0:
            raise ValueError("cursor must be >= 0")
        with self._lock:
            last = self._last.get(session_id, 0)
            if epoch != self.epoch:
                return ReplayGap(session_id, self.epoch, last, "epoch_changed")
            if cursor > last:
                return ReplayGap(session_id, self.epoch, last, "ahead")
            if cursor < self._evicted.get(session_id, 0):
                return ReplayGap(session_id, self.epoch, last, "evicted")
            ring = self._rings.get(session_id)
            kept = [e for e in ring.events if e.seq > cursor] if ring is not None else []
        return Replay(session_id, self.epoch, tuple(e.to_wire() for e in kept), last)

    def last_seq(self, session_id: str) -> int:
        """The newest seq numbered for ``session_id`` (0 before the first)."""
        with self._lock:
            return self._last.get(session_id, 0)

    def forget(self, session_id: str) -> None:
        """Drop a session's buffer (held text is numbered first); its counter stays."""
        done: list[LoggedEvent] = []
        with self._lock:
            if session_id in self._held:
                done.append(self._flush_held(session_id))
            ring = self._rings.pop(session_id, None)
            if ring is not None:
                self._total -= ring.bytes
            if session_id in self._last:
                self._evicted[session_id] = self._last[session_id]
        self._tell(done)

    def stats(self) -> dict[str, int]:
        """Occupancy for diagnostics."""
        with self._lock:
            return {
                "sessions": len(self._rings),
                "events": sum(len(r.events) for r in self._rings.values()),
                "bytes": self._total,
                "held": len(self._held),
            }
