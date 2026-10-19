# pyright: strict
"""The per-server tool schema cache that lets a ``lazy`` server register without starting (P13-05).

``<home>/cache/mcp/<server>.json`` holds ``{version, fingerprint, written_at, tools, utilities}``:
each tool as the server listed it (``name``, ``description``, ``inputSchema``) plus its raw
``readOnlyHint``, and the utility tool kinds that were offered. It is written after every live
listing. An entry counts only for the same fingerprint (the connection-defining settings): an
edited server connects eagerly once and rewrites it. The file is 0600 (it feeds registration).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from indus.foundation.fsio import atomic_write_json
from indus.mcp.config import McpServer

__all__ = ["CACHE_VERSION", "CachedTool", "SchemaCache", "SchemaEntry", "fingerprint"]

log = logging.getLogger(__name__)

CACHE_VERSION = 1
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def fingerprint(server: McpServer) -> str:
    """A short hash of what decides which server we talk to and which tools register."""
    payload = {
        "command": server.command,
        "args": list(server.args),
        "url": server.url,
        "transport": server.transport,
        "include": sorted(server.tools.include) if server.tools.include is not None else None,
        "exclude": sorted(server.tools.exclude or []),
        "resources": server.tools.resources,
        "prompts": server.tools.prompts,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CachedTool:
    name: str
    description: str | None
    input_schema: Mapping[str, Any]
    raw_read_only: object = None

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
            "readOnlyHint": self.raw_read_only,
        }


@dataclass(frozen=True)
class SchemaEntry:
    fingerprint: str
    tools: tuple[CachedTool, ...]
    utilities: tuple[str, ...]
    written_at: float = 0.0


def _tool_of(raw: object) -> CachedTool | None:
    if not isinstance(raw, Mapping):
        return None
    data = cast(Mapping[str, Any], raw)
    name = data.get("name")
    schema = data.get("inputSchema")
    if not isinstance(name, str) or not name:
        return None
    description = data.get("description")
    return CachedTool(
        name=name,
        description=description if isinstance(description, str) else None,
        input_schema=cast(Mapping[str, Any], schema) if isinstance(schema, Mapping) else {},
        raw_read_only=data.get("readOnlyHint"),
    )


class SchemaCache:
    """Reads and writes the cache files of one home."""

    def __init__(self, home: Path) -> None:
        self.folder = home / "cache" / "mcp"

    def path(self, server: str) -> Path:
        return self.folder / f"{_UNSAFE.sub('_', server) or '_'}.json"

    def read(self, server: str, want: str) -> SchemaEntry | None:
        """The entry for ``server`` when its fingerprint is ``want``; None otherwise."""
        path = self.path(server)
        try:
            data: object = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.debug("MCP schema cache %s unreadable: %s", path, exc)
            return None
        if not isinstance(data, Mapping):
            return None
        body = cast(Mapping[str, Any], data)
        if body.get("version") != CACHE_VERSION or body.get("fingerprint") != want:
            return None
        listed = body.get("tools")
        if not isinstance(listed, list):
            return None
        tools = tuple(t for t in map(_tool_of, cast(list[object], listed)) if t is not None)
        utilities = body.get("utilities")
        kinds = (
            tuple(u for u in cast(list[object], utilities) if isinstance(u, str))
            if isinstance(utilities, list)
            else ()
        )
        written = body.get("written_at")
        return SchemaEntry(
            want, tools, kinds, float(written) if isinstance(written, int | float) else 0.0
        )

    def write(
        self,
        server: str,
        print_: str,
        tools: Sequence[CachedTool],
        utilities: Sequence[str],
        *,
        now: float,
    ) -> None:
        """Store the listing (failures are logged, never raised: the cache is an optimisation)."""
        body = {
            "version": CACHE_VERSION,
            "fingerprint": print_,
            "written_at": now,
            "tools": [t.as_json() for t in tools],
            "utilities": list(utilities),
        }
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path(server), body, mode=0o600)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not write the MCP schema cache for %s: %s", server, exc)

    def remove(self, server: str) -> None:
        try:
            self.path(server).unlink(missing_ok=True)
        except OSError as exc:
            log.debug("could not remove the MCP schema cache for %s: %s", server, exc)
