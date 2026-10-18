"""Seeding helpers for the slice-C store tests (FTS, search, browse): plain SQL on schema 1.

The message module of slice A is built in parallel, so rows go in with raw INSERTs; list/dict
content is stored the way that module stores it (``PARTS_PREFIX`` + JSON).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from indus.state.messages import PARTS_PREFIX
from indus.state.registry import StoreHandle

BASE_TIME = 1_767_258_000.0  # 2026-01-01 09:00 UTC, the fake clock's start


def parts(value: object) -> str:
    return PARTS_PREFIX + json.dumps(value)


def add_session(
    store: StoreHandle,
    session_id: str,
    source: str = "cli",
    *,
    started_at: float = BASE_TIME,
    model: str | None = "test-model",
) -> None:
    store.write(
        lambda conn: conn.execute(
            "INSERT INTO sessions(id, source, started_at, model) VALUES (?, ?, ?, ?)",
            (session_id, source, started_at, model),
        )
    )


def insert_message(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    content: str | None,
    *,
    timestamp: float = BASE_TIME,
    tool_name: str | None = None,
    tool_calls: str | None = None,
    active: int = 1,
    compacted: int = 0,
) -> int:
    cur = conn.execute(
        "INSERT INTO messages(session_id, role, content, timestamp, tool_name, tool_calls, "
        "active, compacted) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, role, content, timestamp, tool_name, tool_calls, active, compacted),
    )
    return int(cur.lastrowid or 0)


def add_message(
    store: StoreHandle, session_id: str, role: str, content: str | None, **kw: Any
) -> int:
    return store.write(lambda conn: insert_message(conn, session_id, role, content, **kw))


def names(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,)).fetchall()
    return {str(row[0]) for row in rows}


def strict_check(conn: sqlite3.Connection, table: str) -> None:
    """FTS5's strict check (index against its content view); raises on any mismatch."""
    conn.execute(f"INSERT INTO {table}({table}, rank) VALUES ('integrity-check', 1)")
