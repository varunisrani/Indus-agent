"""What happens around a subagent's life: start and stop hooks, hand-off grants, cleanup (P10-06).

``ChildResources(...)`` is made when a child starts:

- ``open()`` registers the child's hand-off grant with the process registry (``process_manage
  action=handoff`` then works for the child's session only: at most ``MAX_HANDOFFS`` jobs, each
  with a purpose sentence; the parent never holds a grant) and emits ``subagent_start``;
- ``close()`` kills every background job the child still owns — handed-off ones belong to the
  parent by then — and returns them as ``orphaned_processes`` entries; revokes the grant; calls
  every registered releaser with the child's ids; and emits ``subagent_stop`` on the caller's
  task.

Releasers and the lifecycle hook bus are seams: later phases register theirs
(``register_releaser``, ``set_lifecycle_hooks``); until P13 the bus does nothing. Two releasers
are built in: ``code-kernels`` disposes the child's ``execute_code`` kernels, local and remote
(P10-10/11), and ``browser-sessions`` closes the child's browser (P10-12: its tools run under the
child's session id as task id), force-reaping it when the close fails. Vision embed counters
(P10-15) live in a bounded table and need no releaser. A child's kernels are
kept under the owner ``kernel.child_owner(parent, child)`` (``<parent>::child::<child>``), which
``build_child`` gives the child's runtime as ``code_kernel_owner``; the pool pins them outside the
top-level LRU while they live (R02.32). A releaser that fails is logged; it never stops the rest
of the cleanup.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from indus.backends.processes import MAX_HANDOFFS, ProcessRegistry
from indus.foundation.scope import Scope
from indus.safety.redaction import redact_output
from indus.tools.builtin import browser
from indus.tools.builtin.code import kernel as code_kernel

__all__ = [
    "NO_LIFECYCLE_HOOKS",
    "ChildResources",
    "LifecycleHooks",
    "BROWSER_RELEASER",
    "KERNEL_RELEASER",
    "Releaser",
    "lifecycle_hooks",
    "register_releaser",
    "set_lifecycle_hooks",
]

log = logging.getLogger(__name__)

ORPHAN_SOURCE = "subagent ended"
KERNEL_RELEASER = "code-kernels"
BROWSER_RELEASER = "browser-sessions"

type Releaser = Callable[[str, str], object]
"""``releaser(parent_session_id, child_session_id)``: free what the child held (may be async)."""


class LifecycleHooks(Protocol):
    """Where ``subagent_start`` / ``subagent_stop`` go (the plugin hook bus from P13)."""

    def emit(self, event: str, payload: Mapping[str, Any]) -> object: ...


class _NoHooks:
    def emit(self, event: str, payload: Mapping[str, Any]) -> object:
        return None


NO_LIFECYCLE_HOOKS: LifecycleHooks = _NoHooks()
_hooks: list[LifecycleHooks] = [NO_LIFECYCLE_HOOKS]
_releasers: dict[str, Releaser] = {}


def set_lifecycle_hooks(hooks: LifecycleHooks | None) -> None:
    """Install the process's lifecycle hook bus (None restores the no-op one)."""
    _hooks[0] = hooks if hooks is not None else NO_LIFECYCLE_HOOKS


def lifecycle_hooks() -> LifecycleHooks:
    return _hooks[0]


def register_releaser(name: str, releaser: Releaser | None) -> None:
    """Add (or with None remove) a named cleanup run for every child that ends."""
    if releaser is None:
        _releasers.pop(name, None)
    else:
        _releasers[name] = releaser


async def _emit(event: str, payload: Mapping[str, Any]) -> None:
    try:
        outcome = lifecycle_hooks().emit(event, payload)
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        log.warning("lifecycle hook %s failed", event, exc_info=True)


@dataclass
class ChildResources:
    """One child's grant, jobs and releasers (see the module notes)."""

    scope: Scope
    parent_session_id: str
    child_session_id: str
    subagent_id: str
    goal: str = ""
    processes: ProcessRegistry | None = None
    orphans: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    opened: bool = False
    closed: bool = False

    @property
    def home(self) -> str:
        return str(self.scope.home)

    def _payload(self, **extra: Any) -> dict[str, Any]:
        return {
            "parent_session_id": self.parent_session_id,
            "child_session_id": self.child_session_id,
            "subagent_id": self.subagent_id,
            "goal": self.goal,
            **extra,
        }

    async def open(self) -> None:
        self.opened = True
        if self.processes is not None:
            self.processes.grant_handoff(
                self.home, self.child_session_id, self.parent_session_id, limit=MAX_HANDOFFS
            )
        await _emit("subagent_start", self._payload())

    async def close(self, *, status: str = "") -> list[dict[str, Any]]:
        """Clean up once; returns the jobs that were killed (the orphans)."""
        if self.closed:
            return self.orphans
        self.closed = True
        registry = self.processes
        if registry is not None:
            registry.revoke_handoff(self.home, self.child_session_id)
            for record in registry.list(self.home, self.child_session_id):
                if record.exited:
                    continue
                try:
                    await registry.kill(record, source=ORPHAN_SOURCE)
                except Exception:
                    log.warning(
                        "could not stop %s of a finished subagent", record.id, exc_info=True
                    )
                command = redact_output(record.command, scope=self.scope)[:200]
                self.orphans.append({"id": record.id, "command": command, "action": "killed"})
        for name, releaser in list(_releasers.items()):
            try:
                outcome = releaser(self.parent_session_id, self.child_session_id)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                log.warning("subagent releaser %s failed", name, exc_info=True)
        if self.opened:
            await _emit("subagent_stop", self._payload(status=status))
        return self.orphans


async def _release_kernels(parent_session_id: str, child_session_id: str) -> None:
    await code_kernel.dispose_child(child_session_id)


async def _release_browser(parent_session_id: str, child_session_id: str) -> None:
    await browser.DEFAULT_TOOLS.manager.end(child_session_id)


register_releaser(KERNEL_RELEASER, _release_kernels)
register_releaser(BROWSER_RELEASER, _release_browser)
