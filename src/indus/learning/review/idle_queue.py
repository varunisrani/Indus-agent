"""Deferring reviews bound for a local model server (P09-15, F9).

A review fork running on the same local server the user is chatting with competes with the next
turn, and the next turn cancels it. With ``learning.review.defer: auto`` such reviews wait here
instead (``/refine`` never waits):

- one slot per session: a newer snapshot replaces the waiting one but keeps the first enqueue time,
  so a busy session cannot push its review back forever;
- a waiting review is dispatched once the process has had no live turn for the settle window
  (60 s), or regardless once it has waited ``defer_max_age_s`` (1,800 s);
- a dispatched review that a live turn pre-empts goes back in (``requeue``);
- in memory and best effort, like the review itself.

Time is the injected clock; ``run`` polls every ``poll_s`` while anything waits and returns once
the queue is empty (the runner starts it again on the next enqueue).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from indus.foundation.clock import Clock

__all__ = ["DEFAULT_MAX_AGE_S", "POLL_S", "SETTLE_S", "Deferred", "IdleQueue"]

log = logging.getLogger(__name__)

SETTLE_S = 60.0
"""Quiet time (no live turn) before a waiting review runs."""
DEFAULT_MAX_AGE_S = 1800.0
"""A waiting review runs anyway after this long."""
POLL_S = 5.0


@dataclass(frozen=True)
class Deferred[T]:
    key: str
    item: T
    enqueued_at: float
    max_age_s: float = DEFAULT_MAX_AGE_S


class IdleQueue[T]:
    """Session-keyed waiting room for reviews (see the module notes)."""

    def __init__(
        self,
        clock: Clock,
        dispatch: Callable[[Deferred[T]], object],
        *,
        settle_s: float = SETTLE_S,
        poll_s: float = POLL_S,
    ) -> None:
        self.clock = clock
        self.dispatch = dispatch
        self.settle_s = settle_s
        self.poll_s = poll_s
        self._waiting: dict[str, Deferred[T]] = {}
        self._live_turns = 0
        self._quiet_since: float | None = clock.monotonic()

    # -- live turns -------------------------------------------------------------------------------

    def note_turn_started(self) -> None:
        self._live_turns += 1
        self._quiet_since = None

    def note_turn_finished(self) -> None:
        self._live_turns = max(0, self._live_turns - 1)
        if self._live_turns == 0:
            self._quiet_since = self.clock.monotonic()

    def quiet_for(self) -> float:
        """Seconds without a live turn (0 while one runs)."""
        if self._live_turns > 0 or self._quiet_since is None:
            return 0.0
        return max(0.0, self.clock.monotonic() - self._quiet_since)

    # -- the queue --------------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._waiting)

    def waiting(self, key: str) -> Deferred[T] | None:
        return self._waiting.get(key)

    def enqueue(self, key: str, item: T, *, max_age_s: float = DEFAULT_MAX_AGE_S) -> Deferred[T]:
        """Put ``item`` in ``key``'s slot (replacing a waiting one, keeping its enqueue time)."""
        held = self._waiting.get(key)
        since = held.enqueued_at if held is not None else self.clock.monotonic()
        entry = Deferred(key, item, since, max_age_s if max_age_s > 0 else DEFAULT_MAX_AGE_S)
        self._waiting[key] = entry
        log.info("review deferred until the model server is quiet (session=%s)", key[-12:])
        return entry

    def requeue(self, entry: Deferred[T]) -> None:
        """Put back a dispatched review a live turn pre-empted (a newer one waiting wins)."""
        if entry.key not in self._waiting:
            self._waiting[entry.key] = entry

    def drop(self, key: str) -> None:
        self._waiting.pop(key, None)

    def pop_ready(self) -> Deferred[T] | None:
        """The oldest review that aged out, else the oldest one once the process is quiet."""
        if not self._waiting:
            return None
        now = self.clock.monotonic()
        aged = [e for e in self._waiting.values() if now - e.enqueued_at >= e.max_age_s]
        if aged:
            chosen = min(aged, key=lambda e: e.enqueued_at)
        elif self.quiet_for() >= self.settle_s:
            chosen = min(self._waiting.values(), key=lambda e: e.enqueued_at)
        else:
            return None
        return self._waiting.pop(chosen.key)

    def pump(self) -> int:
        """Dispatch every review that may run now; how many."""
        count = 0
        while (entry := self.pop_ready()) is not None:
            count += 1
            try:
                self.dispatch(entry)
            except Exception:
                log.warning("a deferred review could not be dispatched", exc_info=True)
        return count

    async def run(self) -> None:
        """Poll on the clock while anything waits; return once the queue is empty."""
        while self._waiting:
            await self.clock.sleep(self.poll_s)
            self.pump()
