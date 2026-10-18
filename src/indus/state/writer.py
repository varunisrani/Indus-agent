"""The single writer: every mutation of a store runs here, one at a time per path per process.

``Writer.write(fn, budget=ROUTINE)`` takes the in-process mutex, opens ``BEGIN IMMEDIATE``, runs
``fn(conn)``, commits, and rolls back on any exception. Callbacks must not commit and must be
idempotent: when another process holds the write lock the whole callback is run again after a
jittered pause. Patience is a *deadline*, not a number of attempts (R03.8): a sibling's VACUUM or
checkpoint can legitimately hold the lock for seconds.

- ``ROUTINE`` 20 s, ``TRANSCRIPT`` 60 s (a lost transcript write aborts the turn),
  ``OBSERVATION`` 0.5 s (activity heartbeats; the next one will retry).
- Pauses: uniform 20–150 ms while less than 2 s of the budget has passed, then 250–1000 ms,
  never past the deadline. Time and pauses come from the injected clock.
- Busy/locked and "no more rows available" are retried; ``disk I/O error`` raised by
  ``BEGIN IMMEDIATE`` itself (the callback has not run) is retried once on the same connection;
  after the callback ran an I/O error propagates (its effect is unknown).
- Out of budget → ``StoreLocked``, whose text says it is contention, not damage, and avoids every
  word the error classifier would read as disk trouble or corruption.

After every 50 successful writes a ``wal_checkpoint(PASSIVE)`` runs; TRUNCATE never runs on this
path (R03.3). ``awrite`` hands the call to a single-thread executor owned by the writer, so an
event loop never blocks on the store (D-04.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import random
import sqlite3
import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from indus.foundation.clock import Clock
from indus.foundation.errors import StoreLocked
from indus.state.errors import is_disk_io_error, is_lock_error, is_no_more_rows

__all__ = [
    "CHECKPOINT_EVERY",
    "FAST_JITTER",
    "OBSERVATION",
    "ROUTINE",
    "SLOW_AFTER",
    "SLOW_JITTER",
    "TRANSCRIPT",
    "Budget",
    "WriteGuard",
    "Writer",
    "blocking_sleep",
    "jitter_pause",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Budget:
    name: str
    seconds: float


ROUTINE = Budget("routine", 20.0)
TRANSCRIPT = Budget("transcript", 60.0)
OBSERVATION = Budget("observation", 0.5)

FAST_JITTER = (0.020, 0.150)
SLOW_JITTER = (0.250, 1.000)
SLOW_AFTER = 2.0
CHECKPOINT_EVERY = 50


class WriteGuard(Protocol):
    """Integrity checks the writer runs (P02-07 ``FileGuard``)."""

    def before_write(self) -> None: ...

    def on_write_error(self, exc: BaseException) -> Literal["fts", "raise"]: ...

    @property
    def blocks_reopen(self) -> bool: ...


def _event_wait(seconds: float) -> None:
    threading.Event().wait(seconds)


def blocking_sleep(clock: Clock) -> Callable[[float], None]:
    """A blocking pause driven by ``clock`` (``sleep_sync`` when it has one)."""
    fn = getattr(clock, "sleep_sync", None)
    if callable(fn):
        return cast(Callable[[float], None], fn)
    return _event_wait


def jitter_pause(
    *,
    now: float,
    started: float,
    deadline: float,
    rng: random.Random,
    slow_only: bool = False,
) -> float | None:
    """The next pause before a retry, or None when the deadline has passed."""
    if now >= deadline:
        return None
    slow = slow_only or (now - started) >= SLOW_AFTER
    low, high = SLOW_JITTER if slow else FAST_JITTER
    return min(rng.uniform(low, high), max(deadline - now, 0.001))


class _NestedWrite(threading.local):
    depth: int = 0


class Writer:
    """One writer connection, its mutex and its retry policy."""

    def __init__(
        self,
        path: Path,
        conn: sqlite3.Connection,
        *,
        clock: Clock,
        reopen: Callable[[], sqlite3.Connection] | None = None,
        sleep: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
        guard: WriteGuard | None = None,
    ) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = conn
        self._clock = clock
        self._reopen = reopen
        self._sleep = sleep if sleep is not None else blocking_sleep(clock)
        self._rng = rng if rng is not None else random.Random()
        self._guard = guard
        self._mutex = threading.Lock()
        self._local = _NestedWrite()
        self._executor: ThreadPoolExecutor | None = None
        self._executor_lock = threading.Lock()
        self._periodic: list[tuple[int, Callable[[sqlite3.Connection], None]]] = []
        self._fts_fail_open: Callable[[BaseException], bool] | None = None
        self.write_count = 0
        self.closed = False

    # -- configuration ---------------------------------------------------------------------------

    def set_guard(self, guard: WriteGuard | None) -> None:
        self._guard = guard

    def add_periodic(self, every: int, fn: Callable[[sqlite3.Connection], None]) -> None:
        """Run ``fn(conn)`` (best effort, under the mutex) after every ``every`` writes."""
        if every <= 0:
            raise ValueError("every must be positive")
        self._periodic.append((every, fn))

    def set_fts_fail_open(self, fn: Callable[[BaseException], bool] | None) -> None:
        """P02-14: called with an FTS-scoped error; True means "detached, retry the write"."""
        self._fts_fail_open = fn

    @property
    def conn(self) -> sqlite3.Connection | None:
        return self._conn

    # -- the write path --------------------------------------------------------------------------

    def _connection_locked(self) -> sqlite3.Connection:
        """The live connection (caller holds the mutex); reopens once after a teardown race."""
        if self._conn is not None:
            return self._conn
        if self._guard is not None and self._guard.blocks_reopen:
            self._guard.before_write()  # raises the sticky replaced/corrupt error
        if self._reopen is None:
            raise sqlite3.ProgrammingError(f"the writer for {self.path} is closed")
        log.warning(
            "the store connection for %s was closed while a write was still queued; reopening",
            self.path,
        )
        self._conn = self._reopen()
        self.closed = False
        return self._conn

    def write[T](self, fn: Callable[[sqlite3.Connection], T], *, budget: Budget = ROUTINE) -> T:
        """Run ``fn(conn)`` in one ``BEGIN IMMEDIATE`` transaction; see the module docstring."""
        if self._local.depth:
            raise RuntimeError("a store write was started from inside another write callback")
        started = self._clock.monotonic()
        deadline = started + budget.seconds
        begin_io_retried = False
        while True:
            fn_ran = False
            try:
                with self._mutex:
                    if self._guard is not None:
                        self._guard.before_write()
                    conn = self._connection_locked()
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        fn_ran = True
                        self._local.depth += 1
                        try:
                            result = fn(conn)
                        finally:
                            self._local.depth -= 1
                        conn.execute("COMMIT")
                    except BaseException:
                        with contextlib.suppress(sqlite3.Error):
                            conn.execute("ROLLBACK")
                        raise
                self._after_success()
                return result
            except sqlite3.Error as exc:
                if self._retryable(exc, fn_ran, begin_io_retried):
                    if is_disk_io_error(exc) and not is_lock_error(exc):
                        begin_io_retried = True
                    pause = jitter_pause(
                        now=self._clock.monotonic(),
                        started=started,
                        deadline=deadline,
                        rng=self._rng,
                    )
                    if pause is not None:
                        self._sleep(pause)
                        continue
                    if is_lock_error(exc) or is_no_more_rows(exc):
                        raise self._locked_error(budget) from exc
                    raise
                if (
                    self._guard is not None
                    and isinstance(exc, sqlite3.DatabaseError)
                    and self._guard.on_write_error(exc) == "fts"
                    and self._fts_fail_open is not None
                    and self._fts_fail_open(exc)
                ):
                    continue
                raise

    def _retryable(self, exc: sqlite3.Error, fn_ran: bool, begin_io_retried: bool) -> bool:
        if is_no_more_rows(exc):
            return True
        if not isinstance(exc, sqlite3.OperationalError):
            return False
        if is_lock_error(exc):
            return True
        return is_disk_io_error(exc) and not fn_ran and not begin_io_retried

    def _locked_error(self, budget: Budget) -> StoreLocked:
        log.warning(
            "gave up on a %s write to %s after %.1fs: another process kept the write lock",
            budget.name,
            self.path,
            budget.seconds,
        )
        return StoreLocked(
            f"the session store at {self.path} stayed busy for {budget.seconds:g}s because "
            "another process held its write lock; this is contention, the stored data is fine",
            hint="try again in a moment; a long maintenance task may be running",
        )

    def _after_success(self) -> None:
        self.write_count += 1
        if self.write_count % CHECKPOINT_EVERY == 0:
            self.checkpoint_passive()
        for every, fn in self._periodic:
            if self.write_count % every == 0:
                try:
                    with self._mutex:
                        if self._conn is not None:
                            fn(self._conn)
                except Exception:  # periodic housekeeping must never fail a write
                    log.warning("periodic store task failed for %s", self.path, exc_info=True)

    # -- other access ----------------------------------------------------------------------------

    def checkpoint_passive(self) -> None:
        """Best-effort ``wal_checkpoint(PASSIVE)``; never raises, never TRUNCATE."""
        try:
            with self._mutex:
                if self._conn is None:
                    return
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        except sqlite3.Error as exc:
            log.warning("PASSIVE checkpoint of %s failed: %s", self.path, exc)

    @contextlib.contextmanager
    def borrow(self) -> Generator[sqlite3.Connection]:
        """The writer connection under the mutex (reads that cannot use the pool)."""
        if self._local.depth and self._conn is not None:
            yield self._conn  # inside a write callback on this thread: same transaction
            return
        with self._mutex:
            yield self._connection_locked()

    async def awrite[T](
        self, fn: Callable[[sqlite3.Connection], T], *, budget: Budget = ROUTINE
    ) -> T:
        """``write`` on the writer's own thread, awaited without blocking the loop."""
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()

        def call() -> T:
            return ctx.run(self.write, fn, budget=budget)

        return await loop.run_in_executor(self._get_executor(), call)

    def _get_executor(self) -> ThreadPoolExecutor:
        with self._executor_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix=f"indus-store-{self.path.name}"
                )
            return self._executor

    def close(self, *, checkpoint: bool = True) -> None:
        """Optional PASSIVE checkpoint, then close the connection and the executor."""
        with self._executor_lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True)
        with self._mutex:
            conn, self._conn = self._conn, None
            self.closed = True
            if conn is None:
                return
            if checkpoint:
                try:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                except sqlite3.Error as exc:
                    log.debug("PASSIVE checkpoint at close of %s failed: %s", self.path, exc)
            with contextlib.suppress(sqlite3.Error):
                conn.close()
