# pyright: strict
"""The host side of plugins and shell hooks (P13-13..23 integration).

One ``PluginUpkeep`` per session service (one profile, one run). The session service calls it at
these points:

- ``start()`` before it builds the first agent: one ``HookBus`` (``plugins.hook_callback_timeout``)
  and one ``PluginHost`` over the run's shared tool registry; the plugins discovery allows are
  loaded (``register(ctx)`` with its deadline: tools into the profile's overlay, hooks onto the
  bus, slash commands, prompt sections); then the configured shell hooks the person agreed to are
  registered on the same bus (``plugins.shell_hooks.register_shell_hooks``: an interactive
  terminal asks once per hook, ``--accept-hooks`` / ``INDUS_ACCEPT_HOOKS`` /
  ``hooks_auto_accept`` accept, other surfaces skip unconsented hooks with a warning). A profile
  with no plugin enabled and no hook configured imports and loads nothing. Never raises.
- ``selection(...)`` / ``options(...)`` for every top-level session: the toolsets loaded plugins
  registered tools into join the selection, and when anything is live the agent options get the
  bus seams (``plugins.session.plugin_options``: ``pre_tool_call`` / ``post_tool_call`` /
  ``transform_tool_result`` through ``hooks``, ``pre_llm_call`` through ``llm_context``) and the
  plugin prompt sections, rendered once per conversation and frozen with its stored prompt.
  Subagents are built in core from ``subagent_builder`` and never get ``llm_context`` or the
  sections; they do inherit the tool guards (``ChildHooks``).
- ``session_started`` / ``session_ended``: the ``on_session_start`` / ``on_session_end`` observer
  hooks when the service attaches or closes a session.
- ``shutdown()`` at service close: every plugin's registrations are taken back (tools leave the
  registry) and the shell hooks are dropped.

``run_command`` runs a plugin slash command for the classic REPL (on the service loop, through
``call``); ``update_check`` is the maintenance-timer chore for ``plugins.auto_update_check_hours``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from indus.core.agent import AgentOptions
from indus.core.persist import StateStore
from indus.core.prompt.lifecycle import SessionPrompts
from indus.foundation.scope import Scope
from indus.tools.registry import ToolRegistry, scope_key

if TYPE_CHECKING:
    from indus.plugins.context import PluginHost
    from indus.plugins.hooks import HookBus
    from indus.plugins.loader import LoadReport
    from indus.plugins.shell_hooks import HookSpec, ShellHookReport

__all__ = ["PluginUpkeep", "update_check", "upkeep_for"]

log = logging.getLogger(__name__)

_UPKEEP: dict[str, PluginUpkeep] = {}


def upkeep_for(scope: Scope) -> PluginUpkeep | None:
    """The running plugin upkeep of ``scope``'s profile in this process."""
    return _UPKEEP.get(scope_key(scope))


def _wanted(scope: Scope) -> bool:
    """Is any plugin enabled or any shell hook configured? (Nothing imported.)"""
    config = scope.config
    if config is None:
        return False
    for key in ("plugins.enabled", "hooks"):
        try:
            value = config.get(key)
        except Exception:
            continue
        if isinstance(value, Mapping | list) and value:
            return True
    return False


def update_check(scope: Scope) -> Callable[[], object]:
    """The maintenance chore: ``Installer.maybe_check_updates`` when anything is installed."""

    def chore() -> object:
        from indus.plugins.install import Installer

        installer = Installer(scope)
        if not installer.records():
            return None
        return installer.maybe_check_updates()

    return chore


class PluginUpkeep:
    """One session service's plugins and shell hooks (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        registry: ToolRegistry,
        *,
        surface: str,
        interactive: bool = False,
        accept: bool = False,
        ask: Callable[[HookSpec], bool] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.scope = scope
        self.registry = registry
        self.surface = surface
        self.interactive = interactive
        self.accept = accept
        self.ask = ask
        self.cwd = cwd
        self.bus: HookBus | None = None
        self.host: PluginHost | None = None
        self.report: LoadReport | None = None
        self.shell: ShellHookReport | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._toolsets: list[str] = []
        self._started = False
        self._closed = False
        self._lock: asyncio.Lock | None = None

    # -- lifecycle ------------------------------------------------------------------------------

    async def start(self) -> None:
        """Once per service: load the allowed plugins and register the agreed shell hooks."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._started or self._closed:
                return
            self._started = True
            self.loop = asyncio.get_running_loop()
            _UPKEEP[scope_key(self.scope)] = self
            if not _wanted(self.scope) and not self.accept:
                return
            try:
                await self._open()
            except Exception:
                log.warning("plugins and shell hooks could not be started", exc_info=True)

    async def _open(self) -> None:
        from indus.plugins.context import HostPorts, PluginHost
        from indus.plugins.discovery import discover
        from indus.plugins.hooks import HookBus
        from indus.plugins.loader import PluginLoader
        from indus.plugins.shell_hooks import register_shell_hooks
        from indus.service.commands.registry import REGISTRY

        bus = HookBus.from_scope(self.scope, clock=self.scope.clock)
        ports = HostPorts(reserved_commands=lambda name: REGISTRY.resolve(name) is not None)
        host = PluginHost(self.scope, self.registry, bus, ports=ports)
        host.bind_loop()
        self.bus, self.host = bus, host
        found = discover(self.scope, project_root=Path(self.cwd) if self.cwd else None)
        for text in found.warnings:
            log.warning("%s", text)
        if found.loadable():
            self.report = await PluginLoader(host).load(found)
            for key, why in self.report.skipped().items():
                log.warning("plugin %s was not loaded: %s", key, why)
        self._toolsets = sorted(
            {
                entry.toolset
                for provided in host.provided.values()
                for name in provided.tools
                if (entry := self.registry.get(name, self.scope)) is not None
            }
        )
        self.shell = register_shell_hooks(
            bus,
            self.scope,
            interactive=self.interactive,
            surface=self.surface,
            accept=self.accept,
            ask=self.ask,
            cwd=self.cwd,
        )

    @property
    def live(self) -> bool:
        """Does anything run: a loaded plugin or a registered hook?"""
        bus, host = self.bus, self.host
        if bus is None or host is None:
            return False
        return bool(host.provided) or any(bus.has(name) for name in bus.names())

    async def shutdown(self) -> None:
        """Service close: take back what plugins registered, drop the shell hooks."""
        self._closed = True
        host, self.host = self.host, None
        bus, self.bus = self.bus, None
        if host is not None:
            for plugin_id in list(host.provided):
                host.undo(plugin_id)
        if bus is not None:
            from indus.plugins.shell_hooks import unregister

            unregister(bus)
        key = scope_key(self.scope)
        if _UPKEEP.get(key) is self:
            del _UPKEEP[key]

    # -- sessions -------------------------------------------------------------------------------

    def selection(self, selection: Sequence[str] | None) -> list[str] | None:
        """``selection`` plus the toolsets loaded plugins added tools to (``None`` = all)."""
        if selection is None:
            return None
        chosen = list(selection)
        return [*chosen, *(name for name in self._toolsets if name not in chosen)]

    def options(self, options: AgentOptions, session_id: str) -> AgentOptions:
        """``options`` with the bus seams and the frozen plugin sections (unchanged when
        nothing is live)."""
        bus, host = self.bus, self.host
        if bus is None or host is None or not self.live:
            return options
        from indus.plugins.session import plugin_options

        if options.prompt_store is None and isinstance(options.store, StateStore):
            options = replace(options, prompt_store=SessionPrompts(options.store.handle))
        sections = host if host.prompt_sections else None
        return plugin_options(options, bus, session_id=session_id, host=sections)

    async def session_started(self, session_id: str, options: AgentOptions) -> None:
        await self._observe("on_session_start", session_id, options)

    async def session_ended(self, session_id: str, options: AgentOptions | None = None) -> None:
        await self._observe("on_session_end", session_id, options)

    async def _observe(self, name: str, session_id: str, options: AgentOptions | None) -> None:
        bus = self.bus
        if bus is None or not bus.has(name):
            return
        fields: Mapping[str, Any] = options.session_fields if options is not None else {}
        cwd = options.cwd if options is not None else None
        payload: dict[str, Any] = {
            "session_id": session_id,
            "platform": options.platform if options is not None else self.surface,
            "model": str(fields.get("model") or ""),
            "cwd": str(cwd) if cwd is not None else str(fields.get("cwd") or ""),
            "surface": self.surface,
        }
        try:
            await bus.observe(name, **payload)
        except Exception:
            log.warning("the %s hooks failed", name, exc_info=True)

    # -- commands -------------------------------------------------------------------------------

    def command_names(self) -> list[str]:
        host = self.host
        return sorted(host.commands) if host is not None else []

    async def run_command(self, name: str, args: str, *, session_id: str, cwd: str = "") -> str:
        """A plugin slash command's text for the conversation ``session_id``."""
        from indus.plugins.context import CallSite

        host = self.host
        if host is None:
            return f"Unknown command: /{name}"
        site = CallSite(session_key=session_id, conversation=session_id, cwd=cwd, surface="cli")
        return await host.run_command(name, args, site=site, session_id=session_id)

    def call[T](self, coro: Coroutine[Any, Any, T], *, timeout: float | None = 60.0) -> T:
        """Run ``coro`` on the service loop from another thread (slash handlers) and wait."""
        loop = self.loop
        if loop is None or loop.is_closed():
            coro.close()
            raise RuntimeError("the plugin host is not running")
        future: Future[T] = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout)
