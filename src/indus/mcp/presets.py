# pyright: strict
"""Common MCP server shapes (ours): ``indus mcp add --preset`` and the dashboard's catalogue.

Each preset is the ``mcp_servers.<name>`` entry it writes (``command`` and ``args``, stdio) plus a
one-line ``description`` for a catalogue row; ``entry`` gives a fresh copy of the entry alone.
Placeholders such as ``${workspaceFolder}`` are left for the connect to fill (``mcp.config``).
The list is short on purpose: a server is offered here only when it needs no key to start.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

__all__ = ["DESCRIPTIONS", "PRESETS", "entry"]

PRESETS: Mapping[str, Mapping[str, Any]] = {
    "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "${workspaceFolder}"],
    },
    "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
    "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
    "git": {"command": "uvx", "args": ["mcp-server-git"]},
}
"""Preset name → the server entry it writes."""

DESCRIPTIONS: Mapping[str, str] = {
    "filesystem": "Read and write files in the workspace folder.",
    "memory": "A small knowledge graph the agent keeps between sessions.",
    "fetch": "Fetch a web page and hand it over as text.",
    "git": "Work in a git repository: status, log, diffs, staging and commits.",
}
"""Preset name → one line for the catalogue."""


def entry(name: str) -> dict[str, Any]:
    """A fresh copy of the ``mcp_servers`` entry of preset ``name`` (``KeyError`` if none)."""
    return {
        k: (list(cast(list[object], v)) if isinstance(v, list) else v)
        for k, v in PRESETS[name].items()
    }
