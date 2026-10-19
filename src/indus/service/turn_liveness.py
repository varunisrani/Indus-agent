"""The turn liveness watchdog for hosted turns (P17-04, A63).

A turn the daemon runs (an admission's claim, from start to settle) is stopped when it shows no
activity for ``agent.turn_liveness_s`` seconds (600 by default, ``<= 0`` turns it off), polled
every 15 s. The settings come from the P08-16 reader (``core.liveness.LivenessSettings``), which
keeps watching the agent loop itself; this watchdog covers the hosted turn as a whole and also
stops its lease renewal, so a wedge that ignores the interrupt is freed by the lease expiry.

**Activity** is what the host feeds an ``ActivityMeter``: ``touch`` for model deltas, tool
heartbeats and progress, and ``observe_event`` for any turn event it relays. A person being asked
something (approval, clarify) is not idleness: while the ``waiting`` probe says so, the idle clock
does not run, however long the wait. Lease renewal is never activity (a wedged turn would renew it
for ever).

**The abort** is decided on an observation — the generation and the stamp of the last activity —
and committed only when both still hold when they are read again under the meter's lock. A touch
that lands in between, the turn ending, or a newer generation vetoes it, so a turn that resumed is
never cancelled. The ``abort`` callback runs under that lock and must not block: it interrupts the
turn through the host's single exit funnel with reason ``STALLED`` (the turn then ends
``interrupted_by_system`` with failure ``stalled``). Returning False (or raising) declines; the
watchdog keeps polling. After a committed abort ``deactivate`` (stop the lease renewal) runs once.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from indus.core.liveness import LivenessSettings
from indus.foundation.clock import Clock
from indus.protocol.events import EventBase, TurnDone, TurnFailure

__all__ = [
    "STALLED",
    "Activity",
    "ActivityMeter",
    "LivenessSettings",
    "LivenessWatchdog",
    "stall_failure",
    "stall_message",
]

log = logging.getLogger(__name__)

STALLED: Final = "stalled"


def stall_failure() -> TurnFailure:
    """The failure a stalled turn reports beside ``interrupted_by_system``."""
    return TurnFailure(reason=STALLED, retryable=True)


def stall_message(idle_s: float) -> str:
    return (
        f"This reply was stopped: it showed no progress for {round(idle_s)} s. The session is "
        "free again; send your message once more to retry."
    )


@dataclass(frozen=True)
class Activity:
    """One observation: the turn's generation, its last activity stamp, how long it has idled."""

    generation: int
    stamp: float
    idle_s: float


class ActivityMeter:
    """The activity clock of one session's running turn; every method is thread-safe."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.generation: int | None = None
        self.stamp = 0.0
        self.note = ""

    def begin(self, generation: int) -> None:
        """A turn of ``generation`` starts: its idle clock starts now."""
        with self.lock:
            self.generation = generation
            self.stamp = self.clock.monotonic()
            self.note = "turn started"

    def end(self, generation: int) -> None:
        """The turn of ``generation`` ended (a newer one's ``begin`` already replaced it)."""
        with self.lock:
            if self.generation == generation:
                self.generation = None

    def touch(self, note: str = "") -> None:
        """Mark activity now (any thread)."""
        with self.lock:
            self.stamp = self.clock.monotonic()
            if note:
                self.note = note

    def observe_event(self, event: EventBase) -> None:
        """Every relayed turn event is activity; ``turn.done`` is not (it ends the turn)."""
        if isinstance(event, TurnDone):
            return
        self.touch(getattr(event, "type", "event"))

    def observe(self) -> Activity | None:
        """The current observation, or None when no turn is running."""
        with self.lock:
            if self.generation is None:
                return None
            generation, stamp = self.generation, self.stamp
        return Activity(generation, stamp, max(0.0, self.clock.monotonic() - stamp))

    def still(self, seen: Activity) -> bool:
        """Caller holds ``lock``: does ``seen`` still describe the running turn?"""
        return self.generation == seen.generation and self.stamp == seen.stamp


class LivenessWatchdog:
    """Polls one ``ActivityMeter`` (see the module notes)."""

    def __init__(
        self,
        meter: ActivityMeter,
        settings: LivenessSettings,
        *,
        abort: Callable[[Activity], bool],
        clock: Clock | None = None,
        waiting: Callable[[], bool] | None = None,
        deactivate: Callable[[], object] | None = None,
        label: str = "",
    ) -> None:
        self.meter = meter
        self.settings = settings
        self.clock: Clock = clock if clock is not None else meter.clock
        self._abort = abort
        self._waiting = waiting
        self._deactivate = deactivate
        self.label = label
        self.fired: Activity | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.timeout_s is not None

    def _person_waiting(self) -> bool:
        if self._waiting is None:
            return False
        try:
            return bool(self._waiting())
        except Exception:
            log.debug("liveness waiting probe failed", exc_info=True)
            return False

    def tick(self) -> bool:
        """One poll; False when the watchdog is finished (turn over, aborted, or off)."""
        timeout = self.settings.timeout_s
        if timeout is None or self.fired is not None:
            return False
        if self._person_waiting():
            self.meter.touch("waiting for a person")
            return self.meter.observe() is not None
        seen = self.meter.observe()
        if seen is None:
            return False
        if seen.idle_s < timeout:
            return True
        log.warning(
            "turn %s of %s showed no activity for %.0f s (last: %s); stopping it",
            seen.generation,
            self.label or "a session",
            seen.idle_s,
            self.meter.note or "unknown",
        )
        return not self.commit(seen)  # a declined abort keeps watching

    def commit(self, seen: Activity) -> bool:
        """Abort the turn if ``seen`` still holds, re-read under the meter's lock."""
        with self.meter.lock:
            if not self.meter.still(seen):
                return False
            try:
                done = bool(self._abort(seen))
            except Exception:
                log.warning("the liveness abort failed; the turn keeps running", exc_info=True)
                done = False
        if not done:
            return False
        self.fired = seen
        log.error(
            "turn %s of %s stopped by the liveness watchdog after %.0f s without activity",
            seen.generation,
            self.label or "a session",
            seen.idle_s,
        )
        if self._deactivate is not None:
            try:
                self._deactivate()
            except Exception:
                log.warning("stopping the lease renewal after a stall failed", exc_info=True)
        return True

    async def run(self) -> None:
        while True:
            await self.clock.sleep(self.settings.poll_s)
            if not self.tick():
                return

    def start(self) -> asyncio.Task[None] | None:
        """Run on the loop; None when the watchdog is off."""
        if not self.enabled:
            return None
        self._task = asyncio.get_running_loop().create_task(self.run(), name="turn liveness")
        return self._task

    def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
