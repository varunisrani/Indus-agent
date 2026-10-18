"""Journal mode policy and connection pragmas.

``apply(conn, cfg, path)`` picks the journal mode for a freshly opened writer connection and
returns ``"wal"`` or ``"delete"``. The rule that outranks everything else (R03.1): a file that is
WAL on disk — or whose header cannot be read — stays WAL. Switching a live WAL file to DELETE
discards other processes' uncheckpointed commits, so a configured ``delete`` on such a file is
logged once and ignored; mode switches belong to an offline command (P07).

WAL is not *enabled* where it is known to be unsafe: a store on a virtiofs/9p mount (shared
memory is not coherent across the VM boundary) or a SQLite build with the WAL-reset bug
(3.7.0 – 3.51.2, fixed in 3.51.3, backports 3.50.7 and 3.44.6). When enabling WAL fails with an
incompatibility error, or SQLite quietly keeps another mode, the store falls back to DELETE with
one ERROR per process. Every non-WAL switch runs with ``busy_timeout=0`` so another opener makes
it fail instead of racing.

``apply_pragmas(conn, cfg)`` runs on every connection kind (writer, pooled reader, read-only
browse): configured integer pragmas and ``synchronous``, and on macOS always
``checkpoint_fullfsync`` and ``synchronous=FULL`` (plain fsync is no barrier there, R03.27).
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from indus.foundation.scope import Scope
from indus.state.connection import header_says_wal, read_header
from indus.state.errors import is_lock_error

__all__ = [
    "CROSS_VM_FSTYPES",
    "INT_PRAGMAS",
    "WAL_SIZE_LIMIT",
    "Executes",
    "apply",
    "apply_pragmas",
    "configured_mode",
    "database_config",
    "fs_type",
    "linked_sqlite_version",
    "pragma_settings",
    "reset_once_logs",
    "set_mode_no_wait",
    "synchronous_level",
    "wal_reset_vulnerable",
]

log = logging.getLogger(__name__)

JournalMode = Literal["wal", "delete"]
WAL_SIZE_LIMIT = 64 * 1024 * 1024
WAL_INCOMPATIBLE_WORDS = ("locking protocol", "not authorized", "disk i/o error")
CROSS_VM_FSTYPES = frozenset({"virtiofs", "fuse.virtiofs", "9p", "9p2000", "9p2000.l", "9p2000.u"})
MOUNTINFO = "/proc/self/mountinfo"
INT_PRAGMAS = ("cache_size", "mmap_size", "temp_store", "wal_autocheckpoint", "journal_size_limit")
SYNCHRONOUS_LEVELS = {"OFF": 0, "NORMAL": 1, "FULL": 2, "EXTRA": 3}
SYNCHRONOUS_FULL = 2


class Executes(Protocol):
    """The part of ``sqlite3.Connection`` this module uses (tests pass scripted fakes)."""

    def execute(self, sql: str, parameters: Any = ..., /) -> Any: ...


# -- once-per-process logs ----------------------------------------------------------------------

_once_lock = threading.Lock()
_once_seen: set[tuple[str, str]] = set()


def _once(kind: str, key: str) -> bool:
    with _once_lock:
        if (kind, key) in _once_seen:
            return False
        _once_seen.add((kind, key))
        return True


def reset_once_logs() -> None:
    with _once_lock:
        _once_seen.clear()


# -- config -------------------------------------------------------------------------------------


def database_config(scope: Scope) -> Mapping[str, Any]:
    """The ``database`` config section of ``scope`` as a mapping (empty when unset)."""
    if scope.config is None:
        return {}
    section = scope.config.get("database")
    if isinstance(section, Mapping):
        return {str(k): v for k, v in cast(Mapping[Any, Any], section).items()}
    return {}


def configured_mode(cfg: Mapping[str, Any] | None) -> JournalMode:
    raw = (cfg or {}).get("journal_mode", "wal")
    mode = raw.strip().lower() if isinstance(raw, str) else ""
    if mode == "delete":
        return "delete"
    if mode != "wal" and _once("bad_mode", repr(raw)):
        log.warning("database.journal_mode=%r is not 'wal' or 'delete'; using wal", raw)
    return "wal"


def pragma_settings(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """Pragma values from ``database.pragmas`` and the section's top level (top level wins)."""
    cfg = cfg or {}
    merged: dict[str, Any] = {}
    nested = cfg.get("pragmas")
    if isinstance(nested, Mapping):
        merged.update({str(k): v for k, v in cast(Mapping[Any, Any], nested).items()})
    for name in (*INT_PRAGMAS, "synchronous"):
        if name in cfg and cfg[name] is not None:
            merged[name] = cfg[name]
    return merged


# -- host facts ---------------------------------------------------------------------------------


def linked_sqlite_version() -> tuple[int, int, int]:
    major, minor, patch = sqlite3.sqlite_version_info[:3]
    return (major, minor, patch)


def wal_reset_vulnerable(version: tuple[int, int, int]) -> bool:
    """True for SQLite builds with the WAL-reset bug (sqlite.org/wal.html#walresetbug)."""
    if version < (3, 7, 0) or version >= (3, 51, 3):
        return False
    if version[:2] == (3, 50) and version >= (3, 50, 7):
        return False
    return not (version[:2] == (3, 44) and version >= (3, 44, 6))


def fs_type(path: Path, mountinfo: str = MOUNTINFO) -> str:
    """Filesystem type of the longest mount point containing ``path`` ('' when unknown)."""
    try:
        directory = os.path.realpath(path if path.is_dir() else path.parent)
        lines = Path(mountinfo).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return ""
    best, best_type = -1, ""
    for line in lines:
        fields, sep, tail = line.partition(" - ")
        parts = fields.split()
        if not sep or len(parts) < 5 or not tail.split():
            continue
        mount = parts[4].replace("\\040", " ")
        inside = directory == mount or directory.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) > best:
            best, best_type = len(mount), tail.split()[0].lower()
    return best_type


def _on_cross_vm_fs(path: Path, mountinfo: str | None, platform: str) -> bool:
    if mountinfo is None and not platform.startswith("linux"):
        return False
    return fs_type(path, mountinfo or MOUNTINFO) in CROSS_VM_FSTYPES


# -- mode switching -----------------------------------------------------------------------------


def _mode_of(row: Any) -> str:
    if not row or row[0] is None:
        return ""
    value = row[0]
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    return str(value).strip().lower()


def set_mode_no_wait(conn: Executes, mode: str) -> str:
    """``PRAGMA journal_mode=<mode>`` with ``busy_timeout=0``; returns the mode SQLite reports.

    The zero timeout turns "someone else has the file open" into an immediate error instead of a
    switch squeezed between another writer's transactions. Callers treat an error as "leave the
    mode alone", never as something to retry.
    """
    try:
        previous = int(_mode_of(conn.execute("PRAGMA busy_timeout").fetchone()) or 0)
    except (sqlite3.Error, ValueError):
        previous = 0
    conn.execute("PRAGMA busy_timeout=0")
    try:
        return _mode_of(conn.execute(f"PRAGMA journal_mode={mode}").fetchone())
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"PRAGMA busy_timeout={previous}")


def _wal_companions(conn: Executes, platform: str) -> None:
    with contextlib.suppress(sqlite3.Error):
        conn.execute(f"PRAGMA journal_size_limit={WAL_SIZE_LIMIT}")
    if platform == "darwin":
        _macos_barriers(conn)


def _fallback_to_delete(conn: Executes, path: Path, reason: object) -> str:
    if _once("wal_fallback", str(path)):
        log.error(
            "the filesystem holding %s refused WAL journaling (%s); using DELETE journaling. "
            "Set database.journal_mode: delete to make this explicit.",
            path,
            reason,
        )
    try:
        return set_mode_no_wait(conn, "DELETE") or "delete"
    except sqlite3.OperationalError as exc:
        if _once("delete_fallback_failed", str(path)):
            log.error("switching %s to DELETE journaling also failed: %s", path, exc)
        try:
            return _mode_of(conn.execute("PRAGMA journal_mode").fetchone()) or "delete"
        except sqlite3.Error:
            return "delete"


def _enable_wal(conn: Executes, path: Path, platform: str) -> str:
    try:
        mode = _mode_of(conn.execute("PRAGMA journal_mode=WAL").fetchone())
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if not any(word in text for word in WAL_INCOMPATIBLE_WORDS):
            raise
        # Never downgrade a file that now reads WAL (or cannot be read).
        if _disk_state(path) in ("wal", "unknown"):
            raise
        return _fallback_to_delete(conn, path, exc)
    if mode == "wal":
        _wal_companions(conn, platform)
        return "wal"
    return _fallback_to_delete(conn, path, f"SQLite kept journal_mode={mode or 'unknown'}")


def _disk_state(path: Path) -> Literal["wal", "rollback", "fresh", "unknown"]:
    try:
        size = os.stat(path).st_size
    except FileNotFoundError:
        return "fresh"
    except OSError:
        return "unknown"
    if size == 0:
        return "fresh"
    says = header_says_wal(read_header(path))
    if says is None:
        return "unknown"
    return "wal" if says else "rollback"


def apply(
    conn: Executes,
    cfg: Mapping[str, Any] | None,
    path: Path,
    *,
    version: tuple[int, int, int] | None = None,
    mountinfo: str | None = None,
    platform: str | None = None,
) -> str:
    """Set the journal mode of a new writer connection; returns ``"wal"`` or ``"delete"``."""
    plat = sys.platform if platform is None else platform
    vulnerable = wal_reset_vulnerable(linked_sqlite_version() if version is None else version)
    wanted = configured_mode(cfg)
    state = _disk_state(path)
    key = str(path)

    if state in ("wal", "unknown"):
        if wanted == "delete" and _once("delete_ignored", key):
            log.error(
                "database.journal_mode is 'delete' but %s is %s; keeping WAL (switching a live "
                "WAL store would lose other processes' recent writes)",
                path,
                "in WAL mode" if state == "wal" else "unreadable",
            )
        if vulnerable and _once("vulnerable_kept", key):
            log.warning(
                "SQLite %s has the WAL-reset bug; %s keeps its existing WAL mode",
                sqlite3.sqlite_version,
                path,
            )
        if _on_cross_vm_fs(path, mountinfo, plat) and _once("cross_vm_kept", key):
            log.warning("%s is on a shared VM filesystem but already uses WAL; keeping it", path)
        _wal_companions(conn, plat)
        return "wal"

    if wanted == "delete":
        return set_mode_no_wait(conn, "DELETE") or "delete"
    if vulnerable:
        if _once("vulnerable_new", key):
            log.warning(
                "SQLite %s has the WAL-reset bug (fixed in 3.51.3); %s uses DELETE journaling",
                sqlite3.sqlite_version,
                path,
            )
        return _keep_or_set_delete(conn, path)
    if _on_cross_vm_fs(path, mountinfo, plat):
        if _once("cross_vm_new", key):
            log.warning(
                "%s is on a shared VM filesystem (virtiofs/9p) where WAL is unsafe; "
                "using DELETE journaling",
                path,
            )
        return _keep_or_set_delete(conn, path)
    return _enable_wal(conn, path, plat)


def _keep_or_set_delete(conn: Executes, path: Path) -> str:
    try:
        return set_mode_no_wait(conn, "DELETE") or "delete"
    except sqlite3.OperationalError as exc:
        if not is_lock_error(exc):
            raise
        log.warning("could not switch %s to DELETE right now (%s); leaving its mode", path, exc)
        return "delete"


# -- pragmas ------------------------------------------------------------------------------------


def synchronous_level(raw: Any) -> int | None:
    """``OFF/NORMAL/FULL/EXTRA`` (any case) or 0–3 → the pragma integer; None otherwise."""
    if isinstance(raw, bool):
        return 0 if raw is False else None
    if isinstance(raw, int):
        return raw if 0 <= raw <= 3 else None
    text = str(raw).strip()
    if text.upper() in SYNCHRONOUS_LEVELS:
        return SYNCHRONOUS_LEVELS[text.upper()]
    if text.isdigit() and 0 <= int(text) <= 3:
        return int(text)
    return None


def _macos_barriers(conn: Executes) -> None:
    with contextlib.suppress(sqlite3.Error):
        conn.execute("PRAGMA checkpoint_fullfsync=1")
    with contextlib.suppress(sqlite3.Error):
        conn.execute(f"PRAGMA synchronous={SYNCHRONOUS_FULL}")


def apply_pragmas(
    conn: Executes, cfg: Mapping[str, Any] | None, *, platform: str | None = None
) -> None:
    """Configured pragmas plus the macOS durability floor; never raises for a bad value."""
    plat = sys.platform if platform is None else platform
    settings = pragma_settings(cfg)
    for name in INT_PRAGMAS:
        raw = settings.get(name)
        if raw is None:
            continue
        try:
            if isinstance(raw, bool):
                raise ValueError(raw)
            value = int(str(raw).strip())
        except ValueError:
            if _once(f"bad_{name}", repr(raw)):
                log.warning("ignoring database.%s=%r: not a whole number", name, raw)
            continue
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"PRAGMA {name}={value}")
    if plat == "darwin":
        _macos_barriers(conn)
    raw_sync = settings.get("synchronous")
    if raw_sync is None:
        return
    level = synchronous_level(raw_sync)
    if level is None:
        if _once("bad_synchronous", repr(raw_sync)):
            log.warning(
                "ignoring database.synchronous=%r (use OFF, NORMAL, FULL, EXTRA or 0-3)", raw_sync
            )
        return
    if plat == "darwin" and level < SYNCHRONOUS_FULL:
        if _once("macos_synchronous", repr(raw_sync)):
            log.warning(
                "keeping synchronous=FULL on macOS (database.synchronous=%r asked for less)",
                raw_sync,
            )
        return
    with contextlib.suppress(sqlite3.Error):
        conn.execute(f"PRAGMA synchronous={level}")
