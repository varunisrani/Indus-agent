"""``indus egress``: the credential-injection proxy for Docker sandboxes (P24-42).

- ``status [--json]``: on/off, enforcement, the proxy process (pid, address), the CA, the hosts
  whose TLS it opens and the secret names it injects (names only, never values), tokens issued;
- ``enable [--yes]``: turn ``egress.enabled`` on, make the CA and start the proxy (without a
  terminal it needs ``--yes``: it starts a background process);
- ``disable``: turn it off and stop the proxy;
- ``allow <host> [--secret NAME] [--header NAME]``: open TLS for ``host`` (``*.example.com`` for
  subdomains) and, with ``--secret``, inject that secret into the header (default
  ``Authorization``);
- ``serve`` (hidden): the proxy itself, as ``enable`` and the Docker backend start it.

Containers made before a change of posture are not reused: the next sandbox is a new one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import click

from indus.backends.docker import egress
from indus.cli.cmdkit import cli_state, err, group
from indus.cli.commands._support import interactive, scope_with_config, secret_store
from indus.foundation import config
from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope

__all__ = ["egress_group", "status_lines"]


def _scope() -> Scope:
    return scope_with_config(cli_state().scope)


def _raw(scope: Scope) -> dict[str, Any]:
    return config.load(scope).user_raw()


def _section(raw: dict[str, Any]) -> dict[str, Any]:
    section = raw.get("egress")
    if not isinstance(section, dict):
        section = {}
        raw["egress"] = section
    return cast(dict[str, Any], section)


def _save(scope: Scope, raw: Mapping[str, Any]) -> None:
    try:
        config.save(scope, raw, replace=True)
    except IndusError as exc:
        err(f"Not saved: {exc}")
        raise click.exceptions.Exit(1) from None


def _status(scope: Scope) -> dict[str, Any]:
    es = egress.egress_settings(scope)
    folder = scope.home / egress.STATE_DIR
    record = egress.running(scope.home)
    rules = egress.active_rules(scope, es)
    return {
        "enabled": es.enabled,
        "enforce": es.enforce,
        "block_other_hosts": es.block_other_hosts,
        "proxy": (
            {"pid": record.pid, "address": f"{record.host}:{record.port}"}
            if record is not None
            else None
        ),
        "ca": str(folder / "ca.pem") if (folder / "ca.pem").is_file() else None,
        "hosts": sorted({*(r.host for r in rules), *es.hosts}),
        "injects": sorted({f"{r.secret} → {r.host}" for r in rules if r.secret}),
        "tokens": egress.TokenBook(folder).count(),
    }


def status_lines(scope: Scope) -> list[str]:
    info = _status(scope)
    proxy = cast(Mapping[str, Any] | None, info["proxy"])
    lines = [
        f"Egress proxy: {'on' if info['enabled'] else 'off'}"
        f" ({'enforced' if info['enforce'] else 'not enforced'})",
        "Process: "
        + (f"running, pid {proxy['pid']} on {proxy['address']}" if proxy else "not running"),
        f"CA: {info['ca'] or 'not made yet'}",
        f"Other hosts: {'refused' if info['block_other_hosts'] else 'tunnelled, never opened'}",
    ]
    injects = cast(list[str], info["injects"])
    lines.append("Injects: " + (", ".join(injects) if injects else "nothing (no key resolves)"))
    hosts = cast(list[str], info["hosts"])
    lines.append("Opens TLS for: " + (", ".join(hosts) if hosts else "no host"))
    lines.append(f"Container tokens issued: {info['tokens']}")
    return lines


@group("egress", "The credential-injection proxy for Docker sandboxes.")
def egress_group() -> None:
    pass


@egress_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Print the state as JSON.")
def status_command(as_json: bool) -> None:
    """Show whether the proxy is on and what it injects (names only)."""
    scope = _scope()
    if as_json:
        click.echo(json.dumps(_status(scope), indent=2, ensure_ascii=False))
        return
    for line in status_lines(scope):
        click.echo(line)


@egress_group.command("enable")
@click.option("--yes", "-y", is_flag=True, help="Do it without a terminal.")
def enable_command(yes: bool) -> None:
    """Turn the proxy on for this profile's Docker sandboxes and start it."""
    state = cli_state()
    if not yes and not interactive(state):
        err(
            "egress enable starts a background proxy; without a terminal run "
            "`indus egress enable --yes`."
        )
        raise click.exceptions.Exit(1)
    scope = _scope()
    raw = _raw(scope)
    _section(raw)["enabled"] = True
    _save(scope, raw)
    scope = _scope()
    try:
        egress.CertAuthority(scope.home / egress.STATE_DIR).ensure()
        record = egress.start_daemon(scope)
    except IndusError as exc:
        err(f"Egress is on, but the proxy could not start: {exc}")
        if exc.hint:
            err(exc.hint)
        raise click.exceptions.Exit(1) from None
    click.echo(f"Egress proxy on: pid {record.pid} on {record.host}:{record.port}.")
    click.echo("New Docker sandboxes route through it; older containers are not reused.")


@egress_group.command("disable")
def disable_command() -> None:
    """Turn the proxy off and stop it."""
    scope = _scope()
    raw = _raw(scope)
    section = raw.get("egress")
    if isinstance(section, dict) and cast(dict[str, Any], section).get("enabled"):
        cast(dict[str, Any], section)["enabled"] = False
        _save(scope, raw)
    stopped = egress.stop_daemon(scope.home)
    click.echo("Egress proxy off" + (" (stopped)." if stopped else "."))


@egress_group.command("allow")
@click.argument("host")
@click.option("--secret", default="", help="Inject this secret (by name) for the host.")
@click.option("--header", default="Authorization", show_default=True, help="Header to fill.")
def allow_command(host: str, secret: str, header: str) -> None:
    """Open TLS for HOST and optionally inject a secret into one header."""
    name = egress.normalize_host(host)
    if name is None:
        err(f"{host!r} is not a host name (example: api.example.com or *.example.com).")
        raise click.exceptions.Exit(1)
    scope = _scope()
    raw = _raw(scope)
    section = _section(raw)
    if secret:
        rule = {"host": name, "secret": secret.strip(), "header": header.strip()}
        if not egress.check_rule(rule):
            err("--secret must be a variable name and --header a header name.")
            raise click.exceptions.Exit(1)
        existing = section.get("rules")
        rules = cast(list[Any], existing) if isinstance(existing, list) else []
        if rule not in rules:
            rules.append(rule)
        section["rules"] = rules
        what = f"{name}: {rule['secret']} goes into {rule['header']}"
    else:
        existing = section.get("hosts")
        hosts = cast(list[Any], existing) if isinstance(existing, list) else []
        if name not in hosts:
            hosts.append(name)
        section["hosts"] = hosts
        what = f"{name}: TLS opened, nothing injected"
    _save(scope, raw)
    click.echo(f"Allowed {what}.")


@egress_group.command("serve", hidden=True)
@click.option("--home", "home", default="", help="The profile home this proxy serves.")
@click.option("--bind", default="", help="Listen address (default: from egress.bind).")
@click.option("--port", default=-1, type=int, help="Listen port (default: egress.port).")
def serve_command(home: str, bind: str, port: int) -> None:
    """Run the egress proxy in the foreground."""
    base = cli_state().scope
    if home and Path(home).resolve() != base.home.resolve():
        err("--home does not match the selected profile.")
        raise click.exceptions.Exit(2)
    scope = scope_with_config(base)
    es = egress.egress_settings(scope)
    try:
        host = bind or egress.bind_address(scope, es)
    except IndusError as exc:
        err(str(exc))
        raise click.exceptions.Exit(1) from None

    def reload() -> Scope:
        return scope_with_config(base)

    def secret(name: str) -> str | None:
        current = reload()  # a fresh store each time: a changed key is used at once
        return secret_store(current.with_secrets(None)).resolve(name, current.env)

    try:
        asyncio.run(
            egress.serve(
                scope,
                bind=host,
                port=es.port if port < 0 else port,
                reload=reload,
                secret=secret,
            )
        )
    except IndusError as exc:
        err(str(exc))
        raise click.exceptions.Exit(1) from None
    except OSError as exc:
        err(f"the egress proxy could not listen on {host}: {exc.strerror or exc}")
        raise click.exceptions.Exit(1) from None
