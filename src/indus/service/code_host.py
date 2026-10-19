# pyright: strict
"""The host side of ``execute_code`` kernels (P10-10/11 integration).

Kernels live in two process-wide pools (``tools.builtin.code.kernel.POOL`` on this machine,
``remote.POOL`` on Docker/SSH backends). The tool only takes and gives back; the host decides
when they end:

- a session that is closed (``SessionService.close``: ``/new``, ``/clear``, switching sessions,
  service shutdown) loses its kernels at once — ``end_session(*ids)`` disposes every kernel kept
  under one of the session's ids (the current id and any id it continued from after
  compression);
- a subagent's kernels end with the subagent (``core.subagents.teardown``'s ``code-kernels``
  releaser), not here;
- ``KernelUpkeep`` is what a host process owns: ``start()`` runs the idle reaper
  (``kernel.run_reaper``, window = ``code_execution.kernel_idle_timeout``) when the host is
  long-lived, and ``shutdown()`` at host exit stops the reaper and closes both pools.
  ``kernel``'s ``atexit`` hook still kills whatever is left if a host never gets that far.

Browser sessions (P10-12) follow the same ownership: a closed session's browser is closed with
its kernels (the browser tools run under the session id as task id), and ``shutdown()`` closes
every browser of the process (``browser.DEFAULT_TOOLS.manager.shutdown()``, the sessions of the
host's loop and the Playwright driver). A subagent's browser ends with the subagent
(``browser-sessions`` releaser).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from indus.foundation.scope import Scope
from indus.tools.builtin import browser
from indus.tools.builtin.code import kernel, remote
from indus.tools.builtin.code.settings import CodeSettings

__all__ = ["KernelUpkeep", "end_session"]

log = logging.getLogger(__name__)


async def end_session(*session_ids: str) -> int:
    """Dispose the kernels and close the browser of a closed session (all of its ids); how many
    kernels were closed."""
    closed = 0
    for session_id in dict.fromkeys(i for i in session_ids if i):
        try:
            closed += await kernel.dispose_session(session_id)
        except Exception:
            log.warning("could not close the code kernels of %s", session_id, exc_info=True)
        await browser.DEFAULT_TOOLS.manager.end(session_id)
    return closed


@dataclass
class KernelUpkeep:
    """One host's share of the kernel lifecycle (see the module notes)."""

    scope: Scope
    reaper: bool = True
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def idle_s(self) -> float:
        return CodeSettings.from_scope(self.scope).kernel_idle_s

    def start(self) -> None:
        """Start the idle reaper on the running loop (long-lived hosts; once)."""
        if not self.reaper or self._task is not None or self._closed:
            return
        self._task = asyncio.get_running_loop().create_task(
            kernel.run_reaper(self.scope.clock, self.idle_s), name="code kernel reaper"
        )

    @property
    def reaping(self) -> bool:
        return self._task is not None and not self._task.done()

    async def shutdown(self) -> None:
        """Host exit: stop the reaper, close every kernel and every browser of this process."""
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for pool in (kernel.POOL, remote.POOL):
            try:
                await pool.shutdown()
            except Exception:
                log.warning("could not close the %s code kernels", pool.label, exc_info=True)
        try:
            await browser.DEFAULT_TOOLS.manager.shutdown()
        except Exception:
            log.warning("could not close the browser sessions", exc_info=True)
