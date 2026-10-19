"""Deleting sessions, and the tombstones that keep a deleted id from coming back (P02-18).

One write transaction removes the session row with its messages, metadata, usage and lock rows.
Sessions it delegated work to (kind ``delegate``) go with it, recursively; a branch, a reset or a
compression continuation is a conversation of its own and only loses its parent link. A session
some process is writing right now — a live turn lease or compression lock — is refused; a lease
whose holder expired or died in our PID namespace is removed first and protects nothing.

Each removed id leaves ``meta["tombstone:<id>"] = {"deleted_at": …, "by": …}`` in the same
transaction, so it can never be created or imported again (R03.22). ``by == "user"`` stays for
good; ``by == "sweep"`` (housekeeping: empty sessions, prune) is dropped by
``reap_tombstones_in`` once it is older than ``SWEEP_TOMBSTONE_SECONDS``. A sweep never weakens a
user tombstone.

After the commit, ``<home>/sessions/<id>.json`` / ``.jsonl`` are removed best-effort and every
hook registered for ``on_session_delete`` runs; a hook that raises is logged and ignored.
Every ``IN (…)`` list is cut into chunks of ``SQL_CHUNK`` ids (R03.18).
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from indus.foundation.errors import StoreError
from indus.state import holders, lineage, runtime_ledger, sessions
from indus.state.guards import divert_path
from indus.state.registry import StoreHandle
from indus.state.writer import ROUTINE

__all__ = [
    "SQL_CHUNK",
    "SWEEP_TOMBSTONE_SECONDS",
    "DeleteBy",
    "DeleteReport",
    "SessionDeleted",
    "SessionGuarded",
    "Tombstone",
    "after_delete",
    "chain_ids_in",
    "children_in",
    "chunked",
    "delegate_descendants_in",
    "delete_if_empty",
    "delete_many",
    "delete_rows_in",
    "delete_session",
    "guarded_ids_in",
    "is_continuation",
    "is_tombstoned",
    "reap_tombstones_in",
    "rows_by_id_in",
    "register_on_session_delete",
    "tombstone_in",
    "unregister_on_session_delete",
    "write_tombstone_in",
]

log = logging.getLogger(__name__)

SQL_CHUNK: Final = 900
SWEEP_TOMBSTONE_SECONDS: Final = 7 * 24 * 3600.0

DeleteBy = Literal["user", "sweep"]


class SessionGuarded(StoreError):
    """The session (or a delegate child) is under a live turn lease or compression lock."""

    default_code = "store.session_guarded"


@dataclass(frozen=True)
class Tombstone:
    session_id: str
    deleted_at: float
    by: str


@dataclass
class DeleteReport:
    """What ``delete_many`` did: requested ids deleted, every row removed, ids left alone."""

    deleted: list[str] = field(default_factory=lambda: [])
    removed: list[str] = field(default_factory=lambda: [])
    unknown: list[str] = field(default_factory=lambda: [])
    guarded: list[str] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class SessionDeleted:
    """What a delete hook is told after the commit."""

    session_ids: tuple[str, ...]
    reason: str
    profile_id: str
    home: Path


# -- helpers --------------------------------------------------------------------------------------


def chunked(ids: Iterable[str], size: int = SQL_CHUNK) -> Iterator[list[str]]:
    """``ids`` in lists of at most ``size`` (the bound-variable cap of older SQLite builds)."""
    batch: list[str] = []
    for item in ids:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _marks(batch: Sequence[object]) -> str:
    return ",".join("?" * len(batch))


def _now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


def _existing_in(conn: sqlite3.Connection, ids: Sequence[str]) -> set[str]:
    found: set[str] = set()
    for batch in chunked(ids):
        rows = conn.execute(f"SELECT id FROM sessions WHERE id IN ({_marks(batch)})", batch)
        found.update(str(r[0]) for r in rows.fetchall())
    return found


def rows_by_id_in(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for batch in chunked(ids):
        for row in conn.execute(f"SELECT * FROM sessions WHERE id IN ({_marks(batch)})", batch):
            out[str(row["id"])] = dict(row)
    return out


def children_in(conn: sqlite3.Connection, parent_ids: Iterable[str]) -> list[dict[str, Any]]:
    kids: list[dict[str, Any]] = []
    for batch in chunked(parent_ids):
        query = f"SELECT * FROM sessions WHERE parent_session_id IN ({_marks(batch)})"
        kids.extend(dict(r) for r in conn.execute(query, batch))
    return kids


# -- tombstones -----------------------------------------------------------------------------------


def _parse_tombstone(session_id: str, raw: object) -> Tombstone:
    data: Any = None
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            data = json.loads(raw)
    if not isinstance(data, dict):
        # An unreadable marker still fences the id; treat it as a permanent one.
        return Tombstone(session_id, 0.0, "user")
    when = data.get("deleted_at")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    by = data.get("by")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    stamp = float(when) if isinstance(when, int | float) else 0.0
    return Tombstone(session_id, stamp, by if by in ("user", "sweep") else "user")


def tombstone_in(conn: sqlite3.Connection, session_id: str) -> Tombstone | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (sessions.TOMBSTONE_PREFIX + session_id,)
    ).fetchone()
    return None if row is None else _parse_tombstone(session_id, row[0])


def write_tombstone_in(
    conn: sqlite3.Connection, session_id: str, *, by: DeleteBy, now: float
) -> None:
    """Fence ``session_id``; a sweep marker never replaces a permanent (user) one."""
    existing = tombstone_in(conn, session_id)
    if existing is not None and existing.by == "user" and by == "sweep":
        return
    value = json.dumps({"deleted_at": now, "by": by}, separators=(",", ":"))
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (sessions.TOMBSTONE_PREFIX + session_id, value),
    )


def reap_tombstones_in(conn: sqlite3.Connection, *, now: float) -> int:
    """Drop sweep tombstones older than the 7-day window; user tombstones stay."""
    prefix = sessions.TOMBSTONE_PREFIX
    upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key >= ? AND key < ?", (prefix, upper)
    ).fetchall()
    cutoff = now - SWEEP_TOMBSTONE_SECONDS
    aged = [
        str(r[0])
        for r in rows
        if (stone := _parse_tombstone(str(r[0])[len(prefix) :], r[1])).by == "sweep"
        and stone.deleted_at < cutoff
    ]
    for batch in chunked(aged):
        conn.execute(f"DELETE FROM meta WHERE key IN ({_marks(batch)})", batch)
    return len(aged)


def is_tombstoned(store: StoreHandle, session_id: str) -> bool:
    with store.read() as conn:
        return sessions.is_tombstoned_in(conn, session_id)


# -- guards ---------------------------------------------------------------------------------------


def guarded_ids_in(
    conn: sqlite3.Connection, ids: Iterable[str], *, now: float, reap: bool = True
) -> set[str]:
    """Ids protected by a live turn lease or compression lock.

    Stale guards (expired, or a dead holder in our PID namespace) protect nothing; with ``reap``
    they are deleted on the way (needs a write transaction), without it they are only ignored.
    """
    wanted = list(dict.fromkeys(ids))
    live: set[str] = set()
    for table, column in (("turn_leases", "conversation_id"), ("compression_locks", "session_id")):
        stale: list[str] = []
        for batch in chunked(wanted):
            query = (
                f"SELECT {column}, holder, expires_at FROM {table} "
                f"WHERE {column} IN ({_marks(batch)})"
            )
            for row in conn.execute(query, batch):
                sid = str(row[0])
                if holders.holder_is_stale(row[1], row[2], now):
                    stale.append(sid)
                else:
                    live.add(sid)
        if reap:
            for batch in chunked(stale):
                conn.execute(f"DELETE FROM {table} WHERE {column} IN ({_marks(batch)})", batch)
    return live


# -- what a delete covers -------------------------------------------------------------------------


def delegate_descendants_in(conn: sqlite3.Connection, ids: Iterable[str]) -> list[str]:
    """Every session delegated from ``ids``, recursively; never one of ``ids`` themselves."""
    roots = set(ids)
    seen = set(roots)
    found: list[str] = []
    frontier = list(roots)
    while frontier:
        parents = rows_by_id_in(conn, frontier)
        nxt: list[str] = []
        for child in children_in(conn, frontier):
            cid = str(child["id"])
            if cid in seen:
                continue
            parent = parents.get(str(child["parent_session_id"]))
            if sessions.child_kind(child, parent) == "delegate":
                seen.add(cid)
                found.append(cid)
                nxt.append(cid)
        frontier = nxt
    return found


def is_continuation(child: dict[str, Any], parent: dict[str, Any] | None) -> bool:
    if parent is None or parent.get("end_reason") not in sessions.COMPRESSION_REASONS:
        return False
    return sessions.child_kind(child, parent) == "continuation"


def chain_ids_in(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """The whole compression chain of ``session_id``: its root and every continuation after it
    (stale sibling continuations included), root first."""
    root = lineage.root_in(conn, session_id)
    chain = [root]
    seen = {root}
    frontier = [root]
    while frontier:
        parents = rows_by_id_in(conn, frontier)
        nxt: list[str] = []
        for child in children_in(conn, frontier):
            cid = str(child["id"])
            if cid not in seen and is_continuation(
                child, parents.get(str(child["parent_session_id"]))
            ):
                seen.add(cid)
                chain.append(cid)
                nxt.append(cid)
        frontier = nxt
    return chain


# -- the delete itself ----------------------------------------------------------------------------

_CHILD_TABLES: Final = (
    ("messages", "session_id"),
    ("session_meta", "session_id"),
    ("model_usage", "session_id"),
    ("compression_locks", "session_id"),
    ("turn_leases", "conversation_id"),
    # P15-07: a gateway route pointing at a deleted session is dropped; the chat's next message
    # gets a fresh session (the route is created again, never left dangling).
    ("gateway_routes", "session_id"),
)


def delete_rows_in(
    conn: sqlite3.Connection,
    ids: Sequence[str],
    *,
    by: DeleteBy,
    now: float,
    cascade_delegates: bool = True,
) -> list[str]:
    """Delete ``ids`` (and, with ``cascade_delegates``, their delegate descendants) inside the
    caller's transaction: tombstones, orphaned children, child rows, prompt GC. Returns every
    removed id. The caller has already checked guards and existence."""
    doomed = list(dict.fromkeys(ids))
    if cascade_delegates:
        doomed.extend(delegate_descendants_in(conn, doomed))
    for sid in doomed:
        write_tombstone_in(conn, sid, by=by, now=now)
    # P17-01: the daemon's admissions for these sessions become retirement markers, so an exact
    # retry of one answers with its receipt and never admits work for a deleted id (R03.22).
    runtime_ledger.retire_sessions_in(conn, doomed, now)
    for batch in chunked(doomed):
        marks = _marks(batch)
        conn.execute(
            f"UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ({marks})",
            batch,
        )
        for table, column in _CHILD_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})", batch)
        conn.execute(
            f"UPDATE gateway_routes SET prev_session_id = NULL WHERE prev_session_id IN ({marks})",
            batch,
        )
        conn.execute(f"DELETE FROM sessions WHERE id IN ({marks})", batch)
    if doomed:
        conn.execute(
            "DELETE FROM system_prompts WHERE NOT EXISTS "
            "(SELECT 1 FROM sessions s WHERE s.system_prompt_hash = system_prompts.hash)"
        )
    return doomed


def _live_message_ids(conn: sqlite3.Connection, session_id: str) -> set[int]:
    rows = conn.execute(
        "SELECT id FROM messages WHERE session_id = ? AND active = 1", (session_id,)
    ).fetchall()
    return {int(r[0]) for r in rows}


def delete_session(
    store: StoreHandle,
    session_id: str,
    *,
    expect_message_ids: Iterable[int] | None = None,
    whole_chain: bool = False,
) -> bool:
    """Delete one session (a user delete). False when the id is unknown, or when its live
    message ids are not ``expect_message_ids`` (the transcript moved since the caller looked).
    Raises ``SessionGuarded`` while it or a row the delete would take is in use."""
    expected = None if expect_message_ids is None else {int(i) for i in expect_message_ids}
    now = _now(store)

    def run(conn: sqlite3.Connection) -> list[str]:
        if not _existing_in(conn, [session_id]):
            return []
        scope = chain_ids_in(conn, session_id) if whole_chain else [session_id]
        covered = [*scope, *delegate_descendants_in(conn, scope)]
        if guarded_ids_in(conn, covered, now=now):
            raise SessionGuarded(
                f"session {session_id} is in use (a live turn or compression); not deleted",
                hint="wait for the running turn to finish, then delete again",
            )
        if expected is not None and _live_message_ids(conn, session_id) != expected:
            return []
        return delete_rows_in(conn, scope, by="user", now=now)

    removed = store.write(run, budget=ROUTINE)
    if removed:
        after_delete(store, removed, "delete")
    return bool(removed)


def delete_many(
    store: StoreHandle, session_ids: Iterable[str], *, whole_chain: bool = False
) -> DeleteReport:
    """User delete of many sessions in one transaction; unknown ids and ids in use are skipped
    and reported, never raised."""
    requested = [s for s in dict.fromkeys(session_ids) if s]
    now = _now(store)

    def run(conn: sqlite3.Connection) -> DeleteReport:
        report = DeleteReport()
        present = _existing_in(conn, requested)
        report.unknown = [s for s in requested if s not in present]
        targets = [s for s in requested if s in present]
        scopes: dict[str, list[str]] = {
            s: (chain_ids_in(conn, s) if whole_chain else [s]) for s in targets
        }
        everything = list(dict.fromkeys(x for scope in scopes.values() for x in scope))
        pool = [*everything, *delegate_descendants_in(conn, everything)]
        if guarded_ids_in(conn, pool, now=now):
            for sid in targets:
                own = scopes[sid]
                if guarded_ids_in(conn, [*own, *delegate_descendants_in(conn, own)], now=now):
                    report.guarded.append(sid)
        keep = set(report.guarded)
        report.deleted = [s for s in targets if s not in keep]
        doomed = list(dict.fromkeys(x for s in report.deleted for x in scopes[s]))
        report.removed = delete_rows_in(conn, doomed, by="user", now=now)
        return report

    report = store.write(run, budget=ROUTINE)
    if report.removed:
        after_delete(store, report.removed, "delete")
    return report


def delete_if_empty(store: StoreHandle, session_id: str) -> bool:
    """Housekeeping delete: only an ended, untitled, childless, message-less row not in use."""
    now = _now(store)

    def run(conn: sqlite3.Connection) -> list[str]:
        row = conn.execute(
            "SELECT 1 FROM sessions s WHERE s.id = ? AND s.ended_at IS NOT NULL "
            "AND s.title IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id) "
            "AND NOT EXISTS (SELECT 1 FROM sessions c WHERE c.parent_session_id = s.id)",
            (session_id,),
        ).fetchone()
        if row is None or guarded_ids_in(conn, [session_id], now=now):
            return []
        return delete_rows_in(conn, [session_id], by="sweep", now=now, cascade_delegates=False)

    removed = store.write(run, budget=ROUTINE)
    if removed:
        after_delete(store, removed, "empty")
    return bool(removed)


# -- after the commit -----------------------------------------------------------------------------

DeleteHook = Callable[[SessionDeleted], None]

_hooks: list[DeleteHook] = []
_hooks_lock = threading.Lock()


def register_on_session_delete(hook: DeleteHook) -> None:
    """Call ``hook`` after every committed delete (P13 wires plugin hooks here)."""
    with _hooks_lock:
        if hook not in _hooks:
            _hooks.append(hook)


def unregister_on_session_delete(hook: DeleteHook) -> None:
    with _hooks_lock:
        if hook in _hooks:
            _hooks.remove(hook)


def _remove_side_files(home: Path, session_id: str) -> None:
    try:
        jsonl = divert_path(home, session_id)
    except StoreError:
        return  # not a plain file name: there can be no side file for it
    for target in (jsonl, jsonl.with_suffix(".json")):
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove %s after deleting its session: %s", target, exc)


def after_delete(store: StoreHandle, removed: Sequence[str], reason: str) -> None:
    """Post-commit work for removed ids: side files, then the hooks. Never raises."""
    if not removed:
        return
    home = store.scope.home
    for sid in removed:
        _remove_side_files(home, sid)
    event = SessionDeleted(tuple(removed), reason, store.scope.profile_id, home)
    with _hooks_lock:
        hooks = list(_hooks)
    for hook in hooks:
        try:
            hook(event)
        except Exception:
            log.warning("an on_session_delete hook failed (ignored)", exc_info=True)
