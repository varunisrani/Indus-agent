"""The real session service behind ``indus tui-bridge --stdio`` (P12-28).

A lean version of the classic CLI's host wiring (the bridge may not import ``indus.cli``): the
route resolved from the profile, the P02 store, the process terminal runtime shared by every
agent (``SessionRuntime.environments``), ``interactive`` run context for the approval pipeline,
one auxiliary client for titles and summaries, the ``execute_code`` kernel upkeep, and the
profile's MCP servers (one shared tool registry, ``service.mcp_host.McpUpkeep`` with the config
watcher; the bridge's ``reload.mcp`` reaches the same host). At bridge shutdown the terminal
runtime is shut down after the service.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from indus.core.agent import Agent, AgentOptions
from indus.core.persist import StateStore
from indus.core.turn_state import LoopOptions
from indus.foundation.scope import Scope
from indus.providers.auxiliary import AuxClient
from indus.providers.route import resolve
from indus.service import terminal_host
from indus.service.code_host import KernelUpkeep
from indus.service.mcp_host import McpUpkeep
from indus.service.session_service import SessionService
from indus.state import registry
from indus.state.registry import StoreHandle
from indus.tools.builtin import load_builtins, terminal
from indus.tools.registry import ToolRegistry
from indus.tui_bridge.bridge import Bridge

__all__ = ["PLATFORM", "SOURCE", "make_service", "store_source"]

log = logging.getLogger(__name__)

PLATFORM = "tui"
SOURCE = "tui"

_HANDLES: dict[str, StoreHandle] = {}


def _handle(scope: Scope) -> StoreHandle:
    key = str(scope.home)
    handle = _HANDLES.get(key)
    if handle is None:
        handle = registry.acquire(scope)
        _HANDLES[key] = handle
    return handle


def store_source(scope: Scope) -> Callable[[], StoreHandle | None]:
    """The profile's store handle, opened on first use (None when it cannot be opened)."""

    def get() -> StoreHandle | None:
        try:
            return _handle(scope)
        except Exception:
            log.warning("the session store could not be opened", exc_info=True)
            return None

    return get


def _seams(agent: Agent) -> None:
    agent.runtime.environments = terminal.runtime().environments
    agent.runtime.run_context = "interactive"


def make_service(bridge: Bridge) -> SessionService:
    """The profile's session service, with the bridge's request port in every agent."""
    scope = bridge.scope
    route = resolve(scope)
    handle = _handle(scope)
    store = StateStore(handle)
    cwd = Path.cwd()
    terminal_host.install(handle, terminal.runtime().processes)
    # One registry for the bridge (the built-in tools, as each agent had before): every agent
    # sees the MCP tools registered into it (P13-05).
    tool_registry = ToolRegistry(clock=scope.clock)
    load_builtins(tool_registry)
    aux = AuxClient(scope, route)
    options = AgentOptions(
        platform=PLATFORM,
        source=SOURCE,
        cwd=cwd,
        loop=LoopOptions.from_scope(scope, platform=PLATFORM),
        session_fields={"cwd": str(cwd), "model": route.model},
        store=store,
        registry=tool_registry,
    )
    service = SessionService(
        scope,
        route,
        options=options,
        store=store,
        owner=SOURCE,
        agent_factory=bridge.requests.agent_factory(route, store, seams=_seams),
        aux=aux,
        kernels=KernelUpkeep(scope, reaper=True),
        mcp=McpUpkeep(scope, tool_registry, aux=aux, watch=True),
    )

    async def shut_terminal() -> None:
        await terminal.runtime().shutdown()

    bridge.closers.append(shut_terminal)
    return service
