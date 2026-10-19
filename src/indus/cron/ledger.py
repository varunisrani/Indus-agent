"""The execution ledger: one durable row per run attempt in ``<home>/cron/executions.db`` (P14-04).

An attempt is written **before** its run is dispatched (``claim``). A scheduled slot — the exact
(job, instant) pair — can be claimed once; a stale ``jobs.json`` pointing at a slot that already
has an attempt is caught by ``slot_attempted`` and never replayed (R07.13).

States: ``claimed → running → completed | failed | unknown``; finished rows never change
(``AttemptFinal``), except that an unset ``delivery_status`` may be filled in once.

Reaping (every tick, and before a manual run) turns a non-terminal attempt ``unknown`` when its
owner is proved dead — the PID is gone, or now belongs to a process with another start
fingerprint — or when a live owner has sent no heartbeat for max(3 × inactivity timeout, script
timeout, 2 h). The calling process's own attempts are left alone. ``unknown`` attempts are never
re-run: their slot stays taken.

History is bounded to the newest 500 finished attempts per job; active ones are never pruned.
The database is opened with the P02 plumbing (isolation guard, owner-only file, journal policy,
one ``Writer``) and also holds the per-job notepad table (P14-17).
"""

from __future__ import annotations

import errno
import os
import sqlite3
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from indus.cron.errors import AttemptFinal, CronError, SlotTaken
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.state import journal
from indus.state.connection import open_store
from indus.state.writer import Writer

__all__ = [
    "ACTIVE_STATES",
    "KEEP_PER_JOB",
    "LIVE_SILENCE_FLOOR_S",
    "TERMINAL_STATES",
    "Attempt",
    "AttemptKind",
    "AttemptState",
    "ExecutionLedger",
    "ExecutionsDB",
    "ProcessProbe",
    "SystemProcesses",
    "iso",
    "silence_bound",
]

AttemptKind = Literal["scheduled", "manual", "retry"]
AttemptState = Literal["claimed", "running", "completed", "failed", "unknown"]

ACTIVE_STATES: tuple[AttemptState, ...] = ("claimed", "running")
TERMINAL_STATES: tuple[AttemptState, ...] = ("completed", "failed", "unknown")
KEEP_PER_JOB = 500
LIVE_SILENCE_FLOOR_S = 7200.0
SCHEMA_VERSION = 1

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS attempts (
        attempt_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        scheduled_for TEXT,
        kind TEXT NOT NULL CHECK (kind IN ('scheduled', 'manual', 'retry')),
        state TEXT NOT NULL
            CHECK (state IN ('claimed', 'running', 'completed', 'failed', 'unknown')),
        owner_pid INTEGER NOT NULL,
        owner_start_fingerprint TEXT,
        claimed_at TEXT NOT NULL,
        heartbeat_at TEXT,
        started_at TEXT,
        finished_at TEXT,
        result_status TEXT,
        delivery_status TEXT,
        error TEXT
    )""",
    # A scheduled slot is claimed at most once, whatever happened to the attempt.
    """CREATE UNIQUE INDEX IF NOT EXISTS attempts_slot
        ON attempts (job_id, scheduled_for)
        WHERE kind = 'scheduled' AND scheduled_for IS NOT NULL""",
    "CREATE INDEX IF NOT EXISTS attempts_job ON attempts (job_id, claimed_at)",
    "CREATE INDEX IF NOT EXISTS attempts_state ON attempts (state)",
    """CREATE TABLE IF NOT EXISTS job_notes (
        job_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (job_id, key)
    )""",
)

_OWNER_GONE = "the process that ran it ended before recording a result; whether it ran is unknown"
_OWNER_SILENT = (
    "the process that ran it stopped reporting progress for longer than {bound:g} s; "
    "whether it finished is unknown (the process was not stopped)"
)


def iso(value: datetime) -> str:
    """The stored form of an instant: UTC, microsecond precision (sorts as text)."""
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def silence_bound(inactivity_timeout_s: float, script_timeout_s: float) -> float:
    """How long a live owner may stay silent: max(3 × inactivity, script timeout, 2 h)."""
    inactivity = inactivity_timeout_s if inactivity_timeout_s > 0 else 0.0
    return max(3 * inactivity, max(script_timeout_s, 0.0), LIVE_SILENCE_FLOOR_S)


# -- processes ----------------------------------------------------------------------------------


class ProcessProbe(Protocol):
    """What the reaper needs to know about processes (tests use ``tests.fakes.pids``)."""

    def current(self) -> tuple[int, str | None]:
        """This process: (pid, start fingerprint)."""
        ...

    def exists(self, pid: int) -> bool: ...

    def fingerprint(self, pid: int) -> str | None:
        """An exact identity of the process start, or None when it cannot be read."""
        ...


class SystemProcesses:
    """Real processes. The fingerprint is Linux-only (boot id + start ticks)."""

    def __init__(self, proc: Path = Path("/proc")) -> None:
        self.proc = proc
        self._boot: str | None = None

    def _boot_id(self) -> str:
        if self._boot is None:
            try:
                path = self.proc / "sys/kernel/random/boot_id"
                self._boot = path.read_text(encoding="ascii").strip()
            except OSError:
                self._boot = ""
        return self._boot

    def current(self) -> tuple[int, str | None]:
        pid = os.getpid()
        return pid, self.fingerprint(pid)

    def exists(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if sys.platform.startswith("linux"):
            return (self.proc / str(pid)).is_dir()
        if sys.platform == "win32":
            return True  # no side-effect-free probe here; the silence rule still applies
        # Signal 0 only asks the kernel whether the PID is taken; any answer but "no such
        # process" (EPERM included) means some process holds it.
        try:
            os.kill(pid, 0)
        except OSError as exc:
            return exc.errno != errno.ESRCH
        return True

    def fingerprint(self, pid: int) -> str | None:
        if not sys.platform.startswith("linux"):
            return None
        try:
            raw = (self.proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        # The command name (field 2) may hold spaces and parentheses; fields after the last ")".
        fields = raw.rpartition(")")[2].split()
        if len(fields) < 20:
            return None
        return f"{self._boot_id()}:{fields[19]}"


# -- the database -------------------------------------------------------------------------------


class ExecutionsDB:
    """``executions.db``: one writer connection (P02 discipline) shared by ledger and notepad."""

    def __init__(self, scope: Scope, path: Path | None = None) -> None:
        self.scope = scope
        self.path = path if path is not None else scope.home / "cron" / "executions.db"
        self.clock: Clock = scope.clock
        handle = open_store(scope, self.path, on_connected=self._setup)
        self.path = handle.path
        self.writer = Writer(self.path, handle.conn, clock=scope.clock)

    def _setup(self, conn: sqlite3.Connection) -> None:
        cfg = journal.database_config(self.scope)
        journal.apply(conn, cfg, self.path)
        journal.apply_pragmas(conn, cfg)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def read[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self.writer.borrow() as conn:
            return fn(conn)

    def write[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return self.writer.write(fn)

    def close(self) -> None:
        self.writer.close()


# -- attempts -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    job_id: str
    scheduled_for: datetime | None
    kind: AttemptKind
    state: AttemptState
    owner_pid: int
    owner_start_fingerprint: str | None
    claimed_at: datetime
    heartbeat_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    result_status: str | None
    delivery_status: str | None
    error: str | None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Attempt:
        claimed = _parse(row["claimed_at"])
        assert claimed is not None
        return cls(
            attempt_id=row["attempt_id"],
            job_id=row["job_id"],
            scheduled_for=_parse(row["scheduled_for"]),
            kind=row["kind"],
            state=row["state"],
            owner_pid=int(row["owner_pid"]),
            owner_start_fingerprint=row["owner_start_fingerprint"],
            claimed_at=claimed,
            heartbeat_at=_parse(row["heartbeat_at"]),
            started_at=_parse(row["started_at"]),
            finished_at=_parse(row["finished_at"]),
            result_status=row["result_status"],
            delivery_status=row["delivery_status"],
            error=row["error"],
        )


def _fetch(conn: sqlite3.Connection, attempt_id: str) -> Attempt | None:
    row = conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    return Attempt.from_row(row) if row is not None else None


class ExecutionLedger:
    """Attempt bookkeeping over an ``ExecutionsDB``."""

    def __init__(
        self, db: ExecutionsDB, *, clock: Clock | None = None, procs: ProcessProbe | None = None
    ) -> None:
        self.db = db
        self.clock = clock if clock is not None else db.clock
        self.procs: ProcessProbe = procs if procs is not None else SystemProcesses()
        self._me = self.procs.current()

    def _now(self) -> str:
        return iso(self.clock.now())

    # -- writes ----------------------------------------------------------------------------------

    def claim(
        self, job_id: str, *, scheduled_for: datetime | None, kind: AttemptKind = "scheduled"
    ) -> Attempt:
        """Record an attempt before dispatch; ``SlotTaken`` when the slot already has one."""
        if kind == "scheduled" and scheduled_for is None:
            raise ValueError("a scheduled attempt needs the slot it runs")
        attempt_id = uuid.uuid4().hex
        pid, fingerprint = self._me
        now = self._now()
        slot = iso(scheduled_for) if scheduled_for is not None else None

        def insert(conn: sqlite3.Connection) -> Attempt:
            conn.execute(
                """INSERT INTO attempts (attempt_id, job_id, scheduled_for, kind, state,
                       owner_pid, owner_start_fingerprint, claimed_at, heartbeat_at)
                   VALUES (?, ?, ?, ?, 'claimed', ?, ?, ?, ?)""",
                (attempt_id, job_id, slot, kind, pid, fingerprint, now, now),
            )
            found = _fetch(conn, attempt_id)
            assert found is not None
            return found

        try:
            return self.db.write(insert)
        except sqlite3.IntegrityError as exc:
            raise SlotTaken(
                f"the run of job {job_id} due at {slot} was already attempted; it is not run again"
            ) from exc

    def _transition(
        self,
        attempt_id: str,
        sql: str,
        params: tuple[Any, ...],
        *,
        what: str,
    ) -> Attempt:
        def apply(conn: sqlite3.Connection) -> Attempt:
            current = _fetch(conn, attempt_id)
            if current is None:
                raise CronError(f"no attempt {attempt_id}")
            if current.terminal:
                raise AttemptFinal(
                    f"attempt {attempt_id} is already {current.state}; it cannot be {what}"
                )
            conn.execute(sql, (*params, attempt_id))
            found = _fetch(conn, attempt_id)
            assert found is not None
            return found

        return self.db.write(apply)

    def start(self, attempt_id: str) -> Attempt:
        now = self._now()
        return self._transition(
            attempt_id,
            """UPDATE attempts SET state = 'running', started_at = COALESCE(started_at, ?),
                   heartbeat_at = ? WHERE attempt_id = ?""",
            (now, now),
            what="started",
        )

    def heartbeat(self, attempt_id: str) -> bool:
        now = self._now()

        def apply(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE attempts SET heartbeat_at = ? WHERE attempt_id = ? "
                "AND state IN ('claimed', 'running')",
                (now, attempt_id),
            )
            return cur.rowcount == 1

        return self.db.write(apply)

    def finish(
        self,
        attempt_id: str,
        *,
        ok: bool,
        result_status: str | None = None,
        delivery_status: str | None = None,
        error: str | None = None,
    ) -> Attempt:
        """Write the terminal result once (``completed`` when ``ok``, else ``failed``)."""
        state = "completed" if ok else "failed"
        now = self._now()
        attempt = self._transition(
            attempt_id,
            """UPDATE attempts SET state = ?, finished_at = ?, result_status = ?,
                   delivery_status = ?, error = ? WHERE attempt_id = ?""",
            (state, now, result_status, delivery_status, None if ok else (error or "failed")),
            what="finished again",
        )
        self._prune(attempt.job_id)
        return attempt

    def note_delivery(self, attempt_id: str, status: str) -> bool:
        """Fill in ``delivery_status`` once (it may arrive after the result)."""

        def apply(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE attempts SET delivery_status = ? WHERE attempt_id = ? "
                "AND delivery_status IS NULL",
                (status, attempt_id),
            )
            return cur.rowcount == 1

        return self.db.write(apply)

    def _prune(self, job_id: str) -> None:
        def apply(conn: sqlite3.Connection) -> None:
            conn.execute(
                """DELETE FROM attempts WHERE job_id = ? AND state IN
                       ('completed', 'failed', 'unknown')
                   AND attempt_id NOT IN (
                       SELECT attempt_id FROM attempts WHERE job_id = ? AND state IN
                           ('completed', 'failed', 'unknown')
                       ORDER BY COALESCE(finished_at, claimed_at) DESC, claimed_at DESC
                       LIMIT ?)""",
                (job_id, job_id, KEEP_PER_JOB),
            )

        self.db.write(apply)

    # -- reaping ---------------------------------------------------------------------------------

    def _is_mine(self, attempt: Attempt) -> bool:
        pid, fingerprint = self._me
        return attempt.owner_pid == pid and attempt.owner_start_fingerprint == fingerprint

    def _owner_dead(self, attempt: Attempt) -> bool:
        if not self.procs.exists(attempt.owner_pid):
            return True
        recorded = attempt.owner_start_fingerprint
        if recorded is None:
            return False
        current = self.procs.fingerprint(attempt.owner_pid)
        return current is not None and current != recorded

    def reap(
        self, *, inactivity_timeout_s: float = 600.0, script_timeout_s: float = 3600.0
    ) -> list[Attempt]:
        """Mark abandoned attempts ``unknown`` (never re-run); returns the ones changed."""
        bound = silence_bound(inactivity_timeout_s, script_timeout_s)
        now_dt = self.clock.now().astimezone(UTC)
        now = iso(now_dt)

        def apply(conn: sqlite3.Connection) -> list[Attempt]:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE state IN ('claimed', 'running')"
            ).fetchall()
            changed: list[Attempt] = []
            for row in rows:
                attempt = Attempt.from_row(row)
                if self._is_mine(attempt):
                    continue
                if self._owner_dead(attempt):
                    reason = _OWNER_GONE
                else:
                    last = attempt.heartbeat_at or attempt.claimed_at
                    if now_dt - last <= timedelta(seconds=bound):
                        continue
                    reason = _OWNER_SILENT.format(bound=bound)
                conn.execute(
                    "UPDATE attempts SET state = 'unknown', finished_at = ?, error = ? "
                    "WHERE attempt_id = ? AND state IN ('claimed', 'running')",
                    (now, reason, attempt.attempt_id),
                )
                found = _fetch(conn, attempt.attempt_id)
                assert found is not None
                changed.append(found)
            return changed

        return self.db.write(apply)

    # -- reads -----------------------------------------------------------------------------------

    def get(self, attempt_id: str) -> Attempt | None:
        return self.db.read(lambda conn: _fetch(conn, attempt_id))

    def slot_attempted(self, job_id: str, scheduled_for: datetime) -> bool:
        """Has this scheduled slot an attempt (in any state)?"""
        slot = iso(scheduled_for)

        def query(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT 1 FROM attempts WHERE job_id = ? AND scheduled_for = ? "
                "AND kind = 'scheduled' LIMIT 1",
                (job_id, slot),
            ).fetchone()
            return row is not None

        return self.db.read(query)

    def history(self, job_id: str, limit: int = 50) -> list[Attempt]:
        def query(conn: sqlite3.Connection) -> list[Attempt]:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE job_id = ? ORDER BY claimed_at DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
            return [Attempt.from_row(r) for r in rows]

        return self.db.read(query)

    def recent(self, limit: int = 50) -> list[Attempt]:
        def query(conn: sqlite3.Connection) -> list[Attempt]:
            rows = conn.execute(
                "SELECT * FROM attempts ORDER BY claimed_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [Attempt.from_row(r) for r in rows]

        return self.db.read(query)

    def active(self, job_id: str | None = None) -> list[Attempt]:
        def query(conn: sqlite3.Connection) -> list[Attempt]:
            if job_id is None:
                rows = conn.execute(
                    "SELECT * FROM attempts WHERE state IN ('claimed', 'running') "
                    "ORDER BY claimed_at"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM attempts WHERE job_id = ? AND state IN "
                    "('claimed', 'running') ORDER BY claimed_at",
                    (job_id,),
                ).fetchall()
            return [Attempt.from_row(r) for r in rows]

        return self.db.read(query)
