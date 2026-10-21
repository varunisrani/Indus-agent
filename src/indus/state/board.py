"""The task board: one SQLite file per board, shared by every profile under an Indus root (P24-11).

A board lives at ``<root>/boards/<slug>.db`` (default board ``default``); its attachments, worker
logs and scratch workspaces live under ``<root>/boards/<slug>/``. It is never the profile's session
store. Every change runs through one ``Writer`` (``BEGIN IMMEDIATE``, retries with a deadline,
R03.8) and appends an event row, so watchers and the CLI can replay what happened.

Statuses: ``todo`` (waits on an unfinished parent or was parked behind one), ``ready``
(dispatchable), ``running`` (a worker run holds a claim), ``blocked`` (needs a person, or parked by
``schedule``), ``review`` (waits for a reviewer), ``done``, ``archived``. A new task is ``ready``
when every parent is done or archived, else ``todo``. Only a worker creating a task may start it
``running`` (claimed by that worker in the same transaction) or ``blocked``.

Claims last ``CLAIM_TTL_S`` (15 min) unless the worker heartbeats. ``release_stale_claims`` extends
an expired claim whose worker is alive and heartbeated within ``HEARTBEAT_STALE_S`` (60 min);
otherwise it terminates the worker and releases the task, unless the worker survives termination,
in which case the claim is kept for ``RECLAIM_GRACE_S`` (120 s) so no duplicate starts beside it.
A released claim counts as a failure; ``FAILURE_LIMIT`` failures in a row block the task.

Block kinds: ``dependency`` waits in ``todo`` while a parent is open (with no open parent it is
recorded as ``needs_input``); the other kinds count how often the same kind recurs (kept across
unblock, cleared only by a completion). The third block of the same kind in a row escalates: the
task stays ``blocked`` with ``escalated`` set and a ``block_loop`` event, for a person to look at.
``schedule`` parks a task as ``blocked`` with ``scheduled`` set (and an optional wake time).
``unblock`` sends a task to ``ready`` when every parent is done, else ``todo`` (``review`` when the
block interrupted a reviewer run), and resets the failure count.

Attachments are capped at ``ATTACHMENT_MAX_BYTES`` (25 MiB) for every surface; file names are cut to
a safe base name and never overwrite each other.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from indus.foundation.clock import Clock
from indus.foundation.errors import IndusError
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.state import journal
from indus.state.connection import open_store
from indus.state.writer import Writer

__all__ = [
    "ATTACHMENT_MAX_BYTES",
    "BLOCK_KINDS",
    "BLOCK_RECURRENCE_LIMIT",
    "BOARD_ENV",
    "BOARDS_DIR",
    "CLAIM_TTL_S",
    "CRASH_GRACE_S",
    "DEFAULT_BOARD",
    "FAILURE_LIMIT",
    "HEARTBEAT_STALE_S",
    "RECLAIM_GRACE_S",
    "STATUSES",
    "WORKSPACE_KINDS",
    "Attachment",
    "AttachmentTooLarge",
    "BoardError",
    "BoardInputError",
    "BoardRefused",
    "BoardStore",
    "Comment",
    "LiveClaim",
    "Run",
    "Subscription",
    "Task",
    "TaskEvent",
    "TaskNotFound",
    "board_dir",
    "board_path",
    "boards_dir",
    "current_board",
    "list_boards",
    "normalize_slug",
    "set_current_board",
]

BOARDS_DIR = "boards"
DEFAULT_BOARD = "default"
CURRENT_FILE = "current"
STATUSES: tuple[str, ...] = ("todo", "ready", "running", "blocked", "review", "done", "archived")
BLOCK_KINDS: tuple[str, ...] = ("dependency", "needs_input", "capability", "transient")
WORKSPACE_KINDS: tuple[str, ...] = ("scratch", "worktree", "dir")
INITIAL_STATUSES: tuple[str, ...] = ("running", "blocked")
BLOCK_RECURRENCE_LIMIT = 2
ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
CLAIM_TTL_S = 15 * 60
HEARTBEAT_STALE_S = 60 * 60
RECLAIM_GRACE_S = 120
CRASH_GRACE_S = 30
FAILURE_LIMIT = 2
SCHEMA_VERSION = 1
# The pins a dispatched worker gets in its environment (the board tools read them from the
# scope's env view to know the task, run and claim they act for).
BOARD_ENV: dict[str, str] = {
    "task": "INDUS_BOARD_TASK",
    "run": "INDUS_BOARD_RUN",
    "claim": "INDUS_BOARD_CLAIM",
    "slug": "INDUS_BOARD_SLUG",
    "root": "INDUS_BOARD_ROOT",
}

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DONE = ("done", "archived")
_NAME_MAX = 200

# -- errors -------------------------------------------------------------------------------------


class BoardError(IndusError):
    default_code = "board.error"


class TaskNotFound(BoardError):
    default_code = "board.not_found"


class BoardInputError(BoardError, ValueError):
    default_code = "board.bad_input"


class BoardRefused(BoardError):
    default_code = "board.refused"


class LiveClaim(BoardRefused):
    default_code = "board.live_claim"


class AttachmentTooLarge(BoardInputError):
    default_code = "board.too_large"


# -- paths --------------------------------------------------------------------------------------


def normalize_slug(slug: str | None) -> str:
    """A board slug: lowercase letters, digits, ``-`` and ``_``, 1-64 characters."""
    text = (slug or "").strip().lower() or DEFAULT_BOARD
    if not _SLUG.match(text):
        raise BoardInputError(
            f"board name {slug!r} is not usable: 1-64 lowercase letters, digits, '-' or '_', "
            "starting with a letter or digit"
        )
    return text


def boards_dir(root: Path) -> Path:
    return root / BOARDS_DIR


def board_path(root: Path, slug: str) -> Path:
    return boards_dir(root) / f"{normalize_slug(slug)}.db"


def board_dir(root: Path, slug: str) -> Path:
    """The folder of a board's files: ``attachments/``, ``logs/``, ``workspaces/``."""
    return boards_dir(root) / normalize_slug(slug)


def list_boards(root: Path) -> list[str]:
    """Slugs of the boards that exist under ``root`` (the default board always counts)."""
    found = {DEFAULT_BOARD}
    folder = boards_dir(root)
    if folder.is_dir():
        for path in folder.glob("*.db"):
            if _SLUG.match(path.stem):
                found.add(path.stem)
    return sorted(found)


def current_board(root: Path) -> str:
    """The board commands use when none is named (``<root>/boards/current``), else ``default``."""
    try:
        text = (boards_dir(root) / CURRENT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_BOARD
    try:
        return normalize_slug(text)
    except BoardInputError:
        return DEFAULT_BOARD


def set_current_board(root: Path, slug: str) -> str:
    chosen = normalize_slug(slug)
    folder = boards_dir(root)
    secure_dir(folder)
    (folder / CURRENT_FILE).write_text(chosen + "\n", encoding="utf-8")
    return chosen


# -- rows ---------------------------------------------------------------------------------------


def _json(value: object) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, ensure_ascii=False)


def _field(blob: object, key: str) -> object:
    """``blob[key]`` when ``blob`` is a JSON object, else None."""
    if isinstance(blob, dict):
        return cast(dict[str, object], blob).get(key)
    return None


def _unjson(value: object) -> Any:
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    body: str | None
    assignee: str | None
    status: str
    priority: int
    created_by: str | None
    created_at: int
    started_at: int | None
    completed_at: int | None
    workspace_kind: str
    workspace_path: str | None
    project_id: str | None
    claim_lock: str | None
    claim_expires: int | None
    worker_pid: int | None
    worker_fingerprint: str | None
    last_heartbeat_at: int | None
    current_run_id: int | None
    consecutive_failures: int
    last_failure_error: str | None
    protocol_violations: int
    block_kind: str | None
    block_recurrences: int
    escalated: bool
    scheduled: bool
    wake_at: int | None
    result: str | None
    idempotency_key: str | None
    max_runtime_s: int | None
    session_id: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        return cls(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            assignee=row["assignee"],
            status=row["status"],
            priority=int(row["priority"] or 0),
            created_by=row["created_by"],
            created_at=int(row["created_at"]),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            workspace_kind=row["workspace_kind"],
            workspace_path=row["workspace_path"],
            project_id=row["project_id"],
            claim_lock=row["claim_lock"],
            claim_expires=row["claim_expires"],
            worker_pid=row["worker_pid"],
            worker_fingerprint=row["worker_fingerprint"],
            last_heartbeat_at=row["last_heartbeat_at"],
            current_run_id=row["current_run_id"],
            consecutive_failures=int(row["consecutive_failures"] or 0),
            last_failure_error=row["last_failure_error"],
            protocol_violations=int(row["protocol_violations"] or 0),
            block_kind=row["block_kind"],
            block_recurrences=int(row["block_recurrences"] or 0),
            escalated=bool(row["escalated"]),
            scheduled=bool(row["scheduled"]),
            wake_at=row["wake_at"],
            result=row["result"],
            idempotency_key=row["idempotency_key"],
            max_runtime_s=row["max_runtime_s"],
            session_id=row["session_id"],
        )

    def view(self) -> dict[str, Any]:
        """The JSON shape tools, the CLI and watchers show."""
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "assignee": self.assignee,
            "status": self.status,
            "priority": self.priority,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "workspace_kind": self.workspace_kind,
            "workspace_path": self.workspace_path,
            "project_id": self.project_id,
            "current_run_id": self.current_run_id,
            "consecutive_failures": self.consecutive_failures,
            "last_failure_error": self.last_failure_error,
            "block_kind": self.block_kind,
            "block_recurrences": self.block_recurrences,
            "escalated": self.escalated,
            "scheduled": self.scheduled,
            "wake_at": self.wake_at,
            "result": self.result,
        }


@dataclass(frozen=True)
class Run:
    id: int
    task_id: str
    profile: str | None
    source: str
    status: str
    claim_lock: str | None
    claim_expires: int | None
    worker_pid: int | None
    worker_fingerprint: str | None
    last_heartbeat_at: int | None
    started_at: int
    ended_at: int | None
    outcome: str | None
    summary: str | None
    metadata: Any
    error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Run:
        return cls(
            id=int(row["id"]),
            task_id=row["task_id"],
            profile=row["profile"],
            source=row["source"],
            status=row["status"],
            claim_lock=row["claim_lock"],
            claim_expires=row["claim_expires"],
            worker_pid=row["worker_pid"],
            worker_fingerprint=row["worker_fingerprint"],
            last_heartbeat_at=row["last_heartbeat_at"],
            started_at=int(row["started_at"]),
            ended_at=row["ended_at"],
            outcome=row["outcome"],
            summary=row["summary"],
            metadata=_unjson(row["metadata"]),
            error=row["error"],
        )

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile": self.profile,
            "source": self.source,
            "status": self.status,
            "outcome": self.outcome,
            "summary": self.summary,
            "metadata": self.metadata,
            "error": self.error,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


@dataclass(frozen=True)
class TaskEvent:
    id: int
    task_id: str
    run_id: int | None
    kind: str
    payload: Any
    created_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TaskEvent:
        return cls(
            id=int(row["id"]),
            task_id=row["task_id"],
            run_id=row["run_id"],
            kind=row["kind"],
            payload=_unjson(row["payload"]),
            created_at=int(row["created_at"]),
        )

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "payload": self.payload,
            "run_id": self.run_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Comment:
    """A note on a task; comments are how tasks and people pass context along."""

    id: int
    author: str
    body: str
    created_at: int
    task_id: str

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "author": self.author,
            "body": self.body,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Attachment:
    id: int
    task_id: str
    filename: str
    stored_path: str
    content_type: str | None
    size: int
    uploaded_by: str | None
    created_at: int

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "path": self.stored_path,
            "content_type": self.content_type,
            "size": self.size,
            "uploaded_by": self.uploaded_by,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Subscription:
    """A chat that hears about a task's outcomes (``cursor``: the last event id delivered)."""

    task_id: str
    platform: str
    chat_id: str
    thread_id: str
    cursor: int
    failures: int
    profile: str | None


# -- schema -------------------------------------------------------------------------------------

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        body TEXT,
        assignee TEXT,
        status TEXT NOT NULL CHECK (status IN
            ('todo', 'ready', 'running', 'blocked', 'review', 'done', 'archived')),
        priority INTEGER NOT NULL DEFAULT 0,
        created_by TEXT,
        created_at INTEGER NOT NULL,
        started_at INTEGER,
        completed_at INTEGER,
        workspace_kind TEXT NOT NULL DEFAULT 'scratch'
            CHECK (workspace_kind IN ('scratch', 'worktree', 'dir')),
        workspace_path TEXT,
        project_id TEXT,
        claim_lock TEXT,
        claim_expires INTEGER,
        worker_pid INTEGER,
        worker_fingerprint TEXT,
        last_heartbeat_at INTEGER,
        current_run_id INTEGER,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_failure_error TEXT,
        protocol_violations INTEGER NOT NULL DEFAULT 0,
        block_kind TEXT,
        block_recurrences INTEGER NOT NULL DEFAULT 0,
        escalated INTEGER NOT NULL DEFAULT 0,
        scheduled INTEGER NOT NULL DEFAULT 0,
        wake_at INTEGER,
        result TEXT,
        idempotency_key TEXT,
        max_runtime_s INTEGER,
        session_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS links (
        parent_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        PRIMARY KEY (parent_id, child_id)
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        profile TEXT,
        source TEXT NOT NULL DEFAULT 'implement' CHECK (source IN ('implement', 'review')),
        status TEXT NOT NULL,
        claim_lock TEXT,
        claim_expires INTEGER,
        worker_pid INTEGER,
        worker_fingerprint TEXT,
        last_heartbeat_at INTEGER,
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        outcome TEXT,
        summary TEXT,
        metadata TEXT,
        error TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        run_id INTEGER,
        kind TEXT NOT NULL,
        payload TEXT,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        author TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        filename TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        content_type TEXT,
        size INTEGER NOT NULL DEFAULT 0,
        uploaded_by TEXT,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS subscriptions (
        task_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        thread_id TEXT NOT NULL DEFAULT '',
        profile TEXT,
        cursor INTEGER NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (task_id, platform, chat_id, thread_id)
    )""",
    """CREATE TABLE IF NOT EXISTS lanes (
        profile TEXT PRIMARY KEY,
        cooled_until INTEGER NOT NULL,
        reason TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS tasks_status ON tasks (status)",
    "CREATE INDEX IF NOT EXISTS tasks_idem ON tasks (idempotency_key)",
    "CREATE INDEX IF NOT EXISTS links_child ON links (child_id)",
    "CREATE INDEX IF NOT EXISTS runs_task ON runs (task_id, id)",
    "CREATE INDEX IF NOT EXISTS events_task ON events (task_id, id)",
    "CREATE INDEX IF NOT EXISTS comments_task ON comments (task_id, id)",
    "CREATE INDEX IF NOT EXISTS attachments_task ON attachments (task_id, id)",
)

# -- helpers used inside write callbacks ----------------------------------------------------------


def _fetch(conn: sqlite3.Connection, task_id: str) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row is not None else None


def _require(conn: sqlite3.Connection, task_id: str) -> Task:
    task = _fetch(conn, task_id)
    if task is None:
        raise TaskNotFound(f"no task {task_id!r} on this board")
    return task


def _event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: Mapping[str, Any] | None,
    now: int,
    run_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, _json(dict(payload)) if payload else None, now),
    )
    return int(cur.lastrowid or 0)


def _open_parents(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT t.id, t.status FROM links l JOIN tasks t ON t.id = l.parent_id "
        "WHERE l.child_id = ? AND t.status NOT IN ('done', 'archived') ORDER BY t.id",
        (task_id,),
    ).fetchall()
    return [(r["id"], r["status"]) for r in rows]


def _landing(conn: sqlite3.Connection, task_id: str) -> str:
    return "todo" if _open_parents(conn, task_id) else "ready"


def _run_source(conn: sqlite3.Connection, run_id: int | None) -> str:
    if run_id is None:
        return "implement"
    row = conn.execute("SELECT source FROM runs WHERE id = ?", (run_id,)).fetchone()
    return str(row["source"]) if row is not None else "implement"


def _retry_status(conn: sqlite3.Connection, task: Task) -> str:
    """Where a released run sends its task: back to ``review`` for a reviewer run."""
    return "review" if _run_source(conn, task.current_run_id) == "review" else "ready"


def _end_run(
    conn: sqlite3.Connection,
    task: Task,
    *,
    outcome: str,
    status: str,
    now: int,
    summary: str | None = None,
    metadata: object = None,
    error: str | None = None,
) -> int | None:
    run_id = task.current_run_id
    if run_id is None:
        if summary is None and metadata is None:
            return None
        # Never claimed (a person moved it): keep the handoff on a run row of its own.
        cur = conn.execute(
            "INSERT INTO runs (task_id, profile, source, status, started_at, ended_at, outcome, "
            "summary, metadata, error) VALUES (?, ?, 'implement', ?, ?, ?, ?, ?, ?, ?)",
            (task.id, task.assignee, status, now, now, outcome, summary, _json(metadata), error),
        )
        return int(cur.lastrowid or 0)
    conn.execute(
        "UPDATE runs SET status = ?, outcome = ?, ended_at = ?, summary = COALESCE(?, summary), "
        "metadata = COALESCE(?, metadata), error = COALESCE(?, error), claim_lock = NULL, "
        "claim_expires = NULL WHERE id = ? AND ended_at IS NULL",
        (status, outcome, now, summary, _json(metadata), error, run_id),
    )
    return run_id


def _clear_claim_sql(status: str) -> str:
    return (
        f"UPDATE tasks SET status = '{status}', claim_lock = NULL, claim_expires = NULL, "
        "worker_pid = NULL, worker_fingerprint = NULL, current_run_id = NULL"
    )


def _would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """True when ``child_id`` is ``parent_id`` or one of its ancestors."""
    seen: set[str] = set()
    stack = [parent_id]
    while stack:
        here = stack.pop()
        if here == child_id:
            return True
        if here in seen:
            continue
        seen.add(here)
        stack.extend(
            r["parent_id"]
            for r in conn.execute("SELECT parent_id FROM links WHERE child_id = ?", (here,))
        )
    return False


def _safe_name(raw: str) -> str:
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip().lstrip(".").strip()
    if not name:
        raise BoardInputError("the attachment needs a file name")
    return name[:_NAME_MAX]


def _free_path(folder: Path, name: str) -> Path:
    stem, dot, ext = name.partition(".")
    candidate, n = name, 1
    while (folder / candidate).exists():
        candidate = f"{stem} ({n}){dot}{ext}"
        n += 1
    return folder / candidate


# -- the store ----------------------------------------------------------------------------------


class BoardStore:
    """One board file and its writer. Open with ``BoardStore(scope, slug)``; ``close()`` after."""

    def __init__(
        self,
        scope: Scope,
        slug: str | None = None,
        *,
        root: Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.scope = scope
        self.slug = normalize_slug(slug)
        self.root = root if root is not None else scope.root
        self.clock: Clock = clock if clock is not None else scope.clock
        self.dir = board_dir(self.root, self.slug)
        secure_dir(boards_dir(self.root), env=scope.env)
        handle = open_store(scope, board_path(self.root, self.slug), on_connected=self._setup)
        self.path = handle.path
        self.writer = Writer(self.path, handle.conn, clock=self.clock)

    def _setup(self, conn: sqlite3.Connection) -> None:
        cfg = journal.database_config(self.scope)
        journal.apply(conn, cfg, board_path(self.root, self.slug))
        journal.apply_pragmas(conn, cfg)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.writer.close()

    def __enter__(self) -> BoardStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- plumbing -----------------------------------------------------------------------------

    def now(self) -> int:
        return int(self.clock.now().timestamp())

    def _read[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self.writer.borrow() as conn:
            return fn(conn)

    def _write[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return self.writer.write(fn)

    def attachments_dir(self, task_id: str) -> Path:
        return self.dir / "attachments" / task_id

    def logs_dir(self) -> Path:
        return self.dir / "logs"

    def workspaces_dir(self) -> Path:
        return self.dir / "workspaces"

    # -- reads --------------------------------------------------------------------------------

    def get(self, task_id: str) -> Task | None:
        return self._read(lambda conn: _fetch(conn, task_id))

    def require(self, task_id: str) -> Task:
        return self._read(lambda conn: _require(conn, task_id))

    def list(
        self,
        *,
        status: str | None = None,
        assignee: str | None = None,
        include_archived: bool = False,
        limit: int | None = None,
    ) -> list[Task]:
        if status is not None and status not in STATUSES:
            raise BoardInputError(f"status must be one of {', '.join(STATUSES)}")
        where: list[str] = []
        params: list[Any] = []
        if status is not None:
            where.append("status = ?")
            params.append(status)
        elif not include_archived:
            where.append("status != 'archived'")
        if assignee is not None:
            where.append("assignee = ?")
            params.append(assignee)
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY priority DESC, created_at ASC, id ASC"
        if limit is not None:
            sql += f" LIMIT {max(int(limit), 0)}"
        return self._read(lambda conn: [Task.from_row(r) for r in conn.execute(sql, params)])

    def parents(self, task_id: str) -> list[str]:
        sql = "SELECT parent_id FROM links WHERE child_id = ? ORDER BY parent_id"
        return self._read(lambda conn: [r[0] for r in conn.execute(sql, (task_id,))])

    def children(self, task_id: str) -> list[str]:
        sql = "SELECT child_id FROM links WHERE parent_id = ? ORDER BY child_id"
        return self._read(lambda conn: [r[0] for r in conn.execute(sql, (task_id,))])

    def unsatisfied_parents(self, task_id: str) -> list[tuple[str, str]]:
        return self._read(lambda conn: _open_parents(conn, task_id))

    def runs(self, task_id: str) -> list[Run]:
        sql = "SELECT * FROM runs WHERE task_id = ? ORDER BY id"
        return self._read(lambda conn: [Run.from_row(r) for r in conn.execute(sql, (task_id,))])

    def run(self, run_id: int) -> Run | None:
        def fn(conn: sqlite3.Connection) -> Run | None:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return Run.from_row(row) if row is not None else None

        return self._read(fn)

    def latest_run(self, task_id: str) -> Run | None:
        found = self.runs(task_id)
        return found[-1] if found else None

    def events(self, task_id: str, *, limit: int | None = None) -> list[TaskEvent]:
        sql = "SELECT * FROM events WHERE task_id = ? ORDER BY id"
        rows = self._read(
            lambda conn: [TaskEvent.from_row(r) for r in conn.execute(sql, (task_id,))]
        )
        return rows[-limit:] if limit else rows

    def events_after(
        self, cursor: int, *, task_id: str | None = None, kinds: Iterable[str] | None = None
    ) -> list[TaskEvent]:
        wanted = tuple(kinds) if kinds is not None else None
        sql = "SELECT * FROM events WHERE id > ?"
        params: list[Any] = [cursor]
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        if wanted:
            sql += f" AND kind IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)
        sql += " ORDER BY id LIMIT 500"
        return self._read(lambda conn: [TaskEvent.from_row(r) for r in conn.execute(sql, params)])

    def comments(self, task_id: str) -> list[Comment]:
        def fn(conn: sqlite3.Connection) -> list[Comment]:
            rows = conn.execute(
                "SELECT * FROM comments WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
            return [
                Comment(
                    id=r["id"],
                    author=r["author"],
                    body=r["body"],
                    created_at=r["created_at"],
                    task_id=r["task_id"],
                )
                for r in rows
            ]

        return self._read(fn)

    def attachments(self, task_id: str) -> list[Attachment]:
        def fn(conn: sqlite3.Connection) -> list[Attachment]:
            rows = conn.execute(
                "SELECT * FROM attachments WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
            return [
                Attachment(
                    r["id"],
                    r["task_id"],
                    r["filename"],
                    r["stored_path"],
                    r["content_type"],
                    int(r["size"] or 0),
                    r["uploaded_by"],
                    r["created_at"],
                )
                for r in rows
            ]

        return self._read(fn)

    def count_running(self) -> int:
        sql = "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
        return int(self._read(lambda conn: conn.execute(sql).fetchone()[0]))

    def counts(self) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        rows = self._read(lambda conn: conn.execute(sql).fetchall())
        found = {r["status"]: int(r["n"]) for r in rows}
        return {s: found.get(s, 0) for s in STATUSES}

    def lane_cooled_until(self, profile: str) -> int | None:
        sql = "SELECT cooled_until FROM lanes WHERE profile = ?"
        row = self._read(lambda conn: conn.execute(sql, (profile,)).fetchone())
        if row is None or int(row[0]) <= self.now():
            return None
        return int(row[0])

    def subscriptions(self, task_id: str | None = None) -> list[Subscription]:
        sql = "SELECT * FROM subscriptions" + (" WHERE task_id = ?" if task_id else "")
        sql += " ORDER BY created_at, task_id"
        params = (task_id,) if task_id else ()
        rows = self._read(lambda conn: conn.execute(sql, params).fetchall())
        return [
            Subscription(
                r["task_id"],
                r["platform"],
                r["chat_id"],
                r["thread_id"],
                int(r["cursor"]),
                int(r["failures"]),
                r["profile"],
            )
            for r in rows
        ]

    # -- creation and edits -------------------------------------------------------------------

    def create(
        self,
        title: str,
        *,
        body: str | None = None,
        assignee: str | None = None,
        created_by: str | None = None,
        parents: Sequence[str] = (),
        priority: int = 0,
        workspace_kind: str = "scratch",
        workspace_path: str | None = None,
        project_id: str | None = None,
        idempotency_key: str | None = None,
        max_runtime_s: int | None = None,
        session_id: str | None = None,
        initial_status: str | None = None,
        by_worker: bool = False,
        claimer: str | None = None,
    ) -> Task:
        """A new task (see the module notes for its first status)."""
        clean_title = (title or "").strip()
        if not clean_title:
            raise BoardInputError("a task needs a title")
        if workspace_kind not in WORKSPACE_KINDS:
            raise BoardInputError(f"workspace kind must be one of {', '.join(WORKSPACE_KINDS)}")
        if workspace_kind == "dir" and not workspace_path:
            raise BoardInputError("a 'dir' workspace needs a path")
        if initial_status is not None:
            if initial_status not in INITIAL_STATUSES:
                raise BoardInputError("initial status must be 'running' or 'blocked'")
            if not by_worker:
                raise BoardInputError(
                    "only a worker may create a task that starts running or blocked; "
                    "people and orchestrators create ready (or waiting) tasks"
                )
            if initial_status == "running" and not claimer:
                raise BoardInputError("a task created running needs the creating worker's claim")
        wanted_parents = list(dict.fromkeys(p for p in parents if p))
        now = self.now()

        def fn(conn: sqlite3.Connection) -> Task:
            if idempotency_key:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    return Task.from_row(row)
            missing = [p for p in wanted_parents if _fetch(conn, p) is None]
            if missing:
                raise TaskNotFound(f"unknown parent task(s): {', '.join(missing)}")
            task_id = "t_" + secrets.token_hex(4)
            while _fetch(conn, task_id) is not None:
                task_id = "t_" + secrets.token_hex(4)
            open_parent = any(
                (p := _fetch(conn, pid)) is not None and p.status not in _DONE
                for pid in wanted_parents
            )
            status = "todo" if open_parent else "ready"
            if initial_status == "blocked":
                status = "blocked"
            conn.execute(
                "INSERT INTO tasks (id, title, body, assignee, status, priority, created_by, "
                "created_at, workspace_kind, workspace_path, project_id, idempotency_key, "
                "max_runtime_s, session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    clean_title,
                    body,
                    assignee or None,
                    status,
                    int(priority),
                    created_by,
                    now,
                    workspace_kind,
                    workspace_path,
                    project_id,
                    idempotency_key,
                    max_runtime_s,
                    session_id,
                ),
            )
            for pid in wanted_parents:
                conn.execute(
                    "INSERT OR IGNORE INTO links (parent_id, child_id) VALUES (?, ?)",
                    (pid, task_id),
                )
            _event(
                conn,
                task_id,
                "created",
                {"status": status, "assignee": assignee, "parents": wanted_parents},
                now,
            )
            if status == "todo":
                waiting = next(p for p in wanted_parents if _fetch(conn, p).status not in _DONE)  # type: ignore[union-attr]
                _event(conn, task_id, "dependency_wait", {"parent": waiting}, now)
            if status == "blocked":
                _event(conn, task_id, "blocked", {"reason": "created blocked", "kind": None}, now)
            if initial_status == "running" and claimer:
                if open_parent:
                    raise BoardRefused("a task with an unfinished parent cannot start running")
                self._claim_locked(conn, task_id, "ready", claimer, CLAIM_TTL_S, now)
            return _require(conn, task_id)

        return self._write(fn)

    def assign(self, task_id: str, profile: str | None) -> Task:
        now = self.now()

        def fn(conn: sqlite3.Connection) -> Task:
            task = _require(conn, task_id)
            if task.status == "running":
                raise BoardRefused(
                    f"{task_id} is running; reclaim it before giving it to someone else"
                )
            conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (profile or None, task_id))
            _event(conn, task_id, "assigned", {"assignee": profile or None}, now)
            return _require(conn, task_id)

        return self._write(fn)

    def link(self, parent_id: str, child_id: str) -> bool:
        """Add a dependency; True when the child now waits on the parent."""
        if parent_id == child_id:
            raise BoardInputError("a task cannot depend on itself")
        now = self.now()

        def fn(conn: sqlite3.Connection) -> bool:
            parent = _require(conn, parent_id)
            child = _require(conn, child_id)
            if _would_cycle(conn, parent_id, child_id):
                raise BoardInputError(f"linking {parent_id} → {child_id} would make a cycle")
            conn.execute(
                "INSERT OR IGNORE INTO links (parent_id, child_id) VALUES (?, ?)",
                (parent_id, child_id),
            )
            _event(conn, child_id, "linked", {"parent": parent_id}, now)
            gated = parent.status not in _DONE and child.status in ("ready", "todo")
            if gated and child.status == "ready":
                conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child_id,))
                _event(conn, child_id, "dependency_wait", {"parent": parent_id}, now)
            return gated

        return self._write(fn)

    def comment(self, task_id: str, author: str, body: str) -> int:
        text = (body or "").strip()
        if not text:
            raise BoardInputError("a comment needs text")
        now = self.now()

        def fn(conn: sqlite3.Connection) -> int:
            _require(conn, task_id)
            cur = conn.execute(
                "INSERT INTO comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (task_id, author or "unknown", text, now),
            )
            _event(conn, task_id, "commented", {"author": author}, now)
            return int(cur.lastrowid or 0)

        return self._write(fn)

    def attach_bytes(
        self,
        task_id: str,
        filename: str,
        data: bytes,
        *,
        content_type: str | None = None,
        uploaded_by: str | None = None,
    ) -> Attachment:
        """Store a file for a task (the one path every surface uses)."""
        if len(data) > ATTACHMENT_MAX_BYTES:
            raise AttachmentTooLarge(
                f"the attachment is {len(data)} bytes; the limit is "
                f"{ATTACHMENT_MAX_BYTES // (1024 * 1024)} MiB"
            )
        name = _safe_name(filename)
        self.require(task_id)
        folder = self.attachments_dir(task_id)
        secure_dir(folder, env=self.scope.env)
        target = _free_path(folder, name)
        target.write_bytes(data)
        now = self.now()

        def fn(conn: sqlite3.Connection) -> Attachment:
            _require(conn, task_id)
            cur = conn.execute(
                "INSERT INTO attachments (task_id, filename, stored_path, content_type, size, "
                "uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, target.name, str(target), content_type, len(data), uploaded_by, now),
            )
            _event(conn, task_id, "attached", {"filename": target.name, "size": len(data)}, now)
            return Attachment(
                int(cur.lastrowid or 0),
                task_id,
                target.name,
                str(target),
                content_type,
                len(data),
                uploaded_by,
                now,
            )

        try:
            return self._write(fn)
        except BaseException:
            with contextlib.suppress(OSError):
                target.unlink()
            raise

    def subscribe(
        self,
        task_id: str,
        *,
        platform: str,
        chat_id: str,
        thread_id: str = "",
        profile: str | None = None,
    ) -> bool:
        """Have a chat hear this task's outcomes; True when the subscription is new."""
        now = self.now()

        def fn(conn: sqlite3.Connection) -> bool:
            _require(conn, task_id)
            last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            cur = conn.execute(
                "INSERT OR IGNORE INTO subscriptions (task_id, platform, chat_id, thread_id, "
                "profile, cursor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, platform, chat_id, thread_id or "", profile, int(last), now),
            )
            return cur.rowcount == 1

        return self._write(fn)

    def unsubscribe(self, sub: Subscription) -> None:
        self._write(
            lambda conn: conn.execute(
                "DELETE FROM subscriptions WHERE task_id = ? AND platform = ? AND chat_id = ? "
                "AND thread_id = ?",
                (sub.task_id, sub.platform, sub.chat_id, sub.thread_id),
            )
        )

    def move_cursor(self, sub: Subscription, cursor: int, *, failed: bool = False) -> None:
        """Set a subscription's cursor; ``failed`` counts a failed send instead of clearing."""
        failures = "failures + 1" if failed else "0"
        self._write(
            lambda conn: conn.execute(
                f"UPDATE subscriptions SET cursor = ?, failures = {failures} WHERE task_id = ? "
                "AND platform = ? AND chat_id = ? AND thread_id = ?",
                (cursor, sub.task_id, sub.platform, sub.chat_id, sub.thread_id),
            )
        )

    # -- claims -------------------------------------------------------------------------------

    def _claim_locked(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        source_status: str,
        claimer: str,
        ttl: int,
        now: int,
    ) -> int | None:
        expires = now + ttl
        cur = conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = ?, claim_expires = ?, "
            "started_at = COALESCE(started_at, ?), last_heartbeat_at = ? "
            "WHERE id = ? AND status = ? AND claim_lock IS NULL",
            (claimer, expires, now, now, task_id, source_status),
        )
        if cur.rowcount != 1:
            return None
        task = _require(conn, task_id)
        source = "review" if source_status == "review" else "implement"
        run_cur = conn.execute(
            "INSERT INTO runs (task_id, profile, source, status, claim_lock, claim_expires, "
            "last_heartbeat_at, started_at) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
            (task_id, task.assignee, source, claimer, expires, now, now),
        )
        run_id = int(run_cur.lastrowid or 0)
        conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
        _event(
            conn,
            task_id,
            "claimed",
            {"claimer": claimer, "source": source, "expires": expires},
            now,
            run_id,
        )
        return run_id

    def claim(self, task_id: str, claimer: str, *, ttl: int = CLAIM_TTL_S) -> Task | None:
        """``ready → running`` for one run; ``None`` when someone else won or it is not ready."""
        now = self.now()

        def fn(conn: sqlite3.Connection) -> Task | None:
            task = _fetch(conn, task_id)
            if task is None or task.status != "ready":
                return None
            if _open_parents(conn, task_id):
                conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
                _event(conn, task_id, "claim_rejected", {"reason": "parent not done"}, now)
                return None
            if self._claim_locked(conn, task_id, "ready", claimer, ttl, now) is None:
                return None
            return _fetch(conn, task_id)

        return self._write(fn)

    def claim_review(self, task_id: str, claimer: str, *, ttl: int = CLAIM_TTL_S) -> Task | None:
        """``review → running`` for a reviewer run (tracked apart from the implementer's)."""
        now = self.now()

        def fn(conn: sqlite3.Connection) -> Task | None:
            task = _fetch(conn, task_id)
            if task is None or task.status != "review":
                return None
            if _open_parents(conn, task_id):
                conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
                _event(conn, task_id, "dependency_wait", {"reason": "parent reopened"}, now)
                return None
            if self._claim_locked(conn, task_id, "review", claimer, ttl, now) is None:
                return None
            return _fetch(conn, task_id)

        return self._write(fn)

    def set_worker(self, task_id: str, run_id: int, pid: int, fingerprint: str | None) -> bool:
        """Record the worker process of a run (the dispatcher, right after the spawn)."""

        def fn(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE tasks SET worker_pid = ?, worker_fingerprint = ? "
                "WHERE id = ? AND current_run_id = ? AND status = 'running'",
                (pid, fingerprint, task_id, run_id),
            )
            conn.execute(
                "UPDATE runs SET worker_pid = ?, worker_fingerprint = ? WHERE id = ?",
                (pid, fingerprint, run_id),
            )
            return cur.rowcount == 1

        return self._write(fn)

    def heartbeat(
        self,
        task_id: str,
        *,
        claimer: str | None = None,
        run_id: int | None = None,
        note: str | None = None,
        ttl: int = CLAIM_TTL_S,
    ) -> bool:
        """Extend a running claim and record liveness; False when the caller no longer owns it."""
        now = self.now()

        def fn(conn: sqlite3.Connection) -> bool:
            task = _fetch(conn, task_id)
            if task is None or task.status != "running":
                return False
            if claimer is not None and task.claim_lock != claimer:
                return False
            if run_id is not None and task.current_run_id != run_id:
                return False
            expires = now + ttl
            conn.execute(
                "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? WHERE id = ?",
                (expires, now, task_id),
            )
            conn.execute(
                "UPDATE runs SET claim_expires = ?, last_heartbeat_at = ? WHERE id = ?",
                (expires, now, task.current_run_id),
            )
            if note:
                _event(conn, task_id, "heartbeat", {"note": note[:400]}, now, task.current_run_id)
            return True

        return self._write(fn)

    # -- run endings --------------------------------------------------------------------------

    def _check_owner(
        self, task: Task, expected_run_id: int | None, *, force: bool, now: int
    ) -> None:
        if expected_run_id is not None:
            if task.current_run_id != expected_run_id:
                raise BoardRefused(
                    f"run {expected_run_id} no longer owns {task.id} (another run holds it now)"
                )
            return
        live = (
            task.status == "running"
            and task.claim_lock is not None
            and (task.claim_expires or 0) > now
        )
        if live and not force:
            raise LiveClaim(
                f"{task.id} is being worked on by a live worker; wait for it, or force the "
                "change as an operator"
            )

    def complete(
        self,
        task_id: str,
        *,
        summary: str | None = None,
        result: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        expected_run_id: int | None = None,
        force: bool = False,
    ) -> Task:
        """``running|ready|blocked|review → done`` with a handoff (summary or result)."""
        now = self.now()
        has_handoff = bool((summary or "").strip() or (result or "").strip())

        def fn(conn: sqlite3.Connection) -> Task:
            task = _require(conn, task_id)
            blockers = _open_parents(conn, task_id)
            if blockers and task.status not in _DONE:
                listed = ", ".join(f"{pid} ({status})" for pid, status in blockers)
                raise BoardRefused(f"{task_id} waits on unfinished parents: {listed}")
            if task.status not in ("running", "ready", "blocked", "review"):
                raise BoardRefused(f"{task_id} is {task.status}; it cannot be completed")
            if not has_handoff and task.status != "review":
                raise BoardInputError(
                    "a completion needs a handoff: a summary of what was done (or a result)"
                )
            self._check_owner(task, expected_run_id, force=force, now=now)
            handoff = summary if (summary or "").strip() else result
            if not handoff and task.status == "review":
                handoff = "Approved in review."
            conn.execute(
                _clear_claim_sql("done")
                + ", result = COALESCE(?, result), completed_at = ?, block_kind = NULL, "
                "block_recurrences = 0, escalated = 0, scheduled = 0, wake_at = NULL, "
                "consecutive_failures = 0, protocol_violations = 0, last_failure_error = NULL "
                "WHERE id = ?",
                (result, now, task_id),
            )
            run_id = _end_run(
                conn,
                task,
                outcome="completed",
                status="done",
                now=now,
                summary=handoff,
                metadata=metadata,
            )
            _event(
                conn,
                task_id,
                "completed",
                {"summary": (handoff or "")[:400], "approved": task.status == "review"},
                now,
                run_id,
            )
            self._promote_locked(conn, now)
            return _require(conn, task_id)

        return self._write(fn)

    def block(
        self,
        task_id: str,
        *,
        reason: str,
        kind: str | None = None,
        expected_run_id: int | None = None,
        force: bool = False,
    ) -> Task:
        """``running|ready → blocked`` (or ``todo`` for a dependency wait); see the module notes."""
        if kind is not None and kind not in BLOCK_KINDS:
            raise BoardInputError(f"block kind must be one of {', '.join(BLOCK_KINDS)}")
        text = (reason or "").strip()
        if not text:
            raise BoardInputError("a block needs a reason a person can act on")
        now = self.now()

        def fn(conn: sqlite3.Connection) -> Task:
            task = _require(conn, task_id)
            if task.status not in ("running", "ready", "todo"):
                raise BoardRefused(f"{task_id} is {task.status}; only open work can be blocked")
            self._check_owner(task, expected_run_id, force=force, now=now)
            landed_kind = kind
            open_parents = _open_parents(conn, task_id)
            if kind == "dependency" and not open_parents:
                landed_kind = "needs_input"
            source = _run_source(conn, task.current_run_id)
            payload: dict[str, Any] = {"reason": text[:1000], "kind": landed_kind, "source": source}
            if landed_kind != kind:
                payload["requested_kind"] = kind
            if landed_kind == "dependency":
                conn.execute(
                    _clear_claim_sql("todo") + ", block_kind = ? WHERE id = ?",
                    (landed_kind, task_id),
                )
                run_id = _end_run(
                    conn, task, outcome="blocked", status="todo", now=now, summary=text
                )
                _event(conn, task_id, "dependency_wait", payload, now, run_id)
                return _require(conn, task_id)
            recurrences = task.block_recurrences + 1 if task.block_kind == landed_kind else 1
            escalated = recurrences > BLOCK_RECURRENCE_LIMIT
            payload["recurrences"] = recurrences
            conn.execute(
                _clear_claim_sql("blocked")
                + ", block_kind = ?, block_recurrences = ?, escalated = ?, scheduled = 0, "
                "wake_at = NULL WHERE id = ?",
                (landed_kind, recurrences, 1 if escalated else 0, task_id),
            )
            run_id = _end_run(
                conn, task, outcome="blocked", status="blocked", now=now, summary=text
            )
            if source == "review" and run_id is not None:
                conn.execute(
                    "UPDATE runs SET metadata = ? WHERE id = ?",
                    (_json({"resume": "review"}), run_id),
                )
            if escalated:
                payload["limit"] = BLOCK_RECURRENCE_LIMIT
                _event(conn, task_id, "block_loop", payload, now, run_id)
            else:
                _event(conn, task_id, "blocked", payload, now, run_id)
            return _require(conn, task_id)

        return self._write(fn)

    def schedule(
        self,
        task_id: str,
        *,
        reason: str | None = None,
        wake_at: int | None = None,
        expected_run_id: int | None = None,
        force: bool = False,
    ) -> Task:
        """Park a task (not dispatchable) until ``wake_at`` or an explicit unblock."""
        now = self.now()

        def fn(conn: sqlite3.Connection) -> Task:
            task = _require(conn, task_id)
            if task.status not in ("todo", "ready", "running", "blocked"):
                raise BoardRefused(f"{task_id} is {task.status}; it cannot be scheduled")
            self._check_owner(task, expected_run_id, force=force, now=now)
            conn.execute(
                _clear_claim_sql("blocked") + ", scheduled = 1, wake_at = ? WHERE id = ?",
                (wake_at, task_id),
            )
            run_id = _end_run(
                conn, task, outcome="scheduled", status="blocked", now=now, summary=reason
            )
            _event(conn, task_id, "scheduled", {"reason": reason, "wake_at": wake_at}, now, run_id)
            return _require(conn, task_id)

        return self._write(fn)

    def request_review(
        self,
        task_id: str,
        *,
        summary: str,
        reviewer: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        expected_run_id: int | None = None,
        force: bool = False,
    ) -> Task:
        """``running|ready → review``; records who implemented it so changes go back there."""
        text = (summary or "").strip()
        if not text:
            raise BoardInputError(
                "a review request needs a summary of what was done and how it was checked"
            )
        now = self.now()

        def fn(conn: sqlite3.Connection) -> Task:
            task = _require(conn, task_id)
            if task.status not in ("running", "ready"):
                raise BoardRefused(f"{task_id} is {task.status}; only open work can go to review")
            if _open_parents(conn, task_id):
                raise BoardRefused(f"{task_id} waits on unfinished parents")
            self._check_owner(task, expected_run_id, force=force, now=now)
            implementer = None
            if task.current_run_id is not None:
                row = conn.execute(
                    "SELECT profile FROM runs WHERE id = ?", (task.current_run_id,)
                ).fetchone()
                implementer = row["profile"] if row is not None else None
            if implementer is None and task.assignee != reviewer:
                implementer = task.assignee
            new_assignee = reviewer if reviewer else task.assignee
            conn.execute(
                _clear_claim_sql("review") + ", assignee = ? WHERE id = ?", (new_assignee, task_id)
            )
            run_id = _end_run(
                conn,
                task,
                outcome="review_requested",
                status="review",
                now=now,
                summary=text,
                metadata=metadata,
            )
            _event(
                conn,
                task_id,
                "review_requested",
                {"summary": text[:400], "implementer": implementer, "reviewer": reviewer},
                now,
                run_id,
            )
            return _require(conn, task_id)

        return self._write(fn)

    @staticmethod
    def _implementer(conn: sqlite3.Connection, task_id: str) -> str | None:
        row = conn.execute(
            "SELECT payload FROM events WHERE task_id = ? AND kind = 'review_requested' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        payload = _unjson(row["payload"]) if row is not None else None
        value = _field(payload, "implementer")
        return value if isinstance(value, str) and value.strip() else None

    def request_changes(
        self, task_id: str, *, reason: str, expected_run_id: int | None = None
    ) -> Task:
        """A reviewer run hands the task back to its implementer (parents re-checked)."""
        text = (reason or "").strip()
        if not text:
            raise BoardInputError("say which changes are needed")
        now = self.now()

        def fn(conn: sqlite3.Connection) -> Task:
            task = _require(conn, task_id)
            if task.status != "running" or task.current_run_id is None:
                raise BoardRefused(f"{task_id} has no active review run")
            if expected_run_id is not None and task.current_run_id != expected_run_id:
                raise BoardRefused(f"run {expected_run_id} no longer owns {task_id}")
            if _run_source(conn, task.current_run_id) != "review":
                raise BoardRefused(f"the active run of {task_id} is not a review")
            implementer = self._implementer(conn, task_id)
            if implementer is None:
                raise BoardRefused(f"{task_id} has no recorded implementer to send changes to")
            landing = _landing(conn, task_id)
            reviewer = task.assignee
            conn.execute(
                _clear_claim_sql(landing) + ", assignee = ? WHERE id = ?", (implementer, task_id)
            )
            run_id = _end_run(
                conn, task, outcome="changes_requested", status=landing, now=now, summary=text
            )
            _event(
                conn,
                task_id,
                "changes_requested",
                {"reason": text[:1000], "implementer": implementer, "reviewer": reviewer},
                now,
                run_id,
            )
            return _require(conn, task_id)

        return self._write(fn)

    def reopen_review(
        self, task_id: str, *, reason: str | None = None, author: str = "user"
    ) -> Task:
        """A person sends a task in ``review`` back to its implementer."""
        now = self.now()

        def fn(conn: sqlite3.Connection) -> Task:
            task = _require(conn, task_id)
            if task.status != "review":
                raise BoardRefused(f"{task_id} is {task.status}, not in review")
            implementer = self._implementer(conn, task_id) or task.assignee
            landing = _landing(conn, task_id)
            conn.execute(
                _clear_claim_sql(landing) + ", assignee = ? WHERE id = ?", (implementer, task_id)
            )
            if reason:
                conn.execute(
                    "INSERT INTO comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, author, reason, now),
                )
            _event(
                conn,
                task_id,
                "changes_requested",
                {"reason": (reason or "")[:1000], "implementer": implementer, "reviewer": author},
                now,
            )
            return _require(conn, task_id)

        return self._write(fn)

    def unblock(self, task_id: str) -> Task:
        """``blocked → ready`` (``todo`` while a parent is open; ``review`` after a review run)."""
        now = self.now()

        def fn(conn: sqlite3.Connection) -> Task:
            task = _require(conn, task_id)
            if task.status != "blocked":
                raise BoardRefused(f"{task_id} is {task.status}, not blocked")
            return self._unblock_locked(conn, task, now)

        return self._write(fn)

    def _unblock_locked(self, conn: sqlite3.Connection, task: Task, now: int) -> Task:
        landing = _landing(conn, task.id)
        last = conn.execute(
            "SELECT metadata FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task.id,)
        ).fetchone()
        meta = _unjson(last["metadata"]) if last is not None else None
        if landing == "ready" and _field(meta, "resume") == "review":
            landing = "review"
        # block_kind / block_recurrences stay: a re-block of the same kind must read as a loop.
        conn.execute(
            _clear_claim_sql(landing) + ", scheduled = 0, wake_at = NULL, escalated = 0, "
            "consecutive_failures = 0, protocol_violations = 0, last_failure_error = NULL "
            "WHERE id = ?",
            (task.id,),
        )
        _event(conn, task.id, "unblocked", {"status": landing}, now)
        return _require(conn, task.id)

    def wake_scheduled(self) -> list[str]:
        """Unblock scheduled tasks whose wake time has come; returns their ids."""
        now = self.now()

        def fn(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'blocked' AND scheduled = 1 "
                "AND wake_at IS NOT NULL AND wake_at <= ?",
                (now,),
            ).fetchall()
            woke: list[str] = []
            for row in rows:
                self._unblock_locked(conn, Task.from_row(row), now)
                woke.append(row["id"])
            return woke

        return self._write(fn)

    def archive(self, task_id: str) -> Task:
        """Archive a task; a running run is closed. The returned task still names the worker
        process (from before the archive) so the caller can stop it."""
        now = self.now()

        def fn(conn: sqlite3.Connection) -> Task:
            task = _require(conn, task_id)
            if task.status == "archived":
                raise BoardRefused(f"{task_id} is already archived")
            conn.execute(_clear_claim_sql("archived") + " WHERE id = ?", (task_id,))
            run_id = _end_run(
                conn,
                task,
                outcome="reclaimed",
                status="reclaimed",
                now=now,
                summary="archived while the run was active",
            )
            _event(conn, task_id, "archived", None, now, run_id)
            self._promote_locked(conn, now)
            return task

        return self._write(fn)

    # -- dependency promotion -----------------------------------------------------------------

    def _promote_locked(self, conn: sqlite3.Connection, now: int) -> int:
        promoted = 0
        for row in conn.execute("SELECT id FROM tasks WHERE status = 'todo'").fetchall():
            if _open_parents(conn, row["id"]):
                continue
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'todo'", (row["id"],)
            )
            _event(conn, row["id"], "promoted", None, now)
            promoted += 1
        return promoted

    def recompute_ready(self) -> int:
        """``todo → ready`` for every task whose parents are all done or archived."""
        now = self.now()
        return self._write(lambda conn: self._promote_locked(conn, now))

    # -- dispatcher bookkeeping ---------------------------------------------------------------

    def release_run(
        self,
        task_id: str,
        run_id: int,
        *,
        outcome: str,
        error: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        event_payload: Mapping[str, Any] | None = None,
    ) -> bool:
        """End ``run_id`` and send its task back to ``ready`` (``review`` for a review run)."""
        now = self.now()

        def fn(conn: sqlite3.Connection) -> bool:
            task = _fetch(conn, task_id)
            if task is None or task.status != "running" or task.current_run_id != run_id:
                return False
            retry = _retry_status(conn, task)
            conn.execute(
                _clear_claim_sql(retry) + ", last_failure_error = COALESCE(?, last_failure_error) "
                "WHERE id = ?",
                (error, task_id),
            )
            _end_run(
                conn, task, outcome=outcome, status=outcome, now=now, metadata=metadata, error=error
            )
            _event(conn, task_id, outcome, dict(event_payload or {}), now, run_id)
            return True

        return self._write(fn)

    def record_failure(
        self,
        task_id: str,
        error: str,
        *,
        limit: int = FAILURE_LIMIT,
        kind: str | None = None,
    ) -> bool:
        """Count one failed attempt; at ``limit`` in a row the task is blocked. True if blocked."""
        now = self.now()

        def fn(conn: sqlite3.Connection) -> bool:
            task = _fetch(conn, task_id)
            if task is None:
                return False
            failures = task.consecutive_failures + 1
            conn.execute(
                "UPDATE tasks SET consecutive_failures = ?, last_failure_error = ? WHERE id = ?",
                (failures, error[:2000], task_id),
            )
            if failures < limit or task.status not in ("ready", "review", "todo", "running"):
                return False
            conn.execute(
                _clear_claim_sql("blocked") + ", block_kind = ? WHERE id = ?", (kind, task_id)
            )
            _end_run(conn, task, outcome="gave_up", status="blocked", now=now, error=error)
            _event(
                conn,
                task_id,
                "gave_up",
                {"failures": failures, "limit": limit, "error": error[:400]},
                now,
            )
            return True

        return self._write(fn)

    def record_violation(self, task_id: str, run_id: int, *, limit: int, error: str) -> bool:
        """A worker ended without a board call: release the task and count it; at ``limit``
        violations in a row the task is blocked. True when it was blocked."""
        now = self.now()

        def fn(conn: sqlite3.Connection) -> bool:
            task = _fetch(conn, task_id)
            if task is None or task.status != "running" or task.current_run_id != run_id:
                return False
            streak = task.protocol_violations + 1
            blocked = streak >= limit
            status = "blocked" if blocked else _retry_status(conn, task)
            conn.execute(
                _clear_claim_sql(status) + ", protocol_violations = ?, last_failure_error = ? "
                "WHERE id = ?",
                (streak, error[:2000], task_id),
            )
            _end_run(
                conn,
                task,
                outcome="protocol_violation",
                status=status,
                now=now,
                error=error,
                metadata={"protocol_violation": True},
            )
            _event(
                conn,
                task_id,
                "gave_up" if blocked else "protocol_violation",
                {"violations": streak, "limit": limit, "error": error[:400]},
                now,
                run_id,
            )
            return blocked

        return self._write(fn)

    def cool_lane(self, profile: str, seconds: int, reason: str) -> int:
        until = self.now() + int(seconds)
        self._write(
            lambda conn: conn.execute(
                "INSERT INTO lanes (profile, cooled_until, reason) VALUES (?, ?, ?) "
                "ON CONFLICT(profile) DO UPDATE SET cooled_until = MAX(cooled_until, "
                "excluded.cooled_until), reason = excluded.reason",
                (profile, until, reason),
            )
        )
        return until

    def cooled_lanes(self) -> dict[str, int]:
        now = self.now()
        sql = "SELECT profile, cooled_until FROM lanes WHERE cooled_until > ?"
        rows = self._read(lambda conn: conn.execute(sql, (now,)).fetchall())
        return {r["profile"]: int(r["cooled_until"]) for r in rows}

    def release_stale_claims(
        self,
        *,
        alive: Callable[[int, str | None], bool],
        terminate: Callable[[int, str | None], bool] | None = None,
        failure_limit: int = FAILURE_LIMIT,
    ) -> list[str]:
        """Expired claims: extend for a live, recently heartbeating worker; otherwise stop the
        worker and release the task (a failure), or keep the claim ``RECLAIM_GRACE_S`` longer when
        the worker survived termination. Returns the ids released."""
        now = self.now()
        rows = self._read(
            lambda conn: [
                Task.from_row(r)
                for r in conn.execute(
                    "SELECT * FROM tasks WHERE status = 'running' AND claim_expires IS NOT NULL "
                    "AND claim_expires < ?",
                    (now,),
                )
            ]
        )
        released: list[str] = []
        for task in rows:
            pid, fingerprint = task.worker_pid, task.worker_fingerprint
            heartbeat_stale = (
                task.last_heartbeat_at is not None
                and now - task.last_heartbeat_at > HEARTBEAT_STALE_S
            )
            live = pid is not None and alive(pid, fingerprint)
            if live and not heartbeat_stale:
                self._extend_expired(task, now, reason="worker alive")
                continue
            survived = False
            if live and pid is not None:
                stopped = terminate(pid, fingerprint) if terminate is not None else False
                survived = not stopped and alive(pid, fingerprint)
            if survived:
                self._extend_expired(task, now, reason="worker survived termination", grace=True)
                continue
            assert task.current_run_id is not None
            if self.release_run(
                task.id,
                task.current_run_id,
                outcome="reclaimed",
                error=f"claim expired (lock {task.claim_lock})",
                event_payload={"heartbeat_stale": heartbeat_stale, "pid": pid},
            ):
                released.append(task.id)
                self.record_failure(
                    task.id, "the claim expired without a result", limit=failure_limit
                )
        return released

    def _extend_expired(self, task: Task, now: int, *, reason: str, grace: bool = False) -> None:
        expires = now + (RECLAIM_GRACE_S if grace else CLAIM_TTL_S)

        def fn(conn: sqlite3.Connection) -> None:
            cur = conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ? AND status = 'running' "
                "AND claim_lock IS ? AND claim_expires < ?",
                (expires, task.id, task.claim_lock, now),
            )
            if cur.rowcount != 1:
                return
            conn.execute(
                "UPDATE runs SET claim_expires = ? WHERE id = ?", (expires, task.current_run_id)
            )
            _event(
                conn,
                task.id,
                "reclaim_deferred" if grace else "claim_extended",
                {"reason": reason, "expires": expires},
                now,
                task.current_run_id,
            )

        self._write(fn)

    def finished_runs_with_workers(self, older_than: int) -> list[Run]:
        """Ended runs that still name a worker process and ended before ``older_than``."""
        sql = (
            "SELECT * FROM runs WHERE ended_at IS NOT NULL AND ended_at < ? "
            "AND worker_pid IS NOT NULL ORDER BY id"
        )
        return self._read(lambda conn: [Run.from_row(r) for r in conn.execute(sql, (older_than,))])

    def forget_worker(self, run_id: int) -> None:
        self._write(
            lambda conn: conn.execute(
                "UPDATE runs SET worker_pid = NULL, worker_fingerprint = NULL WHERE id = ?",
                (run_id,),
            )
        )

    def drop_archived_subscriptions(self) -> int:
        def fn(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE task_id IN "
                "(SELECT id FROM tasks WHERE status = 'archived')"
            )
            return cur.rowcount

        return self._write(fn)
