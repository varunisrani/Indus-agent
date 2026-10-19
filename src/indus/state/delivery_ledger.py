"""The delivery ledger table: final replies owed to a chat (P15-19).

One row per final reply. Its ``state`` walks ``pending`` (recorded before any send) →
``attempting`` (just before the platform call) → ``delivered`` / ``failed`` → ``abandoned``.
``owner_pid`` and ``owner_start`` name the process that last took the row (pid plus its start
fingerprint, so a reused pid is not mistaken for the owner); ``attempts`` counts redeliveries
(the first send is not one). ``pieces`` holds the formatted pieces of a partly sent reply that
did not go out, so a retry sends only those; ``error_kind`` and ``retry_at`` say why the last send
failed and when a retry is due.

This module stores and answers; the recovery policy (what to retry, when, with which label) is
``indus.gateway.delivery.ledger``. Claims and adoptions are compare-and-set on the previous owner,
so two processes sweeping the same table cannot both take one row.

Retention: delivered and abandoned rows older than ``RETENTION_SECONDS`` are deleted whenever a
row is recorded, and the table never keeps more than ``MAX_ROWS``: the oldest delivered rows go
first, then abandoned ones, then the oldest of the rest.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from indus.state.registry import StoreHandle
from indus.state.writer import ROUTINE

__all__ = [
    "MAX_ROWS",
    "OPEN_STATES",
    "RETENTION_SECONDS",
    "LedgerRow",
    "LedgerState",
    "Owner",
    "abandon",
    "abandon_older_than",
    "adopt",
    "claim",
    "get",
    "list_open",
    "mark_attempting",
    "mark_delivered",
    "mark_failed",
    "obligation_id",
    "prune",
    "record_pending",
    "rows_for_session_key",
]

type LedgerState = Literal["pending", "attempting", "delivered", "failed", "abandoned"]
type Owner = tuple[int | None, int | None]
"""``(pid, start fingerprint)`` of a process; ``(None, None)`` for no owner."""

OPEN_STATES: Final[tuple[LedgerState, ...]] = ("pending", "attempting", "failed")
MAX_ROWS: Final = 500
RETENTION_SECONDS: Final = 7 * 86400.0
_ERROR_CHARS: Final = 500
_COLUMNS: Final = (
    "id, session_key, platform, chat_id, thread_id, content, pieces_json, state, attempts, "
    "last_error, error_kind, retry_at, owner_pid, owner_start, created_at, updated_at"
)


@dataclass(frozen=True)
class LedgerRow:
    id: str
    session_key: str
    platform: str
    chat_id: str
    thread_id: str | None
    content: str
    pieces: tuple[str, ...]
    state: LedgerState
    attempts: int
    last_error: str | None
    error_kind: str | None
    retry_at: float | None
    owner_pid: int | None
    owner_start: int | None
    created_at: float
    updated_at: float

    @property
    def owner(self) -> Owner:
        return (self.owner_pid, self.owner_start)


def obligation_id(session_key: str, message_ref: str, content: str) -> str:
    """The row id: the same final of the same turn records once; distinct chats never collide."""
    blob = "\0".join((session_key, message_ref, content)).encode("utf-8", "replace")
    return hashlib.sha256(blob).hexdigest()[:32]


def _pieces(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw:
        return ()
    try:
        value = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(str(p) for p in cast(list[object], value) if isinstance(p, str))


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _float_or_none(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _str_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def _row(row: Sequence[Any]) -> LedgerRow:
    state = str(row[7])
    return LedgerRow(
        id=str(row[0]),
        session_key=str(row[1]),
        platform=str(row[2]),
        chat_id=str(row[3]),
        thread_id=_str_or_none(row[4]),
        content=str(row[5]),
        pieces=_pieces(row[6]),
        state=cast(LedgerState, state),
        attempts=int(row[8] or 0),
        last_error=_str_or_none(row[9]),
        error_kind=_str_or_none(row[10]),
        retry_at=_float_or_none(row[11]),
        owner_pid=_int_or_none(row[12]),
        owner_start=_int_or_none(row[13]),
        created_at=float(row[14]),
        updated_at=float(row[15]),
    )


def _now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


def _get_in(conn: sqlite3.Connection, row_id: str) -> LedgerRow | None:
    found = conn.execute(
        f"SELECT {_COLUMNS} FROM delivery_ledger WHERE id = ?", (row_id,)
    ).fetchone()
    return None if found is None else _row(tuple(found))


def _prune_in(conn: sqlite3.Connection, now: float) -> int:
    cur = conn.execute(
        "DELETE FROM delivery_ledger WHERE state IN ('delivered', 'abandoned') AND updated_at < ?",
        (now - RETENTION_SECONDS,),
    )
    removed = max(cur.rowcount, 0)
    total = int(conn.execute("SELECT COUNT(*) FROM delivery_ledger").fetchone()[0])
    if total > MAX_ROWS:
        cur = conn.execute(
            "DELETE FROM delivery_ledger WHERE id IN (SELECT id FROM delivery_ledger ORDER BY "
            "CASE state WHEN 'delivered' THEN 0 WHEN 'abandoned' THEN 1 ELSE 2 END, "
            "updated_at ASC, created_at ASC LIMIT ?)",
            (total - MAX_ROWS,),
        )
        removed += max(cur.rowcount, 0)
    return removed


# -- reads ----------------------------------------------------------------------------------------


def get(store: StoreHandle, row_id: str) -> LedgerRow | None:
    with store.read() as conn:
        return _get_in(conn, row_id)


def list_open(store: StoreHandle, *, platform: str | None = None) -> list[LedgerRow]:
    """Rows not yet delivered or abandoned, oldest first."""
    sql = (
        f"SELECT {_COLUMNS} FROM delivery_ledger WHERE state IN ('pending', 'attempting', 'failed')"
    )
    params: tuple[Any, ...] = ()
    if platform is not None:
        sql += " AND platform = ?"
        params = (platform,)
    with store.read() as conn:
        rows = conn.execute(sql + " ORDER BY created_at, id", params).fetchall()
    return [_row(tuple(r)) for r in rows]


def rows_for_session_key(store: StoreHandle, session_key: str) -> list[LedgerRow]:
    """Every row for one conversation, oldest first (boot recovery asks before resuming)."""
    with store.read() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM delivery_ledger WHERE session_key = ? ORDER BY created_at, id",
            (session_key,),
        ).fetchall()
    return [_row(tuple(r)) for r in rows]


# -- writes ---------------------------------------------------------------------------------------


def record_pending(
    store: StoreHandle,
    *,
    row_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    content: str,
    owner: Owner,
    thread_id: str | None = None,
) -> LedgerRow:
    """Record a final as owed (``pending``, no attempts); an existing row with the id is reset.

    Retention runs in the same write."""
    if not row_id or not session_key or not platform or not chat_id:
        raise ValueError("a ledger row needs an id, a session key, a platform and a chat id")
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> LedgerRow:
        conn.execute(
            f"INSERT OR REPLACE INTO delivery_ledger({_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', 0, NULL, NULL, NULL, ?, ?, ?, ?)",
            (
                row_id,
                session_key,
                platform,
                str(chat_id),
                thread_id or None,
                content,
                owner[0],
                owner[1],
                now,
                now,
            ),
        )
        _prune_in(conn, now)
        row = _get_in(conn, row_id)
        assert row is not None
        return row

    return store.write(apply, budget=ROUTINE)


def _set(store: StoreHandle, row_id: str, sql: str, params: tuple[Any, ...]) -> bool:
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            f"UPDATE delivery_ledger SET {sql}, updated_at = ? WHERE id = ?",
            (*params, now, row_id),
        )
        return cur.rowcount > 0

    return store.write(apply, budget=ROUTINE)


def mark_attempting(store: StoreHandle, row_id: str) -> bool:
    return _set(store, row_id, "state = 'attempting'", ())


def mark_delivered(store: StoreHandle, row_id: str) -> bool:
    return _set(
        store,
        row_id,
        "state = 'delivered', pieces_json = NULL, last_error = NULL, error_kind = NULL, "
        "retry_at = NULL",
        (),
    )


def mark_failed(
    store: StoreHandle,
    row_id: str,
    *,
    error: str,
    kind: str,
    retry_at: float | None = None,
    pieces: Sequence[str] = (),
) -> bool:
    """A definitive refusal: the reason, its class, when a retry is due, the pieces still owed."""
    stored = json.dumps(list(pieces), ensure_ascii=False) if pieces else None
    return _set(
        store,
        row_id,
        "state = 'failed', last_error = ?, error_kind = ?, retry_at = ?, "
        "pieces_json = COALESCE(?, pieces_json)",
        (error[:_ERROR_CHARS], kind, retry_at, stored),
    )


def abandon(store: StoreHandle, row_id: str, error: str | None = None) -> bool:
    return _set(
        store,
        row_id,
        "state = 'abandoned', retry_at = NULL, last_error = COALESCE(?, last_error)",
        (None if error is None else error[:_ERROR_CHARS],),
    )


def claim(
    store: StoreHandle,
    row_id: str,
    *,
    expect_owner: Owner,
    owner: Owner,
    states: Sequence[LedgerState] = OPEN_STATES,
) -> LedgerRow | None:
    """Take a row for a redelivery: ``attempting``, one more attempt, owned by ``owner``.

    Only while it is still in one of ``states`` and owned by ``expect_owner``; None when another
    process got there first. Returns the row as it was before the claim."""
    now = _now(store)
    wanted = tuple(states)
    marks = ", ".join("?" for _ in wanted)

    def apply(conn: sqlite3.Connection) -> LedgerRow | None:
        before = _get_in(conn, row_id)
        if before is None:
            return None
        cur = conn.execute(
            "UPDATE delivery_ledger SET state = 'attempting', attempts = attempts + 1, "
            "owner_pid = ?, owner_start = ?, retry_at = NULL, updated_at = ? "
            f"WHERE id = ? AND state IN ({marks}) AND owner_pid IS ? AND owner_start IS ?",
            (owner[0], owner[1], now, row_id, *wanted, expect_owner[0], expect_owner[1]),
        )
        return before if cur.rowcount > 0 else None

    return store.write(apply, budget=ROUTINE)


def adopt(store: StoreHandle, row_id: str, *, expect_owner: Owner, owner: Owner) -> bool:
    """Re-stamp a dead owner's row to ``owner`` without spending an attempt or changing state."""

    def apply(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE delivery_ledger SET owner_pid = ?, owner_start = ? "
            "WHERE id = ? AND owner_pid IS ? AND owner_start IS ?",
            (owner[0], owner[1], row_id, expect_owner[0], expect_owner[1]),
        )
        return cur.rowcount > 0

    return store.write(apply, budget=ROUTINE)


def abandon_older_than(store: StoreHandle, cutoff: float) -> int:
    """Abandon open rows created before ``cutoff``; returns how many."""
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "UPDATE delivery_ledger SET state = 'abandoned', retry_at = NULL, updated_at = ? "
            "WHERE state IN ('pending', 'attempting', 'failed') AND created_at < ?",
            (now, cutoff),
        )
        return max(cur.rowcount, 0)

    return store.write(apply, budget=ROUTINE)


def prune(store: StoreHandle) -> int:
    """Apply retention now; returns how many rows were deleted."""
    now = _now(store)
    return store.write(lambda conn: _prune_in(conn, now), budget=ROUTINE)
