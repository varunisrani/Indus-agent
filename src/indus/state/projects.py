"""Projects: named workspaces of one profile, the folders they own and repo discovery (P24-14).

One file per profile, ``<home>/projects.db``, written through one ``Writer`` (R03.8). A project has
an id (``p_<8 hex>``), a unique slug, a name, an optional description, icon and colour, an optional
bound board (``board_slug``) and the folders it owns; each folder has a kind (``folder``, ``repo``,
``worktree``) and one is primary (``primary_path``). One project may be active per profile
(``use``; ``use(None)`` clears it). Archived projects are hidden from lists and from path lookups.

A path belongs to the project with a folder equal to it or above it; when projects nest, the
deepest folder wins (``for_path``). ``join_session(session_id, cwd)`` records that a session
started inside a project's folder belongs to it. A bare new chat stays detached:
``start_folder`` names a folder only when a project or the profile's default directory applies.

``discover_repos`` walks the scan roots (the user's home by default) to a bounded depth (3), never
following links, skipping hidden folders and excluded names, and stops at a folder holding
``.git`` without walking into it; at most ``MAX_SCAN_DIRS`` folders are looked at per scan.
"""

from __future__ import annotations

import fnmatch
import os
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from indus.foundation.clock import Clock
from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.state import journal
from indus.state.connection import open_store
from indus.state.writer import Writer

__all__ = [
    "DEFAULT_EXCLUDES",
    "DEFAULT_SCAN_DEPTH",
    "FOLDER_KINDS",
    "MAX_SCAN_DIRS",
    "DiscoveryPolicy",
    "Project",
    "ProjectFolder",
    "ProjectInputError",
    "ProjectNotFound",
    "ProjectStore",
    "ProjectsError",
    "discover_repos",
    "normalize_path",
    "project_rows",
    "path_is_inside",
    "slugify",
]

FOLDER_KINDS: tuple[str, ...] = ("folder", "repo", "worktree")
DEFAULT_SCAN_DEPTH = 3
MAX_SCAN_DEPTH = 6
MAX_SCAN_DIRS = 2000
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "node_modules",
    "Library",
    "AppData",
    "Applications",
    "vendor",
    "target",
    "dist",
    "build",
    "venv",
    "__pycache__",
)
SCHEMA_VERSION = 1
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ACTIVE_KEY = "active_project"


class ProjectsError(IndusError):
    default_code = "projects.error"


class ProjectNotFound(ProjectsError):
    default_code = "projects.not_found"


class ProjectInputError(ProjectsError, ValueError):
    default_code = "projects.bad_input"


# -- paths --------------------------------------------------------------------------------------


def normalize_path(path: str | Path, *, user_home: Path | None = None) -> str:
    """Absolute, ``~`` expanded (against ``user_home`` when given), no trailing separator."""
    text = str(path).strip()
    if not text:
        raise ProjectInputError("a folder path must not be empty")
    if text == "~" or text.startswith(("~/", "~\\")):
        base = user_home if user_home is not None else Path(os.path.expanduser("~"))
        text = str(base) + text[1:]
    absolute = os.path.abspath(text)
    return absolute.rstrip("/\\") or absolute


def path_is_inside(folder: str, target: str) -> bool:
    """``target`` is ``folder`` or below it (whole path segments: ``/a/b`` is not in ``/a/bc``)."""
    if target == folder:
        return True
    stem = folder.rstrip("/\\")
    return target.startswith(stem + os.sep) or target.startswith(stem + "/")


def slugify(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-_")
    return text[:64].strip("-_") or "project"


# -- rows ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectFolder:
    path: str
    kind: str
    primary: bool
    label: str | None
    added_at: int

    def view(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "primary": self.primary,
            "label": self.label,
            "added_at": self.added_at,
        }


@dataclass(frozen=True)
class Project:
    id: str
    slug: str
    name: str
    description: str | None
    icon: str | None
    colour: str | None
    board_slug: str | None
    primary_path: str | None
    created_at: int
    archived: bool
    folders: tuple[ProjectFolder, ...] = field(default=())
    sessions: tuple[str, ...] = field(default=())

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "colour": self.colour,
            "board_slug": self.board_slug,
            "primary_path": self.primary_path,
            "created_at": self.created_at,
            "archived": self.archived,
            "folders": [f.view() for f in self.folders],
            "sessions": list(self.sessions),
        }


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        description TEXT,
        icon TEXT,
        colour TEXT,
        board_slug TEXT,
        primary_path TEXT,
        created_at INTEGER NOT NULL,
        archived INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS project_folders (
        project_id TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'folder' CHECK (kind IN ('folder', 'repo', 'worktree')),
        is_primary INTEGER NOT NULL DEFAULT 0,
        label TEXT,
        added_at INTEGER NOT NULL,
        PRIMARY KEY (project_id, path)
    )""",
    """CREATE TABLE IF NOT EXISTS project_sessions (
        session_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        joined_at INTEGER NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS project_meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE INDEX IF NOT EXISTS project_folders_path ON project_folders (path)",
)


def _load(conn: sqlite3.Connection, row: sqlite3.Row) -> Project:
    folders = conn.execute(
        "SELECT * FROM project_folders WHERE project_id = ? ORDER BY is_primary DESC, added_at, "
        "path",
        (row["id"],),
    ).fetchall()
    sessions = conn.execute(
        "SELECT session_id FROM project_sessions WHERE project_id = ? ORDER BY joined_at, "
        "session_id",
        (row["id"],),
    ).fetchall()
    return Project(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        description=row["description"],
        icon=row["icon"],
        colour=row["colour"],
        board_slug=row["board_slug"],
        primary_path=row["primary_path"],
        created_at=int(row["created_at"]),
        archived=bool(row["archived"]),
        folders=tuple(
            ProjectFolder(f["path"], f["kind"], bool(f["is_primary"]), f["label"], f["added_at"])
            for f in folders
        ),
        sessions=tuple(s["session_id"] for s in sessions),
    )


def _set_primary(conn: sqlite3.Connection, project_id: str, path: str | None) -> None:
    conn.execute("UPDATE project_folders SET is_primary = 0 WHERE project_id = ?", (project_id,))
    if path is not None:
        conn.execute(
            "UPDATE project_folders SET is_primary = 1 WHERE project_id = ? AND path = ?",
            (project_id, path),
        )
    conn.execute("UPDATE projects SET primary_path = ? WHERE id = ?", (path, project_id))


def _check_slug(text: str) -> str:
    slug = text.strip().lower()
    if not _SLUG.match(slug):
        raise ProjectInputError(
            f"project slug {text!r} is not usable: 1-64 lowercase letters, digits, '-' or '_'"
        )
    return slug


def _check_kind(kind: str) -> str:
    if kind not in FOLDER_KINDS:
        raise ProjectInputError(f"folder kind must be one of {', '.join(FOLDER_KINDS)}")
    return kind


# -- the store ----------------------------------------------------------------------------------


class ProjectStore:
    """One profile's projects file. Open with ``ProjectStore(scope)``; ``close()`` after."""

    def __init__(self, scope: Scope, *, path: Path | None = None, clock: Clock | None = None):
        self.scope = scope
        self.clock: Clock = clock if clock is not None else scope.clock
        target = path if path is not None else scope.home / "projects.db"
        handle = open_store(scope, target, on_connected=self._setup)
        self.path = handle.path
        self.writer = Writer(self.path, handle.conn, clock=self.clock)

    def _setup(self, conn: sqlite3.Connection) -> None:
        cfg = journal.database_config(self.scope)
        journal.apply(conn, cfg, self.scope.home / "projects.db")
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

    def __enter__(self) -> ProjectStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _now(self) -> int:
        return int(self.clock.now().timestamp())

    def _read[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self.writer.borrow() as conn:
            return fn(conn)

    def _write[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return self.writer.write(fn)

    def _user_home(self) -> Path | None:
        text = self.scope.env.get("HOME") or self.scope.env.get("USERPROFILE")
        return Path(text) if text else None

    def norm(self, path: str | Path) -> str:
        return normalize_path(path, user_home=self._user_home())

    # -- reads --------------------------------------------------------------------------------

    def list(self, *, include_archived: bool = False) -> list[Project]:
        sql = "SELECT * FROM projects" + ("" if include_archived else " WHERE archived = 0")
        sql += " ORDER BY created_at, slug"
        return self._read(lambda conn: [_load(conn, r) for r in conn.execute(sql).fetchall()])

    def find(self, ref: str) -> Project | None:
        """By id, then slug, then name (exact, then ignoring case)."""
        token = (ref or "").strip()
        if not token:
            return None

        def fn(conn: sqlite3.Connection) -> Project | None:
            for sql, value in (
                ("SELECT * FROM projects WHERE id = ?", token),
                ("SELECT * FROM projects WHERE slug = ?", token.lower()),
                ("SELECT * FROM projects WHERE name = ? ORDER BY created_at", token),
                ("SELECT * FROM projects WHERE lower(name) = ? ORDER BY created_at", token.lower()),
            ):
                row = conn.execute(sql, (value,)).fetchone()
                if row is not None:
                    return _load(conn, row)
            return None

        return self._read(fn)

    def get(self, ref: str) -> Project:
        found = self.find(ref)
        if found is None:
            raise ProjectNotFound(f"no project matches {ref!r}")
        return found

    def active(self) -> Project | None:
        def fn(conn: sqlite3.Connection) -> Project | None:
            row = conn.execute(
                "SELECT value FROM project_meta WHERE key = ?", (_ACTIVE_KEY,)
            ).fetchone()
            if row is None:
                return None
            hit = conn.execute(
                "SELECT * FROM projects WHERE id = ? AND archived = 0", (row["value"],)
            ).fetchone()
            return _load(conn, hit) if hit is not None else None

        return self._read(fn)

    def for_path(self, path: str | Path, *, include_archived: bool = False) -> Project | None:
        """The project owning ``path``: the deepest folder that is it or above it."""
        try:
            target = self.norm(path)
        except ProjectInputError:
            return None

        def fn(conn: sqlite3.Connection) -> Project | None:
            sql = (
                "SELECT f.project_id, f.path FROM project_folders f "
                "JOIN projects p ON p.id = f.project_id"
            )
            if not include_archived:
                sql += " WHERE p.archived = 0"
            owners = [r for r in conn.execute(sql).fetchall() if path_is_inside(r["path"], target)]
            if not owners:
                return None
            best = max(owners, key=lambda r: len(r["path"]))
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (best["project_id"],))
            hit = row.fetchone()
            return _load(conn, hit) if hit is not None else None

        return self._read(fn)

    def session_project(self, session_id: str) -> Project | None:
        def fn(conn: sqlite3.Connection) -> Project | None:
            row = conn.execute(
                "SELECT p.* FROM project_sessions s JOIN projects p ON p.id = s.project_id "
                "WHERE s.session_id = ?",
                (session_id,),
            ).fetchone()
            return _load(conn, row) if row is not None else None

        return self._read(fn)

    # -- writes -------------------------------------------------------------------------------

    def create(
        self,
        name: str,
        *,
        folders: Iterable[str | Path] = (),
        primary: str | Path | None = None,
        slug: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        colour: str | None = None,
        board_slug: str | None = None,
        kind: str = "folder",
        allow_duplicate_path: bool = False,
    ) -> Project:
        clean = (name or "").strip()
        if not clean:
            raise ProjectInputError("a project needs a name")
        _check_kind(kind)
        wanted = _check_slug(slug) if slug else slugify(clean)
        board = _check_slug(board_slug) if board_slug else None
        paths = list(dict.fromkeys(self.norm(p) for p in folders))
        first = self.norm(primary) if primary is not None else (paths[0] if paths else None)
        if first is not None and first not in paths:
            paths.insert(0, first)
        if first is not None and not allow_duplicate_path:
            owner = self._primary_owner(first)
            if owner is not None:
                raise ProjectInputError(
                    f"{first} is already the main folder of project '{owner.slug}'; use that "
                    "project instead of making a second one"
                )
        now = self._now()

        def fn(conn: sqlite3.Connection) -> Project:
            project_id = "p_" + secrets.token_hex(4)
            unique, n = wanted, 1
            while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (unique,)).fetchone():
                n += 1
                unique = wanted[: 64 - len(f"-{n}")].rstrip("-_") + f"-{n}"
            conn.execute(
                "INSERT INTO projects (id, slug, name, description, icon, colour, board_slug, "
                "primary_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, unique, clean, description, icon, colour, board, first, now),
            )
            for path in paths:
                conn.execute(
                    "INSERT INTO project_folders (project_id, path, kind, is_primary, added_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (project_id, path, kind, 1 if path == first else 0, now),
                )
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return _load(conn, row)

        return self._write(fn)

    def _primary_owner(self, path: str) -> Project | None:
        for project in self.list():
            if project.primary_path and os.path.normcase(project.primary_path) == os.path.normcase(
                path
            ):
                return project
        return None

    def update(
        self,
        ref: str,
        *,
        name: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        colour: str | None = None,
        board_slug: str | None = None,
    ) -> Project:
        """Change the given fields; ``""`` clears icon, colour and board."""
        project = self.get(ref)
        changes: dict[str, Any] = {}
        if name is not None:
            if not name.strip():
                raise ProjectInputError("a project needs a name")
            changes["name"] = name.strip()
        if description is not None:
            changes["description"] = description
        for column, value in (("icon", icon), ("colour", colour)):
            if value is not None:
                changes[column] = value or None
        if board_slug is not None:
            changes["board_slug"] = _check_slug(board_slug) if board_slug.strip() else None
        if changes:
            sets = ", ".join(f"{column} = ?" for column in changes)
            self._write(
                lambda conn: conn.execute(
                    f"UPDATE projects SET {sets} WHERE id = ?", (*changes.values(), project.id)
                )
            )
        return self.get(project.id)

    def add_path(
        self,
        ref: str,
        path: str | Path,
        *,
        kind: str = "folder",
        primary: bool = False,
        label: str | None = None,
    ) -> Project:
        project = self.get(ref)
        _check_kind(kind)
        target = self.norm(path)
        now = self._now()

        def fn(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO project_folders (project_id, path, kind, is_primary, label, added_at) "
                "VALUES (?, ?, ?, 0, ?, ?) ON CONFLICT (project_id, path) DO UPDATE SET "
                "kind = excluded.kind, label = COALESCE(excluded.label, label)",
                (project.id, target, kind, label, now),
            )
            has_primary = conn.execute(
                "SELECT 1 FROM project_folders WHERE project_id = ? AND is_primary = 1",
                (project.id,),
            ).fetchone()
            if primary or has_primary is None:
                _set_primary(conn, project.id, target)

        self._write(fn)
        return self.get(project.id)

    def remove_path(self, ref: str, path: str | Path) -> Project:
        project = self.get(ref)
        target = self.norm(path)

        def fn(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT is_primary FROM project_folders WHERE project_id = ? AND path = ?",
                (project.id, target),
            ).fetchone()
            if row is None:
                raise ProjectInputError(f"{target} is not a folder of project '{project.slug}'")
            conn.execute(
                "DELETE FROM project_folders WHERE project_id = ? AND path = ?",
                (project.id, target),
            )
            if row["is_primary"]:
                nxt = conn.execute(
                    "SELECT path FROM project_folders WHERE project_id = ? ORDER BY added_at, path "
                    "LIMIT 1",
                    (project.id,),
                ).fetchone()
                _set_primary(conn, project.id, nxt["path"] if nxt is not None else None)

        self._write(fn)
        return self.get(project.id)

    def archive(self, ref: str, *, archived: bool = True) -> Project:
        project = self.get(ref)

        def fn(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE projects SET archived = ? WHERE id = ?", (1 if archived else 0, project.id)
            )
            if archived:
                conn.execute(
                    "DELETE FROM project_meta WHERE key = ? AND value = ?",
                    (_ACTIVE_KEY, project.id),
                )

        self._write(fn)
        return self.get(project.id)

    def use(self, ref: str | None) -> Project | None:
        """Make a project the active one; ``None`` clears it."""
        if ref is None:
            self._write(
                lambda conn: conn.execute("DELETE FROM project_meta WHERE key = ?", (_ACTIVE_KEY,))
            )
            return None
        project = self.get(ref)
        if project.archived:
            raise ProjectInputError(f"project '{project.slug}' is archived; restore it first")
        self._write(
            lambda conn: conn.execute(
                "INSERT INTO project_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (_ACTIVE_KEY, project.id),
            )
        )
        return project

    def join_session(self, session_id: str, cwd: str | Path | None) -> Project | None:
        """Record that ``session_id`` (started in ``cwd``) belongs to the project owning ``cwd``."""
        if not session_id or cwd is None:
            return None
        project = self.for_path(cwd)
        if project is None:
            return None
        now = self._now()
        self._write(
            lambda conn: conn.execute(
                "INSERT INTO project_sessions (session_id, project_id, joined_at) VALUES (?, ?, ?) "
                "ON CONFLICT (session_id) DO UPDATE SET project_id = excluded.project_id",
                (session_id, project.id, now),
            )
        )
        return self.get(project.id)

    def start_folder(
        self, *, project: str | None = None, default_dir: str | Path | None = None
    ) -> str | None:
        """Where a new chat starts: the named (else the active) project's main folder, else the
        default directory, else nowhere (detached)."""
        chosen = self.get(project) if project else self.active()
        if chosen is not None and chosen.primary_path:
            return chosen.primary_path
        if default_dir:
            return self.norm(default_dir)
        return None


# -- discovery ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryPolicy:
    enabled: bool = True
    roots: tuple[str, ...] = ()
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES
    max_depth: int = DEFAULT_SCAN_DEPTH

    @classmethod
    def from_config(cls, view: object) -> DiscoveryPolicy:
        getter = getattr(view, "get", None)

        def read(key: str) -> object:
            if getter is None:
                return None
            try:
                return getter(key)
            except Exception:
                return None

        enabled = read("projects.discovery.enabled")
        roots = read("projects.discovery.roots")
        excludes = read("projects.discovery.excludes")
        depth = read("projects.discovery.max_depth")
        return cls(
            enabled=enabled is not False,
            roots=tuple(str(r) for r in roots) if isinstance(roots, list) else (),  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            excludes=(
                tuple(str(e) for e in excludes)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
                if isinstance(excludes, list)
                else DEFAULT_EXCLUDES
            ),
            max_depth=(
                max(0, min(int(depth), MAX_SCAN_DEPTH))
                if isinstance(depth, int) and not isinstance(depth, bool)
                else DEFAULT_SCAN_DEPTH
            ),
        )


def _excluded(entry: Path, patterns: Iterable[str]) -> bool:
    name = entry.name
    for pattern in patterns:
        if os.path.isabs(pattern):
            if path_is_inside(normalize_path(pattern), str(entry)):
                return True
        elif fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def discover_repos(
    policy: DiscoveryPolicy,
    *,
    user_home: Path,
    max_dirs: int = MAX_SCAN_DIRS,
) -> list[dict[str, str]]:
    """Repository roots under the scan roots, ``[{"root", "label"}]`` sorted by path."""
    if not policy.enabled:
        return []
    roots = [Path(normalize_path(r, user_home=user_home)) for r in policy.roots] or [user_home]
    found: dict[str, str] = {}
    looked = 0
    for root in roots:
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and looked < max_dirs:
            folder, depth = stack.pop()
            looked += 1
            if (folder / ".git").exists():
                found[str(folder)] = folder.name or str(folder)
                continue
            if depth >= policy.max_depth:
                continue
            try:
                entries = sorted(folder.iterdir(), key=lambda p: p.name, reverse=True)
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                if _excluded(entry, policy.excludes):
                    continue
                stack.append((entry, depth + 1))
    return [{"root": root, "label": label} for root, label in sorted(found.items())]


def project_rows(projects: Iterable[Project], active: Project | None) -> list[Mapping[str, Any]]:
    """The list rows CLI and RPC both show."""
    active_id = active.id if active is not None else None
    return [
        {
            "id": p.id,
            "slug": p.slug,
            "name": p.name,
            "primary_path": p.primary_path,
            "folders": len(p.folders),
            "archived": p.archived,
            "active": p.id == active_id,
        }
        for p in projects
    ]
