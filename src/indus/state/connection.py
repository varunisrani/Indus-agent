"""Opening a store: the test-isolation guard, header probe, quarantine, modes and the connect.

``open_store(scope, path=None)`` returns a ``StoreFile``: one writable SQLite connection on
``<home>/state.db`` (or ``path``) plus the file identity recorded at open. The order is fixed:

1. the test-isolation guard (before anything touches the disk);
2. the parent folder, created through the profile-safe mkdir (a deleted profile is never
   re-created by a stale caller);
3. a probe of the first 100 bytes; a file that is not SQLite is moved aside with its sidecars
   under a cross-process lock and re-checked under that lock;
4. the file is created / tightened to 0600 before SQLite opens it, then a writability preflight;
5. ``sqlite3.connect`` (1 s busy timeout, autocommit, usable across threads), foreign keys on;
6. ``on_connected(conn)`` — journal, pragmas and schema setup by the caller — still under the
   quarantine lock when the file was missing or invalid, so a racing opener never sees our
   half-made file as garbage;
7. ``(st_dev, st_ino)`` and the header ``application_id`` recorded for the integrity guard.

Header bytes are read through one cached descriptor per path that is never closed while the
process may hold SQLite connections on the file: opening and closing any descriptor on a SQLite
file drops the POSIX locks SQLite holds on it in this process (R03.2).
"""

from __future__ import annotations

import contextlib
import functools
import itertools
import logging
import os
import sqlite3
import struct
import subprocess
import sys
import threading
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path

from indus.foundation.errors import (
    StoreIsolationRefused,
    StoreNotWritable,
    StoreUnopenable,
)
from indus.foundation.fsio import FileLock, LockTimeout
from indus.foundation.home import APP_DIR, PROFILES_DIR, is_managed, secure_dir
from indus.foundation.profiles import mkdir_in_profile
from indus.foundation.scope import Scope

__all__ = [
    "DB_NAME",
    "GUARD_BYPASS_ENV",
    "HEADER_BYTES",
    "SQLITE_MAGIC",
    "TEST_ISOLATION_ENV",
    "StoreFile",
    "check_isolation",
    "connect",
    "connection_application_id",
    "default_store_path",
    "file_identity",
    "header_application_id",
    "header_says_wal",
    "invalid_header_kind",
    "looks_like_test_run",
    "open_store",
    "quarantine_invalid",
    "read_header",
    "real_default_root",
    "release_header_fd",
]

log = logging.getLogger(__name__)

DB_NAME = "state.db"
SQLITE_MAGIC = b"SQLite format 3\x00"
HEADER_BYTES = 100
APPLICATION_ID_OFFSET = 68
SIDECARS = ("-wal", "-shm")
QUARANTINE_LOCK = ".quarantine.lock"
QUARANTINE_LOCK_SECONDS = 5.0
GUARD_BYPASS_ENV = "INDUS_STATE_DB_GUARD_BYPASS"
TEST_ISOLATION_ENV = "INDUS_TEST_ISOLATION"
TEST_ENV_MARKERS = ("PYTEST_CURRENT_TEST", TEST_ISOLATION_ENV)
ANCESTOR_LEVELS = 8
TEST_RUNNER_NAMES = frozenset({"pytest", "py.test", "pytest.exe", "py.test.exe", "unittest"})
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})


@dataclass
class StoreFile:
    """An open writable connection and what the integrity guard needs to know about the file."""

    path: Path
    conn: sqlite3.Connection
    identity: tuple[int, int] | None
    application_id: int
    quarantined_to: Path | None = None


def default_store_path(scope: Scope) -> Path:
    return scope.home / DB_NAME


# -- test-isolation guard -----------------------------------------------------------------------


def _passwd_home() -> Path | None:
    """The account's home from the passwd database (POSIX); never ``$HOME`` or patched helpers."""
    try:
        import pwd
    except ImportError:
        return None
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return None


def real_default_root(environ: Mapping[str, str] | None = None) -> Path | None:
    """The default root the real user's store lives under, ignoring ``INDUS_HOME``."""
    env = os.environ if environ is None else environ
    if sys.platform == "win32":
        local = env.get("LOCALAPPDATA", "").strip()
        if local:
            return Path(local) / APP_DIR
        profile = env.get("USERPROFILE", "").strip()
        return Path(profile) / "AppData" / "Local" / APP_DIR if profile else None
    home = _passwd_home()
    return home / f".{APP_DIR}" if home is not None else None


def _cmdline_is_test_runner(argv: list[str]) -> bool:
    for arg in argv:
        name = arg.strip("'\"").replace("\\", "/").rsplit("/", 1)[-1].lower()
        if name in TEST_RUNNER_NAMES:
            return True
    return False


def _proc_parent_and_argv(pid: int) -> tuple[int, list[str]] | None:
    """Linux: parent pid and argv from ``/proc``; macOS: from ``ps``."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        # Fields after the parenthesised command name: state, ppid, ...
        tail = stat.rsplit(")", 1)[-1].split()
        if len(tail) < 2 or not tail[1].isdigit():
            return None
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        return int(tail[1]), argv
    if sys.platform == "darwin":
        try:
            done = subprocess.run(
                ["ps", "-o", "ppid=", "-o", "args=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        line = done.stdout.strip()
        head, _, rest = line.partition(" ")
        if not head.isdigit():
            return None
        return int(head), rest.split()
    return None


@functools.cache
def _has_test_runner_ancestor() -> bool:
    pid = os.getppid()
    for _ in range(ANCESTOR_LEVELS):
        if pid <= 1:
            return False
        found = _proc_parent_and_argv(pid)
        if found is None:
            return False
        parent, argv = found
        if _cmdline_is_test_runner(argv):
            return True
        pid = parent
    return False


def looks_like_test_run(
    environ: Mapping[str, str] | None = None,
    *,
    ancestry: Callable[[], bool] | None = None,
) -> bool:
    """True when this process runs under a test runner (env markers, else ancestry)."""
    env = os.environ if environ is None else environ
    if any(env.get(name) for name in TEST_ENV_MARKERS):
        return True
    return (ancestry or _has_test_runner_ancestor)()


def _is_live_store_path(path: Path, root: Path) -> bool:
    if path.parent == root:
        return True
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return len(parts) == 3 and parts[0] == PROFILES_DIR


def check_isolation(
    path: Path,
    environ: Mapping[str, str] | None = None,
    *,
    ancestry: Callable[[], bool] | None = None,
) -> None:
    """Refuse the real user's store from inside a test run (R03.24)."""
    env = os.environ if environ is None else environ
    if env.get(GUARD_BYPASS_ENV, "").strip().lower() in _TRUE_WORDS:
        return
    root = real_default_root(env)
    if root is None:
        return
    resolved = path.resolve(strict=False)
    real = root.resolve(strict=False)
    if not _is_live_store_path(resolved, real):
        return
    if not looks_like_test_run(env, ancestry=ancestry):
        return
    raise StoreIsolationRefused(
        f"refusing to open the real session store {resolved} from a test run",
        hint=f"point INDUS_HOME at a temporary folder; {GUARD_BYPASS_ENV}=1 overrides this",
    )


# -- header probe -------------------------------------------------------------------------------

_probe_lock = threading.Lock()
_probe_fds: dict[str, tuple[int, int, int]] = {}  # path -> (fd, st_dev, st_ino)
_retired_fds: list[int] = []  # kept open on purpose: closing could drop SQLite's locks


def _probe_key(path: Path) -> str:
    return os.path.abspath(path)


def read_header(path: Path, length: int = HEADER_BYTES) -> bytes | None:
    """The first ``length`` bytes of ``path`` without an open/close pair; None when unreadable.

    POSIX uses one cached descriptor per path (``os.pread``); a new inode at the path retires the
    old descriptor without closing it. Windows reads plainly (no POSIX lock hazard there).
    """
    if sys.platform == "win32":
        try:
            with path.open("rb") as handle:
                return handle.read(length)
        except OSError:
            return None
    key = _probe_key(path)
    try:
        st = os.stat(path)
    except OSError:
        return None
    with _probe_lock:
        cached = _probe_fds.get(key)
        if cached is not None and (cached[1], cached[2]) != (st.st_dev, st.st_ino):
            _retired_fds.append(_probe_fds.pop(key)[0])
            cached = None
        if cached is None:
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            except OSError:
                return None
            fst = os.fstat(fd)
            cached = _probe_fds[key] = (fd, fst.st_dev, fst.st_ino)
        try:
            return os.pread(cached[0], length, 0)
        except OSError:
            return None


def release_header_fd(path: Path) -> None:
    """Close the cached descriptors for ``path``; only when no SQLite connection is left on it."""
    key = _probe_key(path)
    with _probe_lock:
        cached = _probe_fds.pop(key, None)
    if cached is not None:
        with contextlib.suppress(OSError):
            os.close(cached[0])


def header_application_id(path: Path) -> int | None:
    header = read_header(path, APPLICATION_ID_OFFSET + 4)
    if header is None or len(header) < APPLICATION_ID_OFFSET + 4:
        return None
    if not header.startswith(SQLITE_MAGIC):
        return None
    return int(struct.unpack(">I", header[APPLICATION_ID_OFFSET : APPLICATION_ID_OFFSET + 4])[0])


def header_says_wal(header: bytes | None) -> bool | None:
    """True/False from header bytes 18/19 (2 = WAL); None when the header is not readable."""
    if header is None or len(header) < 20 or not header.startswith(SQLITE_MAGIC):
        return None
    return header[18] == 2 or header[19] == 2


def invalid_header_kind(path: Path) -> str | None:
    """``zeroed`` / ``notadb`` for an existing file that is not SQLite; None when fine or absent."""
    if not path.is_file():
        return None
    head = read_header(path)
    if head is None:
        return None
    if head.startswith(SQLITE_MAGIC):
        return None
    if all(b == 0 for b in head):  # also the 0-byte file
        return "zeroed"
    return "notadb"


def file_identity(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


# -- quarantine ---------------------------------------------------------------------------------


def _stamp(scope: Scope) -> str:
    return scope.clock.now().strftime("%Y%m%d-%H%M%S")


def quarantine_invalid(path: Path, kind: str, stamp: str) -> Path | None:
    """Move ``path`` and its sidecars aside; the backup path, or None when the move failed."""
    base = f"{path.name}.{kind}-{stamp}-{os.getpid()}"
    dest = next(
        candidate
        for candidate in (
            path.parent / (base + (f"-{n}" if n else "") + ".bak") for n in itertools.count()
        )
        if not candidate.exists()
    )
    try:
        os.rename(path, dest)
    except OSError as exc:
        log.error("could not move the invalid store %s aside: %s", path, exc)
        return None
    release_header_fd(path)
    for suffix in SIDECARS:
        side = Path(f"{path}{suffix}")
        if side.exists():
            try:
                os.rename(side, Path(f"{dest}{suffix}"))
            except OSError as exc:
                log.error("could not move %s aside: %s", side, exc)
    log.error(
        "the session store %s was %s and was moved to %s; a new empty store is used. "
        "Run `indus doctor` to see how to bring old sessions back.",
        path,
        "empty" if kind == "zeroed" else "not a SQLite file",
        dest,
    )
    return dest


@contextlib.contextmanager
def _quarantine_lock(path: Path) -> Generator[bool]:
    seconds = QUARANTINE_LOCK_SECONDS
    lock = FileLock(path.parent / QUARANTINE_LOCK, timeout=seconds)
    try:
        lock.acquire()
    except LockTimeout:
        log.warning(
            "could not take %s within %gs; going on without it (nothing is moved aside)",
            path.parent / QUARANTINE_LOCK,
            seconds,
        )
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


# -- modes and preflight ------------------------------------------------------------------------


def _ensure_parent(scope: Scope, path: Path) -> None:
    parent = path.parent
    home = scope.home
    if parent == home or home in parent.parents:
        mkdir_in_profile(home, parent.relative_to(home))
    else:
        secure_dir(parent, env=scope.env)


def _create_owner_only(scope: Scope, path: Path) -> None:
    """Create the file 0600 (O_EXCL, so the umask cannot widen it) or tighten an existing one."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if os.name != "nt" and not is_managed(scope.home, scope.env):
            mode = os.stat(path).st_mode & 0o777
            if mode & 0o077:
                # Drop group/other bits only: a deliberately read-only file stays read-only.
                os.chmod(path, mode & 0o700)
        return
    os.close(fd)


def _writable(path: Path) -> bool:
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return True  # absent: the directory check decides
    return os.access(path, os.W_OK) and bool(mode & 0o222)


def _preflight(path: Path, *, directory_only: bool = False) -> None:
    hint = (
        "the file probably belongs to another user (for example after running with sudo); "
        "fix it with chown/chmod so this user can write it"
    )
    if not os.access(path.parent, os.W_OK | os.X_OK):
        raise StoreNotWritable(
            f"cannot write to the folder of the session store: {path.parent}", hint=hint
        )
    if directory_only:
        return
    for candidate in (path, *(Path(f"{path}{suffix}") for suffix in SIDECARS)):
        if not _writable(candidate):
            raise StoreNotWritable(f"the session store file is read-only: {candidate}", hint=hint)


# -- connect ------------------------------------------------------------------------------------


def connect(path: Path, *, timeout: float = 1.0) -> sqlite3.Connection:
    """A writer connection: autocommit, cross-thread, ``sqlite3.Row`` rows, foreign keys on."""
    conn = sqlite3.connect(
        str(path), timeout=timeout, isolation_level=None, check_same_thread=False
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        conn.close()
        raise
    return conn


def _close_quietly(conn: sqlite3.Connection) -> None:
    with contextlib.suppress(sqlite3.Error):
        conn.close()


def _connect_and_setup(
    path: Path,
    timeout: float,
    on_connected: Callable[[sqlite3.Connection], None] | None,
) -> sqlite3.Connection:
    conn = connect(path, timeout=timeout)
    try:
        if on_connected is not None:
            on_connected(conn)
    except BaseException:
        _close_quietly(conn)
        raise
    return conn


def open_store(
    scope: Scope,
    path: Path | None = None,
    *,
    on_connected: Callable[[sqlite3.Connection], None] | None = None,
    timeout: float = 1.0,
    environ: Mapping[str, str] | None = None,
) -> StoreFile:
    """Open the store for ``scope`` (see the module docstring for the order of steps)."""
    target = Path(path) if path is not None else default_store_path(scope)
    if not target.is_absolute():
        target = target.resolve()
    check_isolation(target, environ)
    _ensure_parent(scope, target)

    quarantined: Path | None = None
    if not target.exists() or invalid_header_kind(target) is not None:
        _preflight(target, directory_only=True)
        with _quarantine_lock(target) as locked:
            kind = invalid_header_kind(target)
            if kind is not None and not locked:
                # Moving a file aside without the lock could race another opener's move.
                raise StoreUnopenable(
                    f"the session store {target} is not a SQLite file, and another process "
                    "is busy setting it up",
                    hint="try again in a moment",
                )
            if kind is not None:
                quarantined = quarantine_invalid(target, kind, _stamp(scope))
                if quarantined is None and invalid_header_kind(target) is not None:
                    raise StoreUnopenable(
                        f"the session store {target} is not a SQLite file and could not be "
                        "moved aside",
                        hint="move it away by hand, then start again",
                    )
            _create_owner_only(scope, target)
            _preflight(target)
            conn = _connect_and_setup(target, timeout, on_connected)
    else:
        _create_owner_only(scope, target)
        _preflight(target)
        conn = _connect_and_setup(target, timeout, on_connected)

    try:
        identity = file_identity(target)
        app_id = _application_id_of(conn, target)
    except BaseException:
        _close_quietly(conn)
        raise
    return StoreFile(target, conn, identity, app_id, quarantined)


def connection_application_id(conn: sqlite3.Connection) -> int:
    """``PRAGMA application_id`` on ``conn`` (0 when it cannot be read)."""
    try:
        row = conn.execute("PRAGMA application_id").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row else 0


def _application_id_of(conn: sqlite3.Connection, path: Path) -> int:
    """The stamp from the header, else from the connection (it may still sit in the WAL)."""
    return header_application_id(path) or connection_application_id(conn)
