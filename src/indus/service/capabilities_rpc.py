"""``capabilities.summary``: counts for the desktop Capabilities page's tab pills (P24-26).

The page reads and changes skills, toolsets, MCP servers and plugins through the daemon's existing
REST routes (P18-14); this one read method only counts them for a profile, so the pills can show
numbers for the tabs the person is not on without loading four lists. The counts follow the same
rules as those routes: loadable skills that do not come from a plugin (enabled unless listed in
``skills.disabled``); the plain toolsets the CLI selection offers; ``mcp_servers`` entries
(enabled unless ``enabled: false``); discovered plugins (enabled when their state is ``enabled``).

``CAPABILITY_METHODS`` (with the wire models, in ``indus.protocol.capabilities``, re-exported here)
holds the catalogue row and ``register(table)`` adds the handler to a dispatcher table.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

from indus.foundation import config
from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.learning.skills.resolver import load_catalog
from indus.plugins.discovery import discover
from indus.protocol.base import ProtocolModel
from indus.protocol.capabilities import (
    CAPABILITY_METHODS,
    CapabilitiesSummaryParams,
    CapabilitiesSummaryResult,
    CapabilityCount,
)
from indus.tools import toolsets

__all__ = [
    "CAPABILITY_HANDLERS",
    "CAPABILITY_METHODS",
    "CapabilitiesSummaryParams",
    "CapabilitiesSummaryResult",
    "CapabilityCount",
    "register",
    "summarize",
]

CLI_PLATFORM = "cli"


class CapabilitiesContext(Protocol):
    @property
    def scope(self) -> Scope: ...


def _skills(scope: Scope) -> CapabilityCount:
    records = [r for r in load_catalog(scope).loadable() if r.plugin is None]
    return CapabilityCount(total=len(records), enabled=sum(1 for r in records if not r.disabled))


def _plain_toolsets() -> list[str]:
    names: list[str] = []
    for name, entry in sorted(toolsets.TOOLSETS.items()):
        if entry.includes or entry.posture or name == toolsets.CORE:
            continue
        if name.startswith(toolsets.BUNDLE_PREFIX):
            continue
        names.append(name)
    return names


def _cli_selection(scope: Scope) -> list[str]:
    saved: object = config.load(scope).user_raw().get("platform_toolsets")
    if isinstance(saved, Mapping):
        chosen: object = cast(Mapping[str, object], saved).get(CLI_PLATFORM)
        if isinstance(chosen, list):
            return [str(n) for n in cast(list[object], chosen)]
    return [f"{toolsets.BUNDLE_PREFIX}{CLI_PLATFORM}"]


def _toolsets(scope: Scope) -> CapabilityCount:
    offered = toolsets.select(_cli_selection(scope), scope=scope, platform=CLI_PLATFORM).tools
    names = _plain_toolsets()
    enabled = 0
    for name in names:
        tools = set(toolsets.resolve(name, None, scope))
        if tools and tools <= offered:
            enabled += 1
    return CapabilityCount(total=len(names), enabled=enabled)


def _mcp(scope: Scope) -> CapabilityCount:
    raw: object = config.load(scope).get("mcp_servers")
    if not isinstance(raw, Mapping):
        return CapabilityCount(total=0, enabled=0)
    entries = list(cast(Mapping[str, object], raw).values())
    on = sum(
        1
        for entry in entries
        if isinstance(entry, Mapping)
        and cast(Mapping[str, object], entry).get("enabled") is not False
    )
    return CapabilityCount(total=len(entries), enabled=on)


def _plugins(scope: Scope) -> CapabilityCount:
    found = discover(scope, project_root=scope.home / ".no-project")
    records = list(found.records.values())
    return CapabilityCount(
        total=len(records), enabled=sum(1 for r in records if r.state == "enabled")
    )


def summarize(scope: Scope) -> CapabilitiesSummaryResult:
    """The counts for ``scope`` (blocking: reads the profile's files)."""
    live = scope.with_config(config.load(scope))
    return CapabilitiesSummaryResult(
        skills=_skills(live),
        toolsets=_toolsets(live),
        mcp_servers=_mcp(live),
        plugins=_plugins(live),
    )


async def capabilities_summary(
    ctx: CapabilitiesContext, params: CapabilitiesSummaryParams
) -> CapabilitiesSummaryResult:
    return await run_blocking(summarize, ctx.scope, label="capabilities summary")


CAPABILITY_HANDLERS: Mapping[str, Callable[[Any, Any], Awaitable[ProtocolModel]]] = {
    "capabilities.summary": capabilities_summary,
}


def register(table: Any) -> None:
    """Add the handler to a ``MethodTable`` (the name must be in the method catalogue)."""
    for name, handler in CAPABILITY_HANDLERS.items():
        table.add(name, handler)
