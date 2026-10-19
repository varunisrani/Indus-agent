"""The gateway routing index: session key → current session and its routing state (P15-07).

One row per session key (built only by ``indus.gateway.core.session_keys``). A row holds the
session id the key points at now, the origin source (JSON) that replies and restores use, a
display name, a session model override (model / provider / base URL only, never credentials),
``resume_pending`` with its reason, the durable active-turn marker, and the previous session id
after a rotation.

Conversations never reset on idle or daily boundaries: nothing here looks at the time since the
last message, and only ``rotate`` (``/new``) points a key at a new session.

The active-turn marker is compare-and-clear: ``end_turn(token)`` clears only the marker that
``begin_turn(token)`` set, so a displaced turn can never clear its successor's.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from indus.state.registry import StoreHandle
from indus.state.writer import ROUTINE

__all__ = [
    "MODEL_OVERRIDE_KEYS",
    "RouteEntry",
    "begin_turn",
    "clear_resume_pending",
    "delete",
    "end_turn",
    "get",
    "get_or_create",
    "list_routes",
    "mark_resume_pending",
    "rotate",
    "routes_for_session",
    "sanitize_model_override",
    "set_display_name",
    "set_model_override",
]

MODEL_OVERRIDE_KEYS: Final = ("model", "provider", "base_url")
_COLUMNS: Final = (
    "session_key, session_id, platform, chat_type, origin_json, display_name, "
    "model_override_json, resume_pending, resume_reason, active_turn_token, "
    "active_turn_started_at, prev_session_id, created_at, updated_at"
)


@dataclass(frozen=True)
class RouteEntry:
    session_key: str
    session_id: str
    platform: str
    chat_type: str
    origin: dict[str, Any]
    display_name: str | None
    model_override: dict[str, str] | None
    resume_pending: bool
    resume_reason: str | None
    active_turn_token: str | None
    active_turn_started_at: float | None
    prev_session_id: str | None
    created_at: float
    updated_at: float


def _load_json_object(text: object) -> dict[str, Any]:
    if not isinstance(text, str) or not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast(dict[object, Any], value).items()}


def _row(row: tuple[Any, ...]) -> RouteEntry:
    override = _load_json_object(row[6])
    return RouteEntry(
        session_key=str(row[0]),
        session_id=str(row[1]),
        platform=str(row[2]),
        chat_type=str(row[3]),
        origin=_load_json_object(row[4]),
        display_name=None if row[5] is None else str(row[5]),
        model_override={k: str(v) for k, v in override.items()} if override else None,
        resume_pending=bool(row[7]),
        resume_reason=None if row[8] is None else str(row[8]),
        active_turn_token=None if row[9] is None else str(row[9]),
        active_turn_started_at=None if row[10] is None else float(row[10]),
        prev_session_id=None if row[11] is None else str(row[11]),
        created_at=float(row[12]),
        updated_at=float(row[13]),
    )


def _now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


def _encode(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _check_key(session_key: str) -> str:
    if not session_key:
        raise ValueError("a route needs a non-empty session key")
    return session_key


def sanitize_model_override(override: Mapping[str, object] | None) -> dict[str, str] | None:
    """Keep only model / provider / base URL; credentials are re-resolved, never stored."""
    if not override:
        return None
    kept = {
        key: str(value).strip()
        for key, value in override.items()
        if key in MODEL_OVERRIDE_KEYS and value is not None and str(value).strip()
    }
    return kept or None


def _get_in(conn: sqlite3.Connection, session_key: str) -> RouteEntry | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM gateway_routes WHERE session_key = ?", (session_key,)
    ).fetchone()
    return None if row is None else _row(tuple(row))


# -- reads ----------------------------------------------------------------------------------------


def get(store: StoreHandle, session_key: str) -> RouteEntry | None:
    with store.read() as conn:
        return _get_in(conn, session_key)


def list_routes(store: StoreHandle, *, platform: str | None = None) -> list[RouteEntry]:
    with store.read() as conn:
        if platform is None:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM gateway_routes ORDER BY session_key"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM gateway_routes WHERE platform = ? ORDER BY session_key",
                (platform,),
            ).fetchall()
    return [_row(tuple(r)) for r in rows]


def routes_for_session(store: StoreHandle, session_id: str) -> list[RouteEntry]:
    """Every key currently pointing at ``session_id`` (several keys may share one transcript)."""
    with store.read() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM gateway_routes WHERE session_id = ? ORDER BY session_key",
            (session_id,),
        ).fetchall()
    return [_row(tuple(r)) for r in rows]


# -- writes ---------------------------------------------------------------------------------------


def get_or_create(
    store: StoreHandle,
    session_key: str,
    *,
    new_session_id: Callable[[], str],
    platform: str,
    chat_type: str,
    origin: Mapping[str, Any],
    display_name: str | None = None,
) -> tuple[RouteEntry, bool]:
    """The route for ``session_key``, created with a fresh session id when missing.

    Returns ``(entry, created)``. An existing route keeps its session id however long it was idle;
    only its origin (the latest sender's details) and ``updated_at`` are refreshed.
    """
    _check_key(session_key)
    now = _now(store)
    origin_text = _encode(origin)

    def apply(conn: sqlite3.Connection) -> tuple[RouteEntry, bool]:
        current = _get_in(conn, session_key)
        if current is not None:
            conn.execute(
                "UPDATE gateway_routes SET origin_json = ?, updated_at = ?, "
                "display_name = COALESCE(?, display_name) WHERE session_key = ?",
                (origin_text, now, display_name, session_key),
            )
            refreshed = _get_in(conn, session_key)
            assert refreshed is not None
            return refreshed, False
        session_id = new_session_id()
        if not session_id:
            raise ValueError("new_session_id returned an empty id")
        conn.execute(
            "INSERT INTO gateway_routes(session_key, session_id, platform, chat_type, "
            "origin_json, display_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_key, session_id, platform, chat_type, origin_text, display_name, now, now),
        )
        created = _get_in(conn, session_key)
        assert created is not None
        return created, True

    return store.write(apply, budget=ROUTINE)


def rotate(store: StoreHandle, session_key: str, new_session_id: str) -> RouteEntry | None:
    """Point the key at a new session (``/new``), remembering the previous one.

    Resume and active-turn state belong to the old session and are cleared. None when the key has
    no route.
    """
    _check_key(session_key)
    if not new_session_id:
        raise ValueError("rotate needs a non-empty session id")
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> RouteEntry | None:
        current = _get_in(conn, session_key)
        if current is None:
            return None
        conn.execute(
            "UPDATE gateway_routes SET prev_session_id = session_id, session_id = ?, "
            "resume_pending = 0, resume_reason = NULL, active_turn_token = NULL, "
            "active_turn_started_at = NULL, updated_at = ? WHERE session_key = ?",
            (new_session_id, now, session_key),
        )
        return _get_in(conn, session_key)

    return store.write(apply, budget=ROUTINE)


def _update(store: StoreHandle, session_key: str, sql: str, params: tuple[Any, ...]) -> bool:
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            f"UPDATE gateway_routes SET {sql}, updated_at = ? WHERE session_key = ?",
            (*params, now, session_key),
        )
        return cur.rowcount > 0

    return store.write(apply, budget=ROUTINE)


def set_display_name(store: StoreHandle, session_key: str, name: str | None) -> bool:
    return _update(store, session_key, "display_name = ?", (name,))


def set_model_override(
    store: StoreHandle, session_key: str, override: Mapping[str, object] | None
) -> bool:
    """Persist a session ``/model`` choice across restarts (None clears it)."""
    kept = sanitize_model_override(override)
    text = None if kept is None else _encode(kept)
    return _update(store, session_key, "model_override_json = ?", (text,))


def mark_resume_pending(store: StoreHandle, session_key: str, reason: str) -> bool:
    return _update(store, session_key, "resume_pending = 1, resume_reason = ?", (reason,))


def clear_resume_pending(store: StoreHandle, session_key: str) -> bool:
    return _update(store, session_key, "resume_pending = 0, resume_reason = NULL", ())


def begin_turn(store: StoreHandle, session_key: str, token: str) -> bool:
    """Set the durable active-turn marker (left behind by a kill; boot recovery reads it)."""
    if not token:
        raise ValueError("an active-turn marker needs a token")
    now = _now(store)
    return _update(
        store, session_key, "active_turn_token = ?, active_turn_started_at = ?", (token, now)
    )


def end_turn(store: StoreHandle, session_key: str, token: str) -> bool:
    """Clear the marker only when it is still ``token``'s."""
    now = _now(store)

    def apply(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE gateway_routes SET active_turn_token = NULL, active_turn_started_at = NULL, "
            "updated_at = ? WHERE session_key = ? AND active_turn_token = ?",
            (now, session_key, token),
        )
        return cur.rowcount > 0

    return store.write(apply, budget=ROUTINE)


def delete(store: StoreHandle, session_key: str) -> bool:
    def apply(conn: sqlite3.Connection) -> bool:
        cur = conn.execute("DELETE FROM gateway_routes WHERE session_key = ?", (session_key,))
        return cur.rowcount > 0

    return store.write(apply, budget=ROUTINE)
