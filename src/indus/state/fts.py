"""Full-text indexes over the transcript, and how they fail open.

Two derived FTS5 indexes sit beside ``messages``; neither may ever cost a transcript write.

- ``messages_fts`` (unicode61 words) reads its external content through the view
  ``messages_fts_rows``: a pure function of one message row — its text (content stored as
  ``PARTS_PREFIX + JSON`` contributes only its text parts), the tool name and the tool-call
  JSON, with tool-role rows cut to their first 8,192 characters (R03.7). The view, the sync
  triggers and FTS5's own ``rebuild`` / ``integrity-check`` all evaluate the same SQL expression,
  so the stored tokens always equal what the checker re-reads.
- ``messages_trigram`` (trigram tokenizer, SQLite ≥ 3.34) serves substring and CJK search. It
  leaves out tool rows, tool-call JSON and sessions whose source is ``cron`` or ``subagent``.

Fail-open (R03.6): when SQLite reports corruption scoped to one of these indexes during a write
or a search, one transaction records ``meta.fts_stale`` and drops every FTS trigger; the
canonical write is then retried and lands, and search answers from canonical rows with LIKE.
No live path ever runs a full ``rebuild``. A later open rebuilds the indexes, but only under the
cross-process ``.fts_rebuild.lock`` and only when no other in-process generation of the store is
open; otherwise it defers with a doubling backoff and finally gives up (both kept in ``meta``).

Every 1,000 writes a bounded ``merge`` keeps the segment count low (the writer's housekeeping
slot); ``merge(store)`` is the same work on demand (before VACUUM).
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import socket
import sqlite3
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from indus.foundation.clock import Clock
from indus.state import journal
from indus.state.errors import is_fts_scoped_corruption, is_lock_error
from indus.state.schema import meta_delete, meta_get, meta_set
from indus.state.writer import blocking_sleep

if TYPE_CHECKING:
    from indus.state.registry import StoreHandle

__all__ = [
    "BASE",
    "BASE_TRIGGERS",
    "BASE_VIEW",
    "DEFERRAL_KEY",
    "EXTENSION",
    "MERGE_EVERY",
    "STALE_KEY",
    "TOOL_PREFIX_CHARS",
    "TRIGRAM",
    "TRIGRAM_EXCLUDED_SOURCES",
    "TRIGRAM_STALE_KEY",
    "TRIGRAM_TRIGGERS",
    "TRIGRAM_VIEW",
    "Deferral",
    "FtsExtension",
    "RebuildLock",
    "attach",
    "declared_tables",
    "detach_in",
    "fail_open",
    "fts5_available",
    "holder_is_dead",
    "integrity_ok",
    "is_stale",
    "merge",
    "merge_in",
    "probe",
    "read_deferral",
    "rebuild",
    "recover",
    "reset_capabilities",
    "stale_info",
    "text_sql",
    "trigram_supported",
    "usable_tables",
]

log = logging.getLogger(__name__)

BASE = "messages_fts"
TRIGRAM = "messages_trigram"
BASE_VIEW = "messages_fts_rows"
TRIGRAM_VIEW = "messages_trigram_rows"
BASE_TRIGGERS = ("messages_fts_ai", "messages_fts_ad", "messages_fts_au")
TRIGRAM_TRIGGERS = ("messages_trigram_ai", "messages_trigram_ad", "messages_trigram_au")
ALL_TRIGGERS = BASE_TRIGGERS + TRIGRAM_TRIGGERS
SHADOW_SUFFIXES = ("data", "idx", "content", "docsize", "config")

TOOL_PREFIX_CHARS = 8192
TRIGRAM_EXCLUDED_SOURCES = ("cron", "subagent")
TRIGRAM_MIN_SQLITE = (3, 34, 0)

STALE_KEY = "fts_stale"
TRIGRAM_STALE_KEY = "fts_trigram_stale"
DEFERRAL_KEY = "fts_rebuild_deferral"

MERGE_EVERY = 1000
MERGE_COMMANDS = 4
MERGE_PAGES = 500

REBUILD_LOCK_NAME = ".fts_rebuild.lock"
REBUILD_LOCK_SECONDS = 120.0
REBUILD_LOCK_POLL = 0.1
DEAD_HOLDER_AFTER = 5.0

BACKOFF_FIRST = 60.0
BACKOFF_CAP = 3600.0
GIVE_UP_ATTEMPTS = 10
GIVE_UP_SECONDS = 1800.0

# -- capabilities of the linked SQLite ----------------------------------------------------------

_caps_lock = threading.Lock()
_caps: dict[str, bool] = {}
_said: set[str] = set()


def _once(key: str) -> bool:
    with _caps_lock:
        if key in _said:
            return False
        _said.add(key)
        return True


def _probe(spec: str) -> bool:
    """Can this SQLite build create ``fts5(<spec>)``? Checked on a private in-memory database."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(f"CREATE VIRTUAL TABLE temp.indus_fts_probe USING fts5({spec})")
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _cached_probe(key: str, spec: str) -> bool:
    with _caps_lock:
        known = _caps.get(key)
    if known is not None:
        return known
    found = _probe(spec)
    with _caps_lock:
        _caps[key] = found
    return found


def fts5_available() -> bool:
    found = _cached_probe("fts5", "x")
    if not found and _once("no-fts5"):
        log.warning(
            "this SQLite build (%s) has no FTS5; session search scans messages with LIKE",
            sqlite3.sqlite_version,
        )
    return found


def trigram_supported() -> bool:
    """Trigram needs SQLite ≥ 3.34 and the tokenizer compiled in."""
    if not fts5_available() or journal.linked_sqlite_version() < TRIGRAM_MIN_SQLITE:
        return False
    return _cached_probe("trigram", "x, tokenize='trigram'")


def reset_capabilities() -> None:
    """Forget the probes and the one-time notes (tests)."""
    with _caps_lock:
        _caps.clear()
        _said.clear()


# -- the projection -----------------------------------------------------------------------------


def _parts_prefix() -> str:
    # Imported late: the message module may import the registry, which imports this module.
    from indus.state.messages import PARTS_PREFIX

    return PARTS_PREFIX


def text_sql(alias: str) -> str:
    """The searchable text of the message row ``alias``: plain content as stored; a parts payload
    contributes its text parts joined by spaces. Pure per row, never raises on bad JSON."""
    col = f"{alias}.content"
    # Compared and cut as bytes: SQLite's text functions are unreliable around the NUL.
    prefix = _parts_prefix().encode("utf-8")
    body = f"CAST(substr(CAST({col} AS BLOB), {len(prefix) + 1}) AS TEXT)"
    item = f"{body}, '$[' || part_no || ']'"
    # A recursive counter, not json_each(): FTS5 reads its content view as "main.<view>", and
    # SQLite then looks for a table "main.json_each" inside the view.
    parts = (
        "(WITH RECURSIVE indus_part(part_no) AS (SELECT 0 UNION ALL "
        f"SELECT part_no + 1 FROM indus_part WHERE part_no + 1 < json_array_length({body})) "
        f"SELECT group_concat(CASE json_type({item}) "
        f"WHEN 'text' THEN json_extract({item}) "
        f"WHEN 'object' THEN CASE WHEN json_extract({body}, '$[' || part_no || '].type') = 'text' "
        f"THEN json_extract({body}, '$[' || part_no || '].text') END END, ' ') "
        "FROM indus_part)"
    )
    return (
        f"(CASE WHEN substr(CAST({col} AS BLOB), 1, {len(prefix)}) = X'{prefix.hex()}' THEN "
        f"CASE WHEN json_valid({body}) THEN CASE json_type({body}) "
        f"WHEN 'text' THEN json_extract({body}, '$') "
        f"WHEN 'object' THEN json_extract({body}, '$.text') "
        f"WHEN 'array' THEN {parts} END "
        f"ELSE {col} END "
        f"ELSE {col} END)"
    )


def _base_text(alias: str) -> str:
    text = text_sql(alias)
    return (
        f"(CASE WHEN {alias}.role = 'tool' "
        f"THEN substr(COALESCE({text}, ''), 1, {TOOL_PREFIX_CHARS}) ELSE {text} END)"
    )


def _excluded_sources_sql() -> str:
    return ", ".join(f"'{source}'" for source in TRIGRAM_EXCLUDED_SOURCES)


def _trigram_member(alias: str) -> str:
    """Does the row ``alias`` belong in the trigram index (judged at write time)?"""
    return (
        f"{alias}.role <> 'tool' AND EXISTS (SELECT 1 FROM sessions AS s "
        f"WHERE s.id = {alias}.session_id AND s.source NOT IN ({_excluded_sources_sql()}))"
    )


def _in_trigram(alias: str) -> str:
    """Is the row ``alias`` in the trigram index now? Asked of the index itself: on a session
    cascade the session row is already gone when the message trigger runs."""
    return f"EXISTS (SELECT 1 FROM {TRIGRAM}_docsize WHERE id = {alias}.id)"


def _base_objects() -> list[str]:
    return [
        f"CREATE VIEW IF NOT EXISTS {BASE_VIEW} AS SELECT m.id AS id, "
        f"{_base_text('m')} AS content, m.tool_name AS tool_name, m.tool_calls AS tool_calls "
        "FROM messages AS m",
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {BASE} USING fts5(content, tool_name, tool_calls, "
        f"content='{BASE_VIEW}', content_rowid='id', tokenize='unicode61')",
    ]


def _base_triggers() -> list[str]:
    add = (
        f"INSERT INTO {BASE}(rowid, content, tool_name, tool_calls) "
        f"VALUES (new.id, {_base_text('new')}, new.tool_name, new.tool_calls);"
    )
    remove = (
        f"INSERT INTO {BASE}({BASE}, rowid, content, tool_name, tool_calls) "
        f"VALUES ('delete', old.id, {_base_text('old')}, old.tool_name, old.tool_calls);"
    )
    changed = (
        "old.content IS NOT new.content OR old.tool_name IS NOT new.tool_name "
        "OR old.tool_calls IS NOT new.tool_calls OR old.role IS NOT new.role"
    )
    ins, dele, upd = BASE_TRIGGERS
    return [
        f"CREATE TRIGGER IF NOT EXISTS {ins} AFTER INSERT ON messages BEGIN {add} END",
        f"CREATE TRIGGER IF NOT EXISTS {dele} AFTER DELETE ON messages BEGIN {remove} END",
        f"CREATE TRIGGER IF NOT EXISTS {upd} AFTER UPDATE OF content, tool_name, tool_calls, role "
        f"ON messages WHEN {changed} BEGIN {remove} {add} END",
    ]


def _trigram_objects() -> list[str]:
    return [
        f"CREATE VIEW IF NOT EXISTS {TRIGRAM_VIEW} AS SELECT m.id AS id, "
        f"{text_sql('m')} AS content, m.tool_name AS tool_name "
        "FROM messages AS m JOIN sessions AS s ON s.id = m.session_id "
        f"WHERE m.role <> 'tool' AND s.source NOT IN ({_excluded_sources_sql()})",
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {TRIGRAM} USING fts5(content, tool_name, "
        f"content='{TRIGRAM_VIEW}', content_rowid='id', tokenize='trigram')",
    ]


def _trigram_triggers() -> list[str]:
    ins, dele, upd = TRIGRAM_TRIGGERS
    changed = (
        "old.content IS NOT new.content OR old.tool_name IS NOT new.tool_name "
        "OR old.role IS NOT new.role OR old.session_id IS NOT new.session_id"
    )
    return [
        f"CREATE TRIGGER IF NOT EXISTS {ins} AFTER INSERT ON messages "
        f"WHEN {_trigram_member('new')} BEGIN "
        f"INSERT INTO {TRIGRAM}(rowid, content, tool_name) "
        f"VALUES (new.id, {text_sql('new')}, new.tool_name); END",
        f"CREATE TRIGGER IF NOT EXISTS {dele} AFTER DELETE ON messages "
        f"WHEN {_in_trigram('old')} BEGIN "
        f"INSERT INTO {TRIGRAM}({TRIGRAM}, rowid, content, tool_name) "
        f"VALUES ('delete', old.id, {text_sql('old')}, old.tool_name); END",
        f"CREATE TRIGGER IF NOT EXISTS {upd} AFTER UPDATE OF content, tool_name, role, session_id "
        f"ON messages WHEN {changed} BEGIN "
        f"INSERT INTO {TRIGRAM}({TRIGRAM}, rowid, content, tool_name) "
        f"SELECT 'delete', old.id, {text_sql('old')}, old.tool_name WHERE {_in_trigram('old')}; "
        f"INSERT INTO {TRIGRAM}(rowid, content, tool_name) "
        f"SELECT new.id, {text_sql('new')}, new.tool_name WHERE {_trigram_member('new')}; END",
    ]


# -- what the store holds -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Objects:
    tables: frozenset[str]
    views: frozenset[str]
    triggers: frozenset[str]

    def has_family(self, family: str, view: str, triggers: tuple[str, ...]) -> bool:
        return family in self.tables and view in self.views and set(triggers) <= self.triggers

    def orphans(self, family: str) -> list[str]:
        if family in self.tables:
            return []
        return [f"{family}_{s}" for s in SHADOW_SUFFIXES if f"{family}_{s}" in self.tables]


def _objects(conn: sqlite3.Connection) -> _Objects:
    found: dict[str, set[str]] = {"table": set(), "view": set(), "trigger": set()}
    for row in conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view', 'trigger')"
    ):
        found[str(row[0])].add(str(row[1]))
    return _Objects(
        frozenset(found["table"]), frozenset(found["view"]), frozenset(found["trigger"])
    )


def declared_tables(conn: sqlite3.Connection) -> list[str]:
    """Our FTS virtual tables present in the schema (queryable or not)."""
    tables = _objects(conn).tables
    return [name for name in (BASE, TRIGRAM) if name in tables]


def probe(conn: sqlite3.Connection) -> tuple[list[str], sqlite3.Error | None]:
    """Our FTS tables this connection can query (SELECT probes only), and the first
    FTS-scoped corruption a probe raised. A missing tokenizer makes a table unusable too."""
    usable: list[str] = []
    damage: sqlite3.Error | None = None
    for name in declared_tables(conn):
        try:
            conn.execute(f"SELECT rowid FROM {name} LIMIT 0").fetchall()
        except sqlite3.Error as exc:
            if damage is None and is_fts_scoped_corruption(exc):
                damage = exc
            continue
        usable.append(name)
    return usable, damage


def usable_tables(conn: sqlite3.Connection) -> list[str]:
    return probe(conn)[0]


def _as_dict(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    items = cast(dict[object, Any], value).items()
    return {str(key): item for key, item in items}


def stale_info(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The fail-open marker (``{"reason", "at"}``), or None when the indexes are trusted."""
    raw = meta_get(conn, STALE_KEY)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return {"reason": raw, "at": None}
    fields = _as_dict(value)
    return fields if fields is not None else {"reason": str(value), "at": None}


def is_stale(conn: sqlite3.Connection) -> bool:
    return meta_get(conn, STALE_KEY) is not None


def _has_messages(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone() is not None


def _sql_now(conn: sqlite3.Connection) -> float:
    return float(conn.execute("SELECT (julianday('now') - 2440587.5) * 86400.0").fetchone()[0])


def _drop_triggers(conn: sqlite3.Connection, names: tuple[str, ...]) -> int:
    present = _objects(conn).triggers
    dropped = 0
    for name in names:
        if name in present:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            dropped += 1
    return dropped


def _drop_family(conn: sqlite3.Connection, family: str, view: str, triggers: tuple[str, ...]):
    _drop_triggers(conn, triggers)
    conn.execute(f"DROP TABLE IF EXISTS {family}")
    conn.execute(f"DROP VIEW IF EXISTS {view}")


def _drop_orphans(conn: sqlite3.Connection, objects: _Objects) -> None:
    for family in (BASE, TRIGRAM):
        names = objects.orphans(family)
        for name in names:
            conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        if names:
            log.warning(
                "dropped leftover search-index tables without their index (%s); the index is "
                "created again from the messages",
                ", ".join(names),
            )


def detach_in(conn: sqlite3.Connection, reason: str, *, now: float | None) -> int:
    """Mark the indexes stale and drop every FTS trigger, inside the caller's transaction.

    Returns how many triggers were dropped. The first reason recorded is kept."""
    if meta_get(conn, STALE_KEY) is None:
        at = now if now is not None else _sql_now(conn)
        meta_set(conn, STALE_KEY, json.dumps({"reason": reason[:500], "at": at}))
    return _drop_triggers(conn, ALL_TRIGGERS)


# -- schema extension ---------------------------------------------------------------------------


def _create(conn: sqlite3.Connection, statements: list[str], family: str, existed: bool) -> None:
    for sql in statements:
        conn.execute(sql)
    if not existed and _has_messages(conn):
        conn.execute(f"INSERT INTO {family}({family}) VALUES ('rebuild')")


def _install(conn: sqlite3.Connection, objects: _Objects) -> None:
    """Create whatever is missing (the store is not stale). Runs inside a write transaction."""
    _create(conn, _base_objects(), BASE, BASE in objects.tables)
    for sql in _base_triggers():
        conn.execute(sql)
    if trigram_supported():
        trigram_existed = TRIGRAM in objects.tables
        if meta_get(conn, TRIGRAM_STALE_KEY) is not None:
            _drop_family(conn, TRIGRAM, TRIGRAM_VIEW, TRIGRAM_TRIGGERS)
            meta_delete(conn, TRIGRAM_STALE_KEY)
            trigram_existed = False
        _create(conn, _trigram_objects(), TRIGRAM, trigram_existed)
        for sql in _trigram_triggers():
            conn.execute(sql)
        return
    if _drop_triggers(conn, TRIGRAM_TRIGGERS):
        meta_set(conn, TRIGRAM_STALE_KEY, "trigram tokenizer unavailable")
        log.warning(
            "this SQLite build cannot run the substring search index; it is left out of date "
            "until a build with the trigram tokenizer opens the store"
        )
    if _once("no-trigram"):
        log.info(
            "SQLite %s has no trigram tokenizer; substring and CJK search fall back to LIKE",
            sqlite3.sqlite_version,
        )


class FtsExtension:
    """Schema-init hook (``schema.SchemaExtension``) that keeps the FTS objects in place."""

    name = "fts"

    def needs_work(self, conn: sqlite3.Connection) -> bool:
        with _caps_lock:
            if _caps.get("setup_failed"):
                return False  # tried in this process already; do not retry on every open
        objects = _objects(conn)
        if objects.orphans(BASE) or objects.orphans(TRIGRAM):
            return True
        if not fts5_available():
            return bool(objects.triggers & set(ALL_TRIGGERS))
        if is_stale(conn):
            return False  # rebuilt by ``recover`` under the admission lock, never here
        if not objects.has_family(BASE, BASE_VIEW, BASE_TRIGGERS):
            return True
        if trigram_supported():
            return meta_get(conn, TRIGRAM_STALE_KEY) is not None or not objects.has_family(
                TRIGRAM, TRIGRAM_VIEW, TRIGRAM_TRIGGERS
            )
        return bool(objects.triggers & set(TRIGRAM_TRIGGERS))

    def apply(self, conn: sqlite3.Connection) -> None:
        objects = _objects(conn)
        _drop_orphans(conn, objects)
        if not fts5_available():
            if detach_in(conn, "this SQLite build has no FTS5", now=None):
                log.warning("dropped the search-index triggers: this SQLite build has no FTS5")
            return
        if is_stale(conn):
            return
        conn.execute("SAVEPOINT indus_fts_setup")
        try:
            _install(conn, _objects(conn))
        except sqlite3.OperationalError as exc:
            if is_lock_error(exc):
                raise  # the schema init rolls back and waits
            conn.execute("ROLLBACK TO indus_fts_setup")
            conn.execute("RELEASE indus_fts_setup")
            with _caps_lock:
                _caps["setup_failed"] = True
            log.error(
                "could not set up the search index (%s); search scans messages with LIKE", exc
            )
            return
        conn.execute("RELEASE indus_fts_setup")


EXTENSION = FtsExtension()


# -- fail-open ----------------------------------------------------------------------------------


class _FailOpenState(threading.local):
    busy: bool = False
    blank_retry_used: bool = False


_fail_open_state = _FailOpenState()


def fail_open(store: StoreHandle, exc: BaseException) -> bool:
    """Detach the indexes after FTS-scoped corruption; True when the caller should retry.

    A detach that drops nothing (a sibling detached first) still allows one retry on this
    thread; a second such call is refused so an FTS error that is not caused by the triggers
    cannot loop the writer.
    """
    if not is_fts_scoped_corruption(exc):
        return False
    state = _fail_open_state
    if state.busy:
        return False
    state.busy = True
    try:
        now = store.scope.clock.now().timestamp()
        dropped = store.write(lambda conn: detach_in(conn, str(exc), now=now))
    finally:
        state.busy = False
    if dropped:
        state.blank_retry_used = False
        log.error(
            "the search index of %s is damaged (%s); its triggers were removed so messages keep "
            "saving, and search scans messages until the index is rebuilt on a later start",
            store.path,
            exc,
        )
        return True
    if state.blank_retry_used:
        return False
    state.blank_retry_used = True
    return True


# -- merge --------------------------------------------------------------------------------------


def merge_in(conn: sqlite3.Connection) -> int:
    """Bounded ``'merge'``: at most 4 commands of 500 pages per index, stopping when a command
    finds nothing to do. Returns the number of commands that did work."""
    if is_stale(conn):
        return 0
    worked = 0
    for table in usable_tables(conn):
        for _ in range(MERGE_COMMANDS):
            before = conn.total_changes
            conn.execute(f"INSERT INTO {table}({table}, rank) VALUES ('merge', {MERGE_PAGES})")
            if conn.total_changes - before < 2:
                break
            worked += 1
    return worked


def merge(store: StoreHandle) -> int:
    """``merge_in`` in one write transaction (used before VACUUM)."""
    return store.write(merge_in)


def _housekeeping_merge(conn: sqlite3.Connection) -> None:
    merged = merge_in(conn)
    if merged:
        log.debug("merged %d search-index segment batches", merged)


# -- integrity ----------------------------------------------------------------------------------


def _is_read_only_error(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF == sqlite3.SQLITE_READONLY
    return "readonly" in str(exc).lower()


def integrity_ok(conn: sqlite3.Connection) -> bool | None:
    """FTS5's own consistency check over every FTS table; None when there are none.

    The strict check (index against the projection view) is an INSERT, so a read-only
    connection falls back to ``PRAGMA integrity_check(<table>)`` (structure only)."""
    tables = declared_tables(conn)
    if not tables:
        return None
    for table in tables:
        try:
            conn.execute(f"INSERT INTO {table}({table}, rank) VALUES ('integrity-check', 1)")
        except sqlite3.Error as exc:
            if not _is_read_only_error(exc):
                return False
            try:
                rows = conn.execute(f"PRAGMA integrity_check({table})").fetchall()
            except sqlite3.Error:
                return False
            if [str(row[0]) for row in rows] != ["ok"]:
                return False
    return True


# -- rebuild admission --------------------------------------------------------------------------


def _start_ticks(pid: int) -> int | None:
    """Process start time from ``/proc`` (Linux); identifies a pid across reuse."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
        return int(raw.rsplit(b")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _pid_namespace() -> int | None:
    try:
        return os.stat("/proc/self/ns/pid").st_ino
    except OSError:
        return None


def _holder_record(clock: Clock) -> dict[str, Any]:
    pid = os.getpid()
    return {
        "pid": pid,
        "start": _start_ticks(pid),
        "pidns": _pid_namespace(),
        "host": socket.gethostname(),
        "at": clock.now().timestamp(),
    }


def holder_is_dead(record: object) -> bool:
    """True only when the recorded holder provably no longer runs: same host and PID
    namespace, and its pid is gone or now belongs to a process started later."""
    fields = _as_dict(record)
    if fields is None:
        return False
    pid = fields.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if fields.get("host") != socket.gethostname() or fields.get("pidns") != _pid_namespace():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    recorded = fields.get("start")
    current = _start_ticks(pid)
    return isinstance(recorded, int) and current is not None and current != recorded


if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI only
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


_CONTENTION = frozenset({errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK})


class RebuildLock:
    """The cross-process admission lock for a full index rebuild (fails closed)."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Clock,
        sleep: Callable[[float], None] | None = None,
        timeout: float = REBUILD_LOCK_SECONDS,
        poll: float = REBUILD_LOCK_POLL,
        dead_after: float = DEAD_HOLDER_AFTER,
    ) -> None:
        self.path = path
        self._clock = clock
        self._sleep = sleep if sleep is not None else blocking_sleep(clock)
        self.timeout = timeout
        self.poll = poll
        self.dead_after = dead_after
        self._fd: int | None = None
        self.broke_stale_holder = False

    def _open(self) -> int:
        return os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)

    def _read_record(self, fd: int) -> object:
        try:
            raw = os.pread(fd, 4096, 0)
            return json.loads(raw.decode("utf-8", "replace")) if raw else None
        except (OSError, ValueError):
            return None

    def _same_file(self, fd: int) -> bool:
        try:
            held, named = os.fstat(fd), os.stat(self.path)
        except OSError:
            return False
        return (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)

    def acquire(self) -> bool:
        """Wait up to ``timeout`` s; break a provably dead holder after ``dead_after`` s."""
        try:
            fd = self._open()
        except OSError as exc:
            log.warning("cannot open %s (%s); the index rebuild waits", self.path, exc)
            return False
        started = self._clock.monotonic()
        last_holder_check = started
        while True:
            try:
                _try_lock(fd)
            except OSError as exc:
                if exc.errno not in _CONTENTION:
                    os.close(fd)
                    log.warning("cannot lock %s (%s); the index rebuild waits", self.path, exc)
                    return False
                now = self._clock.monotonic()
                if now - last_holder_check >= self.dead_after:
                    last_holder_check = now
                    if holder_is_dead(self._read_record(fd)):
                        fd = self._break(fd)
                        if fd < 0:
                            return False
                        continue
                if now - started >= self.timeout:
                    os.close(fd)
                    return False
                self._sleep(self.poll)
                continue
            if not self._same_file(fd):  # a breaker replaced the file while we waited
                with contextlib.suppress(OSError):
                    _unlock(fd)
                os.close(fd)
                try:
                    fd = self._open()
                except OSError:
                    return False
                continue
            self._write_record(fd)
            self._fd = fd
            return True

    def _break(self, fd: int) -> int:
        log.warning(
            "the index rebuild lock %s is held by a process that no longer runs; taking it over",
            self.path,
        )
        self.broke_stale_holder = True
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            os.close(fd)
            log.warning("could not remove the stale lock %s: %s", self.path, exc)
            return -1
        os.close(fd)
        try:
            return self._open()
        except OSError:
            return -1

    def _write_record(self, fd: int) -> None:
        payload = json.dumps(_holder_record(self._clock)).encode("utf-8")
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
            os.pwrite(fd, payload, 0)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)  # a record left behind means the holder died
        with contextlib.suppress(OSError):
            _unlock(fd)
        os.close(fd)


# -- recovery -----------------------------------------------------------------------------------


@dataclass
class Deferral:
    attempts: int = 0
    first_at: float = 0.0
    next_at: float = 0.0
    reason: str = ""
    gave_up: bool = False

    def after(self, now: float, reason: str) -> Deferral:
        attempts = self.attempts + 1
        first = self.first_at if self.attempts else now
        delay = min(BACKOFF_FIRST * 2 ** (attempts - 1), BACKOFF_CAP)
        futile = attempts >= GIVE_UP_ATTEMPTS or now - first >= GIVE_UP_SECONDS
        return Deferral(attempts, first, now + delay, reason, futile)


def read_deferral(conn: sqlite3.Connection) -> Deferral | None:
    raw = meta_get(conn, DEFERRAL_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return Deferral(
            int(data["attempts"]),
            float(data["first_at"]),
            float(data["next_at"]),
            str(data.get("reason", "")),
            bool(data.get("gave_up", False)),
        )
    except (ValueError, TypeError, KeyError):
        return Deferral(attempts=GIVE_UP_ATTEMPTS, gave_up=True, reason="unreadable record")


def _defer(store: StoreHandle, previous: Deferral | None, reason: str) -> str:
    now = store.scope.clock.now().timestamp()
    record = (previous or Deferral()).after(now, reason)
    store.write(lambda conn: meta_set(conn, DEFERRAL_KEY, json.dumps(asdict(record))))
    if record.gave_up:
        log.error(
            "gave up rebuilding the search index of %s after %d attempts (%s); search keeps "
            "scanning messages until `indus sessions repair` rebuilds it",
            store.path,
            record.attempts,
            reason,
        )
        return "gave_up"
    log.warning(
        "the search index of %s is out of date; rebuilding it is postponed (%s), next try "
        "after %.0fs",
        store.path,
        reason,
        record.next_at - now,
    )
    return "deferred"


def _rebuild_in(conn: sqlite3.Connection, *, force: bool) -> bool:
    if not force and not is_stale(conn):
        return False  # another process finished first
    _drop_family(conn, BASE, BASE_VIEW, BASE_TRIGGERS)
    _drop_family(conn, TRIGRAM, TRIGRAM_VIEW, TRIGRAM_TRIGGERS)
    for key in (STALE_KEY, TRIGRAM_STALE_KEY, DEFERRAL_KEY):
        meta_delete(conn, key)
    _drop_orphans(conn, _objects(conn))
    _install(conn, _objects(conn))
    return True


def _other_handles(store: StoreHandle) -> list[str]:
    from indus.state import registry  # the registry imports this module

    return registry.other_generations(store.path, exclude=store)


def rebuild(store: StoreHandle, *, force: bool = True) -> bool:
    """Drop and recreate the indexes from the messages, under the admission rules: no other
    in-process generation and the cross-process lock. False when refused or not needed."""
    if not fts5_available() or _other_handles(store):
        return False
    lock = RebuildLock(store.path.parent / REBUILD_LOCK_NAME, clock=store.scope.clock)
    if not lock.acquire():
        return False
    try:
        done = store.write(lambda conn: _rebuild_in(conn, force=force))
    finally:
        lock.release()
    if done:
        log.warning("rebuilt the search index of %s", store.path)
    return done


def recover(store: StoreHandle) -> str:
    """At open: rebuild stale indexes when admitted, else record a deferral.

    Returns ``not_stale``, ``unsupported``, ``waiting`` (backoff not over), ``deferred``,
    ``gave_up`` or ``rebuilt``."""
    # The writer connection: an open must not leave a pooled read connection behind.
    with store.writer.borrow() as conn:
        if meta_get(conn, STALE_KEY) is None:
            return "not_stale"
        previous = read_deferral(conn)
    if not fts5_available():
        return "unsupported"
    if previous is not None and previous.gave_up:
        if _once(f"gave-up:{store.path}"):
            log.warning(
                "the search index of %s stays out of date (automatic rebuild gave up); run "
                "`indus sessions repair`",
                store.path,
            )
        return "gave_up"
    now = store.scope.clock.now().timestamp()
    if previous is not None and now < previous.next_at:
        return "waiting"
    others = _other_handles(store)
    if others:
        return _defer(store, previous, f"another handle on this store is open ({others[0]})")
    lock = RebuildLock(store.path.parent / REBUILD_LOCK_NAME, clock=store.scope.clock)
    if not lock.acquire():
        return _defer(store, previous, "another process holds the rebuild lock")
    try:
        done = store.write(lambda conn: _rebuild_in(conn, force=False))
    finally:
        lock.release()
    if done:
        log.warning("rebuilt the out-of-date search index of %s", store.path)
    return "rebuilt"


def attach(store: StoreHandle) -> None:
    """Wire a freshly opened handle: fail-open hook, periodic merge, one recovery attempt."""
    store.writer.set_fts_fail_open(lambda exc: fail_open(store, exc))
    store.writer.add_periodic(MERGE_EVERY, _housekeeping_merge)
    try:
        recover(store)
    except Exception:  # recovery is best effort; the store itself is usable
        log.warning("could not check the search index of %s", store.path, exc_info=True)
