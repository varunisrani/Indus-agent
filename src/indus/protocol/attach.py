"""Methods a client attached to the daemon's WebSocket uses to follow sessions (P17-13/14).

A connection to the per-profile daemon may watch several sessions. ``session.subscribe`` makes it
a viewer of one session: the events after its cursor are sent first (or a ``session.replay_gap``
when they cannot be), then every new event of the session. ``events.since`` is the pull form.
``sessions.watch`` turns on ``session.updated`` notifications for list views.
``admission.discard`` resolves a turn the daemon lost in a restart (its outcome is unknown) so the
session's queue moves on; it names the lost turn's generation. ``admission.receipt`` tells a
client that lost an ack what became of its input. ``runtime.describe`` is the control socket's
``describe`` over the WebSocket, and ``maintenance.run`` runs bulk store maintenance inside the
daemon that owns the store (P17-21). These names are served only by the daemon
(``DAEMON_ONLY_METHODS``); the stdio bridge answers them ``-32601``.

Frames the daemon sends outside a session's numbered stream are notifications
(``ATTACH_NOTIFICATIONS``): ``session.replay_gap`` (this viewer cannot be caught up; reload a
snapshot), ``session.updated`` (list views) and ``sessions.resync`` (list updates were dropped).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, NonNegativeInt, PositiveInt

from indus.protocol.base import ProtocolModel, SessionId
from indus.protocol.errors import (
    ADMISSION_NOT_FOUND,
    SESSION_NOT_FOUND,
    SESSION_REFUSED,
    STORE_FAILED,
    ErrorCode,
)

__all__ = [
    "ATTACH_METHODS",
    "ATTACH_NOTIFICATIONS",
    "DAEMON_ONLY_METHODS",
    "AdmissionDiscardParams",
    "AdmissionDiscardResult",
    "AdmissionOutcomeName",
    "AdmissionReceiptParams",
    "AdmissionReceiptResult",
    "AdmissionStateName",
    "EventsSinceParams",
    "EventsSinceResult",
    "MaintenanceRunParams",
    "MaintenanceRunResult",
    "ReplayGapInfo",
    "RuntimeDescribeParams",
    "RuntimeDescribeResult",
    "SessionReplayGap",
    "SessionUpdated",
    "SessionsResync",
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


class AdmissionDiscardParams(ProtocolModel):
    """Resolve a turn the daemon lost in a restart (its outcome is unknown): it is never run again
    and the session's queued input may go on."""

    session_id: SessionId
    """A session this connection watches."""
    admission_id: str = Field(min_length=1, max_length=200)
    """The lost turn's admission."""
    generation: NonNegativeInt
    """The lost turn's generation; another value changes nothing (``stale_generation``)."""


class AdmissionDiscardResult(ProtocolModel):
    """Whether the lost turn was discarded."""

    discarded: bool
    """True when it is discarded now (or already was, by this same request)."""
    reason: Literal["stale_generation", "not_found"] | None = None
    """Why nothing changed: the admission is not a lost turn of that generation, or not one of
    this session."""


type AdmissionStateName = Literal["queued", "started", "terminal", "unknown"]
"""Where an input stands: waiting in the FIFO, running, finished, or lost in a restart."""

type AdmissionOutcomeName = Literal[
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "steering",
    "steered",
    "discarded",
    "replaced",
]
"""How a finished input ended (``steered``: folded into the running turn; ``replaced``: a queued
input swapped for another; ``discarded``: a lost turn resolved by ``admission.discard``)."""


class AdmissionReceiptParams(ProtocolModel):
    """What became of an input (for a client that lost the answer to its submit)."""

    session_id: SessionId
    """A session this connection watches."""
    admission_id: str = Field(min_length=1, max_length=200)
    """The input's admission (``turn_id`` in the submit answer)."""


class AdmissionReceiptResult(ProtocolModel):
    """The input's place in the session's queue, or how it ended."""

    admission_id: str
    """The admission asked about."""
    session_id: str
    """The session it belongs to."""
    state: AdmissionStateName
    """``queued``, ``started``, ``terminal`` or ``unknown`` (lost in a restart: never re-run)."""
    generation: NonNegativeInt | None = None
    """The turn that runs (or ran) it, once claimed."""
    position: PositiveInt | None = None
    """1-based place in the queue while queued."""
    outcome: AdmissionOutcomeName | None = None
    """How it ended, once terminal."""
    blocked_by: str | None = None
    """The lost (``unknown``) admission the session's queue waits behind, if any."""
    retired: bool = False
    """True when the session was deleted; it takes no new input."""


class RuntimeDescribeParams(ProtocolModel):
    """The daemon's attach descriptor: which profile and daemon start this is, what it offers."""


class RuntimeDescribeResult(ProtocolModel):
    """What the control socket's ``describe`` answers, plus the replay epoch."""

    profile_id: str
    """The profile the daemon serves (a hash of its home); a client refuses another one."""
    instance_id: str
    """This daemon start; it changes on every restart."""
    code_version: str
    """The daemon's code version."""
    protocol_version: NonNegativeInt
    """The protocol version the daemon speaks."""
    replay_epoch: str
    """Event cursors are only valid within this epoch."""
    ws_url: str | None = None
    """The loopback WebSocket URL (no credentials ride in it)."""
    creatable_sources: list[str] = Field(default_factory=list[str])
    """Surface kinds a client may create sessions as (``cli``, ``tui``, ...)."""
    capabilities: list[str] = Field(default_factory=list[str])
    """What the daemon offers attached clients (``events.since``, ``server_requests``, ...)."""


class MaintenanceRunParams(ProtocolModel):
    """Run one bulk store operation inside the daemon that owns the store (serialised with live
    turns)."""

    op: Literal["prune", "delete", "repair", "import"]
    """``prune`` deletes ``ids``; ``delete`` one session's whole chain; ``repair`` rebuilds the
    search index; ``import`` reads a JSONL export from ``path``."""
    ids: list[str] | None = Field(default=None, max_length=1000)
    """Sessions to delete (``prune``)."""
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    """The session to delete (``delete``)."""
    path: str | None = Field(default=None, min_length=1)
    """The absolute path of the export to import (``import``)."""


class MaintenanceRunResult(ProtocolModel):
    """What the operation did, or why it did nothing."""

    op: str
    """The operation asked for."""
    refused: str | None = None
    """Why nothing was done: ``bad_op``, ``bad_params``, ``guarded`` or ``store_error``."""
    detail: str | None = None
    """A short explanation of the refusal."""
    report: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """The operation's counts (``deleted``, ``removed``, ``rebuilt``, ``imported`` ...)."""


# -- notifications (not numbered) -----------------------------------------------------------------


class SessionReplayGap(ProtocolModel):
    """The events after this viewer's cursor cannot be sent; reload a snapshot of the session."""

    type: Literal["session.replay_gap"] = "session.replay_gap"
    """Notification type."""
    session_id: str
    """The session the viewer watches."""
    replay_epoch: str
    """The daemon's current replay epoch."""
    last_seq: NonNegativeInt
    """The newest seq of the session: the cursor to hold after the snapshot."""
    reason: Literal["epoch_changed", "evicted", "ahead", "overflow"]
    """Another daemon run, dropped from memory, a cursor past the end, or the viewer fell behind
    (it is detached and subscribes again)."""


class SessionUpdated(ProtocolModel):
    """A session changed in a way list views show (sent to ``sessions.watch`` watchers)."""

    type: Literal["session.updated"] = "session.updated"
    """Notification type."""
    session_id: str
    """The session that changed."""
    last_activity: float | None = None
    """When it last changed (seconds since the epoch)."""
    running: bool | None = None
    """Whether a turn runs in it now."""
    title: str | None = None
    """Its title (absent or null when it has none)."""
    archived: bool | None = None
    """Whether it is archived."""
    pinned: bool | None = None
    """Whether it is pinned."""


class SessionsResync(ProtocolModel):
    """List updates for this connection were dropped; reload the list (``session.list``)."""

    reason: Literal["overflow"]
    """Too many updates piled up for the connection."""


ATTACH_NOTIFICATIONS: dict[str, type[ProtocolModel]] = {
    "session.replay_gap": SessionReplayGap,
    "session.updated": SessionUpdated,
    "sessions.resync": SessionsResync,
}
"""Notifications of the daemon's WebSocket that are not numbered events: method → params model."""


type _Row = tuple[str, type[ProtocolModel], type[ProtocolModel], tuple[ErrorCode, ...]]

_SESSION = (SESSION_NOT_FOUND, SESSION_REFUSED, STORE_FAILED)

ATTACH_METHODS: tuple[_Row, ...] = (
    ("session.subscribe", SessionSubscribeParams, SessionSubscribeResult, _SESSION),
    ("session.unsubscribe", SessionUnsubscribeParams, SessionUnsubscribeResult, ()),
    ("sessions.watch", SessionsWatchParams, SessionsWatchResult, ()),
    ("events.since", EventsSinceParams, EventsSinceResult, (SESSION_NOT_FOUND,)),
    ("admission.discard", AdmissionDiscardParams, AdmissionDiscardResult, (SESSION_NOT_FOUND,)),
    (
        "admission.receipt",
        AdmissionReceiptParams,
        AdmissionReceiptResult,
        (SESSION_NOT_FOUND, ADMISSION_NOT_FOUND),
    ),
    ("runtime.describe", RuntimeDescribeParams, RuntimeDescribeResult, ()),
    ("maintenance.run", MaintenanceRunParams, MaintenanceRunResult, (STORE_FAILED,)),
)
"""``(name, params, result, extra errors)`` for the attach methods, in declaration order."""

DAEMON_ONLY_METHODS: frozenset[str] = frozenset(row[0] for row in ATTACH_METHODS)
"""Declared names only the daemon's WebSocket serves (the stdio bridge answers ``-32601``)."""
