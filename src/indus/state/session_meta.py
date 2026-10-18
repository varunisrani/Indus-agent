"""Per-session metadata: small namespaced JSON values attached to one session (P02-23).

Features that keep a little state per conversation — goals, loops, heartbeats, later surfaces —
store it here instead of growing the ``sessions`` table. A row is ``(session_id, namespace,
key) -> value`` plus ``updated_at`` and an optional ``due_at`` next-run time; ``due()`` reads the
``(namespace, due_at)`` index so a poller gets its due rows in time order without scanning JSON.

Values are JSON with finite numbers only and at most ``MAX_VALUE_BYTES`` once encoded
(``MetaValueTooLarge`` past that). ``patch`` applies a JSON merge-patch (``None`` removes a
field, nested objects merge) inside one write transaction, so two writers changing different
fields of one value both keep their change.

Rows belong to their session: the foreign key cascades them away on delete (P02-18 also deletes
them explicitly), export/import carries them (P02-17), and a branch (P02-11) copies only the
namespaces registered with ``register_branch_safe``.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from indus.foundation.errors import StoreError
from indus.state.registry import StoreHandle
from indus.state.writer import ROUTINE

__all__ = [
    "KEEP",
    "MAX_NAME_CHARS",
    "MAX_VALUE_BYTES",
    "DueEntry",
    "MetaValueTooLarge",
    "branch_safe_namespaces",
    "copy_branch_safe_in",
    "delete",
    "delete_in",
    "due",
    "encode_value",
    "export_rows_in",
    "get",
    "import_rows_in",
    "list_entries",
    "patch",
    "patch_in",
    "prepare_import_row",
    "put",
    "put_in",
    "register_branch_safe",
]

MAX_VALUE_BYTES: Final = 64 * 1024
MAX_NAME_CHARS: Final = 200


class MetaValueTooLarge(StoreError):
    """A metadata value is over the 64 KiB encoded limit; nothing was written (P02-23)."""

    default_code = "store.meta_too_large"


class _Keep:
    """Marker: leave the stored ``due_at`` as it is."""

    def __repr__(self) -> str:
        return "KEEP"


KEEP: Final = _Keep()


@dataclass(frozen=True)
class DueEntry:
    session_id: str
    key: str
    due_at: float
    value: Any


# -- branch-safe namespaces -----------------------------------------------------------------------

_branch_safe: set[str] = set()
_branch_safe_lock = threading.Lock()


def register_branch_safe(namespace: str) -> None:
    """Rows of ``namespace`` are copied when a session is branched."""
    _check_name(namespace, "namespace")
    with _branch_safe_lock:
        _branch_safe.add(namespace)


def branch_safe_namespaces() -> frozenset[str]:
    with _branch_safe_lock:
        return frozenset(_branch_safe)


# -- value handling -------------------------------------------------------------------------------


def _check_name(text: object, what: str) -> str:
    if not isinstance(text, str) or not text or len(text) > MAX_NAME_CHARS:
        raise ValueError(f"metadata {what} must be a non-empty string of at most 200 characters")
    return text


def _check_due(due_at: object) -> float | None:
    if due_at is None:
        return None
    if isinstance(due_at, bool) or not isinstance(due_at, int | float):
        raise ValueError("due_at must be a number of epoch seconds or None")
    number = float(due_at)
    if not math.isfinite(number):
        raise ValueError("due_at must be finite")
    return number


def encode_value(value: object) -> str:
    """The stored JSON text of ``value``; refuses non-JSON values and values over the limit."""
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"metadata values must be plain JSON with finite numbers: {exc}") from exc
    size = len(text.encode("utf-8", errors="surrogatepass"))
    if size > MAX_VALUE_BYTES:
        raise MetaValueTooLarge(
            f"metadata value is {size} bytes; the limit is {MAX_VALUE_BYTES} bytes",
            hint="store large data in a file and keep only its path here",
        )
    return text


def _decode(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _merge(target: object, change: object) -> object:
    """JSON merge-patch: objects merge key by key, ``None`` removes, anything else replaces."""
    if not isinstance(change, Mapping):
        return change
    patch_map = cast(Mapping[str, object], change)
    base: dict[str, object] = (
        dict(cast(Mapping[str, object], target)) if isinstance(target, Mapping) else {}
    )
    for name, part in patch_map.items():
        if part is None:
            base.pop(name, None)
        else:
            base[name] = _merge(base.get(name), part)
    return base


def _now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


# -- in-transaction forms -------------------------------------------------------------------------


def put_in(
    conn: sqlite3.Connection,
    session_id: str,
    namespace: str,
    key: str,
    value: object,
    *,
    now: float,
    due_at: float | None = None,
) -> None:
    _check_name(namespace, "namespace")
    _check_name(key, "key")
    text = encode_value(value)
    when = _check_due(due_at)
    conn.execute(
        "INSERT INTO session_meta(session_id, namespace, key, value_json, updated_at, due_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, namespace, key) DO UPDATE SET "
        "value_json = excluded.value_json, updated_at = excluded.updated_at, "
        "due_at = excluded.due_at",
        (session_id, namespace, key, text, now, when),
    )


def patch_in(
    conn: sqlite3.Connection,
    session_id: str,
    namespace: str,
    key: str,
    merge: Mapping[str, object],
    *,
    now: float,
    due_at: float | None | _Keep = KEEP,
) -> Any:
    if not isinstance(merge, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("a metadata patch must be a mapping")
    _check_name(namespace, "namespace")
    _check_name(key, "key")
    row = conn.execute(
        "SELECT value_json, due_at FROM session_meta "
        "WHERE session_id = ? AND namespace = ? AND key = ?",
        (session_id, namespace, key),
    ).fetchone()
    current = _decode(row[0]) if row is not None else None
    merged = _merge(current, merge)
    keep_due: float | None = row[1] if row is not None else None
    when = keep_due if isinstance(due_at, _Keep) else due_at
    put_in(conn, session_id, namespace, key, merged, now=now, due_at=when)
    return merged


def delete_in(conn: sqlite3.Connection, session_id: str, namespace: str, key: str) -> bool:
    cur = conn.execute(
        "DELETE FROM session_meta WHERE session_id = ? AND namespace = ? AND key = ?",
        (session_id, namespace, key),
    )
    return cur.rowcount > 0


def copy_branch_safe_in(
    conn: sqlite3.Connection, from_session_id: str, to_session_id: str, *, now: float
) -> int:
    """Copy the rows of every branch-safe namespace from one session to another."""
    names = sorted(branch_safe_namespaces())
    if not names:
        return 0
    marks = ",".join("?" for _ in names)
    cur = conn.execute(
        "INSERT INTO session_meta(session_id, namespace, key, value_json, updated_at, due_at) "
        f"SELECT ?, namespace, key, value_json, ?, due_at FROM session_meta "
        f"WHERE session_id = ? AND namespace IN ({marks}) "
        "ON CONFLICT(session_id, namespace, key) DO UPDATE SET "
        "value_json = excluded.value_json, updated_at = excluded.updated_at, "
        "due_at = excluded.due_at",
        (to_session_id, now, from_session_id, *names),
    )
    return max(cur.rowcount, 0)


def export_rows_in(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    """Every metadata row of a session as plain dicts (value decoded), in a stable order."""
    rows = conn.execute(
        "SELECT namespace, key, value_json, updated_at, due_at FROM session_meta "
        "WHERE session_id = ? ORDER BY namespace, key",
        (session_id,),
    ).fetchall()
    return [
        {
            "namespace": r[0],
            "key": r[1],
            "value": _decode(r[2]),
            "updated_at": r[3],
            "due_at": r[4],
        }
        for r in rows
    ]


def prepare_import_row(
    row: Mapping[str, Any], now: float
) -> tuple[str, str, str, float, float | None]:
    """Check one exported row; raises ``ValueError`` / ``MetaValueTooLarge`` when unusable."""
    namespace = _check_name(row.get("namespace"), "namespace")
    key = _check_name(row.get("key"), "key")
    text = encode_value(row.get("value"))
    stamp = row.get("updated_at")
    updated = now
    if isinstance(stamp, int | float) and not isinstance(stamp, bool) and math.isfinite(stamp):
        updated = float(stamp)
    return namespace, key, text, updated, _check_due(row.get("due_at"))


def import_rows_in(
    conn: sqlite3.Connection, session_id: str, rows: Sequence[Mapping[str, Any]], *, now: float
) -> int:
    """Insert exported rows for ``session_id``; every row is checked before any is written."""
    prepared = [prepare_import_row(row, now) for row in rows]
    for namespace, key, text, updated, due_at in prepared:
        conn.execute(
            "INSERT INTO session_meta(session_id, namespace, key, value_json, updated_at, due_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, namespace, key) DO UPDATE SET "
            "value_json = excluded.value_json, updated_at = excluded.updated_at, "
            "due_at = excluded.due_at",
            (session_id, namespace, key, text, updated, due_at),
        )
    return len(prepared)


# -- public operations ----------------------------------------------------------------------------


def get(store: StoreHandle, session_id: str, namespace: str, key: str, default: Any = None) -> Any:
    with store.read() as conn:
        row = conn.execute(
            "SELECT value_json FROM session_meta "
            "WHERE session_id = ? AND namespace = ? AND key = ?",
            (session_id, namespace, key),
        ).fetchone()
    return default if row is None else _decode(row[0])


def put(
    store: StoreHandle,
    session_id: str,
    namespace: str,
    key: str,
    value: object,
    *,
    due_at: float | None = None,
) -> None:
    encode_value(value)  # refuse before taking the write lock
    now = _now(store)
    store.write(
        lambda c: put_in(c, session_id, namespace, key, value, now=now, due_at=due_at),
        budget=ROUTINE,
    )


def patch(
    store: StoreHandle,
    session_id: str,
    namespace: str,
    key: str,
    merge: Mapping[str, object],
    *,
    due_at: float | None | _Keep = KEEP,
) -> Any:
    now = _now(store)
    return store.write(
        lambda c: patch_in(c, session_id, namespace, key, merge, now=now, due_at=due_at),
        budget=ROUTINE,
    )


def delete(store: StoreHandle, session_id: str, namespace: str, key: str) -> bool:
    return store.write(lambda c: delete_in(c, session_id, namespace, key), budget=ROUTINE)


def list_entries(store: StoreHandle, session_id: str, namespace: str) -> dict[str, Any]:
    with store.read() as conn:
        rows = conn.execute(
            "SELECT key, value_json FROM session_meta "
            "WHERE session_id = ? AND namespace = ? ORDER BY key",
            (session_id, namespace),
        ).fetchall()
    return {str(r[0]): _decode(r[1]) for r in rows}


def due(store: StoreHandle, namespace: str, before: float, *, limit: int = 100) -> list[DueEntry]:
    """Rows of ``namespace`` whose ``due_at`` is at or before ``before``, earliest first."""
    with store.read() as conn:
        rows = conn.execute(
            "SELECT session_id, key, due_at, value_json FROM session_meta "
            "WHERE namespace = ? AND due_at IS NOT NULL AND due_at <= ? "
            "ORDER BY due_at, session_id, key LIMIT ?",
            (namespace, float(before), max(int(limit), 0)),
        ).fetchall()
    return [DueEntry(str(r[0]), str(r[1]), float(r[2]), _decode(r[3])) for r in rows]
