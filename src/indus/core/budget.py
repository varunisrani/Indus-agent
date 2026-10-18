"""The iteration budget: how many model calls a turn may still make.

``consume`` takes one unit when an iteration starts; ``refund`` gives it back when the iteration
produced no answer (a redirect or a route change restarted it). A cap of ``0`` means unlimited.
One budget object may be shared by a parent and its subagents (delegation, later), so it is safe to
use from several tasks; all calls happen on one event loop, so no lock is needed.
"""

from __future__ import annotations

import math

__all__ = ["DEFAULT_MAX_ITERATIONS", "IterationBudget", "max_iterations_from"]

DEFAULT_MAX_ITERATIONS = 150  # D-16.62; config ``agent.max_turns``


def max_iterations_from(value: object) -> int:
    """``agent.max_turns`` as a cap: a non-negative int (``0`` = unlimited), else the default."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_MAX_ITERATIONS
    return value


class IterationBudget:
    def __init__(self, max_total: int = DEFAULT_MAX_ITERATIONS) -> None:
        self.max_total = max(0, max_total)
        self._used = 0

    @property
    def unlimited(self) -> bool:
        return self.max_total == 0

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> float:
        """Units left; ``inf`` when unlimited."""
        if self.unlimited:
            return math.inf
        return max(0, self.max_total - self._used)

    def consume(self) -> bool:
        """Take one unit; False (nothing taken) when none is left."""
        if not self.unlimited and self._used >= self.max_total:
            return False
        self._used += 1
        return True

    def refund(self) -> None:
        if self._used > 0:
            self._used -= 1
