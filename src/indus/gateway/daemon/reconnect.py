"""Bring failed platforms back (P15-29).

A platform whose connect failed with a retryable error waits in the watcher's queue and is tried
again after 30 s, then 60, 120, 240 s and every 300 s after that, forever: outages heal on their
own. A non-retryable failure (bad credentials, a token another gateway holds at startup, ...)
leaves the queue as ``fatal``. A platform that has kept failing for
``agent.reconnect_attention_after`` seconds (read on every pass; 0 or less turns it off) is
flagged ``needs_attention`` once — a signal for status and ``/platform list``, never a pause.
Pausing is manual only: ``/platform pause <name>`` holds the retries, ``resume`` makes the
platform due at once.

The daemon hands in ``connect(name)``, which builds the adapter, connects it as a reconnect and
installs it on success, answering a ``ConnectOutcome``; an exception from it counts as a
retryable failure. ``on_change(name, health)`` reports every state change (``None`` when the
platform is connected again) so the daemon can update its runtime status.

The loop itself is supervised: when it crashes it is started again after 2, 4, 8 … 60 s; after
five crashes in a row, each within 300 s of its start, supervision gives up with an error log.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal

from indus.foundation.clock import Clock

__all__ = [
    "BACKOFF_CAP_S",
    "BACKOFF_FIRST_S",
    "ConnectOutcome",
    "PlatformHealth",
    "ReconnectWatcher",
    "backoff_s",
]

log = logging.getLogger(__name__)

BACKOFF_FIRST_S = 30.0
BACKOFF_CAP_S = 300.0
POLL_S = 30.0
RESPAWN_LIMIT = 5
RESPAWN_HEALTHY_S = 300.0
RESPAWN_CAP_S = 60.0


def backoff_s(attempt: int) -> float:
    """Wait before try number ``attempt`` (1-based): 30, 60, 120, 240, 300, 300, ..."""
    return min(BACKOFF_FIRST_S * 2 ** max(0, attempt - 1), BACKOFF_CAP_S)


@dataclass(frozen=True)
class ConnectOutcome:
    state: Literal["connected", "retryable", "fatal"]
    error: str = ""
    error_code: str = ""

    @classmethod
    def connected(cls) -> ConnectOutcome:
        return cls("connected")

    @classmethod
    def retryable(cls, error: str, code: str = "") -> ConnectOutcome:
        return cls("retryable", error, code)

    @classmethod
    def fatal(cls, error: str, code: str = "") -> ConnectOutcome:
        return cls("fatal", error, code)


@dataclass(frozen=True)
class PlatformHealth:
    """One queued platform. Times are the clock's monotonic seconds; ``since`` is wall time."""

    name: str
    state: Literal["retrying", "paused", "fatal"]
    attempts: int
    queued_at: float
    next_retry: float
    since: str
    error: str = ""
    error_code: str = ""
    needs_attention: bool = False

    def as_status(self) -> dict[str, object]:
        """The runtime-status shape (P15-24): ``{state, needs_attention?, error_code?,
        retrying_since?}``."""
        out: dict[str, object] = {"state": self.state}
        if self.needs_attention:
            out["needs_attention"] = True
        if self.error_code:
            out["error_code"] = self.error_code
        if self.state != "fatal":
            out["retrying_since"] = self.since
        return out


type Connect = Callable[[str], Awaitable[ConnectOutcome]]
type OnChange = Callable[[str, PlatformHealth | None], None]


class ReconnectWatcher:
    """See the module notes."""

    def __init__(
        self,
        connect: Connect,
        *,
        clock: Clock,
        attention_after: Callable[[], float] = lambda: 1800.0,
        on_change: OnChange | None = None,
        poll_s: float = POLL_S,
    ) -> None:
        self.connect = connect
        self.clock = clock
        self.attention_after = attention_after
        self.on_change = on_change
        self.poll_s = poll_s
        self._queue: dict[str, PlatformHealth] = {}
        self._wake = asyncio.Event()
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._spares: set[asyncio.Task[None]] = set()
        self.gave_up = False
        self.respawns = 0

    # -- the queue ------------------------------------------------------------------------------

    def _set(self, health: PlatformHealth) -> None:
        self._queue[health.name] = health
        self._report(health.name, health)

    def _report(self, name: str, health: PlatformHealth | None) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(name, health)
        except Exception:
            log.warning("reconnect status update for %s failed", name, exc_info=True)

    def enqueue(
        self,
        name: str,
        *,
        error: str = "",
        error_code: str = "",
        retryable: bool = True,
        immediate: bool = False,
    ) -> PlatformHealth:
        """``name`` failed to connect. ``immediate``: a platform lost while it was running is
        tried again at once; one that failed at startup waits the first backoff. A platform
        already queued keeps its first failure time (failure is continuous) and its attempt
        count, and a watcher whose supervision gave up is started again."""
        now = self.clock.monotonic()
        current = self._queue.get(name)
        if not retryable:
            since = current.since if current is not None else self.clock.now().isoformat()
            queued = current.queued_at if current is not None else now
            health = PlatformHealth(name, "fatal", 0, queued, now, since, error, error_code)
            log.warning("%s cannot reconnect (%s); it stays down", name, error or error_code)
        elif current is not None and current.state != "fatal":
            health = replace(current, error=error or current.error, error_code=error_code)
        else:
            health = PlatformHealth(
                name,
                "retrying",
                0,
                now,
                now if immediate else now + backoff_s(1),
                self.clock.now().isoformat(),
                error,
                error_code,
            )
        self._set(health)
        self._wake.set()
        self._revive()
        return health

    def _revive(self) -> None:
        """Queued work needs a live loop: restart one that crashed past its supervision."""
        if not self._running or (self._task is not None and not self._task.done()):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        log.warning("the reconnect watcher was not running; starting it again")
        self.gave_up = False
        self._spawn(0)

    def forget(self, name: str) -> None:
        """``name`` is connected or removed: no more retries."""
        if self._queue.pop(name, None) is not None:
            self._report(name, None)

    def pause(self, name: str) -> bool:
        """Hold the retries of a queued platform (``/platform pause``)."""
        current = self._queue.get(name)
        if current is None or current.state != "retrying":
            return False
        self._set(replace(current, state="paused"))
        return True

    def resume(self, name: str) -> bool:
        """Retry a paused platform now (``/platform resume``)."""
        current = self._queue.get(name)
        if current is None or current.state != "paused":
            return False
        self._set(replace(current, state="retrying", next_retry=self.clock.monotonic()))
        self._wake.set()
        return True

    def get(self, name: str) -> PlatformHealth | None:
        return self._queue.get(name)

    def snapshot(self) -> dict[str, PlatformHealth]:
        return dict(self._queue)

    def queued(self) -> list[str]:
        """Platforms still being retried or held (not the fatal ones)."""
        return sorted(n for n, h in self._queue.items() if h.state != "fatal")

    # -- one pass -------------------------------------------------------------------------------

    def _threshold(self) -> float:
        try:
            return float(self.attention_after())
        except Exception:
            return 0.0

    def _flag(self, health: PlatformHealth, now: float) -> PlatformHealth:
        threshold = self._threshold()
        if health.needs_attention or threshold <= 0 or now - health.queued_at < threshold:
            return health
        log.warning(
            "%s has failed to connect for %.0f minutes (%d tries); it needs attention "
            "(retries go on)",
            health.name,
            (now - health.queued_at) / 60,
            health.attempts,
        )
        flagged = replace(health, needs_attention=True)
        self._set(flagged)
        return flagged

    async def tick(self) -> None:
        """Try every platform that is due now."""
        for name in list(self._queue):
            health = self._queue.get(name)
            if health is None or health.state != "retrying":
                continue
            health = self._flag(health, self.clock.monotonic())
            if self.clock.monotonic() >= health.next_retry:
                await self._attempt(health)

    async def _attempt(self, health: PlatformHealth) -> None:
        attempt = health.attempts + 1
        log.info("reconnecting %s (try %d)", health.name, attempt)
        try:
            outcome = await self.connect(health.name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome = ConnectOutcome.retryable(f"{type(exc).__name__}: {exc}")
        latest = self._queue.get(health.name)
        if latest is None:
            return  # forgotten while the connect ran
        health = latest  # paused or updated meanwhile: keep the newer entry's state
        if outcome.state == "connected":
            log.info("%s is connected again", health.name)
            self.forget(health.name)
            return
        now = self.clock.monotonic()
        if outcome.state == "fatal":
            self._set(
                replace(
                    health,
                    state="fatal",
                    attempts=attempt,
                    error=outcome.error,
                    error_code=outcome.error_code,
                )
            )
            log.warning("%s cannot reconnect (%s); it stays down", health.name, outcome.error)
            return
        wait = backoff_s(attempt + 1)
        self._set(
            replace(
                health,
                attempts=attempt,
                next_retry=now + wait,
                error=outcome.error,
                error_code=outcome.error_code,
            )
        )
        log.info("%s did not reconnect (%s); next try in %.0f s", health.name, outcome.error, wait)

    def _next_due(self) -> float | None:
        due = [h.next_retry for h in self._queue.values() if h.state == "retrying"]
        return min(due) if due else None

    async def run(self) -> None:
        """Pass after pass until stopped: sleep until the next platform is due (at most
        ``poll_s``), or until something is queued or resumed."""
        while True:
            self._wake.clear()
            await self.tick()
            due = self._next_due()
            now = self.clock.monotonic()
            delay = self.poll_s if due is None else min(self.poll_s, max(0.0, due - now))
            await self._sleep_or_wake(delay)

    async def _sleep_or_wake(self, delay: float) -> None:
        if self._wake.is_set():
            return
        sleeper = asyncio.ensure_future(self.clock.sleep(delay))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, waker):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    # -- supervision ----------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def start(self) -> None:
        """Run the loop as a supervised task on the current event loop."""
        if self._running:
            return
        self._running = True
        self.gave_up = False
        self._spawn(0)

    def _spawn(self, crashes: int) -> None:
        started = self.clock.monotonic()
        task = asyncio.get_running_loop().create_task(self.run(), name="reconnect-watcher")
        self._task = task
        task.add_done_callback(lambda done: self._ended(done, crashes, started))

    def _ended(self, task: asyncio.Task[None], crashes: int, started: float) -> None:
        if task.cancelled() or not self._running:
            return
        exc = task.exception()
        if exc is None:
            return
        log.error("the reconnect watcher crashed", exc_info=exc)
        if self.clock.monotonic() - started >= RESPAWN_HEALTHY_S:
            crashes = 0
        if crashes >= RESPAWN_LIMIT:
            log.error(
                "the reconnect watcher crashed %d times in a row; it is not restarted again",
                crashes,
            )
            self.gave_up = True
            return
        delay = min(RESPAWN_CAP_S, 2.0 ** (crashes + 1))

        async def later() -> None:
            await self.clock.sleep(delay)
            if self._running:
                self.respawns += 1
                self._spawn(crashes + 1)

        spare = asyncio.get_running_loop().create_task(later(), name="reconnect-watcher-respawn")
        self._spares.add(spare)
        spare.add_done_callback(self._spares.discard)

    async def stop(self) -> None:
        self._running = False
        tasks = [t for t in (self._task, *self._spares) if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None
