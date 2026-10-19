"""Reload, config watch and ``notifications/tools/list_changed`` (P13-08).

- ``reconcile(host)``: compare the configured servers with the running ones. Added → started;
  removed or disabled → closed, tools and alias deregistered; changed (any setting of the entry)
  → closed and started again; unchanged → kept (a parked one is nudged unless it is parked on
  refused credentials, P13-07). An explicit ``/reload-mcp`` (``reason="reload"``) restarts a
  parked server instead, so it gets fresh connect extras (a new OAuth handler that reads the
  stored sign-in again, P13-09). Bounded by 30 s; servers still connecting then go on in the
  background and register themselves when they come up.
- ``reload_mcp(host, ask=…)`` is ``/reload-mcp``: read the config again, ask first while
  ``approvals.mcp_reload_confirm`` is on (once / always — which writes the switch off — / cancel),
  reconcile. The running session keeps its stored tool array and system prompt (R01.1, R07.23);
  the host's listeners get ``ToolSurfaceChanged`` so the provider prompt cache is dropped and the
  array is rebuilt for the next session or compression. ``reload(scope)`` is the TUI bridge entry
  (the client's request is the confirmation).
- ``ConfigWatcher`` (CLI; on unless ``mcp.auto_reload_on_config_change`` is off) polls the config
  file's signature, waits for it to settle (debounce), and reconciles when ``mcp_servers``
  actually changed. The gateway (P15) calls ``reconcile`` itself.
- ``ListChangedRefresher``: the first ``tools/list_changed`` of a server opens a 2 s window that
  folds every further one in, then one re-fetch runs (one at a time per server); notifications
  that arrive during a re-fetch cause exactly one more. Prompt and resource change notifications
  are received and ignored (``discovery.ServerHandle``).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from indus.foundation.blocking import run_blocking
from indus.foundation.config import loader, writer
from indus.foundation.scope import Scope
from indus.mcp.config import ServerTable, load_servers, load_settings
from indus.mcp.connect import clock_timeout
from indus.mcp.discovery import DiscoveryReport, McpHost, host_for
from indus.mcp.lifecycle import ServerState
from indus.safety.approvals.decision import setting

if TYPE_CHECKING:
    from indus.mcp.discovery import ServerHandle

__all__ = [
    "COALESCE_S",
    "CONFIRM_KEY",
    "RECONCILE_TIMEOUT_S",
    "WATCH_DEBOUNCE_S",
    "WATCH_INTERVAL_S",
    "ConfigWatcher",
    "ListChangedRefresher",
    "ReconcileReport",
    "ReloadChoice",
    "ReloadOutcome",
    "ToolSurfaceChanged",
    "fresh_scope",
    "reconcile",
    "reload",
    "reload_mcp",
]

log = logging.getLogger(__name__)

COALESCE_S = 2.0
RECONCILE_TIMEOUT_S = 30.0
WATCH_INTERVAL_S = 2.0
WATCH_DEBOUNCE_S = 1.0
CONFIRM_KEY = "approvals.mcp_reload_confirm"

ReloadChoice = Literal["once", "always", "cancel"]
Ask = Callable[[str], Awaitable[ReloadChoice]]


@dataclass(frozen=True)
class ToolSurfaceChanged:
    """The MCP tools of a profile changed: drop the provider prompt cache; rebuild the stored
    tool array for the next session or compression (never mid-session)."""

    profile: str
    reason: str
    generation: int
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


@dataclass
class ReconcileReport:
    added: list[str] = field(default_factory=list[str])
    removed: list[str] = field(default_factory=list[str])
    restarted: list[str] = field(default_factory=list[str])
    kept: list[str] = field(default_factory=list[str])
    discovery: DiscoveryReport | None = None
    timed_out: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.restarted)

    def summary(self) -> str:
        parts = [
            f"{label}: {', '.join(sorted(names))}"
            for label, names in (
                ("added", self.added),
                ("removed", self.removed),
                ("restarted", self.restarted),
            )
            if names
        ]
        text = "; ".join(parts) if parts else "no server changes"
        if self.discovery is not None:
            text += f". {self.discovery.summary()}"
        if self.timed_out:
            text += f". Still connecting after {RECONCILE_TIMEOUT_S:g} s (in the background)."
        return text


@dataclass(frozen=True)
class ReloadOutcome:
    reloaded: bool
    message: str
    report: ReconcileReport | None = None


# -- reconcile ----------------------------------------------------------------------------------


def _same(a: Any, b: Any) -> bool:
    return a.model_dump(mode="json") == b.model_dump(mode="json")


def fresh_scope(scope: Scope) -> Scope:
    """``scope`` with its config read again from disk (an unreadable file keeps the old view)."""
    try:
        view = loader.load(scope)
    except Exception as exc:
        log.warning("could not read the config again (%s); keeping the loaded one", exc)
        return scope
    if view.failed:
        log.warning("the config file could not be read; MCP servers left as they are")
        return scope
    return scope.with_config(view)


async def reconcile(
    host: McpHost,
    table: ServerTable | None = None,
    *,
    scope: Scope | None = None,
    reason: str = "reload",
    timeout: float = RECONCILE_TIMEOUT_S,
) -> ReconcileReport:
    """Bring the running servers in line with ``table`` (default: the host scope's config)."""
    if scope is not None:
        host.scope = scope
    table = table if table is not None else load_servers(host.scope)
    before = {name: set(h.specs) for name, h in host.handles.items()}
    generation = host.registry.generation
    wanted = {name: cfg for name, cfg in table.servers.items() if cfg.enabled}
    report = ReconcileReport()
    for name in list(host.handles):
        current = host.handles[name]
        if name not in wanted:
            await host.remove(name)
            report.removed.append(name)
        elif not _same(current.config, wanted[name]) or (
            reason == "reload" and current.state == ServerState.PARKED
        ):
            await host.remove(name)
            report.restarted.append(name)
        else:
            report.kept.append(name)
    report.added = [n for n in wanted if n not in before]
    try:
        async with clock_timeout(host.clock, timeout):
            report.discovery = await host.discover(table)
    except TimeoutError:
        report.timed_out = True
        log.warning("MCP reconcile: servers still connecting after %.0f s", timeout)
    after = {name for h in host.handles.values() for name in h.specs}
    old = {name for names in before.values() for name in names}
    if host.registry.generation != generation or report.changed:
        host.emit(
            ToolSurfaceChanged(
                host.profile,
                reason,
                host.registry.generation,
                tuple(sorted(after - old)),
                tuple(sorted(old - after)),
            )
        )
    log.info("MCP reconcile (%s): %s", reason, report.summary())
    return report


# -- /reload-mcp --------------------------------------------------------------------------------

_QUESTION = (
    "Reload the MCP servers? Their tools change, so the provider's prompt cache starts over; "
    "this session keeps its current tools until the next session or compression."
)


def confirm_on(scope: Scope) -> bool:
    raw = setting(scope, CONFIRM_KEY)
    if isinstance(raw, str):
        return raw.strip().lower() not in ("false", "no", "off", "0")
    return bool(raw) if raw is not None else True


async def reload_mcp(host: McpHost, *, ask: Ask | None = None) -> ReloadOutcome:
    """``/reload-mcp``. ``ask`` is the confirmation prompt; None means the request itself is the
    confirmation (the TUI bridge, the API)."""
    scope = fresh_scope(host.scope)
    if ask is not None and confirm_on(scope):
        choice = await ask(_QUESTION)
        if choice == "cancel":
            return ReloadOutcome(False, "MCP reload cancelled; nothing changed.")
        if choice == "always":
            try:
                await run_blocking(
                    writer.save,
                    scope,
                    {"approvals": {"mcp_reload_confirm": False}},
                    pool="io",
                    label="mcp reload confirm",
                )
            except Exception as exc:
                log.warning("could not save %s: false (%s)", CONFIRM_KEY, exc)
    report = await reconcile(host, scope=scope, reason="reload")
    return ReloadOutcome(True, f"MCP servers reloaded: {report.summary()}", report)


async def reload(scope: Scope) -> ReloadOutcome:
    """The bridge's ``reload.mcp``: reload the profile's servers without asking."""
    host = host_for(scope)
    if host is None:
        return ReloadOutcome(False, "No MCP servers are running for this profile.")
    return await reload_mcp(host)


# -- the config watcher -------------------------------------------------------------------------


def _signature(scope: Scope) -> tuple[int, int] | None:
    try:
        info = loader.config_path(scope).stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _servers_raw(scope: Scope) -> Any:
    config = scope.config
    raw = config.get("mcp_servers") if config is not None else None
    return copy.deepcopy(dict(raw)) if isinstance(raw, Mapping) else raw


class ConfigWatcher:
    """Reconciles ``host`` when ``mcp_servers`` changes on disk (see the module notes)."""

    def __init__(
        self,
        host: McpHost,
        *,
        interval: float = WATCH_INTERVAL_S,
        debounce: float = WATCH_DEBOUNCE_S,
        timeout: float = RECONCILE_TIMEOUT_S,
    ) -> None:
        self.host = host
        self.interval = interval
        self.debounce = debounce
        self.timeout = timeout
        self.checks = 0
        self.runs = 0
        self._task: asyncio.Task[None] | None = None
        self._signature = _signature(host.scope)
        self._servers = _servers_raw(host.scope)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        clock = self.host.clock
        while True:
            await clock.sleep(self.interval)
            seen = await run_blocking(_signature, self.host.scope, pool="io", label="mcp watch")
            if seen == self._signature:
                continue
            while True:  # wait for the writer to finish
                await clock.sleep(self.debounce)
                again = await run_blocking(
                    _signature, self.host.scope, pool="io", label="mcp watch"
                )
                if again == seen:
                    break
                seen = again
            self._signature = seen
            try:
                await self.check()
            except Exception:
                log.exception("MCP config watch: reconcile failed")

    async def check(self) -> ReconcileReport | None:
        """Reconcile when ``mcp_servers`` differs from the last state seen and auto reload is on."""
        self.checks += 1
        scope = await run_blocking(fresh_scope, self.host.scope, pool="io", label="mcp watch")
        servers = _servers_raw(scope)
        if servers == self._servers:
            return None
        if not load_settings(scope).auto_reload_on_config_change:
            log.info("mcp_servers changed on disk; auto reload is off (use /reload-mcp)")
            self._servers = servers
            return None
        self._servers = servers
        self.runs += 1
        log.info("mcp_servers changed on disk; reconciling")
        return await reconcile(self.host, scope=scope, reason="config_change", timeout=self.timeout)


# -- tools/list_changed -------------------------------------------------------------------------


class ListChangedRefresher:
    """Coalesces one server's ``tools/list_changed`` storms into few re-fetches."""

    def __init__(self, handle: ServerHandle, *, window: float = COALESCE_S) -> None:
        self.handle = handle
        self.window = window
        self.fetches = 0
        self._phase: Literal["idle", "waiting", "fetching"] = "idle"
        self._again = False
        self._task: asyncio.Task[None] | None = None

    def poke(self) -> None:
        if self._phase == "idle":
            self._phase = "waiting"
            self._task = asyncio.ensure_future(self._run())
        elif self._phase == "fetching":
            self._again = True
        # waiting: folded into the coming re-fetch

    async def _run(self) -> None:
        handle = self.handle
        try:
            while True:
                self._phase = "waiting"
                await handle.clock.sleep(self.window)
                self._phase = "fetching"
                self._again = False
                generation = handle.host.registry.generation
                before = set(handle.specs)
                try:
                    await handle.refetch()
                except Exception as exc:
                    log.warning("MCP server %r: tool re-fetch failed: %s", handle.name, exc)
                self.fetches += 1
                if handle.host.registry.generation != generation:
                    after = set(handle.specs)
                    handle.host.emit(
                        ToolSurfaceChanged(
                            handle.host.profile,
                            "list_changed",
                            handle.host.registry.generation,
                            tuple(sorted(after - before)),
                            tuple(sorted(before - after)),
                        )
                    )
                if not self._again or handle.closed:
                    return
        finally:
            self._phase = "idle"

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
