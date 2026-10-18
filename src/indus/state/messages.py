"""The transcript: append, batch append, read, replace, clone, and the resume/export size guards.

Order is the row id (AUTOINCREMENT, never reused), never the timestamp: clocks step backwards and
imports carry old times (R03.14). An append runs under the long ``TRANSCRIPT`` budget because a
lost transcript write aborts the turn, and bumps the session's counters in the same transaction.

Values are made safe on the way in:

- list/dict content is stored as ``PARTS_PREFIX`` + JSON. A plain string that already starts with
  the prefix is stored as prefix + a JSON *string*, so it reads back as the same plain string and
  an import can never smuggle structure in (R03.19).
- lone UTF-16 surrogates make the sqlite3 module refuse the whole write; they become U+FFFD in
  content, reasoning, ``api_content`` and tool names (R03.19).
- a timestamp that is not a number or falls outside [2020-01-01, now + 1 day] becomes now.

Every row carries a durable ``message_uid`` (32 hex): kept when the caller supplies one, minted
otherwise, and carried by every copy. An assistant row's tool calls get ``tool_call_uids``, a map
from the provider's call id to a uid.

Ordinary appends never look at the compression lock (R03.10): a rotation clones whatever arrived
after its watermark, so blocking appends would only kill turns. A session already ended by
compression refuses appends — its conversation continues in the child.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, cast

from indus.foundation.errors import ResumeTooLarge, StoreError, StoreReplaced
from indus.foundation.ids import new_message_uid
from indus.foundation.scope import Scope
from indus.state import guards, lineage
from indus.state.registry import StoreHandle
from indus.state.sessions import END_COMPRESSION, chunks
from indus.state.writer import TRANSCRIPT

__all__ = [
    "BATCH_ROWS",
    "DEFAULT_RESUME_LIMIT",
    "EARLIEST_TIMESTAMP",
    "EXPORT_NUMERIC",
    "PARTS_PREFIX",
    "RESUME_NUMERIC",
    "CompressionClosed",
    "TranscriptMoved",
    "append",
    "append_batch",
    "append_batch_in",
    "append_in",
    "as_conversation",
    "check_export_size",
    "check_export_size_in",
    "check_resume_size",
    "check_resume_size_in",
    "clone_rows_in",
    "decode_content",
    "encode_content",
    "export_limit",
    "get",
    "get_in",
    "live_ids_in",
    "replace",
    "replace_in",
    "resume_limit",
    "scrub_surrogates",
]

log = logging.getLogger(__name__)

PARTS_PREFIX = "\x00indus.parts:"
BATCH_ROWS = 500
EARLIEST_TIMESTAMP = 1_577_836_800.0  # 2020-01-01T00:00:00Z
_FUTURE_SLACK = 86_400.0
DEFAULT_RESUME_LIMIT = 20_000
RESUME_NUMERIC = 4130
EXPORT_NUMERIC = 4131

_SURROGATE = re.compile("[\ud800-\udfff]")

# Columns an insert writes, in statement order (``session_id`` goes first).
_WRITE_COLUMNS = (
    "role",
    "content",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "timestamp",
    "token_count",
    "finish_reason",
    "reasoning",
    "reasoning_details",
    "platform_message_id",
    "active",
    "compacted",
    "api_content",
    "message_uid",
    "tool_call_uids",
)
# Columns a clone does not copy: identity and liveness belong to the new row.
_CLONE_SKIP = frozenset({"id", "session_id", "active", "compacted"})


class CompressionClosed(StoreError):
    """The session was rotated into a continuation; write to the child instead."""

    default_code = "store.compression_closed"


class TranscriptMoved(StoreError):
    """A pinned replace found a live id set other than the one the caller computed from."""

    default_code = "store.transcript_moved"


# -- value codecs ---------------------------------------------------------------------------------


def scrub_surrogates(text: str) -> str:
    """Replace lone UTF-16 surrogates with U+FFFD (sqlite3 cannot bind them)."""
    if text.isascii():
        return text
    return _SURROGATE.sub("\ufffd", text)


def _scrub_deep(value: object) -> object:
    if isinstance(value, str):
        return scrub_surrogates(value)
    if isinstance(value, list):
        return [_scrub_deep(item) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return [_scrub_deep(item) for item in cast(tuple[object, ...], value)]
    if isinstance(value, dict):
        items = cast(dict[object, object], value).items()
        return {
            (scrub_surrogates(k) if isinstance(k, str) else k): _scrub_deep(v) for k, v in items
        }
    return value


def _dump(value: object) -> str:
    return json.dumps(_scrub_deep(value), ensure_ascii=False)


def encode_content(value: object) -> str | None:
    """Message content as stored text (see the module notes for the prefix rule)."""
    if value is None:
        return None
    if isinstance(value, str):
        text = scrub_surrogates(value)
        if text.startswith(PARTS_PREFIX):
            return PARTS_PREFIX + json.dumps(text, ensure_ascii=False)
        return text
    try:
        return PARTS_PREFIX + _dump(value)
    except (TypeError, ValueError):
        return scrub_surrogates(str(value))


def decode_content(stored: str | None) -> object:
    """Reverse ``encode_content``; a prefixed value that is not valid JSON comes back raw."""
    if isinstance(stored, str) and stored.startswith(PARTS_PREFIX):
        try:
            return json.loads(stored[len(PARTS_PREFIX) :])
        except ValueError:
            log.warning("a stored message has a damaged parts payload; returning it as text")
            return stored
    return stored


def _json_or(raw: object, fallback: object) -> object:
    if not isinstance(raw, str):
        return fallback if raw is None else raw
    try:
        return json.loads(raw)
    except ValueError:
        return fallback


def _clamp_timestamp(value: object, now: float) -> float:
    stamp: float | None = None
    if isinstance(value, bool):
        stamp = None
    elif isinstance(value, (int, float)):
        stamp = float(value)
    elif isinstance(value, str):
        try:
            stamp = float(value.strip())
        except ValueError:
            stamp = None
    else:
        stamper = getattr(value, "timestamp", None)
        if callable(stamper):
            try:
                stamp = float(cast(Any, stamper)())
            except (TypeError, ValueError, OverflowError, OSError):
                stamp = None
    if stamp is None or not math.isfinite(stamp):
        return now
    if stamp < EARLIEST_TIMESTAMP or stamp > now + _FUTURE_SLACK:
        return now
    return stamp


def _tool_call_list(raw: object, index: int) -> list[Any] | dict[str, Any] | None:
    if raw is None:
        return None
    value: object = raw
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"message {index}: tool_calls is not valid JSON") from exc
    if isinstance(value, (list, tuple)):
        items = list(cast(Sequence[Any], value))
        return items or None
    if isinstance(value, dict):
        return dict(cast(dict[str, Any], value))
    if value is None:
        return None
    raise ValueError(f"message {index}: tool_calls must be a list or an object")


def _call_ids(calls: list[Any] | dict[str, Any] | None) -> list[str]:
    items: list[Any] = [calls] if isinstance(calls, dict) else list(calls or [])
    found: list[str] = []
    for item in items:
        if isinstance(item, dict):
            call = cast(dict[str, Any], item)
            ident = call.get("id") or call.get("call_id")
            if isinstance(ident, str) and ident and ident not in found:
                found.append(ident)
    return found


def _call_count(calls: object) -> int:
    if isinstance(calls, list):
        return len(cast(list[object], calls))
    return 1 if calls else 0


def _text_or_none(value: object) -> str | None:
    if value is None:
        return None
    return scrub_surrogates(value if isinstance(value, str) else str(value))


def _sidecar(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return scrub_surrogates(value)
    return _dump(value)


# -- row preparation ------------------------------------------------------------------------------


class _Row:
    __slots__ = ("calls", "values")

    def __init__(self, values: tuple[object, ...], calls: int) -> None:
        self.values = values
        self.calls = calls


def _prepare(msg: Mapping[str, Any], now: float, index: int = 0) -> _Row:
    role = msg.get("role")
    if not isinstance(role, str) or not role.strip():
        raise ValueError(f"message {index}: a message needs a role")
    calls = _tool_call_list(msg.get("tool_calls"), index)
    uids: dict[str, str] = {}
    given = _json_or(msg.get("tool_call_uids"), {})
    if isinstance(given, dict):
        for key, value in cast(dict[object, object], given).items():
            if isinstance(key, str) and isinstance(value, str):
                uids[key] = value
    for ident in _call_ids(calls):
        uids.setdefault(ident, new_message_uid())
    uid = msg.get("message_uid")
    token_count = msg.get("token_count")
    values: tuple[object, ...] = (
        role,
        encode_content(msg.get("content")),
        _text_or_none(msg.get("tool_call_id")),
        _dump(calls) if calls is not None else None,
        _text_or_none(msg.get("tool_name")),
        _clamp_timestamp(msg.get("timestamp"), now),
        token_count if isinstance(token_count, int) and not isinstance(token_count, bool) else None,
        _text_or_none(msg.get("finish_reason")),
        _sidecar(msg.get("reasoning")),
        _sidecar(msg.get("reasoning_details")),
        _text_or_none(msg.get("platform_message_id")),
        0 if msg.get("active", 1) in (0, False) else 1,
        1 if msg.get("compacted") else 0,
        _text_or_none(msg.get("api_content")),
        uid if isinstance(uid, str) and uid else new_message_uid(),
        json.dumps(uids, sort_keys=True) if uids else None,
    )
    return _Row(values, _call_count(calls))


def _refuse_if_closed(conn: sqlite3.Connection, session_id: str) -> None:
    row = conn.execute(
        "SELECT ended_at, end_reason FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is not None and row["ended_at"] is not None and row["end_reason"] == END_COMPRESSION:
        raise CompressionClosed(
            f"session {session_id} was compressed into a continuation; continue in the child"
        )


def _bump_counters(conn: sqlite3.Connection, session_id: str, rows: int, calls: int) -> None:
    if rows or calls:
        conn.execute(
            "UPDATE sessions SET message_count = message_count + ?, "
            "tool_call_count = tool_call_count + ? WHERE id = ?",
            (rows, calls, session_id),
        )


_ONE_ROW = "(" + ",".join("?" * (len(_WRITE_COLUMNS) + 1)) + ")"
_INSERT_HEAD = f"INSERT INTO messages (session_id, {', '.join(_WRITE_COLUMNS)}) VALUES "


def _insert_rows(conn: sqlite3.Connection, session_id: str, rows: Sequence[_Row]) -> list[int]:
    ids: list[int] = []
    for group in chunks(rows, BATCH_ROWS):
        params: list[object] = []
        for row in group:
            params.append(session_id)
            params.extend(row.values)
        cur = conn.execute(
            _INSERT_HEAD + ",".join([_ONE_ROW] * len(group)) + " RETURNING id", params
        )
        # One statement allocates ascending ids in VALUES order; RETURNING order is unspecified.
        ids.extend(sorted(int(r[0]) for r in cur.fetchall()))
    return ids


# -- append ---------------------------------------------------------------------------------------


def append_in(
    conn: sqlite3.Connection, session_id: str, msg: Mapping[str, Any], *, now: float
) -> int:
    """Insert one message in the caller's transaction; returns its row id."""
    row = _prepare(msg, now)
    _refuse_if_closed(conn, session_id)
    (row_id,) = _insert_rows(conn, session_id, [row])
    _bump_counters(conn, session_id, 1, row.calls)
    return row_id


def append_batch_in(
    conn: sqlite3.Connection, session_id: str, msgs: Sequence[Mapping[str, Any]], *, now: float
) -> list[int]:
    """Insert several messages; every row is validated before the first insert."""
    rows = [_prepare(msg, now, index) for index, msg in enumerate(msgs)]
    if not rows:
        return []
    _refuse_if_closed(conn, session_id)
    ids = _insert_rows(conn, session_id, rows)
    _bump_counters(conn, session_id, len(rows), sum(row.calls for row in rows))
    return ids


def _divert(store: StoreHandle, session_id: str, msgs: Sequence[Mapping[str, Any]]) -> None:
    try:
        guards.divert_messages(store.scope.home, session_id, [dict(msg) for msg in msgs])
    except Exception:
        log.error("could not divert pending messages for %s", session_id, exc_info=True)


def append(store: StoreHandle, session_id: str, msg: Mapping[str, Any]) -> int:
    """Append one message under the ``TRANSCRIPT`` budget; returns its row id.

    When the store file was replaced underneath us the message goes to the divert file
    (``<home>/sessions/<id>.jsonl``) and ``StoreReplaced`` is raised.
    """
    now = store.scope.clock.now().timestamp()
    _prepare(msg, now)  # fail on a bad message before taking the write lock
    try:
        return store.write(
            lambda conn: append_in(conn, session_id, msg, now=now), budget=TRANSCRIPT
        )
    except StoreReplaced:
        _divert(store, session_id, [msg])
        raise


def append_batch(
    store: StoreHandle, session_id: str, msgs: Sequence[Mapping[str, Any]]
) -> list[int]:
    """Append all of ``msgs`` in one transaction or none of them; returns the row ids in order."""
    if not msgs:
        return []
    now = store.scope.clock.now().timestamp()
    for index, msg in enumerate(msgs):
        _prepare(msg, now, index)
    try:
        return store.write(
            lambda conn: append_batch_in(conn, session_id, msgs, now=now), budget=TRANSCRIPT
        )
    except StoreReplaced:
        _divert(store, session_id, msgs)
        raise


# -- reads ----------------------------------------------------------------------------------------


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["content"] = decode_content(out.get("content"))
    out["tool_calls"] = _json_or(out.get("tool_calls"), None) if out.get("tool_calls") else None
    out["tool_call_uids"] = (
        _json_or(out.get("tool_call_uids"), {}) if out.get("tool_call_uids") else None
    )
    details = out.get("reasoning_details")
    out["reasoning_details"] = _json_or(details, details) if details else details
    return out


def get_in(
    conn: sqlite3.Connection, session_id: str, *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    live = "" if include_inactive else " AND active = 1"
    rows = conn.execute(
        f"SELECT * FROM messages WHERE session_id = ?{live} ORDER BY id", (session_id,)
    ).fetchall()
    return [_decode_row(row) for row in rows]


def get(
    store: StoreHandle, session_id: str, *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    """Rows in id order, decoded. Default: live rows; ``include_inactive`` adds rewound ones."""
    with store.read() as conn:
        return get_in(conn, session_id, include_inactive=include_inactive)


_CONVERSATION_KEYS = ("tool_calls", "tool_call_id", "tool_name", "timestamp", "api_content")
_ASSISTANT_KEYS = ("finish_reason", "reasoning", "reasoning_details")


def as_conversation(store: StoreHandle, session_id: str) -> list[dict[str, Any]]:
    """The live transcript as provider-shaped dicts (no row ids, uids or store flags)."""
    out: list[dict[str, Any]] = []
    for row in get(store, session_id):
        msg: dict[str, Any] = {"role": row["role"], "content": row["content"]}
        for key in _CONVERSATION_KEYS:
            if row.get(key) is not None:
                msg[key] = row[key]
        if row["role"] == "assistant":
            for key in _ASSISTANT_KEYS:
                if row.get(key) is not None:
                    msg[key] = row[key]
        out.append(msg)
    return out


def live_ids_in(conn: sqlite3.Connection, session_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM messages WHERE session_id = ? AND active = 1 ORDER BY id", (session_id,)
    ).fetchall()
    return [int(row[0]) for row in rows]


# -- replace / clone ------------------------------------------------------------------------------

_IDENTITY = slice(0, 4)  # role, content, tool_call_id, tool_calls of a prepared row


def _recount_in(conn: sqlite3.Connection, session_id: str) -> None:
    rows = conn.execute(
        "SELECT tool_calls FROM messages WHERE session_id = ? AND active = 1", (session_id,)
    ).fetchall()
    calls = sum(_call_count(_json_or(row[0], None)) for row in rows)
    conn.execute(
        "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?",
        (len(rows), calls, session_id),
    )


def replace_in(
    conn: sqlite3.Connection,
    session_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    now: float,
    soft: bool = False,
    expect_active_ids: Sequence[int] | None = None,
) -> list[int]:
    """Swap the live transcript for ``rows`` in the caller's transaction; returns the live ids.

    Destructive by default (every row of the session is deleted, then ``rows`` inserted). ``soft``
    keeps the leading live rows that already match ``rows`` (same ids and uids), archives the
    rest of the live rows with ``active = 0`` and inserts only the new tail. With
    ``expect_active_ids`` the live id list must still be exactly that, else ``TranscriptMoved``.
    """
    prepared = [_prepare(msg, now, index) for index, msg in enumerate(rows)]
    _refuse_if_closed(conn, session_id)
    live = conn.execute(
        "SELECT id, role, content, tool_call_id, tool_calls FROM messages "
        "WHERE session_id = ? AND active = 1 ORDER BY id",
        (session_id,),
    ).fetchall()
    if expect_active_ids is not None and [int(r["id"]) for r in live] != list(expect_active_ids):
        raise TranscriptMoved(
            f"the live transcript of {session_id} changed before the replacement was written"
        )
    kept = 0
    if soft:
        for stored, row in zip(live, prepared, strict=False):
            if tuple(stored)[1:5] != row.values[_IDENTITY]:
                break
            kept += 1
        if kept < len(live):
            conn.execute(
                "UPDATE messages SET active = 0 WHERE session_id = ? AND active = 1 AND id >= ?",
                (session_id, int(live[kept]["id"])),
            )
    else:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    _insert_rows(conn, session_id, prepared[kept:])
    _recount_in(conn, session_id)
    return live_ids_in(conn, session_id)


def replace(
    store: StoreHandle,
    session_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    soft: bool = False,
    expect_active_ids: Sequence[int] | None = None,
) -> list[int]:
    now = store.scope.clock.now().timestamp()
    return store.write(
        lambda conn: replace_in(
            conn, session_id, rows, now=now, soft=soft, expect_active_ids=expect_active_ids
        ),
        budget=TRANSCRIPT,
    )


def _message_columns(conn: sqlite3.Connection) -> list[str]:
    return [str(row[1]) for row in conn.execute("PRAGMA table_info(messages)").fetchall()]


def clone_rows_in(
    conn: sqlite3.Connection, to_session_id: str, row_ids: Sequence[int]
) -> list[int]:
    """Copy rows byte for byte into ``to_session_id`` as fresh live rows, in source id order.

    Every payload column (including ones added later by reconcile) is copied by SQL, so values
    keep their exact stored form and uids; the copies get new ids, ``active = 1`` and
    ``compacted = 0``. The target's counters grow by the copies. Unknown ids are skipped.
    """
    wanted = sorted({int(i) for i in row_ids})
    if not wanted:
        return []
    columns = [c for c in _message_columns(conn) if c not in _CLONE_SKIP]
    names = ", ".join(columns)
    new_ids: list[int] = []
    for part in chunks(wanted):
        marks = ",".join("?" * len(part))
        cur = conn.execute(
            f"INSERT INTO messages (session_id, active, compacted, {names}) "
            f"SELECT ?, 1, 0, {names} FROM messages WHERE id IN ({marks}) ORDER BY id "
            "RETURNING id, tool_calls",
            (to_session_id, *part),
        )
        made = sorted((int(r[0]), r[1]) for r in cur.fetchall())
        new_ids.extend(ident for ident, _ in made)
        calls = sum(_call_count(_json_or(raw, None)) for _, raw in made)
        _bump_counters(conn, to_session_id, len(made), calls)
    return new_ids


# -- resume / export size guards ------------------------------------------------------------------


def _config_int(scope: Scope, key: str) -> int | None:
    if scope.config is None:
        return None
    try:
        value = scope.config.get(key)
    except (KeyError, TypeError, ValueError):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def resume_limit(scope: Scope) -> int:
    """``sessions.resume_message_limit`` (default 20,000; 0 disables the guard)."""
    found = _config_int(scope, "sessions.resume_message_limit")
    return DEFAULT_RESUME_LIMIT if found is None else found


def export_limit(scope: Scope) -> int:
    """``sessions.export_message_limit`` when set, else the resume limit."""
    found = _config_int(scope, "sessions.export_message_limit")
    return resume_limit(scope) if found is None else found


def check_resume_size_in(conn: sqlite3.Connection, session_id: str, *, limit: int) -> int:
    """Live rows a resume of ``session_id`` loads (root … this segment); 4130 past ``limit``."""
    if limit <= 0:
        return 0
    ids = lineage.ancestors_in(conn, session_id)
    count = 0
    for part in chunks(ids):
        row = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE active = 1 "
            f"AND session_id IN ({','.join('?' * len(part))})",
            tuple(part),
        ).fetchone()
        count += int(row[0])
    if count > limit:
        raise ResumeTooLarge(
            f"session {session_id} has {count} messages, more than the resume limit of {limit}",
            count=count,
            numeric=RESUME_NUMERIC,
            hint="raise sessions.resume_message_limit (0 turns it off) or branch the session",
        )
    return count


def check_resume_size(store: StoreHandle, session_id: str, *, limit: int | None = None) -> int:
    wanted = resume_limit(store.scope) if limit is None else limit
    with store.read() as conn:
        return check_resume_size_in(conn, session_id, limit=wanted)


def check_export_size_in(conn: sqlite3.Connection, session_id: str, *, limit: int) -> int:
    """Rows an export of ``session_id`` writes (rewound ones too); 4131 past ``limit``."""
    if limit <= 0:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    count = int(row[0])
    if count > limit:
        raise ResumeTooLarge(
            f"session {session_id} has {count} messages, more than the export limit of {limit}",
            count=count,
            numeric=EXPORT_NUMERIC,
            hint="raise sessions.export_message_limit (0 turns the check off)",
        )
    return count


def check_export_size(store: StoreHandle, session_id: str, *, limit: int | None = None) -> int:
    wanted = export_limit(store.scope) if limit is None else limit
    with store.read() as conn:
        return check_export_size_in(conn, session_id, limit=wanted)
