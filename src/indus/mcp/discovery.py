"""MCP servers of one profile: discovery, tool registration, schema cache, lazy start (P13-05).

``McpHost`` is the MCP client of one profile. It keeps a ``ServerHandle`` per configured server:
the ``ManagedServer`` (P13-04) that holds the connection, the tools it registered (with the
read-only hints of that listing, frozen until the next one), its call slot and its breaker.

``discover()`` starts every enabled server not running yet, at most ``mcp.discovery_concurrency``
(4; 0 = all) connecting at once (R07.22). Each server has 120 s within its wave and the whole pass
is bounded by min(120 s × waves, 300 s); a server that is still connecting then goes on in the
background. Registration happens on every connect (``ManagedServer.on_ready``), so a late server,
a reconnect and a ``list_changed`` re-fetch all go through the same code:

- ``tools/list`` (≤ 50 pages) → ``tools.include`` / ``exclude`` → ``naming.tool_schema`` → the
  registry overlay of this profile, toolset ``mcp-<server>``, plus the alias ``<server>`` →
  ``mcp-<server>`` (a server named like a built-in toolset unions with it) and the umbrella alias
  ``mcp`` → every ``mcp-<server>`` (what a session host adds to a platform bundle's selection). No
  tool left after the filter: no toolset and no alias. Names that disappeared are deregistered;
  others are replaced in place. Two server tools that normalise to one name are both refused.
- ``list_resources`` / ``read_resource`` / ``list_prompts`` / ``get_prompt`` (``mcp__<server>__…``)
  only when ``tools.resources`` / ``tools.prompts`` allow it and the server advertises the
  capability; a server tool of the same name wins.
- the listing goes to ``<home>/cache/mcp/<server>.json``. A ``lazy`` server with a cache entry of
  the same fingerprint registers from it without starting; its first call starts it. A lazy server
  without a usable entry connects once now to write one.

Status (``status()``): one ``ServerReport`` per configured server; ``lazy`` means registered from
the cache and not started ("starts on first use"). ``DiscoveryReport.summary()`` is the startup
line: tools and servers up, every failed server with its error, servers skipped while cooling down.

Several profiles in one process each have their own host (``register_host`` / ``host_for``).

Per-server connect extras (P13-09..11: OAuth handler, sampling and elicitation callbacks, the
requester slot the call path binds) come from the host's ``extras`` factory when one is given; each
``ServerHandle`` builds its own when it is made, so a restarted server (reload) gets fresh ones.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import mcp_types as types

from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.mcp import calls, trust
from indus.mcp.calls import CallContext, ToolKind, ToolSpec
from indus.mcp.config import McpServer, ServerTable, load_servers, load_settings
from indus.mcp.connect import ConnectOptions, LiveSession, clock_timeout, open_session
from indus.mcp.lifecycle import (
    CooldownLedger,
    ManagedServer,
    McpServerUnavailable,
    Opener,
    RetryPolicy,
    ServerState,
)
from indus.mcp.naming import NameFilter, describe, tool_name, tool_parameters
from indus.mcp.schema_cache import CachedTool, SchemaCache, SchemaEntry, fingerprint
from indus.protocol.requests_port import ServerRequestPort
from indus.safety.approvals.decision import ApprovalState
from indus.tools.registry import ToolNameTaken, ToolRegistry, ToolTraits, scope_key

__all__ = [
    "LAZY_START_EXTRA_S",
    "MCP_TOOLSET",
    "PASS_MAX_S",
    "WAVE_S",
    "DiscoveryReport",
    "McpHost",
    "ServerHandle",
    "ServerReport",
    "drop_host",
    "host_for",
    "register_host",
    "toolset_of",
]

log = logging.getLogger(__name__)

WAVE_S = 120.0
PASS_MAX_S = 300.0
LAZY_START_EXTRA_S = 30.0
_MISSING: object = object()


MCP_TOOLSET = "mcp"
"""The umbrella toolset alias: every MCP server toolset of the profile."""

ExtrasFactory = Callable[[str, McpServer, ConnectOptions], Any]
"""``(server name, config, base options) -> extras.ServerExtras`` (P13-09..11)."""


def toolset_of(server: str) -> str:
    return f"mcp-{server}"


# -- tool entries -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Listed:
    """One server tool as listed (live or from the cache)."""

    name: str
    description: str | None
    input_schema: Mapping[str, Any]
    raw_read_only: object


_UTILITY_TEXT: dict[str, tuple[str, dict[str, Any]]] = {
    "list_resources": (
        "List the resources (files, records, documents) the MCP server {s} offers.",
        {"type": "object", "properties": {}},
    ),
    "read_resource": (
        "Read one resource of the MCP server {s} by its URI.",
        {
            "type": "object",
            "properties": {"uri": {"type": "string", "description": "the resource URI"}},
            "required": ["uri"],
        },
    ),
    "list_prompts": (
        "List the prompt templates the MCP server {s} offers.",
        {"type": "object", "properties": {}},
    ),
    "get_prompt": (
        "Fetch one prompt template of the MCP server {s}, filled with the given arguments.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "the prompt name"},
                "arguments": {
                    "type": "object",
                    "description": "values for the prompt's arguments",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["name"],
        },
    ),
}
_UTILITY_FAMILY: dict[str, str] = {
    "list_resources": "resources",
    "read_resource": "resources",
    "list_prompts": "prompts",
    "get_prompt": "prompts",
}


def utility_kinds(server: McpServer, caps: types.ServerCapabilities | None) -> list[str]:
    """Utility tools allowed by the config and advertised by the server."""
    allowed = {"resources": server.tools.resources, "prompts": server.tools.prompts}
    kinds: list[str] = []
    for kind in calls.UTILITY_KINDS:
        family = _UTILITY_FAMILY[kind]
        if not allowed[family] or caps is None or getattr(caps, family, None) is None:
            continue
        kinds.append(kind)
    return kinds


# -- one server ---------------------------------------------------------------------------------


class ServerHandle:
    """One configured server of a host (see the module notes)."""

    def __init__(self, host: McpHost, name: str, config: McpServer) -> None:
        self.host = host
        self.name = name
        self.config = config
        self.print = fingerprint(config)
        self.home = host.scope.home
        self.clock: Clock = host.clock
        self.breaker = host.breakers.get(host.profile, name)
        self.specs: dict[str, ToolSpec] = {}
        self.schemas: dict[str, dict[str, Any]] = {}
        self.lazy_pending = False
        self.closed = False
        self.last_listing: tuple[str, ...] = ()
        self._call_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self.refresher: Any = None  # reload.ListChangedRefresher, made on the first notification
        self.extras: Any = None  # extras.ServerExtras when the host has an extras factory
        self.requesters: Any = None  # sampling.Requesters: calls.call binds each tools/call
        base = host.options
        if host.extras is not None:
            self.extras = host.extras(name, config, base)
            base = self.extras.options
            self.requesters = self.extras.requesters
        options = replace(base, on_notification=self._on_notification)
        self.managed = ManagedServer(
            name,
            config,
            host.scope,
            clock=host.clock,
            options=options,
            policy=host.policy,
            ledger=host.ledger,
            opener=host.watched_opener,
            on_ready=self._on_ready,
        )

    # -- state ------------------------------------------------------------------------------

    @property
    def launched(self) -> bool:
        return self.managed._task is not None  # pyright: ignore[reportPrivateUsage]

    @property
    def state(self) -> str:
        if self.closed:
            return "closed"
        if self.lazy_pending and not self.launched:
            return "lazy"
        return self.managed.state.value

    def launch(self) -> None:
        with trust.background_auth():
            self.managed.launch()

    # -- the call path (calls.CallTarget) ---------------------------------------------------

    def slot(self) -> AbstractAsyncContextManager[None]:
        if self.config.supports_parallel_tool_calls:
            return contextlib.nullcontext()
        return self._call_lock

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[LiveSession]:
        if self.closed:
            raise McpServerUnavailable(f"MCP server {self.name!r} was removed", server=self.name)
        if not self.launched:
            await self._start_now()
        async with self.managed.use() as live:
            yield live

    async def _start_now(self) -> None:
        """First use of a lazy server: start it and wait (bounded) for the first connect."""
        async with self._start_lock:
            if self.launched:
                return
            log.info("MCP server %r: starting on first use", self.name)
            self.launch()
        bound = self.config.connect_timeout + LAZY_START_EXTRA_S
        try:
            async with clock_timeout(self.clock, bound):
                await self.managed.start()
        except TimeoutError:
            raise McpServerUnavailable(
                f"MCP server {self.name!r} did not start within {bound:g} s", server=self.name
            ) from None

    # -- listings ---------------------------------------------------------------------------

    async def _on_ready(self, live: LiveSession) -> None:
        """Runs in the server's own task on every connect, before it is published."""
        async with clock_timeout(self.clock, self.config.connect_timeout):
            listed = await live.list_tools() if live.offers_tools else []
        self.apply_listing(live, listed)

    def apply_listing(self, live: LiveSession, listed: Sequence[types.Tool]) -> None:
        """Register a live listing and write it to the schema cache."""
        tools = [
            _Listed(
                t.name,
                t.description,
                t.input_schema,
                live.raw_hints.get(t.name, _MISSING),
            )
            for t in listed
        ]
        utilities = utility_kinds(self.config, live.capabilities)
        self.host.register(self, tools, utilities)
        self.lazy_pending = False
        self.host.auth.clear(self.name)
        self.host.cache.write(
            self.name,
            self.print,
            [
                CachedTool(
                    t.name,
                    t.description,
                    t.input_schema,
                    None if t.raw_read_only is _MISSING else t.raw_read_only,
                )
                for t in tools
            ],
            utilities,
            now=self.clock.now().timestamp(),
        )

    def apply_cache(self, entry: SchemaEntry) -> None:
        tools = [
            _Listed(t.name, t.description, t.input_schema, t.raw_read_only) for t in entry.tools
        ]
        self.host.register(self, tools, list(entry.utilities))
        self.lazy_pending = True

    async def refetch(self) -> None:
        """Re-list the tools of the live session (``list_changed``); skipped when not ready."""
        live = self.managed.live
        if live is None or self.closed:
            log.debug("MCP server %r: not ready; tool re-fetch skipped", self.name)
            return
        async with self.managed.use() as session:
            async with clock_timeout(self.clock, self.config.connect_timeout):
                listed = await session.list_tools() if session.offers_tools else []
            before = set(self.specs)
            self.apply_listing(session, listed)
        added, gone = set(self.specs) - before, before - set(self.specs)
        if added or gone:
            log.warning(
                "MCP server %r changed its tools: added %s; removed %s",
                self.name,
                sorted(added) or "none",
                sorted(gone) or "none",
            )

    async def _on_notification(self, name: str, message: types.ServerNotification) -> None:
        if isinstance(message, types.ToolListChangedNotification):
            if self.closed:
                return
            if self.refresher is None:
                from indus.mcp.reload import ListChangedRefresher

                self.refresher = ListChangedRefresher(self)
            self.refresher.poke()
        elif isinstance(
            message, types.PromptListChangedNotification | types.ResourceListChangedNotification
        ):
            log.debug("MCP server %r: %s received and ignored", name, message.method)

    async def close(self) -> None:
        self.closed = True
        if self.refresher is not None:
            await self.refresher.close()
        await self.managed.close()

    def report(self) -> ServerReport:
        status = self.managed.status()
        error = status.last_error
        detail = ""
        if self.state == "lazy":
            detail = "lazy, starts on first use"
        elif self.host.auth.parked(self.name):
            detail = f"needs sign-in again (indus mcp login {self.name})"
        return ServerReport(
            name=self.name,
            state=self.state,
            kind=self.config.kind,
            tools=len(self.specs),
            error=error,
            detail=detail,
            next_attempt_in=status.next_attempt_in,
        )


# -- reports ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerReport:
    name: str
    state: str
    kind: str
    tools: int = 0
    error: str | None = None
    detail: str = ""
    next_attempt_in: float | None = None


@dataclass
class DiscoveryReport:
    connected: list[str] = field(default_factory=list[str])
    lazy: list[str] = field(default_factory=list[str])
    failed: dict[str, str] = field(default_factory=dict[str, str])
    cooling_down: dict[str, float] = field(default_factory=dict[str, float])
    disabled: list[str] = field(default_factory=list[str])
    problems: dict[str, str] = field(default_factory=dict[str, str])
    tools: int = 0

    def summary(self) -> str:
        servers = len(self.connected) + len(self.lazy)
        text = f"MCP: {self.tools} tool(s) from {servers} server(s)"
        if self.lazy:
            text += f" ({len(self.lazy)} lazy, not started: {', '.join(sorted(self.lazy))})"
        if self.failed:
            parts = "; ".join(f"{n} ({why})" for n, why in sorted(self.failed.items()))
            text += f"; {len(self.failed)} failed: {parts}"
        if self.cooling_down:
            parts = "; ".join(
                f"{n} (next try in {math.ceil(w)} s)" for n, w in sorted(self.cooling_down.items())
            )
            text += f"; skipped while cooling down: {parts}"
        if self.problems:
            parts = "; ".join(f"{n} ({why})" for n, why in sorted(self.problems.items()))
            text += f"; not configured correctly: {parts}"
        return text


# -- the host -----------------------------------------------------------------------------------


class McpHost:
    """The MCP client of one profile (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        registry: ToolRegistry,
        *,
        clock: Clock | None = None,
        options: ConnectOptions | None = None,
        opener: Opener = open_session,
        policy: RetryPolicy | None = None,
        credential_stamp: trust.CredentialStamp | None = None,
        approvals: ApprovalState | None = None,
        extras: ExtrasFactory | None = None,
    ) -> None:
        self.scope = scope
        self.extras = extras
        self.registry = registry
        self.clock: Clock = clock or scope.clock
        self.options = options or ConnectOptions()
        self.opener = opener
        self.policy = policy or RetryPolicy()
        self.ledger = CooldownLedger(self.policy)
        self.breakers = trust.BreakerBook(self.clock)
        self.auth = trust.AuthParking(credential_stamp)
        self.cache = SchemaCache(scope.home)
        self.approvals = approvals
        self.profile = scope_key(scope)
        self.handles: dict[str, ServerHandle] = {}
        self.table = ServerTable()
        # Told when the tool surface changed (``reload.ToolSurfaceChanged``): the session host
        # drops the provider prompt cache and rebuilds the stored tool array at the next session
        # or compression, never in the middle of one.
        self.listeners: list[Callable[[Any], None]] = []

    def emit(self, event: object) -> None:
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                log.exception("MCP tool-change listener failed")

    # -- opening ----------------------------------------------------------------------------

    def watched_opener(
        self,
        name: str,
        server: McpServer,
        scope: Scope,
        *,
        clock: Clock,
        options: ConnectOptions | None = None,
    ) -> AbstractAsyncContextManager[LiveSession]:
        """``self.opener`` with credential refusals recorded for parking (P13-07)."""
        return self._watch(name, self.opener(name, server, scope, clock=clock, options=options))

    @asynccontextmanager
    async def _watch(
        self, name: str, opening: AbstractAsyncContextManager[LiveSession]
    ) -> AsyncGenerator[LiveSession]:
        entered = False
        try:
            async with opening as live:
                entered = True
                yield live
        except Exception as exc:
            if not entered:
                self.auth.note(name, exc)
            raise

    # -- registration -----------------------------------------------------------------------

    def _handler(self, handle: ServerHandle, spec: ToolSpec) -> Any:
        host = self

        async def run(
            args: dict[str, Any],
            *,
            scope: Scope | None = None,
            session_id: str | None = None,
            turn_id: str | None = None,
            broker: ServerRequestPort | None = None,
            run_context: object = None,
            approval_judge: Any = None,
            native_vision: bool = False,
        ) -> Any:
            ctx = CallContext(
                scope=scope if scope is not None else host.scope,
                session_id=session_id,
                turn_id=turn_id,
                broker=broker,
                run_context=run_context,
                judge=approval_judge,
                native_vision=bool(native_vision),
                approvals=host.approvals,
            )
            return await calls.call(handle, spec, args, ctx)

        run.__qualname__ = f"mcp:{handle.name}:{spec.server_tool}"
        return run

    def _entries(
        self, handle: ServerHandle, tools: Iterable[_Listed], utilities: Iterable[str]
    ) -> list[tuple[ToolSpec, dict[str, Any]]]:
        name = handle.name
        keep = NameFilter.of(handle.config.tools.include, handle.config.tools.exclude)
        native: dict[str, list[_Listed]] = {}
        for tool in tools:
            if not keep(tool.name):
                continue
            native.setdefault(tool_name(name, tool.name), []).append(tool)
        entries: list[tuple[ToolSpec, dict[str, Any]]] = []
        for registry_name, group in native.items():
            if len(group) > 1:
                log.error(
                    "MCP server %r: tools %s all become %s; none of them is registered",
                    name,
                    ", ".join(sorted(t.name for t in group)),
                    registry_name,
                )
                continue
            tool = group[0]
            hint = None if tool.raw_read_only is _MISSING else tool.raw_read_only
            spec = ToolSpec(registry_name, tool.name, "tool", trust.read_only(hint))
            schema = {
                "name": registry_name,
                "description": describe(name, tool.name, tool.description),
                "parameters": tool_parameters(dict(tool.input_schema)),
            }
            entries.append((spec, schema))
        for kind in utilities:
            text = _UTILITY_TEXT.get(kind)
            if text is None:
                continue
            registry_name = tool_name(name, kind)
            if registry_name in native:
                log.info("MCP server %r has its own %s; the generic one is skipped", name, kind)
                continue
            utility: ToolKind = kind  # pyright: ignore[reportAssignmentType]
            spec = ToolSpec(registry_name, kind, utility, True)
            schema = {
                "name": registry_name,
                "description": text[0].format(s=name),
                "parameters": dict(text[1]),
            }
            entries.append((spec, schema))
        return entries

    def register(
        self, handle: ServerHandle, tools: Iterable[_Listed], utilities: Iterable[str]
    ) -> None:
        """Bring the registry in line with one listing of ``handle``'s server."""
        if handle.closed or self.handles.get(handle.name) is not handle:
            return
        toolset = toolset_of(handle.name)
        fresh: dict[str, ToolSpec] = {}
        schemas: dict[str, dict[str, Any]] = {}
        for spec, schema in self._entries(handle, tools, utilities):
            name = spec.registry_name
            current = self.registry.get(name, self.scope)
            if (
                handle.specs.get(name) == spec
                and handle.schemas.get(name) == schema
                and current is not None
                and current.toolset == toolset
            ):
                fresh[name], schemas[name] = spec, schema  # unchanged: keep the entry
                continue
            try:
                self.registry.register(
                    spec.registry_name,
                    toolset,
                    schema,
                    self._handler(handle, spec),
                    is_async=True,
                    profile=self.profile,
                    traits=ToolTraits(read_only=spec.read_only),
                )
            except ToolNameTaken as exc:
                log.warning("MCP server %r: %s skipped (%s)", handle.name, spec.registry_name, exc)
                continue
            fresh[name], schemas[name] = spec, schema
        for stale in set(handle.specs) - set(fresh):
            self.registry.deregister(stale, profile=self.profile)
        handle.specs = fresh
        handle.schemas = schemas
        handle.last_listing = tuple(sorted(fresh))
        self.registry.set_toolset_alias(
            handle.name, toolset, profile=self.profile, present=bool(fresh)
        )
        self.registry.set_toolset_alias(
            MCP_TOOLSET, toolset, profile=self.profile, present=bool(fresh)
        )

    def unregister(self, handle: ServerHandle) -> None:
        for name in handle.specs:
            entry = self.registry.get(name, self.scope)
            if entry is not None and entry.toolset == toolset_of(handle.name):
                self.registry.deregister(name, profile=self.profile)
        handle.specs = {}
        handle.schemas = {}
        self.registry.set_toolset_alias(
            handle.name, toolset_of(handle.name), profile=self.profile, present=False
        )
        self.registry.set_toolset_alias(
            MCP_TOOLSET, toolset_of(handle.name), profile=self.profile, present=False
        )

    # -- discovery --------------------------------------------------------------------------

    def tool_count(self) -> int:
        return sum(len(h.specs) for h in self.handles.values())

    async def discover(self, table: ServerTable | None = None) -> DiscoveryReport:
        """Start every enabled server that is not running yet; see the module notes."""
        table = table if table is not None else load_servers(self.scope)
        self.table = table
        report = DiscoveryReport(problems=dict(table.problems))
        fresh: list[ServerHandle] = []
        now = self.clock.monotonic()
        for name, config in table.servers.items():
            if not config.enabled:
                report.disabled.append(name)
                continue
            handle = self.handles.get(name)
            if handle is not None:
                self._existing(handle, report, now)
                continue
            handle = ServerHandle(self, name, config)
            self.handles[name] = handle
            if config.lazy:
                entry = self.cache.read(name, handle.print)
                if entry is not None:
                    handle.apply_cache(entry)
                    report.lazy.append(name)
                    continue
                log.info("MCP server %r: lazy, but no current schema cache; connecting once", name)
            fresh.append(handle)
        await self._start_all(fresh, load_settings(self.scope).discovery_concurrency, report)
        report.tools = self.tool_count()
        if report.failed or report.problems:
            log.warning("%s", report.summary())
        else:
            log.info("%s", report.summary())
        return report

    def _existing(self, handle: ServerHandle, report: DiscoveryReport, now: float) -> None:
        state = handle.state
        if state == "lazy":
            report.lazy.append(handle.name)
        elif state == ServerState.READY:
            report.connected.append(handle.name)
        elif state == ServerState.COOLING_DOWN:
            wait = self.ledger.remaining(handle.name, now)
            report.cooling_down[handle.name] = wait or 0.0
        elif state == ServerState.PARKED:
            if self.auth.may_retry(handle.name):
                handle.managed.request_reconnect()
            else:
                log.debug(
                    "MCP server %r: auth-parked, credentials unchanged; left alone", handle.name
                )
            report.failed[handle.name] = handle.managed.status().last_error or "parked"
        else:
            report.failed[handle.name] = f"still {state}"

    async def _start_all(
        self, handles: Sequence[ServerHandle], concurrency: int, report: DiscoveryReport
    ) -> None:
        if not handles:
            return
        cap = concurrency if concurrency > 0 else len(handles)
        waves = max(1, math.ceil(len(handles) / cap))
        budget = min(WAVE_S * waves, PASS_MAX_S)
        gate = asyncio.Semaphore(cap)
        settled: set[str] = set()
        started: dict[str, float] = {}
        slow = f"still connecting after {WAVE_S:g} s (keeps trying in the background)"

        async def one(handle: ServerHandle) -> None:
            async with gate:
                started[handle.name] = self.clock.monotonic()
                handle.launch()
                try:
                    async with clock_timeout(self.clock, WAVE_S):
                        await handle.managed.start()
                except McpServerUnavailable as exc:
                    report.failed[handle.name] = str(exc)
                except TimeoutError:
                    report.failed[handle.name] = slow
                else:
                    report.connected.append(handle.name)
                settled.add(handle.name)

        try:
            async with clock_timeout(self.clock, budget):
                await asyncio.gather(*(one(h) for h in handles))
        except TimeoutError:
            now = self.clock.monotonic()
            for handle in handles:
                if handle.name in settled:
                    continue
                began = started.get(handle.name)
                if began is not None and now - began >= WAVE_S:
                    report.failed[handle.name] = slow
                else:
                    report.failed[handle.name] = (
                        f"discovery ended after {budget:g} s before it connected "
                        "(keeps trying in the background)"
                    )

    # -- changes ----------------------------------------------------------------------------

    async def remove(self, name: str) -> bool:
        handle = self.handles.pop(name, None)
        if handle is None:
            return False
        self.unregister(handle)
        await handle.close()
        self.breakers.drop(self.profile, name)
        self.auth.clear(name)
        self.ledger.clear(name)
        return True

    def reauthorised(self, name: str) -> None:
        """The user signed in again: an auth-parked server tries at once."""
        self.auth.clear(name)
        handle = self.handles.get(name)
        if handle is not None and handle.launched:
            handle.managed.request_reconnect()

    def status(self) -> list[ServerReport]:
        reports: list[ServerReport] = []
        for name, config in self.table.servers.items():
            handle = self.handles.get(name)
            if not config.enabled:
                reports.append(ServerReport(name, "disabled", config.kind))
            elif handle is None:
                reports.append(ServerReport(name, "not_started", config.kind))
            else:
                reports.append(handle.report())
        for name, problem in self.table.problems.items():
            reports.append(ServerReport(name or "(mcp_servers)", "failed", "?", error=problem))
        return reports

    async def close(self) -> None:
        for name in list(self.handles):
            await self.remove(name)


# -- hosts of this process ----------------------------------------------------------------------

_HOSTS: dict[str, McpHost] = {}


def register_host(host: McpHost) -> None:
    _HOSTS[host.profile] = host


def host_for(scope: Scope) -> McpHost | None:
    return _HOSTS.get(scope_key(scope))


def drop_host(scope: Scope) -> McpHost | None:
    return _HOSTS.pop(scope_key(scope), None)
