"""``/api/mcp``: the MCP page's backend (P18-14, over P13's ``indus.mcp``).

- ``GET /api/mcp/servers``: every ``mcp_servers`` entry with its transport, command and args, env,
  URL and sign-in kind. Env values are redacted (a ``${NAME}`` reference is shown as written; any
  other value is masked), URLs and args pass through the redactor, header values are never
  shown. A broken entry is listed with its problem.
- ``POST /api/mcp/servers {name, command | url, args?, env?, auth?}``: validated as
  ``indus mcp add`` does (P13's model; a secret-looking literal is refused: store the value on the
  Keys page and write ``${NAME}``), a duplicate name is 409, saved through the config writer;
  jobs waiting for the server become suggestions (wave 45, ``service.integration_offers``).
- ``POST /api/mcp/servers/{name}/test``: connect once, list the tools, disconnect, all within
  ``TEST_DEADLINE_S``; an ``auth: oauth`` server without a stored sign-in answers "sign-in needed"
  without connecting. Failures are ``{ok: false, error}`` (redacted), never a 500.
- ``PUT /api/mcp/servers/{name}/enabled {enabled}``, ``DELETE /api/mcp/servers/{name}`` (also
  drops the stored sign-in).
- OAuth (``routes.mcp_oauth``): ``POST /api/mcp/servers/{name}/auth`` starts a sign-in and answers
  the authorization URL; ``GET|DELETE /api/mcp/oauth/flows/{id}`` polls or cancels it;
  ``GET /api/mcp/oauth/callback`` is the browser's way back (public, fixed HTML).

Seams for tests (``DashboardState.extras``): ``mcp.oauth_connect`` (the flow's connector) and
``mcp.oauth_start`` (``(flow, name, server, scope, redirect_uri) -> None``, how a flow is run).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from indus.foundation import config as config_module
from indus.foundation.clock import SystemClock
from indus.foundation.errors import ConfigError
from indus.foundation.redact import mask, redact_text
from indus.foundation.scope import Scope
from indus.mcp import oauth, osv
from indus.mcp.config import McpServer, parse_server, stdio_launch
from indus.mcp.connect import Connection, ConnectOptions, first_cause
from indus.service import integration_offers
from indus.web.auth.gate import request_prefix
from indus.web.routes import mcp_oauth
from indus.web.routes.mcp_oauth import CALLBACK_PATH, FlowBusy, OAuthFlows
from indus.web.scope import read_scope, write_scope
from indus.web.state import DashboardState, dashboard_state

__all__ = ["ROUTE_LIST", "TEST_DEADLINE_S", "router"]

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/mcp/servers", True),
    ("POST", "/api/mcp/servers", True),
    ("POST", "/api/mcp/servers/{name}/test", True),
    ("PUT", "/api/mcp/servers/{name}/enabled", True),
    ("DELETE", "/api/mcp/servers/{name}", True),
    ("POST", "/api/mcp/servers/{name}/auth", True),
    ("GET", "/api/mcp/oauth/flows/{flow_id}", True),
    ("DELETE", "/api/mcp/oauth/flows/{flow_id}", True),
    ("GET", CALLBACK_PATH, False),
)
"""``(method, path, scoped)`` of every route here. A flow answers only to the profile it was
started for; the callback finds its flow through ``state`` and takes no ``?profile=``."""

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]

TEST_DEADLINE_S: Final = 15.0
_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REFERENCE: Final = re.compile(r"^\$\{[^{}]+\}$")
_FLOWS: Final = "mcp.oauth_flows"


def _fail(status: int, code: str, message: str, **extra: Any) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _configured(scope: Scope) -> Scope:
    view = config_module.load(scope)
    if view.error is not None:
        _fail(500, "config_unreadable", "config.yaml cannot be read right now; fix it first.")
    return scope.with_config(view)


def _servers_raw(scope: Scope) -> dict[str, Any]:
    raw: object = config_module.load(scope).get("mcp_servers")
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): v for k, v in raw.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def _server(scope: Scope, name: str) -> McpServer:
    raw = _servers_raw(scope)
    if name not in raw:
        _fail(404, "server_not_found", "No MCP server has this name.")
    server, _warnings, problem = parse_server(name, raw[name])
    if server is None:
        _fail(409, "server_invalid", redact_text(problem or "the entry is not valid"))
    return server


def _save(scope: Scope, change: Any) -> None:
    view = config_module.load(scope)
    if view.error is not None:
        _fail(500, "config_unreadable", "config.yaml cannot be read right now; fix it first.")
    raw = view.user_raw()
    change(raw)
    try:
        config_module.save(scope, raw, replace=True)
    except ConfigError as exc:
        _fail(500, "config_unwritable", f"{exc.message}. Nothing was changed.")


# -- list ------------------------------------------------------------------------------------------


def _shown_env(value: str) -> str:
    return value if _REFERENCE.match(value.strip()) else mask(value) if value else ""


def _auth_kind(entry: Mapping[str, Any]) -> str:
    if entry.get("auth") == "oauth":
        return "oauth"
    return "header" if entry.get("headers") else "none"


def server_row(name: str, entry: object) -> dict[str, Any]:
    """The page's view of one entry (secrets redacted)."""
    server, _warnings, problem = parse_server(name, entry)
    data: Mapping[str, Any] = entry if isinstance(entry, Mapping) else {}  # pyright: ignore[reportUnknownVariableType]
    if server is None:
        return {
            "name": name,
            "enabled": False,
            "transport": "http" if data.get("url") else "stdio",
            "command": None,
            "args": [],
            "env": {},
            "url": None,
            "auth": "none",
            "problem": redact_text(problem or "the entry is not valid"),
        }
    return {
        "name": name,
        "enabled": server.enabled,
        "transport": "stdio" if server.kind == "stdio" else "http",
        "command": redact_text(server.command) if server.command else None,
        "args": [redact_text(arg) for arg in server.args],
        "env": {key: _shown_env(value) for key, value in sorted(server.env.items())},
        "url": redact_text(server.url) if server.url else None,
        "auth": _auth_kind(data),
        "problem": None,
    }


@router.get("/api/mcp/servers")
def list_servers(scope: ReadScope) -> dict[str, Any]:
    raw = _servers_raw(scope)
    return {"servers": [server_row(name, raw[name]) for name in sorted(raw)]}


# -- add -------------------------------------------------------------------------------------------


class ServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    command: str | None = Field(default=None, max_length=1024)
    args: list[str] = Field(default_factory=list[str], max_length=100)
    env: dict[str, str] = Field(default_factory=dict[str, str], max_length=100)
    url: str | None = Field(default=None, max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict[str, str], max_length=50)
    auth: Literal["none", "oauth", "header"] = "none"

    @model_validator(mode="after")
    def _one_transport(self) -> ServerCreate:
        if bool(self.command) == bool(self.url):
            raise ValueError("give a command (stdio) or a url (http), not both and not neither")
        if self.env and self.url:
            raise ValueError("env applies to stdio servers only")
        if self.headers and self.command:
            raise ValueError("headers apply to http servers only")
        return self


def _no_literal_secrets(values: Mapping[str, str], what: str) -> None:
    for key, value in values.items():
        if redact_text(value) != value or redact_text(f"{key}={value}") != f"{key}={value}":
            _fail(
                400,
                "secret_value",
                f"The {what} value of {key} looks like a secret: store it on the Keys page and "
                f"write ${{{key}}} here instead.",
            )


@router.post("/api/mcp/servers", status_code=201)
def add_server(body: ServerCreate, scope: WriteScope) -> dict[str, Any]:
    if not _NAME.match(body.name):
        _fail(400, "invalid_name", "Server names are letters, digits, '.', '_' and '-'.")
    _no_literal_secrets(body.env, "env")
    _no_literal_secrets(body.headers, "header")
    if body.url is not None and redact_text(body.url) != body.url:
        _fail(400, "secret_value", "The URL carries a credential; use a header with ${NAME}.")
    entry: dict[str, Any] = {}
    if body.command:
        entry["command"] = body.command
        if body.args:
            entry["args"] = list(body.args)
        if body.env:
            entry["env"] = dict(body.env)
    else:
        entry["url"] = body.url
        if body.headers:
            entry["headers"] = dict(body.headers)
        if body.auth == "oauth":
            entry["auth"] = "oauth"
    server, _warnings, problem = parse_server(body.name, entry)
    if server is None:
        _fail(400, "invalid_server", redact_text(problem or "the entry is not valid"))
    if body.name in _servers_raw(scope):
        _fail(409, "server_exists", "An MCP server with this name exists already.")

    def change(raw: dict[str, Any]) -> None:
        table: object = raw.get("mcp_servers")
        servers = dict(table) if isinstance(table, Mapping) else {}  # pyright: ignore[reportUnknownArgumentType]
        servers[body.name] = entry
        raw["mcp_servers"] = servers

    _save(scope, change)
    # Jobs that were waiting for this server become suggestions (wave 45); never scheduled here.
    integration_offers.connected(scope, "mcp", body.name)
    return server_row(body.name, entry)


# -- test ------------------------------------------------------------------------------------------


async def _probe(name: str, server: McpServer, scope: Scope) -> list[dict[str, str]]:
    if server.kind == "stdio" and osv.preflight_enabled(scope):
        launch = stdio_launch(name, server, scope)
        await osv.checker_for(scope).preflight(name, launch.command, launch.args)
    auth = oauth.auth_for(name, server, scope) if server.auth == "oauth" else None
    options = ConnectOptions(auth=auth)
    async with Connection(name, server, scope, clock=SystemClock(), options=options) as live:
        tools = await live.list_tools()
    return [{"name": t.name, "description": " ".join((t.description or "").split())} for t in tools]


def _signed_in(name: str, scope: Scope) -> bool:
    store = oauth.OAuthStore(oauth.secrets_for(scope), name, clock=scope.clock, home=scope.home)
    return store.has_tokens()


@router.post("/api/mcp/servers/{name}/test")
def test_server(name: str, scope: WriteScope) -> dict[str, Any]:
    live = _configured(scope)
    server = _server(live, name)
    if server.auth == "oauth" and server.is_remote and not _signed_in(name, live):
        return {"ok": False, "error": "This server needs a sign-in first.", "tools": []}
    bounded = server.model_copy(
        update={"connect_timeout": min(server.connect_timeout, TEST_DEADLINE_S)}
    )

    async def bounded_probe() -> list[dict[str, str]]:
        return await asyncio.wait_for(_probe(name, bounded, live), TEST_DEADLINE_S)

    try:
        tools = asyncio.run(bounded_probe())
    except TimeoutError:
        return {"ok": False, "error": f"No answer within {TEST_DEADLINE_S:g} s.", "tools": []}
    except Exception as exc:
        root = first_cause(exc)
        reason = getattr(root, "message", None) or str(root) or type(root).__name__
        return {"ok": False, "error": redact_text(str(reason)), "tools": []}
    return {"ok": True, "tools": tools}


# -- enable, delete --------------------------------------------------------------------------------


class EnabledToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


@router.put("/api/mcp/servers/{name}/enabled")
def set_enabled(name: str, body: EnabledToggle, scope: WriteScope) -> dict[str, Any]:
    raw = _servers_raw(scope)
    if name not in raw or not isinstance(raw[name], Mapping):
        _fail(404, "server_not_found", "No MCP server has this name.")

    def change(tree: dict[str, Any]) -> None:
        servers = dict(tree["mcp_servers"])
        entry = dict(servers[name])
        entry["enabled"] = body.enabled
        servers[name] = entry
        tree["mcp_servers"] = servers

    _save(scope, change)
    return {"ok": True, "name": name, "enabled": body.enabled}


@router.delete("/api/mcp/servers/{name}")
def delete_server(name: str, scope: WriteScope) -> dict[str, Any]:
    if name not in _servers_raw(scope):
        _fail(404, "server_not_found", "No MCP server has this name.")

    def change(tree: dict[str, Any]) -> None:
        servers = dict(tree.get("mcp_servers") or {})
        servers.pop(name, None)
        tree["mcp_servers"] = servers

    _save(scope, change)
    removed = 0
    try:
        removed = oauth.logout(name, scope)
    except Exception as exc:
        return {"ok": True, "signin_removed": False, "warning": type(exc).__name__}
    return {"ok": True, "signin_removed": bool(removed)}


# -- OAuth -----------------------------------------------------------------------------------------


def _flows(state: DashboardState) -> OAuthFlows:
    found: object = state.extras.get(_FLOWS)
    if isinstance(found, OAuthFlows):
        return found
    return state.extras.setdefault(_FLOWS, OAuthFlows())  # pyright: ignore[reportReturnType]


def _redirect_uri(request: Request) -> str:
    state = dashboard_state(request.app)
    if state.public is not None:
        return state.public.url.rstrip("/") + CALLBACK_PATH
    base = str(request.base_url).rstrip("/")
    return f"{base}{request_prefix(request.scope)}{CALLBACK_PATH}"


@router.post("/api/mcp/servers/{name}/auth")
def start_auth(request: Request, name: str, scope: WriteScope) -> dict[str, Any]:
    live = _configured(scope)
    server = _server(live, name)
    if not server.is_remote:
        _fail(400, "oauth_not_applicable", "stdio servers sign in with env values, not OAuth.")
    if server.headers and server.auth != "oauth":
        _fail(400, "oauth_not_applicable", "This server signs in with a header, not OAuth.")
    state = dashboard_state(request.app)
    try:
        flow = _flows(state).start(name, str(live.home))
    except FlowBusy as exc:
        _fail(429 if exc.code == "too_many_flows" else 409, exc.code, str(exc))
    target = server.model_copy(update={"auth": "oauth"})
    redirect_uri = _redirect_uri(request)
    starter: object = state.extras.get("mcp.oauth_start")
    if callable(starter):
        starter(flow, name, target, live, redirect_uri)
    else:
        connect: object = state.extras.get("mcp.oauth_connect")
        extra: dict[str, Any] = {"connect": connect} if callable(connect) else {}
        mcp_oauth.start_thread(flow, name, target, live, redirect_uri, **extra)
    flow.url_ready.wait(mcp_oauth.URL_WAIT_S)
    return flow.snapshot()


def _flow(request: Request, flow_id: str, scope: Scope) -> mcp_oauth.OAuthFlow | None:
    flow = _flows(dashboard_state(request.app)).get(flow_id)
    return flow if flow is not None and flow.home == str(scope.home) else None


@router.get("/api/mcp/oauth/flows/{flow_id}")
def flow_status(request: Request, flow_id: str, scope: ReadScope) -> dict[str, Any]:
    flow = _flow(request, flow_id, scope)
    if flow is None:
        _fail(404, "flow_not_found", "This sign-in is not known or has expired.")
    return flow.snapshot()


@router.delete("/api/mcp/oauth/flows/{flow_id}")
def cancel_flow(request: Request, flow_id: str, scope: WriteScope) -> dict[str, Any]:
    flow = _flow(request, flow_id, scope)
    if flow is None:
        return {"ok": True, "status": "expired"}
    flow.fail("cancelled from the dashboard", cancelled=True)
    return {"ok": True, "status": flow.snapshot()["status"]}


@router.get(CALLBACK_PATH, response_class=HTMLResponse)
def oauth_callback(
    request: Request,
    state: str = "",
    code: str | None = None,
    error: str | None = None,
    iss: str | None = None,
) -> HTMLResponse:
    flow = _flows(dashboard_state(request.app)).by_state(state)
    if flow is None:
        kind = "unknown"
    elif not flow.deliver(code=code, state=state, iss=iss, error=error):
        kind = "repeated"
    else:
        kind = "refused" if error else "received"
    status, html = mcp_oauth.callback_page(kind)
    return HTMLResponse(
        html,
        status_code=status,
        headers={"cache-control": "no-store", "referrer-policy": "no-referrer"},
    )
