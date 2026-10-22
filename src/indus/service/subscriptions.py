"""Subscriptions of one client connection to daemon-hosted sessions (P17-14).

A connection to the daemon (the WebSocket endpoint, P17-13) may watch several sessions. This module
holds what the transport needs for that, on top of the ``DaemonSessionHost`` (P17-08):

- ``Outbox``: the frames waiting for one connection's writer, in order. Each session the
  connection watches is a *lane*: a lane may hold at most ``viewer_events`` events and
  ``viewer_bytes`` bytes. A lane that would go past either is dropped: its queued frames are
  removed and ``on_overflow(lane)`` is told; it detaches the viewer and returns the
  ``session.replay_gap {reason: "overflow"}`` frame queued in their place. Later frames of a dropped
  lane are refused until the lane is opened again by a new subscribe. Frames outside lanes
  (answers, list updates) only count toward the connection's ``total_bytes``; past it the outbox is
  *overloaded* and the transport closes the connection. ``session.updated`` frames still waiting
  are merged per session (the newest values win). Nothing here waits on a socket, so one slow
  connection never holds up another.
- ``Subscriptions``: the sessions one connection attached (created, resumed or subscribed). Only
  those may be controlled, replayed or unsubscribed from this connection; any other id reads as an
  unknown session (``SESSION_NOT_FOUND``). ``subscribe`` replays the events after the client's
  cursor (or queues a ``session.replay_gap``), then makes the connection a viewer, which offers
  every open server request it can render again; all of it happens in one step on the daemon loop
  so no event is lost or doubled between the replay and the live stream.
- ``SessionWatch``: ``session.updated`` for list views (running state, title, last activity) from
  the host's event log, plus ``report`` for changes that do not travel as events.
- Presence: ``announce_viewers`` puts a ``session.viewers`` event (surface kinds and counts) into
  the session's log whenever a subscription changes its viewers.
- Header: every attach also asks the host for a ``session.info`` (``announce_info``), so a
  client that attached a session learns its model, folder and title (W48-h, D-16.550).

Every method runs on the daemon's event loop. Event frames are checked against their declared
models (``check_event``); in strict mode a mismatch is kept in ``violations``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from indus.protocol.errors import INVALID_PARAMS, SESSION_NOT_FOUND
from indus.protocol.methods import SERVER_REQUESTS_FEATURE, ProtocolError
from indus.protocol.server_requests import (
    PERSON_REQUEST_KINDS,
    SERVER_REQUEST_KINDS,
    SERVER_REQUEST_METHOD,
)
from indus.service.daemon_host import DaemonSessionHost
from indus.service.event_log import LoggedEvent, Replay, ReplayGap
from indus.service.request_redelivery import Offer
from indus.service.rpc.dispatch import ContractViolation, check_event
from indus.service.viewers import DECLINE_CAPABILITY, SURFACES, Viewer

__all__ = [
    "GAP_EVENT",
    "MAX_SUBSCRIPTIONS",
    "RESYNC_METHOD",
    "UPDATED_EVENT",
    "VIEWERS_EVENT",
    "Outbox",
    "OutboxLimits",
    "SessionWatch",
    "Subscribed",
    "Subscriptions",
    "announce_viewers",
    "capabilities_from",
    "event_frame",
    "notification",
]

log = logging.getLogger(__name__)

_MIB = 1024 * 1024
GAP_EVENT: Final = "session.replay_gap"
UPDATED_EVENT: Final = "session.updated"
VIEWERS_EVENT: Final = "session.viewers"
RESYNC_METHOD: Final = "sessions.resync"
"""Sent once when list updates piled past the cap: reload the list (``session.list``)."""
MAX_SUBSCRIPTIONS: Final = 64
"""Sessions one connection may watch at once."""
_WATCHED_TYPES: Final = frozenset({"turn.started", "turn.done", "session.title"})
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def notification(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """A JSON-RPC notification frame."""
    return {"jsonrpc": "2.0", "method": method, "params": dict(params)}


def event_frame(event: Mapping[str, Any]) -> dict[str, Any]:
    """The notification carrying one numbered event (``method`` = its type)."""
    return notification(str(event.get("type", "")), event)


def serialize(frame: Mapping[str, Any]) -> str:
    """One frame as the text a WebSocket message carries. A lone surrogate (text the model or a
    tool produced from undecodable bytes) becomes U+FFFD instead of failing the send."""
    text = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    return _LONE_SURROGATE.sub("�", text)


def capabilities_from(features: Iterable[str]) -> frozenset[str]:
    """Viewer capabilities from a client's ``client.capabilities`` features: ``server_requests``
    renders every kind a person answers, ``server_requests.<kind>`` one kind (the preview kinds
    only this way: a desktop window names them), ``request.decline`` allows 4404."""
    found: set[str] = set()
    for name in features:
        if name == SERVER_REQUESTS_FEATURE:
            found.update(PERSON_REQUEST_KINDS)
        elif name.startswith(SERVER_REQUESTS_FEATURE + "."):
            kind = name[len(SERVER_REQUESTS_FEATURE) + 1 :]
            if kind in SERVER_REQUEST_KINDS:
                found.add(kind)
        elif name == DECLINE_CAPABILITY:
            found.add(name)
    return frozenset(found)


# -- the outbox -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutboxLimits:
    """How much one connection may have waiting for its writer."""

    viewer_events: int = 1024
    viewer_bytes: int = 4 * _MIB
    total_bytes: int = 16 * _MIB
    pending_updates: int = 1024

    def __post_init__(self) -> None:
        for name in ("viewer_events", "viewer_bytes", "total_bytes", "pending_updates"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


@dataclass(slots=True)
class _Item:
    text: str
    lane: str | None
    queued_at: float
    update_for: str | None = None
    params: dict[str, Any] | None = None
    resync: bool = False

    @property
    def size(self) -> int:
        return len(self.text)


@dataclass(slots=True)
class _Lane:
    events: int = 0
    bytes: int = 0
    dropped: bool = False


class Outbox:
    """Frames waiting for one connection's writer (see the module notes)."""

    def __init__(
        self,
        limits: OutboxLimits | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        on_overflow: Callable[[str], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.limits = limits or OutboxLimits()
        self.on_overflow = on_overflow
        self._now = monotonic
        self._queue: deque[_Item] = deque()
        self._lanes: dict[str, _Lane] = {}
        self._updates: dict[str, _Item] = {}
        self._bytes = 0
        self._ready = asyncio.Event()
        self._closed = False
        self.overloaded = False
        self.resync_pending = False

    # -- reading state --------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def queued_bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._queue)

    def lane_events(self, lane: str) -> int:
        state = self._lanes.get(lane)
        return state.events if state is not None else 0

    def dropped(self, lane: str) -> bool:
        state = self._lanes.get(lane)
        return state is not None and state.dropped

    def oldest_wait(self) -> float:
        """Seconds the oldest waiting frame has waited (0 when nothing waits)."""
        if not self._queue:
            return 0.0
        return max(0.0, self._now() - self._queue[0].queued_at)

    # -- putting --------------------------------------------------------------------------------

    def _push(self, item: _Item) -> bool:
        if self._bytes + item.size > self.limits.total_bytes:
            if not self.overloaded:
                log.warning("a connection's outbox passed %d bytes", self.limits.total_bytes)
            self.overloaded = True
            self._ready.set()
            return False
        self._queue.append(item)
        self._bytes += item.size
        self._ready.set()
        return True

    def put(self, frame: Mapping[str, Any]) -> bool:
        """Queue a frame outside any lane (an answer, a notification). False when closed or the
        connection is over its byte cap (``overloaded``)."""
        if self._closed or self.overloaded:
            return False
        return self._push(_Item(serialize(frame), None, self._now()))

    def put_event(self, lane: str, frame: Mapping[str, Any]) -> bool:
        """Queue a frame of ``lane`` (a watched session). Past the lane's caps the lane drops to a
        gap; False when the frame was not queued."""
        if self._closed or self.overloaded:
            return False
        state = self._lanes.setdefault(lane, _Lane())
        if state.dropped:
            return False
        text = serialize(frame)
        if (
            state.events + 1 > self.limits.viewer_events
            or state.bytes + len(text) > self.limits.viewer_bytes
        ):
            self._overflow(lane, state)
            return False
        if not self._push(_Item(text, lane, self._now())):
            return False
        state.events += 1
        state.bytes += len(text)
        return True

    def _overflow(self, lane: str, state: _Lane) -> None:
        dropped = self.drop_lane(lane)
        state.dropped = True
        log.info("a viewer of session %s fell %d frames behind; dropped to a gap", lane, dropped)
        gap: Mapping[str, Any] | None = None
        if self.on_overflow is not None:
            try:
                gap = self.on_overflow(lane)
            except Exception:
                log.warning("the overflow hook failed for session %s", lane, exc_info=True)
        if gap is not None:
            self._push(_Item(serialize(gap), None, self._now()))

    def put_update(self, session_id: str, fields: Mapping[str, Any]) -> bool:
        """Queue (or merge into a waiting) ``session.updated`` for ``session_id``."""
        if self._closed or self.overloaded:
            return False
        waiting = self._updates.get(session_id)
        if waiting is not None and waiting.params is not None:
            waiting.params.update(fields)
            old = waiting.size
            waiting.text = serialize(notification(UPDATED_EVENT, waiting.params))
            self._bytes += waiting.size - old
            return True
        if len(self._updates) >= self.limits.pending_updates:
            self._drop_updates()
            if not self.resync_pending:
                self.resync_pending = True
                frame = notification(RESYNC_METHOD, {"reason": "overflow"})
                self._push(_Item(serialize(frame), None, self._now(), resync=True))
            return False
        params = {"type": UPDATED_EVENT, "session_id": session_id, **fields}
        item = _Item(
            serialize(notification(UPDATED_EVENT, params)),
            None,
            self._now(),
            update_for=session_id,
            params=params,
        )
        if not self._push(item):
            return False
        self._updates[session_id] = item
        return True

    def _drop_updates(self) -> None:
        kept: deque[_Item] = deque()
        for item in self._queue:
            if item.update_for is not None:
                self._bytes -= item.size
            else:
                kept.append(item)
        self._queue = kept
        self._updates.clear()

    def drop_lane(self, lane: str) -> int:
        """Remove every waiting frame of ``lane``; how many were removed."""
        state = self._lanes.get(lane)
        if state is None or state.events == 0:
            return 0
        kept: deque[_Item] = deque()
        removed = 0
        for item in self._queue:
            if item.lane == lane:
                self._bytes -= item.size
                removed += 1
            else:
                kept.append(item)
        self._queue = kept
        state.events = 0
        state.bytes = 0
        return removed

    def open_lane(self, lane: str) -> None:
        """Start ``lane`` afresh (a new subscription): nothing waiting, not dropped."""
        self.drop_lane(lane)
        self._lanes[lane] = _Lane()

    def close_lane(self, lane: str) -> None:
        """Forget ``lane`` (unsubscribed): its waiting frames go too."""
        self.drop_lane(lane)
        self._lanes.pop(lane, None)

    # -- taking ---------------------------------------------------------------------------------

    def take(self) -> str | None:
        """The next frame's text without waiting, or None when nothing waits."""
        if not self._queue:
            return None
        item = self._queue.popleft()
        self._bytes -= item.size
        if item.lane is not None:
            state = self._lanes.get(item.lane)
            if state is not None:
                state.events -= 1
                state.bytes -= item.size
        if item.update_for is not None and self._updates.get(item.update_for) is item:
            del self._updates[item.update_for]
        if item.resync:
            self.resync_pending = False
        return item.text

    async def get(self) -> str | None:
        """The next frame's text; None once the outbox is closed (or overloaded)."""
        while True:
            if self.overloaded:
                return None
            text = self.take()
            if text is not None:
                return text
            if self._closed:
                return None
            self._ready.clear()
            await self._ready.wait()

    def close(self) -> None:
        """Stop taking frames; the writer ends after what is already queued."""
        self._closed = True
        self._ready.set()


# -- presence and list updates --------------------------------------------------------------------


def announce_viewers(host: DaemonSessionHost, key: str) -> None:
    """Put a ``session.viewers`` event (surface kinds and counts) into the session's log."""
    if host.key_of(key) is None:
        return
    counts = Counter(viewer.surface for viewer in host.viewers.viewers(key))
    host.event_log.append(
        key,
        {
            "type": VIEWERS_EVENT,
            "session_id": host.state(key).session_id,
            "viewers": [{"surface": s, "count": n} for s, n in sorted(counts.items())],
            "count": sum(counts.values()),
        },
    )


type UpdateSink = Callable[[str, Mapping[str, Any]], object]


class SessionWatch:
    """``session.updated`` for every list watcher of the daemon (see the module notes)."""

    def __init__(self, host: DaemonSessionHost) -> None:
        self.host = host
        self._sinks: dict[int, UpdateSink] = {}
        self._next = 0
        self._undo: Callable[[], None] | None = None

    def start(self) -> None:
        if self._undo is None:
            self._undo = self.host.event_log.add_listener(self._on_event)

    def stop(self) -> None:
        undo, self._undo = self._undo, None
        if undo is not None:
            undo()
        self._sinks.clear()

    def add(self, sink: UpdateSink) -> Callable[[], None]:
        """Send updates to ``sink(session_id, fields)``; the returned callable stops them."""
        self.start()
        token = self._next
        self._next += 1
        self._sinks[token] = sink

        def remove() -> None:
            self._sinks.pop(token, None)

        return remove

    @property
    def watchers(self) -> int:
        return len(self._sinks)

    def report(self, session_id: str, **fields: Any) -> None:
        """A change that does not travel as an event (archived, pinned, a title set elsewhere)."""
        self._tell(session_id, {**fields, "last_activity": self._now()})

    def _now(self) -> float:
        return self.host.store.scope.clock.now().timestamp()

    def _on_event(self, entry: LoggedEvent) -> None:
        if not self._sinks:
            return
        event = entry.to_wire()
        kind = event.get("type")
        if kind not in _WATCHED_TYPES:
            return
        session_id = str(event.get("session_id") or entry.session_id)
        fields: dict[str, Any] = {"last_activity": self._now()}
        if kind == "turn.started":
            fields["running"] = True
        elif kind == "turn.done":
            fields["running"] = False
        else:
            fields["title"] = event.get("title")
        self._tell(session_id, fields)

    def _tell(self, session_id: str, fields: Mapping[str, Any]) -> None:
        for sink in list(self._sinks.values()):
            try:
                sink(session_id, fields)
            except Exception:
                log.warning("a list watcher could not take an update", exc_info=True)


# -- one connection's subscriptions ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Subscribed:
    """Where a subscription (or a catch-up read) starts."""

    session_id: str
    key: str
    replay_epoch: str
    last_seq: int
    replayed: int
    gap: ReplayGap | None
    reoffered: int
    events: tuple[dict[str, Any], ...] = ()


@dataclass(slots=True)
class _Counts:
    offers: int = 0


@dataclass(slots=True)
class _State:
    attached: set[str] = field(default_factory=set[str])


class Subscriptions:
    """The sessions one connection attached and its viewers of them (see the module notes)."""

    def __init__(
        self,
        host: DaemonSessionHost,
        connection_id: str,
        outbox: Outbox,
        *,
        watch: SessionWatch | None = None,
        surface: str = "cli",
        capabilities: Iterable[str] = (),
        strict: bool = False,
        max_sessions: int = MAX_SUBSCRIPTIONS,
    ) -> None:
        if surface not in SURFACES:
            raise ValueError(f"unknown surface kind {surface!r}")
        self.host = host
        self.connection_id = connection_id
        self.outbox = outbox
        self.watch_hub = watch
        self.surface = surface
        self.capabilities: frozenset[str] = frozenset(capabilities)
        self.strict = strict
        self.max_sessions = max_sessions
        self.violations: list[ContractViolation] = []
        self._state = _State()
        self._counts = _Counts()
        self._unwatch: Callable[[], None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        if outbox.on_overflow is None:
            outbox.on_overflow = self._overflowed

    # -- reading --------------------------------------------------------------------------------

    @property
    def attached(self) -> frozenset[str]:
        """Host keys of the sessions this connection attached."""
        return frozenset(self._state.attached)

    @property
    def watching(self) -> bool:
        return self._unwatch is not None

    def require(self, session_id: str) -> str:
        """The host key of ``session_id`` when this connection attached it; ``SESSION_NOT_FOUND``
        for anything else (another connection's session reads as unknown)."""
        key = self.host.key_of(session_id)
        if key is None or key not in self._state.attached:
            raise ProtocolError(
                SESSION_NOT_FOUND, f"session {session_id} is not attached here", path="session_id"
            )
        return key

    def is_viewer(self, key: str) -> bool:
        return self.host.viewers.find(key, self.connection_id) is not None

    # -- configuration --------------------------------------------------------------------------

    def configure(self, *, surface: str | None = None, features: Iterable[str] = ()) -> None:
        """Apply the client's ``client.capabilities``; sessions already watched get the new viewer
        (open requests it can now render are offered)."""
        if surface is not None:
            if surface not in SURFACES or surface == "messaging":
                raise ProtocolError(INVALID_PARAMS, "unknown surface kind", path="surface")
            self.surface = surface
        self.capabilities = capabilities_from(features)
        for key in sorted(self._state.attached):
            if self.is_viewer(key) and self.host.key_of(key) is not None:
                self.host.attach(key, self._viewer(key))

    # -- the viewer -----------------------------------------------------------------------------

    def _viewer(self, key: str) -> Viewer:
        return Viewer.of(
            self.connection_id,
            self.surface,
            self.capabilities,
            send=lambda event: self._deliver(key, event),
            offer=lambda offer: self._offer(key, offer),
        )

    def _deliver(self, key: str, event: dict[str, Any]) -> None:
        method = str(event.get("type", ""))
        checked = {k: v for k, v in event.items() if k != "replay_epoch"}
        try:
            check_event(method, checked, strict=self.strict)
        except ContractViolation as exc:
            self.violations.append(exc)
            log.error("event %s does not match its model: %s", method, exc)
        self.outbox.put_event(key, event_frame(event))

    def _offer(self, key: str, offer: Offer) -> None:
        self._counts.offers += 1
        self.outbox.put_event(key, notification(SERVER_REQUEST_METHOD, offer.to_wire()))

    def _overflowed(self, key: str) -> dict[str, Any] | None:
        """A lane dropped to a gap: the viewer is detached (it subscribes again to recover) and
        the gap frame tells the client where the session stands."""
        if self._closed or self.host.key_of(key) is None:
            return None
        self._spawn(self._detach(key))
        gap = {
            "type": GAP_EVENT,
            "session_id": self.host.state(key).session_id,
            "replay_epoch": self.host.replay_epoch,
            "last_seq": self.host.event_log.last_seq(key),
            "reason": "overflow",
        }
        return notification(GAP_EVENT, gap)

    async def _detach(self, key: str) -> None:
        if self.host.key_of(key) is None:
            return
        if await self.host.detach(key, self.connection_id):
            announce_viewers(self.host, key)

    # -- attaching ------------------------------------------------------------------------------

    def _check_room(self, key: str | None) -> None:
        if key in self._state.attached:
            return
        if len(self._state.attached) >= self.max_sessions:
            raise ProtocolError(
                INVALID_PARAMS,
                f"a connection may watch at most {self.max_sessions} sessions",
                path="session_id",
            )

    async def subscribe(
        self, session_id: str, epoch: str | None = None, cursor: int | None = None
    ) -> Subscribed:
        """Watch ``session_id``: replay after ``cursor`` (or a gap), then live events."""
        self._check_open()
        self._check_room(self.host.key_of(session_id))
        if self.host.key_of(session_id) is None:
            await self.host.resume(session_id, source=self.surface)
        key = self.host.key_of(session_id)
        if key is None:  # closed again while resuming
            raise ProtocolError(SESSION_NOT_FOUND, path="session_id")
        self._check_room(key)
        return self._attach(key, epoch, cursor)

    def adopt(self, session_id: str, *, cursor: int | None = None) -> Subscribed:
        """Attach a session this connection just created or resumed (``cursor`` 0 sends what the
        session numbered since it was created)."""
        self._check_open()
        key = self.host.key_of(session_id)
        if key is None:
            raise ProtocolError(SESSION_NOT_FOUND, path="session_id")
        self._check_room(key)
        return self._attach(key, self.host.replay_epoch if cursor is not None else None, cursor)

    def _attach(self, key: str, epoch: str | None, cursor: int | None) -> Subscribed:
        # One synchronous step: nothing can be numbered between the replay read and the attach.
        log_ = self.host.event_log
        self.outbox.open_lane(key)
        gap: ReplayGap | None = None
        replayed = 0
        if cursor is not None:
            found = log_.since(key, epoch, cursor)
            if isinstance(found, ReplayGap):
                gap = found
                self.outbox.put_event(key, notification(GAP_EVENT, _gap_wire(self.host, found)))
            else:
                for event in found.events:
                    self._deliver(key, event)
                    replayed += 1
        before = self._counts.offers
        attachment = self.host.attach(key, self._viewer(key))
        reoffered = self._counts.offers - before
        self._state.attached.add(key)
        announce_viewers(self.host, key)
        self.host.announce_info(key)  # the status bar's header (W48-h)
        return Subscribed(
            session_id=attachment.session_id,
            key=key,
            replay_epoch=attachment.replay_epoch,
            last_seq=attachment.last_seq,
            replayed=replayed,
            gap=gap,
            reoffered=reoffered,
        )

    async def unsubscribe(self, session_id: str) -> bool:
        """Stop watching; the session and any running turn carry on."""
        key = self.host.key_of(session_id)
        if key is None or key not in self._state.attached:
            return False
        self._state.attached.discard(key)
        self.outbox.close_lane(key)
        removed = await self.host.detach(key, self.connection_id)
        if removed:
            announce_viewers(self.host, key)
        return removed

    def since(self, session_id: str, epoch: str | None, cursor: int) -> Subscribed:
        """The events after ``cursor`` in the answer (or a gap); open requests offered again."""
        key = self.require(session_id)
        before = self._counts.offers
        found = self.host.events_since(key, epoch, cursor, connection_id=self.connection_id)
        reoffered = self._counts.offers - before
        if isinstance(found, Replay):
            return Subscribed(
                found.session_id,
                key,
                found.replay_epoch,
                found.last_seq,
                len(found.events),
                None,
                reoffered,
                found.events,
            )
        return Subscribed(
            self.host.state(key).session_id,
            key,
            found.replay_epoch,
            found.last_seq,
            0,
            found,
            reoffered,
        )

    def forget(self, key: str) -> None:
        """The host closed the session (``session.close``): drop it here too."""
        self._state.attached.discard(key)
        self.outbox.close_lane(key)

    # -- refusals -------------------------------------------------------------------------------

    def refuse(self, request_id: str, code: int) -> bool:
        """This connection answered a server request with an error (``-32601`` cannot render,
        ``4404`` not shown here): recorded on whichever attached session holds the request."""
        for key in sorted(self._state.attached):
            if self.host.key_of(key) is None:
                continue
            with contextlib.suppress(ProtocolError):
                if self.host.refuse(key, self.connection_id, request_id, code):
                    return True
        return False

    # -- list updates ---------------------------------------------------------------------------

    def watch(self, enabled: bool) -> bool:
        """Turn ``session.updated`` on or off for this connection; the state after the call."""
        if enabled and self._unwatch is None and self.watch_hub is not None and not self._closed:
            self._unwatch = self.watch_hub.add(self.outbox.put_update)
        elif not enabled and self._unwatch is not None:
            self._unwatch()
            self._unwatch = None
        return self._unwatch is not None

    # -- closing --------------------------------------------------------------------------------

    async def close(self) -> list[str]:
        """The connection ended: stop list updates, detach every viewer (turns keep running)."""
        if self._closed:
            return []
        self._closed = True
        self.watch(False)
        self.outbox.close()
        for task in list(self._tasks):
            task.cancel()
        keys = await self.host.detach_connection(self.connection_id)
        for key in keys:
            announce_viewers(self.host, key)
        self._state.attached.clear()
        return keys

    def _check_open(self) -> None:
        if self._closed:
            raise ProtocolError(SESSION_NOT_FOUND, "the connection is closing")

    def _spawn(self, coro: Any) -> None:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _gap_wire(host: DaemonSessionHost, gap: ReplayGap) -> dict[str, Any]:
    wire = gap.to_wire()
    wire["session_id"] = host.state(gap.session_id).session_id
    return wire
