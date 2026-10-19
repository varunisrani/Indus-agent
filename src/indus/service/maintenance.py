"""The service's maintenance timer: background chores on a slow beat (P09-18).

``MaintenanceTimer(clock)`` runs each registered task once ``first_delay_s`` (90 s) after
``run()`` starts and then every ``interval_s`` (an hour). Tasks are plain callables; they run in
a worker thread one after another so a slow chore never blocks the event loop, and a task that
raises is logged and does not stop the others or the timer. P09 registers the curator check
(``indus.learning.curator.lifecycle.curator_check``); later phases add theirs.

``learning_timer(scope, aux=..., idle_s=...)`` is that timer for one profile: the curator check
with its consolidation pass on the service's auxiliary client (``curator`` task) when one is
given. ``profile_idle_s(store)`` is the idle measure hosts pass: seconds since the newest stored
message of the profile. ``SessionService(maintenance=...)`` runs the timer while it is open.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock

if TYPE_CHECKING:
    from indus.foundation.scope import Scope
    from indus.state.registry import StoreHandle

__all__ = [
    "FIRST_DELAY_S",
    "INTERVAL_S",
    "MaintenanceTimer",
    "learning_timer",
    "profile_idle_s",
]

log = logging.getLogger(__name__)

FIRST_DELAY_S = 90.0
INTERVAL_S = 3600.0


@dataclass(frozen=True)
class _Task:
    name: str
    fn: Callable[[], object]


class MaintenanceTimer:
    def __init__(
        self,
        clock: Clock,
        *,
        first_delay_s: float = FIRST_DELAY_S,
        interval_s: float = INTERVAL_S,
        in_thread: bool = True,
    ) -> None:
        self.clock = clock
        self.first_delay_s = first_delay_s
        self.interval_s = interval_s
        self.in_thread = in_thread
        self._tasks: list[_Task] = []
        self._stopped = asyncio.Event()
        self.ticks = 0

    def register(self, name: str, fn: Callable[[], object]) -> Callable[[], None]:
        """Add a chore; the returned callable removes it."""
        task = _Task(name, fn)
        self._tasks.append(task)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._tasks.remove(task)

        return remove

    @property
    def names(self) -> list[str]:
        return [t.name for t in self._tasks]

    async def tick(self) -> None:
        """Run every chore once, in registration order."""
        self.ticks += 1
        for task in list(self._tasks):
            try:
                if self.in_thread:
                    await run_blocking(task.fn, label=f"maintenance {task.name}")
                else:
                    task.fn()
            except Exception:
                log.warning("maintenance task %s failed", task.name, exc_info=True)

    async def run(self) -> None:
        """Tick after the first delay, then every interval, until ``stop()``."""
        delay = self.first_delay_s
        while not self._stopped.is_set():
            await self.clock.sleep(delay)
            if self._stopped.is_set():
                break
            await self.tick()
            delay = self.interval_s

    def stop(self) -> None:
        self._stopped.set()


def profile_idle_s(store: StoreHandle | None, clock: Clock) -> float | None:
    """Seconds since the newest stored message of the profile (None: nothing stored or the store
    could not be read, so idleness is not known)."""
    if store is None:
        return None
    try:
        with store.read() as conn:
            row = conn.execute("SELECT MAX(timestamp) FROM messages").fetchone()
    except Exception:
        log.debug("idle time not read from the store", exc_info=True)
        return None
    newest = row[0] if row is not None else None
    if not isinstance(newest, int | float):
        return None
    return max(0.0, clock.now().timestamp() - float(newest))


def learning_timer(
    scope: Scope,
    *,
    aux: Any = None,
    idle_s: Callable[[], float | None] | None = None,
    gateway_running: Callable[[], bool] | None = None,
    clock: Clock | None = None,
) -> MaintenanceTimer:
    """The maintenance timer of one profile with the curator check registered (P09-18)."""
    from indus.learning.curator.consolidate import AuxStep
    from indus.learning.curator.lifecycle import curator_check

    timer = MaintenanceTimer(clock if clock is not None else scope.clock)

    def step_factory(_scope: Scope) -> AuxStep | None:
        return AuxStep(aux) if aux is not None else None

    timer.register(
        "curator",
        curator_check(
            scope, idle_s=idle_s, gateway_running=gateway_running, step_factory=step_factory
        ),
    )
    return timer
