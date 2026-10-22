"""The ``groups.*`` methods the desktop and TUI room views call (P24-06).

``GroupsRpc(store, driver, process_generation=)`` answers each method with plain JSON: params are
checked against a strict model (an unknown key is refused), results are dictionaries. The table is
transport-free; the daemon's dispatcher mounts ``handlers()`` once the names are declared in the
protocol (wiring for the lead).

Methods: ``groups.capabilities``, ``groups.list``, ``groups.create``, ``groups.state``,
``groups.log``, ``groups.send``, ``groups.rename``, ``groups.disband``, ``groups.stop``,
``groups.retry``, ``groups.status``, ``groups.promote``, ``groups.demote``, ``groups.approve``.

``groups.send`` stores the person's message under an id derived from the client's retry key (a
retried send is a no-op) and, unless ``detect_stop`` is false, holds every member a stop directive
names (``member.held`` event, its queued turns cancelled). ``groups.stop`` logs
``room.stop_requested``, cancels queued turns and asks running ones to stop (two-phase).

``groups.approve`` (wave 47, RS3) is the person's answer to an approval a member's turn asked
(``approval.requested``, logged by the room loop): it is stored as ``approval.answered`` (a person
event: neither a member nor the gateway may write one) and the room loop hands it to the member's
session. Only a request that is open (asked, not yet ``approval.settled``) takes an answer, only a
choice the request offered, and only the first answer: the same answer again is a no-op, a
different one ``rooms.event_conflict``.

Authority transfer is an **operator recovery procedure, not an atomic hand-over**:

1. fence the old authority yourself (stop its gateway and keep it from restarting): nothing here
   can reach another host, and a stop request or a lost connection is no proof it stopped;
2. ``groups.promote {room_id, confirm: true}`` on the replacement claims the room at the next
   epoch (without ``confirm: true`` it is refused with ``rooms.confirm_required``);
3. ``groups.demote {room_id, observed_gateway_id, observed_epoch}`` on the old host, before it
   writes again, adopts that newer lineage; it refuses anything that is not strictly newer.

Promoting while the old host can still write lets two stores take messages and diverge; neither
call merges histories.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from indus.gateway.rooms.driver import (
    BadTransition,
    DriverError,
    RoomDriver,
    StaleTask,
    Task,
    stop_directives,
)
from indus.state.rooms import (
    PROTOCOL_VERSION,
    RoomAuthorityConflict,
    RoomError,
    RoomStore,
    user_event_id,
)

__all__ = [
    "METHODS",
    "ApprovalNotOpen",
    "ConfirmRequired",
    "GroupsRpc",
    "GroupsRpcError",
    "approval_event_id",
]

RETRY_LEASE_S: Final = 60.0
_LIVE: Final = ("queued", "running", "stopping")
_RETRYABLE: Final = ("indeterminate", "deferred")


class GroupsRpcError(RoomError):
    """Bad params or an unknown method."""


class ConfirmRequired(RoomError):
    """Promotion needs the operator's ``confirm: true``."""

    default_code = "rooms.confirm_required"


class ApprovalNotOpen(RoomError):
    """``groups.approve`` named no open approval of the room, or a choice it did not offer."""

    default_code = "rooms.approval_not_open"


def approval_event_id(kind: str, request_id: str) -> str:
    """The room event id of ``approval.<kind>`` for one request (one event of each per request)."""
    return f"approval.{kind}:{request_id}"


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Room(_Params):
    room_id: str


class _List(_Params):
    limit: int = 100
    offset: int = 0
    include_disbanded: bool = False


class _Create(_Room):
    name: str
    members: list[dict[str, Any]]


class _State(_Room):
    include_disbanded: bool = False


class _Log(_Room):
    since_seq: int = 0
    limit: int = 100
    include_disbanded: bool = False


class _Send(_Room):
    client_event_id: str
    text: str = Field(min_length=1)
    thread_id: str | None = None
    author: str = "person"
    detect_stop: bool = True


class _Rename(_Room):
    event_id: str
    name: str


class _Stop(_Room):
    cancel_id: str


class _Retry(_Room):
    task_id: str


class _Promote(_Room):
    confirm: bool = False
    reason: str = "authority-unreachable"


class _Demote(_Room):
    observed_gateway_id: str
    observed_epoch: int


class _Approve(_Room):
    request_id: str
    choice: Literal["once", "session", "always", "deny"]
    reason: str | None = Field(default=None, max_length=2000)
    author: str = "person"


def _handle(member: Mapping[str, Any]) -> str | None:
    for key in ("handle", "profile", "id"):
        value = member.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _short(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


class GroupsRpc:
    """The ``groups.*`` handler table over one room store and its driver."""

    def __init__(self, store: RoomStore, driver: RoomDriver, *, process_generation: str) -> None:
        self.store = store
        self.driver = driver
        self.process_generation = process_generation
        self._table: dict[str, tuple[type[_Params], Callable[[Any], dict[str, Any]]]] = {
            "groups.capabilities": (_Params, self._capabilities),
            "groups.list": (_List, self._list),
            "groups.create": (_Create, self._create),
            "groups.state": (_State, self._state),
            "groups.log": (_Log, self._log),
            "groups.send": (_Send, self._send),
            "groups.rename": (_Rename, self._rename),
            "groups.disband": (_Room, self._disband),
            "groups.stop": (_Stop, self._stop),
            "groups.retry": (_Retry, self._retry),
            "groups.status": (_Room, self._status),
            "groups.promote": (_Promote, self._promote),
            "groups.demote": (_Demote, self._demote),
            "groups.approve": (_Approve, self._approve),
        }

    # -- dispatch --------------------------------------------------------------------------------

    def handlers(self) -> dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]]:
        return {name: (lambda params, n=name: self.call(n, params)) for name in self._table}

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        entry = self._table.get(method)
        if entry is None:
            raise GroupsRpcError(f"unknown method {method!r}")
        model, handler = entry
        try:
            parsed = model.model_validate(dict(params or {}))
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ())) or "params"
            raise GroupsRpcError(f"bad params at {where}: {first.get('msg', 'invalid')}") from None
        return handler(parsed)

    # -- helpers ---------------------------------------------------------------------------------

    def _authority(self, room_id: str) -> tuple[str, int]:
        room = self.store.get_room(room_id)
        return room.authority, room.epoch

    def _own_authority(self, room_id: str) -> tuple[str, int]:
        authority, epoch = self._authority(room_id)
        if authority != self.store.gateway_id:
            raise RoomAuthorityConflict("this gateway is not the room's authority; ask that one")
        return authority, epoch

    # -- methods ---------------------------------------------------------------------------------

    def _capabilities(self, _: _Params) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL_VERSION,
            "driver": True,
            "gateway_id": self.store.gateway_id,
            "methods": sorted(self._table),
            "authority_transfer": "operator recovery; not atomic",
        }

    def _list(self, p: _List) -> dict[str, Any]:
        rooms = self.store.list_rooms(
            limit=p.limit, offset=p.offset, include_disbanded=p.include_disbanded
        )
        return {"rooms": [r.to_dict() for r in rooms]}

    def _create(self, p: _Create) -> dict[str, Any]:
        room = self.store.create_room(p.room_id, p.name, p.members)
        return {"room": room.to_dict(), "idempotent": room.idempotent}

    def _state(self, p: _State) -> dict[str, Any]:
        room = self.store.get_room(p.room_id, include_disbanded=p.include_disbanded)
        out: dict[str, Any] = {"room": room.to_dict()}
        claim = self.store.authority_claim(p.room_id, include_disbanded=p.include_disbanded)
        if claim is not None:
            out["authority_claim"] = claim.to_dict()
        return out

    def _log(self, p: _Log) -> dict[str, Any]:
        page = self.store.read_events(
            p.room_id, since_seq=p.since_seq, limit=p.limit, include_disbanded=p.include_disbanded
        )
        return page.to_dict()

    def _send(self, p: _Send) -> dict[str, Any]:
        authority, epoch = self._own_authority(p.room_id)
        room = self.store.get_room(p.room_id)
        payload: dict[str, Any] = {"text": p.text}
        if p.thread_id is not None:
            payload["thread_id"] = p.thread_id
        event = self.store.append_event(
            p.room_id,
            user_event_id(p.client_event_id),
            "message.user",
            {"kind": "user", "id": p.author},
            payload,
            authority=authority,
            epoch=epoch,
        )
        handles = [h for h in (_handle(m) for m in room.members) if h]
        held = sorted(stop_directives(p.text, handles, enabled=p.detect_stop))
        cancelled: list[str] = []
        for handle in held:
            self.store.append_event(
                p.room_id,
                f"held:{_short(event.event_id, handle)}",
                "member.held",
                {"kind": "gateway", "id": authority},
                {"member": handle, "source_seq": event.seq},
                authority=authority,
                epoch=epoch,
            )
            cancelled += self._cancel_queued(p.room_id, f"hold:{event.seq}", target=handle)
        return {"event": event.to_dict(), "held": held, "cancelled": cancelled}

    def _cancel_queued(self, room_id: str, cancel_id: str, *, target: str | None) -> list[str]:
        done: list[str] = []
        for task in self.driver.list_tasks(room_id, status="queued"):
            if target is not None and task.payload.get("target") != target:
                continue
            try:
                self.driver.cancel_queued(
                    task.key, cancel_id=cancel_id, expected_cancel_gen=task.cancel_gen
                )
            except (StaleTask, BadTransition):
                continue  # it moved on meanwhile; the next pass sees its new state
            done.append(task.key.task_id)
        return done

    def _rename(self, p: _Rename) -> dict[str, Any]:
        authority, epoch = self._own_authority(p.room_id)
        room = self.store.rename_room(
            p.room_id, p.event_id, p.name, authority=authority, epoch=epoch
        )
        return {"room": room.to_dict(), "idempotent": room.idempotent}

    def _disband(self, p: _Room) -> dict[str, Any]:
        authority, epoch = self._own_authority(p.room_id)
        done = self.store.disband_room(p.room_id, authority=authority, epoch=epoch)
        return {
            "room_id": done.room_id,
            "disbanded_at": done.disbanded_at,
            "idempotent": done.idempotent,
        }

    def _stop(self, p: _Stop) -> dict[str, Any]:
        authority, epoch = self._own_authority(p.room_id)
        event = self.store.request_stop(p.room_id, p.cancel_id, authority=authority, epoch=epoch)
        cancelled = self._cancel_queued(p.room_id, p.cancel_id, target=None)
        stopping: list[str] = []
        for task in self.driver.list_tasks(p.room_id, status="running"):
            try:
                self.driver.begin_stop(
                    task.key, cancel_id=p.cancel_id, expected_cancel_gen=task.cancel_gen
                )
            except (StaleTask, BadTransition):
                continue
            stopping.append(task.key.task_id)
        return {"event": event.to_dict(), "cancelled": cancelled, "stopping": stopping}

    def _find(self, room_id: str, task_id: str) -> Task:
        for task in self.driver.list_tasks(room_id):
            if task.key.task_id == task_id:
                return task
        raise DriverError("no task has this id in this room")

    def _retry(self, p: _Retry) -> dict[str, Any]:
        """An operator's explicit retry of a deferred or indeterminate turn (it may run twice)."""
        self._own_authority(p.room_id)
        task = self._find(p.room_id, p.task_id)
        if task.status not in _RETRYABLE:
            raise BadTransition(f"a {task.status} task is not waiting for a retry")
        lease = self.driver.acquire_lease(
            p.room_id, process_generation=self.process_generation, ttl_seconds=RETRY_LEASE_S
        )
        move = (
            self.driver.requeue_deferred
            if task.status == "deferred"
            else self.driver.requeue_indeterminate
        )
        done = move(
            task.key, lease, expected_exec_gen=task.exec_gen, expected_cancel_gen=task.cancel_gen
        )
        return {"task": done.to_dict()}

    def _status(self, p: _Room) -> dict[str, Any]:
        tasks = self.driver.list_tasks(p.room_id)
        counts = Counter(t.status for t in tasks)
        return {
            "working": any(counts.get(s) for s in _LIVE),
            "blocked": bool(counts.get("indeterminate") or counts.get("stopping")),
            "counts": dict(counts),
            "pending_actions": [
                {"kind": "retry", "task_id": t.key.task_id} for t in tasks if t.status in _RETRYABLE
            ],
        }

    def _promote(self, p: _Promote) -> dict[str, Any]:
        if p.confirm is not True:
            raise ConfirmRequired(
                "promotion is an operator recovery step: fence the old authority first, then "
                "send confirm: true"
            )
        authority, epoch = self._authority(p.room_id)
        me = self.store.gateway_id
        if authority == me:
            return {"authority": me, "epoch": epoch, "idempotent": True, "reason": p.reason}
        room = self.store.claim_authority(
            p.room_id,
            expected=authority,
            expected_epoch=epoch,
            new=me,
            event_id=f"promote-{epoch + 1}-{_short(me)}",
        )
        return {
            "authority": room.authority,
            "epoch": room.epoch,
            "idempotent": room.idempotent,
            "reason": p.reason,
        }

    def _demote(self, p: _Demote) -> dict[str, Any]:
        room = self.store.record_authority_lost(
            p.room_id, observed=p.observed_gateway_id, observed_epoch=p.observed_epoch
        )
        return {"authority": room.authority, "epoch": room.epoch, "idempotent": room.idempotent}

    def _approve(self, p: _Approve) -> dict[str, Any]:
        """The person's answer to a member turn's approval (see the module notes)."""
        authority, epoch = self._own_authority(p.room_id)
        asked = self.store.find_event(p.room_id, approval_event_id("requested", p.request_id))
        if asked is None or asked.kind != "approval.requested":
            raise ApprovalNotOpen("no approval request has this id in this room")
        answered = approval_event_id("answered", p.request_id)
        if self.store.find_event(p.room_id, approval_event_id("settled", p.request_id)) and (
            self.store.find_event(p.room_id, answered) is None
        ):
            raise ApprovalNotOpen("this approval request is no longer open")
        offered = asked.payload.get("choices")
        if not isinstance(offered, list) or p.choice not in offered:
            raise ApprovalNotOpen(f"this approval request does not offer {p.choice!r}")
        payload: dict[str, Any] = {
            "request_id": p.request_id,
            "task_id": asked.payload.get("task_id"),
            "member": asked.payload.get("member"),
            "choice": p.choice,
        }
        if p.choice == "deny" and p.reason:
            payload["reason"] = p.reason
        event = self.store.append_event(
            p.room_id,
            answered,
            "approval.answered",
            {"kind": "user", "id": p.author},
            payload,
            authority=authority,
            epoch=epoch,
        )
        return {"event": event.to_dict(), "idempotent": event.idempotent}


METHODS: Final = (
    "groups.capabilities",
    "groups.list",
    "groups.create",
    "groups.state",
    "groups.log",
    "groups.send",
    "groups.rename",
    "groups.disband",
    "groups.stop",
    "groups.retry",
    "groups.status",
    "groups.promote",
    "groups.demote",
    "groups.approve",
)
