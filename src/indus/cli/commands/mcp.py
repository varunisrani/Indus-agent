"""``indus mcp``: add, test and manage MCP servers (P13-12).

- ``list``: every configured server with its transport, state and tool selection (state from the
  config here: ``disabled`` / ``lazy`` / ``configured``; ``/mcp`` in chat passes the running
  servers' live states through ``status_rows(..., live=...)``);
- ``add NAME --command CMD [--arg A ...] [--env K=V ...] | --url URL [--header K=V ...]``
  (``--preset NAME`` fills command or url): validated, probed (connect, list tools) unless
  ``--no-probe``; a failed probe saves the server disabled after a confirmation (without a
  terminal: saved disabled, with a note);
- ``test NAME``: connect once, list tools; exit 0 connected, 1 failed or unknown server; an
  HTTP failure shows the status, URL and the start of the body;
- ``remove NAME``: drops the entry and its stored OAuth sign-in;
- ``enable`` / ``disable NAME``; ``login NAME [--device]`` (OAuth, P13-09);
- ``configure NAME``: a checklist of the server's tools, pre-checked from the current filter;
  ``include`` is written only for a partial selection (all checked removes the filter). Without a
  terminal only ``--include``/``--all`` work (exit 2 without them).

Every change goes through the round-trip config writer (comments and order kept). Secret values
never go into the config: header and env values that look like secrets are refused (use
``${NAME}`` and store the value with ``indus config set``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import click
import httpx
import httpx2

from indus.cli import prompts
from indus.cli.cmdkit import cli_state, err, group
from indus.cli.commands._support import fresh_view
from indus.foundation.config import writer
from indus.foundation.errors import IndusError
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.mcp import oauth, osv, trust
from indus.mcp.config import McpServer, parse_server, remote_endpoint, stdio_launch
from indus.mcp.connect import (
    Connection,
    ConnectOptions,
    default_http_client,
    first_cause,
    proxy_for_url,
)
from indus.mcp.naming import NameFilter
from indus.providers.auth import browser

__all__ = [
    "PRESETS",
    "LiveStatus",
    "ServerRow",
    "mcp_group",
    "probe_tools",
    "status_lines",
    "status_rows",
]

PRESETS: Mapping[str, Mapping[str, Any]] = {
    "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "${workspaceFolder}"],
    },
    "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
    "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
    "git": {"command": "uvx", "args": ["mcp-server-git"]},
}
"""Common server shapes for ``add --preset`` (ours; names only, the owner may extend)."""

BODY_CHARS = 200
TRANSPORT_WIDTH = 34


# -- reading the config ---------------------------------------------------------------------------


def _servers_raw(scope: Scope) -> dict[str, Any]:
    raw = fresh_view(scope).get("mcp_servers")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): value for key, value in cast(Mapping[object, Any], raw).items()}


def _server(scope: Scope, name: str) -> McpServer | None:
    """The parsed server, or None when unknown; a broken entry is a usage problem."""
    raw = _servers_raw(scope)
    if name not in raw:
        return None
    server, _warnings, problem = parse_server(name, raw[name])
    if server is None:
        raise click.ClickException(f"mcp_servers.{name} is not valid: {problem}")
    return server


def _unknown(scope: Scope, name: str) -> None:
    known = ", ".join(sorted(_servers_raw(scope))) or "none"
    err(f"No MCP server named {name!r} (configured: {known}). See `indus mcp list`.")


def _save(scope: Scope, change: Any) -> str:
    """Apply ``change(raw)`` to the user's settings and write them back."""
    view = fresh_view(scope)
    if view.error is not None:
        raise click.ClickException(f"the settings file cannot be read right now ({view.error})")
    raw = view.user_raw()
    change(raw)
    try:
        return str(writer.save(scope, raw, replace=True))
    except IndusError as exc:
        raise click.ClickException(exc.message + (f" ({exc.hint})" if exc.hint else "")) from exc


def _set_entry(scope: Scope, name: str, entry: Mapping[str, Any]) -> str:
    def change(raw: dict[str, Any]) -> None:
        servers = raw.get("mcp_servers")
        table = dict(cast(Mapping[str, Any], servers)) if isinstance(servers, Mapping) else {}
        table[name] = dict(entry)
        raw["mcp_servers"] = table

    return _save(scope, change)


def _edit_entry(scope: Scope, name: str, edit: Any) -> str:
    def change(raw: dict[str, Any]) -> None:
        table = cast(dict[str, Any], raw["mcp_servers"])
        table[name] = edit(dict(cast(Mapping[str, Any], table[name])))

    return _save(scope, change)


# -- status ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveStatus:
    """What a running session knows about one server (from the discovery manager)."""

    state: str
    tools: int | None = None


@dataclass(frozen=True)
class ServerRow:
    name: str
    transport: str
    state: str
    tools: str
    problem: str | None = None


def _transport(entry: Mapping[str, Any]) -> str:
    url = entry.get("url")
    if url:
        return str(url)
    command = str(entry.get("command") or "?")
    args = entry.get("args")
    words = [str(a) for a in cast(list[object], args)] if isinstance(args, list) else []
    return " ".join([command, *words[:2]])


def _selection(server: McpServer) -> str:
    tools = server.tools
    if tools.include is not None:
        return f"{len(tools.include)} selected"
    if tools.exclude:
        return f"-{len(tools.exclude)} excluded"
    return "all"


def status_rows(scope: Scope, live: Mapping[str, LiveStatus] | None = None) -> list[ServerRow]:
    """One row per configured server; ``live`` (from a running session) overrides the state and
    gives tool counts."""
    rows: list[ServerRow] = []
    for name, entry in sorted(_servers_raw(scope).items()):
        shown = _transport(cast(Mapping[str, Any], entry) if isinstance(entry, Mapping) else {})
        if len(shown) > TRANSPORT_WIDTH:
            shown = shown[: TRANSPORT_WIDTH - 1] + "…"
        server, _warnings, problem = parse_server(name, entry)
        if server is None:
            rows.append(ServerRow(name, shown, "invalid", "-", problem))
            continue
        selection = _selection(server)
        if not server.enabled:
            state = "disabled"
        elif live is not None and name in live:
            now = live[name]
            state = now.state.replace("_", " ")
            if now.tools is not None:
                selection = f"{now.tools} ({selection})" if selection != "all" else str(now.tools)
        elif server.lazy:
            state = "lazy"
        else:
            state = "configured"
        rows.append(ServerRow(name, shown, state, selection))
    return rows


def status_lines(rows: Sequence[ServerRow]) -> list[str]:
    """The ``list`` table (also used by ``/mcp`` in chat)."""
    if not rows:
        return [
            "No MCP servers are configured.",
            "Add one with `indus mcp add NAME --command CMD` or `indus mcp add NAME --url URL`.",
        ]
    width = max(4, *(len(r.name) for r in rows))
    lines = [f"{'Name':<{width}}  {'Transport':<{TRANSPORT_WIDTH}}  {'State':<12}  Tools"]
    for row in rows:
        lines.append(
            f"{row.name:<{width}}  {row.transport:<{TRANSPORT_WIDTH}}  {row.state:<12}  {row.tools}"
        )
        if row.problem:
            lines.append(f"{'':<{width}}  problem: {row.problem}")
    return lines


# -- probing ------------------------------------------------------------------------------------


class ProbeFailed(Exception):
    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


async def _probe(name: str, server: McpServer, scope: Scope) -> list[tuple[str, str]]:
    if server.kind == "stdio" and osv.preflight_enabled(scope):
        launch = stdio_launch(name, server, scope)
        await osv.checker_for(scope).preflight(name, launch.command, launch.args)
    auth = oauth.auth_for(name, server, scope) if server.auth == "oauth" else None
    options = ConnectOptions(auth=auth)
    async with Connection(name, server, scope, clock=scope.clock, options=options) as live:
        tools = await live.list_tools()
    return [(tool.name, " ".join((tool.description or "").split())) for tool in tools]


async def _http_detail(name: str, server: McpServer, scope: Scope) -> str | None:
    """One plain ``initialize`` POST to say what the server answers (status, URL, body start)."""
    try:
        endpoint = remote_endpoint(name, server, scope)
    except IndusError:
        return None
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "indus", "version": "probe"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    timeout = httpx2.Timeout(min(server.connect_timeout, 15.0))
    client = default_http_client(
        endpoint, proxy=proxy_for_url(endpoint.url, scope.env), timeout=timeout, auth=None
    )
    try:
        async with client:
            response = await client.post(endpoint.url, json=body, headers=headers)
            text = response.text
    except httpx2.HTTPError as exc:
        return f"no answer from {endpoint.url}: {type(exc).__name__}"
    if response.status_code < 400:
        return None
    start = " ".join(text.split())[:BODY_CHARS]
    return redact_text(
        f"HTTP {response.status_code} from {endpoint.url}: {start or '(empty body)'}"
    )


def probe_tools(name: str, server: McpServer, scope: Scope) -> list[tuple[str, str]]:
    """Connect once and list ``(tool, description)``; raises ``ProbeFailed`` with the reason."""

    async def go() -> list[tuple[str, str]]:
        try:
            return await _probe(name, server, scope)
        except Exception as exc:
            root = first_cause(exc)
            reason = getattr(root, "message", None) or str(root) or type(root).__name__
            detail = await _http_detail(name, server, scope) if server.is_remote else None
            raise ProbeFailed(redact_text(str(reason)), detail) from exc

    return asyncio.run(go())


def _report_failure(name: str, failure: ProbeFailed, elapsed: float | None = None) -> None:
    took = f" after {elapsed:.1f} s" if elapsed is not None else ""
    err(f"Could not connect to {name!r}{took}: {failure.reason}")
    if failure.detail:
        err(f"  {failure.detail}")


# -- the group ----------------------------------------------------------------------------------


@group("mcp", "Add, test and manage MCP servers.")
def mcp_group() -> None:
    pass


@mcp_group.command("list")
def list_command() -> None:
    """List configured MCP servers with their state and tools."""
    for line in status_lines(status_rows(cli_state().scope)):
        click.echo(line)


def _pairs(values: Sequence[str], what: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise click.BadParameter(f"{item!r} is not KEY=VALUE", param_hint=what)
        if redact_text(value) != value:
            raise click.BadParameter(
                f"the value of {key} looks like a secret; write ${{NAME}} and store the value "
                "with `indus config set NAME VALUE`",
                param_hint=what,
            )
        found[key.strip()] = value
    return found


def _entry_from_flags(
    *,
    preset: str | None,
    command: str | None,
    args: Sequence[str],
    env: Sequence[str],
    url: str | None,
    headers: Sequence[str],
    transport: str | None,
    auth: str | None,
    lazy: bool,
    trust: str | None,
    connect_timeout: float | None,
) -> dict[str, Any]:
    if command and url:
        raise click.UsageError("give either --command or --url, not both")
    entry: dict[str, Any] = {}
    if preset is not None and not command and not url:
        entry.update(
            {k: (list(v) if isinstance(v, list) else v) for k, v in PRESETS[preset].items()}
        )
    if command:
        entry["command"] = command
        if args:
            entry["args"] = list(args)
    elif args and "command" in entry:
        entry["args"] = [*entry.get("args", []), *args]
    if url:
        entry["url"] = url
    if not entry.get("command") and not entry.get("url"):
        raise click.UsageError("give --command CMD, --url URL or --preset NAME")
    if env:
        if entry.get("url"):
            raise click.UsageError("--env applies to stdio servers (--command) only")
        entry["env"] = _pairs(env, "--env")
    if headers:
        if not entry.get("url"):
            raise click.UsageError("--header applies to http servers (--url) only")
        entry["headers"] = _pairs(headers, "--header")
    if transport:
        entry["transport"] = transport
    if auth:
        entry["auth"] = auth
    if lazy:
        entry["lazy"] = True
    if trust:
        entry["trust"] = trust
    if connect_timeout is not None:
        entry["connect_timeout"] = connect_timeout
    return entry


@mcp_group.command("add")
@click.argument("name")
@click.option("--preset", type=click.Choice(sorted(PRESETS)), help="A common server shape.")
@click.option("--command", "command", help="Program that runs a stdio server.")
@click.option("--arg", "args", multiple=True, help="One argument for --command (repeat).")
@click.option("--env", "env", multiple=True, help="KEY=VALUE for a stdio server (repeat).")
@click.option("--url", help="Endpoint of an http (or sse) server.")
@click.option("--header", "headers", multiple=True, help="KEY=VALUE request header (repeat).")
@click.option("--transport", type=click.Choice(["http", "sse"]), help="Remote transport.")
@click.option("--auth", type=click.Choice(["oauth"]), help="Sign in with OAuth.")
@click.option("--lazy", is_flag=True, help="Start the server on its first use.")
@click.option("--trust", type=click.Choice(["full", "untrusted"]), help="Trust tier.")
@click.option("--connect-timeout", type=float, help="Seconds a connect may take.")
@click.option("--no-probe", is_flag=True, help="Save without connecting first.")
@click.option("--force", "-f", is_flag=True, help="Replace a server of the same name.")
def add_command(
    name: str,
    preset: str | None,
    command: str | None,
    args: tuple[str, ...],
    env: tuple[str, ...],
    url: str | None,
    headers: tuple[str, ...],
    transport: str | None,
    auth: str | None,
    lazy: bool,
    trust: str | None,
    connect_timeout: float | None,
    no_probe: bool,
    force: bool,
) -> None:
    """Add an MCP server (stdio with --command, remote with --url)."""
    state = cli_state()
    scope = state.scope
    entry = _entry_from_flags(
        preset=preset,
        command=command,
        args=args,
        env=env,
        url=url,
        headers=headers,
        transport=transport,
        auth=auth,
        lazy=lazy,
        trust=trust,
        connect_timeout=connect_timeout,
    )
    server, warnings, problem = parse_server(name, entry)
    if server is None:
        raise click.UsageError(f"the server settings are not valid: {problem}")
    for text in warnings:
        err(f"Note: {text}")
    if name in _servers_raw(scope):
        io = prompts.io_for(state, f"{name!r} exists already; pass --force to replace it.")
        if prompts.confirm(io, f"Replace the MCP server {name!r}?", assume_yes=force) is not True:
            raise click.exceptions.Exit(1)
    if not no_probe and not (server.auth == "oauth" and server.is_remote):
        try:
            tools = probe_tools(name, server, scope)
        except ProbeFailed as failure:
            _report_failure(name, failure)
            io = prompts.io_for(state)
            if io.tty and prompts.confirm(io, "Save it anyway, turned off?") is not True:
                raise click.exceptions.Exit(1) from None
            entry["enabled"] = False
            path = _set_entry(scope, name, entry)
            click.echo(
                f"Saved {name!r} turned off in {path}. Fix it, then `indus mcp test {name}`."
            )
            raise click.exceptions.Exit(1) from None
        click.echo(f"Connected to {name!r}: {len(tools)} tool(s).")
    path = _set_entry(scope, name, entry)
    click.echo(f"Saved {name!r} in {path}.")
    if server.auth == "oauth":
        click.echo(f"Sign in with `indus mcp login {name}` before using it.")
    click.echo("Open sessions pick it up after /reload-mcp.")


@mcp_group.command("test")
@click.argument("name")
def test_command(name: str) -> None:
    """Connect to a server once and list its tools (exit 0 connected, 1 otherwise)."""
    scope = cli_state().scope
    server = _server(scope, name)
    if server is None:
        _unknown(scope, name)
        raise click.exceptions.Exit(1)
    where = server.url if server.is_remote else server.command
    click.echo(f"Testing {name!r} ({server.kind}: {where})…")
    if server.auth == "oauth":
        click.echo("Auth: OAuth (stored sign-in)")
    started = time.monotonic()
    try:
        tools = probe_tools(name, server, scope)
    except ProbeFailed as failure:
        _report_failure(name, failure, time.monotonic() - started)
        raise click.exceptions.Exit(1) from None
    click.echo(f"Connected in {(time.monotonic() - started) * 1000:.0f} ms; {len(tools)} tool(s):")
    for tool, description in tools:
        click.echo(f"  {tool}" + (f" — {description[:70]}" if description else ""))


@mcp_group.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Do not ask.")
def remove_command(name: str, yes: bool) -> None:
    """Remove a server and its stored sign-in."""
    state = cli_state()
    scope = state.scope
    if name not in _servers_raw(scope):
        _unknown(scope, name)
        raise click.exceptions.Exit(1)
    io = prompts.io_for(state, f"Pass --yes to remove {name!r} without a terminal.")
    if prompts.confirm(io, f"Remove the MCP server {name!r}?", assume_yes=yes) is not True:
        raise click.exceptions.Exit(1)

    def change(raw: dict[str, Any]) -> None:
        table = dict(cast(Mapping[str, Any], raw.get("mcp_servers") or {}))
        table.pop(name, None)
        raw["mcp_servers"] = table

    path = _save(scope, change)
    removed = 0
    try:
        removed = oauth.logout(name, scope)
    except Exception as exc:
        err(f"The stored sign-in could not be removed: {type(exc).__name__}")
    click.echo(
        f"Removed {name!r} from {path}." + (" Its sign-in was removed too." if removed else "")
    )


def _toggle(name: str, on: bool) -> None:
    scope = cli_state().scope
    if name not in _servers_raw(scope):
        _unknown(scope, name)
        raise click.exceptions.Exit(1)

    def edit(entry: dict[str, Any]) -> dict[str, Any]:
        entry["enabled"] = on
        return entry

    path = _edit_entry(scope, name, edit)
    click.echo(f"{name!r} is {'on' if on else 'off'} ({path}). Open sessions: /reload-mcp.")


@mcp_group.command("enable")
@click.argument("name")
def enable_command(name: str) -> None:
    """Turn a server on."""
    _toggle(name, True)


@mcp_group.command("disable")
@click.argument("name")
def disable_command(name: str) -> None:
    """Turn a server off (its settings stay)."""
    _toggle(name, False)


class _CliUi:
    def say(self, text: str) -> None:
        click.echo(text)

    def open_url(self, url: str) -> bool:
        return browser.open_url(url)


@mcp_group.command("login")
@click.argument("name")
@click.option("--device", is_flag=True, help="Sign in with a code on another device.")
def login_command(name: str, device: bool) -> None:
    """Sign in to an OAuth server (auth: oauth)."""
    scope = cli_state().scope
    server = _server(scope, name)
    if server is None:
        _unknown(scope, name)
        raise click.exceptions.Exit(1)
    if server.auth != "oauth" or not server.is_remote:
        err(f"{name!r} does not use OAuth; set `auth: oauth` on an http server to sign in.")
        raise click.exceptions.Exit(1)
    ui = _CliUi()
    # The person's own sign-in: the only place an MCP login may open a browser (R07.24).
    with trust.allow_interactive_auth():
        try:
            if device:

                async def by_device() -> None:
                    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                        await oauth.device_login(name, server, scope, ui=ui, client=client)

                asyncio.run(by_device())
                click.echo(f"Signed in to {name!r}.")
                return
            outcome = asyncio.run(oauth.login(name, server, scope, ui=ui))
        except IndusError as exc:
            err(f"Sign-in failed: {exc.message}" + (f" ({exc.hint})" if exc.hint else ""))
            raise click.exceptions.Exit(1) from None
        except Exception as exc:
            err(f"Sign-in failed: {redact_text(str(exc)) or type(exc).__name__}")
            raise click.exceptions.Exit(1) from None
    if not outcome.authorized:
        err(f"{name!r} answered without asking for a sign-in; any earlier sign-in is kept.")
        raise click.exceptions.Exit(1)
    click.echo(f"Signed in to {name!r}; {len(outcome.tools)} tool(s) available.")


@mcp_group.command("configure")
@click.argument("name")
@click.option("--include", "include", multiple=True, help="Keep only this tool (repeat).")
@click.option("--all", "all_tools", is_flag=True, help="Use every tool (remove the filter).")
def configure_command(name: str, include: tuple[str, ...], all_tools: bool) -> None:
    """Choose which of a server's tools are used."""
    state = cli_state()
    scope = state.scope
    if include and all_tools:
        raise click.UsageError("give --include or --all, not both")
    server = _server(scope, name)
    if server is None:
        _unknown(scope, name)
        raise click.exceptions.Exit(1)
    io = prompts.io_for(state)
    if not include and not all_tools and not io.tty:
        err("Without a terminal, choose with --include TOOL (repeat) or --all.")
        raise click.exceptions.Exit(2)
    chosen: list[str] | None
    if all_tools:
        chosen = None
    elif include:
        chosen = list(include)
    else:
        try:
            tools = probe_tools(name, server, scope)
        except ProbeFailed as failure:
            _report_failure(name, failure)
            raise click.exceptions.Exit(1) from None
        if not tools:
            click.echo(f"{name!r} offers no tools.")
            return
        names = [tool for tool, _ in tools]
        rule = NameFilter.of(server.tools.include, server.tools.exclude)
        checked = [i for i, tool in enumerate(names) if rule(tool)]
        rows = [f"{tool}  {desc[:60]}" if desc else tool for tool, desc in tools]
        try:
            picked = prompts.ask_checklist(io, f"Tools of {name!r} to use:", rows, checked)
        except prompts.Cancelled:
            click.echo("Nothing changed.")
            return
        if sorted(picked) == sorted(checked):
            click.echo("Nothing changed.")
            return
        chosen = None if len(picked) == len(names) else [names[i] for i in picked]

    def edit(entry: dict[str, Any]) -> dict[str, Any]:
        raw_tools = entry.get("tools")
        tools_entry = (
            dict(cast(Mapping[str, Any], raw_tools)) if isinstance(raw_tools, Mapping) else {}
        )
        tools_entry.pop("exclude", None)
        if chosen is None:
            tools_entry.pop("include", None)
        else:
            tools_entry["include"] = chosen
        if tools_entry:
            entry["tools"] = tools_entry
        else:
            entry.pop("tools", None)
        return entry

    path = _edit_entry(scope, name, edit)
    summary = "all tools" if chosen is None else f"{len(chosen)} tool(s): {', '.join(chosen)}"
    click.echo(f"{name!r} now uses {summary} ({path}). Open sessions: /reload-mcp.")
