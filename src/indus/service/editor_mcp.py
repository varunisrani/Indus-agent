# pyright: strict
"""An editor's own MCP servers on a daemon-hosted session (P19-13, the daemon half of D-16.602).

An ACP editor names MCP servers for its session (stdio commands, or HTTP / SSE URLs). The daemon
runs them for that session alone:

- **Specs stay in memory.** Commands, arguments, env and headers are kept by ``EditorServers`` in
  the daemon's memory only. Nothing of them is logged; a refusal never echoes one.
- **The record is durable.** ``EditorRecord`` is what the session row keeps
  (``model_config["editor_policy"]``): a fingerprint per server name — a hash of the spec with every
  env and header value hashed first — and the tool schemas of the server's first listing. After a
  daemon restart the editor must send the same specs again: a server whose fingerprint differs is
  refused (``EditorRecord.refused``); a server not in the record joins it. A later listing that
  differs from the frozen schemas is reported as changed; the frozen copy stays.
- **One session only.** ``EditorServers`` connects through the P13 client (``McpHost``) into a
  ``SessionRegistry`` of its own, as ``editor-<name>`` (toolset ``mcp-editor-<name>``). The agent of
  that session gets this registry; every other session keeps reading the shared one, so the tools
  never reach it — not even through a selection of every toolset. stdio servers run in the
  session's folder; a move restarts them there.

``acp_selection`` is the toolset choice of an ACP session the client gave none for:
``platform_toolsets.acp`` (without its ``no_mcp`` marker) or the ``indus-acp`` bundle.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

from indus.foundation.scope import Scope
from indus.state import sessions
from indus.state.registry import StoreHandle
from indus.tools.registry import ToolRegistry
from indus.tools.session_registry import SessionRegistry

if TYPE_CHECKING:
    from indus.mcp.config import McpServer
    from indus.mcp.discovery import McpHost

__all__ = [
    "ACP_BUNDLE",
    "EDITOR_PREFIX",
    "MAX_LIST_BYTES",
    "MAX_SERVERS",
    "RECORD_KEY",
    "EditorRecord",
    "EditorServers",
    "SpecError",
    "acp_selection",
    "editor_toolset",
    "load_record",
    "save_record",
    "spec_fingerprint",
    "valid_specs",
]

log = logging.getLogger(__name__)

EDITOR_PREFIX: Final = "editor-"
RECORD_KEY: Final = "editor_policy"
"""The ``model_config`` key of a session's ``EditorRecord``."""
MAX_SERVERS: Final = 16
MAX_LIST_BYTES: Final = 65536
"""The largest server list (as compact JSON) an editor may send."""
ACP_BUNDLE: Final = "indus-acp"
_NO_MCP: Final = "no_mcp"
_KINDS: Final = frozenset({"stdio", "http", "sse"})


class SpecError(ValueError):
    """An editor's server list that cannot be used (the text names no spec value)."""


def editor_toolset(name: str) -> str:
    """The toolset of editor server ``name`` (``mcp-editor-<name>``)."""
    return f"mcp-{EDITOR_PREFIX}{name}"


def acp_selection(scope: Scope) -> list[str]:
    """The toolsets of an ACP session the client named none for (see the module notes)."""
    config = scope.config
    try:
        saved = config.get("platform_toolsets.acp") if config is not None else None
    except Exception:
        saved = None
    if isinstance(saved, list):
        names = [str(n).strip() for n in cast(list[object], saved)]
        chosen = [n for n in names if n and n != _NO_MCP]
        if chosen:
            return chosen
    return [ACP_BUNDLE]


# -- specs -----------------------------------------------------------------------------------------


def _pairs(raw: object) -> dict[str, str]:
    """ACP ``[{name, value}]`` (env, headers), or a plain mapping, as a mapping."""
    out: dict[str, str] = {}
    if isinstance(raw, list):
        for item in cast(list[object], raw):
            if isinstance(item, Mapping):
                entry = cast(Mapping[str, object], item)
                name = entry.get("name")
                if isinstance(name, str) and name:
                    value = entry.get("value")
                    out[name] = "" if value is None else str(value)
    elif isinstance(raw, Mapping):
        for key, value in cast(Mapping[object, object], raw).items():
            out[str(key)] = "" if value is None else str(value)
    return out


def _kind(spec: Mapping[str, Any]) -> str:
    kind = spec.get("type")
    return str(kind) if isinstance(kind, str) and kind else "stdio"


def valid_specs(raw: object) -> list[dict[str, Any]]:
    """The editor's server list, checked: a list of at most ``MAX_SERVERS`` objects, each named,
    stdio with a command or http / sse with a URL. A name given twice keeps the later spec."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpecError("mcp_servers must be a list")
    items = cast(list[object], raw)
    if len(items) > MAX_SERVERS:
        raise SpecError(f"at most {MAX_SERVERS} editor MCP servers")
    try:
        size = len(json.dumps(items, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        raise SpecError("mcp_servers must be plain JSON") from None
    if size > MAX_LIST_BYTES:
        raise SpecError(f"the server list is larger than {MAX_LIST_BYTES} bytes")
    chosen: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise SpecError(f"server {index} is not an object")
        spec = dict(cast(Mapping[str, Any], item))
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            raise SpecError(f"server {index} has no usable name")
        kind = _kind(spec)
        if kind not in _KINDS:
            raise SpecError(f"server {name.strip()!r} has an unknown type")
        target = spec.get("url") if kind in ("http", "sse") else spec.get("command")
        if not isinstance(target, str) or not target.strip():
            what = "a URL" if kind in ("http", "sse") else "a command"
            raise SpecError(f"server {name.strip()!r} needs {what}")
        spec["name"] = name.strip()
        chosen.pop(spec["name"], None)
        chosen[spec["name"]] = spec
    return list(chosen.values())


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spec_fingerprint(spec: Mapping[str, Any]) -> str:
    """A hash of the spec that holds no secret: env and header values are hashed first."""
    args = spec.get("args")
    canonical = {
        "type": _kind(spec),
        "command": str(spec.get("command") or ""),
        "args": [str(a) for a in cast(list[object], args)] if isinstance(args, list) else [],
        "url": str(spec.get("url") or ""),
        "env": {k: _digest(v) for k, v in sorted(_pairs(spec.get("env")).items())},
        "headers": {k: _digest(v) for k, v in sorted(_pairs(spec.get("headers")).items())},
    }
    return _digest(json.dumps(canonical, sort_keys=True, separators=(",", ":")))


def server_config(spec: Mapping[str, Any], cwd: str | None) -> McpServer:
    """An editor's spec as a P13 server config (stdio servers run in ``cwd``)."""
    from indus.mcp.config import McpServer

    kind = _kind(spec)
    if kind in ("http", "sse"):
        return McpServer.model_validate(
            {"url": str(spec["url"]), "transport": kind, "headers": _pairs(spec.get("headers"))}
        )
    args = spec.get("args")
    return McpServer.model_validate(
        {
            "command": str(spec["command"]),
            "args": [str(a) for a in cast(list[object], args)] if isinstance(args, list) else [],
            "env": _pairs(spec.get("env")),
            "cwd": cwd or None,
        }
    )


# -- the durable record ----------------------------------------------------------------------------


@dataclass
class EditorRecord:
    """What a session row keeps of its editor's servers (see the module notes)."""

    fingerprints: dict[str, str] = field(default_factory=dict[str, str])
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])

    def refused(self, specs: Sequence[Mapping[str, Any]]) -> list[str]:
        """Names whose spec differs from the recorded one."""
        out: list[str] = []
        for spec in specs:
            name = str(spec.get("name", ""))
            known = self.fingerprints.get(name)
            if known is not None and known != spec_fingerprint(spec):
                out.append(name)
        return out

    def admit(self, specs: Sequence[Mapping[str, Any]]) -> bool:
        """Record servers not seen before; True when the record changed."""
        changed = False
        for spec in specs:
            name = str(spec.get("name", ""))
            if name and name not in self.fingerprints:
                self.fingerprints[name] = spec_fingerprint(spec)
                changed = True
        return changed

    def freeze(self, name: str, tools: Mapping[str, Any]) -> tuple[bool, bool]:
        """``(recorded now, differs)``: the first listing of ``name`` is kept; a later one that
        differs is reported, the frozen copy stays."""
        frozen = self.schemas.get(name)
        if frozen is None:
            self.schemas[name] = dict(tools)
            return True, False
        return False, frozen != dict(tools)

    def to_record(self) -> dict[str, Any]:
        return {"fingerprints": dict(self.fingerprints), "schemas": dict(self.schemas)}

    @classmethod
    def from_record(cls, raw: object) -> EditorRecord:
        if not isinstance(raw, Mapping):
            return cls()
        record = cast(Mapping[str, object], raw)
        prints = record.get("fingerprints")
        schemas = record.get("schemas")
        found = cls()
        if isinstance(prints, Mapping):
            for key, value in cast(Mapping[object, object], prints).items():
                found.fingerprints[str(key)] = str(value)
        if isinstance(schemas, Mapping):
            for key, value in cast(Mapping[object, object], schemas).items():
                if isinstance(value, Mapping):
                    found.schemas[str(key)] = dict(cast(Mapping[str, Any], value))
        return found


def load_record(store: StoreHandle, session_id: str) -> EditorRecord | None:
    """The session's record, or None when it has none (blocking)."""
    row = sessions.get(store, session_id)
    if row is None:
        return None
    raw = sessions.parse_model_config(row.get("model_config")).get(RECORD_KEY)
    return EditorRecord.from_record(raw) if raw is not None else None


def save_record(store: StoreHandle, session_id: str, record: EditorRecord, *, source: str) -> None:
    """Write the record into the session row, creating the row when the first reply has not
    (blocking)."""
    now = store.scope.clock.now().timestamp()
    profile = store.scope.profile_id
    patch = {RECORD_KEY: record.to_record()}

    def write(conn: Any) -> None:
        sessions.create_in(conn, session_id, source, now=now, profile_id=profile)
        sessions.patch_model_config_in(conn, session_id, patch)

    store.write(write)


# -- the servers of one session --------------------------------------------------------------------


class EditorServers:
    """One session's editor MCP servers (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        base: ToolRegistry | None,
        specs: Sequence[Mapping[str, Any]],
        *,
        opener: Any = None,
    ) -> None:
        self.scope = scope
        self.registry = SessionRegistry(base if base is not None else _builtin_registry(scope))
        self.opener = opener
        self.specs: dict[str, dict[str, Any]] = {}
        self.listings: dict[str, dict[str, Any]] = {}
        """The tool schemas of each connected server's latest listing, by editor name."""
        self._host: McpHost | None = None
        self._prints: dict[str, str] = {}
        self._cwd: str | None = None
        self.replace(specs)

    @property
    def names(self) -> list[str]:
        return list(self.specs)

    def spec_list(self) -> list[dict[str, Any]]:
        return [dict(spec) for spec in self.specs.values()]

    def same(self, specs: Sequence[Mapping[str, Any]]) -> bool:
        """True when ``specs`` are exactly the servers held now."""
        wanted = {str(s.get("name", "")): spec_fingerprint(s) for s in specs}
        return wanted == {name: spec_fingerprint(s) for name, s in self.specs.items()}

    def replace(self, specs: Sequence[Mapping[str, Any]]) -> None:
        """Hold ``specs`` from now on (they connect at the next ``connect``)."""
        self.specs = {str(s["name"]): dict(s) for s in specs}

    def toolsets(self) -> list[str]:
        """The toolsets of the servers whose tools arrived."""
        return [editor_toolset(name) for name in self.connected()]

    def connected(self) -> list[str]:
        host = self._host
        if host is None:
            return []
        out: list[str] = []
        for name in self.specs:
            handle = host.handles.get(EDITOR_PREFIX + name)
            if handle is not None and handle.specs:
                out.append(name)
        return out

    def _built(self) -> McpHost:
        if self._host is None:
            from indus.mcp import discovery, extras

            opener = self.opener if self.opener is not None else extras.opener()
            self._host = discovery.McpHost(self.scope, self.registry, opener=opener)
        return self._host

    async def connect(self, cwd: str | None) -> list[str]:
        """Connect every held server not running yet (a changed spec or folder restarts it, a
        dropped one is closed); the editor names whose tools arrived."""
        from indus.mcp.config import ServerTable

        if not self.specs and self._host is None:
            return []
        host = self._built()
        moved = cwd != self._cwd
        self._cwd = cwd
        wanted: dict[str, McpServer] = {}
        for name, spec in self.specs.items():
            key = EDITOR_PREFIX + name
            fingerprint = spec_fingerprint(spec)
            if key in host.handles and (moved or self._prints.get(key) != fingerprint):
                await host.remove(key)
            self._prints[key] = fingerprint
            wanted[key] = server_config(spec, cwd)
        for key in [k for k in host.handles if k not in wanted]:
            await host.remove(key)
            self._prints.pop(key, None)
        report = await host.discover(ServerTable(servers=wanted))
        for key in report.failed:
            log.warning("editor MCP server %s did not connect", key[len(EDITOR_PREFIX) :])
        self.listings = {}
        for name in self.connected():
            handle = host.handles[EDITOR_PREFIX + name]
            self.listings[name] = {n: dict(s) for n, s in sorted(handle.schemas.items())}
        return self.connected()

    async def aclose(self) -> None:
        host, self._host = self._host, None
        if host is not None:
            try:
                await host.close()
            except Exception:
                log.warning("editor MCP servers did not close cleanly", exc_info=True)


def _builtin_registry(scope: Scope) -> ToolRegistry:
    """The registry an agent without one builds itself (the built-in tools)."""
    from indus.tools.builtin import load_builtins

    registry = ToolRegistry(clock=scope.clock)
    load_builtins(registry)
    return registry
