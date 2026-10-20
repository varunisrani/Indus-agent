"""Session rows: create, end, reopen, child kinds, model config, per-chat flags, status, lookups.

Public operations are module functions that take the store handle first; each write also has an
``*_in(conn, ..., now=...)`` form that runs inside a caller's write transaction and never begins,
commits or rolls back.

Rules worth knowing before changing anything here:

- ``create`` is an upsert that only fills gaps. A value an earlier writer stored survives every
  later create; the one exception is the placeholder source ``unknown``. ``created_source`` and
  ``user_id`` are therefore set once. A child (``parent_session_id`` set) copies cwd, git root,
  git branch and profile from its parent when its own are empty.
- ``end`` is first-reason-wins: ending an ended row changes nothing, so a stale close arriving after
  a compression rotation cannot overwrite ``compression`` (R03.12). ``reopen`` clears both.
- Child kinds come from ``parent_session_id`` plus a marker in ``model_config``. A continuation
  copies its parent's config verbatim, so a marker counts only when it names this row's parent
  (R03.13).
- Per-chat flags (archive, pin, hide, read state) go to every member of the compression lineage:
  listings show a root through its tip, so a flag on one segment only would come back (R03.11).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Literal, cast

from indus.foundation.errors import SessionTombstoned, StoreError, StoreLocked
from indus.state.registry import StoreHandle
from indus.state.writer import OBSERVATION

__all__ = [
    "AUTOMATIC_REASONS",
    "COMPRESSION_REASONS",
    "CREATE_FIELDS",
    "END_BRANCHED",
    "END_COMPRESSION",
    "END_ORPHANED_COMPRESSION",
    "END_STARTUP_ORPHAN_REAP",
    "EXPLICIT_REASONS",
    "IN_CHUNK",
    "MARKERS",
    "MARKER_BRANCHED",
    "MARKER_DELEGATE",
    "MARKER_RESET",
    "RECOVERABLE_REASONS",
    "RESET_REASONS",
    "STATUS_COMPLETE",
    "STATUS_EMPTY",
    "STATUS_ERROR",
    "STATUS_INTERRUPTED",
    "TOMBSTONE_PREFIX",
    "YOLO_KEY",
    "AmbiguousSessionId",
    "ChildKind",
    "SessionNotFound",
    "auto_archive_in",
    "child_kind",
    "chunks",
    "classify_status",
    "create",
    "create_in",
    "end",
    "end_in",
    "get",
    "get_in",
    "is_automatic_end",
    "is_tombstoned_in",
    "is_unread",
    "mark_read",
    "mark_read_in",
    "mark_unread",
    "mark_unread_in",
    "parse_model_config",
    "patch_model_config",
    "patch_model_config_in",
    "reopen",
    "reopen_in",
    "resolve_id",
    "set_archived",
    "set_archived_in",
    "set_hidden",
    "set_hidden_in",
    "set_pinned",
    "set_pinned_in",
    "set_system_prompt",
    "set_system_prompt_in",
    "set_cwd",
    "set_cwd_in",
    "set_yolo",
    "status",
    "status_in",
    "statuses",
    "touch_activity",
    "touch_activity_in",
    "yolo_enabled",
]

log = logging.getLogger(__name__)

TOMBSTONE_PREFIX = "tombstone:"

# -- end reasons ----------------------------------------------------------------------------------

END_COMPRESSION = "compression"
END_ORPHANED_COMPRESSION = "orphaned_compression"
END_BRANCHED = "branched"
END_STARTUP_ORPHAN_REAP = "startup_orphan_reap"

# The row was rotated into a continuation (or the continuation was abandoned before any call).
COMPRESSION_REASONS = frozenset({END_COMPRESSION, END_ORPHANED_COMPRESSION})
# Deliberate conversation boundaries: the next message starts a new conversation.
RESET_REASONS = frozenset({"session_reset", "session_switch", "idle", "daily", "new_session"})
# Accidental closes a later resume may undo.
RECOVERABLE_REASONS = frozenset({"agent_close", END_STARTUP_ORPHAN_REAP, "superseded_by_resume"})
# Closes written by cleanup when a runtime went away, not by a person.
AUTOMATIC_REASONS = frozenset({"tui_shutdown", "ws_disconnect", "idle_timeout"})
# Closes a person asked for.
EXPLICIT_REASONS = frozenset({"cli_close", "tui_close", "user_exit", END_BRANCHED})


def is_automatic_end(reason: object) -> bool:
    """True for a close that no person asked for (automatic cleanup or a recoverable accident)."""
    if not isinstance(reason, str):
        return False
    return reason in AUTOMATIC_REASONS or reason in RECOVERABLE_REASONS


# -- child kinds ----------------------------------------------------------------------------------

MARKER_BRANCHED = "_branched_from"
MARKER_DELEGATE = "_delegate_from"
MARKER_RESET = "_reset_from"
MARKERS = (MARKER_BRANCHED, MARKER_DELEGATE, MARKER_RESET)

ChildKind = Literal["root", "continuation", "branch", "reset", "delegate", "tool"]

_MARKER_KINDS: tuple[tuple[str, ChildKind], ...] = (
    (MARKER_BRANCHED, "branch"),
    (MARKER_DELEGATE, "delegate"),
    (MARKER_RESET, "reset"),
)

YOLO_KEY = "yolo"

# SQLite's default bound-variable ceiling is far above this; keeping every IN list at 900 leaves
# room for the other parameters of a statement on any build (R03.18).
IN_CHUNK = 900

CREATE_FIELDS = frozenset(
    {
        "user_id",
        "model",
        "model_config",
        "system_prompt",
        "system_prompt_hash",
        "parent_session_id",
        "cwd",
        "git_root",
        "git_branch",
        "billing_provider",
        "billing_base_url",
        "profile_id",
        "started_at",
        "created_source",
        "archived",
        "auto_archived",
        "hidden",
        "pinned",
    }
)
# Columns a child copies from its parent while its own value is empty.
_INHERITED = ("cwd", "git_root", "git_branch", "profile_id")

STATUS_ERROR = "error"
STATUS_INTERRUPTED = "interrupted"
STATUS_EMPTY = "empty"
STATUS_COMPLETE = "complete"
_ERROR_FINISHES = frozenset({"error", "agent_error", "content_filter"})

_ACTIVITY_NOTE_MAX = 200


class SessionNotFound(StoreError):
    """No session has the id (or id prefix) asked for."""

    default_code = "store.session_not_found"


class AmbiguousSessionId(StoreError):
    """An id prefix matches several sessions; ``candidates`` lists them, newest first."""

    default_code = "store.ambiguous_id"

    def __init__(self, message: str, *, candidates: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.candidates: list[str] = list(candidates)


# -- small helpers --------------------------------------------------------------------------------


def chunks[T](items: Sequence[T], size: int = IN_CHUNK) -> Iterator[Sequence[T]]:
    """Consecutive slices of at most ``size`` items (for ``IN (...)`` lists)."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _marks(count: int) -> str:
    return ",".join("?" * count)


def _now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


def _field(row: Mapping[str, Any] | sqlite3.Row, name: str) -> Any:
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def parse_model_config(raw: object) -> dict[str, Any]:
    """A session's ``model_config`` as a dict; anything but a JSON object reads as ``{}``."""
    value: object = raw
    if isinstance(raw, (str, bytes)):
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if isinstance(value, dict):
        return dict(cast(dict[str, Any], value))
    return {}


def _dump_config(config: Mapping[str, Any]) -> str | None:
    return json.dumps(dict(config), sort_keys=True) if config else None


def _store_prompt_in(conn: sqlite3.Connection, text: object) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    clean = text.encode("utf-8", "replace").decode("utf-8")
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    conn.execute("INSERT OR IGNORE INTO system_prompts (hash, text) VALUES (?, ?)", (digest, clean))
    return digest


# -- create / get ---------------------------------------------------------------------------------


def is_tombstoned_in(conn: sqlite3.Connection, session_id: str) -> bool:
    """Was this id deleted here (P02-18)? A tombstoned id can never be created again (R03.22)."""
    found = conn.execute(
        "SELECT 1 FROM meta WHERE key = ?", (TOMBSTONE_PREFIX + session_id,)
    ).fetchone()
    return found is not None


def create_in(
    conn: sqlite3.Connection, session_id: str, source: str, *, now: float, **fields: Any
) -> bool:
    """Insert the row, or fill the gaps of an existing one. True when a new row was inserted.

    ``fields`` are columns from ``CREATE_FIELDS`` (``system_prompt`` is stored by hash,
    ``model_config`` may be a dict). Raises ``SessionTombstoned`` for a deleted id.
    """
    unknown = sorted(set(fields) - CREATE_FIELDS)
    if unknown:
        raise TypeError(f"unknown session fields: {', '.join(unknown)}")
    if not session_id or not source:
        raise ValueError("a session needs an id and a source")
    existed = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if existed is None and is_tombstoned_in(conn, session_id):
        raise SessionTombstoned(f"session {session_id} was deleted and cannot be created again")

    values: dict[str, Any] = {}
    for name, value in fields.items():
        if name == "system_prompt":
            digest = _store_prompt_in(conn, value)
            if digest is not None:
                values["system_prompt_hash"] = digest
        elif name == "model_config":
            if isinstance(value, Mapping):
                values[name] = _dump_config(cast(Mapping[str, Any], value))
            else:
                values[name] = value
        elif name not in ("started_at", "created_source"):
            values[name] = value
    started = fields.get("started_at")
    started_at = float(started) if isinstance(started, (int, float)) else now
    created_source = fields.get("created_source") or source

    names = ["id", "source", "created_source", "started_at", *values]
    params = [session_id, source, created_source, started_at, *values.values()]
    keep = [f"{name} = COALESCE(sessions.{name}, excluded.{name})" for name in values]
    conn.execute(
        f"INSERT INTO sessions ({', '.join(names)}) VALUES ({_marks(len(names))}) "
        "ON CONFLICT(id) DO UPDATE SET source = CASE WHEN sessions.source = 'unknown' "
        "THEN excluded.source ELSE sessions.source END" + "".join(f", {clause}" for clause in keep),
        params,
    )
    fill = ", ".join(
        f"{name} = COALESCE({name}, (SELECT p.{name} FROM sessions p "
        "WHERE p.id = sessions.parent_session_id))"
        for name in _INHERITED
    )
    conn.execute(
        f"UPDATE sessions SET {fill} WHERE id = ? AND parent_session_id IS NOT NULL", (session_id,)
    )
    return existed is None


def create(store: StoreHandle, session_id: str, source: str, **fields: Any) -> bool:
    """``create_in`` in its own write; stamps the store's profile when the caller names none."""
    if fields.get("profile_id") is None:
        fields["profile_id"] = store.scope.profile_id
    now = _now(store)
    return store.write(lambda conn: create_in(conn, session_id, source, now=now, **fields))


_GET_SQL = (
    "SELECT s.*, sp.text AS system_prompt FROM sessions s "
    "LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash WHERE s.id = ?"
)


def get_in(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    """The whole row as a dict (plus ``system_prompt`` text), or None."""
    row = conn.execute(_GET_SQL, (session_id,)).fetchone()
    return dict(row) if row is not None else None


def get(store: StoreHandle, session_id: str) -> dict[str, Any] | None:
    with store.read() as conn:
        return get_in(conn, session_id)


def set_system_prompt_in(conn: sqlite3.Connection, session_id: str, text: str) -> bool:
    """Point an existing row at ``text`` (stored by hash, like ``create``). True when updated.

    ``create`` only fills gaps, so a prompt rebuilt after compaction or a runtime change needs
    this write. A missing row is not created here; empty text is refused.
    """
    digest = _store_prompt_in(conn, text)
    if digest is None:
        raise ValueError("a stored system prompt cannot be empty")
    cur = conn.execute(
        "UPDATE sessions SET system_prompt_hash = ? WHERE id = ?", (digest, session_id)
    )
    return cur.rowcount > 0


def set_system_prompt(store: StoreHandle, session_id: str, text: str) -> bool:
    return store.write(lambda conn: set_system_prompt_in(conn, session_id, text))


# -- end / reopen ---------------------------------------------------------------------------------


def end_in(conn: sqlite3.Connection, session_id: str, reason: str, *, now: float) -> bool:
    """Stamp the end; a row that already ended keeps its first reason. True when stamped."""
    cur = conn.execute(
        "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL",
        (now, reason, session_id),
    )
    return cur.rowcount > 0


def end(store: StoreHandle, session_id: str, reason: str) -> bool:
    now = _now(store)
    return store.write(lambda conn: end_in(conn, session_id, reason, now=now))


def reopen_in(conn: sqlite3.Connection, session_id: str) -> bool:
    """Clear the end so the session can be resumed (and ended again).

    Resuming makes the chat active again, so an archive the idle sweep stamped on the lineage is
    lifted too, unless some segment was archived deliberately.
    """
    cur = conn.execute(
        "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?", (session_id,)
    )
    if cur.rowcount == 0:
        return False
    ids = _flag_ids_in(conn, session_id)
    deliberate = False
    for part in chunks(ids):
        hit = conn.execute(
            f"SELECT 1 FROM sessions WHERE id IN ({_marks(len(part))}) "
            "AND archived <> 0 AND auto_archived = 0 LIMIT 1",
            tuple(part),
        ).fetchone()
        deliberate = deliberate or hit is not None
    if not deliberate:
        _update_ids_in(conn, ids, "archived = 0, auto_archived = 0", ())
    return True


def reopen(store: StoreHandle, session_id: str) -> bool:
    return store.write(lambda conn: reopen_in(conn, session_id))


# -- child kinds ----------------------------------------------------------------------------------


def child_kind(
    row: Mapping[str, Any] | sqlite3.Row, parent: Mapping[str, Any] | sqlite3.Row | None = None
) -> ChildKind:
    """Why this row has a parent. ``parent`` (its parent's row) refines unmarked children.

    A marker decides only when its value is this row's parent id: a continuation inherits its
    parent's ``model_config`` verbatim, markers included, and must not turn into a fork (R03.13).
    """
    parent_id = _field(row, "parent_session_id")
    if not parent_id:
        return "root"
    config = parse_model_config(_field(row, "model_config"))
    for marker, kind in _MARKER_KINDS:
        if config.get(marker) == parent_id:
            return kind
    if _field(row, "source") == "tool":
        return "tool"
    if parent is not None:
        reason = _field(parent, "end_reason")
        if reason in RESET_REASONS:
            return "reset"
        if reason == END_BRANCHED:
            return "branch"
    return "continuation"


# -- model config ---------------------------------------------------------------------------------


def patch_model_config_in(
    conn: sqlite3.Connection, session_id: str, patch: Mapping[str, Any]
) -> bool:
    """Merge ``patch`` into the stored config (``None`` deletes a key). False when no row.

    Lineage markers are never changed by a patch: they record how the row was born.
    """
    row = conn.execute("SELECT model_config FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return False
    config = parse_model_config(row["model_config"])
    for key, value in patch.items():
        if key in MARKERS:
            log.debug("ignoring a patch of lineage marker %s on %s", key, session_id)
            continue
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = ?", (_dump_config(config), session_id)
    )
    return True


def patch_model_config(store: StoreHandle, session_id: str, patch: Mapping[str, Any]) -> bool:
    if not patch:
        return get(store, session_id) is not None
    return store.write(lambda conn: patch_model_config_in(conn, session_id, patch))


def set_cwd_in(conn: sqlite3.Connection, session_id: str, cwd: str) -> bool:
    """Move the session to another working folder (P19-03 workspace move). False when no row.
    The git root and branch recorded at start are kept as they are."""
    cursor = conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (cwd, session_id))
    return cursor.rowcount > 0


def set_cwd(store: StoreHandle, session_id: str, cwd: str) -> bool:
    return store.write(lambda conn: set_cwd_in(conn, session_id, cwd))


def set_yolo(store: StoreHandle, session_id: str, enabled: bool) -> bool:
    """Remember the per-session approval bypass across resume."""
    return patch_model_config(store, session_id, {YOLO_KEY: bool(enabled)})


def yolo_enabled(row: Mapping[str, Any] | sqlite3.Row | None) -> bool:
    """The stored bypass flag; False for a missing row or any parse problem."""
    if row is None:
        return False
    return parse_model_config(_field(row, "model_config")).get(YOLO_KEY) is True


# -- lineage-wide flags ---------------------------------------------------------------------------


def _flag_ids_in(conn: sqlite3.Connection, session_id: str) -> list[str]:
    from indus.state import lineage

    if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None:
        return []
    try:
        return lineage.lineage_ids_in(conn, session_id)
    except lineage.AmbiguousChain:
        # A forked chain still gets the user's flag on every segment rather than failing.
        return lineage.members_in(conn, session_id)


def _update_ids_in(
    conn: sqlite3.Connection, ids: Sequence[str], assignments: str, params: Sequence[Any]
) -> None:
    for part in chunks(ids):
        conn.execute(
            f"UPDATE sessions SET {assignments} WHERE id IN ({_marks(len(part))})",
            (*params, *part),
        )


def _set_lineage_in(
    conn: sqlite3.Connection, session_id: str, assignments: str, params: Sequence[Any]
) -> bool:
    ids = _flag_ids_in(conn, session_id)
    _update_ids_in(conn, ids, assignments, params)
    return bool(ids)


def set_archived_in(conn: sqlite3.Connection, session_id: str, archived: bool) -> bool:
    """The deliberate archive (or unarchive) of a whole chat; clears the idle-sweep stamp."""
    return _set_lineage_in(conn, session_id, "archived = ?, auto_archived = 0", (int(archived),))


def auto_archive_in(conn: sqlite3.Connection, session_id: str) -> bool:
    """The idle sweep's archive: marks only rows it hides as automatic, so resuming can undo it."""
    return _set_lineage_in(
        conn,
        session_id,
        "auto_archived = CASE WHEN archived = 0 THEN 1 ELSE auto_archived END, archived = 1",
        (),
    )


def set_pinned_in(conn: sqlite3.Connection, session_id: str, pinned: bool) -> bool:
    """Pin or unpin a chat; a pin also unhides it (a pinned chat is one to keep in view)."""
    assignments = "pinned = 1, hidden = 0" if pinned else "pinned = 0"
    return _set_lineage_in(conn, session_id, assignments, ())


def set_hidden_in(conn: sqlite3.Connection, session_id: str, hidden: bool) -> bool:
    return _set_lineage_in(conn, session_id, "hidden = ?", (int(hidden),))


def mark_read_in(conn: sqlite3.Connection, session_id: str, *, now: float) -> bool:
    return _set_lineage_in(conn, session_id, "last_read_at = ?", (now,))


def mark_unread_in(conn: sqlite3.Connection, session_id: str) -> bool:
    return _set_lineage_in(conn, session_id, "last_read_at = 0", ())


def set_archived(store: StoreHandle, session_id: str, archived: bool) -> bool:
    return store.write(lambda conn: set_archived_in(conn, session_id, archived))


def set_pinned(store: StoreHandle, session_id: str, pinned: bool) -> bool:
    return store.write(lambda conn: set_pinned_in(conn, session_id, pinned))


def set_hidden(store: StoreHandle, session_id: str, hidden: bool) -> bool:
    return store.write(lambda conn: set_hidden_in(conn, session_id, hidden))


def mark_read(store: StoreHandle, session_id: str) -> bool:
    now = _now(store)
    return store.write(lambda conn: mark_read_in(conn, session_id, now=now))


def mark_unread(store: StoreHandle, session_id: str) -> bool:
    return store.write(lambda conn: mark_unread_in(conn, session_id))


def is_unread(row: Mapping[str, Any] | sqlite3.Row) -> bool:
    """Activity after the read watermark. NULL watermark = never tracked = read; 0 = unread."""
    watermark = _field(row, "last_read_at")
    if watermark is None:
        return False
    latest = (
        _field(row, "last_active") or _field(row, "last_activity_at") or _field(row, "started_at")
    )
    return float(latest or 0.0) > float(watermark)


# -- status ---------------------------------------------------------------------------------------


def _has_tool_calls(raw: object) -> bool:
    if raw is None:
        return False
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return bool(raw.strip())
        return bool(value)
    return bool(raw)


def classify_status(role: str | None, has_tool_calls: bool, finish_reason: str | None) -> str:
    """Status of a session from its last message alone: error / interrupted / complete.

    Unknown shapes read as complete: a picker should not raise an alarm it cannot explain.
    """
    if (finish_reason or "").strip().lower() in _ERROR_FINISHES:
        return STATUS_ERROR
    kind = (role or "").strip().lower()
    if kind in ("user", "tool") or (kind == "assistant" and has_tool_calls):
        return STATUS_INTERRUPTED
    return STATUS_COMPLETE


def status_in(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute(
        "SELECT role, tool_calls, finish_reason FROM messages "
        "WHERE session_id = ? AND active = 1 ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if row is None:
        return STATUS_EMPTY
    return classify_status(row["role"], _has_tool_calls(row["tool_calls"]), row["finish_reason"])


def status(store: StoreHandle, session_id: str) -> str:
    with store.read() as conn:
        return status_in(conn, session_id)


def statuses(store: StoreHandle, session_ids: Sequence[str]) -> dict[str, str]:
    """``{id: status}`` for many sessions; one indexed lookup of the last live row per chunk."""
    ids = list(dict.fromkeys(sid for sid in session_ids if sid))
    found = dict.fromkeys(ids, STATUS_EMPTY)
    with store.read() as conn:
        for part in chunks(ids):
            rows = conn.execute(
                "SELECT m.session_id, m.role, m.tool_calls, m.finish_reason FROM messages m "
                "JOIN (SELECT session_id, MAX(id) AS last_id FROM messages "
                f"WHERE active = 1 AND session_id IN ({_marks(len(part))}) GROUP BY session_id) t "
                "ON m.id = t.last_id",
                tuple(part),
            ).fetchall()
            for row in rows:
                found[row["session_id"]] = classify_status(
                    row["role"], _has_tool_calls(row["tool_calls"]), row["finish_reason"]
                )
    return found


# -- lookups --------------------------------------------------------------------------------------

_PREFIX_CANDIDATES = 10


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def resolve_id(store: StoreHandle, text: str) -> str:
    """The exact id, else the one id starting with ``text``.

    Raises ``SessionNotFound`` when nothing matches and ``AmbiguousSessionId`` (with candidates,
    newest first) when several do.
    """
    wanted = (text or "").strip()
    if not wanted:
        raise SessionNotFound("no session id was given")
    with store.read() as conn:
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (wanted,)).fetchone() is not None:
            return wanted
        rows = conn.execute(
            "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' "
            "ORDER BY started_at DESC, id DESC LIMIT ?",
            (_like_escape(wanted) + "%", _PREFIX_CANDIDATES + 1),
        ).fetchall()
    ids = [str(row["id"]) for row in rows]
    if not ids:
        raise SessionNotFound(f"no session id starts with {wanted!r}")
    if len(ids) > 1:
        shown = ids[:_PREFIX_CANDIDATES]
        more = " and more" if len(ids) > _PREFIX_CANDIDATES else ""
        raise AmbiguousSessionId(
            f"{wanted!r} matches several sessions: {', '.join(shown)}{more}", candidates=shown
        )
    return ids[0]


# -- activity -------------------------------------------------------------------------------------


def _bounded_note(note: str | None) -> str | None:
    if note is None:
        return None
    text = note.encode("utf-8", "replace").decode("utf-8")
    return text[:_ACTIVITY_NOTE_MAX]


def touch_activity_in(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    at: float,
    note: str | None = None,
    provenance: str | None = None,
) -> bool:
    """Record activity at ``at``; an older time than the stored one changes nothing."""
    cur = conn.execute(
        "UPDATE sessions SET last_activity_at = ?, last_activity_note = ?, "
        "last_activity_provenance = ? "
        "WHERE id = ? AND (last_activity_at IS NULL OR last_activity_at < ?)",
        (at, _bounded_note(note), provenance, session_id, at),
    )
    return cur.rowcount > 0


def touch_activity(
    store: StoreHandle,
    session_id: str,
    note: str | None = None,
    provenance: str | None = None,
    *,
    at: float | None = None,
) -> bool:
    """Observation-only heartbeat under the short ``OBSERVATION`` budget.

    Contention drops this heartbeat (False) instead of stalling the turn it observes; the next
    one retries.
    """
    when = _now(store) if at is None else float(at)
    try:
        return store.write(
            lambda conn: touch_activity_in(
                conn, session_id, at=when, note=note, provenance=provenance
            ),
            budget=OBSERVATION,
        )
    except StoreLocked:
        log.debug("activity heartbeat for %s skipped: the store is busy", session_id)
        return False
