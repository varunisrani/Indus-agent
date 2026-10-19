"""Subagents: ``delegate_task`` run by the agent loop (P10-01..07).

An agent that offers ``delegate_task`` gets one ``Delegation`` (``attach_delegation``, called
while the agent opens); its ``execute`` is the inline executor of the P04-09 table. The host
connects background results with ``set_delivery`` (``SessionService`` does, so they enter the
conversation between turns) and stops detached children with ``stop_detached`` on its stop
surfaces. Modules: ``child`` (building one child), ``batch`` (running a batch), ``output_schema``,
``background``, ``teardown`` (hand-off, cleanup, lifecycle hooks), ``worktree``.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING

from indus.core.subagents.delegation import Delegation

if TYPE_CHECKING:
    from indus.core.agent import Agent

__all__ = [
    "Delegation",
    "attach_delegation",
    "delegation_of",
    "set_delivery",
    "stop_detached",
]

_by_agent: weakref.WeakKeyDictionary[Agent, Delegation] = weakref.WeakKeyDictionary()


def attach_delegation(agent: Agent) -> Delegation:
    """The agent's delegation, made on first use."""
    found = _by_agent.get(agent)
    if found is None:
        found = _by_agent[agent] = Delegation(agent)
    return found


def delegation_of(agent: Agent) -> Delegation | None:
    return _by_agent.get(agent)


def set_delivery(agent: Agent, deliver: Callable[[str], object] | None) -> bool:
    """Where background results of ``agent`` go (between turns); False when it cannot delegate."""
    delegation = _by_agent.get(agent)
    if delegation is None:
        return False
    delegation.deliver = deliver
    return True


async def stop_detached(agent: Agent) -> int:
    """Stop every background child of ``agent`` (``/stop``, hard interrupt, session end)."""
    delegation = _by_agent.get(agent)
    return await delegation.stop_all() if delegation is not None else 0
