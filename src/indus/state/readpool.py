"""Bounded read-only connections for WAL stores.

In WAL mode readers need no write lock, so reads borrow a read-only connection from a LIFO pool
and never queue behind the writer's mutex; reads are autocommit, so a read after a write sees it.
Read connections are file descriptors, and unbounded per-thread connections once pushed a process
into EMFILE where the error surfaced in unrelated tools (R03.17). The limits:

- at most 8 live read connections per database file, shared by every handle on that file in the
  process (idle pooled ones keep their permit);
- at most 24 per process across all files;
- none when fewer than 64 descriptors remain under the soft ``RLIMIT_NOFILE`` (the count is
  cached for 0.25 s; when it cannot be measured, reads are allowed).

A reader without a permit first closes an idle pooled connection anywhere in the process; when
there is none it reads on the writer connection — slower, never blocked forever. A failed open
blocks new opens for 60 s (a timestamp, not a sticky flag). In DELETE mode every read uses the
writer connection with ``busy_timeout`` raised to 5 s for the read.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import sqlite3
import threading
import weakref
from collections.abc import Callable, Generator
from pathlib import Path

from indus.foundation.clock import Clock
from indus.state.writer import Writer

__all__ = [
    "DELETE_READ_BUSY_MS",
    "FD_CACHE_SECONDS",
    "FD_RESERVE",
    "OPEN_RETRY_SECONDS",
    "POOL_MAX_PER_PATH",
    "POOL_MAX_PER_PROCESS",
    "ReadPool",
    "fd_soft_limit",
    "open_fd_count",
    "open_read_only",
    "process_permits_in_use",
    "read_only_uri",
    "reset_fd_cache",
]

log = logging.getLogger(__name__)

POOL_MAX_PER_PATH = 8
POOL_MAX_PER_PROCESS = 24
FD_RESERVE = 64
FD_CACHE_SECONDS = 0.25
OPEN_RETRY_SECONDS = 60.0
DELETE_READ_BUSY_MS = 5000
READ_BUSY_SECONDS = 5.0

_process_permits = threading.BoundedSemaphore(POOL_MAX_PER_PROCESS)
_process_in_use = 0
_process_lock = threading.Lock()


def process_permits_in_use() -> int:
    with _process_lock:
        return _process_in_use


def _take_process_permit() -> bool:
    global _process_in_use
    if not _process_permits.acquire(blocking=False):
        return False
    with _process_lock:
        _process_in_use += 1
    return True


def _give_process_permit() -> None:
    global _process_in_use
    with _process_lock:
        _process_in_use -= 1
    _process_permits.release()


# -- descriptor headroom ------------------------------------------------------------------------

_fd_lock = threading.Lock()
_fd_cache: tuple[float, int | None] | None = None


def open_fd_count() -> int | None:
    """Descriptors open in this process; None when unmeasurable, -1 when the probe hit EMFILE."""
    candidates = [Path("/proc/self/fd"), Path("/dev/fd")]
    existing = [d for d in candidates if d.is_dir()]
    if not existing:
        return None
    try:
        with os.scandir(existing[0]) as entries:
            return sum(1 for _ in entries)
    except OSError as exc:
        starved = exc.errno in (errno.EMFILE, errno.ENFILE)
        return -1 if starved else None


def fd_soft_limit() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return None
    if soft == resource.RLIM_INFINITY or soft < 0:
        return None
    return int(soft)


def reset_fd_cache() -> None:
    global _fd_cache
    with _fd_lock:
        _fd_cache = None


def _fd_headroom_ok(now: float) -> bool:
    global _fd_cache
    soft = fd_soft_limit()
    if soft is None:
        return True
    with _fd_lock:
        cached = _fd_cache
    if cached is None or now - cached[0] >= FD_CACHE_SECONDS or now < cached[0]:
        cached = (now, open_fd_count())
        with _fd_lock:
            _fd_cache = cached
    count = cached[1]
    if count is None:
        return True
    return count >= 0 and soft - count > FD_RESERVE


# -- per-path budgets ---------------------------------------------------------------------------


class _PathBudget:
    """Read permits for one database file, shared by every pool on it in this process."""

    def __init__(self) -> None:
        self.permits = threading.BoundedSemaphore(POOL_MAX_PER_PATH)
        self._lock = threading.Lock()
        self._members: weakref.WeakSet[ReadPool] = weakref.WeakSet()

    def register(self, pool: ReadPool) -> None:
        with self._lock:
            self._members.add(pool)

    def unregister(self, pool: ReadPool) -> None:
        with self._lock:
            self._members.discard(pool)

    def reclaim_idle(self) -> bool:
        with self._lock:
            members = list(self._members)
        return any(member.evict_one_idle() for member in members)

    def acquire(self, now: float) -> bool:
        if not _fd_headroom_ok(now):
            log.debug("not opening a read connection: too few file descriptors left")
            return False
        if not (_take_process_permit() or (_reclaim_anywhere() and _take_process_permit())):
            return False
        if self.permits.acquire(blocking=False) or (
            self.reclaim_idle() and self.permits.acquire(blocking=False)
        ):
            return True
        _give_process_permit()
        return False

    def release(self) -> None:
        self.permits.release()
        _give_process_permit()


_budgets: weakref.WeakValueDictionary[str, _PathBudget] = weakref.WeakValueDictionary()
_budgets_lock = threading.Lock()


def _budget_for(path: Path) -> _PathBudget:
    key = str(path.resolve(strict=False))
    with _budgets_lock:
        budget = _budgets.get(key)
        if budget is None:
            budget = _PathBudget()
            _budgets[key] = budget
        return budget


def _reclaim_anywhere() -> bool:
    with _budgets_lock:
        budgets = list(_budgets.values())
    return any(budget.reclaim_idle() for budget in budgets)


# -- connections --------------------------------------------------------------------------------


def read_only_uri(path: Path) -> str:
    """``file:`` URI with every special character percent-encoded, opened ``mode=ro``."""
    return f"{path.resolve(strict=False).as_uri()}?mode=ro"


def open_read_only(path: Path, *, timeout: float = READ_BUSY_SECONDS) -> sqlite3.Connection:
    conn = sqlite3.connect(
        read_only_uri(path),
        uri=True,
        timeout=timeout,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


class ReadPool:
    """Read connections for one handle; falls back to the writer connection."""

    def __init__(
        self,
        path: Path,
        writer: Writer,
        *,
        wal: bool,
        clock: Clock,
        setup: Callable[[sqlite3.Connection], None] | None = None,
        opener: Callable[[Path], sqlite3.Connection] | None = None,
        still_ours: Callable[[], bool] | None = None,
    ) -> None:
        self.path = path
        # New read connections open by path: when the file there is no longer the one the
        # writer opened (a restore), reads stay on the writer's connection instead.
        self._still_ours = still_ours
        self.wal = wal
        self._writer = writer
        self._clock = clock
        self._setup = setup
        self._opener = opener if opener is not None else open_read_only
        self._lock = threading.Lock()
        self._idle: list[sqlite3.Connection] = []
        self._live = 0
        self._closed = False
        self._failed_at: float | None = None
        self._budget = _budget_for(path)
        self._budget.register(self)

    # -- stats -----------------------------------------------------------------------------------

    @property
    def idle_count(self) -> int:
        with self._lock:
            return len(self._idle)

    @property
    def live_count(self) -> int:
        with self._lock:
            return self._live

    # -- borrowing -------------------------------------------------------------------------------

    @contextlib.contextmanager
    def read(self) -> Generator[sqlite3.Connection]:
        """A connection for read-only statements (pooled, else the writer's)."""
        conn = self._checkout()
        if conn is not None:
            try:
                yield conn
            finally:
                self._checkin(conn)
            return
        with self._writer.borrow() as wconn:
            if self.wal:
                yield wconn
                return
            previous = int(wconn.execute("PRAGMA busy_timeout").fetchone()[0])
            wconn.execute(f"PRAGMA busy_timeout={DELETE_READ_BUSY_MS}")
            try:
                yield wconn
            finally:
                with contextlib.suppress(sqlite3.Error):
                    wconn.execute(f"PRAGMA busy_timeout={previous}")

    def _checkout(self) -> sqlite3.Connection | None:
        if not self.wal:
            return None
        with self._lock:
            if self._closed:
                return None
            if self._idle:
                return self._idle.pop()
            failed_at = self._failed_at
        now = self._clock.monotonic()
        if failed_at is not None and now - failed_at < OPEN_RETRY_SECONDS:
            return None
        if self._still_ours is not None and not self._still_ours():
            return None
        if not self._budget.acquire(now):
            log.debug("read pool for %s is full; reading on the writer connection", self.path)
            return None
        conn: sqlite3.Connection | None = None
        try:
            conn = self._opener(self.path)
            if self._setup is not None:
                self._setup(conn)
        except sqlite3.Error:
            if conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
            self._budget.release()
            with self._lock:
                self._failed_at = self._clock.monotonic()
            log.debug("opening a read connection on %s failed", self.path, exc_info=True)
            return None
        except BaseException:
            if conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
            self._budget.release()
            raise
        with self._lock:
            self._live += 1
        return conn

    def _checkin(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            if not self._closed:
                self._idle.append(conn)
                return
        self._discard(conn)

    def _discard(self, conn: sqlite3.Connection) -> None:
        """Close one live connection and give its permits back, even when the close fails."""
        try:
            conn.close()
        except sqlite3.Error:
            log.debug("closing a read connection on %s failed", self.path, exc_info=True)
        finally:
            with self._lock:
                self._live -= 1
            self._budget.release()

    def evict_one_idle(self) -> bool:
        """Close one idle pooled connection (another reader needs its permit)."""
        with self._lock:
            if not self._idle:
                return False
            conn = self._idle.pop(0)
        self._discard(conn)
        return True

    def close(self) -> None:
        """Close idle connections; connections in use are closed by their reader."""
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for conn in idle:
            self._discard(conn)
        self._budget.unregister(self)
