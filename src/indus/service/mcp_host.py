# pyright: strict
"""The host side of MCP servers (P13-05..12 integration).

One ``McpUpkeep`` per session service (one profile). The session service calls it at three points:

- ``start()`` before it builds the first agent: when the profile configures any MCP server, the
  ``McpHost`` is built (the OSV-guarded opener, the profile's OAuth credential stamp for auth
  parking, and per-server connect extras: OAuth handler, sampling through the service's auxiliary
  client, elicitation through the session whose call is in flight), registered for the profile
  (``discovery.register_host``, which the TUI bridge's ``reload.mcp`` finds), and discovery runs
  once, in the background. A new session waits for it at most ``discovery_wait`` seconds
  (``DISCOVERY_WAIT_S``, 5 s, for an interactive run; ``SINGLE_QUERY_DISCOVERY_WAIT_S``, 15 s, for
  one-shot runs and the daemon, which also serves them): a server that connects later registers its
  tools then (new sessions get them at once), so a slow or dead server never holds a chat past its
  start deadline. An interactive host also starts the config watcher. A profile without MCP
  servers never imports the MCP stack (about 0.7 s), so it has no watcher either: servers added
  later arrive with ``/reload-mcp``.
- ``selection(...)`` / ``attach(agent)`` for every agent: a platform bundle's selection gains the
  umbrella toolset ``mcp`` (every server toolset of the profile), so the tools reach the agent's
  first tool array; the agent is told when the tool surface changes (``ToolSurfaceChanged`` from a
  reload, the watcher or ``list_changed``) and rebuilds its stored tool array at its next
  compaction; a new session gets the new tools at once. The provider's prompt cache cannot be
  dropped from here: the rebuilt array and prompt start a new prefix, so the next request after the
  rebuild is an expected miss.
- ``shutdown()`` at service close: watcher stopped, servers closed, host unregistered.

``reload(ask=...)`` is ``/reload-mcp`` for a host that runs one (it builds the host first when the
profile had no servers at start). Slash handlers on another thread reach the service loop through
``call``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from indus.foundation.scope import Scope
from indus.tools.registry import ToolRegistry, scope_key

if TYPE_CHECKING:
    from indus.mcp.discovery import DiscoveryReport, McpHost
    from indus.mcp.reload import ConfigWatcher, ReloadChoice

__all__ = [
    "DISCOVERY_WAIT_S",
    "SINGLE_QUERY_DISCOVERY_WAIT_S",
    "McpUpkeep",
    "ServerLive",
    "UpkeepAgent",
    "configured",
    "upkeep_for",
]

log = logging.getLogger(__name__)

MCP_TOOLSET = "mcp"  # discovery.MCP_TOOLSET, without importing the MCP stack

DISCOVERY_WAIT_S = 5.0
"""How long an interactive session's start waits for discovery still in flight."""
SINGLE_QUERY_DISCOVERY_WAIT_S = 15.0
"""The same for one-shot runs: one turn has no later chance to see a server that came late."""


class UpkeepAgent(Protocol):
    """What the upkeep needs of an agent (``core.agent.Agent``)."""

    toolset_selection: list[str] | None

    def tools_changed(self, selection: Sequence[str] | None = None) -> None: ...

    def on_close(self, closer: Any, *, name: str = "") -> None: ...


@dataclass(frozen=True)
class ServerLive:
    """One running server as ``/mcp`` shows it."""

    state: str
    tools: int
    detail: str = ""
    error: str | None = None


def configured(scope: Scope) -> bool:
    """Does the profile name any MCP server? (No MCP import.)"""
    config = scope.config
    if config is None:
        return False
    try:
        raw = config.get("mcp_servers")
    except Exception:
        return False
    return isinstance(raw, Mapping) and len(cast(Mapping[object, object], raw)) > 0


_UPKEEP: dict[str, McpUpkeep] = {}


def upkeep_for(scope: Scope) -> McpUpkeep | None:
    """The running upkeep of ``scope``'s profile in this process."""
    return _UPKEEP.get(scope_key(scope))


class McpUpkeep:
    """One session service's MCP servers (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        registry: ToolRegistry,
        *,
        aux: object = None,
        watch: bool = False,
        opener: Any = None,
        discovery_wait: float = DISCOVERY_WAIT_S,
    ) -> None:
        self.scope = scope
        self.discovery_wait = discovery_wait
        self.registry = registry
        self.aux = aux
        self.watch = watch
        self.opener = opener
        self.host: McpHost | None = None
        self.report: DiscoveryReport | None = None
        self.watcher: ConfigWatcher | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._agents: list[UpkeepAgent] = []
        self._started = False
        self._closed = False
        self._lock: asyncio.Lock | None = None
        self._discovery: asyncio.Task[None] | None = None

    # -- lifecycle ------------------------------------------------------------------------------

    async def start(self) -> DiscoveryReport | None:
        """Once per service: build the host and start discovery when servers are configured;
        every call waits at most ``discovery_wait`` for discovery still running (the report is
        None until it ends). Never raises (a broken MCP setup must not stop sessions)."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not self._started and not self._closed:
                self._started = True
                self.loop = asyncio.get_running_loop()
                _UPKEEP[scope_key(self.scope)] = self
                if configured(self.scope):
                    try:
                        host = self._open()
                    except Exception:
                        log.warning("MCP servers could not be started", exc_info=True)
                    else:
                        self._discovery = asyncio.create_task(
                            self._discover(host), name="mcp discovery"
                        )
        task = self._discovery
        if task is not None and not task.done():
            done, _ = await asyncio.wait({task}, timeout=self.discovery_wait)
            if not done:
                log.warning(
                    "MCP servers are still connecting after %g s; this session starts without "
                    "the ones not up yet (they join new sessions when they connect)",
                    self.discovery_wait,
                )
        return self.report

    async def _discover(self, host: McpHost) -> None:
        try:
            self.report = await host.discover()
            self._start_watcher()
        except Exception:
            log.warning("MCP servers could not be started", exc_info=True)

    def _open(self) -> McpHost:
        from indus.mcp import discovery, extras, oauth

        host = discovery.McpHost(
            self.scope,
            self.registry,
            opener=self.opener if self.opener is not None else extras.opener(),
            credential_stamp=oauth.credential_stamp(self.scope),
            extras=self._extras,
        )
        host.listeners.append(self._changed)
        discovery.register_host(host)
        self.host = host
        return host

    def _extras(self, name: str, server: Any, base: Any) -> Any:
        from indus.mcp import extras, sampling

        assert self.host is not None
        backend = sampling.aux_backend(self.aux) if self.aux is not None else None
        return extras.extras_for(name, server, self.host.scope, base=base, sampling=backend)

    def _start_watcher(self) -> None:
        if not self.watch or self.watcher is not None or self.host is None:
            return
        from indus.mcp.reload import ConfigWatcher

        self.watcher = ConfigWatcher(self.host)
        self.watcher.start()

    async def shutdown(self) -> None:
        """Service close: stop the watcher, close the servers, unregister the host."""
        self._closed = True
        discovery, self._discovery = self._discovery, None
        if discovery is not None and not discovery.done():
            discovery.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await discovery
        watcher, self.watcher = self.watcher, None
        if watcher is not None:
            try:
                await watcher.close()
            except Exception:
                log.warning("the MCP config watcher did not stop cleanly", exc_info=True)
        host, self.host = self.host, None
        if host is not None:
            from indus.mcp import discovery

            try:
                await host.close()
            except Exception:
                log.warning("the MCP servers did not close cleanly", exc_info=True)
            if discovery.host_for(self.scope) is host:
                discovery.drop_host(self.scope)
        key = scope_key(self.scope)
        if _UPKEEP.get(key) is self:
            del _UPKEEP[key]
        self._agents.clear()

    # -- agents ---------------------------------------------------------------------------------

    def selection(self, selection: Sequence[str] | None) -> list[str] | None:
        """``selection`` with the umbrella ``mcp`` toolset when the profile has MCP tools.
        ``None`` (every toolset) already includes them."""
        if selection is None:
            return None
        chosen = list(selection)
        if self.host is None or MCP_TOOLSET in chosen:
            return chosen
        if MCP_TOOLSET not in self.registry.toolset_names(self.scope):
            return chosen
        return [*chosen, MCP_TOOLSET]

    def attach(self, agent: UpkeepAgent) -> None:
        """Tell ``agent`` about tool-surface changes until it closes."""
        self._agents.append(agent)

        def detach() -> None:
            if agent in self._agents:
                self._agents.remove(agent)

        agent.on_close(detach, name="mcp tool listener")

    def _changed(self, event: object) -> None:
        added = getattr(event, "added", ())
        removed = getattr(event, "removed", ())
        log.info(
            "MCP tools changed (%s; added %d, removed %d): sessions rebuild their tool array at "
            "their next compaction; new sessions get it at once",
            getattr(event, "reason", "change"),
            len(added),
            len(removed),
        )
        for agent in list(self._agents):
            try:
                agent.tools_changed(self.selection(agent.toolset_selection))
            except Exception:
                log.warning("an agent could not take the MCP tool change", exc_info=True)

    # -- commands -------------------------------------------------------------------------------

    async def reload(self, ask: Callable[[str], Awaitable[ReloadChoice]] | None = None) -> Any:
        """``/reload-mcp`` (``reload.ReloadOutcome``)."""
        from indus.mcp import reload

        if self._closed:
            return reload.ReloadOutcome(False, "The session service is closing.")
        host = self.host
        if host is None:
            if not configured(reload.fresh_scope(self.scope)):
                return reload.ReloadOutcome(False, "No MCP servers are configured.")
            host = self._open()
        outcome = await reload.reload_mcp(host, ask=ask)
        self._start_watcher()
        return outcome

    async def live(self) -> dict[str, ServerLive]:
        """The running servers' states for ``/mcp``."""
        host = self.host
        if host is None:
            return {}
        return {r.name: ServerLive(r.state, r.tools, r.detail, r.error) for r in host.status()}

    def call[T](self, coro: Coroutine[Any, Any, T], *, timeout: float | None = 60.0) -> T:
        """Run ``coro`` on the service loop from another thread (slash handlers) and wait."""
        loop = self.loop
        if loop is None or loop.is_closed():
            coro.close()
            raise RuntimeError("the MCP host is not running")
        future: Future[T] = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout)
