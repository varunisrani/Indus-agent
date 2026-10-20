"""The tools of an editor session, and the editor's own MCP servers (P19-13).

Selection (``selection``): ``platform_toolsets.acp`` when the user saved one (``indus tools``),
else the ``indus-acp`` bundle — files, terminal and processes, web and browser, memory, todo,
session search, skills, code execution, delegation, vision; no messaging delivery, no cron, no
clarify tool (questions reach the editor through the permission bridge). The service adds the
resolver extras on top (enabled plugin toolsets; the umbrella ``mcp`` for the profile's admitted
servers). ``session_selection`` then makes it per session: the umbrella is replaced by the
configured servers' own toolsets (none for a ``no_mcp`` launch) plus this session's editor
servers, so one session never sees another's editor tools; the launch's disabled toolsets are
removed last by the agent's resolver. ``no_mcp`` in the saved list leaves the configured servers out
(editor servers stay).

Editor servers (``EditorServers``) connect through the P13 client (``McpHost``) as
``editor-<name>`` (toolset ``mcp-editor-<name>``), so they never collide with a configured server.
Their specs (command, env, headers, URL) live in memory only.

Durable policy (``EditorToolPolicy``): what may be stored for a session is a fingerprint per
server — a hash of the spec with every env and header value hashed — and the tool schemas frozen
at the first listing. After a restart the editor must send identical specs: a server whose
fingerprint differs is refused (its tools do not run). Schemas stay frozen for the conversation: a
later listing that differs is reported as changed (a notice: the new list applies from the next
turn) but does not replace the frozen copy.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

from indus.foundation.scope import Scope

if TYPE_CHECKING:
    from indus.mcp.config import McpServer
    from indus.mcp.discovery import McpHost
    from indus.mcp.lifecycle import Opener
    from indus.tools.registry import ToolRegistry

__all__ = [
    "BUNDLE",
    "EDITOR_PREFIX",
    "MCP_UMBRELLA",
    "NO_MCP",
    "EditorServers",
    "EditorToolPolicy",
    "editor_name",
    "editor_toolset",
    "no_mcp",
    "selection",
    "server_config",
    "session_selection",
    "spec_fingerprint",
]

log = logging.getLogger(__name__)

BUNDLE: Final = "indus-acp"
EDITOR_PREFIX: Final = "editor-"
MCP_UMBRELLA: Final = "mcp"
NO_MCP: Final = "no_mcp"
_PLATFORM_KEY: Final = "platform_toolsets.acp"


def editor_name(name: str) -> str:
    """The registry name of an editor server (``editor-<name>``)."""
    return f"{EDITOR_PREFIX}{name}"


def editor_toolset(name: str) -> str:
    return f"mcp-{editor_name(name)}"


# -- selection -------------------------------------------------------------------------------------


def _saved(scope: Scope, view: Any = None) -> list[str] | None:
    config = view if view is not None else scope.config
    try:
        saved = config.get(_PLATFORM_KEY) if config is not None else None
    except Exception:
        saved = None
    if isinstance(saved, list) and saved:
        return [str(name).strip() for name in cast(list[object], saved) if str(name).strip()]
    return None


def selection(scope: Scope, view: Any = None) -> list[str]:
    """The ACP toolsets of this profile (see the module notes); ``view``: a config view to read
    instead of the scope's."""
    saved = _saved(scope, view)
    names = [name for name in saved or [] if name != NO_MCP]
    return names or [BUNDLE]


def no_mcp(scope: Scope, view: Any = None) -> bool:
    """``no_mcp`` in ``platform_toolsets.acp``: the configured MCP servers are left out."""
    return NO_MCP in (_saved(scope, view) or [])


def session_selection(
    chosen: Sequence[str] | None,
    *,
    config_servers: Iterable[str] = (),
    editor_servers: Iterable[str] = (),
    no_mcp: bool = False,
) -> list[str] | None:
    """One session's toolsets: the umbrella ``mcp`` replaced by the configured servers' toolsets
    (none with ``no_mcp``) and this session's editor servers added. ``None`` (every toolset)
    stays ``None``."""
    if chosen is None:
        return None
    names = [name for name in chosen if name != MCP_UMBRELLA]
    wanted_config = MCP_UMBRELLA in chosen and not no_mcp
    if wanted_config:
        names += [f"mcp-{name}" for name in config_servers if not name.startswith(EDITOR_PREFIX)]
    elif no_mcp:
        names = [n for n in names if not n.startswith("mcp-") or n.startswith("mcp-editor-")]
    names += [editor_toolset(name) for name in editor_servers]
    return list(dict.fromkeys(names))


# -- editor specs ----------------------------------------------------------------------------------


def _pairs(raw: object) -> dict[str, str]:
    """ACP ``[{name, value}]`` (env, headers) as a mapping."""
    out: dict[str, str] = {}
    if isinstance(raw, list):
        for item in cast(list[object], raw):
            if isinstance(item, Mapping):
                entry = cast(Mapping[str, Any], item)
                name = entry.get("name")
                if isinstance(name, str) and name:
                    out[name] = str(entry.get("value", ""))
    elif isinstance(raw, Mapping):
        out = {str(k): str(v) for k, v in cast(Mapping[object, object], raw).items()}
    return out


def server_config(spec: Mapping[str, Any]) -> McpServer:
    """An editor's MCP server spec (stdio / http / sse) as a P13 server config."""
    from indus.mcp.config import McpServer

    kind = spec.get("type")
    if kind in ("http", "sse"):
        return McpServer.model_validate(
            {
                "url": str(spec.get("url") or ""),
                "transport": kind,
                "headers": _pairs(spec.get("headers")),
            }
        )
    args = spec.get("args")
    return McpServer.model_validate(
        {
            "command": str(spec.get("command") or ""),
            "args": [str(a) for a in cast(list[object], args)] if isinstance(args, list) else [],
            "env": _pairs(spec.get("env")),
        }
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def spec_fingerprint(spec: Mapping[str, Any]) -> str:
    """A hash of the spec that holds no secret: env and header values are hashed first."""
    args = spec.get("args")
    canonical = {
        "type": str(spec.get("type") or "stdio"),
        "command": str(spec.get("command") or ""),
        "args": [str(a) for a in cast(list[object], args)] if isinstance(args, list) else [],
        "url": str(spec.get("url") or ""),
        "env": {k: _digest(v) for k, v in sorted(_pairs(spec.get("env")).items())},
        "headers": {k: _digest(v) for k, v in sorted(_pairs(spec.get("headers")).items())},
    }
    return _digest(json.dumps(canonical, sort_keys=True))


@dataclass
class EditorToolPolicy:
    """The durable part of an editor's MCP choice (see the module notes)."""

    fingerprints: dict[str, str] = field(default_factory=dict[str, str])
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])

    @classmethod
    def of(cls, specs: Sequence[Mapping[str, Any]]) -> EditorToolPolicy:
        policy = cls()
        for spec in specs:
            policy.fingerprints[str(spec.get("name", ""))] = spec_fingerprint(spec)
        return policy

    def record(self) -> dict[str, Any]:
        """What may be stored (no command line secrets, no env or header value)."""
        return {"fingerprints": dict(self.fingerprints), "schemas": dict(self.schemas)}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> EditorToolPolicy:
        prints = record.get("fingerprints")
        schemas = record.get("schemas")
        return cls(
            {str(k): str(v) for k, v in cast(Mapping[str, Any], prints).items()}
            if isinstance(prints, Mapping)
            else {},
            {
                str(k): dict(cast(Mapping[str, Any], v))
                for k, v in cast(Mapping[str, Any], schemas).items()
            }
            if isinstance(schemas, Mapping)
            else {},
        )

    def refused(self, specs: Sequence[Mapping[str, Any]]) -> list[str]:
        """Servers whose resent spec differs from the recorded one (their tools must not run)."""
        out: list[str] = []
        for spec in specs:
            name = str(spec.get("name", ""))
            known = self.fingerprints.get(name)
            if known is not None and known != spec_fingerprint(spec):
                out.append(name)
        return out

    def freeze(self, server: str, tools: Mapping[str, Any]) -> bool:
        """Keep ``tools`` (name → schema) as the server's frozen listing on first sight; True
        when a later listing differs from the frozen one (the frozen copy stays)."""
        frozen = self.schemas.get(server)
        if frozen is None:
            self.schemas[server] = dict(tools)
            return False
        return frozen != dict(tools)


# -- connecting editor servers ---------------------------------------------------------------------


class EditorServers:
    """The editor's MCP servers of one ACP process, through the P13 client."""

    def __init__(
        self,
        scope: Scope,
        registry: ToolRegistry,
        *,
        opener: Opener | None = None,
    ) -> None:
        self.scope = scope
        self.registry = registry
        self.opener = opener
        self.policy = EditorToolPolicy()
        self._host: McpHost | None = None
        self._configs: dict[str, McpServer] = {}
        self._prints: dict[str, str] = {}

    def _built(self) -> McpHost:
        if self._host is None:
            from indus.mcp.discovery import McpHost

            if self.opener is not None:
                self._host = McpHost(self.scope, self.registry, opener=self.opener)
            else:
                self._host = McpHost(self.scope, self.registry)
        return self._host

    async def attach(self, specs: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
        """Connect the servers of ``specs`` (a changed spec restarts its server). Returns
        ``(connected editor names, names whose listing changed after it was frozen)``."""
        from indus.mcp.config import ServerTable

        host = self._built()
        wanted: dict[str, McpServer] = {}
        for spec in specs:
            name = str(spec.get("name", "")).strip()
            if not name:
                continue
            fingerprint = spec_fingerprint(spec)
            key = editor_name(name)
            if self._prints.get(key) not in (None, fingerprint):
                await host.remove(key)
            self._prints[key] = fingerprint
            wanted[key] = server_config(spec)
        self._configs.update(wanted)
        report = await host.discover(ServerTable(servers=dict(self._configs)))
        changed: list[str] = []
        connected: list[str] = []
        for key in wanted:
            handle = host.handles.get(key)
            if handle is None or not handle.specs:
                continue
            connected.append(key[len(EDITOR_PREFIX) :])
            tools = {}
            for spec_name in sorted(handle.specs):
                entry = self.registry.get(spec_name, self.scope)
                tools[spec_name] = dict(entry.schema) if entry is not None else {}
            if self.policy.freeze(key, tools):
                changed.append(key[len(EDITOR_PREFIX) :])
        for name, error in report.failed.items():
            log.warning("editor MCP server %s did not connect: %s", name, error)
        return connected, changed

    def tool_names(self, name: str) -> list[str]:
        handle = self._host.handles.get(editor_name(name)) if self._host is not None else None
        return sorted(handle.specs) if handle is not None else []

    async def aclose(self) -> None:
        host, self._host = self._host, None
        if host is not None:
            await host.close()
