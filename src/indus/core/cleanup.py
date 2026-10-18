"""Cleanup registry: what must be given back when an agent's task ends (P04-20).

Tool backends hold resources per task id — a terminal sandbox, a browser, a background process
table. A backend registers one cleanup function with ``TASK_CLEANUPS`` when it is loaded;
``Agent.close()`` then runs every registered function once for its task id. Each function is
guarded on its own: one that raises is logged and the rest still run. A function may be plain
or a coroutine function.
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Awaitable, Callable

__all__ = ["TASK_CLEANUPS", "CleanupRegistry", "TaskCleanup"]

log = logging.getLogger(__name__)

type TaskCleanup = Callable[[str], object | Awaitable[object]]


class CleanupRegistry:
    """Named cleanup functions, each called with a task id."""

    def __init__(self) -> None:
        self._entries: dict[str, TaskCleanup] = {}
        self._guard = threading.Lock()

    def register(self, name: str, cleanup: TaskCleanup) -> Callable[[], None]:
        """Add ``cleanup`` under ``name`` (a second registration under one name replaces the
        first, so a reloaded backend does not run twice). Returns a callable that removes it."""
        if not name:
            raise ValueError("a cleanup needs a name")
        with self._guard:
            self._entries[name] = cleanup

        def remove() -> None:
            with self._guard:
                if self._entries.get(name) is cleanup:
                    del self._entries[name]

        return remove

    def names(self) -> list[str]:
        with self._guard:
            return list(self._entries)

    async def run(self, task_id: str) -> list[str]:
        """Run every cleanup for ``task_id`` in registration order; returns the failures."""
        with self._guard:
            entries = list(self._entries.items())
        failures: list[str] = []
        for name, cleanup in entries:
            try:
                outcome = cleanup(task_id)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                log.warning("cleanup %s failed for task %s", name, task_id, exc_info=True)
        return failures


TASK_CLEANUPS = CleanupRegistry()
"""The process registry tool backends register into (P05 terminal, P10 processes, …)."""
