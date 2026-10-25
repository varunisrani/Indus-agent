"""``indus gateway``: run and look after this profile's messaging gateway (P15-28).

- ``run``: the gateway in the foreground (P15-24; its command is loaded from ``RUN_TARGET``).
- ``start``: through the installed service when there is one, else as a detached
  ``indus gateway run`` writing to ``<home>/logs/gateway-stdio.log``; waits for the control
  socket to answer.
- ``stop``: records the planned stop (pid + start time) *before* signalling, then stops the
  service or sends SIGTERM, and waits. It never kills on its own; ``--force`` sends SIGKILL only
  after the wait ran out. Windows has no SIGTERM to catch: the daemon is asked through its
  control channel's ``stop`` verb, and ``--force`` terminates it.
- ``restart``: the service's restart, or stop then start.
- ``status``: the control socket first, then the runtime status file (stale after 120 s or when
  its process is gone), then the service's state. Exit 0 when running, 1 otherwise.
- ``install`` / ``uninstall``: a systemd user unit (Linux) or a launchd agent (macOS); nothing is
  written outside the user's own config folders and nothing needs root. ``--system`` prints a
  system-wide unit for an administrator to place. Windows has no native service in 1.0 (use
  WSL2, D-16.55).

Shared pieces live in ``indus.service`` (``gateway_control``, ``gateway_units``,
``gateway_manager``) and ``indus.state.gateway_runtime`` (the status file, planned-stop record and
process identity the daemon writes); this module never imports ``indus.gateway`` (D-16.246).
``stop`` and ``restart`` refuse inside the gateway's own process tree (``_INDUS_GATEWAY``): the
agent cannot stop the gateway that hosts it.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from indus.cli.commands._support import err, fresh_view, state_of
from indus.cli.lazy import LazyGroup
from indus.foundation.scope import Scope
from indus.platform import winproc
from indus.service import gateway_control
from indus.service.gateway_manager import ServiceError, ServiceManager, manager_for
from indus.service.gateway_units import UnitSpec, default_program, systemd_unit
from indus.state import gateway_runtime

__all__ = ["RUN_TARGET", "gateway_group", "run_argv"]

RUN_TARGET = "indus.gateway.daemon.command:run_command"
"""The foreground daemon's click command (P15-24). The one place ``run`` is wired."""

_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}
_POLL_S = 0.2
_START_WAIT_S = 30.0

PLATFORM = sys.platform
"""Which service manager applies (tests set it)."""


def _pause(seconds: float) -> None:
    threading.Event().wait(seconds)


def run_argv(scope: Scope) -> list[str]:
    """The command a detached start runs (the unit runs the same one)."""
    return [*default_program(), "gateway", "run"]


@dataclass(frozen=True)
class _Live:
    pid: int
    start_time: int | None
    source: str
    data: dict[str, Any]


def _scope(ctx: click.Context) -> Scope:
    return state_of(ctx).scope


def _spec(scope: Scope) -> UnitSpec:
    view = fresh_view(scope)
    return UnitSpec.build(scope.home, env=scope.env, setting=lambda key, d: view.get(key, d))


def _manager(scope: Scope, spec: UnitSpec | None = None) -> ServiceManager | None:
    return manager_for(spec or _spec(scope), scope.env, platform=PLATFORM)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _find_live(home: Path) -> _Live | None:
    """The gateway serving ``home``: the socket's answer, else a fresh status file."""
    found = gateway_control.identify(home)
    if found is not None:
        return _Live(found["pid"], _int(found.get("start_time")), "socket", found)
    view = gateway_runtime.runtime_view(home)
    if view.liveness == "running" and view.pid is not None:
        return _Live(view.pid, view.start_time, "file", view.data or {})
    return None


def _wait_gone(live: _Live, timeout: float) -> bool:
    waited = 0.0
    while gateway_runtime.alive(live.pid, live.start_time):
        if waited >= timeout:
            return False
        _pause(_POLL_S)
        waited += _POLL_S
    return True


def _wait_answer(
    home: Path,
    timeout: float,
    *,
    not_pid: int | None = None,
    child: subprocess.Popen[bytes] | None = None,
) -> dict[str, Any] | str:
    """The new gateway's identity, or why none came: ``exited:<code>`` / ``timeout``."""
    waited = 0.0
    while True:
        found = gateway_control.identify(home)
        if found is not None and found["pid"] != not_pid:
            return found
        if child is not None and (code := child.poll()) is not None:
            return f"exited:{code}"
        if waited >= timeout:
            return "timeout"
        _pause(_POLL_S)
        waited += _POLL_S


def _spawn(scope: Scope) -> subprocess.Popen[bytes]:
    log_path = gateway_runtime.stdio_log_path(scope.home)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = scope.env.to_dict()
    env["INDUS_HOME"] = str(scope.home)
    env.pop("INDUS_GATEWAY_SUPERVISED", None)
    # Windows: its own process group with no console window, so Ctrl+C here does not reach it.
    flags = winproc.DETACHED_FLAGS if PLATFORM == "win32" else 0
    with open(log_path, "ab") as out:
        return subprocess.Popen(
            run_argv(scope),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=str(scope.home),
            env=env,
            start_new_session=True,
            close_fds=True,
            creationflags=flags,
        )


def _service_label(manager: ServiceManager) -> str:
    return manager.spec.unit_file if manager.kind == "systemd" else manager.spec.label


def _service_usable(manager: ServiceManager | None) -> ServiceManager | None:
    if manager is None or not manager.installed():
        return None
    if not manager.available():
        raise ServiceError(f"the service is installed but `{manager.tool}` is not on PATH")
    return manager


# -- the group ------------------------------------------------------------------------------------


@click.group(
    "gateway",
    cls=LazyGroup,
    registry=(
        ("run", RUN_TARGET, "Run the gateway in the foreground (Ctrl+C stops it)."),
        (
            "ensure",
            "indus.cli.commands.gateway_ensure:ensure_command",
            "Make sure the gateway runs and print where clients attach.",
        ),
        (
            "api-key",
            "indus.cli.commands.gateway_api_key:api_key_group",
            "Create, list and revoke the API server's bearer keys.",
        ),
        (
            "setup",
            "indus.cli.wizards.gateway_setup:setup_command",
            "Set up a messaging platform step by step.",
        ),
        (
            "grant",
            "indus.cli.commands.gateway_grant:grant_command",
            "Print a single-use attach grant for a client of the daemon (used by the TUI).",
        ),
    ),
    help="Run and look after the messaging gateway of this profile.",
    context_settings=_SETTINGS,
)
def gateway_group() -> None:
    pass


@gateway_group.command("start", help="Start the gateway in the background (or its service).")
@click.option(
    "--wait",
    "wait_s",
    type=float,
    default=_START_WAIT_S,
    show_default=True,
    help="Seconds to wait for it to answer.",
)
@click.pass_context
def start_command(ctx: click.Context, wait_s: float) -> int:
    return _start(_scope(ctx), wait_s)


def _start(scope: Scope, wait_s: float) -> int:
    home = scope.home
    live = gateway_control.identify(home)
    if live is not None:
        click.echo(f"The gateway is already running (pid {live['pid']}).")
        return 0
    try:
        service = _service_usable(_manager(scope))
        child = None
        if service is not None:
            service.start()
            how = f"through its service {_service_label(service)}"
        else:
            child = _spawn(scope)
            how = "in the background"
    except (ServiceError, OSError) as exc:
        err(f"Could not start the gateway: {exc}")
        return 1
    got = _wait_answer(home, wait_s, child=child)
    if isinstance(got, dict):
        click.echo(f"Started the gateway {how} (pid {got['pid']}).")
        return 0
    log_path = gateway_runtime.stdio_log_path(home)
    if got.startswith("exited:"):
        err(
            f"The gateway exited (code {got.split(':', 1)[1]}) before it answered.",
            f"See {log_path}",
        )
    else:
        err(
            f"The gateway did not answer within {wait_s:g} s; it may still be starting.",
            "Check with: indus gateway status",
        )
    return 1


@gateway_group.command("stop", help="Stop the gateway (it finishes or hands over running work).")
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=None,
    help="Seconds to wait for it to exit (default: the service's stop budget).",
)
@click.option("--force", is_flag=True, help="Kill it if it is still running after the wait.")
@click.pass_context
def stop_command(ctx: click.Context, timeout_s: float | None, force: bool) -> int:
    scope = _scope(ctx)
    if _inside_gateway(scope):
        return 1
    return _stop(scope, timeout_s, force)


def _inside_gateway(scope: Scope) -> bool:
    """True inside the gateway's own process tree (its agent's terminal)."""
    if scope.env.get(gateway_runtime.GATEWAY_GUARD_ENV, ""):
        err(
            "Refusing: this command runs inside the gateway it would stop.",
            "Run it from a terminal outside the gateway.",
        )
        return True
    return False


def _stop(
    scope: Scope,
    timeout_s: float | None,
    force: bool,
    *,
    quiet: bool = False,
    reason: str = "stop",
) -> int:
    home = scope.home
    spec = _spec(scope)
    budget = float(spec.timeout_stop_s) if timeout_s is None else timeout_s
    live = _find_live(home)
    try:
        service = _service_usable(_manager(scope, spec))
        unit = service.state() if service is not None else None
    except ServiceError as exc:
        err(str(exc))
        return 1
    if live is None and (unit is None or not unit.running):
        if not quiet:
            view = gateway_runtime.runtime_view(home)
            note = f" (its status file is stale: {view.reason})" if view.liveness == "stale" else ""
            click.echo(f"The gateway is not running{note}.")
        return 0
    if live is not None:
        gateway_runtime.record_planned_stop(
            home, live.pid, reason, start_time=live.start_time, by="cli"
        )
    if service is not None and unit is not None and unit.running:
        try:
            service.stop()
        except ServiceError as exc:
            err(f"Could not stop the service: {exc}")
            return 1
    elif live is not None and PLATFORM == "win32":
        asked = gateway_control.query(home, "stop", {"reason": reason})
        if not asked.ok and asked.answered:
            err(f"The gateway refused to stop: {asked.error}.")
            return 1
    elif live is not None:
        try:
            os.kill(live.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            err(f"Not allowed to signal pid {live.pid}.")
            return 1
    if live is not None and not _wait_gone(live, budget):
        if not force:
            err(
                f"The gateway (pid {live.pid}) is still shutting down after {budget:g} s.",
                "Run `indus gateway stop --force` to kill it.",
            )
            return 1
        with contextlib.suppress(ProcessLookupError):
            os.kill(live.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        err(f"Killed the gateway (pid {live.pid}) after {budget:g} s.")
        return 0
    if not quiet:
        click.echo(f"Stopped the gateway{f' (pid {live.pid})' if live else ''}.")
    return 0


@gateway_group.command("restart", help="Restart the gateway.")
@click.option(
    "--wait",
    "wait_s",
    type=float,
    default=_START_WAIT_S,
    show_default=True,
    help="Seconds to wait for it to answer again.",
)
@click.pass_context
def restart_command(ctx: click.Context, wait_s: float) -> int:
    scope = _scope(ctx)
    home = scope.home
    if _inside_gateway(scope):
        return 1
    try:
        service = _service_usable(_manager(scope))
    except ServiceError as exc:
        err(str(exc))
        return 1
    if service is None:
        code = _stop(scope, None, False, quiet=True, reason="restart")
        return code if code != 0 else _start(scope, wait_s)
    live = _find_live(home)
    if live is not None:
        gateway_runtime.record_planned_stop(
            home, live.pid, "restart", start_time=live.start_time, by="cli"
        )
    try:
        service.restart()
    except ServiceError as exc:
        err(f"Could not restart the service: {exc}")
        return 1
    got = _wait_answer(home, wait_s, not_pid=live.pid if live else None)
    if isinstance(got, dict):
        click.echo(f"Restarted the gateway (pid {got['pid']}).")
        return 0
    err(
        f"The gateway did not answer within {wait_s:g} s after the restart.",
        "Check with: indus gateway status",
    )
    return 1


# -- status ---------------------------------------------------------------------------------------


def _platform_lines(platforms: object) -> list[str]:
    if not isinstance(platforms, dict) or not platforms:
        return []
    lines = ["Platforms:"]
    for name, info in sorted(platforms.items()):
        if not isinstance(info, dict):
            continue
        state = str(info.get("state") or "?")
        extra = []
        if info.get("needs_attention"):
            extra.append("needs attention")
        if info.get("error_code"):
            extra.append(str(info["error_code"]))
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"  {name:<12} {state}{suffix}")
    return lines


@gateway_group.command("status", help="Is the gateway running, and how are its platforms?")
@click.option("--json", "as_json", is_flag=True, help="Print JSON for scripts.")
@click.pass_context
def status_command(ctx: click.Context, as_json: bool) -> int:
    scope = _scope(ctx)
    home = scope.home
    report: dict[str, Any] = {"profile": scope.profile_id, "home": str(home)}
    reply = gateway_control.query(home, "status")
    if reply.ok and reply.result is not None:
        report.update(running=True, source="socket", liveness="running", status=reply.result)
    else:
        view = gateway_runtime.runtime_view(home)
        report.update(
            running=view.liveness == "running",
            source="file" if view.liveness != "missing" else None,
            liveness=view.liveness,
            status=view.data,
            reason=view.reason,
        )
        if reply.error == "unsafe_home":
            report["warning"] = "the home folder is writable by others; the socket was not used"
    service: dict[str, Any] | None = None
    try:
        manager = _manager(scope)
        if manager is not None:
            unit = manager.state() if manager.available() else None
            service = {
                "kind": manager.kind,
                "name": _service_label(manager),
                "installed": manager.installed(),
                "active": unit.active if unit is not None else "unknown",
                "main_pid": unit.main_pid if unit is not None else None,
            }
    except ServiceError as exc:
        service = {"error": str(exc)}
    report["service"] = service
    if as_json:
        click.echo(json.dumps(report, indent=2, default=str))
        return 0 if report["running"] else 1
    _print_status(report)
    return 0 if report["running"] else 1


def _print_status(report: dict[str, Any]) -> None:
    status: dict[str, Any] = report.get("status") or {}
    pid = status.get("answering_pid") or status.get("pid")
    if report["running"]:
        via = "answered on the control socket" if report["source"] == "socket" else "status file"
        state = status.get("state") or "running"
        click.echo(f"Gateway ({report['profile']}): {state} (pid {pid}, {via})")
    elif report["liveness"] == "stale":
        click.echo(
            f"Gateway ({report['profile']}): not running — stale status ({report['reason']})"
        )
    elif report["liveness"] == "stopped":
        reason = report.get("reason") or status.get("state") or "stopped"
        click.echo(f"Gateway ({report['profile']}): stopped ({reason})")
    else:
        click.echo(f"Gateway ({report['profile']}): not running")
    if report.get("warning"):
        click.echo(f"Warning: {report['warning']}")
    for line in _platform_lines(status.get("platforms")):
        click.echo(line)
    service = report.get("service")
    if service is None:
        click.echo("Service: none on this system (the desktop app and `gateway start` run it)")
    elif "error" in service:
        click.echo(f"Service: {service['error']}")
    elif not service["installed"]:
        click.echo(f"Service: not installed (indus gateway install sets up {service['name']})")
    else:
        main = f", pid {service['main_pid']}" if service.get("main_pid") else ""
        click.echo(f"Service: {service['name']} — {service['active']}{main}")


# -- install / uninstall --------------------------------------------------------------------------


@gateway_group.command("install", help="Install the gateway as a user service (no root needed).")
@click.option("--force", is_flag=True, help="Replace a different existing definition.")
@click.option("--start", "start_now", is_flag=True, help="Start it right away.")
@click.option(
    "--system",
    is_flag=True,
    help="Print a system-wide unit for an administrator instead (nothing is written).",
)
@click.pass_context
def install_command(ctx: click.Context, force: bool, start_now: bool, system: bool) -> int:
    scope = _scope(ctx)
    spec = _spec(scope)
    if PLATFORM == "win32":
        click.echo(
            "Windows has no native gateway service in this version. Run Indus Agent inside "
            "WSL2 and install the service there, or keep `indus gateway run` open."
        )
        return 1
    if system:
        if not PLATFORM.startswith("linux"):
            err("--system is for Linux (systemd).")
            return 1
        click.echo(f"# {spec.name}.service: place it as root, for example:")
        click.echo(f"#   sudo tee /etc/systemd/system/{spec.unit_file} < this-file")
        click.echo(
            f"#   sudo systemctl daemon-reload && sudo systemctl enable --now {spec.unit_file}"
        )
        click.echo(systemd_unit(spec, system_user=getpass.getuser()), nl=False)
        return 0
    manager = _manager(scope, spec)
    if manager is None:
        err("No supported service manager on this system.")
        return 1
    if not manager.available():
        err(
            f"`{manager.tool}` was not found; the gateway service needs it.",
            "You can still run the gateway with: indus gateway run",
        )
        return 1
    try:
        outcome = manager.install(force=force)
    except ServiceError as exc:
        err(f"Could not install the service: {exc}")
        return 1
    if outcome == "conflict":
        err(
            f"A different definition already exists at {manager.path}.",
            "Run `indus gateway install --force` to replace it.",
        )
        return 1
    name = _service_label(manager)
    if outcome == "unchanged":
        click.echo(f"The service {name} is already installed ({manager.path}).")
    else:
        click.echo(f"Installed the service {name} ({manager.path}).")
    if manager.kind == "systemd":
        click.echo(
            "It starts when you log in. To keep it running after you log out: "
            f"loginctl enable-linger {getpass.getuser()}"
        )
        if start_now:
            return _start(scope, _START_WAIT_S)
        click.echo("Start it now with: indus gateway start")
    return 0


@gateway_group.command("uninstall", help="Stop and remove the gateway's user service.")
@click.pass_context
def uninstall_command(ctx: click.Context) -> int:
    scope = _scope(ctx)
    try:
        manager = _service_usable(_manager(scope))
    except ServiceError as exc:
        err(str(exc))
        return 1
    if manager is None:
        click.echo("No gateway service is installed for this profile.")
        return 0
    live = _find_live(scope.home)
    if live is not None:
        gateway_runtime.record_planned_stop(
            scope.home, live.pid, "uninstall", start_time=live.start_time, by="cli"
        )
    try:
        if manager.state().running:
            manager.stop()
        manager.uninstall()
    except ServiceError as exc:
        err(f"Could not remove the service: {exc}")
        return 1
    click.echo(f"Removed the service ({manager.path}).")
    return 0


@gateway_group.command("mark-stop", hidden=True, help="Record a planned stop (used by the unit).")
@click.option("--pid", type=int, required=True)
@click.pass_context
def mark_stop_command(ctx: click.Context, pid: int) -> int:
    with contextlib.suppress(OSError):
        gateway_runtime.record_planned_stop(
            _scope(ctx).home,
            pid,
            "stop",
            start_time=gateway_runtime.process_start_time(pid),
            by="unit",
        )
    return 0
