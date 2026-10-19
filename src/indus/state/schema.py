"""Schema 1: the declared tables, the declarative column reconcile and the data-migration runner.

The schema is declared as data (``TABLES``, ``INDEXES``, ``TRIGGERS``) in our own names (D-16.2;
the store is not compatible with any other program's database). ``init(conn, clock=...)`` makes a
store match the declaration:

1. a read-only probe first: a store that already matches costs no write lock at all;
2. otherwise one ``BEGIN IMMEDIATE`` transaction: create missing tables, add every declared
   column a table lacks (``PRAGMA table_info`` diffed against the declaration — new columns and
   tables never need a version bump), then indexes and triggers (after the reconcile, so an index
   on a just-added column works), stamp a fresh store (``schema_version`` 1, ``meta.store_id``,
   ``meta.created_at``), mint the once-per-file generation token ``meta.db_file_generation``
   (first opener wins) and a non-zero 31-bit ``PRAGMA application_id`` derived from it when the
   header holds 0, then run the extensions (FTS setup from P02-14).

Opening must wait out a sibling's write lock like any write does (R03.9): lock errors are retried
with 250–1000 ms jitter for up to 20 s, then logged and raised as ``StoreLocked``. A store whose
version is newer than ours is refused (``StoreUnavailable``, kind ``incompatible``).

``run_migrations`` runs *data* migrations for later versions in id-range chunks of 2,000, each
chunk its own short write transaction, at most ~1 s per open after the first chunk, with a resume
cursor and a done marker in ``meta`` (R03.15).
"""

from __future__ import annotations

import contextlib
import logging
import random
import re
import secrets
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Protocol

from indus.foundation.clock import Clock
from indus.foundation.errors import StoreLocked, StoreUnavailable
from indus.state.errors import is_lock_error
from indus.state.writer import blocking_sleep, jitter_pause

__all__ = [
    "INDEXES",
    "INIT_LOCK_SECONDS",
    "MIGRATION_CHUNK",
    "SCHEMA_VERSION",
    "TABLES",
    "TRIGGERS",
    "Column",
    "InitReport",
    "MigrationStep",
    "SchemaExtension",
    "Table",
    "derive_application_id",
    "init",
    "meta_delete",
    "meta_get",
    "meta_set",
    "read_version",
    "run_migrations",
]

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
INIT_LOCK_SECONDS = 20.0
MIGRATION_CHUNK = 2000
MIGRATION_TIME_BOX = 1.0
GENERATION_KEY = "db_file_generation"


@dataclass(frozen=True)
class Column:
    name: str
    decl: str

    @property
    def add_decl(self) -> str:
        """What ``ALTER TABLE … ADD COLUMN`` can take: no PRIMARY KEY / UNIQUE, and NOT NULL
        only with a default (SQLite refuses the rest on an existing table)."""
        decl = re.sub(r"\b(PRIMARY KEY( AUTOINCREMENT)?|UNIQUE)\b", "", self.decl)
        if "NOT NULL" in decl and "DEFAULT" not in decl:
            decl = decl.replace("NOT NULL", "")
        return " ".join(decl.split())


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    constraints: tuple[str, ...] = ()

    def create_sql(self) -> str:
        parts = [f"{c.name} {c.decl}" for c in self.columns] + list(self.constraints)
        body = ",\n  ".join(parts)
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n  {body}\n)"

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


def _cols(*pairs: tuple[str, str]) -> tuple[Column, ...]:
    return tuple(Column(name, decl) for name, decl in pairs)


_COUNTER = "INTEGER NOT NULL DEFAULT 0"
_FLAG = "INTEGER NOT NULL DEFAULT 0"
_TOKENS = (
    ("input_tokens", _COUNTER),
    ("output_tokens", _COUNTER),
    ("cache_read_tokens", _COUNTER),
    ("cache_write_tokens", _COUNTER),
    ("reasoning_tokens", _COUNTER),
)

TABLES: tuple[Table, ...] = (
    Table("schema_version", _cols(("version", "INTEGER NOT NULL"))),
    Table("meta", _cols(("key", "TEXT PRIMARY KEY"), ("value", "TEXT"))),
    Table("system_prompts", _cols(("hash", "TEXT PRIMARY KEY"), ("text", "TEXT NOT NULL"))),
    Table(
        "sessions",
        _cols(
            ("id", "TEXT PRIMARY KEY"),
            ("source", "TEXT NOT NULL"),
            ("created_source", "TEXT"),
            ("user_id", "TEXT"),
            ("model", "TEXT"),
            ("model_config", "TEXT"),
            ("system_prompt_hash", "TEXT REFERENCES system_prompts(hash)"),
            ("parent_session_id", "TEXT REFERENCES sessions(id) ON DELETE SET NULL"),
            ("started_at", "REAL NOT NULL"),
            ("ended_at", "REAL"),
            ("end_reason", "TEXT"),
            ("message_count", _COUNTER),
            ("tool_call_count", _COUNTER),
            ("api_call_count", _COUNTER),
            *_TOKENS,
            ("cwd", "TEXT"),
            ("git_root", "TEXT"),
            ("git_branch", "TEXT"),
            ("billing_provider", "TEXT"),
            ("billing_base_url", "TEXT"),
            ("estimated_cost_usd", "REAL"),
            ("actual_cost_usd", "REAL"),
            ("cost_status", "TEXT"),
            ("title", "TEXT"),
            ("title_source", "TEXT"),
            ("last_activity_at", "REAL"),
            ("last_activity_note", "TEXT"),
            ("last_activity_provenance", "TEXT"),
            ("compression_cooldown_until", "REAL"),
            ("compression_cooldown_error", "TEXT"),
            ("compression_fallback_streak", _COUNTER),
            ("compression_ineffective_count", _COUNTER),
            ("compression_overload_streak", _COUNTER),
            ("profile_id", "TEXT"),
            ("rewind_count", _COUNTER),
            ("archived", _FLAG),
            ("auto_archived", _FLAG),
            ("pinned", _FLAG),
            ("hidden", _FLAG),
            ("last_read_at", "REAL"),
        ),
    ),
    Table(
        "messages",
        _cols(
            ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
            ("session_id", "TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE"),
            ("role", "TEXT NOT NULL"),
            ("content", "TEXT"),
            ("tool_call_id", "TEXT"),
            ("tool_calls", "TEXT"),
            ("tool_name", "TEXT"),
            ("timestamp", "REAL NOT NULL"),
            ("token_count", "INTEGER"),
            ("finish_reason", "TEXT"),
            ("reasoning", "TEXT"),
            ("reasoning_details", "TEXT"),
            ("platform_message_id", "TEXT"),
            ("active", "INTEGER NOT NULL DEFAULT 1"),
            ("compacted", _FLAG),
            ("api_content", "TEXT"),
            ("message_uid", "TEXT"),
            ("tool_call_uids", "TEXT"),
        ),
    ),
    Table(
        "model_usage",
        _cols(
            ("session_id", "TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE"),
            ("model", "TEXT NOT NULL DEFAULT ''"),
            ("billing_provider", "TEXT NOT NULL DEFAULT ''"),
            ("billing_base_url", "TEXT NOT NULL DEFAULT ''"),
            ("task", "TEXT NOT NULL DEFAULT ''"),
            ("api_call_count", _COUNTER),
            *_TOKENS,
            ("estimated_cost_usd", "REAL NOT NULL DEFAULT 0"),
            ("actual_cost_usd", "REAL"),
            ("cost_status", "TEXT"),
            ("first_seen", "REAL"),
            ("last_seen", "REAL"),
        ),
        ("PRIMARY KEY (session_id, model, billing_provider, billing_base_url, task)",),
    ),
    Table(
        "compression_locks",
        _cols(
            ("session_id", "TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE"),
            ("holder", "TEXT NOT NULL"),
            ("acquired_at", "REAL NOT NULL"),
            ("expires_at", "REAL NOT NULL"),
        ),
    ),
    Table(
        "turn_leases",
        _cols(
            ("conversation_id", "TEXT PRIMARY KEY"),
            ("holder", "TEXT NOT NULL"),
            ("acquired_at", "REAL NOT NULL"),
            ("expires_at", "REAL NOT NULL"),
        ),
    ),
    Table(
        "session_meta",
        _cols(
            ("session_id", "TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE"),
            ("namespace", "TEXT NOT NULL"),
            ("key", "TEXT NOT NULL"),
            ("value_json", "TEXT NOT NULL"),
            ("updated_at", "REAL NOT NULL"),
            ("due_at", "REAL"),
        ),
        ("PRIMARY KEY (session_id, namespace, key)",),
    ),
    # P15-07: gateway routing index, session key -> current session (no FK: the route may be
    # written before the service creates the session row).
    Table(
        "gateway_routes",
        _cols(
            ("session_key", "TEXT PRIMARY KEY"),
            ("session_id", "TEXT NOT NULL"),
            ("platform", "TEXT NOT NULL"),
            ("chat_type", "TEXT NOT NULL"),
            ("origin_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("display_name", "TEXT"),
            ("model_override_json", "TEXT"),
            ("resume_pending", _FLAG),
            ("resume_reason", "TEXT"),
            ("active_turn_token", "TEXT"),
            ("active_turn_started_at", "REAL"),
            ("prev_session_id", "TEXT"),
            ("created_at", "REAL NOT NULL"),
            ("updated_at", "REAL NOT NULL"),
        ),
    ),
)

INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_activity ON sessions(last_activity_at)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_prompt ON sessions(system_prompt_hash)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique ON sessions(title) "
    "WHERE title IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_session_time ON messages(session_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_messages_session_active ON messages(session_id, active)",
    "CREATE INDEX IF NOT EXISTS idx_messages_session_uid ON messages(session_id, message_uid)",
    "CREATE INDEX IF NOT EXISTS idx_messages_platform_id "
    "ON messages(session_id, platform_message_id) WHERE platform_message_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_compression_locks_expiry ON compression_locks(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_turn_leases_expiry ON turn_leases(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_model_usage_model ON model_usage(model)",
    "CREATE INDEX IF NOT EXISTS idx_session_meta_due ON session_meta(namespace, due_at) "
    "WHERE due_at IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_gateway_routes_session ON gateway_routes(session_id)",
)

TRIGGERS: tuple[str, ...] = (
    # A row inserted without a uid (an older writer, a raw import) still gets one.
    "CREATE TRIGGER IF NOT EXISTS messages_mint_uid AFTER INSERT ON messages "
    "WHEN new.message_uid IS NULL BEGIN "
    "UPDATE messages SET message_uid = lower(hex(randomblob(16))) WHERE id = new.id; END",
)

_OBJECT_NAME = re.compile(r"IF NOT EXISTS (\w+)")


def _object_names(statements: Sequence[str]) -> tuple[str, ...]:
    names: list[str] = []
    for sql in statements:
        found = _OBJECT_NAME.search(sql)
        if found is not None:
            names.append(found.group(1))
    return tuple(names)


INDEX_NAMES = _object_names(INDEXES)
TRIGGER_NAMES = _object_names(TRIGGERS)


class SchemaExtension(Protocol):
    """Extra DDL run inside the init transaction (P02-14 FTS setup)."""

    name: str

    def needs_work(self, conn: sqlite3.Connection) -> bool: ...

    def apply(self, conn: sqlite3.Connection) -> None: ...


@dataclass
class InitReport:
    fresh: bool = False
    changed: bool = False
    version: int = SCHEMA_VERSION
    added_columns: list[str] = field(default_factory=lambda: [])
    generation: str = ""
    application_id: int = 0


# -- meta helpers -------------------------------------------------------------------------------


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def meta_delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))


def read_version(conn: sqlite3.Connection) -> int | None:
    """The stored schema version; None for an uninitialised store."""
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    return None if row is None else int(row[0])


def derive_application_id(token: str) -> int:
    """A non-zero 31-bit value from the generation token."""
    return (int(token[:8], 16) & 0x7FFFFFFF) or 1


# -- init ---------------------------------------------------------------------------------------


def _live_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _existing_objects(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,)).fetchall()
    return {str(row[0]) for row in rows}


def _application_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA application_id").fetchone()
    return int(row[0] or 0) if row else 0


def _matches(conn: sqlite3.Connection, extensions: Sequence[SchemaExtension]) -> bool:
    """Read-only: does the store already match the declaration (nothing to write)?"""
    version = read_version(conn)
    if version != SCHEMA_VERSION:
        if version is not None and version > SCHEMA_VERSION:
            _refuse_newer(version)
        return False
    tables = _existing_objects(conn, "table")
    for table in TABLES:
        if table.name not in tables:
            return False
        if set(table.column_names) - _live_columns(conn, table.name):
            return False
    if set(INDEX_NAMES) - _existing_objects(conn, "index"):
        return False
    if set(TRIGGER_NAMES) - _existing_objects(conn, "trigger"):
        return False
    if not meta_get(conn, GENERATION_KEY) or _application_id(conn) == 0:
        return False
    return not any(ext.needs_work(conn) for ext in extensions)


def _refuse_newer(version: int) -> None:
    raise StoreUnavailable(
        f"the session store has schema version {version}, newer than this build understands "
        f"({SCHEMA_VERSION})",
        kind="incompatible",
        hint="update Indus Agent, or open a different home",
    )


def _reconcile(conn: sqlite3.Connection) -> list[str]:
    """Add every declared column a live table lacks; returns ``table.column`` names added."""
    added: list[str] = []
    for table in TABLES:
        live = _live_columns(conn, table.name)
        for column in table.columns:
            if column.name in live:
                continue
            try:
                conn.execute(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.add_decl}")
            except sqlite3.OperationalError as exc:
                if is_lock_error(exc):
                    raise
                if "duplicate column" in str(exc).lower():
                    continue
                log.warning(
                    "could not add column %s.%s to the session store: %s",
                    table.name,
                    column.name,
                    exc,
                )
                continue
            added.append(f"{table.name}.{column.name}")
    return added


def _init_once(
    conn: sqlite3.Connection, clock: Clock, extensions: Sequence[SchemaExtension]
) -> InitReport:
    report = InitReport()
    if _matches(conn, extensions):
        report.generation = meta_get(conn, GENERATION_KEY) or ""
        report.application_id = _application_id(conn)
        return report
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = read_version(conn)
        if version is not None and version > SCHEMA_VERSION:
            _refuse_newer(version)
        for table in TABLES:
            conn.execute(table.create_sql())
        report.added_columns = _reconcile(conn)
        for sql in (*INDEXES, *TRIGGERS):
            conn.execute(sql)
        if version is None:
            report.fresh = True
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            now = clock.now().astimezone(UTC).isoformat()
            conn.executemany(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                [("store_id", uuid.uuid4().hex), ("created_at", now)],
            )
        token = meta_get(conn, GENERATION_KEY)
        if not token:
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                (GENERATION_KEY, secrets.token_hex(16)),
            )
            token = meta_get(conn, GENERATION_KEY) or ""
        app_id = _application_id(conn)
        if app_id == 0 and token:
            app_id = derive_application_id(token)
            conn.execute(f"PRAGMA application_id={app_id}")
        for ext in extensions:
            if ext.needs_work(conn):
                ext.apply(conn)
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    report.changed = True
    report.version = SCHEMA_VERSION
    report.generation = token
    report.application_id = app_id
    return report


def init(
    conn: sqlite3.Connection,
    *,
    clock: Clock,
    sleep: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
    extensions: Sequence[SchemaExtension] = (),
    patience: float = INIT_LOCK_SECONDS,
) -> InitReport:
    """Bring the store to schema 1, waiting out a sibling's lock for up to ``patience`` s."""
    pause_for = sleep if sleep is not None else blocking_sleep(clock)
    jitter = rng if rng is not None else random.Random()
    started = clock.monotonic()
    deadline = started + patience
    while True:
        try:
            return _init_once(conn, clock, extensions)
        except sqlite3.OperationalError as exc:
            if not is_lock_error(exc):
                raise
            pause = jitter_pause(
                now=clock.monotonic(),
                started=started,
                deadline=deadline,
                rng=jitter,
                slow_only=True,
            )
            if pause is None:
                log.error(
                    "setting up the session store schema waited %.0fs for another process's "
                    "write lock and gave up",
                    patience,
                )
                raise StoreLocked(
                    f"the session store stayed busy for {patience:g}s while it was being opened; "
                    "this is contention, the stored data is fine",
                    hint="close other Indus processes using this profile, then try again",
                ) from exc
            pause_for(pause)


# -- data migrations ----------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationStep:
    """A data migration over an id range: ``run_chunk(conn, after, upto)`` handles
    ``after < id <= upto``; ``high_water(conn)`` is the largest id to cover."""

    name: str
    high_water: Callable[[sqlite3.Connection], int]
    run_chunk: Callable[[sqlite3.Connection, int, int], None]
    chunk: int = MIGRATION_CHUNK

    @property
    def cursor_key(self) -> str:
        return f"migration:{self.name}:cursor"

    @property
    def done_key(self) -> str:
        return f"migration:{self.name}:done"


class Writes(Protocol):
    """``Writer.write`` (or anything that runs a callback in one write transaction)."""

    def __call__[T](self, fn: Callable[[sqlite3.Connection], T], /) -> T: ...


def _migration_chunk(step: MigrationStep) -> Callable[[sqlite3.Connection], bool]:
    def run(conn: sqlite3.Connection) -> bool:
        if meta_get(conn, step.done_key):
            return True
        after = int(meta_get(conn, step.cursor_key) or 0)
        high = int(step.high_water(conn) or 0)
        if after < high:
            upto = after + step.chunk
            step.run_chunk(conn, after, upto)
            after = upto
        if after >= high:
            meta_set(conn, step.done_key, "1")
            meta_delete(conn, step.cursor_key)
            return True
        meta_set(conn, step.cursor_key, str(after))
        return False

    return run


def run_migrations(
    write: Writes,
    steps: Sequence[MigrationStep],
    *,
    clock: Clock,
    time_box: float = MIGRATION_TIME_BOX,
) -> dict[str, bool]:
    """Advance each step; returns ``{name: done}``. Each chunk is its own write transaction."""
    results: dict[str, bool] = {}
    for step in steps:
        chunk = _migration_chunk(step)
        first_done_at: float | None = None
        while True:
            done = write(chunk)
            if done:
                results[step.name] = True
                break
            now = clock.monotonic()
            if first_done_at is None:
                first_done_at = now
            if now - first_done_at >= time_box:
                results[step.name] = False
                log.info("data migration %s paused; it continues on the next open", step.name)
                break
    return results
