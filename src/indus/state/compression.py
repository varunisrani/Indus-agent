"""Compression records for rotation: the per-session lock, the watermark, the atomic publish of a
continuation, the anti-thrash counters, and chain helpers.

Compression (P08) summarises a long conversation into a child session while the conversation goes
on. The summary call is slow, so:

- **Lock.** One ``compression_locks`` row per session (holder, expiry; TTL 300 s). ``acquire``
  replaces a stale row — expired, or a holder whose process is provably gone (``holders``) — inserts
  ours if the slot is free and reads the row back, all in one transaction, so of N racing callers
  exactly one wins. Lock writes have their own 5 s budget; contention past it means "no".
- **Watermark.** The newest live message id when the summary starts.
- **Publish.** ``publish_continuation`` runs as one write transaction: the lock must still be ours
  and unexpired; the parent is ended with ``compression``; the child row is created from the
  parent's route, workspace and lineage-wide flags; the handoff rows go in first; then every live
  parent row newer than the watermark is cloned behind them (fresh ids, same payload and uids), so
  a message that arrived during the summary is carried over instead of summarised away (R03.10).
  Readers see the live parent or the finished child, never half. Ordinary appends never look at
  the lock.

Anti-thrash counters persist across restarts (cooldown deadline and error, fallback streak,
ineffective count, overload streak); ``snapshot`` / ``restore`` put them back exactly when a
compression attempt is cancelled.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from indus.foundation.errors import StoreError, StoreLocked
from indus.foundation.ids import new_session_id
from indus.state import lineage, messages, sessions, titles
from indus.state.handoff import HANDOFF_FINISH, recount_in
from indus.state.holders import holder_is_stale
from indus.state.registry import StoreHandle
from indus.state.writer import ROUTINE, TRANSCRIPT, Budget

__all__ = [
    "LOCK_BUDGET",
    "LOCK_TTL",
    "ORPHAN_AGE",
    "CompressionBusy",
    "CompressionClosed",
    "CompressionCounters",
    "CompressionParentEnded",
    "CounterName",
    "CounterSnapshot",
    "acquire",
    "acquire_in",
    "active_cooldown",
    "bump_overload_streak",
    "clear_cooldown",
    "counters",
    "holder_of",
    "is_locked_in",
    "lineage_ids",
    "mark_orphans",
    "publish_continuation",
    "publish_continuation_in",
    "record_cooldown",
    "refresh",
    "release",
    "restore",
    "root",
    "set_counter",
    "snapshot",
    "tip",
    "watermark",
    "watermark_in",
]

log = logging.getLogger(__name__)

LOCK_TTL = 300.0
LOCK_BUDGET = Budget("compression_lock", 5.0)
ORPHAN_AGE = 7 * 24 * 3600.0

# Columns a continuation takes over from its parent (``child_fields`` may override any of them).
_CARRIED = (
    "created_source",
    "user_id",
    "model",
    "model_config",
    "system_prompt_hash",
    "cwd",
    "git_root",
    "git_branch",
    "billing_provider",
    "billing_base_url",
    "profile_id",
    "pinned",
    "archived",
    "auto_archived",
    "hidden",
)


class CompressionBusy(StoreError):
    """The compression lock is held by someone else, or ours was lost before publishing."""

    default_code = "store.compression_busy"


class CompressionClosed(StoreError):
    """The session already continues in a compression child."""

    default_code = "store.compression_closed"


class CompressionParentEnded(StoreError):
    """The session was ended on purpose while its summary was being made; nothing was published."""

    default_code = "store.compression_parent_ended"


def _now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


# -- the lock -----------------------------------------------------------------------------------


def acquire_in(
    conn: sqlite3.Connection, session_id: str, holder: str, *, now: float, ttl: float = LOCK_TTL
) -> tuple[bool, str | None]:
    """Claim the lock inside a write transaction: ``(won, holder_we_replaced)``."""
    replaced: str | None = None
    row = conn.execute(
        "SELECT holder, expires_at FROM compression_locks WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is not None and holder_is_stale(row["holder"], row["expires_at"], now):
        conn.execute(
            "DELETE FROM compression_locks WHERE session_id = ? AND holder = ?",
            (session_id, row["holder"]),
        )
        replaced = str(row["holder"])
    conn.execute(
        "INSERT OR IGNORE INTO compression_locks (session_id, holder, acquired_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, holder, now, now + ttl),
    )
    owner = conn.execute(
        "SELECT holder FROM compression_locks WHERE session_id = ?", (session_id,)
    ).fetchone()
    return owner is not None and owner["holder"] == holder, replaced


def acquire(store: StoreHandle, session_id: str, holder: str, *, ttl: float = LOCK_TTL) -> bool:
    """True when ``holder`` now owns the lock; False when someone else does (never raises on
    contention: the caller simply does not compress this time)."""
    if not session_id or not holder:
        return False

    def claim(conn: sqlite3.Connection) -> tuple[bool, str | None]:
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
            return False, None
        return acquire_in(conn, session_id, holder, now=_now(store), ttl=ttl)

    try:
        won, replaced = store.write(claim, budget=LOCK_BUDGET)
    except StoreLocked:
        log.warning("compression lock for %s not taken: the store stayed busy", session_id)
        return False
    if replaced is not None:
        log.warning("replaced a stale compression lock on %s (was %s)", session_id, replaced)
    return won


def refresh(store: StoreHandle, session_id: str, holder: str, *, ttl: float = LOCK_TTL) -> bool:
    """Extend the lock while ``holder`` is still the row's holder (even past its expiry, as long as
    nobody else took it). False when the lock is gone or belongs to someone else."""
    if not session_id or not holder:
        return False

    def extend(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE compression_locks SET expires_at = ? WHERE session_id = ? AND holder = ?",
            (_now(store) + ttl, session_id, holder),
        )
        return cur.rowcount == 1

    try:
        return store.write(extend, budget=LOCK_BUDGET)
    except StoreLocked:
        log.warning("compression lock refresh for %s skipped: the store stayed busy", session_id)
        return False


def release(store: StoreHandle, session_id: str, holder: str) -> bool:
    """Drop the lock if ``holder`` owns it; a lock someone else holds is left alone."""
    if not session_id or not holder:
        return False

    def drop(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "DELETE FROM compression_locks WHERE session_id = ? AND holder = ?",
            (session_id, holder),
        )
        return cur.rowcount == 1

    try:
        return store.write(drop, budget=ROUTINE)
    except StoreLocked:
        log.warning("compression lock release for %s failed: the store stayed busy", session_id)
        return False


def holder_of(store: StoreHandle, session_id: str) -> str | None:
    """The holder of an unexpired lock on ``session_id`` (diagnostics)."""
    with store.read() as conn:
        row = conn.execute(
            "SELECT holder FROM compression_locks WHERE session_id = ? AND expires_at > ?",
            (session_id, _now(store)),
        ).fetchone()
    return None if row is None else str(row["holder"])


def is_locked_in(conn: sqlite3.Connection, session_id: str, *, now: float) -> bool:
    """Inside a write transaction: a live lock protects ``session_id``. A stale row is deleted on
    the way (so the caller's write may proceed)."""
    row = conn.execute(
        "SELECT holder, expires_at FROM compression_locks WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return False
    if holder_is_stale(row["holder"], row["expires_at"], now):
        conn.execute(
            "DELETE FROM compression_locks WHERE session_id = ? AND holder = ?",
            (session_id, row["holder"]),
        )
        log.info("removed a stale compression lock on %s (was %s)", session_id, row["holder"])
        return False
    return True


# -- watermark and publish ----------------------------------------------------------------------


def watermark_in(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ? AND active = 1",
        (session_id,),
    ).fetchone()
    return int(row[0])


def watermark(store: StoreHandle, session_id: str) -> int:
    """The newest live message id of ``session_id`` (0 when it has none)."""
    with store.read() as conn:
        return watermark_in(conn, session_id)


def _check_lock(conn: sqlite3.Connection, session_id: str, holder: str, now: float) -> None:
    row = conn.execute(
        "SELECT holder, expires_at FROM compression_locks WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None or row["holder"] != holder or float(row["expires_at"]) <= now:
        raise CompressionBusy(
            f"the compression lock on session {session_id} was lost before the summary could be "
            "saved; nothing was published",
            hint="the next compression attempt starts from the current transcript",
        )


def _reopen_stale_end(conn: sqlite3.Connection, parent: Mapping[str, Any]) -> None:
    """A parent that is already ended may only continue when the end came from automatic cleanup."""
    if parent.get("ended_at") is None and parent.get("end_reason") is None:
        return
    reason = parent.get("end_reason")
    if reason in sessions.COMPRESSION_REASONS:
        raise CompressionClosed(
            f"session {parent['id']} already continues in a compression child",
            hint="continue in the newest session of this conversation",
        )
    if reason in sessions.AUTOMATIC_REASONS or reason in sessions.RECOVERABLE_REASONS:
        # The lock holder is still running this conversation: the cleanup stamp is out of date.
        conn.execute(
            "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?", (parent["id"],)
        )
        return
    raise CompressionParentEnded(
        f"session {parent['id']} was ended ({reason}) while its summary was being made",
    )


def publish_continuation_in(
    conn: sqlite3.Connection,
    parent_id: str,
    child_id: str,
    child_fields: Mapping[str, Any],
    handoff_rows: Sequence[Mapping[str, Any]],
    watermark: int,
    holder: str,
    *,
    now: float,
    ceiling: int | None = None,
) -> None:
    """The body of ``publish_continuation`` on the caller's write transaction."""
    if not handoff_rows:
        raise ValueError("a continuation needs at least one handoff row")
    _check_lock(conn, parent_id, holder, now)
    parent = sessions.get_in(conn, parent_id)
    if parent is None:
        raise CompressionBusy(f"session {parent_id} no longer exists; nothing was published")
    _reopen_stale_end(conn, parent)
    if not sessions.end_in(conn, parent_id, sessions.END_COMPRESSION, now=now):
        raise CompressionBusy(f"session {parent_id} changed while its continuation was published")

    fields: dict[str, Any] = {name: parent.get(name) for name in _CARRIED}
    fields.update(child_fields)
    title = fields.pop("title", None)
    title_source = fields.pop("title_source", None) or "user"
    source = str(fields.pop("source", None) or parent["source"])
    fields["parent_session_id"] = parent_id
    if not sessions.create_in(conn, child_id, source, now=now, **fields):
        raise ValueError(f"session {child_id} already exists")

    for row in handoff_rows:
        messages.append_in(conn, child_id, {**row, "finish_reason": HANDOFF_FINISH}, now=now)
    sql = "SELECT id FROM messages WHERE session_id = ? AND active = 1 AND id > ?"
    args: list[object] = [parent_id, int(watermark)]
    if ceiling is not None:
        sql += " AND id <= ?"
        args.append(int(ceiling))
    tail = [int(r[0]) for r in conn.execute(sql + " ORDER BY id", args).fetchall()]
    if tail:
        messages.clone_rows_in(conn, child_id, tail)
    recount_in(conn, child_id)
    if parent.get("archived") and parent.get("auto_archived"):
        # Archived by the idle sweep, yet the conversation is alive again: lift it lineage-wide.
        conn.execute(
            "UPDATE sessions SET archived = 0, auto_archived = 0 WHERE id IN (?, ?)",
            (parent_id, child_id),
        )
    if title is not None:
        titles.set_title_in(conn, child_id, str(title), source=title_source)


def publish_continuation(
    store: StoreHandle,
    parent_id: str,
    child_fields: Mapping[str, Any],
    handoff_rows: Sequence[Mapping[str, Any]],
    watermark: int,
    holder: str,
    *,
    ceiling: int | None = None,
) -> str:
    """End ``parent_id`` and publish its continuation in one transaction; returns the child id.

    ``child_fields`` may name the child ``id`` (minted otherwise), a ``title`` (+ ``title_source``)
    and any session column to override what is carried from the parent. ``watermark`` is the
    parent's newest live id the handoff already covers; live rows above it (up to ``ceiling`` when
    given) are cloned after the handoff rows.
    """
    if not handoff_rows:
        raise ValueError("a continuation needs at least one handoff row")
    fields = dict(child_fields)
    child_id = str(fields.pop("id", None) or new_session_id(store.scope.clock))
    rows = [dict(r) for r in handoff_rows]

    def publish(conn: sqlite3.Connection) -> None:
        publish_continuation_in(
            conn,
            parent_id,
            child_id,
            fields,
            rows,
            watermark,
            holder,
            now=_now(store),
            ceiling=ceiling,
        )

    store.write(publish, budget=TRANSCRIPT)
    return child_id


# -- anti-thrash counters -----------------------------------------------------------------------

CounterName = Literal["fallback_streak", "ineffective_count", "overload_streak"]
_COUNTER_COLUMNS: dict[str, str] = {
    "fallback_streak": "compression_fallback_streak",
    "ineffective_count": "compression_ineffective_count",
    "overload_streak": "compression_overload_streak",
}
_SNAPSHOT_COLUMNS = (
    "compression_cooldown_until",
    "compression_cooldown_error",
    "compression_fallback_streak",
    "compression_ineffective_count",
    "compression_overload_streak",
)


@dataclass(frozen=True, slots=True)
class CompressionCounters:
    cooldown_until: float | None
    cooldown_error: str | None
    fallback_streak: int
    ineffective_count: int
    overload_streak: int


@dataclass(frozen=True, slots=True)
class CounterSnapshot:
    """The exact stored counter columns of a session (``exists`` False: no row)."""

    exists: bool
    values: tuple[Any, ...] = ()


def _read_counters(conn: sqlite3.Connection, session_id: str) -> tuple[Any, ...] | None:
    row = conn.execute(
        f"SELECT {', '.join(_SNAPSHOT_COLUMNS)} FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    return None if row is None else tuple(row)


def _as_count(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def counters(store: StoreHandle, session_id: str) -> CompressionCounters | None:
    with store.read() as conn:
        raw = _read_counters(conn, session_id)
    if raw is None:
        return None
    until, error, fallback, ineffective, overload = raw
    return CompressionCounters(
        cooldown_until=None if until is None else float(until),
        cooldown_error=None if error is None else str(error),
        fallback_streak=_as_count(fallback),
        ineffective_count=_as_count(ineffective),
        overload_streak=_as_count(overload),
    )


def active_cooldown(store: StoreHandle, session_id: str) -> CompressionCounters | None:
    """The counters when a cooldown deadline lies in the future, else None."""
    found = counters(store, session_id)
    if found is None or found.cooldown_until is None or found.cooldown_until <= _now(store):
        return None
    return found


def record_cooldown(
    store: StoreHandle, session_id: str, until: float, error: str | None = None
) -> bool:
    """Arm the cooldown. A later, shorter deadline never shortens a live longer one; the error
    text is always the newest."""

    def arm(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE sessions SET compression_cooldown_until = "
            "MAX(COALESCE(compression_cooldown_until, ?), ?), compression_cooldown_error = ? "
            "WHERE id = ?",
            (until, until, error, session_id),
        )
        return cur.rowcount == 1

    return store.write(arm)


def clear_cooldown(store: StoreHandle, session_id: str) -> bool:
    def disarm(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE sessions SET compression_cooldown_until = NULL, "
            "compression_cooldown_error = NULL WHERE id = ?",
            (session_id,),
        )
        return cur.rowcount == 1

    return store.write(disarm)


def set_counter(store: StoreHandle, session_id: str, name: CounterName, value: int) -> bool:
    column = _COUNTER_COLUMNS[name]
    clean = _as_count(value)

    def put(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(f"UPDATE sessions SET {column} = ? WHERE id = ?", (clean, session_id))
        return cur.rowcount == 1

    return store.write(put)


def bump_overload_streak(store: StoreHandle, session_id: str) -> int | None:
    """Add one to the overload streak atomically; the new value, or None without a session row."""

    def bump(conn: sqlite3.Connection) -> int | None:
        row = conn.execute(
            "UPDATE sessions SET compression_overload_streak = compression_overload_streak + 1 "
            "WHERE id = ? RETURNING compression_overload_streak",
            (session_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    return store.write(bump)


def snapshot(store: StoreHandle, session_id: str) -> CounterSnapshot:
    with store.read() as conn:
        raw = _read_counters(conn, session_id)
    return CounterSnapshot(False) if raw is None else CounterSnapshot(True, raw)


def restore(store: StoreHandle, session_id: str, snap: CounterSnapshot) -> None:
    """Put the counters back exactly as ``snap`` saw them, then check that they are.

    A session that disappeared meanwhile took its counters with it (logged, not an error). A
    restore that cannot be confirmed raises, so a cancellation is never reported clean when its
    compensation failed.
    """
    if not snap.exists:
        if snapshot(store, session_id).exists:
            raise StoreError(
                "store.error",
                f"cannot restore the compression counters of {session_id}: the snapshot had no "
                "session row but one exists now",
            )
        return
    assignments = ", ".join(f"{c} = ?" for c in _SNAPSHOT_COLUMNS)

    def put_back(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            f"UPDATE sessions SET {assignments} WHERE id = ?", (*snap.values, session_id)
        )
        return cur.rowcount == 1

    if not store.write(put_back):
        log.warning("compression counters of %s not restored: the session is gone", session_id)
        return
    now_seen = snapshot(store, session_id)
    if not now_seen.exists:
        log.warning("compression counters of %s: the session vanished after restore", session_id)
        return
    if now_seen.values != snap.values:
        raise StoreError(
            "store.error",
            f"restoring the compression counters of {session_id} did not stick: "
            f"wanted {snap.values!r}, found {now_seen.values!r}",
        )


# -- chains ------------------------------------------------------------------------------------


def tip(store: StoreHandle, session_id: str) -> str:
    """The newest segment of ``session_id``'s compression chain (``AmbiguousChain`` when two live
    continuations compete)."""
    with store.read() as conn:
        return lineage.tip_in(conn, session_id)


def root(store: StoreHandle, session_id: str) -> str:
    with store.read() as conn:
        return lineage.root_in(conn, session_id)


def lineage_ids(store: StoreHandle, session_id: str) -> list[str]:
    with store.read() as conn:
        return lineage.lineage_ids_in(conn, session_id)


def mark_orphans(store: StoreHandle, *, older_than: float = ORPHAN_AGE) -> int:
    """End open continuations that never made an API call and are older than ``older_than``
    seconds with ``orphaned_compression``; returns how many were marked."""

    def mark(conn: sqlite3.Connection) -> int:
        now = _now(store)
        candidates = conn.execute(
            "SELECT c.id, c.parent_session_id FROM sessions c "
            "JOIN sessions p ON p.id = c.parent_session_id "
            "WHERE c.ended_at IS NULL AND c.end_reason IS NULL AND c.api_call_count = 0 "
            "AND c.started_at < ? AND p.ended_at IS NOT NULL "
            f"AND p.end_reason IN ({', '.join('?' for _ in sessions.COMPRESSION_REASONS)}) "
            "AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id = c.id)",
            (now - older_than, *sorted(sessions.COMPRESSION_REASONS)),
        ).fetchall()
        marked = 0
        for found in candidates:
            child = sessions.get_in(conn, str(found["id"]))
            parent = sessions.get_in(conn, str(found["parent_session_id"]))
            if child is None or sessions.child_kind(child, parent) != "continuation":
                continue
            if sessions.end_in(conn, child["id"], sessions.END_ORPHANED_COMPRESSION, now=now):
                marked += 1
        return marked

    return store.write(mark)
