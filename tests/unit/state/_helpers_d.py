"""Plain-SQL seeding for the slice-D store tests (deletion, maintenance, portability, metadata).

Rows go in through SQL against schema 1 so these tests do not depend on the session and message
repositories; content is encoded with the shared message codec so exports decode it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from indus.state import messages
from indus.state.registry import StoreHandle
from tests.fakes.clock import FakeClock

DAY = 86_400.0


def now_of(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


def advance(store: StoreHandle, seconds: float) -> None:
    """Move the store's fake clock forward."""
    clock = store.scope.clock
    assert isinstance(clock, FakeClock)
    clock.advance(seconds)


def add_session(
    store: StoreHandle,
    session_id: str,
    *,
    source: str = "cli",
    started_at: float | None = None,
    ended_at: float | None = None,
    end_reason: str | None = None,
    parent: str | None = None,
    marker: str | None = None,
    prompt: str | None = None,
    **columns: Any,
) -> None:
    """One session row; ``marker`` (``_delegate_from`` …) names ``parent`` in ``model_config``."""
    start = now_of(store) if started_at is None else started_at
    if ended_at is None and end_reason is not None:
        ended_at = start
    config = columns.pop("model_config", None)
    if marker is not None:
        config = {**(config or {}), marker: parent}
    row: dict[str, Any] = {
        "id": session_id,
        "source": source,
        "started_at": start,
        "ended_at": ended_at,
        "end_reason": end_reason,
        "parent_session_id": parent,
        "model_config": None if config is None else json.dumps(config),
        **columns,
    }

    def run(conn: sqlite3.Connection) -> None:
        if prompt is not None:
            key = hashlib.sha256(prompt.encode()).hexdigest()
            conn.execute("INSERT OR IGNORE INTO system_prompts VALUES (?, ?)", (key, prompt))
            row["system_prompt_hash"] = key
        names = ", ".join(row)
        marks = ", ".join("?" * len(row))
        conn.execute(f"INSERT INTO sessions ({names}) VALUES ({marks})", tuple(row.values()))

    store.write(run)


def add_message(
    store: StoreHandle,
    session_id: str,
    content: object = "hello",
    *,
    role: str = "user",
    timestamp: float | None = None,
    active: int = 1,
    **columns: Any,
) -> int:
    for name in ("tool_calls", "reasoning_details", "tool_call_uids"):
        if name in columns and not isinstance(columns[name], str | None):
            columns[name] = json.dumps(columns[name])
    row: dict[str, Any] = {
        "session_id": session_id,
        "role": role,
        "content": messages.encode_content(content),
        "timestamp": now_of(store) if timestamp is None else timestamp,
        "active": active,
        **columns,
    }

    def run(conn: sqlite3.Connection) -> int:
        names = ", ".join(row)
        marks = ", ".join("?" * len(row))
        cur = conn.execute(f"INSERT INTO messages ({names}) VALUES ({marks})", tuple(row.values()))
        return int(cur.lastrowid or 0)

    return store.write(run)


def add_guard(
    store: StoreHandle, session_id: str, *, table: str, holder: str, expires_at: float
) -> None:
    column = "conversation_id" if table == "turn_leases" else "session_id"
    now = now_of(store)
    store.write(
        lambda c: c.execute(
            f"INSERT INTO {table} ({column}, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, holder, now, expires_at),
        )
    )


def session_row(store: StoreHandle, session_id: str) -> dict[str, Any] | None:
    with store.read() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return None if row is None else dict(row)


def count(store: StoreHandle, sql: str, *params: object) -> int:
    with store.read() as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def meta_value(store: StoreHandle, key: str) -> str | None:
    with store.read() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


class Config:
    """A config view over flat dotted keys (what ``Scope.config.get`` needs)."""

    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get(self, path: str, /) -> object:
        return self.values.get(path)
