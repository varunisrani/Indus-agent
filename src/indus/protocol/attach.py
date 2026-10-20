"""Methods a client attached to the daemon's WebSocket uses to follow sessions (P17-13/14).

A connection to the per-profile daemon may watch several sessions. ``session.subscribe`` makes it
a viewer of one session: the events after its cursor are sent first (or a ``session.replay_gap``
when they cannot be), then every new event of the session. ``events.since`` is the pull form.
``sessions.watch`` turns on ``session.updated`` notifications for list views. These names are
served only by the daemon (``DAEMON_ONLY_METHODS``); the stdio bridge answers them ``-32601``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, NonNegativeInt

from indus.protocol.base import ProtocolModel, SessionId
from indus.protocol.errors import SESSION_NOT_FOUND, SESSION_REFUSED, STORE_FAILED, ErrorCode

__all__ = [
    "ATTACH_METHODS",
    "DAEMON_ONLY_METHODS",
    "EventsSinceParams",
    "EventsSinceResult",
    "ReplayGapInfo",
    "SessionSubscribeParams",
    "SessionSubscribeResult",
    "SessionUnsubscribeParams",
    "SessionUnsubscribeResult",
    "SessionsWatchParams",
    "SessionsWatchResult",
]


class ReplayGapInfo(ProtocolModel):
    """Why the events after a cursor cannot be sent; the client reloads a snapshot."""

    reason: Literal["epoch_changed", "evicted", "ahead", "overflow"]
    """Another daemon run, dropped from memory, a cursor past the end, or the viewer fell behind."""
    replay_epoch: str
    """The daemon's current replay epoch."""
    last_seq: NonNegativeInt
    """The newest seq of the session: the cursor to hold after the snapshot."""


class SessionSubscribeParams(ProtocolModel):
    """Watch a session from this connection: replay since a cursor, then live events."""

    session_id: SessionId
    """The session to watch."""
    epoch: str | None = Field(default=None, min_length=1)
    """The replay epoch the cursor belongs to; absent with a cursor counts as another run."""
    cursor: NonNegativeInt | None = None
    """The last seq the client applied; absent means no replay (live from now on)."""


class SessionSubscribeResult(ProtocolModel):
    """Where the subscription starts."""

    session_id: str
    """The id the session runs under now."""
    replay_epoch: str
    """The daemon's replay epoch."""
    last_seq: NonNegativeInt
    """The newest seq when the subscription began (after the replayed events)."""
    replayed: NonNegativeInt
    """Events sent ahead of this answer to catch up from the cursor."""
    gap: ReplayGapInfo | None = None
    """Set when the events after the cursor could not be replayed (reload a snapshot)."""
    reoffered: NonNegativeInt = 0
    """Open server requests offered again to this connection."""


class SessionUnsubscribeParams(ProtocolModel):
    """Stop watching a session from this connection (its turns keep running)."""

    session_id: SessionId
    """The session to stop watching."""


class SessionUnsubscribeResult(ProtocolModel):
    """Whether this connection was watching."""

    unsubscribed: bool
    """False when the connection was not a viewer of the session."""


class SessionsWatchParams(ProtocolModel):
    """Receive ``session.updated`` for list views (title, running state, last activity)."""

    enabled: bool = True
    """False stops the updates."""


class SessionsWatchResult(ProtocolModel):
    """Whether list updates are on for this connection."""

    watching: bool
    """The state after the call."""


class EventsSinceParams(ProtocolModel):
    """The events of a watched session after a cursor."""

    session_id: SessionId
    """A session this connection watches."""
    epoch: str | None = Field(default=None, min_length=1)
    """The replay epoch the cursor belongs to."""
    cursor: NonNegativeInt
    """The last seq the client applied."""


class EventsSinceResult(ProtocolModel):
    """The missing events, oldest first, or a gap."""

    session_id: str
    """The id the session runs under now."""
    replay_epoch: str
    """The daemon's replay epoch."""
    last_seq: NonNegativeInt
    """The newest seq of the session (the cursor to hold afterwards)."""
    events: list[dict[str, JsonValue]] = Field(default_factory=list[dict[str, JsonValue]])
    """The events after the cursor (each with ``seq`` and ``replay_epoch``); empty on a gap."""
    gap: ReplayGapInfo | None = None
    """Set when the events cannot be replayed (reload a snapshot)."""
    reoffered: NonNegativeInt = 0
    """Open server requests offered again to this connection."""


type _Row = tuple[str, type[ProtocolModel], type[ProtocolModel], tuple[ErrorCode, ...]]

_SESSION = (SESSION_NOT_FOUND, SESSION_REFUSED, STORE_FAILED)

ATTACH_METHODS: tuple[_Row, ...] = (
    ("session.subscribe", SessionSubscribeParams, SessionSubscribeResult, _SESSION),
    ("session.unsubscribe", SessionUnsubscribeParams, SessionUnsubscribeResult, ()),
    ("sessions.watch", SessionsWatchParams, SessionsWatchResult, ()),
    ("events.since", EventsSinceParams, EventsSinceResult, (SESSION_NOT_FOUND,)),
)
"""``(name, params, result, extra errors)`` for the attach methods, in declaration order."""

DAEMON_ONLY_METHODS: frozenset[str] = frozenset(row[0] for row in ATTACH_METHODS)
"""Declared names only the daemon's WebSocket serves (the stdio bridge answers ``-32601``)."""
