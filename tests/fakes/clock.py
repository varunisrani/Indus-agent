"""A clock that only moves when a test moves it.

Code under test that waits takes a clock; tests hand it a ``FakeClock`` and call ``advance()``
instead of waiting on real time. This module (and the product clock in P01-19) is the only place
allowed to call ``asyncio.sleep``: ``checkpoint()`` below is the sanctioned way to give way to other
tasks for one loop iteration.

Threads: ``advance()`` may be called from any thread. ``sleep_sync()`` blocks the calling thread
until another thread advances the clock, so it refuses to run until ``allow_threaded_advance()``
has been called; on a single thread it would block forever.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# A blocked ``sleep_sync`` gives up after this much real time so a broken test fails instead of
# hanging the run.
SYNC_SLEEP_SAFETY_SECONDS = 30.0


async def checkpoint() -> None:
    """Give way to the event loop for exactly one iteration (no timed wait)."""
    await asyncio.sleep(0)


@dataclass(order=True)
class _Waiter:
    deadline: float
    seq: int
    future: asyncio.Future[None] | None = field(compare=False, default=None)
    loop: asyncio.AbstractEventLoop | None = field(compare=False, default=None)
    event: threading.Event | None = field(compare=False, default=None)
    removed: bool = field(compare=False, default=False)


class FakeClock:
    """Virtual wall and monotonic time with parked sleepers released by ``advance``."""

    def __init__(self, start_wall: datetime, start_mono: float = 0.0) -> None:
        if start_wall.tzinfo is None or start_wall.utcoffset() != timedelta(0):
            raise ValueError("start_wall must be an aware UTC datetime")
        self._start_wall = start_wall.astimezone(UTC)
        self._start_mono = float(start_mono)
        self._elapsed = 0.0
        self._waiters: list[_Waiter] = []
        self._seq = itertools.count()
        self._lock = threading.RLock()
        self._parked = threading.Condition(self._lock)
        self._threaded = False

    # -- reading -------------------------------------------------------------------------------

    def now(self) -> datetime:
        with self._lock:
            return self._start_wall + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        with self._lock:
            return self._start_mono + self._elapsed

    @property
    def pending(self) -> int:
        """Number of sleepers still parked."""
        with self._lock:
            return sum(1 for w in self._waiters if not w.removed)

    @property
    def sync_sleepers(self) -> int:
        """Threads parked in ``sleep_sync`` right now."""
        with self._lock:
            return sum(1 for w in self._waiters if not w.removed and w.event is not None)

    # -- sleeping ------------------------------------------------------------------------------

    async def sleep(self, seconds: float) -> None:
        """Park the caller until ``advance`` moves time past ``now + seconds``."""
        if seconds < 0:
            raise ValueError("sleep seconds must be >= 0")
        if seconds == 0:
            await checkpoint()
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        with self._lock:
            waiter = _Waiter(self.monotonic() + seconds, next(self._seq), future=future, loop=loop)
            heapq.heappush(self._waiters, waiter)
        try:
            await future
        finally:
            if not future.done() or future.cancelled():
                with self._lock:
                    waiter.removed = True

    def wait_for_sleepers(self, count: int, timeout: float = 5.0) -> bool:
        """Block (another thread) until ``count`` sync sleepers are parked; False on timeout."""
        with self._parked:
            return self._parked.wait_for(lambda: self.pending >= count, timeout)

    def allow_threaded_advance(self) -> None:
        """Declare that another thread drives ``advance``, which makes ``sleep_sync`` usable."""
        self._threaded = True

    def sleep_sync(self, seconds: float) -> None:
        """Block this thread until another thread advances past the deadline."""
        if not self._threaded:
            raise RuntimeError(
                "FakeClock.sleep_sync blocks until another thread calls advance(); "
                "call allow_threaded_advance() first, or use 'await clock.sleep()'"
            )
        if seconds < 0:
            raise ValueError("sleep seconds must be >= 0")
        if seconds == 0:
            return
        event = threading.Event()
        with self._lock:
            waiter = _Waiter(self.monotonic() + seconds, next(self._seq), event=event)
            heapq.heappush(self._waiters, waiter)
            self._parked.notify_all()
        if not event.wait(SYNC_SLEEP_SAFETY_SECONDS):
            with self._lock:
                waiter.removed = True
            raise TimeoutError("FakeClock.sleep_sync was never released by advance()")

    # -- driving -------------------------------------------------------------------------------

    def advance(self, seconds: float) -> int:
        """Move time forward and release every sleeper whose deadline has passed.

        Sleepers are released earliest deadline first, equal deadlines in the order they started
        sleeping. All of them observe the time after the whole advance. Returns how many were
        released (cancelled sleepers are not counted).
        """
        if seconds < 0:
            raise ValueError("advance seconds must be >= 0")
        released: list[_Waiter] = []
        with self._lock:
            self._elapsed += seconds
            now = self._start_mono + self._elapsed
            while self._waiters and self._waiters[0].deadline <= now:
                waiter = heapq.heappop(self._waiters)
                if not waiter.removed:
                    released.append(waiter)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        count = 0
        for waiter in released:
            if waiter.event is not None:
                waiter.event.set()
                count += 1
                continue
            future, loop = waiter.future, waiter.loop
            assert future is not None and loop is not None
            if future.done():
                continue
            if loop is running:
                future.set_result(None)
            else:
                loop.call_soon_threadsafe(_resolve, future)
            count += 1
        return count

    async def run_until_idle(self, max_iterations: int = 10_000) -> None:
        """Let every task that is ready run until none is left (timers and I/O excluded)."""
        loop = asyncio.get_running_loop()
        ready = getattr(loop, "_ready", None)
        for _ in range(max_iterations):
            await checkpoint()
            if ready is not None and len(ready) == 0:
                return
        if ready is not None:
            raise RuntimeError(f"loop still busy after {max_iterations} iterations")

    async def advance_and_settle(self, seconds: float) -> int:
        """``advance`` then ``run_until_idle``: the usual one-liner in async tests."""
        released = self.advance(seconds)
        await self.run_until_idle()
        return released


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)
