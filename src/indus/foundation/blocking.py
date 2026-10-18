"""The one way blocking work leaves the event loop (D-04.1).

File reads, synchronous tool handlers and synchronous plugin hooks run on a small named pool of
daemon worker threads through ``run_blocking``. The awaiting task never blocks the loop, the
caller's context variables (the profile ``Scope`` among them, R01.16) travel into the worker, and
a deadline turns a call that does not come back into ``DeadlineMissed`` for the caller.

Abandoned, never killed: Python cannot stop a thread, so work past its deadline (or whose caller
was cancelled) is left to finish on its own. It keeps its worker until it does, whatever it
returns or raises later is dropped, and one WARNING says so. Pools never grow. When every worker
is busy, the ``io`` and ``tools`` pools let a bounded number of calls wait for a worker in arrival
order (the wait counts against the caller's deadline; a call whose caller gave up while waiting
never runs); past that bound, and at once in the ``hooks`` pool, new work is refused with
``PoolFull``. Workers are daemon threads, so a stuck call never holds up process exit, and
``shutdown`` never waits for one.

Child processes are not this module's job: the terminal runner owns their timeout and kill.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import queue
import threading
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from indus.foundation.clock import Clock
from indus.foundation.errors import IndusError

__all__ = [
    "POOLS",
    "POOL_QUEUE_LIMITS",
    "POOL_SIZES",
    "BlockingError",
    "BlockingPool",
    "DeadlineMissed",
    "PoolClosed",
    "PoolFull",
    "run_blocking",
]

log = logging.getLogger(__name__)

# Workers per named pool. ``hooks`` is small on purpose: a hung plugin hook should be refused
# quickly rather than pile up threads (the size the P00/P03 trial measured).
POOL_SIZES: Mapping[str, int] = MappingProxyType({"io": 8, "tools": 8, "hooks": 2})
# Calls that may wait for a busy pool's worker before new work is refused. Many sessions starting
# turns at once read their transcripts through ``io``/``tools`` (D-04.1 criterion c), so those
# queue; a hung plugin hook must not collect a backlog behind it, so ``hooks`` refuses at once.
POOL_QUEUE_LIMITS: Mapping[str, int] = MappingProxyType({"io": 256, "tools": 256, "hooks": 0})


class BlockingError(IndusError):
    default_code = "blocking.error"


class PoolFull(BlockingError):
    """Every worker of the pool is busy (or stuck) and no waiting place is free; the work was
    refused, nothing ran."""

    default_code = "blocking.pool_full"


class PoolClosed(BlockingError):
    """The pool was shut down; the work was refused, nothing ran."""

    default_code = "blocking.pool_closed"


class DeadlineMissed(BlockingError, TimeoutError):
    """The work did not finish in time. It was abandoned, not stopped: its thread runs on."""

    default_code = "blocking.deadline_missed"


def _describe(fn: Callable[..., object]) -> str:
    target = getattr(fn, "func", fn)  # functools.partial
    return getattr(target, "__qualname__", None) or type(target).__name__


def _settle(future: asyncio.Future[Any], value: object, error: BaseException | None) -> None:
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(value)


def _drain(future: asyncio.Future[Any]) -> None:
    # Fetch a late error so asyncio never reports it as "exception was never retrieved".
    if not future.cancelled():
        future.exception()


class _Job:
    __slots__ = ("abandoned", "args", "context", "fn", "future", "loop", "started")

    def __init__(
        self,
        fn: Callable[..., object],
        args: tuple[object, ...],
        loop: asyncio.AbstractEventLoop,
        future: asyncio.Future[Any],
    ) -> None:
        self.fn = fn
        self.args = args
        self.context = contextvars.copy_context()
        self.loop = loop
        self.future = future
        self.abandoned = False
        self.started = False

    def execute(self, release: Callable[[], None]) -> None:
        """Run the call, ``release`` the worker, then hand the outcome to the loop. The worker is
        free before the caller can resume, so its next call is never refused for it."""
        value: object = None
        error: BaseException | None = None
        try:
            value = self.context.run(self.fn, *self.args)
        except BaseException as exc:  # handed to the awaiting task, never raised on this thread
            error = exc
        release()
        try:
            with contextlib.suppress(RuntimeError):  # the loop is closed: nobody is waiting
                self.loop.call_soon_threadsafe(_settle, self.future, value, error)
        finally:
            del value, error


class BlockingPool:
    """A fixed number of daemon worker threads for one purpose; it never grows.

    Threads start lazily, one per concurrently running call, up to ``workers``. With every worker
    busy, up to ``queue_limit`` further calls wait for one in arrival order; 0 refuses at once.
    """

    def __init__(self, name: str, workers: int, *, queue_limit: int = 0) -> None:
        if workers < 1:
            raise ValueError("a blocking pool needs at least one worker")
        if queue_limit < 0:
            raise ValueError("queue_limit must be >= 0")
        self.name = name
        self.workers = workers
        self.queue_limit = queue_limit
        self._lock = threading.Lock()
        self._queue: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._busy = 0  # accepted and not finished: running (or stuck) plus waiting
        self._closed = False

    def __repr__(self) -> str:
        return f"BlockingPool({self.name!r}, workers={self.workers}, busy={self.busy})"

    @property
    def busy(self) -> int:
        """Workers taken; abandoned work keeps its worker until it really returns."""
        with self._lock:
            return min(self._busy, self.workers)

    @property
    def waiting(self) -> int:
        """Accepted calls waiting for a worker."""
        with self._lock:
            return max(0, self._busy - self.workers)

    @property
    def threads(self) -> int:
        """Worker threads alive right now (never more than ``workers``)."""
        with self._lock:
            return sum(1 for t in self._threads if t.is_alive())

    @property
    def closed(self) -> bool:
        return self._closed

    async def run[T, *Ts](
        self,
        fn: Callable[[*Ts], T],
        /,
        *args: *Ts,
        deadline: float | None = None,
        clock: Clock | None = None,
        label: str = "",
    ) -> T:
        """``fn(*args)`` on a worker thread, in a copy of the caller's context.

        ``deadline`` is in seconds on ``clock`` (loop time when None) and covers any wait for a
        worker; ``None`` waits for ever. Raises ``PoolFull`` / ``PoolClosed`` before anything
        runs, ``DeadlineMissed`` when the deadline passes first, or whatever ``fn`` raised (same
        object, same traceback).
        """
        if deadline is not None and deadline < 0:
            raise ValueError("deadline must be >= 0 seconds")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        future.add_done_callback(_drain)
        job = _Job(fn, tuple(args), loop, future)
        self._submit(job)
        what = label or _describe(fn)
        try:
            finished = await _wait(future, deadline, clock)
        except asyncio.CancelledError:
            job.abandoned = True
            log.debug("%s abandoned in the %s pool: its caller was cancelled", what, self.name)
            raise
        if not finished:
            with self._lock:  # atomic with the worker's start check: a job either runs or not
                job.abandoned = True
                started = job.started
            if not started:
                log.warning(
                    "%s missed its %g s deadline waiting for a worker of the %s pool; it will "
                    "not run",
                    what,
                    deadline,
                    self.name,
                )
                raise DeadlineMissed(f"{what} did not start within {deadline:g} s")
            log.warning(
                "%s missed its %g s deadline in the %s pool; abandoned, its thread runs on "
                "(%d of %d workers busy)",
                what,
                deadline,
                self.name,
                self.busy,
                self.workers,
            )
            raise DeadlineMissed(f"{what} did not finish within {deadline:g} s")
        return future.result()

    def shutdown(self) -> None:
        """Refuse new work and let idle workers stop. Never waits: stuck workers are left."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            count = len(self._threads)
        for _ in range(count):
            self._queue.put(None)  # behind any queued work (FIFO), so that still runs

    # -- worker side --------------------------------------------------------------------------

    def _submit(self, job: _Job) -> None:
        with self._lock:
            if self._closed:
                raise PoolClosed(f"the {self.name} pool is shut down")
            if self._busy >= self.workers + self.queue_limit:
                waiting = f" and {self.queue_limit} calls are waiting" if self.queue_limit else ""
                raise PoolFull(
                    f"all {self.workers} workers of the {self.name} pool are busy{waiting}",
                    hint="a stuck call keeps its worker until it returns",
                )
            self._busy += 1
            self._threads = [t for t in self._threads if t.is_alive()]
            if len(self._threads) < min(self._busy, self.workers):
                thread = threading.Thread(
                    target=self._work,
                    name=f"indus-{self.name}-{len(self._threads) + 1}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()
            self._queue.put(job)

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            released = False

            def release() -> None:
                nonlocal released
                if not released:
                    released = True
                    with self._lock:
                        self._busy -= 1

            with self._lock:
                job.started = not job.abandoned  # its caller may have given up while it waited
            try:
                if job.started:
                    job.execute(release)
            finally:
                release()
                del job


async def _wait(future: asyncio.Future[Any], deadline: float | None, clock: Clock | None) -> bool:
    """True once ``future`` is done; False when ``deadline`` passed first."""
    if deadline is None:
        await asyncio.wait({future})
        return True
    if clock is None:
        await asyncio.wait({future}, timeout=deadline)
        return future.done()
    timer = asyncio.ensure_future(clock.sleep(deadline))
    try:
        await asyncio.wait({future, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    return future.done()  # a result that lands with the timer still wins


POOLS: Mapping[str, BlockingPool] = MappingProxyType(
    {
        name: BlockingPool(name, size, queue_limit=POOL_QUEUE_LIMITS.get(name, 0))
        for name, size in POOL_SIZES.items()
    }
)


async def run_blocking[T, *Ts](
    fn: Callable[[*Ts], T],
    /,
    *args: *Ts,
    pool: str | BlockingPool = "io",
    deadline: float | None = None,
    clock: Clock | None = None,
    label: str = "",
) -> T:
    """Run blocking ``fn(*args)`` on a named pool (``io``, ``tools``, ``hooks``) or a given one.

    See ``BlockingPool.run`` for the deadline and the errors.
    """
    if isinstance(pool, str):
        try:
            target = POOLS[pool]
        except KeyError:
            raise ValueError(
                f"no blocking pool named {pool!r} (have: {', '.join(POOLS)})"
            ) from None
    else:
        target = pool
    return await target.run(fn, *args, deadline=deadline, clock=clock, label=label)
