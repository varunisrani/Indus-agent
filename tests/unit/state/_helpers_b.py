"""Plain-SQL seeding for the slice-B store tests (listing, rewind, compression, usage).

Rows are written straight against schema 1 so these tests do not depend on how other modules
create sessions or append messages.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from indus.state.registry import StoreHandle

PARTS_PREFIX = "\x00indus.parts:"


def now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


def add_session(store: StoreHandle, sid: str, source: str = "cli", **cols: Any) -> str:
    values: dict[str, Any] = {"id": sid, "source": source, "started_at": now(store), **cols}
    if isinstance(values.get("model_config"), dict):
        values["model_config"] = json.dumps(values["model_config"])
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(f"INSERT INTO sessions ({names}) VALUES ({marks})", tuple(values.values()))

    store.write(insert)
    return sid


def add_message(
    store: StoreHandle, sid: str, role: str, content: object = None, **cols: Any
) -> int:
    stored = content
    if content is not None and not isinstance(content, str):
        stored = PARTS_PREFIX + json.dumps(content)
    values: dict[str, Any] = {
        "session_id": sid,
        "role": role,
        "content": stored,
        "timestamp": now(store),
        **cols,
    }
    if isinstance(values.get("tool_calls"), list):
        values["tool_calls"] = json.dumps(values["tool_calls"])
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)

    def insert(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            f"INSERT INTO messages ({names}) VALUES ({marks})", tuple(values.values())
        )
        conn.execute("UPDATE sessions SET message_count = message_count + 1 WHERE id = ?", (sid,))
        return int(cur.lastrowid or 0)

    return store.write(insert)


def update_session(store: StoreHandle, sid: str, **cols: Any) -> None:
    sets = ", ".join(f"{name} = ?" for name in cols)
    store.write(
        lambda conn: conn.execute(f"UPDATE sessions SET {sets} WHERE id = ?", (*cols.values(), sid))
    )


def session(store: StoreHandle, sid: str) -> dict[str, Any] | None:
    with store.read() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    return None if row is None else dict(row)


def message_rows(
    store: StoreHandle, sid: str, *, include_inactive: bool = True
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM messages WHERE session_id = ?"
    if not include_inactive:
        sql += " AND active = 1"
    with store.read() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", (sid,)).fetchall()]


def live_contents(store: StoreHandle, sid: str) -> list[Any]:
    return [r["content"] for r in message_rows(store, sid, include_inactive=False)]


def seed_turns(store: StoreHandle, sid: str, turns: int = 3) -> None:
    for i in range(1, turns + 1):
        add_message(store, sid, "user", f"q{i}")
        add_message(store, sid, "assistant", f"a{i}")
