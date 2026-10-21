"""Group rooms: durable chats in which several profiles talk, and their append-only logs (P24-05).

The store is ``<root>/rooms.db``: one file per install (shared by every profile and by every
gateway process under the root), never a profile's session store. It holds

- ``rooms``: identity (id, name, members), the room's authority (the gateway id allowed to write
  and its epoch), the next log sequence number, the bytes the log uses, and ``disbanded_at``;
- ``room_events``: the log, one immutable row per event, numbered per room (``seq``) and keyed by
  a caller-chosen ``event_id`` that is idempotent: the same id with the same content returns the
  stored event, the same id with other content is refused (``RoomEventConflict``);
- ``retired_rooms``: every id ever disbanded. A retired id can never be created again, even after
  its log was purged;
- ``room_meta``: this install's gateway id (minted on first open).

Who may append is fixed per actor kind (``ACTOR_EVENT_KINDS``): a person writes only
``message.user``, a member only ``message.member``, the gateway (which must be the room's current
authority) the turn and stop events, the system the room and authority events. Every write names
the authority it believes in (gateway id + epoch); a write under another authority is refused
(``RoomAuthorityConflict``). ``claim_authority`` moves the epoch by one behind a compare-and-set,
``record_authority_lost`` adopts a newer lineage another host proved; neither is a remote fence
(the room driver documents the operator procedure).

Bounds (``RoomLimits``; research 06 §3.20): ids ≤ 128 characters, names ≤ 200, event kinds ≤ 64
matching ``EVENT_KIND_RE``, ≤ 128 members (≤ 128 KiB of JSON), an event's payload ≤ 256 KiB, a log
page ≤ 500 events and ≤ 2 MiB, ≤ 256 live rooms, ≤ 512 disbanded rooms kept for at most 90 days,
≤ 50,000 events and ≤ 256 MiB per room, ≤ 16 MiB of events per gateway. The control events
(``CONTROL_KINDS``) may go 64 events / 1 MiB past the room and gateway bounds, so a full room can
still be stopped, disbanded or handed over. Before refusing for the gateway bound, the oldest
disbanded rooms are purged.

Writes go through the store's single ``Writer`` (``BEGIN IMMEDIATE``, deadline retries; R03.8).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.state import journal
from indus.state.connection import open_store
from indus.state.writer import Writer

__all__ = [
    "ACTOR_EVENT_KINDS",
    "CONTROL_KINDS",
    "DEFAULT_LIMITS",
    "EVENT_KIND_RE",
    "ID_RE",
    "PROTOCOL_VERSION",
    "ROOMS_FILE",
    "LogPage",
    "Room",
    "RoomAuthorityConflict",
    "RoomAuthoritySuperseded",
    "RoomConflict",
    "RoomCursorMoved",
    "RoomError",
    "RoomEvent",
    "RoomEventConflict",
    "RoomFull",
    "RoomLimits",
    "RoomNotFound",
    "RoomStore",
    "user_event_id",
]

ROOMS_FILE: Final = "rooms.db"
PROTOCOL_VERSION: Final = 2
SCHEMA_VERSION: Final = 1
KIB: Final = 1024
MIB: Final = 1024 * KIB
DAY: Final = 86_400.0

EVENT_KIND_RE: Final = re.compile(r"^[a-z][a-z0-9._-]*$")
ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")

CONTROL_KINDS: Final = frozenset(
    {"authority.claimed", "authority.lost", "room.disbanded", "room.stop_requested"}
)
ACTOR_EVENT_KINDS: Final[Mapping[str, frozenset[str]]] = {
    "user": frozenset({"message.user"}),
    "member": frozenset({"message.member"}),
    "gateway": frozenset(
        {
            "room.stop_requested",
            "turn.started",
            "turn.settled",
            "turn.failed",
            "turn.cancelled",
            "turn.expired",
            "member.held",
            "member.released",
            "member.unavailable",
        }
    ),
    "system": frozenset(
        {
            "room.created",
            "room.renamed",
            "room.members_changed",
            "room.disbanded",
            "authority.claimed",
            "authority.lost",
        }
    ),
}
_ACTOR_OPTIONAL: Final = {"display_name": 200, "profile": 128, "device": 128}

# Tables whose rows belong to one room; purged with a disbanded room (the driver's tables first).
DEPENDENT_TABLES: Final = ("room_tasks", "room_leases", "room_events")


@dataclass(frozen=True)
class RoomLimits:
    """Every bound of the store. The defaults are the spec table; tests shrink the large ones."""

    max_id_chars: int = 128
    max_name_chars: int = 200
    max_kind_chars: int = 64
    max_members: int = 128
    max_members_bytes: int = 128 * KIB
    max_event_bytes: int = 256 * KIB
    max_actor_bytes: int = 4 * KIB
    max_page_events: int = 500
    max_page_bytes: int = 2 * MIB
    max_list: int = 500
    max_active_rooms: int = 256
    max_tombstones: int = 512
    tombstone_seconds: float = 90 * DAY
    max_room_events: int = 50_000
    max_room_bytes: int = 256 * MIB
    max_gateway_bytes: int = 16 * MIB
    reserve_events: int = 64
    reserve_bytes: int = 1 * MIB


DEFAULT_LIMITS: Final = RoomLimits()


# -- errors -----------------------------------------------------------------------------------


class RoomError(IndusError):
    """An invalid room operation (bad id, name, kind, actor or payload)."""

    default_code = "rooms.invalid"


class RoomNotFound(RoomError):
    default_code = "rooms.not_found"


class RoomConflict(RoomError):
    """The room id is taken with other state, or belongs to a disbanded room."""

    default_code = "rooms.conflict"


class RoomEventConflict(RoomError):
    """The event id is already bound to other content."""

    default_code = "rooms.event_conflict"


class RoomCursorMoved(RoomError):
    """New events arrived after the caller's expected sequence number."""

    default_code = "rooms.cursor_moved"


class RoomAuthorityConflict(RoomError):
    """The writer is not the room's current authority (gateway id and epoch)."""

    default_code = "rooms.authority_conflict"


class RoomAuthoritySuperseded(RoomAuthorityConflict):
    """This claim was committed once, but a newer authority has taken the room since."""

    default_code = "rooms.authority_superseded"


class RoomFull(RoomError):
    """A count or size bound was reached; the message is fit to show a person."""

    default_code = "rooms.full"


# -- records ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Room:
    room_id: str
    name: str
    members: list[dict[str, Any]]
    authority: str
    epoch: int
    latest_seq: int
    revision: int
    created_at: float
    updated_at: float
    disbanded_at: float | None = None
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "room_id": self.room_id,
            "name": self.name,
            "members": [dict(m) for m in self.members],
            "authority": self.authority,
            "epoch": self.epoch,
            "latest_seq": self.latest_seq,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.disbanded_at is not None:
            out["disbanded_at"] = self.disbanded_at
        return out


@dataclass(frozen=True)
class RoomEvent:
    room_id: str
    seq: int
    event_id: str
    kind: str
    actor: dict[str, Any]
    epoch: int
    payload: dict[str, Any]
    created_at: float
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "seq": self.seq,
            "event_id": self.event_id,
            "kind": self.kind,
            "actor": dict(self.actor),
            "epoch": self.epoch,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class LogPage:
    events: list[RoomEvent]
    cursor: int
    latest_seq: int
    authority: str
    epoch: int
    has_more: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "has_more", self.cursor < self.latest_seq)

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [e.to_dict() for e in self.events],
            "cursor": self.cursor,
            "latest_seq": self.latest_seq,
            "has_more": self.has_more,
            "authority": {"gateway_id": self.authority, "epoch": self.epoch},
        }


@dataclass(frozen=True)
class Disbanded:
    room_id: str
    disbanded_at: float
    idempotent: bool
    purged: bool = False


# -- helpers ----------------------------------------------------------------------------------


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utf8(*parts: str) -> int:
    return sum(len(p.encode("utf-8")) for p in parts)


def _bounded_json(value: object, label: str, limit: int) -> str:
    try:
        text = _compact(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise RoomError(f"{label} must be plain JSON") from exc
    if _utf8(text) > limit:
        raise RoomError(f"{label} is larger than {limit:,} bytes")
    return text


def user_event_id(client_key: str) -> str:
    """A person's retry key mapped into a namespace no other actor can write into."""
    return "user:" + hashlib.sha256(client_key.encode("utf-8")).hexdigest()


def _stop_event_id(cancel_id: str) -> str:
    return "stop:" + hashlib.sha256(cancel_id.encode("utf-8")).hexdigest()[:40]


def _lost_event_id(gateway_id: str, epoch: int) -> str:
    digest = hashlib.sha256(f"{gateway_id}\n{epoch}".encode()).hexdigest()[:40]
    return f"authority-lost:{digest}"


_SCHEMA: Final = (
    "CREATE TABLE IF NOT EXISTS room_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS rooms (
        room_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        members TEXT NOT NULL,
        authority TEXT NOT NULL,
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        next_seq INTEGER NOT NULL DEFAULT 1 CHECK (next_seq >= 1),
        log_bytes INTEGER NOT NULL DEFAULT 0 CHECK (log_bytes >= 0),
        revision INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        disbanded_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS room_events (
        room_id TEXT NOT NULL REFERENCES rooms(room_id),
        seq INTEGER NOT NULL CHECK (seq >= 1),
        event_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        actor TEXT NOT NULL,
        epoch INTEGER NOT NULL,
        payload TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (room_id, seq),
        UNIQUE (room_id, event_id)
    )""",
    "CREATE TABLE IF NOT EXISTS retired_rooms (room_id TEXT PRIMARY KEY, retired_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS rooms_by_disband ON rooms (disbanded_at)",
)

_ROOM_SQL: Final = (
    "SELECT room_id, name, members, authority, epoch, next_seq, log_bytes, revision, created_at, "
    "updated_at, disbanded_at FROM rooms WHERE room_id = ?"
)
_EVENT_COLS: Final = "room_id, seq, event_id, kind, actor, epoch, payload, created_at"


def _room_of(row: sqlite3.Row, *, idempotent: bool = False) -> Room:
    disbanded = row["disbanded_at"]
    return Room(
        room_id=str(row["room_id"]),
        name=str(row["name"]),
        members=cast(list[dict[str, Any]], json.loads(row["members"])),
        authority=str(row["authority"]),
        epoch=int(row["epoch"]),
        latest_seq=int(row["next_seq"]) - 1,
        revision=int(row["revision"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        disbanded_at=float(disbanded) if disbanded is not None else None,
        idempotent=idempotent,
    )


def _event_of(row: sqlite3.Row, *, idempotent: bool = False) -> RoomEvent:
    return RoomEvent(
        room_id=str(row["room_id"]),
        seq=int(row["seq"]),
        event_id=str(row["event_id"]),
        kind=str(row["kind"]),
        actor=cast(dict[str, Any], json.loads(row["actor"])),
        epoch=int(row["epoch"]),
        payload=cast(dict[str, Any], json.loads(row["payload"])),
        created_at=float(row["created_at"]),
        idempotent=idempotent,
    )


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return found is not None


@dataclass(frozen=True)
class _Pending:
    """One validated event waiting for its sequence number."""

    event_id: str
    kind: str
    actor: str
    payload: str

    @property
    def size(self) -> int:
        return _utf8(self.event_id, self.kind, self.actor, self.payload)

    @property
    def control(self) -> bool:
        return self.kind in CONTROL_KINDS


# -- the store --------------------------------------------------------------------------------


class RoomStore:
    """The install's group-room store (see the module notes)."""

    def __init__(
        self, scope: Scope, path: Path | None = None, *, limits: RoomLimits = DEFAULT_LIMITS
    ) -> None:
        self.scope = scope
        self.limits = limits
        self.path = path if path is not None else scope.root / ROOMS_FILE
        handle = open_store(scope, self.path, on_connected=self._setup)
        self.path = handle.path
        self.writer = Writer(self.path, handle.conn, clock=scope.clock)
        self.gateway_id: str = self.read(self._gateway_id_in)

    # -- plumbing --------------------------------------------------------------------------------

    def _setup(self, conn: sqlite3.Connection) -> None:
        cfg = journal.database_config(self.scope)
        journal.apply(conn, cfg, self.path)
        journal.apply_pragmas(conn, cfg)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(
                "INSERT OR IGNORE INTO room_meta (key, value) VALUES ('gateway_id', ?)",
                ("gw-" + secrets.token_hex(12),),
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _gateway_id_in(conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT value FROM room_meta WHERE key = 'gateway_id'").fetchone()
        return str(row["value"])

    def ensure_schema(self, statements: Sequence[str]) -> None:
        """Create a companion module's tables (the room driver) in this file."""

        def run(conn: sqlite3.Connection) -> None:
            for statement in statements:
                conn.execute(statement)

        self.write(run)

    def read[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self.writer.borrow() as conn:
            return fn(conn)

    def write[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return self.writer.write(fn)

    def close(self) -> None:
        self.writer.close()

    def now(self) -> float:
        value = self.scope.clock.now().timestamp()
        if not math.isfinite(value):
            raise RoomError("the clock returned a non-finite time")
        return value

    # -- validation ------------------------------------------------------------------------------

    def _id(self, value: object, label: str) -> str:
        if not isinstance(value, str):
            raise RoomError(f"{label} must be text")
        text = value.strip()
        if not text or len(text) > self.limits.max_id_chars or not ID_RE.fullmatch(text):
            raise RoomError(f"{label} is not a valid id (1–{self.limits.max_id_chars} characters)")
        return text

    def _name(self, value: object) -> str:
        if not isinstance(value, str):
            raise RoomError("the room name must be text")
        text = " ".join(value.split())
        if not text or len(text) > self.limits.max_name_chars:
            raise RoomError(f"the room name must be 1–{self.limits.max_name_chars} characters")
        return text

    def _kind(self, value: object) -> str:
        if (
            not isinstance(value, str)
            or len(value) > self.limits.max_kind_chars
            or not EVENT_KIND_RE.fullmatch(value)
        ):
            raise RoomError("invalid event kind")
        return value

    def _epoch(self, value: object, label: str = "epoch") -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RoomError(f"{label} must be a positive whole number")
        return value

    def _members(self, value: object) -> tuple[list[dict[str, Any]], str]:
        if not isinstance(value, (list, tuple)):
            raise RoomError("members must be a list")
        items = cast(Sequence[object], value)
        if len(items) > self.limits.max_members:
            raise RoomError(f"a room has at most {self.limits.max_members} members")
        members: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise RoomError("each member must be an object")
            members.append(dict(cast(Mapping[str, Any], item)))
        return members, _bounded_json(members, "members", self.limits.max_members_bytes)

    def _actor(self, value: object, kind: str) -> tuple[dict[str, str], str]:
        if not isinstance(value, Mapping):
            raise RoomError("actor must be an object")
        raw = cast(Mapping[str, object], value)
        unknown = set(raw) - {"kind", "id", *_ACTOR_OPTIONAL}
        if unknown:
            raise RoomError(f"unknown actor fields: {', '.join(sorted(unknown))}")
        actor_kind = raw.get("kind")
        if not isinstance(actor_kind, str) or actor_kind not in ACTOR_EVENT_KINDS:
            raise RoomError("invalid actor kind")
        if kind not in ACTOR_EVENT_KINDS[actor_kind]:
            raise RoomError(f"a {actor_kind} actor cannot append {kind!r}")
        actor: dict[str, str] = {"kind": actor_kind, "id": self._id(raw.get("id"), "actor id")}
        for name, limit in _ACTOR_OPTIONAL.items():
            extra = raw.get(name)
            if extra is None:
                continue
            if not isinstance(extra, str):
                raise RoomError(f"actor {name} must be text")
            extra = extra.strip()
            if len(extra) > limit:
                raise RoomError(f"actor {name} is too long")
            if extra:
                actor[name] = extra
        return actor, _bounded_json(actor, "actor", self.limits.max_actor_bytes)

    def _payload(self, value: object) -> str:
        if not isinstance(value, Mapping):
            raise RoomError("payload must be an object")
        payload = dict(cast(Mapping[str, object], value))
        return _bounded_json(payload, "payload", self.limits.max_event_bytes)

    # -- row access ------------------------------------------------------------------------------

    def _row_in(
        self, conn: sqlite3.Connection, room_id: str, *, include_disbanded: bool = False
    ) -> sqlite3.Row:
        row = conn.execute(_ROOM_SQL, (room_id,)).fetchone()
        if row is None:
            retired = conn.execute(
                "SELECT 1 FROM retired_rooms WHERE room_id = ?", (room_id,)
            ).fetchone()
            if retired is not None:
                raise RoomNotFound("this room was disbanded and its history has expired")
            raise RoomNotFound("no room has this id")
        if row["disbanded_at"] is not None and not include_disbanded:
            raise RoomNotFound("this room was disbanded")
        return row

    def live_row_in(self, conn: sqlite3.Connection, room_id: str) -> sqlite3.Row:
        """The live room's row (for companion modules running in this store's transactions)."""
        return self._row_in(conn, self._id(room_id, "room id"))

    @staticmethod
    def require_authority(row: sqlite3.Row, gateway_id: str, epoch: int) -> None:
        if str(row["authority"]) != gateway_id or int(row["epoch"]) != epoch:
            raise RoomAuthorityConflict(
                "this gateway is not the room's current authority "
                f"(room: {row['authority']} epoch {row['epoch']})"
            )

    @staticmethod
    def _gateway_bytes_in(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COALESCE(SUM(log_bytes), 0) FROM rooms").fetchone()[0])

    def _check_space(self, conn: sqlite3.Connection, row: sqlite3.Row, ev: _Pending) -> None:
        """Refuse an event the room or the gateway has no space for (control kinds get the
        reserve); the gateway bound first purges the oldest disbanded rooms."""
        lim = self.limits
        extra_events = lim.reserve_events if ev.control else 0
        extra_bytes = lim.reserve_bytes if ev.control else 0
        if int(row["next_seq"]) - 1 >= lim.max_room_events + extra_events:
            raise RoomFull("this room reached its history limit; start a new room to go on")
        if int(row["log_bytes"]) + ev.size > lim.max_room_bytes + extra_bytes:
            raise RoomFull("this room reached its storage limit; start a new room to go on")
        ceiling = lim.max_gateway_bytes + extra_bytes
        if self._gateway_bytes_in(conn) + ev.size > ceiling:
            self._prune_in(conn, now=None, byte_target=max(0, ceiling - ev.size))
            if self._gateway_bytes_in(conn) + ev.size > ceiling:
                raise RoomFull("room storage on this host is full; disband an old room first")

    def _insert_in(
        self, conn: sqlite3.Connection, row: sqlite3.Row, ev: _Pending, epoch: int, now: float
    ) -> int:
        """Capacity check, insert at the room's next sequence number, advance the room."""
        self._check_space(conn, row, ev)
        seq = int(row["next_seq"])
        room_id = str(row["room_id"])
        conn.execute(
            f"INSERT INTO room_events ({_EVENT_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (room_id, seq, ev.event_id, ev.kind, ev.actor, epoch, ev.payload, now),
        )
        moved = conn.execute(
            "UPDATE rooms SET next_seq = ?, log_bytes = log_bytes + ?, updated_at = ? "
            "WHERE room_id = ? AND next_seq = ?",
            (seq + 1, ev.size, now, room_id, seq),
        ).rowcount
        if moved != 1:
            raise RoomCursorMoved("the room changed while the event was being written")
        return seq

    @staticmethod
    def _event_in(conn: sqlite3.Connection, room_id: str, event_id: str) -> sqlite3.Row | None:
        return conn.execute(
            f"SELECT {_EVENT_COLS} FROM room_events WHERE room_id = ? AND event_id = ?",
            (room_id, event_id),
        ).fetchone()

    @staticmethod
    def _same(row: sqlite3.Row, ev: _Pending, epoch: int) -> bool:
        return (row["kind"], row["actor"], int(row["epoch"]), row["payload"]) == (
            ev.kind,
            ev.actor,
            epoch,
            ev.payload,
        )

    # -- rooms -----------------------------------------------------------------------------------

    def create_room(
        self,
        room_id: str,
        name: str,
        members: Sequence[Mapping[str, Any]],
        *,
        authority: str | None = None,
    ) -> Room:
        """Create a room, or return the identical existing one (``idempotent``)."""
        rid = self._id(room_id, "room id")
        clean_name = self._name(name)
        _, members_json = self._members(members)
        owner = self._id(authority or self.gateway_id, "authority")
        now = self.now()

        def run(conn: sqlite3.Connection) -> Room:
            retired = conn.execute(
                "SELECT 1 FROM retired_rooms WHERE room_id = ?", (rid,)
            ).fetchone()
            if retired is not None:
                raise RoomConflict("this room id belonged to a disbanded room")
            found = conn.execute(_ROOM_SQL, (rid,)).fetchone()
            if found is not None:
                if found["disbanded_at"] is not None:
                    raise RoomConflict("this room id belonged to a disbanded room")
                if found["name"] != clean_name or found["members"] != members_json:
                    raise RoomConflict("a room with this id already exists with other settings")
                if found["authority"] != owner:
                    raise RoomConflict("a room with this id belongs to another gateway")
                return _room_of(found, idempotent=True)
            live = int(
                conn.execute("SELECT COUNT(*) FROM rooms WHERE disbanded_at IS NULL").fetchone()[0]
            )
            if live >= self.limits.max_active_rooms:
                raise RoomFull("this host has too many rooms; disband one and try again")
            conn.execute(
                "INSERT INTO rooms (room_id, name, members, authority, epoch, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                (rid, clean_name, members_json, owner, now, now),
            )
            return _room_of(conn.execute(_ROOM_SQL, (rid,)).fetchone())

        return self.write(run)

    def get_room(self, room_id: str, *, include_disbanded: bool = False) -> Room:
        rid = self._id(room_id, "room id")
        return self.read(
            lambda c: _room_of(self._row_in(c, rid, include_disbanded=include_disbanded))
        )

    def authority_claim(self, room_id: str, *, include_disbanded: bool = False) -> RoomEvent | None:
        """The ``authority.claimed`` event of the room's current epoch (None for epoch 1, which
        the creator holds without a claim)."""
        rid = self._id(room_id, "room id")

        def run(conn: sqlite3.Connection) -> RoomEvent | None:
            row = self._row_in(conn, rid, include_disbanded=include_disbanded)
            found = conn.execute(
                f"SELECT {_EVENT_COLS} FROM room_events WHERE room_id = ? AND kind = "
                "'authority.claimed' AND epoch = ? ORDER BY seq DESC LIMIT 1",
                (rid, int(row["epoch"])),
            ).fetchone()
            return _event_of(found) if found is not None else None

        return self.read(run)

    def list_rooms(
        self, *, limit: int = 100, offset: int = 0, include_disbanded: bool = False
    ) -> list[Room]:
        """One page, most recently changed first."""
        if isinstance(limit, bool) or not 1 <= limit <= self.limits.max_list:
            raise RoomError(f"limit must be between 1 and {self.limits.max_list}")
        if isinstance(offset, bool) or offset < 0:
            raise RoomError("offset must not be negative")
        where = "" if include_disbanded else "WHERE disbanded_at IS NULL "
        sql = (
            "SELECT room_id, name, members, authority, epoch, next_seq, log_bytes, revision, "
            f"created_at, updated_at, disbanded_at FROM rooms {where}"
            "ORDER BY updated_at DESC, room_id ASC LIMIT ? OFFSET ?"
        )
        return self.read(lambda c: [_room_of(r) for r in c.execute(sql, (limit, offset))])

    def rename_room(
        self, room_id: str, event_id: str, name: str, *, authority: str, epoch: int
    ) -> Room:
        """Rename a live room and log ``room.renamed`` in the same write."""
        rid = self._id(room_id, "room id")
        eid = self._id(event_id, "event id")
        clean = self._name(name)
        owner = self._id(authority, "authority")
        ep = self._epoch(epoch)
        actor = _compact({"id": "room-control", "kind": "system"})
        ev = _Pending(eid, "room.renamed", actor, _compact({"name": clean}))
        now = self.now()

        def run(conn: sqlite3.Connection) -> Room:
            row = self._row_in(conn, rid)
            existing = self._event_in(conn, rid, eid)
            if existing is not None:
                if not self._same(existing, ev, int(existing["epoch"])):
                    raise RoomEventConflict("this event id is already used for other content")
                return _room_of(row, idempotent=True)
            self.require_authority(row, owner, ep)
            self._insert_in(conn, row, ev, ep, now)
            conn.execute(
                "UPDATE rooms SET name = ?, revision = revision + 1, updated_at = ? "
                "WHERE room_id = ?",
                (clean, now, rid),
            )
            return _room_of(conn.execute(_ROOM_SQL, (rid,)).fetchone())

        return self.write(run)

    # -- events ----------------------------------------------------------------------------------

    def append_event(
        self,
        room_id: str,
        event_id: str,
        kind: str,
        actor: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        authority: str,
        epoch: int,
        expected_latest_seq: int | None = None,
    ) -> RoomEvent:
        """Append one event under ``authority``/``epoch`` (see the module notes)."""
        rid = self._id(room_id, "room id")
        eid = self._id(event_id, "event id")
        clean_kind = self._kind(kind)
        clean_actor, actor_json = self._actor(actor, clean_kind)
        owner = self._id(authority, "authority")
        ep = self._epoch(epoch)
        if clean_actor["kind"] == "gateway" and clean_actor["id"] != owner:
            raise RoomError("a gateway actor must be the room's authority")
        if expected_latest_seq is not None and (
            isinstance(expected_latest_seq, bool) or expected_latest_seq < 0
        ):
            raise RoomError("expected_latest_seq must not be negative")
        ev = _Pending(eid, clean_kind, actor_json, self._payload(payload))
        now = self.now()

        def run(conn: sqlite3.Connection) -> RoomEvent:
            existing = self._event_in(conn, rid, eid)
            if existing is not None:
                if not self._same(existing, ev, ep):
                    raise RoomEventConflict("this event id is already used for other content")
                return _event_of(existing, idempotent=True)
            row = self._row_in(conn, rid)
            self.require_authority(row, owner, ep)
            if expected_latest_seq is not None and int(row["next_seq"]) - 1 != expected_latest_seq:
                raise RoomCursorMoved("the room has newer events than the caller has seen")
            seq = self._insert_in(conn, row, ev, ep, now)
            found = conn.execute(
                f"SELECT {_EVENT_COLS} FROM room_events WHERE room_id = ? AND seq = ?", (rid, seq)
            ).fetchone()
            return _event_of(found)

        return self.write(run)

    def request_stop(
        self, room_id: str, cancel_id: str, *, authority: str, epoch: int
    ) -> RoomEvent:
        """Log ``room.stop_requested`` (idempotent per ``cancel_id``; draws on the reserve)."""
        cid = self._id(cancel_id, "cancel id")
        return self.append_event(
            room_id,
            _stop_event_id(cid),
            "room.stop_requested",
            {"kind": "gateway", "id": authority},
            {"cancel_id": cid},
            authority=authority,
            epoch=epoch,
        )

    def read_events(
        self,
        room_id: str,
        *,
        since_seq: int = 0,
        limit: int = 100,
        include_disbanded: bool = False,
    ) -> LogPage:
        """Events after ``since_seq``: at most ``limit`` (≤ 500) and ≤ 2 MiB serialized, whole
        events only."""
        rid = self._id(room_id, "room id")
        if isinstance(since_seq, bool) or since_seq < 0:
            raise RoomError("since_seq must not be negative")
        if isinstance(limit, bool) or not 1 <= limit <= self.limits.max_page_events:
            raise RoomError(f"limit must be between 1 and {self.limits.max_page_events}")

        def run(conn: sqlite3.Connection) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
            row = self._row_in(conn, rid, include_disbanded=include_disbanded)
            if since_seq > int(row["next_seq"]) - 1:
                raise RoomError("since_seq is ahead of the room's log")
            rows = conn.execute(
                f"SELECT {_EVENT_COLS} FROM room_events WHERE room_id = ? AND seq > ? "
                "ORDER BY seq LIMIT ?",
                (rid, since_seq, limit),
            ).fetchall()
            return row, rows

        room, rows = self.read(run)
        latest = int(room["next_seq"]) - 1
        events = [_event_of(r) for r in rows]

        def page(n: int) -> LogPage:
            cursor = events[n - 1].seq if n else since_seq
            return LogPage(events[:n], cursor, latest, str(room["authority"]), int(room["epoch"]))

        def size(p: LogPage) -> int:
            return _utf8(json.dumps(p.to_dict(), ensure_ascii=False, separators=(",", ":")))

        # Cheap first cut by the stored row sizes, then trim against the real serialized page.
        count, running = 0, 0
        for ev in events:
            running += _utf8(_compact(ev.to_dict()))
            if running > self.limits.max_page_bytes:
                break
            count += 1
        while count and size(page(count)) > self.limits.max_page_bytes:
            count -= 1
        if events and count == 0:
            raise RoomError("one event is larger than a log page")
        return page(count)

    # -- authority -------------------------------------------------------------------------------

    def claim_authority(
        self,
        room_id: str,
        *,
        expected: str,
        expected_epoch: int,
        new: str,
        event_id: str,
    ) -> Room:
        """Move the room to ``new`` at ``expected_epoch + 1`` behind a compare-and-set.

        This does not decide that taking over is safe; the caller (an operator's promotion)
        asserts the old authority can no longer write.
        """
        rid = self._id(room_id, "room id")
        old = self._id(expected, "expected authority")
        ep = self._epoch(expected_epoch, "expected epoch")
        owner = self._id(new, "new authority")
        eid = self._id(event_id, "event id")
        target = ep + 1
        actor = _compact({"id": "authority-control", "kind": "system"})
        body = {"previous": old, "authority": owner, "epoch": target}
        ev = _Pending(eid, "authority.claimed", actor, _compact(body))
        now = self.now()

        def run(conn: sqlite3.Connection) -> Room:
            row = self._row_in(conn, rid)
            existing = self._event_in(conn, rid, eid)
            if existing is not None:
                if not self._same(existing, ev, target):
                    raise RoomEventConflict("this event id is already used for other content")
                if str(row["authority"]) != owner or int(row["epoch"]) != target:
                    raise RoomAuthoritySuperseded(
                        "this claim succeeded once but a newer authority holds the room now"
                    )
                return _room_of(row, idempotent=True)
            self.require_authority(row, old, ep)
            self._insert_in(conn, row, ev, target, now)
            moved = conn.execute(
                "UPDATE rooms SET authority = ?, epoch = epoch + 1, revision = revision + 1, "
                "updated_at = ? WHERE room_id = ? AND authority = ? AND epoch = ? "
                "AND disbanded_at IS NULL",
                (owner, now, rid, old, ep),
            ).rowcount
            if moved != 1:
                raise RoomAuthorityConflict("the room's authority changed during the claim")
            return _room_of(conn.execute(_ROOM_SQL, (rid,)).fetchone())

        return self.write(run)

    def record_authority_lost(self, room_id: str, *, observed: str, observed_epoch: int) -> Room:
        """Adopt a newer lineage another host proved (demotion): log ``authority.lost`` and
        make ``observed`` at ``observed_epoch`` the room's authority. Only a strictly newer
        epoch is adopted; repeating the same lineage is idempotent."""
        rid = self._id(room_id, "room id")
        owner = self._id(observed, "observed authority")
        ep = self._epoch(observed_epoch, "observed epoch")
        eid = _lost_event_id(owner, ep)
        actor = _compact({"id": "authority-control", "kind": "system"})
        now = self.now()

        def run(conn: sqlite3.Connection) -> Room:
            row = self._row_in(conn, rid)
            existing = self._event_in(conn, rid, eid)
            if existing is not None:
                if str(row["authority"]) == owner and int(row["epoch"]) == ep:
                    return _room_of(row, idempotent=True)
                raise RoomAuthoritySuperseded("a newer authority holds the room now")
            if ep <= int(row["epoch"]):
                raise RoomAuthorityConflict(
                    "demotion needs proof of a newer authority: the observed epoch must be "
                    f"greater than {row['epoch']}"
                )
            body = {"previous": str(row["authority"]), "authority": owner, "epoch": ep}
            ev = _Pending(eid, "authority.lost", actor, _compact(body))
            self._insert_in(conn, row, ev, ep, now)
            conn.execute(
                "UPDATE rooms SET authority = ?, epoch = ?, revision = revision + 1, "
                "updated_at = ? WHERE room_id = ?",
                (owner, ep, now, rid),
            )
            return _room_of(conn.execute(_ROOM_SQL, (rid,)).fetchone())

        return self.write(run)

    # -- disband and retention -------------------------------------------------------------------

    def disband_room(self, room_id: str, *, authority: str, epoch: int) -> Disbanded:
        """Tombstone the room and retire its id for good (idempotent)."""
        rid = self._id(room_id, "room id")
        owner = self._id(authority, "authority")
        ep = self._epoch(epoch)
        actor = _compact({"id": "room-control", "kind": "system"})
        ev = _Pending("room-disbanded", "room.disbanded", actor, _compact({"room_id": rid}))
        now = self.now()

        def run(conn: sqlite3.Connection) -> Disbanded:
            row = conn.execute(_ROOM_SQL, (rid,)).fetchone()
            if row is None:
                retired = conn.execute(
                    "SELECT retired_at FROM retired_rooms WHERE room_id = ?", (rid,)
                ).fetchone()
                if retired is None:
                    raise RoomNotFound("no room has this id")
                return Disbanded(rid, float(retired["retired_at"]), True, purged=True)
            if row["disbanded_at"] is not None:
                return Disbanded(rid, float(row["disbanded_at"]), True)
            self.require_authority(row, owner, ep)
            self._insert_in(conn, row, ev, ep, now)
            conn.execute(
                "UPDATE rooms SET disbanded_at = ?, revision = revision + 1, updated_at = ? "
                "WHERE room_id = ?",
                (now, now, rid),
            )
            conn.execute(
                "INSERT OR IGNORE INTO retired_rooms (room_id, retired_at) VALUES (?, ?)",
                (rid, now),
            )
            self._prune_in(conn, now=now, byte_target=self.limits.max_gateway_bytes)
            return Disbanded(rid, now, False)

        return self.write(run)

    def prune_disbanded(self) -> int:
        """Purge disbanded rooms past 90 days or beyond the newest 512; ids stay retired."""
        now = self.now()
        return self.write(lambda c: self._prune_in(c, now=now, byte_target=None))

    def _prune_in(
        self, conn: sqlite3.Connection, *, now: float | None, byte_target: int | None
    ) -> int:
        lim = self.limits
        doomed: set[str] = set()
        if now is not None:
            cutoff = now - lim.tombstone_seconds
            doomed.update(
                str(r["room_id"])
                for r in conn.execute(
                    "SELECT room_id FROM rooms WHERE disbanded_at IS NOT NULL "
                    "AND disbanded_at <= ?",
                    (cutoff,),
                )
            )
        doomed.update(
            str(r["room_id"])
            for r in conn.execute(
                "SELECT room_id FROM rooms WHERE disbanded_at IS NOT NULL "
                "ORDER BY disbanded_at DESC, room_id ASC LIMIT -1 OFFSET ?",
                (lim.max_tombstones,),
            )
        )
        if byte_target is not None:
            total = self._gateway_bytes_in(conn)
            if total > byte_target:
                for r in conn.execute(
                    "SELECT room_id, log_bytes FROM rooms WHERE disbanded_at IS NOT NULL "
                    "ORDER BY disbanded_at ASC, room_id ASC"
                ).fetchall():
                    rid = str(r["room_id"])
                    if rid not in doomed:
                        doomed.add(rid)
                        total -= int(r["log_bytes"])
                    if total <= byte_target:
                        break
        if not doomed:
            return 0
        ids = sorted(doomed)
        marks = ",".join("?" for _ in ids)
        conn.execute(
            "INSERT OR IGNORE INTO retired_rooms (room_id, retired_at) "
            f"SELECT room_id, disbanded_at FROM rooms WHERE room_id IN ({marks})",
            ids,
        )
        for table in DEPENDENT_TABLES:
            if table_exists(conn, table):
                conn.execute(f"DELETE FROM {table} WHERE room_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM rooms WHERE room_id IN ({marks})", ids)
        return len(ids)

    # -- reads for tests and status ------------------------------------------------------------

    def gateway_bytes(self) -> int:
        return self.read(self._gateway_bytes_in)

    def is_retired(self, room_id: str) -> bool:
        rid = self._id(room_id, "room id")
        return self.read(
            lambda c: (
                c.execute("SELECT 1 FROM retired_rooms WHERE room_id = ?", (rid,)).fetchone()
                is not None
            )
        )
