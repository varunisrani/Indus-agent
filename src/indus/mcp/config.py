# pyright: strict
"""MCP server settings: the typed ``mcp_servers.<name>`` entry and what a connect needs from it.

``load_servers`` turns the config mapping into ``McpServer`` models; a broken entry becomes a
problem line and the rest still load. Placeholders (``${NAME}``, ``${env:NAME}`` and the context
variables ``${userHome}``, ``${workspaceFolder}``, ``${workspaceFolderBasename}``,
``${pathSeparator}``, ``${/}``) stay in the model and are filled in only when a connection is
built (``stdio_launch`` / ``remote_endpoint``), from the scope's secret store first, then its env
view. Context variable names are matched exactly (case-sensitive).

A stdio server's environment comes from ``safety.child_env`` (``minimal`` variant) plus the
``XDG_*`` names and the Windows location names of the scope's env, then the server's own ``env``:
never the whole parent environment (R07.22).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from indus.foundation.errors import IndusError
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.safety.child_env import child_env

__all__ = [
    "CALL_TIMEOUT_S",
    "CONNECT_TIMEOUT_S",
    "ElicitationSettings",
    "IdentityHeader",
    "McpClientCertMissing",
    "McpConfigInvalid",
    "McpError",
    "McpServer",
    "McpSettings",
    "McpUnresolvedRef",
    "OAuthSettings",
    "RemoteEndpoint",
    "SamplingSettings",
    "ServerTable",
    "StdioLaunch",
    "ToolFilter",
    "Interpolator",
    "load_servers",
    "load_settings",
    "parse_server",
    "remote_endpoint",
    "stdio_launch",
]

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 300.0
CONNECT_TIMEOUT_S = 60.0

TrustLevel = Literal["full", "untrusted"]
ServerKind = Literal["stdio", "http", "sse"]

# Windows names a launcher-style server needs to find its own files (matched without case),
# grouped by purpose: the user's dirs, program dirs, the system, temp space.
_WINDOWS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN"),
    ("PUBLIC", "ALLUSERSPROFILE", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"),
    ("PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432"),
    ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "OS", "COMPUTERNAME"),
    ("PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"),
    ("TEMP", "TMP"),
)
WINDOWS_BASELINE: frozenset[str] = frozenset(n for group in _WINDOWS_GROUPS for n in group)
_SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_REF = re.compile(r"\$\{([^{}]*)\}")


# -- errors -------------------------------------------------------------------------------------


class McpError(IndusError):
    default_code = "mcp.error"

    def __init__(self, message: str, *, server: str = "", hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.server = server


class McpConfigInvalid(McpError):
    default_code = "mcp.config_invalid"


class McpUnresolvedRef(McpError):
    """A remote server's URL or headers still name an unset variable: sending the literal text
    would only earn a confusing 401, so the connect is refused before any network."""

    default_code = "mcp.unresolved_ref"

    def __init__(self, message: str, *, server: str = "", names: Sequence[str] = ()) -> None:
        super().__init__(message, server=server)
        self.names: tuple[str, ...] = tuple(names)


class McpClientCertMissing(McpError):
    default_code = "mcp.client_cert_missing"


# -- the models ---------------------------------------------------------------------------------


def _warn(info_context: object, text: str) -> None:
    if isinstance(info_context, dict):
        bucket = cast(dict[str, Any], info_context).get("warnings")
        if isinstance(bucket, list):
            cast(list[str], bucket).append(text)
            return
    log.warning("%s", text)


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class OAuthSettings(_Model):
    """How the client identifies itself to an OAuth server (P13-09 drives the flow)."""

    client_id: str | None = None
    client_secret_ref: str | None = Field(
        default=None, description="name of the secret holding the client secret, never the value"
    )
    client_name: str | None = None
    redirect_host: str | None = None
    redirect_port: int | None = Field(default=None, ge=0, le=65535)
    timeout: float = Field(default=300.0, gt=0, description="seconds an interactive login waits")

    @field_validator("client_secret_ref")
    @classmethod
    def _secret_by_name(cls, value: str | None) -> str | None:
        if value is not None and (not _SECRET_NAME.match(value) or redact_text(value) != value):
            raise ValueError("client_secret_ref must name a stored secret, not hold its value")
        return value


class IdentityHeader(_Model):
    """A header that tells the server who is calling: a fixed value or the profile's name."""

    name: str
    value_from: Literal["static", "profile"] = "static"
    value: str | None = None

    @model_validator(mode="after")
    def _complete(self) -> IdentityHeader:
        if not _HEADER_NAME.match(self.name):
            raise ValueError(f"{self.name!r} is not a valid header name")
        if self.value_from == "static" and not (self.value or "").strip():
            raise ValueError("a static identity header needs a value")
        return self


class ToolFilter(_Model):
    """Which server tools are registered; ``include`` (even empty) wins over ``exclude``."""

    include: list[str] | None = None
    exclude: list[str] | None = None
    resources: bool = True
    prompts: bool = True

    @field_validator("include", "exclude", mode="before")
    @classmethod
    def _one_or_many(cls, value: object, info: Any) -> object:
        if value is None or isinstance(value, list | tuple):
            return None if value is None else [str(v) for v in cast(Sequence[object], value)]
        if isinstance(value, str):
            return [value]
        _warn(info.context, f"tools.{info.field_name} must be a name or a list of names; ignored")
        return None


class SamplingSettings(_Model):
    """Server-initiated completions (P13-10 enforces these)."""

    enabled: bool = True
    model: str | None = None
    max_tokens_cap: int = Field(default=4096, gt=0)
    timeout: float = Field(default=30.0, gt=0)
    max_rpm: int = Field(default=10, ge=0)
    max_tool_rounds: int = Field(default=5, ge=0)
    allowed_models: list[str] = Field(default_factory=list[str])
    log_level: str = "info"


class ElicitationSettings(_Model):
    """Server-initiated questions to the user (P13-10)."""

    enabled: bool = True
    timeout: float = Field(default=300.0, gt=0)


class McpServer(_Model):
    """One ``mcp_servers.<name>`` entry."""

    command: str | None = None
    args: list[str] = Field(default_factory=list[str])
    env: dict[str, str] = Field(default_factory=dict[str, str])
    cwd: str | None = None
    url: str | None = None
    transport: Literal["http", "sse"] | None = None
    headers: dict[str, str] = Field(default_factory=dict[str, str])
    auth: Literal["oauth"] | None = None
    oauth: OAuthSettings | None = None
    client_cert: str | list[str] | None = None
    client_key: str | None = None
    identity_header: IdentityHeader | None = None
    timeout: float = Field(
        default=CALL_TIMEOUT_S, gt=0, description="seconds one tool call may take"
    )
    connect_timeout: float = Field(default=CONNECT_TIMEOUT_S, gt=0)
    lazy: bool = False
    idle_timeout_seconds: float = Field(default=0.0, ge=0, description="stdio recycle; 0 = never")
    max_lifetime_seconds: float = Field(default=0.0, ge=0, description="stdio recycle; 0 = never")
    enabled: bool = True
    supports_parallel_tool_calls: bool = False
    tools: ToolFilter = Field(default_factory=ToolFilter)
    trust: TrustLevel = "full"
    sampling: SamplingSettings = Field(default_factory=SamplingSettings)
    elicitation: ElicitationSettings = Field(default_factory=ElicitationSettings)

    @field_validator("args", mode="before")
    @classmethod
    def _args_as_text(cls, value: object) -> object:
        if isinstance(value, list | tuple):
            return [str(v) for v in cast(Sequence[object], value)]
        return value

    @field_validator("env", "headers", mode="before")
    @classmethod
    def _values_as_text(cls, value: object) -> object:
        if value is None:
            return {}
        if isinstance(value, Mapping):
            pairs = cast(Mapping[object, object], value).items()
            return {str(k): "" if v is None else str(v) for k, v in pairs}
        return value

    @field_validator("idle_timeout_seconds", "max_lifetime_seconds", mode="before")
    @classmethod
    def _limit_or_off(cls, value: object, info: Any) -> object:
        """A recycle limit that is not a non-negative number is ignored (off), not fatal."""
        if value is None:
            return 0.0
        try:
            seconds = float(cast(Any, value))
        except (TypeError, ValueError):
            seconds = -1.0
        if seconds < 0 or isinstance(value, bool):
            _warn(info.context, f"{info.field_name} must be a number of seconds >= 0; ignored")
            return 0.0
        return seconds

    @field_validator("trust", mode="before")
    @classmethod
    def _trust_tier(cls, value: object, info: Any) -> str:
        if value is None:
            return "full"
        text = str(value).strip().lower()
        if text in ("full", "untrusted"):
            return text
        _warn(info.context, f"trust value {value!r} is not 'full' or 'untrusted'; using untrusted")
        return "untrusted"

    @field_validator("identity_header", mode="before")
    @classmethod
    def _identity_or_nothing(cls, value: object, info: Any) -> object:
        if value is None:
            return None
        try:
            return IdentityHeader.model_validate(value)
        except ValidationError as exc:
            first = exc.errors()[0]["msg"] if exc.errors() else "invalid"
            _warn(info.context, f"identity_header ignored: {first}")
            return None

    @field_validator("tools", "sampling", "elicitation", mode="before")
    @classmethod
    def _null_is_default(cls, value: object) -> object:
        return {} if value is None else value

    @model_validator(mode="after")
    def _shape(self, info: Any) -> McpServer:
        if not self.command and not self.url:
            raise ValueError("needs either command (stdio) or url (http, sse)")
        if self.command and self.url:
            _warn(info.context, "has both command and url; the url is used")
        if isinstance(self.client_cert, list):
            if self.client_key is not None:
                raise ValueError("give client_cert as [cert, key] or client_cert + client_key")
            if len(self.client_cert) not in (2, 3):
                raise ValueError("client_cert as a list needs [cert, key] or [cert, key, password]")
        elif self.client_key is not None and self.client_cert is None:
            raise ValueError("client_key needs client_cert")
        return self

    @property
    def kind(self) -> ServerKind:
        if self.url:
            return "sse" if self.transport == "sse" else "http"
        return "stdio"

    @property
    def is_remote(self) -> bool:
        return self.kind != "stdio"


class McpSettings(_Model):
    """The ``mcp`` section."""

    auto_reload_on_config_change: bool = True
    discovery_concurrency: int = Field(default=4, ge=0)


@dataclass(frozen=True)
class ServerTable:
    """Every configured server that parsed, and why the others did not."""

    servers: Mapping[str, McpServer] = field(default_factory=dict[str, McpServer])
    problems: Mapping[str, str] = field(default_factory=dict[str, str])
    warnings: tuple[str, ...] = ()


# -- loading ------------------------------------------------------------------------------------

_space_lock = threading.Lock()
_space_warned: set[tuple[str, str]] = set()


def _padded_paths(value: object, path: str = "") -> list[str]:
    if isinstance(value, str):
        return [path] if value != value.strip() else []
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in cast(Mapping[object, object], value).items():
            found += _padded_paths(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for i, child in enumerate(cast(list[object], value)):
            found += _padded_paths(child, f"{path}[{i}]")
    return found


def _warn_padding(name: str, raw: Mapping[str, Any]) -> None:
    """One warning per (server, key path) for a value with stray leading/trailing whitespace
    (a pasted newline makes auth fail mysteriously). The value itself is kept and never logged."""
    for path in _padded_paths(raw):
        with _space_lock:
            if (name, path) in _space_warned:
                continue
            _space_warned.add((name, path))
        log.warning(
            "MCP server %r: %s starts or ends with whitespace; check the config for a stray "
            "space or newline",
            name,
            path,
        )


def parse_server(name: str, raw: object) -> tuple[McpServer | None, list[str], str | None]:
    """``(server, warnings, problem)`` for one entry; ``server`` is None when ``problem`` is set."""
    warnings: list[str] = []
    if not isinstance(raw, Mapping):
        return None, warnings, "the entry must be a mapping of settings"
    data = dict(cast(Mapping[str, Any], raw))
    _warn_padding(name, data)
    try:
        server = McpServer.model_validate(data, context={"warnings": warnings})
    except ValidationError as exc:
        parts: list[str] = []
        for err in exc.errors():
            where = ".".join(str(p) for p in err["loc"])
            parts.append(f"{where}: {err['msg']}" if where else str(err["msg"]))
        return None, warnings, "; ".join(parts) or "invalid entry"
    for extra in sorted(server.model_extra or {}):
        warnings.append(f"{extra}: unknown setting (kept)")
    return server, warnings, None


def load_servers(scope: Scope) -> ServerTable:
    """The scope's ``mcp_servers`` as models. Broken entries are left out with a reason."""
    raw = scope.config.get("mcp_servers") if scope.config is not None else None
    if raw is None:
        return ServerTable()
    if not isinstance(raw, Mapping):
        log.warning("mcp_servers must be a mapping of server names; ignored")
        return ServerTable(problems={"": "mcp_servers is not a mapping"})
    servers: dict[str, McpServer] = {}
    problems: dict[str, str] = {}
    notes: list[str] = []
    for key, entry in cast(Mapping[object, object], raw).items():
        name = str(key)
        server, warnings, problem = parse_server(name, entry)
        for text in warnings:
            log.warning("MCP server %r: %s", name, text)
            notes.append(f"{name}: {text}")
        if problem is not None or server is None:
            log.warning("MCP server %r skipped: %s", name, problem)
            problems[name] = problem or "invalid entry"
            continue
        servers[name] = server
    return ServerTable(servers, problems, tuple(notes))


def load_settings(scope: Scope) -> McpSettings:
    raw = scope.config.get("mcp") if scope.config is not None else None
    if not isinstance(raw, Mapping):
        return McpSettings()
    try:
        return McpSettings.model_validate(dict(cast(Mapping[str, Any], raw)))
    except ValidationError as exc:
        log.warning("mcp settings ignored: %s", exc.errors()[0]["msg"] if exc.errors() else exc)
        return McpSettings()


# -- placeholders -------------------------------------------------------------------------------


class Interpolator:
    """Fills ``${...}`` references for one connect; remembers the names it could not fill."""

    CONTEXT_NAMES = ("userHome", "workspaceFolder", "workspaceFolderBasename", "pathSeparator", "/")

    def __init__(self, scope: Scope, workspace: Path | None = None) -> None:
        self._scope = scope
        self._workspace = workspace
        self.unresolved: list[str] = []

    def _home(self) -> str:
        for name in ("HOME", "USERPROFILE"):
            value = self._scope.env.get(name)
            if value:
                return value
        return str(Path.home())

    def _folder(self) -> str:
        return str(self._workspace if self._workspace is not None else Path.cwd())

    def _context(self, body: str) -> str | None:
        if body == "userHome":
            return self._home()
        if body == "workspaceFolder":
            return self._folder()
        if body == "workspaceFolderBasename":
            folder = self._folder().rstrip("/\\")
            return os.path.basename(folder) or folder
        if body in ("pathSeparator", "/"):
            return os.sep
        return None

    def lookup(self, name: str) -> str | None:
        """A variable's value for this scope: its secret store first, then its env view."""
        secrets = self._scope.secrets
        if secrets is not None:
            try:
                value = secrets.get(name)
            except Exception as exc:  # a broken store must not take the connect down
                log.debug("secret lookup for %s failed: %s", name, type(exc).__name__)
                value = None
            if value is not None:
                return value
        return self._scope.env.get(name)

    def render(self, text: str) -> str:
        def fill(match: re.Match[str]) -> str:
            body = match.group(1).strip()
            context = self._context(body)
            if context is not None:
                return context
            name = body[4:].strip() if body.startswith("env:") else body
            value = self.lookup(name) if name else None
            if value is None:
                if name not in self.unresolved:
                    self.unresolved.append(name or match.group(0))
                return match.group(0)
            return value

        return _REF.sub(fill, text)


# -- what a connect needs -----------------------------------------------------------------------


@dataclass(frozen=True)
class StdioLaunch:
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path | None

    def __repr__(self) -> str:  # values may be secrets
        return f"StdioLaunch({self.command!r}, {len(self.args)} args, env={sorted(self.env)})"


ClientCert = str | tuple[str, str] | tuple[str, str, str]


@dataclass(frozen=True)
class RemoteEndpoint:
    url: str
    headers: Mapping[str, str]
    transport: Literal["http", "sse"]
    cert: ClientCert | None = None

    def __repr__(self) -> str:  # header values may be secrets
        return f"RemoteEndpoint({self.transport}, headers={sorted(self.headers)})"


_identity_warned: set[str] = set()


def _baseline_names(scope: Scope) -> dict[str, str]:
    return {
        key: value
        for key, value in scope.env.items()
        if key.startswith("XDG_") or key.upper() in WINDOWS_BASELINE
    }


def stdio_launch(
    name: str, server: McpServer, scope: Scope, *, workspace: Path | None = None
) -> StdioLaunch:
    """The command, args, filtered env and cwd for a stdio server, placeholders filled.

    An unset variable stays literal: the server itself says what it misses.
    """
    if not server.command:
        raise McpConfigInvalid(f"MCP server {name!r} has no command", server=name)
    if server.identity_header is not None:
        with _space_lock:
            first = name not in _identity_warned
            _identity_warned.add(name)
        if first:
            log.warning("MCP server %r: identity_header only applies to http/sse; ignored", name)
    fill = Interpolator(scope, workspace)
    configured = {key: fill.render(value) for key, value in server.env.items()}
    env = child_env(scope, variant="minimal", extra={**_baseline_names(scope), **configured})
    cwd: Path | None = workspace
    if server.cwd:
        cwd = Path(fill.render(server.cwd)).expanduser()
    if fill.unresolved:
        log.info("MCP server %r: unset variables left as written: %s", name, fill.unresolved)
    return StdioLaunch(
        command=fill.render(server.command),
        args=tuple(fill.render(a) for a in server.args),
        env=env,
        cwd=cwd,
    )


def _existing(name: str, label: str, raw: str, fill: Interpolator) -> str:
    text = fill.render(raw).strip()
    if not text:
        raise McpConfigInvalid(f"MCP server {name!r}: {label} is empty", server=name)
    path = Path(text).expanduser()
    if not path.is_file():
        raise McpClientCertMissing(
            f"MCP server {name!r}: {label} file not found: {path}",
            server=name,
            hint=f"fix mcp_servers.{name}.{label.split('[')[0]} or create the file",
        )
    return str(path)


def _client_cert(name: str, server: McpServer, fill: Interpolator) -> ClientCert | None:
    cert = server.client_cert
    if cert is None:
        return None
    if isinstance(cert, str):
        cert_path = _existing(name, "client_cert", cert, fill)
        if server.client_key is None:
            return cert_path
        return (cert_path, _existing(name, "client_key", server.client_key, fill))
    pair = (
        _existing(name, "client_cert[0]", cert[0], fill),
        _existing(name, "client_cert[1]", cert[1], fill),
    )
    if len(cert) == 3:
        return (pair[0], pair[1], fill.render(cert[2]))
    return pair


def remote_endpoint(
    name: str, server: McpServer, scope: Scope, *, workspace: Path | None = None
) -> RemoteEndpoint:
    """URL, headers (identity header merged, explicit header of that name wins) and the client
    certificate for an http/sse server. An unset variable in the URL or a header refuses."""
    if not server.url:
        raise McpConfigInvalid(f"MCP server {name!r} has no url", server=name)
    fill = Interpolator(scope, workspace)
    url = fill.render(server.url).strip()
    headers = {key: fill.render(value) for key, value in server.headers.items()}
    identity = server.identity_header
    if identity is not None and not any(k.lower() == identity.name.lower() for k in headers):
        if identity.value_from == "profile":
            headers[identity.name] = scope.profile_id
        else:
            headers[identity.name] = fill.render(identity.value or "")
    if fill.unresolved:
        shown = ", ".join("${" + n + "}" for n in fill.unresolved)
        raise McpUnresolvedRef(
            f"MCP server {name!r}: {shown} in the url or headers is not set for this profile",
            server=name,
            names=fill.unresolved,
        )
    cert = _client_cert(name, server, fill)
    transport: Literal["http", "sse"] = "sse" if server.kind == "sse" else "http"
    return RemoteEndpoint(url=url, headers=headers, transport=transport, cert=cert)
