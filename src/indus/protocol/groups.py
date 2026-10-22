"""``groups.*``: the group-room methods the desktop and TUI room views call (P24-06).

The wire models and ``GROUP_METHODS`` rows (``(name, params, result, extra errors)``) live here so
the method catalogue and the generated TypeScript include them. The handlers are
``indus.gateway.rooms.rpc.GroupsRpc``, which checks the same params with its own strict models and
answers plain JSON; the daemon's WebSocket validates that JSON against the result models below.
Rooms, events and tasks travel as JSON objects (their keys are the room store's ``to_dict``
shapes, documented in ``docs/notes/P24/room-store.md``).

Wave 47 (RS3): ``groups.approve`` answers an approval a member's turn asked (the room's
``approval.requested`` event); ``groups.watch`` / ``groups.unwatch`` (daemon WebSocket only) make
the connection receive every new event of a room as a ``groups.event`` notification.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, JsonValue

from indus.protocol.base import ProtocolModel
from indus.protocol.errors import ErrorCode


class GroupsNoParams(ProtocolModel):
    """No parameters."""


class GroupsRoomParams(ProtocolModel):
    """One room."""

    room_id: str
    """The room's id."""


class GroupsListParams(ProtocolModel):
    """A page of rooms."""

    limit: int = 100
    """Rooms per page."""
    offset: int = 0
    """Rooms to skip."""
    include_disbanded: bool = False
    """Also list disbanded rooms."""


class GroupsCreateParams(ProtocolModel):
    """Create a room (idempotent for the same id and settings)."""

    room_id: str
    """The new room's id."""
    name: str
    """Its display name."""
    members: list[dict[str, JsonValue]]
    """The members (``{handle | profile | id, ...}``)."""


class GroupsStateParams(ProtocolModel):
    """A room's current state."""

    room_id: str
    """The room's id."""
    include_disbanded: bool = False
    """Answer for a disbanded room too."""


class GroupsLogParams(ProtocolModel):
    """A page of a room's event log."""

    room_id: str
    """The room's id."""
    since_seq: int = 0
    """Events after this sequence number."""
    limit: int = 100
    """Events per page."""
    include_disbanded: bool = False
    """Read a disbanded room's log too."""


class GroupsSendParams(ProtocolModel):
    """The person's message to a room."""

    room_id: str
    """The room's id."""
    client_event_id: str
    """The client's retry key: a repeated send is a no-op."""
    text: str = Field(min_length=1)
    """The message."""
    thread_id: str | None = None
    """A thread inside the room."""
    author: str = "person"
    """Who wrote it."""
    detect_stop: bool = True
    """Hold every member a stop directive in the text names."""


class GroupsRenameParams(ProtocolModel):
    """Rename a room."""

    room_id: str
    """The room's id."""
    event_id: str
    """The rename's idempotency key."""
    name: str
    """The new name."""


class GroupsStopParams(ProtocolModel):
    """Stop every queued and running turn of a room."""

    room_id: str
    """The room's id."""
    cancel_id: str
    """The stop's idempotency key."""


class GroupsRetryParams(ProtocolModel):
    """Retry a deferred or indeterminate turn (it may run twice)."""

    room_id: str
    """The room's id."""
    task_id: str
    """The task to retry."""


class GroupsPromoteParams(ProtocolModel):
    """Claim a room's authority on this gateway (operator recovery; not atomic)."""

    room_id: str
    """The room's id."""
    confirm: bool = False
    """Must be true: the old authority was fenced first."""
    reason: str = "authority-unreachable"
    """Why, for the log."""


class GroupsDemoteParams(ProtocolModel):
    """Adopt a strictly newer authority lineage seen elsewhere."""

    room_id: str
    """The room's id."""
    observed_gateway_id: str
    """The gateway that holds the room now."""
    observed_epoch: int
    """Its epoch."""


class GroupsApproveParams(ProtocolModel):
    """The person's answer to an approval a member's turn put to the room."""

    room_id: str
    """The room's id."""
    request_id: str
    """The ``request_id`` of the room's ``approval.requested`` event."""
    choice: Literal["once", "session", "always", "deny"]
    """One of the choices the request offered."""
    reason: str | None = Field(default=None, max_length=2000)
    """Why the person denied (passed to the member unchanged); ignored for other choices."""
    author: str = "person"
    """Who answered."""


class GroupsWatchParams(ProtocolModel):
    """Receive a room's new events as ``groups.event`` notifications on this connection."""

    room_id: str
    """The room's id."""
    since_seq: int | None = Field(default=None, ge=0)
    """Also send the events after this sequence number; absent means only new ones."""


class GroupsCapabilitiesResult(ProtocolModel):
    """What this gateway's room service offers."""

    protocol: int
    """The room protocol version."""
    driver: bool
    """A room driver runs here."""
    gateway_id: str
    """This gateway's room identity."""
    methods: list[str]
    """The ``groups.*`` methods served."""
    authority_transfer: str
    """How authority moves (an operator recovery step)."""


class GroupsListResult(ProtocolModel):
    """A page of rooms."""

    rooms: list[dict[str, JsonValue]]
    """The rooms."""


class GroupsRoomResult(ProtocolModel):
    """The room a create or rename acted on."""

    room: dict[str, JsonValue]
    """The room."""
    idempotent: bool
    """True when nothing changed (a repeated call)."""


class GroupsStateResult(ProtocolModel):
    """A room and its pending authority claim."""

    room: dict[str, JsonValue]
    """The room."""
    authority_claim: dict[str, JsonValue] | None = None
    """A claim in progress, if any."""


class GroupsLogResult(ProtocolModel):
    """A page of events."""

    events: list[dict[str, JsonValue]]
    """The events, oldest first."""
    cursor: int
    """Pass as ``since_seq`` for the next page."""
    latest_seq: int
    """The room's newest sequence number."""
    has_more: bool
    """More events follow."""
    authority: dict[str, JsonValue]
    """``{gateway_id, epoch}`` of the room's authority."""


class GroupsSendResult(ProtocolModel):
    """The stored message and what its stop directives did."""

    event: dict[str, JsonValue]
    """The stored event."""
    held: list[str]
    """Members a stop directive held."""
    cancelled: list[str]
    """Queued tasks cancelled for them."""


class GroupsDisbandResult(ProtocolModel):
    """The disbanded room."""

    room_id: str
    """The room's id."""
    disbanded_at: float
    """When."""
    idempotent: bool
    """True when it was disbanded already."""


class GroupsStopResult(ProtocolModel):
    """What the stop did."""

    event: dict[str, JsonValue]
    """The ``room.stop_requested`` event."""
    cancelled: list[str]
    """Queued tasks cancelled."""
    stopping: list[str]
    """Running tasks asked to stop."""


class GroupsRetryResult(ProtocolModel):
    """The requeued task."""

    task: dict[str, JsonValue]
    """The task."""


class GroupsStatusResult(ProtocolModel):
    """Whether the room is working, blocked or waiting for an operator."""

    working: bool
    """A turn is queued, running or stopping."""
    blocked: bool
    """A turn is indeterminate or stopping."""
    counts: dict[str, int]
    """Tasks by status."""
    pending_actions: list[dict[str, JsonValue]]
    """What an operator may do (``{kind: retry, task_id}``)."""


class GroupsAuthorityResult(ProtocolModel):
    """The room's authority after a promote or demote."""

    authority: str
    """The gateway that holds the room."""
    epoch: int
    """Its epoch."""
    idempotent: bool
    """True when nothing changed."""
    reason: str | None = None
    """The promote's reason."""


class GroupsApproveResult(ProtocolModel):
    """The stored answer."""

    event: dict[str, JsonValue]
    """The ``approval.answered`` event."""
    idempotent: bool
    """True when the same answer was stored already."""


class GroupsWatchResult(ProtocolModel):
    """The watch is on."""

    room_id: str
    """The room's id."""
    latest_seq: int
    """The room's newest sequence number when the watch began."""


class GroupsUnwatchResult(ProtocolModel):
    """The watch is off."""

    watching: bool
    """False: this connection no longer receives the room's events."""


class GroupsEvent(ProtocolModel):
    """``groups.event``: a new event of a room this connection watches (in order, once)."""

    room_id: str
    """The room's id."""
    event: dict[str, JsonValue]
    """The event (the room store's ``to_dict`` shape)."""


GROUPS_EVENT: Final = "groups.event"

type _Row = tuple[str, type[ProtocolModel], type[ProtocolModel], tuple[ErrorCode, ...]]

GROUP_METHODS: tuple[_Row, ...] = (
    ("groups.capabilities", GroupsNoParams, GroupsCapabilitiesResult, ()),
    ("groups.list", GroupsListParams, GroupsListResult, ()),
    ("groups.create", GroupsCreateParams, GroupsRoomResult, ()),
    ("groups.state", GroupsStateParams, GroupsStateResult, ()),
    ("groups.log", GroupsLogParams, GroupsLogResult, ()),
    ("groups.send", GroupsSendParams, GroupsSendResult, ()),
    ("groups.rename", GroupsRenameParams, GroupsRoomResult, ()),
    ("groups.disband", GroupsRoomParams, GroupsDisbandResult, ()),
    ("groups.stop", GroupsStopParams, GroupsStopResult, ()),
    ("groups.retry", GroupsRetryParams, GroupsRetryResult, ()),
    ("groups.status", GroupsRoomParams, GroupsStatusResult, ()),
    ("groups.promote", GroupsPromoteParams, GroupsAuthorityResult, ()),
    ("groups.demote", GroupsDemoteParams, GroupsAuthorityResult, ()),
    ("groups.approve", GroupsApproveParams, GroupsApproveResult, ()),
    ("groups.watch", GroupsWatchParams, GroupsWatchResult, ()),
    ("groups.unwatch", GroupsRoomParams, GroupsUnwatchResult, ()),
)
"""``(name, params, result, extra errors)`` for the group-room methods, in declaration order."""
