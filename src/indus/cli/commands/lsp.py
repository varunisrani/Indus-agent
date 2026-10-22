"""``indus lsp``: the language servers that check edits (P24-15).

- ``status [--json]`` (also bare): whether LSP is on, the settings, the servers running in this
  process (when it hosts any), the ones that failed or were skipped as untrusted, and the
  catalogue with which binaries are installed on the PATH children get;
- ``install-hint SERVER``: how to install a server (nothing is ever installed for you);
- ``restart``: stop the servers and forget their failures (the next edit starts them again): the
  profile's running gateway daemon first, where attached sessions run (the owner-only control verb
  ``lsp-restart``, ``service.lsp_control``, wave 45), then this process's own, if any.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import click

from indus.cli.cmdkit import cli_state, config_of, err, group
from indus.foundation.scope import Scope
from indus.safety.child_env import child_env

__all__ = ["hint_lines", "lsp_group", "restart_lines", "status_lines", "status_payload"]

_MARK = {True: "+", False: "-"}


def _scoped(scope: Scope) -> Scope:
    return scope if scope.config is not None else scope.with_config(config_of(scope))


def _active_manager(scope: Scope | None) -> Any:
    """The manager of ``scope``'s tier in this process (None: the bound one), when a host
    installed one (never imported otherwise; one tier per profile, SEC-14)."""
    if "indus.tools.lsp.diagnostics" not in sys.modules:
        return None
    from indus.tools.lsp import diagnostics

    checker = diagnostics.active(diagnostics.tier_key(scope))
    return checker.manager if checker is not None else None


def status_payload(scope: Scope) -> dict[str, Any]:
    from indus.tools.lsp.manager import LspSettings, describe_servers

    scoped = _scoped(scope)
    settings = LspSettings.from_scope(scoped)
    search_path = child_env(scoped).get("PATH")
    manager = _active_manager(scope)
    running = manager.status().as_dict() if manager is not None else None
    return {
        "enabled": settings.enabled,
        "wait_timeout": settings.wait_timeout,
        "idle_timeout": settings.idle_timeout,
        "trusted_workspaces": list(settings.trusted_workspaces),
        "process": running,
        "servers": describe_servers(settings.servers, search_path),
    }


def status_lines(payload: dict[str, Any]) -> list[str]:
    lines = [
        f"Language servers: {'on' if payload['enabled'] else 'off'}",
        f"  wait for a verdict: {payload['wait_timeout']:g} s; idle servers stop after "
        + (f"{payload['idle_timeout']:g} s" if payload["idle_timeout"] else "never"),
    ]
    if payload["trusted_workspaces"]:
        lines.append("  trusted workspaces: " + ", ".join(payload["trusted_workspaces"]))
    running = payload["process"]
    if running is None:
        lines.append("  no language servers run in this process")
    else:
        clients = running["clients"]
        lines.append(f"  running here: {len(clients) or 'none'}")
        lines += [f"    {c['server']:<26} {c['state']:<8} {c['root']}" for c in clients]
        for item in running["broken"]:
            lines.append(f"  unavailable: {item['server']} for {item['root']}: {item['reason']}")
        for item in running["untrusted"]:
            lines.append(
                f"  skipped (untrusted): {item['server']} for {item['root']} "
                "(add the folder to lsp.trusted_workspaces)"
            )
    lines += ["", "Servers (+ installed, - missing):"]
    for row in payload["servers"]:
        mark = "x" if row["disabled"] else _MARK[bool(row["installed"])]
        note = " [disabled]" if row["disabled"] else ""
        if row["trusted_only"]:
            note += " [trusted workspaces only]"
        handles = ", ".join(row["handles"][:6]) + (" ..." if len(row["handles"]) > 6 else "")
        lines.append(f"  {mark} {row['server']:<26} {handles}{note}")
    lines.append("")
    lines.append("`indus lsp install-hint SERVER` shows how to install one.")
    return lines


def hint_lines(scope: Scope, server: str) -> tuple[int, list[str]]:
    from indus.tools.lsp.manager import LspSettings

    settings = LspSettings.from_scope(_scoped(scope))
    spec = next((s for s in settings.servers if s.server_id == server), None)
    if spec is None:
        known = ", ".join(s.server_id for s in settings.servers)
        return 1, [f"Unknown language server {server!r}. Known: {known}"]
    lines = [f"{spec.label or spec.server_id}:", f"  {spec.install_hint or 'no hint known'}"]
    if spec.binaries:
        lines.append("  looked for on the PATH: " + ", ".join(spec.binaries))
    lines.append("Indus Agent never installs language servers itself.")
    return 0, lines


def restart_lines(scope: Scope, *, query: Any = None) -> list[str]:
    from indus.service import lsp_control

    lines: list[str] = []
    daemon = lsp_control.restart_in_gateway(scope.home, query=query)
    if daemon is not None:
        stopped, running = daemon
        lines.append(
            f"Stopped {stopped} language server(s) in the gateway; the next edit starts them again."
            if running
            else "The gateway runs no language servers for this profile now; a session's first "
            "edit there starts them."
        )
    manager = _active_manager(scope)
    if manager is not None:
        count = asyncio.run(manager.restart())
        lines.append(f"Stopped {count} language server(s); the next edit starts them again.")
    if not lines:
        lines.append(
            "No language servers run in this process. A running session starts its own and "
            "stops them when it ends or after the idle timeout."
        )
    return lines


@group(
    "lsp",
    "Language servers that check edits: status, install hints, restart.",
    short_help="Language servers that check edits.",
    invoke_without_command=True,
)
@click.pass_context
def lsp_group(ctx: click.Context) -> int:
    if ctx.invoked_subcommand is None:
        for line in status_lines(status_payload(cli_state().scope)):
            click.echo(line)
    return 0


@lsp_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def status_command(as_json: bool) -> int:
    """Settings, running servers and the catalogue."""
    payload = status_payload(cli_state().scope)
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for line in status_lines(payload):
        click.echo(line)
    return 0


@lsp_group.command("install-hint")
@click.argument("server")
def install_hint_command(server: str) -> int:
    """How to install a language server."""
    code, lines = hint_lines(cli_state().scope, server)
    for line in lines:
        (err if code else click.echo)(line)
    return code


@lsp_group.command("restart")
def restart_command() -> int:
    """Stop the language servers (the gateway's, then this process's) and forget their failures."""
    for line in restart_lines(cli_state().scope):
        click.echo(line)
    return 0
