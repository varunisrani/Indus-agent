"""``capabilities.summary``: the counts the desktop Capabilities page shows (P24-26).

The wire models and the ``CAPABILITY_METHODS`` row live here so the method catalogue and the
generated TypeScript include them; the handler is in ``indus.service.capabilities_rpc``.
"""

from __future__ import annotations

from indus.protocol.base import ProtocolModel
from indus.protocol.errors import ErrorCode


class CapabilitiesSummaryParams(ProtocolModel):
    """Count the capabilities of the caller's profile."""


class CapabilityCount(ProtocolModel):
    """How many of one kind there are, and how many are on."""

    total: int
    """All of them."""
    enabled: int
    """Those switched on."""


class CapabilitiesSummaryResult(ProtocolModel):
    """One count per Capabilities tab."""

    skills: CapabilityCount
    """Installed skills (plugin skills excluded)."""
    toolsets: CapabilityCount
    """Plain toolsets, enabled when the CLI selection offers all their tools."""
    mcp_servers: CapabilityCount
    """Configured MCP servers."""
    plugins: CapabilityCount
    """Discovered agent plugins."""


type _Row = tuple[str, type[ProtocolModel], type[ProtocolModel], tuple[ErrorCode, ...]]

CAPABILITY_METHODS: tuple[_Row, ...] = (
    ("capabilities.summary", CapabilitiesSummaryParams, CapabilitiesSummaryResult, ()),
)
"""``(name, params, result, extra errors)`` rows."""
