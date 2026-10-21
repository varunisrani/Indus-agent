"""``indus dashboard`` and ``indus serve`` (P18-01, P18-02).

The commands live with the web package and the CLI loads them by name (``cli.app.REGISTRY``): the
CLI and the web surface are sibling layers and may not import each other (D-16.246); the process
state comes in as ``ctx.obj``.

``indus dashboard [--host 127.0.0.1] [--port 9119] [--no-open] [--isolated]`` serves the web
dashboard. One backend serves the whole machine root (every profile under it):

- a live machine backend (``<root>/dashboard.json`` + its health answer) that serves the asked
  address is reused: the browser opens on it (``?profile=<name>`` for a named profile) and the
  command ends (exit 0); one that cannot serve the asked host/port ends the command with exit 78,
  naming its pid and address;
- a port another program holds ends it with exit 75 before anything starts;
- ``--isolated`` runs a backend for this profile alone, beside any machine backend.

Gated mode (a bind beyond loopback, or a public URL) needs a login provider. Without one: on a
terminal the command offers to set a username and password (written through the config writer as
an argon2id hash), otherwise it refuses to bind and lists why each provider is off (exit 1). There
is no flag that skips the login (D-16.25).

A missing SPA bundle in a source checkout with ``pnpm`` on PATH is built once before serving
(``indus.web.spa.build_plan``). The dashboard attaches to the profile daemon in the background.

``indus serve [--host] [--port 0]`` runs the headless backend (JSON and WebSocket only) and writes
``<home>/serve.json``; ``indus serve discover --json [-p NAME]`` and ``indus serve ensure --json``
find or start one (``indus.web.serve_runtime``) and print ``{url, port, pid, profile_id,
code_version}`` — never a token: the caller mints it and hands it over in
``INDUS_DASHBOARD_SESSION_TOKEN``.
"""

from __future__ import annotations

import errno
import json
import socket
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import click

if TYPE_CHECKING:
    from fastapi import FastAPI

    from indus.foundation.scope import Scope

__all__ = [
    "EX_CONFIG",
    "EX_TEMPFAIL",
    "Launch",
    "dashboard_command",
    "run_launch",
    "serve_group",
]

EX_TEMPFAIL = 75
"""The port is held by another program (try again later or pick another)."""
EX_CONFIG = 78
"""This machine's dashboard already runs somewhere the request cannot be served."""
DEFAULT_PORT = 9119


@dataclass
class Launch:
    """One dashboard/serve launch and its seams (tests replace the probes and the runner)."""

    scope: Scope
    mode: str
    host: str
    port: int
    port_given: bool = True
    open_browser: bool = True
    isolated: bool = False
    interactive: bool = False
    bind: Callable[[str, int], socket.socket] | None = None
    owner: Callable[[Path], Any] | None = None
    serve: Callable[[FastAPI, socket.socket], None] | None = None
    opener: Callable[[str], object] = webbrowser.open
    build: bool = True
    ask: Callable[[], tuple[str, str] | None] | None = None
    extras: dict[str, Any] = field(default_factory=dict[str, Any])


# -- process state (the CLI's ``ctx.obj``; the CLI package itself is a sibling layer) -------------


def err(*lines: str) -> None:
    for line in lines:
        click.echo(line, err=True)


def _process(ctx: click.Context) -> Any:
    state: Any = ctx.find_root().obj
    if getattr(state, "scope", None) is None:
        raise click.UsageError(f"{ctx.command_path} was started without the process scope")
    return state


def _scope_of(ctx: click.Context) -> Scope:
    return _process(ctx).scope


def _interactive(ctx: click.Context) -> bool:
    """Both stdin and stdout are terminals (a prompt can be shown and answered)."""
    state = _process(ctx)
    checks = (getattr(state, "stdin_isatty", None), getattr(state, "stdout_isatty", None))
    return all(callable(check) and bool(check()) for check in checks)


# -- sockets and owners ---------------------------------------------------------------------------


def bind_socket(host: str, port: int) -> socket.socket:
    """A listening socket on ``host:port`` (``OSError`` EADDRINUSE when it is taken)."""
    family = socket.AF_INET6 if ":" in host.strip("[]") else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host.strip("[]"), port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(False)
    return sock


def machine_owner(root: Path) -> Any:
    """The live machine backend's record, or None."""
    from indus.web import serve_runtime

    record = serve_runtime.read_record(serve_runtime.dashboard_record_path(root))
    if record is None:
        return None
    return record if serve_runtime.live_health(record) is not None else None


def _same_address(record: Any, host: str, port: int, port_given: bool) -> bool:
    if port_given and record.port != port:
        return False
    return str(record.host).strip("[]").lower() == host.strip("[]").lower()


def _profile_url(url: str, profile: str) -> str:
    if not profile or profile == "default":
        return url + "/"
    return f"{url}/?profile={quote(profile, safe='')}"


# -- gated mode without a provider ----------------------------------------------------------------


def _ask_credentials() -> tuple[str, str] | None:
    if not click.confirm(
        "This address needs a sign-in. Set a dashboard username and password now?", default=False
    ):
        return None
    username = click.prompt("Username", default="admin").strip()
    password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    if not username or not password:
        return None
    return username, password


def _save_credentials(scope: Scope, username: str, password: str) -> None:
    from indus.foundation.config.writer import save
    from indus.web.auth.basic import hash_password

    save(
        scope,
        {
            "dashboard": {
                "basic_auth": {"username": username, "password_hash": hash_password(password)}
            }
        },
    )


# -- running --------------------------------------------------------------------------------------


def run_server(app: FastAPI, sock: socket.socket) -> None:
    import uvicorn

    config = uvicorn.Config(
        app,
        log_level="warning",
        access_log=False,
        proxy_headers=False,
        server_header=False,
        ws="wsproto",
        lifespan="on",
        timeout_graceful_shutdown=5,
    )
    uvicorn.Server(config).run(sockets=[sock])


def _maybe_build(launch: Launch) -> None:
    from indus.web import bundle, spa

    if bundle.locate().found:
        return
    repo = bundle.source_checkout()
    plan = spa.build_plan(repo, launch.scope.env) if repo is not None else None
    if plan is None:
        err(
            "The dashboard pages are not built; the API still works.",
            f"Build them with: {spa.BUILD_COMMAND}",
        )
        return
    click.echo("Building the dashboard pages (first launch)...")
    if not spa.run_build(plan):
        err(f"The build failed; run `{spa.BUILD_COMMAND}` to see why.")


def run_launch(launch: Launch) -> int:
    """Run one launch to its end; the exit code."""
    from dataclasses import replace

    from indus.foundation.profiles import DEFAULT_PROFILE, profile_home
    from indus.web import serve_runtime
    from indus.web.app import AppOptions, DaemonLink, create_app
    from indus.web.auth.gate import GateRefused
    from indus.web.auth.proxy import ProxyConfigError
    from indus.web.auth.token import SessionTokenError
    from indus.web.scope import build_profile_scope

    scope = launch.scope
    profile = scope.profile_id
    machine = launch.mode == "dashboard" and not launch.isolated
    if machine:
        owner = (launch.owner or machine_owner)(scope.root)
        if owner is not None:
            if not _same_address(owner, launch.host, launch.port, launch.port_given):
                err(
                    f"This machine's dashboard already runs at {owner.url} (pid {owner.pid}).",
                    "Open that address, stop it first, or use --isolated for a separate one.",
                )
                return EX_CONFIG
            target = _profile_url(owner.url, profile)
            click.echo(f"The dashboard is already running: {target}")
            if launch.open_browser:
                launch.opener(target)
            return 0
        if profile != DEFAULT_PROFILE:
            # The machine backend is the default profile's: its own env (the inherited process
            # env and the default profile's .env), never the launching profile's view (P18-10).
            scope = replace(
                build_profile_scope(scope, profile_home(scope.root, DEFAULT_PROFILE)), routed=False
            )
    try:
        sock = (launch.bind or bind_socket)(launch.host, launch.port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            err(
                f"Port {launch.port} on {launch.host} is in use by another program.",
                "Stop it or pick another port with --port.",
            )
            return EX_TEMPFAIL
        err(f"Could not listen on {launch.host}:{launch.port}: {exc.strerror or exc}")
        return 1
    try:
        port = int(sock.getsockname()[1])
        options = AppOptions(
            host=launch.host,
            port=port,
            initial_profile=profile if machine and profile != DEFAULT_PROFILE else "",
            daemon=DaemonLink(scope.home, scope.env) if launch.mode == "dashboard" else None,
        )
        while True:
            try:
                app = create_app(
                    scope, "serve" if launch.mode == "serve" else "dashboard", options=options
                )
                break
            except GateRefused as refused:
                if not launch.interactive:
                    err(str(refused))
                    return 1
                answer = (launch.ask or _ask_credentials)()
                if answer is None:
                    err(str(refused))
                    return 1
                _save_credentials(scope, *answer)
            except (ProxyConfigError, SessionTokenError) as exc:
                err(f"The dashboard cannot start: {exc}")
                return 1
        if launch.mode == "dashboard" and launch.build:
            _maybe_build(launch)
        import indus

        record = serve_runtime.new_record(
            home=scope.home,
            profile=scope.profile_id,
            host=launch.host,
            port=port,
            mode="serve" if launch.mode == "serve" else "dashboard",
            code_version=indus.__version__,
        )
        if launch.mode == "serve":
            path = serve_runtime.serve_record_path(scope.home)
        elif machine:
            path = serve_runtime.dashboard_record_path(scope.root)
        else:
            path = None
        if path is not None:
            serve_runtime.write_record(path, record)
        url = record.url
        shown = _profile_url(url, profile) if machine else url + "/"
        if launch.mode == "serve":
            click.echo(f"Headless backend on {url} (JSON and WebSocket only).")
        else:
            click.echo(f"Dashboard on {shown}")
            if launch.open_browser:
                threading.Timer(0.8, launch.opener, args=(shown,)).start()
        try:
            (launch.serve or run_server)(app, sock)
        finally:
            if path is not None:
                serve_runtime.clear_record(path, record.pid)
        return 0
    finally:
        sock.close()


# -- click ----------------------------------------------------------------------------------------


def _port_given(ctx: click.Context) -> bool:
    source = ctx.get_parameter_source("port")
    return source is not None and source.name not in ("DEFAULT", "DEFAULT_MAP")


@click.command("dashboard", help="Serve the web dashboard (127.0.0.1:9119 by default).")
@click.option("--host", default="127.0.0.1", show_default=True, help="Address to bind.")
@click.option(
    "--port",
    type=click.IntRange(0, 65535),
    default=DEFAULT_PORT,
    show_default=True,
    help="Port to bind (0 = any free one).",
)
@click.option("--no-open", is_flag=True, help="Do not open a browser.")
@click.option("--isolated", is_flag=True, help="Run a separate backend for this profile only.")
@click.pass_context
def dashboard_command(
    ctx: click.Context, host: str, port: int, no_open: bool, isolated: bool
) -> int:
    return run_launch(
        Launch(
            scope=_scope_of(ctx),
            mode="dashboard",
            host=host,
            port=port,
            port_given=_port_given(ctx),
            open_browser=not no_open,
            isolated=isolated,
            interactive=_interactive(ctx),
        )
    )


@click.group(
    "serve",
    invoke_without_command=True,
    help="Run the headless backend (JSON and WebSocket only), or find or start one.",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="Address to bind.")
@click.option(
    "--port",
    type=click.IntRange(0, 65535),
    default=0,
    show_default=True,
    help="Port to bind (0 = any free one).",
)
@click.pass_context
def serve_group(ctx: click.Context, host: str, port: int) -> int | None:
    if ctx.invoked_subcommand is not None:
        return None
    return run_launch(
        Launch(
            scope=_scope_of(ctx),
            mode="serve",
            host=host,
            port=port,
            open_browser=False,
            interactive=_interactive(ctx),
            build=False,
        )
    )


def _profile_home(ctx: click.Context, name: str | None) -> Path:
    from indus.foundation.errors import InvalidProfileId
    from indus.foundation.profiles import is_live, normalize_id, profile_home

    scope = _scope_of(ctx)
    if not name:
        return scope.home
    try:
        pid = normalize_id(name)
    except InvalidProfileId as exc:
        raise click.BadParameter(str(exc), param_hint="--profile") from None
    if not is_live(scope.root, pid):
        raise click.BadParameter(f"no profile named {pid!r}", param_hint="--profile")
    return profile_home(scope.root, pid)


@serve_group.command("discover", help="Find this profile's running headless backend.")
@click.option("--json", "as_json", is_flag=True, help="Print the answer as JSON.")
@click.option("-p", "--profile", "profile", default=None, help="Look for another profile's.")
@click.pass_context
def discover_command(ctx: click.Context, as_json: bool, profile: str | None) -> int:
    from indus.web import serve_runtime

    home = _profile_home(ctx, profile)
    found = serve_runtime.discover(home)
    if found is None:
        if as_json:
            click.echo(json.dumps({"state": "absent", "reason": "not_running"}, sort_keys=True))
        else:
            err("No headless backend runs for this profile; `indus serve ensure` starts one.")
        return 1
    if as_json:
        click.echo(json.dumps(found.to_json(), sort_keys=True))
    else:
        click.echo(
            f"Headless backend on {found.url} (pid {found.pid}, version {found.code_version})."
        )
    return 0


@serve_group.command("ensure", help="Find or start this profile's headless backend.")
@click.option("--json", "as_json", is_flag=True, help="Print the answer as JSON.")
@click.option(
    "--timeout",
    "timeout_s",
    type=click.FloatRange(min=0.0),
    default=30.0,
    show_default=True,
    help="Seconds to wait for a new backend to answer.",
)
@click.pass_context
def ensure_command(ctx: click.Context, as_json: bool, timeout_s: float) -> int:
    from indus.web import serve_runtime

    scope = _scope_of(ctx)
    result = serve_runtime.ensure(scope.home, scope.env.to_dict(), timeout=timeout_s)
    ok = isinstance(result, serve_runtime.Served)
    if as_json:
        click.echo(json.dumps(result.to_json(), sort_keys=True))
    elif isinstance(result, serve_runtime.Served):
        how = "restarted" if result.restarted else ("started" if result.started else "running")
        click.echo(f"Headless backend {how} on {result.url} (pid {result.pid}).")
    else:
        err(f"No headless backend: {result.detail}")
    return 0 if ok else 1
