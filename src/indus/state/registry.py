"""Store handles and the process-wide registry that shares one handle per store file.

A ``StoreHandle`` owns a writer connection, a read pool, the integrity guard and a close-time
checkpoint. Two handles on one file in one process race each other's checkpoints, so long-lived
code shares one per resolved path: ``acquire(scope, path=None)`` returns the current generation
and bumps its refcount, ``handle.close()`` gives one reference back, and the last release tears
the handle down outside the registry lock (a slow close never stalls other paths).

When the file at the path is a different inode than the generation opened (backup restore,
recovery swap), that generation is retired — its holders keep it, its writes are fenced by the
guard — and a new generation opens. Teardowns of one path are counted behind a barrier, so an
acquire of that path waits until every admitted physical close has finished.

Open sequence of a handle: ``connection.open_store`` → (under the quarantine lock when the file
was missing or invalid) ``journal.apply`` + ``journal.apply_pragmas`` + ``schema.init`` → the guard
records ``(st_dev, st_ino)`` and the ``application_id`` → writer + read pool → registered data
migrations. Close sequence: PASSIVE checkpoint (skipped when quarantined or replaced) → close the
readers → close the writer → release the cached header descriptor when no other handle in the
process still uses the path.
"""

from __future__ import annotations

import contextlib
import logging
import os
import random
import sqlite3
import threading
import weakref
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager
from pathlib import Path

from indus.foundation.scope import Scope
from indus.state import connection, journal, schema
from indus.state.guards import FileGuard
from indus.state.readpool import ReadPool
from indus.state.writer import ROUTINE, Budget, Writer, blocking_sleep

__all__ = [
    "EXTENSIONS",
    "HANDLES_PER_PATH_WARN",
    "MIGRATIONS",
    "StoreHandle",
    "acquire",
    "close_all",
    "close_all_under",
    "live_handle_count",
    "open_unshared",
    "other_generations",
    "release",
    "shared",
]

log = logging.getLogger(__name__)

HANDLES_PER_PATH_WARN = 4

# Wired by later tasks: extra DDL inside schema init (P02-14 FTS) and data migrations.
EXTENSIONS: list[schema.SchemaExtension] = []
MIGRATIONS: list[schema.MigrationStep] = []


class StoreHandle:
    """One open store: writer, read pool, guard. Get one from ``acquire`` or ``open_unshared``."""

    def __init__(
        self,
        scope: Scope,
        path: Path,
        *,
        sleep: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.scope = scope
        self.path = path
        self.journal_mode: str = "delete"
        self.init_report = schema.InitReport()
        self._sleep = sleep if sleep is not None else blocking_sleep(scope.clock)
        self._rng = rng
        self._shared = False
        self._closed = False
        self._torn_down = False
        self._teardown_lock = threading.Lock()
        cfg = journal.database_config(scope)

        modes: list[str] = []

        def setup(conn: sqlite3.Connection) -> None:
            modes.append(journal.apply(conn, cfg, path))
            journal.apply_pragmas(conn, cfg)
            self.init_report = schema.init(
                conn,
                clock=scope.clock,
                sleep=self._sleep,
                rng=self._rng,
                extensions=tuple(EXTENSIONS),
            )

        opened = connection.open_store(scope, path, on_connected=setup)
        self.journal_mode = modes[-1]
        self.path = opened.path
        self.quarantined_file = opened.quarantined_to
        self.guard = FileGuard(opened.path, opened.identity, opened.application_id, opened.conn)

        def reopen() -> sqlite3.Connection:
            conn = connection.connect(self.path)
            journal.apply_pragmas(conn, cfg)
            self.guard.attach(conn)
            return conn

        self.writer = Writer(
            opened.path,
            opened.conn,
            clock=scope.clock,
            reopen=reopen,
            sleep=self._sleep,
            rng=self._rng,
            guard=self.guard,
        )

        def setup_reader(conn: sqlite3.Connection) -> None:
            journal.apply_pragmas(conn, cfg)

        self.reads = ReadPool(
            opened.path,
            self.writer,
            wal=self.journal_mode == "wal",
            clock=scope.clock,
            setup=setup_reader,
            still_ours=lambda: not (self.guard.replaced or self.guard.file_was_replaced()),
        )
        try:
            if MIGRATIONS:
                schema.run_migrations(self.writer.write, tuple(MIGRATIONS), clock=scope.clock)
        except BaseException:
            self._teardown()
            raise
        _track(self)

    # -- use -------------------------------------------------------------------------------------

    @property
    def wal(self) -> bool:
        return self.journal_mode == "wal"

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def quarantined(self) -> bool:
        return self.guard.quarantined

    @property
    def replaced(self) -> bool:
        return self.guard.replaced

    def write[T](self, fn: Callable[[sqlite3.Connection], T], *, budget: Budget = ROUTINE) -> T:
        return self.writer.write(fn, budget=budget)

    async def awrite[T](
        self, fn: Callable[[sqlite3.Connection], T], *, budget: Budget = ROUTINE
    ) -> T:
        return await self.writer.awrite(fn, budget=budget)

    def read(self) -> AbstractContextManager[sqlite3.Connection]:
        return self.reads.read()

    # -- lifecycle -------------------------------------------------------------------------------

    def close(self) -> None:
        """Release this reference (shared) or tear the handle down (unshared)."""
        if self._shared:
            release(self)
            return
        self._teardown()

    def _teardown(self) -> None:
        with self._teardown_lock:
            if self._torn_down:
                return
            self._torn_down = True
            self._closed = True
        checkpoint = not self.guard.skip_close_checkpoint
        if not checkpoint:
            log.warning(
                "closing %s without a checkpoint: this handle saw the file %s",
                self.path,
                "replaced" if self.guard.replaced else "damaged",
            )
        try:
            if checkpoint and self.writer.conn is not None:
                self.writer.checkpoint_passive()
        finally:
            try:
                self.reads.close()
            finally:
                try:
                    self.writer.close(checkpoint=False)
                finally:
                    _untrack(self)
                    if live_handle_count(self.path) == 0:
                        connection.release_header_fd(self.path)

    def __enter__(self) -> StoreHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"StoreHandle({self.path}, {self.journal_mode}, closed={self._closed})"


# -- live handle tracking (diagnostics and the header descriptor) -------------------------------

_track_lock = threading.Lock()
_live: dict[str, weakref.WeakSet[StoreHandle]] = {}
_warned_paths: set[str] = set()


def _key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def _track(handle: StoreHandle) -> None:
    key = _key(handle.path)
    with _track_lock:
        members = _live.setdefault(key, weakref.WeakSet())
        members.add(handle)
        count = len(members)
        warn = count > HANDLES_PER_PATH_WARN and key not in _warned_paths
        if warn:
            _warned_paths.add(key)
    if warn:
        log.warning(
            "%d store handles are open on %s in this process; long-lived code should share one "
            "(registry.acquire)",
            count,
            handle.path,
        )


def _untrack(handle: StoreHandle) -> None:
    key = _key(handle.path)
    with _track_lock:
        members = _live.get(key)
        if members is not None:
            members.discard(handle)
            if not members:
                _live.pop(key, None)


def live_handle_count(path: Path) -> int:
    with _track_lock:
        members = _live.get(_key(path))
        return len(members) if members is not None else 0


# -- the registry -------------------------------------------------------------------------------


class _Generation:
    __slots__ = ("handle", "identity", "path", "refcount", "retired")

    def __init__(self, path: Path, handle: StoreHandle, identity: tuple[int, int] | None) -> None:
        self.path = path
        self.handle = handle
        self.identity = identity
        self.refcount = 1
        self.retired = False


class _Barrier:
    """Admitted but unfinished physical closes of one path; the last one to settle lifts it."""

    __slots__ = ("event", "pending")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.pending = 0


_lock = threading.Lock()
_current: dict[Path, _Generation] = {}
_retired: dict[int, _Generation] = {}
_opening: dict[Path, threading.Event] = {}
_barriers: dict[Path, _Barrier] = {}


def _open_handle(scope: Scope, path: Path) -> StoreHandle:
    """Construct a handle (tests patch this)."""
    return StoreHandle(scope, path)


def _resolve(scope: Scope, path: Path | None) -> Path:
    raw = Path(path) if path is not None else connection.default_store_path(scope)
    return raw.resolve(strict=False)


def _admit_teardown_locked(path: Path) -> _Barrier:
    barrier = _barriers.get(path)
    if barrier is None:
        barrier = _barriers[path] = _Barrier()
    barrier.pending += 1
    return barrier


def _settle(path: Path, barrier: _Barrier) -> None:
    with _lock:
        barrier.pending -= 1
        if barrier.pending > 0:
            return
        if _barriers.get(path) is barrier:
            _barriers.pop(path, None)
    barrier.event.set()


def _teardown_handle(handle: StoreHandle) -> None:
    try:
        handle._teardown()  # pyright: ignore[reportPrivateUsage]
    except Exception:
        log.warning("closing the store handle for %s failed", handle.path, exc_info=True)


def acquire(scope: Scope, path: Path | None = None) -> StoreHandle:
    """The shared handle for the store of ``scope`` (or ``path``), with one more reference."""
    resolved = _resolve(scope, path)
    while True:
        wait_for: threading.Event | None = None
        opening: threading.Event | None = None
        with _lock:
            gen = _current.get(resolved)
            if gen is not None:
                now = connection.file_identity(resolved)
                if now is not None and gen.identity is not None and now != gen.identity:
                    gen.retired = True
                    del _current[resolved]
                    _retired[id(gen.handle)] = gen
                else:
                    gen.refcount += 1
                    return gen.handle
            barrier = _barriers.get(resolved)
            if barrier is not None:
                wait_for = barrier.event
            elif resolved in _opening:
                wait_for = _opening[resolved]
            else:
                opening = _opening[resolved] = threading.Event()
        if wait_for is not None:
            wait_for.wait()
            continue
        assert opening is not None
        try:
            handle = _open_handle(scope, resolved)
        except BaseException:
            with _lock:
                _opening.pop(resolved, None)
            opening.set()
            raise
        handle._shared = True  # pyright: ignore[reportPrivateUsage]
        with _lock:
            late = _barriers.get(resolved)
            if late is None:
                _current[resolved] = _Generation(resolved, handle, handle.guard.identity)
            _opening.pop(resolved, None)
        opening.set()
        if late is None:
            return handle
        # A close of this path was admitted while we were opening: never publish over it.
        _teardown_handle(handle)
        late.event.wait()


def release(handle: StoreHandle) -> bool:
    """Give back one reference; the last one tears the handle down. False if not shared."""
    barrier: _Barrier | None = None
    with _lock:
        gen = _retired.get(id(handle))
        if gen is None:
            gen = _current.get(_key_path(handle))
            if gen is None or gen.handle is not handle:
                return False
        gen.refcount -= 1
        if gen.refcount > 0:
            return True
        if gen.retired:
            _retired.pop(id(handle), None)
        elif _current.get(gen.path) is gen:
            _current.pop(gen.path, None)
        barrier = _admit_teardown_locked(gen.path)
    try:
        _teardown_handle(handle)
    finally:
        _settle(gen.path, barrier)
    return True


def _key_path(handle: StoreHandle) -> Path:
    return handle.path.resolve(strict=False)


def open_unshared(scope: Scope, path: Path | None = None) -> StoreHandle:
    """A private handle (one-shot callers, tests); ``close()`` tears it down directly."""
    return _open_handle(scope, _resolve(scope, path))


def _is_under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _sweep(select: Callable[[Path], bool]) -> int:
    with _lock:
        gens = [g for g in (*_current.values(), *_retired.values()) if select(g.path)]
        waiting = [b.event for p, b in _barriers.items() if select(p)]
        barriers = {g.path: _admit_teardown_locked(g.path) for g in gens}
        for gen in gens:
            gen.retired = True
            if _current.get(gen.path) is gen:
                _current.pop(gen.path, None)
            _retired.pop(id(gen.handle), None)
    for path, barrier in barriers.items():
        try:
            for gen in gens:
                if gen.path == path:
                    _teardown_handle(gen.handle)
        finally:
            _settle(path, barrier)
    for event in waiting:
        event.wait()
    return len(gens)


def close_all() -> int:
    """Close every shared handle regardless of references (process shutdown). Idempotent."""
    return _sweep(lambda _path: True)


def close_all_under(directory: Path) -> int:
    """Close every shared handle whose file lives under ``directory`` (profile delete/rename)."""
    root = Path(directory).expanduser().resolve(strict=False)
    return _sweep(lambda path: _is_under(path, root))


def other_generations(path: Path, *, exclude: StoreHandle | None = None) -> list[str]:
    """Live (not retired) shared handles on ``path`` other than ``exclude`` (VACUUM gate)."""
    resolved = Path(path).resolve(strict=False)
    with _lock:
        return [
            f"in-process store handle (refcount {g.refcount})"
            for g in _current.values()
            if g.path == resolved and g.handle is not exclude and not g.retired
        ]


@contextlib.contextmanager
def shared(scope: Scope, path: Path | None = None) -> Generator[StoreHandle]:
    """``with shared(scope) as store:`` — acquire and release around a block."""
    handle = acquire(scope, path)
    try:
        yield handle
    finally:
        handle.close()
