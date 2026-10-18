"""Seeding helpers for the slice-A store tests (sessions, lineage, messages, titles)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from indus.state import sessions
from indus.state.registry import StoreHandle


def seed(store: StoreHandle, session_id: str, source: str = "cli", **fields: Any) -> str:
    sessions.create(store, session_id, source, **fields)
    return session_id


def rotate(store: StoreHandle, parent_id: str, child_id: str, **fields: Any) -> str:
    """End ``parent_id`` with ``compression`` and create its continuation ``child_id``."""
    sessions.end(store, parent_id, sessions.END_COMPRESSION)
    return seed(store, child_id, parent_session_id=parent_id, **fields)


def chain(store: StoreHandle, ids: Sequence[str]) -> list[str]:
    """A compression lineage over ``ids`` (root first)."""
    seed(store, ids[0])
    for parent, child in zip(ids, ids[1:], strict=False):
        rotate(store, parent, child)
    return list(ids)


def sql(store: StoreHandle, statement: str, params: Sequence[Any] = ()) -> None:
    def run(conn: sqlite3.Connection) -> None:
        conn.execute(statement, tuple(params))

    store.write(run)


def column(store: StoreHandle, session_id: str, name: str) -> Any:
    with store.read() as conn:
        row = conn.execute(f"SELECT {name} FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return None if row is None else row[0]
