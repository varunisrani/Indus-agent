"""What a full backup holds, and how a database is copied safely (P07-22, R04.23).

A path is judged relative to the root home. Left out:

- folders anywhere in the tree: version control and dependency trees, bytecode and tool caches,
  virtual environments, earlier backups and state snapshots, checkpoint stores, and browser
  profiles — by name, and by content: a folder that holds a browser's cookie or saved-login
  database is skipped whatever it is called, because those files are credentials;
- folders at a profile's top level that rebuild themselves (downloaded models, runtimes);
  ``cache/`` there except the media and evidence kept in ``KEPT_CACHE``;
- the install tree when it lives inside the home;
- files: SQLite sidecars (``-wal``, ``-shm``, ``-journal``: a fresh snapshot paired with an old
  sidecar restores torn), retired-WAL captures, bytecode, lock and pid files, sockets, devices and
  symlinks (they could pull in data from outside the home), and the archive being written.

``copy_database`` copies one SQLite file through the online backup API from a read-only
connection: safe while another process writes, and it never changes the source.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from indus.state.readpool import read_only_uri

__all__ = [
    "BROWSER_MARKER_FILES",
    "KEPT_CACHE",
    "SKIPPED_DIRS",
    "SQLITE_HEADER",
    "copy_database",
    "is_sidecar",
    "is_sqlite_file",
    "iter_backup_files",
    "skip_dir",
    "skip_file",
]

SKIPPED_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        "backups",
        "state-snapshots",
        "checkpoints",
        "browser-profiles",
        "browser_profiles",
        "browser-profile",
        ".venv",
        "venv",
        "site-packages",
        ".cache",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
# Only at a profile's top level: deeper folders of the same name are the user's own data.
TOP_LEVEL_SKIPPED = frozenset({"models", "runtimes", "node"})
KEPT_CACHE = frozenset(
    {"images", "audio", "videos", "documents", "screenshots", "citations", "generated"}
)
BROWSER_MARKER_FILES = ("Cookies", "Login Data", "Login Data For Account", "Web Data")
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
SKIPPED_SUFFIXES = (".pyc", ".pyo", ".lock", ".pid", ".partial")
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SKIPPED_NAMES = frozenset({".backup.lock"})
RETIRED_WAL_MARK = ".retired-wal-"
SQLITE_HEADER = b"SQLite format 3\x00"


def _profile_relative(parts: tuple[str, ...]) -> tuple[str, ...]:
    """The part of a root-relative path below its profile home."""
    if len(parts) >= 2 and parts[0] == "profiles":
        return parts[2:]
    return parts


def looks_like_browser_profile(directory: Path) -> bool:
    """A folder holding a browser's cookie or saved-login database (or its ``Default`` one)."""
    for base in (directory, directory / "Default"):
        for name in BROWSER_MARKER_FILES:
            if (base / name).is_file():
                return True
    return False


def skip_dir(rel: PurePosixPath, absolute: Path, *, install_dir: Path | None = None) -> bool:
    """Should the walk leave this folder (``rel`` is relative to the root home) out?"""
    name = rel.name
    if name in SKIPPED_DIRS or RETIRED_WAL_MARK in name:
        return True
    inner = _profile_relative(rel.parts)
    if len(inner) == 1 and inner[0] in TOP_LEVEL_SKIPPED:
        return True
    if len(inner) == 2 and inner[0] == "cache" and inner[1] not in KEPT_CACHE:
        return True
    if install_dir is not None:
        try:
            if absolute.resolve() == install_dir.resolve():
                return True
        except OSError:
            pass
    try:
        if stat.S_ISLNK(absolute.lstat().st_mode):
            return True
    except OSError:
        return False
    return looks_like_browser_profile(absolute)


def is_sidecar(absolute: Path) -> bool:
    """``x.db-wal`` and friends: a database name plus a sidecar suffix, or a sibling database."""
    name = absolute.name
    for suffix in SIDECAR_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            base = name[: -len(suffix)]
            return base.endswith(DB_SUFFIXES) or (absolute.parent / base).is_file()
    return False


def skip_file(rel: PurePosixPath, absolute: Path) -> bool:
    """Should this file be left out (name rules, then anything that is not a regular file)?"""
    name = rel.name
    if name in SKIPPED_NAMES or name.endswith(SKIPPED_SUFFIXES) or RETIRED_WAL_MARK in name:
        return True
    if is_sidecar(absolute):
        return True
    inner = _profile_relative(rel.parts)
    if len(inner) == 2 and inner[0] == "cache":
        return True  # loose files directly in cache/ are regenerable state
    try:
        return not stat.S_ISREG(absolute.lstat().st_mode)
    except OSError:
        return False  # let the archive step report the read failure


def iter_backup_files(
    root: Path,
    *,
    output: Path | None = None,
    install_dir: Path | None = None,
    skipped: set[str] | None = None,
) -> Iterator[tuple[Path, PurePosixPath]]:
    """``(absolute, root-relative)`` for every file a full backup holds, in walk order."""
    out = output.resolve() if output is not None else None
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        rel_dir = PurePosixPath(here.relative_to(root).as_posix())
        kept: list[str] = []
        for name in sorted(dirnames):
            rel = rel_dir / name if str(rel_dir) != "." else PurePosixPath(name)
            if skip_dir(rel, here / name, install_dir=install_dir):
                if skipped is not None:
                    skipped.add(str(rel))
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            rel = rel_dir / name if str(rel_dir) != "." else PurePosixPath(name)
            absolute = here / name
            if skip_file(rel, absolute):
                continue
            if out is not None and absolute.resolve() == out:
                continue
            yield absolute, rel


def is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def copy_database(source: Path, target: Path, *, busy_seconds: float = 30.0) -> None:
    """Copy ``source`` into ``target`` (created or overwritten) with the online backup API."""
    src = sqlite3.connect(read_only_uri(source), uri=True, timeout=busy_seconds)
    try:
        dst = sqlite3.connect(str(target), timeout=busy_seconds)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(target, 0o600)
