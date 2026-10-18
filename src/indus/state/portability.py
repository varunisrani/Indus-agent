"""Sessions as JSON lines: export, and an import that trusts nothing it reads (P02-17).

Export format (one JSON object per line, ASCII-escaped):

- ``{"kind": "session", "format": "indus.session/1", "id": …, <columns>, "system_prompt": …,
  "meta": [<session_meta rows>]}`` — every column of the row except the prompt hash; JSON-text
  columns are written as JSON values;
- ``{"kind": "message", "session_id": …, <columns>}`` for each message in id order, with the
  content decoded back to text or parts (``messages.decode_content``); the row id is left out;
- lineage mode starts with ``{"kind": "lineage", "segments": [root, …, tip]}`` and then writes
  every segment as above, root first.

Import reads the whole payload first and reports **every** problem with its line number;
nothing is written unless all of it is usable. Limits: 500 sessions, 10,000 messages per
session, 50,000 messages, 5 MiB per session, 25 MiB in all. NaN / Infinity (also ``1e309``)
anywhere, nested included, are refused; integer columns are clamped to SQLite's signed 64-bit
range. The write is one transaction: ids already present are skipped, ids deleted here
(tombstoned, P02-18) refuse the whole import, a parent link is kept only when the parent is in
the store or the payload and keeping it closes no cycle (the closing edge is the one cut).
Activity, rewind and compression bookkeeping start fresh; message counters are recomputed from
the live rows; message uids are kept. Content is always re-encoded, so a literal string that
looks like our internal parts encoding stays a string.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from indus.foundation.errors import ResumeTooLarge
from indus.state import lineage, messages, schema, session_meta, sessions
from indus.state.deletion import chunked
from indus.state.registry import StoreHandle
from indus.state.writer import ROUTINE

__all__ = [
    "EXPORT_GUARD_CODE",
    "FORMAT",
    "MAX_MESSAGES_PER_SESSION",
    "MAX_MESSAGES_TOTAL",
    "MAX_SESSIONS",
    "MAX_SESSION_BYTES",
    "MAX_TOTAL_BYTES",
    "ExportReport",
    "ImportIssue",
    "ImportReport",
    "dump_line",
    "export_all",
    "export_session",
    "import_jsonl",
]

FORMAT: Final = "indus.session/1"
MAX_SESSIONS: Final = 500
MAX_MESSAGES_PER_SESSION: Final = 10_000
MAX_MESSAGES_TOTAL: Final = 50_000
MAX_SESSION_BYTES: Final = 5 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 25 * 1024 * 1024
EXPORT_GUARD_CODE: Final = 4131
DEFAULT_MESSAGE_LIMIT: Final = 20_000
INT_MIN: Final = -(2**63)
INT_MAX: Final = 2**63 - 1

# Message columns that hold JSON text; exported as JSON values.
_JSON_MESSAGE_COLUMNS: Final = frozenset({"tool_calls", "reasoning_details", "tool_call_uids"})
# Session columns that start fresh on import (runtime state, not history).
_FRESH_SESSION_COLUMNS: Final = frozenset(
    {
        "last_activity_at",
        "last_activity_note",
        "last_activity_provenance",
        "rewind_count",
        "compression_cooldown_until",
        "compression_cooldown_error",
        "compression_fallback_streak",
        "compression_ineffective_count",
        "compression_overload_streak",
        "last_read_at",
    }
)
# Session columns the import fills itself.
_DERIVED_SESSION_COLUMNS: Final = frozenset(
    {"id", "system_prompt_hash", "parent_session_id", "message_count", "tool_call_count"}
)


@dataclass(frozen=True)
class ImportIssue:
    line: int
    session_id: str | None
    message: str

    def __str__(self) -> str:
        who = f" (session {self.session_id})" if self.session_id else ""
        return f"line {self.line}{who}: {self.message}"


@dataclass
class ImportReport:
    ok: bool = True
    imported: list[str] = field(default_factory=lambda: [])
    skipped: list[str] = field(default_factory=lambda: [])
    detached: list[str] = field(default_factory=lambda: [])
    retitled: list[str] = field(default_factory=lambda: [])
    errors: list[ImportIssue] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class ExportReport:
    path: Path
    sessions: int
    messages: int


def _columns(table: str) -> dict[str, str]:
    """Column name -> declared affinity word (TEXT / REAL / INTEGER) from the schema."""
    for spec in schema.TABLES:
        if spec.name == table:
            return {c.name: c.decl.split()[0].upper() for c in spec.columns}
    raise LookupError(table)


def dump_line(obj: Mapping[str, Any]) -> str:
    """One export line (no newline): ASCII JSON so lone surrogates survive as escapes."""
    return json.dumps(obj, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


# -- export ---------------------------------------------------------------------------------------


def _json_value(text: object) -> object:
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _finite(value: object) -> object:
    return None if isinstance(value, float) and not math.isfinite(value) else value


def _session_header(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    data = {k: _finite(row[k]) for k in row.keys()}  # noqa: SIM118 (sqlite3.Row)
    prompt_hash = data.pop("system_prompt_hash", None)
    prompt: object = None
    if prompt_hash:
        found = conn.execute(
            "SELECT text FROM system_prompts WHERE hash = ?", (prompt_hash,)
        ).fetchone()
        prompt = found[0] if found is not None else None
    config = _json_value(data.get("model_config"))
    data["model_config"] = config
    sid = str(data.pop("id"))
    return {
        "kind": "session",
        "format": FORMAT,
        "id": sid,
        **data,
        "system_prompt": prompt,
        "meta": session_meta.export_rows_in(conn, sid),
    }


def _message_line(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": "message", "session_id": row["session_id"]}
    for name in row.keys():  # noqa: SIM118 (sqlite3.Row)
        if name in ("id", "session_id"):
            continue
        value = _finite(row[name])
        if name == "content":
            value = messages.decode_content(cast(str | None, value))
        elif name in _JSON_MESSAGE_COLUMNS:
            value = _json_value(value)
        out[name] = value
    return out


def _session_lines(
    conn: sqlite3.Connection, session_id: str, include_inactive: bool
) -> list[dict[str, Any]]:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return []
    query = "SELECT * FROM messages WHERE session_id = ?"
    if not include_inactive:
        query += " AND active = 1"
    lines = [_session_header(conn, row)]
    lines.extend(_message_line(m) for m in conn.execute(query + " ORDER BY id", (session_id,)))
    return lines


def _message_limit(store: StoreHandle) -> int:
    config = store.scope.config
    raw = None if config is None else config.get("sessions.resume_message_limit")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return DEFAULT_MESSAGE_LIMIT
    return raw


def _check_export_size(store: StoreHandle, conn: sqlite3.Connection, ids: list[str]) -> None:
    limit = _message_limit(store)
    if limit == 0:
        return
    count = 0
    for batch in chunked(ids):
        marks = ",".join("?" * len(batch))
        row = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE active = 1 AND session_id IN ({marks})", batch
        ).fetchone()
        count += int(row[0])
    if count > limit:
        raise ResumeTooLarge(
            f"the session has {count} live messages; exports held in memory stop at {limit}",
            count=count,
            numeric=EXPORT_GUARD_CODE,
            hint="export it to a file instead, or raise sessions.resume_message_limit",
        )


def export_session(
    store: StoreHandle,
    session_id: str,
    *,
    lineage: bool = False,
    include_inactive: bool = False,
    guard: bool = False,
) -> Iterator[dict[str, Any]]:
    """The export objects of one session (or, with ``lineage``, its whole compression chain).

    ``guard=True`` is for callers that hold the whole result in memory: more live messages than
    ``sessions.resume_message_limit`` raises ``ResumeTooLarge`` with code 4131 first.
    """
    with store.read() as conn:
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
            return iter(())
        ids = _lineage_ids(conn, session_id) if lineage else [session_id]
        if guard:
            _check_export_size(store, conn, ids)
        out: list[dict[str, Any]] = []
        if lineage:
            out.append({"kind": "lineage", "format": FORMAT, "segments": ids})
        for sid in ids:
            out.extend(_session_lines(conn, sid, include_inactive))
    return iter(out)


def _lineage_ids(conn: sqlite3.Connection, session_id: str) -> list[str]:
    return list(lineage.lineage_ids_in(conn, session_id))


def export_all(store: StoreHandle, path: Path, *, include_inactive: bool = True) -> ExportReport:
    """Stream every session into ``path`` (owner-only; written aside, then moved into place)."""
    target = Path(path)
    with store.read() as conn:
        ids = [
            str(r[0])
            for r in conn.execute("SELECT id FROM sessions ORDER BY started_at, id").fetchall()
        ]
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    sessions_written = 0
    messages_written = 0
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
            for sid in ids:
                with store.read() as conn:
                    lines = _session_lines(conn, sid, include_inactive)
                if not lines:
                    continue  # deleted while we were exporting
                sessions_written += 1
                messages_written += len(lines) - 1
                handle.writelines(dump_line(obj) + "\n" for obj in lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    return ExportReport(target, sessions_written, messages_written)


# -- import: reading ------------------------------------------------------------------------------


class _NonFinite:
    """Stands in for a NaN / Infinity token while a line is parsed."""

    __slots__ = ("token",)

    def __init__(self, token: str) -> None:
        self.token = token


def _float_or_marker(text: str) -> object:
    number = float(text)
    return number if math.isfinite(number) else _NonFinite(text)


def _loads(text: str) -> object:
    return json.loads(text, parse_constant=_NonFinite, parse_float=_float_or_marker)


def _non_finite_path(value: object, root: str = "$") -> str | None:
    """Where the first NaN / Infinity marker sits in a parsed value, else None."""
    pending: list[tuple[object, str]] = [(value, root)]
    while pending:
        current, where = pending.pop()
        if isinstance(current, _NonFinite):
            return f"{where} ({current.token})"
        if isinstance(current, dict):
            items = cast(dict[str, object], current).items()
            pending.extend((child, f"{where}.{key}") for key, child in items)
        elif isinstance(current, list):
            pending.extend(
                (child, f"{where}[{i}]") for i, child in enumerate(cast(list[object], current))
            )
    return None


def _scrub(value: object) -> object:
    """Lone UTF-16 surrogates become U+FFFD (SQLite cannot bind them)."""
    if isinstance(value, str):
        return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(value, dict):
        return {str(k): _scrub(v) for k, v in cast(dict[str, object], value).items()}
    if isinstance(value, list):
        return [_scrub(v) for v in cast(list[object], value)]
    return value


@dataclass
class _Draft:
    """One session as read from the payload, before normalising."""

    line: int
    session_id: str
    header: dict[str, object]
    size: int
    messages: list[tuple[int, dict[str, object]]] = field(default_factory=lambda: [])


@dataclass
class _Ready:
    """One session normalised and ready to insert."""

    line: int
    session_id: str
    columns: dict[str, object]
    parent_id: str | None
    prompt: str | None
    meta: list[Mapping[str, Any]]
    rows: list[dict[str, object]]


class _Problems:
    def __init__(self) -> None:
        self.items: list[ImportIssue] = []

    def add(self, line: int, session_id: str | None, message: str) -> None:
        self.items.append(ImportIssue(line, session_id, message))


def _text_lines(source: str | os.PathLike[str] | Iterable[str]) -> Iterator[str]:
    if isinstance(source, str | os.PathLike):
        with open(source, encoding="utf-8") as handle:
            yield from handle
    else:
        yield from source


def _read_payload(
    source: str | os.PathLike[str] | Iterable[str], problems: _Problems
) -> list[_Draft]:
    drafts: dict[str, _Draft] = {}
    loose: list[tuple[int, str, dict[str, object], int]] = []
    total = 0
    sessions_seen = 0
    try:
        for number, raw in enumerate(_text_lines(source), start=1):
            text = raw.strip()
            if not text:
                continue
            size = len(text.encode("utf-8", errors="surrogatepass"))
            total += size
            if total > MAX_TOTAL_BYTES:
                problems.add(number, None, "the payload is larger than the 25 MiB import limit")
                break
            try:
                obj = _loads(text)
            except (ValueError, RecursionError) as exc:
                problems.add(number, None, f"not valid JSON: {exc}")
                continue
            if not isinstance(obj, dict):
                problems.add(number, None, "each line must be a JSON object")
                continue
            line = cast(dict[str, object], obj)
            kind = line.get("kind")
            bad = _non_finite_path(line)
            sid_text = line.get("id") if kind == "session" else line.get("session_id")
            sid = sid_text if isinstance(sid_text, str) and sid_text.strip() else None
            if bad is not None:
                problems.add(number, sid, f"non-finite number at {bad}")
                continue
            if kind == "lineage":
                continue
            if kind == "session":
                sessions_seen += 1
                if sessions_seen == MAX_SESSIONS + 1:
                    problems.add(number, sid, "more than 500 sessions in one import")
                if sid is None:
                    problems.add(number, None, "a session needs a non-empty string id")
                elif sid in drafts:
                    problems.add(number, sid, "the same session id appears twice")
                else:
                    drafts[sid] = _Draft(number, sid, line, size)
            elif kind == "message":
                if sid is None:
                    problems.add(number, None, "a message needs a non-empty string session_id")
                else:
                    loose.append((number, sid, line, size))
            else:
                problems.add(number, sid, f"unknown line kind {kind!r}")
    except (OSError, UnicodeDecodeError) as exc:
        problems.add(0, None, f"the payload could not be read: {exc}")
    for number, sid, line, size in loose:
        owner = drafts.get(sid)
        if owner is None:
            problems.add(number, sid, "a message for a session that has no session line")
            continue
        owner.messages.append((number, line))
        owner.size += size
    return list(drafts.values())


# -- import: checking -----------------------------------------------------------------------------


def _clamp(value: int) -> int:
    return max(INT_MIN, min(INT_MAX, value))


def _cell(name: str, affinity: str, value: object) -> object:
    """One scalar column value checked against its declared type (ValueError when unusable)."""
    if value is None:
        return None
    if affinity == "TEXT":
        if not isinstance(value, str):
            raise ValueError(f"{name} must be text")
        return value
    if affinity == "REAL":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{name} must be a number")
        return float(value)
    if affinity == "INTEGER":
        if isinstance(value, bool):
            return int(value)
        if not isinstance(value, int):
            raise ValueError(f"{name} must be a whole number")
        return _clamp(value)
    return value


def _json_text(name: str, value: object, *, objects_only: bool = False) -> str | None:
    """A JSON-ish column: a value is dumped, a string is kept (checked when it parses)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = _loads(value)
        except (ValueError, RecursionError):
            if objects_only:
                raise ValueError(f"{name} must be a JSON object") from None
            return value  # unparsable text is kept verbatim, as stored
        bad = _non_finite_path(parsed, name)
        if bad is not None:
            raise ValueError(f"non-finite number at {bad}")
        if objects_only and not isinstance(parsed, dict):
            raise ValueError(f"{name} must be a JSON object")
        return value
    if objects_only and not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _flag(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool | int):
        return 1 if value else 0
    raise ValueError("flags must be true/false or 0/1")


def _normal_message(line: dict[str, object], now: float) -> dict[str, object]:
    columns = _columns("messages")
    row: dict[str, object] = {}
    role = line.get("role")
    if not isinstance(role, str) or not role:
        raise ValueError("role must be a non-empty string")
    content: object = line.get("content")
    if content is not None and not isinstance(content, str | list | dict):
        raise ValueError("content must be text, a list, an object or null")
    stamp = line.get("timestamp")
    if stamp is None:
        stamp = now
    elif isinstance(stamp, bool) or not isinstance(stamp, int | float):
        raise ValueError("timestamp must be a number of epoch seconds")
    uid = line.get("message_uid")
    if uid is not None and (not isinstance(uid, str) or not uid or len(uid) > 64):
        raise ValueError("message_uid must be a short non-empty string")
    for name, affinity in columns.items():
        if name in ("id", "session_id"):
            continue
        value = line.get(name)
        if name == "content":
            row[name] = messages.encode_content(cast(object, content))
        elif name == "timestamp":
            row[name] = float(stamp)
        elif name in ("active", "compacted"):
            row[name] = _flag(value, 1 if name == "active" else 0)
        elif name in _JSON_MESSAGE_COLUMNS:
            row[name] = _json_text(name, value)
        else:
            row[name] = _cell(name, affinity, value)
    return row


def _normal_session(draft: _Draft, now: float) -> _Ready:
    header = draft.header
    columns: dict[str, object] = {}
    for name, affinity in _columns("sessions").items():
        if name in _DERIVED_SESSION_COLUMNS or name in _FRESH_SESSION_COLUMNS:
            continue
        value = header.get(name)
        if name == "model_config":
            columns[name] = _json_text(name, value, objects_only=True)
        elif name in ("archived", "auto_archived", "pinned", "hidden"):
            columns[name] = _flag(value, 0)
        else:
            columns[name] = _cell(name, affinity, value)
    if not columns.get("source"):
        columns["source"] = "import"
    if columns.get("started_at") is None:
        columns["started_at"] = now
    if not columns.get("archived"):
        columns["auto_archived"] = 0  # only a sweep sets it, and only on rows it archived
    parent = header.get("parent_session_id")
    if parent is not None and not isinstance(parent, str):
        raise ValueError("parent_session_id must be text")
    prompt = header.get("system_prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise ValueError("system_prompt must be text")
    meta: object = header.get("meta") or []
    if not isinstance(meta, list):
        raise ValueError("meta must be a list")
    meta_rows: list[Mapping[str, Any]] = []
    for item in cast(list[object], meta):
        if not isinstance(item, dict):
            raise ValueError("each meta entry must be an object")
        session_meta.prepare_import_row(cast(dict[str, Any], item), now)
        meta_rows.append(cast(dict[str, Any], item))
    parent_id = (parent.strip() or None) if isinstance(parent, str) else None
    return _Ready(draft.line, draft.session_id, columns, parent_id, prompt, meta_rows, [])


def _check(drafts: list[_Draft], now: float, problems: _Problems) -> list[_Ready]:
    ready: list[_Ready] = []
    total_messages = 0
    for draft in drafts:
        sid = draft.session_id
        failed = False
        if len(draft.messages) > MAX_MESSAGES_PER_SESSION:
            problems.add(draft.line, sid, "more than 10,000 messages in one session")
            failed = True
        before = total_messages
        total_messages += len(draft.messages)
        if before <= MAX_MESSAGES_TOTAL < total_messages:
            problems.add(draft.line, sid, "more than 50,000 messages in one import")
            failed = True
        if draft.size > MAX_SESSION_BYTES:
            problems.add(draft.line, sid, "the session is larger than the 5 MiB import limit")
            failed = True
        try:
            item = _normal_session(_scrub_draft(draft), now)
        except ValueError as exc:
            problems.add(draft.line, sid, str(exc))
            failed = True
            item = None
        for number, line in draft.messages:
            try:
                row = _normal_message(cast(dict[str, object], _scrub(line)), now)
            except ValueError as exc:
                problems.add(number, sid, str(exc))
                failed = True
                continue
            if item is not None:
                item.rows.append(row)
        if item is not None and not failed:
            ready.append(item)
    return ready


def _scrub_draft(draft: _Draft) -> _Draft:
    return _Draft(
        draft.line,
        draft.session_id,
        cast(dict[str, object], _scrub(draft.header)),
        draft.size,
        draft.messages,
    )


# -- import: writing ------------------------------------------------------------------------------


def _store_prompt(conn: sqlite3.Connection, text: str | None) -> str | None:
    if text is None:
        return None
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    conn.execute("INSERT OR IGNORE INTO system_prompts(hash, text) VALUES (?, ?)", (key, text))
    return key


def _free_title(conn: sqlite3.Connection, title: str) -> str:
    candidate = title
    number = 1
    while conn.execute("SELECT 1 FROM sessions WHERE title = ?", (candidate,)).fetchone():
        number += 1
        candidate = f"{title} #{number}"
    return candidate


def _tool_call_count(stored: object) -> int:
    value = _json_value(stored)
    if isinstance(value, list):
        return len(cast(list[object], value))
    return 1 if isinstance(value, dict) else 0


def _insert_row(conn: sqlite3.Connection, table: str, values: Mapping[str, object]) -> None:
    """INSERT with only the non-NULL values, so declared defaults fill the rest."""
    present = {k: v for k, v in values.items() if v is not None}
    names = ", ".join(present)
    marks = ", ".join("?" * len(present))
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(present.values()))


def _insert(conn: sqlite3.Connection, item: _Ready, now: float) -> bool:
    """Insert one session with its messages and metadata; True when its title had to change."""
    columns = dict(item.columns)
    columns["system_prompt_hash"] = _store_prompt(conn, item.prompt)
    renamed = False
    title = columns.get("title")
    if isinstance(title, str):
        free = _free_title(conn, title)
        renamed = free != title
        columns["title"] = free
    live = [r for r in item.rows if r.get("active")]
    columns["message_count"] = len(live)
    columns["tool_call_count"] = sum(_tool_call_count(r.get("tool_calls")) for r in live)
    _insert_row(conn, "sessions", {"id": item.session_id, **columns})
    for row in item.rows:
        _insert_row(conn, "messages", {"session_id": item.session_id, **row})
    session_meta.import_rows_in(conn, item.session_id, item.meta, now=now)
    return renamed


def _closes_a_cycle(conn: sqlite3.Connection, child: str, parent: str) -> bool:
    current: str | None = parent
    seen: set[str] = set()
    while current is not None and current not in seen:
        if current == child:
            return True
        seen.add(current)
        row = conn.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?", (current,)
        ).fetchone()
        current = None if row is None or row[0] is None else str(row[0])
    return current is not None  # a loop that does not pass through the child


def _write(conn: sqlite3.Connection, ready: list[_Ready], now: float) -> ImportReport:
    report = ImportReport()
    refused = [item for item in ready if sessions.is_tombstoned_in(conn, item.session_id)]
    if refused:
        report.ok = False
        report.errors = [
            ImportIssue(i.line, i.session_id, "this session was deleted here and is not restored")
            for i in refused
        ]
        return report
    links: list[tuple[str, str]] = []
    for item in ready:
        exists = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (item.session_id,))
        if exists.fetchone() is not None:
            report.skipped.append(item.session_id)
            continue
        if _insert(conn, item, now):
            report.retitled.append(item.session_id)
        report.imported.append(item.session_id)
        if item.parent_id is not None:
            links.append((item.session_id, item.parent_id))
    for child, parent in links:
        known = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (parent,)).fetchone()
        if known is None or _closes_a_cycle(conn, child, parent):
            report.detached.append(child)
            continue
        conn.execute("UPDATE sessions SET parent_session_id = ? WHERE id = ?", (parent, child))
    return report


def import_jsonl(
    store: StoreHandle, source: str | os.PathLike[str] | Iterable[str]
) -> ImportReport:
    """Validate the whole payload, then import it in one transaction (or not at all)."""
    now = store.scope.clock.now().timestamp()
    problems = _Problems()
    drafts = _read_payload(source, problems)
    ready = _check(drafts, now, problems)
    if problems.items:
        return ImportReport(ok=False, errors=sorted(problems.items, key=lambda p: p.line))
    return store.write(lambda conn: _write(conn, ready, now), budget=ROUTINE)
