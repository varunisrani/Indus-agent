"""The group-room driver: a lease plus a task state machine that schedules member turns (P24-06).

The driver never calls a model. It keeps two tables in the room store's file (``rooms.db``):

- ``room_leases``: who drives a room now. One row per room: the gateway id and the room epoch it
  holds the lease under, the driving process's generation (random per process start), a lease
  generation that grows on every take-over, and the expiry. A live lease of another process of the
  same authority refuses a second driver (``LeaseHeld``); an expired or released lease, or one
  under an older authority, is taken over with the next lease generation.
- ``room_tasks``: one row per member turn, identified by ``TaskKey`` (room, task, thread, turn;
  task id idempotent per payload, thread + turn unique). States::

      queued ──start──▶ running ──settle──▶ settled | failed
        │                 │  └─begin_stop─▶ stopping ──complete_stop──▶ cancelled
        └─cancel─▶ cancelled                    └──settle_stopping──▶ settled | failed
      running (under a lease that is no longer current) ──recover──▶ indeterminate
      indeterminate ──resolve / cancel / defer / requeue (operator)──▶ …
      deferred ──requeue_deferred (operator)──▶ queued

Every write is fenced. A running attempt (``TaskAttempt``) carries the task's execution
generation, its cancel generation and the run fence (gateway id, process generation, lease
generation); when any of them moved the update hits no row and ``StaleTask`` is raised. Recovery
never re-runs uncertain work (R03.21): a task found running under another fence becomes
``indeterminate`` and stays there until an operator resolves it (with a verified result, a
cancellation, a deferral, or an explicit requeue that accepts the risk of running twice).

Turn timing: a member that keeps reporting work (``heartbeat``) keeps its turn for up to
``TURN_LIMIT_S`` (3 h); one silent for ``SILENCE_LIMIT_S`` (3 min) loses it. ``expire_turns``
fails such tasks (reason ``turn_limit`` / ``member_silent``) so the room moves on.

Stop directives (``stop_directives``): ``stop``, ``halt`` or ``pause`` holds a member only next to
its @mention (``stop @bot``, ``@bot stop``, ``@bot please pause``); fenced code, inline code,
quoted text and blockquote lines never count; ``@all`` / ``@everyone`` beside a stop word holds
everyone.

Authority transfer is an operator recovery step, not an atomic hand-over: see ``rpc.py``
(``groups.promote`` / ``groups.demote``).

Bounds: prompt ≤ 128 KiB, result JSON ≤ 256 KiB, ids ≤ 128 characters; terminal tasks are kept 30
days and at most 2,048 per room, pruned at most 1,000 per call.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, cast

from indus.foundation.errors import IndusError
from indus.state.rooms import ID_RE, RoomStore

__all__ = [
    "MAX_PROMPT_BYTES",
    "MAX_RESULT_BYTES",
    "MAX_TERMINAL_TASKS",
    "PRUNE_BATCH",
    "SILENCE_LIMIT_S",
    "STATUSES",
    "TERMINAL",
    "TERMINAL_RETENTION_S",
    "TURN_LIMIT_S",
    "BadTransition",
    "DriverError",
    "DriverLease",
    "LeaseHeld",
    "RoomDriver",
    "StaleLease",
    "StaleTask",
    "Task",
    "TaskAttempt",
    "TaskConflict",
    "TaskKey",
    "stop_directives",
]

KIB: Final = 1024
MAX_ID_CHARS: Final = 128
MAX_PROMPT_BYTES: Final = 128 * KIB
MAX_RESULT_BYTES: Final = 256 * KIB
TERMINAL_RETENTION_S: Final = 30 * 86_400.0
MAX_TERMINAL_TASKS: Final = 2_048
PRUNE_BATCH: Final = 1_000
TURN_LIMIT_S: Final = 3 * 3_600.0
SILENCE_LIMIT_S: Final = 3 * 60.0

type Status = Literal[
    "queued", "running", "stopping", "settled", "failed", "cancelled", "indeterminate", "deferred"
]
type Terminal = Literal["settled", "failed"]
STATUSES: Final = frozenset(
    {"queued", "running", "stopping", "settled", "failed", "cancelled", "indeterminate", "deferred"}
)
TERMINAL: Final = frozenset({"settled", "failed", "cancelled"})
_BLOCKING: Final = ("running", "stopping", "indeterminate")
_ORDER: Final = "ORDER BY source_seq, created_at, task_id"

DRIVER_SCHEMA: Final = (
    """CREATE TABLE IF NOT EXISTS room_leases (
        room_id TEXT PRIMARY KEY,
        gateway_id TEXT NOT NULL,
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        process_generation TEXT NOT NULL,
        lease_generation INTEGER NOT NULL CHECK (lease_generation >= 1),
        expires_at REAL NOT NULL,
        acquired_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        released_at REAL
    )""",
    f"""CREATE TABLE IF NOT EXISTS room_tasks (
        room_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        source_seq INTEGER NOT NULL CHECK (source_seq >= 1),
        payload TEXT NOT NULL,
        digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ({", ".join(f"'{s}'" for s in sorted(STATUSES))})),
        exec_gen INTEGER NOT NULL DEFAULT 0,
        cancel_gen INTEGER NOT NULL DEFAULT 0,
        run_gateway TEXT,
        run_process TEXT,
        run_lease INTEGER,
        cancel_id TEXT,
        settlement_id TEXT,
        result TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        started_at REAL,
        active_at REAL,
        terminal_at REAL,
        indeterminate_at REAL,
        PRIMARY KEY (room_id, task_id),
        UNIQUE (room_id, thread_id, turn_id)
    )""",
    "CREATE INDEX IF NOT EXISTS room_tasks_by_status "
    "ON room_tasks (room_id, status, source_seq, created_at, task_id)",
)


# -- errors -----------------------------------------------------------------------------------


class DriverError(IndusError):
    """A refused driver operation (bad input, wrong state, conflicting ids)."""

    default_code = "rooms.driver"


class LeaseHeld(DriverError):
    """Another live process of the room's authority drives the room."""


class TaskConflict(DriverError):
    """The task id (or thread + turn) is bound to other content or another result."""


class BadTransition(DriverError):
    """The task is not in a state this operation moves from."""


class StaleLease(DriverError):
    """The lease expired, was released or taken over."""

    default_code = "rooms.stale"


class StaleTask(DriverError):
    """The attempt's generations or run fence moved on; nothing was written."""

    default_code = "rooms.stale"


# -- records ----------------------------------------------------------------------------------


def _ident(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DriverError(f"{label} must be text")
    text = value.strip()
    if not text or len(text) > MAX_ID_CHARS or not ID_RE.fullmatch(text):
        raise DriverError(f"{label} is not a valid id (1–{MAX_ID_CHARS} characters)")
    return text


@dataclass(frozen=True)
class TaskKey:
    """One admitted member turn: the room, the task id, the thread and the turn in it."""

    room_id: str
    task_id: str
    thread_id: str
    turn_id: str

    def __post_init__(self) -> None:
        for name in ("room_id", "task_id", "thread_id", "turn_id"):
            object.__setattr__(self, name, _ident(getattr(self, name), name))


@dataclass(frozen=True)
class DriverLease:
    room_id: str
    gateway_id: str
    epoch: int
    process_generation: str
    lease_generation: int
    expires_at: float
    reclaimed: bool = False

    @property
    def fence(self) -> tuple[str, str, int]:
        return self.gateway_id, self.process_generation, self.lease_generation


@dataclass(frozen=True)
class TaskAttempt:
    """The exact running generation allowed to settle one task."""

    key: TaskKey
    lease: DriverLease
    exec_gen: int
    cancel_gen: int


@dataclass(frozen=True)
class Task:
    key: TaskKey
    status: Status
    source_seq: int
    payload: dict[str, Any]
    exec_gen: int
    cancel_gen: int
    run_fence: tuple[str, str, int] | None
    cancel_id: str | None
    settlement_id: str | None
    result: dict[str, Any] | None
    created_at: float
    updated_at: float
    started_at: float | None
    active_at: float | None
    terminal_at: float | None
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "room_id": self.key.room_id,
            "task_id": self.key.task_id,
            "thread_id": self.key.thread_id,
            "turn_id": self.key.turn_id,
            "status": self.status,
            "source_seq": self.source_seq,
            "target": self.payload.get("target"),
            "exec_gen": self.exec_gen,
            "cancel_gen": self.cancel_gen,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "terminal_at": self.terminal_at,
        }


def _opt_float(value: object) -> float | None:
    return float(cast(float, value)) if value is not None else None


def _task_of(row: sqlite3.Row, *, idempotent: bool = False) -> Task:
    fence = (
        (str(row["run_gateway"]), str(row["run_process"]), int(row["run_lease"]))
        if row["run_gateway"] is not None
        else None
    )
    return Task(
        key=TaskKey(row["room_id"], row["task_id"], row["thread_id"], row["turn_id"]),
        status=cast(Status, row["status"]),
        source_seq=int(row["source_seq"]),
        payload=cast(dict[str, Any], json.loads(row["payload"])),
        exec_gen=int(row["exec_gen"]),
        cancel_gen=int(row["cancel_gen"]),
        run_fence=fence,
        cancel_id=row["cancel_id"],
        settlement_id=row["settlement_id"],
        result=cast(dict[str, Any], json.loads(row["result"])) if row["result"] else None,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        started_at=_opt_float(row["started_at"]),
        active_at=_opt_float(row["active_at"]),
        terminal_at=_opt_float(row["terminal_at"]),
        idempotent=idempotent,
    )


def _lease_of(row: sqlite3.Row, *, reclaimed: bool = False) -> DriverLease:
    return DriverLease(
        room_id=str(row["room_id"]),
        gateway_id=str(row["gateway_id"]),
        epoch=int(row["epoch"]),
        process_generation=str(row["process_generation"]),
        lease_generation=int(row["lease_generation"]),
        expires_at=float(row["expires_at"]),
        reclaimed=reclaimed,
    )


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _result_json(result: object) -> str:
    if not isinstance(result, Mapping):
        raise DriverError("a task result must be an object")
    try:
        text = _compact(result)
    except (TypeError, ValueError, RecursionError) as exc:
        raise DriverError("a task result must be plain JSON") from exc
    if len(text.encode()) > MAX_RESULT_BYTES:
        raise DriverError(f"a task result is larger than {MAX_RESULT_BYTES:,} bytes")
    return text


def _gen(value: object, label: str, *, low: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise DriverError(f"{label} must be a whole number of at least {low}")
    return value


def _terminal(status: object) -> Terminal:
    if status not in ("settled", "failed"):
        raise DriverError("a settlement status is 'settled' or 'failed'")
    return cast(Terminal, status)


# -- the driver -------------------------------------------------------------------------------


class RoomDriver:
    """Leases and member-turn tasks of the rooms in one ``RoomStore`` (see the module notes)."""

    def __init__(self, store: RoomStore) -> None:
        self.store = store
        store.ensure_schema(DRIVER_SCHEMA)

    # -- plumbing --------------------------------------------------------------------------------

    def _now(self) -> float:
        return self.store.now()

    @staticmethod
    def _load(conn: sqlite3.Connection, key: TaskKey) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM room_tasks WHERE room_id = ? AND task_id = ?",
            (key.room_id, key.task_id),
        ).fetchone()
        if row is None:
            raise DriverError("no task has this id in this room")
        if (row["thread_id"], row["turn_id"]) != (key.thread_id, key.turn_id):
            raise TaskConflict("the task id belongs to another thread or turn")
        if hashlib.sha256(str(row["payload"]).encode("utf-8")).hexdigest() != row["digest"]:
            raise DriverError("the stored task does not match its digest; it is not used")
        return row

    def _active_lease_in(
        self, conn: sqlite3.Connection, lease: DriverLease, now: float
    ) -> sqlite3.Row:
        room = self.store.live_row_in(conn, lease.room_id)
        self.store.require_authority(room, lease.gateway_id, lease.epoch)
        row = conn.execute(
            "SELECT * FROM room_leases WHERE room_id = ?", (lease.room_id,)
        ).fetchone()
        if (
            row is None
            or (
                row["gateway_id"],
                int(row["epoch"]),
                row["process_generation"],
                int(row["lease_generation"]),
            )
            != (lease.gateway_id, lease.epoch, lease.process_generation, lease.lease_generation)
            or row["released_at"] is not None
            or float(row["expires_at"]) <= now
        ):
            raise StaleLease("the room lease expired, was released or was taken over")
        return row

    @staticmethod
    def _fenced(conn: sqlite3.Connection, sql: str, params: Iterable[object], stale: str) -> None:
        if conn.execute(sql, tuple(params)).rowcount != 1:
            raise StaleTask(stale)

    def _reload(self, conn: sqlite3.Connection, key: TaskKey) -> Task:
        return _task_of(self._load(conn, key))

    @staticmethod
    def _ttl(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DriverError("the lease time must be a number of seconds")
        ttl = float(value)
        if not math.isfinite(ttl) or ttl <= 0:
            raise DriverError("the lease time must be a positive number of seconds")
        return ttl

    # -- leases ----------------------------------------------------------------------------------

    def acquire_lease(
        self, room_id: str, *, process_generation: str, ttl_seconds: float
    ) -> DriverLease:
        """Take an empty, expired, released or older-authority lease, or renew our own."""
        rid = _ident(room_id, "room id")
        proc = _ident(process_generation, "process generation")
        ttl = self._ttl(ttl_seconds)
        gateway = self.store.gateway_id

        def run(conn: sqlite3.Connection) -> DriverLease:
            now = self._now()
            room = self.store.live_row_in(conn, rid)
            epoch = int(room["epoch"])
            self.store.require_authority(room, gateway, epoch)
            expires = now + ttl
            row = conn.execute("SELECT * FROM room_leases WHERE room_id = ?", (rid,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO room_leases (room_id, gateway_id, epoch, process_generation, "
                    "lease_generation, expires_at, acquired_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                    (rid, gateway, epoch, proc, expires, now, now),
                )
                return _lease_of(
                    conn.execute("SELECT * FROM room_leases WHERE room_id = ?", (rid,)).fetchone()
                )
            same_authority = row["gateway_id"] == gateway and int(row["epoch"]) == epoch
            live = row["released_at"] is None and float(row["expires_at"]) > now
            if same_authority and live and row["process_generation"] == proc:
                kept = max(float(row["expires_at"]), expires)
                conn.execute(
                    "UPDATE room_leases SET expires_at = ?, updated_at = ? WHERE room_id = ?",
                    (kept, now, rid),
                )
                return replace(_lease_of(row), expires_at=kept)
            if same_authority and live:
                raise LeaseHeld("another process drives this room; its lease is still live")
            moved = conn.execute(
                "UPDATE room_leases SET gateway_id = ?, epoch = ?, process_generation = ?, "
                "lease_generation = lease_generation + 1, expires_at = ?, acquired_at = ?, "
                "updated_at = ?, released_at = NULL WHERE room_id = ? AND lease_generation = ?",
                (gateway, epoch, proc, expires, now, now, rid, int(row["lease_generation"])),
            ).rowcount
            if moved != 1:
                raise LeaseHeld("the room lease changed while it was being taken")
            fresh = conn.execute("SELECT * FROM room_leases WHERE room_id = ?", (rid,)).fetchone()
            return _lease_of(fresh, reclaimed=True)

        return self.store.write(run)

    def renew_lease(self, lease: DriverLease, *, ttl_seconds: float) -> DriverLease:
        ttl = self._ttl(ttl_seconds)

        def run(conn: sqlite3.Connection) -> DriverLease:
            now = self._now()
            row = self._active_lease_in(conn, lease, now)
            kept = max(float(row["expires_at"]), now + ttl)
            conn.execute(
                "UPDATE room_leases SET expires_at = ?, updated_at = ? WHERE room_id = ?",
                (kept, now, lease.room_id),
            )
            return replace(lease, expires_at=kept, reclaimed=False)

        return self.store.write(run)

    def release_lease(self, lease: DriverLease) -> bool:
        """Release exactly this lease; False when it was released already. Refused while a task
        of the room is running."""

        def run(conn: sqlite3.Connection) -> bool:
            now = self._now()
            row = conn.execute(
                "SELECT * FROM room_leases WHERE room_id = ?", (lease.room_id,)
            ).fetchone()
            if row is None or (
                row["gateway_id"],
                int(row["epoch"]),
                row["process_generation"],
                int(row["lease_generation"]),
            ) != (lease.gateway_id, lease.epoch, lease.process_generation, lease.lease_generation):
                raise StaleLease("this lease is not the room's current lease")
            if row["released_at"] is not None:
                return False
            if float(row["expires_at"]) <= now:
                raise StaleLease("the lease expired before it was released")
            busy = conn.execute(
                "SELECT 1 FROM room_tasks WHERE room_id = ? AND status = 'running' LIMIT 1",
                (lease.room_id,),
            ).fetchone()
            if busy is not None:
                raise BadTransition("a task of this room is still running")
            conn.execute(
                "UPDATE room_leases SET expires_at = ?, released_at = ?, updated_at = ? "
                "WHERE room_id = ?",
                (now, now, now, lease.room_id),
            )
            return True

        return self.store.write(run)

    # -- admission and start -------------------------------------------------------------------

    @staticmethod
    def _payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
        allowed = {"target", "prompt", "source_seq", "member_id"}
        unknown = set(payload) - allowed
        if unknown:
            raise DriverError(f"unknown task fields: {', '.join(sorted(unknown))}")
        missing = {"target", "prompt", "source_seq"} - set(payload)
        if missing:
            raise DriverError(f"missing task fields: {', '.join(sorted(missing))}")
        prompt = payload["prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise DriverError("the prompt must be non-empty text")
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise DriverError(f"the prompt is larger than {MAX_PROMPT_BYTES:,} bytes")
        clean: dict[str, Any] = {
            "target": _ident(payload["target"], "target"),
            "prompt": prompt,
            "source_seq": _gen(payload["source_seq"], "source_seq", low=1),
        }
        if "member_id" in payload:
            clean["member_id"] = _ident(payload["member_id"], "member id")
        text = _compact(clean)
        return clean, text, hashlib.sha256(text.encode("utf-8")).hexdigest()

    def admit(self, key: TaskKey, payload: Mapping[str, Any]) -> Task:
        """Queue a member turn, or return the identical admission."""
        clean, text, digest = self._payload(payload)

        def run(conn: sqlite3.Connection) -> Task:
            now = self._now()
            self.store.live_row_in(conn, key.room_id)
            found = conn.execute(
                "SELECT * FROM room_tasks WHERE room_id = ? AND task_id = ?",
                (key.room_id, key.task_id),
            ).fetchone()
            if found is not None:
                if found["digest"] != digest or found["payload"] != text:
                    raise TaskConflict("this task id is already bound to another turn")
                return _task_of(found, idempotent=True)
            clash = conn.execute(
                "SELECT 1 FROM room_tasks WHERE room_id = ? AND thread_id = ? AND turn_id = ?",
                (key.room_id, key.thread_id, key.turn_id),
            ).fetchone()
            if clash is not None:
                raise TaskConflict("this thread and turn already have a task")
            conn.execute(
                "INSERT INTO room_tasks (room_id, task_id, thread_id, turn_id, source_seq, "
                "payload, digest, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                (
                    key.room_id,
                    key.task_id,
                    key.thread_id,
                    key.turn_id,
                    clean["source_seq"],
                    text,
                    digest,
                    now,
                    now,
                ),
            )
            return self._reload(conn, key)

        return self.store.write(run)

    def start(self, key: TaskKey, lease: DriverLease, *, expected_cancel_gen: int) -> TaskAttempt:
        """Run the head of the room's queue under ``lease``; nothing else may be in flight."""
        if lease.room_id != key.room_id:
            raise DriverError("the lease and the task belong to different rooms")
        cancel_gen = _gen(expected_cancel_gen, "expected cancel generation")

        def run(conn: sqlite3.Connection) -> TaskAttempt:
            now = self._now()
            self._active_lease_in(conn, lease, now)
            row = self._load(conn, key)
            if int(row["cancel_gen"]) != cancel_gen:
                raise StaleTask("the task was cancelled or stopped meanwhile")
            if row["status"] != "queued":
                raise BadTransition(f"a {row['status']} task cannot start")
            busy = conn.execute(
                f"SELECT 1 FROM room_tasks WHERE room_id = ? AND status IN {_BLOCKING} LIMIT 1",
                (key.room_id,),
            ).fetchone()
            if busy is not None:
                raise BadTransition("the room has a task in flight or awaiting recovery")
            head = conn.execute(
                f"SELECT task_id FROM room_tasks WHERE room_id = ? AND status = 'queued' {_ORDER} "
                "LIMIT 1",
                (key.room_id,),
            ).fetchone()
            if head is None or head["task_id"] != key.task_id:
                raise BadTransition("this task is not next in the room's order")
            exec_gen = int(row["exec_gen"]) + 1
            self._fenced(
                conn,
                "UPDATE room_tasks SET status = 'running', exec_gen = ?, run_gateway = ?, "
                "run_process = ?, run_lease = ?, started_at = ?, active_at = ?, updated_at = ? "
                "WHERE room_id = ? AND task_id = ? AND status = 'queued' AND cancel_gen = ?",
                (exec_gen, *lease.fence, now, now, now, key.room_id, key.task_id, cancel_gen),
                "the task changed while it was being started",
            )
            return TaskAttempt(key, lease, exec_gen, cancel_gen)

        return self.store.write(run)

    # -- running attempts ----------------------------------------------------------------------

    _RUNNING_FENCE: Final = (
        "status = 'running' AND exec_gen = ? AND cancel_gen = ? AND run_gateway = ? "
        "AND run_process = ? AND run_lease = ?"
    )

    @staticmethod
    def _attempt_params(attempt: TaskAttempt) -> tuple[object, ...]:
        return (attempt.exec_gen, attempt.cancel_gen, *attempt.lease.fence)

    def _check_attempt(self, row: sqlite3.Row, attempt: TaskAttempt) -> None:
        fence = (row["run_gateway"], row["run_process"], row["run_lease"])
        if (
            row["status"] != "running"
            or int(row["exec_gen"]) != attempt.exec_gen
            or int(row["cancel_gen"]) != attempt.cancel_gen
            or fence != attempt.lease.fence
        ):
            raise StaleTask("this attempt is no longer the task's running attempt")

    def requeue_not_admitted(self, attempt: TaskAttempt) -> Task:
        """The runner proved the member never took the turn (refused before it started): put
        the task back in the queue under its exact fence. Idempotent."""
        key = attempt.key

        def run(conn: sqlite3.Connection) -> Task:
            now = self._now()
            row = self._load(conn, key)
            if (
                row["status"] == "queued"
                and int(row["exec_gen"]) == attempt.exec_gen
                and int(row["cancel_gen"]) == attempt.cancel_gen
                and row["run_gateway"] is None
            ):
                return _task_of(row, idempotent=True)
            self._active_lease_in(conn, attempt.lease, now)
            self._check_attempt(row, attempt)
            self._fenced(
                conn,
                "UPDATE room_tasks SET status = 'queued', run_gateway = NULL, run_process = NULL, "
                "run_lease = NULL, started_at = NULL, active_at = NULL, updated_at = ? "
                f"WHERE room_id = ? AND task_id = ? AND {self._RUNNING_FENCE}",
                (now, key.room_id, key.task_id, *self._attempt_params(attempt)),
                "the task changed while it was being put back",
            )
            return self._reload(conn, key)

        return self.store.write(run)

    def heartbeat(self, attempt: TaskAttempt) -> None:
        """The member reported work: its turn stays alive (up to ``TURN_LIMIT_S``)."""

        def run(conn: sqlite3.Connection) -> None:
            now = self._now()
            self._active_lease_in(conn, attempt.lease, now)
            self._fenced(
                conn,
                "UPDATE room_tasks SET active_at = ?, updated_at = ? "
                f"WHERE room_id = ? AND task_id = ? AND {self._RUNNING_FENCE}",
                (
                    now,
                    now,
                    attempt.key.room_id,
                    attempt.key.task_id,
                    *self._attempt_params(attempt),
                ),
                "this attempt is no longer the task's running attempt",
            )

        self.store.write(run)

    @staticmethod
    def _replayed(row: sqlite3.Row, settlement_id: str, status: str, result: str) -> bool:
        """An identical committed settlement is a replay; a different one a conflict."""
        if row["settlement_id"] is None:
            return False
        if (row["settlement_id"], row["status"], row["result"]) == (settlement_id, status, result):
            return True
        raise TaskConflict("the task already has another settlement")

    def settle(
        self,
        attempt: TaskAttempt,
        *,
        settlement_id: str,
        status: Terminal,
        result: Mapping[str, Any],
    ) -> Task:
        """Commit the attempt's result if every fence still matches (identical replays pass)."""
        sid = _ident(settlement_id, "settlement id")
        final = _terminal(status)
        body = _result_json(result)
        key = attempt.key

        def run(conn: sqlite3.Connection) -> Task:
            now = self._now()
            row = self._load(conn, key)
            if self._replayed(row, sid, final, body):
                return _task_of(row, idempotent=True)
            self._active_lease_in(conn, attempt.lease, now)
            self._check_attempt(row, attempt)
            self._fenced(
                conn,
                "UPDATE room_tasks SET status = ?, settlement_id = ?, result = ?, terminal_at = ?, "
                f"updated_at = ? WHERE room_id = ? AND task_id = ? AND {self._RUNNING_FENCE}",
                (
                    final,
                    sid,
                    body,
                    now,
                    now,
                    key.room_id,
                    key.task_id,
                    *self._attempt_params(attempt),
                ),
                "the task changed while it was being settled",
            )
            return self._reload(conn, key)

        return self.store.write(run)

    # -- cancellation and stop -----------------------------------------------------------------

    def cancel_queued(self, key: TaskKey, *, cancel_id: str, expected_cancel_gen: int) -> Task:
        """Cancel a task no work was started for (queued or deferred)."""
        cid = _ident(cancel_id, "cancel id")
        gen = _gen(expected_cancel_gen, "expected cancel generation")

        def run(conn: sqlite3.Connection) -> Task:
            now = self._now()
            row = self._load(conn, key)
            if row["status"] == "cancelled" and row["cancel_id"] == cid:
                return _task_of(row, idempotent=True)
            if row["status"] in TERMINAL:
                raise BadTransition(f"a {row['status']} task cannot be cancelled")
            if row["status"] not in ("queued", "deferred"):
                raise BadTransition("running work needs a two-phase stop")
            if int(row["cancel_gen"]) != gen:
                raise StaleTask("the task's cancel generation moved")
            self._fenced(
                conn,
                "UPDATE room_tasks SET status = 'cancelled', cancel_gen = ?, cancel_id = ?, "
                "terminal_at = ?, updated_at = ? WHERE room_id = ? AND task_id = ? "
                "AND status IN ('queued', 'deferred') AND cancel_gen = ?",
                (gen + 1, cid, now, now, key.room_id, key.task_id, gen),
                "the task changed while it was being cancelled",
            )
            return self._reload(conn, key)

        return self.store.write(run)

    def begin_stop(self, key: TaskKey, *, cancel_id: str, expected_cancel_gen: int) -> Task:
        """Record the intent to stop running (or uncertain) work; it is not stopped yet."""
        cid = _ident(cancel_id, "cancel id")
        gen = _gen(expected_cancel_gen, "expected cancel generation")

        def run(conn: sqlite3.Connection) -> Task:
            now = self._now()
            row = self._load(conn, key)
            if row["status"] == "stopping" and row["cancel_id"] == cid:
                return _task_of(row, idempotent=True)
            if row["status"] not in ("running", "indeterminate"):
                raise BadTransition(f"a {row['status']} task cannot be stopped")
            if int(row["cancel_gen"]) != gen:
                raise StaleTask("the task's cancel generation moved")
            self._fenced(
                conn,
                "UPDATE room_tasks SET status = 'stopping', cancel_gen = ?, cancel_id = ?, "
                "updated_at = ? WHERE room_id = ? AND task_id = ? "
                "AND status IN ('running', 'indeterminate') AND cancel_gen = ?",
                (gen + 1, cid, now, key.room_id, key.task_id, gen),
                "the task changed while its stop was recorded",
            )
            return self._reload(conn, key)

        return self.store.write(run)

    def complete_stop(self, key: TaskKey, *, cancel_id: str, expected_cancel_gen: int) -> Task:
        """The runner acknowledged this exact stop: the task is cancelled."""
        cid = _ident(cancel_id, "cancel id")
        gen = _gen(expected_cancel_gen, "expected cancel generation")

        def run(conn: sqlite3.Connection) -> Task:
            now = self._now()
            row = self._load(conn, key)
            if row["status"] == "cancelled" and row["cancel_id"] == cid:
                return _task_of(row, idempotent=True)
            if (row["status"], row["cancel_id"], int(row["cancel_gen"])) != ("stopping", cid, gen):
                raise StaleTask("this stop acknowledgement does not match the task's stop")
            self._fenced(
                conn,
                "UPDATE room_tasks SET status = 'cancelled', terminal_at = ?, updated_at = ? "
                "WHERE room_id = ? AND task_id = ? AND status = 'stopping' AND cancel_id = ? "
                "AND cancel_gen = ?",
                (now, now, key.room_id, key.task_id, cid, gen),
                "the task changed while its stop was acknowledged",
            )
            return self._reload(conn, key)

        return self.store.write(run)

    def settle_stopping(
        self,
        key: TaskKey,
        lease: DriverLease,
        *,
        expected_exec_gen: int,
        expected_cancel_gen: int,
        settlement_id: str,
        status: Terminal,
        result: Mapping[str, Any],
    ) -> Task:
        """The result won the race with an unacknowledged stop: commit it."""
        return self._operator_settle(
            key,
            lease,
            "stopping",
            expected_exec_gen,
            expected_cancel_gen,
            settlement_id,
            status,
            result,
            lease_first=False,
        )

    # -- recovery (operator paths) -------------------------------------------------------------

    def recover(self, lease: DriverLease) -> dict[str, list[TaskKey]]:
        """Fence running attempts of other leases as ``indeterminate`` (never requeued) and
        answer the room's queued and indeterminate tasks in order."""

        def run(conn: sqlite3.Connection) -> dict[str, list[TaskKey]]:
            now = self._now()
            self._active_lease_in(conn, lease, now)
            conn.execute(
                "UPDATE room_tasks SET status = 'indeterminate', indeterminate_at = ?, "
                "updated_at = ? WHERE room_id = ? AND status = 'running' AND NOT "
                "(run_gateway = ? AND run_process = ? AND run_lease = ?)",
                (now, now, lease.room_id, *lease.fence),
            )
            out: dict[str, list[TaskKey]] = {}
            for status in ("queued", "indeterminate"):
                rows = conn.execute(
                    f"SELECT * FROM room_tasks WHERE room_id = ? AND status = ? {_ORDER}",
                    (lease.room_id, status),
                ).fetchall()
                out[status] = [_task_of(r).key for r in rows]
            return out

        return self.store.write(run)

    def _generation_move(
        self,
        key: TaskKey,
        lease: DriverLease,
        from_status: str,
        exec_gen: int,
        cancel_gen: int,
        set_sql: str,
        set_params: tuple[object, ...],
        replay: Callable[[sqlite3.Row], bool] | None = None,
        *,
        lease_first: bool = True,
    ) -> Task:
        if lease.room_id != key.room_id:
            raise DriverError("the lease and the task belong to different rooms")
        ex = _gen(exec_gen, "expected execution generation", low=1)
        cg = _gen(cancel_gen, "expected cancel generation")

        def run(conn: sqlite3.Connection) -> Task:
            now = self._now()
            if lease_first:
                self._active_lease_in(conn, lease, now)
            row = self._load(conn, key)
            if replay is not None and replay(row):
                return _task_of(row, idempotent=True)
            if not lease_first:
                self._active_lease_in(conn, lease, now)
            if (row["status"], int(row["exec_gen"]), int(row["cancel_gen"])) != (
                from_status,
                ex,
                cg,
            ):
                raise StaleTask(f"the {from_status} task's generations moved")
            self._fenced(
                conn,
                f"UPDATE room_tasks SET {set_sql}, updated_at = ? WHERE room_id = ? AND "
                "task_id = ? AND status = ? AND exec_gen = ? AND cancel_gen = ?",
                (*set_params, now, key.room_id, key.task_id, from_status, ex, cg),
                f"the {from_status} task changed meanwhile",
            )
            return self._reload(conn, key)

        return self.store.write(run)

    def _operator_settle(
        self,
        key: TaskKey,
        lease: DriverLease,
        from_status: str,
        exec_gen: int,
        cancel_gen: int,
        settlement_id: str,
        status: Terminal,
        result: Mapping[str, Any],
        *,
        lease_first: bool = True,
    ) -> Task:
        sid = _ident(settlement_id, "settlement id")
        final = _terminal(status)
        body = _result_json(result)
        now = self._now()
        return self._generation_move(
            key,
            lease,
            from_status,
            exec_gen,
            cancel_gen,
            "status = ?, settlement_id = ?, result = ?, terminal_at = ?",
            (final, sid, body, now),
            lambda row: self._replayed(row, sid, final, body),
            lease_first=lease_first,
        )

    def resolve_indeterminate(
        self,
        key: TaskKey,
        lease: DriverLease,
        *,
        expected_exec_gen: int,
        expected_cancel_gen: int,
        settlement_id: str,
        status: Terminal,
        result: Mapping[str, Any],
    ) -> Task:
        """Commit a verified result for uncertain work."""
        return self._operator_settle(
            key,
            lease,
            "indeterminate",
            expected_exec_gen,
            expected_cancel_gen,
            settlement_id,
            status,
            result,
        )

    def cancel_indeterminate(
        self,
        key: TaskKey,
        lease: DriverLease,
        *,
        expected_exec_gen: int,
        expected_cancel_gen: int,
        cancel_id: str,
    ) -> Task:
        """Commit a verified cancellation of uncertain work."""
        cid = _ident(cancel_id, "cancel id")
        now = self._now()
        return self._generation_move(
            key,
            lease,
            "indeterminate",
            expected_exec_gen,
            expected_cancel_gen,
            "status = 'cancelled', cancel_gen = cancel_gen + 1, cancel_id = ?, terminal_at = ?",
            (cid, now),
            lambda row: row["status"] == "cancelled" and row["cancel_id"] == cid,
        )

    def requeue_indeterminate(
        self, key: TaskKey, lease: DriverLease, *, expected_exec_gen: int, expected_cancel_gen: int
    ) -> Task:
        """Run uncertain work again: only on an operator's explicit request (it may run twice)."""
        return self._generation_move(
            key,
            lease,
            "indeterminate",
            expected_exec_gen,
            expected_cancel_gen,
            "status = 'queued', run_gateway = NULL, run_process = NULL, run_lease = NULL, "
            "started_at = NULL, active_at = NULL, indeterminate_at = NULL",
            (),
        )

    def defer_indeterminate(
        self,
        key: TaskKey,
        lease: DriverLease,
        *,
        expected_exec_gen: int,
        expected_cancel_gen: int,
        reason: str,
    ) -> Task:
        """Fence uncertain work aside so later room work can go on."""
        why = _ident(reason, "reason")
        body = _compact({"reason": why, "retryable": True})
        now = self._now()
        return self._generation_move(
            key,
            lease,
            "indeterminate",
            expected_exec_gen,
            expected_cancel_gen,
            "status = 'deferred', result = ?, terminal_at = ?",
            (body, now),
            lambda row: row["status"] == "deferred" and row["result"] == body,
        )

    def requeue_deferred(
        self, key: TaskKey, lease: DriverLease, *, expected_exec_gen: int, expected_cancel_gen: int
    ) -> Task:
        """An operator retries a deferred turn."""
        return self._generation_move(
            key,
            lease,
            "deferred",
            expected_exec_gen,
            expected_cancel_gen,
            "status = 'queued', result = NULL, run_gateway = NULL, run_process = NULL, "
            "run_lease = NULL, started_at = NULL, active_at = NULL, terminal_at = NULL, "
            "indeterminate_at = NULL",
            (),
        )

    # -- turn timing and retention -------------------------------------------------------------

    def expire_turns(self, lease: DriverLease) -> list[tuple[TaskKey, str]]:
        """Fail this lease's running turns that went silent (3 min) or ran too long (3 h)."""

        def run(conn: sqlite3.Connection) -> list[tuple[TaskKey, str]]:
            now = self._now()
            self._active_lease_in(conn, lease, now)
            rows = conn.execute(
                "SELECT * FROM room_tasks WHERE room_id = ? AND status = 'running' AND "
                f"run_gateway = ? AND run_process = ? AND run_lease = ? {_ORDER}",
                (lease.room_id, *lease.fence),
            ).fetchall()
            expired: list[tuple[TaskKey, str]] = []
            for row in rows:
                started = float(row["started_at"])
                active = float(row["active_at"] if row["active_at"] is not None else started)
                if now - started >= TURN_LIMIT_S:
                    why = "turn_limit"
                elif now - active >= SILENCE_LIMIT_S:
                    why = "member_silent"
                else:
                    continue
                key = _task_of(row).key
                self._fenced(
                    conn,
                    "UPDATE room_tasks SET status = 'failed', settlement_id = ?, result = ?, "
                    "terminal_at = ?, updated_at = ? WHERE room_id = ? AND task_id = ? "
                    "AND status = 'running' AND exec_gen = ?",
                    (
                        f"expired-{int(row['exec_gen'])}",
                        _compact({"reason": why}),
                        now,
                        now,
                        key.room_id,
                        key.task_id,
                        int(row["exec_gen"]),
                    ),
                    "the task changed while it was being expired",
                )
                expired.append((key, why))
            return expired

        return self.store.write(run)

    def prune_terminal(
        self,
        room_id: str,
        *,
        retention_s: float = TERMINAL_RETENTION_S,
        keep: int = MAX_TERMINAL_TASKS,
    ) -> int:
        """Drop terminal tasks older than ``retention_s`` or beyond the newest ``keep``, at most
        ``PRUNE_BATCH`` per call."""
        rid = _ident(room_id, "room id")
        if not math.isfinite(retention_s) or retention_s <= 0:
            raise DriverError("retention must be a positive number of seconds")
        _gen(keep, "keep")

        def run(conn: sqlite3.Connection) -> int:
            cutoff = self._now() - retention_s
            rows = conn.execute(
                "SELECT task_id, terminal_at FROM room_tasks WHERE room_id = ? AND status IN "
                "('settled', 'failed', 'cancelled') ORDER BY terminal_at DESC, task_id ASC",
                (rid,),
            ).fetchall()
            doomed = [
                str(r["task_id"])
                for index, r in enumerate(rows)
                if index >= keep
                or (r["terminal_at"] is not None and float(r["terminal_at"]) <= cutoff)
            ][:PRUNE_BATCH]
            if not doomed:
                return 0
            marks = ",".join("?" for _ in doomed)
            conn.execute(
                f"DELETE FROM room_tasks WHERE room_id = ? AND task_id IN ({marks})", (rid, *doomed)
            )
            return len(doomed)

        return self.store.write(run)

    # -- reads -----------------------------------------------------------------------------------

    def get_task(self, key: TaskKey) -> Task:
        return self.store.read(lambda c: self._reload(c, key))

    def list_tasks(self, room_id: str, *, status: str | None = None) -> list[Task]:
        rid = _ident(room_id, "room id")
        if status is not None and status not in STATUSES:
            raise DriverError("unknown task status")

        def run(conn: sqlite3.Connection) -> list[Task]:
            if status is None:
                rows = conn.execute(
                    f"SELECT * FROM room_tasks WHERE room_id = ? {_ORDER}", (rid,)
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM room_tasks WHERE room_id = ? AND status = ? {_ORDER}",
                    (rid, status),
                ).fetchall()
            return [_task_of(r) for r in rows]

        return self.store.read(run)

    def lease_of(self, room_id: str) -> DriverLease | None:
        rid = _ident(room_id, "room id")
        row = self.store.read(
            lambda c: c.execute("SELECT * FROM room_leases WHERE room_id = ?", (rid,)).fetchone()
        )
        return _lease_of(row) if row is not None else None


# -- stop directives --------------------------------------------------------------------------

STOP_WORDS: Final = frozenset({"stop", "halt", "pause"})
FILLER_WORDS: Final = frozenset({"please", "pls", "plz", "kindly"})
BROADCAST: Final = frozenset({"all", "everyone"})
_GLUE: Final = frozenset({",", ":", ";", "-", "–", "—"})
_FENCE_MARKS: Final = ("```", "~~~")
_INLINE_CODE: Final = re.compile(r"`+[^`]*`+")
_QUOTED: Final = re.compile(r"\"[^\"]*\"|“[^”]*”|«[^»]*»")
_TOKEN: Final = re.compile(r"@[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?|[A-Za-z0-9_']+|\S")
_BREAK: Final = " \u00a7 "  # stands in for removed content; a token that is never glue


def _prose_lines(text: str) -> list[str]:
    """The lines of ``text`` that are prose: fenced code blocks and blockquote lines dropped,
    inline code and quoted spans replaced by a break, so words on either side never count as
    next to each other."""
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        mark = next((m for m in _FENCE_MARKS if stripped.startswith(m)), None)
        if mark is not None:
            fence = mark
            continue
        if stripped.startswith(">"):
            continue
        lines.append(_QUOTED.sub(_BREAK, _INLINE_CODE.sub(_BREAK, line)))
    return lines


def _adjacent_stop(tokens: list[str], start: int, step: int) -> bool:
    index = start + step
    while 0 <= index < len(tokens):
        word = tokens[index].lower()
        if word in STOP_WORDS:
            return True
        if word in FILLER_WORDS or word in _GLUE:
            index += step
            continue
        return False
    return False


def stop_directives(text: str, handles: Iterable[str], *, enabled: bool = True) -> frozenset[str]:
    """The member handles ``text`` holds (see the module notes); empty when ``enabled`` is off."""
    known = {h.lower().lstrip("@"): h for h in handles}
    if not enabled or not text:
        return frozenset()
    held: set[str] = set()
    for line in _prose_lines(text):
        tokens = _TOKEN.findall(line)
        for index, token in enumerate(tokens):
            if not token.startswith("@"):
                continue
            name = token[1:].lower()
            if name not in known and name not in BROADCAST:
                continue
            if not (_adjacent_stop(tokens, index, 1) or _adjacent_stop(tokens, index, -1)):
                continue
            if name in BROADCAST:
                held.update(known.values())
            else:
                held.add(known[name])
    return frozenset(held)
