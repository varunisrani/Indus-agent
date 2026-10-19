"""Turn lease rows: which process may load, run and flush a session right now (P17-03).

One row per session in ``turn_leases`` (``conversation_id`` is the session id). It names the
holder — pid, process start time, pid namespace (the ``/proc/self/ns/pid`` inode, or ``host:<name>``
where there is none), host and a per-lease nonce — the generation the holder runs, and an expiry
on the wall clock (every process compares the same column).

``holder`` keeps the P02 stamp shape ``pid=<n>:pidns=<inode|->:<host>:start=<t>:lease=<nonce>``,
so the deletion and maintenance guards (``state.holders``) read lease rows unchanged.

A row may be taken over when it has expired, or when its holder is *provably* dead: same host,
same known pid namespace, and the pid is gone or now belongs to a process that started at another
time. A holder in another pid namespace or on another host is never probed — a pid number there
means nothing here — so only its expiry frees the row (R03.20). Refresh and release name the
holder **and** the generation, so a displaced turn can never extend or clear its successor's row
(R06.2).

The ``*_in`` functions run inside the caller's write transaction (``Writer.write`` /
``StoreHandle.awrite``); the plain ones wrap them for synchronous callers. The service side
(waiting, refreshing, loss) is ``indus.service.turn_lease``.
"""

from __future__ import annotations

import os
import secrets
import socket
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from indus.state import gateway_runtime
from indus.state.registry import StoreHandle
from indus.state.runtime_ledger import next_generation_in
from indus.state.writer import ROUTINE, Budget

__all__ = [
    "Acquired",
    "Held",
    "LeaseHolder",
    "LeaseRow",
    "ProcessProbe",
    "default_probe",
    "get",
    "get_in",
    "holder_is_dead",
    "local_namespace",
    "refresh",
    "refresh_in",
    "release",
    "release_in",
    "try_acquire",
    "try_acquire_in",
]

_COLUMNS: Final = (
    "conversation_id, holder, acquired_at, expires_at, holder_pid, holder_start, holder_ns, "
    "holder_host, generation, refreshed_at"
)


def local_namespace() -> str:
    """This process's pid-namespace id: the namespace inode, else ``host:<hostname>``."""
    inode = gateway_runtime.pid_namespace()
    return inode if inode else f"host:{_host()}"


def _host() -> str:
    try:
        return socket.gethostname() or "localhost"
    except OSError:
        return "localhost"


@dataclass(frozen=True)
class LeaseHolder:
    """Who asks for (or holds) a lease."""

    holder_id: str
    pid: int
    start_time: int | None
    namespace: str
    host: str

    @classmethod
    def current(cls, nonce: str | None = None) -> LeaseHolder:
        """This process, with a fresh nonce (each lease in a process is its own holder)."""
        me = gateway_runtime.ProcessIdentity.current()
        namespace = me.pidns or f"host:{me.host or 'localhost'}"
        tag = nonce or secrets.token_hex(6)
        stamp_ns = me.pidns or "-"
        holder_id = (
            f"pid={me.pid}:pidns={stamp_ns}:{me.host or 'localhost'}"
            f":start={me.start_time if me.start_time is not None else '-'}:lease={tag}"
        )
        return cls(holder_id, me.pid, me.start_time, namespace, me.host or "localhost")


@dataclass(frozen=True)
class LeaseRow:
    session_id: str
    holder_id: str
    acquired_at: float
    expires_at: float
    holder_pid: int | None
    holder_start: int | None
    holder_ns: str | None
    holder_host: str | None
    generation: int
    refreshed_at: float | None


@dataclass(frozen=True)
class Acquired:
    """The row is ours: ``row`` as committed, and the holder it was taken from (if any)."""

    row: LeaseRow
    reclaimed_from: str | None = None


@dataclass(frozen=True)
class Held:
    """Another live holder has the row."""

    row: LeaseRow


@dataclass(frozen=True)
class ProcessProbe:
    """How liveness is probed (tests pass fakes): ``alive(pid)``, ``start_time(pid)``,
    ``namespace()``, ``host()``."""

    alive: Callable[[int], bool]
    start_time: Callable[[int], int | None]
    namespace: Callable[[], str]
    host: Callable[[], str]


def default_probe() -> ProcessProbe:
    return ProcessProbe(
        alive=gateway_runtime.pid_alive,
        start_time=gateway_runtime.process_start_time,
        namespace=local_namespace,
        host=_host,
    )


def holder_is_dead(row: LeaseRow, probe: ProcessProbe | None = None) -> bool:
    """True only with proof that the holder's process is gone (see the module notes)."""
    check = probe or default_probe()
    pid = row.holder_pid
    if pid is None or pid < 1 or not row.holder_ns or not row.holder_host:
        return False
    try:
        if row.holder_host != check.host() or row.holder_ns != check.namespace():
            return False  # another container or machine: wait for the expiry
        if pid == os.getpid():
            return False  # another lease in this very process: it is alive
        if not check.alive(pid):
            return True
        now_start = check.start_time(pid)
    except Exception:  # any doubt keeps the row until it expires
        return False
    return row.holder_start is not None and now_start is not None and now_start != row.holder_start


def _row(raw: Sequence[Any]) -> LeaseRow:
    return LeaseRow(
        session_id=str(raw[0]),
        holder_id=str(raw[1]),
        acquired_at=float(raw[2]),
        expires_at=float(raw[3]),
        holder_pid=None if raw[4] is None else int(raw[4]),
        holder_start=None if raw[5] is None else int(raw[5]),
        holder_ns=None if raw[6] is None else str(raw[6]),
        holder_host=None if raw[7] is None else str(raw[7]),
        generation=int(raw[8] or 0),
        refreshed_at=None if raw[9] is None else float(raw[9]),
    )


def get_in(conn: sqlite3.Connection, session_id: str) -> LeaseRow | None:
    found = conn.execute(
        f"SELECT {_COLUMNS} FROM turn_leases WHERE conversation_id = ?", (session_id,)
    ).fetchone()
    return None if found is None else _row(tuple(found))


def get(store: StoreHandle, session_id: str) -> LeaseRow | None:
    with store.read() as conn:
        return get_in(conn, session_id)


def try_acquire_in(
    conn: sqlite3.Connection,
    session_id: str,
    holder: LeaseHolder,
    *,
    now: float,
    ttl: float,
    generation: int | None = None,
    is_dead: Callable[[LeaseRow], bool] = holder_is_dead,
) -> Acquired | Held:
    """Take the session's lease for ``holder`` (one write transaction).

    A row of the same holder is renewed (re-entrant). ``generation`` None mints the session's next
    generation from the runtime ledger's counter; a claimed admission passes its own."""
    if not session_id:
        raise ValueError("a turn lease needs a session id")
    current = get_in(conn, session_id)
    reclaimed: str | None = None
    if current is not None:
        if current.holder_id == holder.holder_id:
            conn.execute(
                "UPDATE turn_leases SET expires_at = ?, refreshed_at = ? "
                "WHERE conversation_id = ? AND holder = ?",
                (now + ttl, now, session_id, holder.holder_id),
            )
            mine = get_in(conn, session_id)
            assert mine is not None
            return Acquired(mine)
        if current.expires_at > now and not is_dead(current):
            return Held(current)
        conn.execute(
            "DELETE FROM turn_leases WHERE conversation_id = ? AND holder = ?",
            (session_id, current.holder_id),
        )
        reclaimed = current.holder_id
    gen = next_generation_in(conn, session_id, now) if generation is None else int(generation)
    conn.execute(
        f"INSERT INTO turn_leases({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            holder.holder_id,
            now,
            now + ttl,
            holder.pid,
            holder.start_time,
            holder.namespace,
            holder.host,
            gen,
            now,
        ),
    )
    mine = get_in(conn, session_id)
    assert mine is not None
    return Acquired(mine, reclaimed_from=reclaimed)


def refresh_in(
    conn: sqlite3.Connection,
    session_id: str,
    holder_id: str,
    generation: int,
    *,
    now: float,
    ttl: float,
) -> bool:
    """Push the expiry to ``now + ttl`` while ``holder_id`` still holds ``generation``."""
    cur = conn.execute(
        "UPDATE turn_leases SET expires_at = ?, refreshed_at = ? "
        "WHERE conversation_id = ? AND holder = ? AND generation = ?",
        (now + ttl, now, session_id, holder_id, generation),
    )
    return cur.rowcount > 0


def release_in(conn: sqlite3.Connection, session_id: str, holder_id: str, generation: int) -> bool:
    """Delete the row only while it is still ``holder_id``'s for ``generation``; idempotent."""
    cur = conn.execute(
        "DELETE FROM turn_leases WHERE conversation_id = ? AND holder = ? AND generation = ?",
        (session_id, holder_id, generation),
    )
    return cur.rowcount > 0


def _now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


def try_acquire(
    store: StoreHandle,
    session_id: str,
    holder: LeaseHolder,
    *,
    ttl: float,
    generation: int | None = None,
    budget: Budget = ROUTINE,
    is_dead: Callable[[LeaseRow], bool] = holder_is_dead,
) -> Acquired | Held:
    now = _now(store)
    return store.write(
        lambda conn: try_acquire_in(
            conn, session_id, holder, now=now, ttl=ttl, generation=generation, is_dead=is_dead
        ),
        budget=budget,
    )


def refresh(
    store: StoreHandle,
    session_id: str,
    holder_id: str,
    generation: int,
    *,
    ttl: float,
    budget: Budget = ROUTINE,
) -> bool:
    now = _now(store)  # stamped before the lock wait: the expiry is never later than it seems
    return store.write(
        lambda conn: refresh_in(conn, session_id, holder_id, generation, now=now, ttl=ttl),
        budget=budget,
    )


def release(
    store: StoreHandle,
    session_id: str,
    holder_id: str,
    generation: int,
    *,
    budget: Budget = ROUTINE,
) -> bool:
    return store.write(
        lambda conn: release_in(conn, session_id, holder_id, generation), budget=budget
    )
