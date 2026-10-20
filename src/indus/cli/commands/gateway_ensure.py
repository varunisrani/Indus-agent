"""``indus gateway ensure``: make sure the daemon runs and print where to attach (P17-15).

Prints the credential-free endpoint descriptor ``{ws_url, profile_id, instance_id,
code_version}`` (``--json``: exactly that object) once a daemon answers on the control socket,
starting one first when none runs: through the installed user service when there is one, else
detached (``indus.service.gateway_ensure``). It never installs a service and never prints a
ticket (a client asks the control socket for one per dial).

- ``--no-start``: discovery only; nothing is started or restarted.
- ``--timeout``: seconds to wait for an answer (default 60).

A daemon running another version than this CLI is restarted once (gracefully). Exit codes: 0
ready, 1 not ready (the reason is printed; ``--json`` prints ``{state, reason, detail}``). Inside
the gateway's own process tree it only discovers (it never restarts the daemon that hosts it).
"""

from __future__ import annotations

import json

import click

from indus.cli.commands._support import err, state_of
from indus.cli.commands.gateway import (  # pyright: ignore[reportPrivateUsage]
    _manager,
    _service_usable,
)
from indus.service.gateway_ensure import DEFAULT_TIMEOUT_S, Ensured, Mode, ensure
from indus.service.gateway_manager import ServiceError
from indus.state import gateway_runtime

__all__ = ["ensure_command"]


@click.command(
    "ensure",
    help="Make sure the gateway runs (start it if needed) and print where clients attach.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the endpoint as JSON.")
@click.option(
    "--timeout",
    "timeout_s",
    type=click.FloatRange(min=0.0),
    default=DEFAULT_TIMEOUT_S,
    show_default=True,
    help="Seconds to wait for the gateway to answer.",
)
@click.option("--no-start", is_flag=True, help="Only look for a running gateway.")
@click.pass_context
def ensure_command(ctx: click.Context, as_json: bool, timeout_s: float, no_start: bool) -> int:
    scope = state_of(ctx).scope
    inside = bool(scope.env.get(gateway_runtime.GATEWAY_GUARD_ENV, ""))
    mode: Mode = "discover" if no_start or inside else "start"
    service = None
    if mode == "start":
        try:
            service = _service_usable(_manager(scope))
        except ServiceError as exc:
            err(str(exc))
            return 1
    result = ensure(scope.home, scope.env.to_dict(), mode=mode, timeout=timeout_s, service=service)
    if as_json:
        click.echo(json.dumps(result.to_json(), sort_keys=True))
        return 0 if result.attachable else 1
    return _report(result)


def _report(result: Ensured) -> int:
    endpoint = result.endpoint
    if result.attachable and endpoint is not None:
        how = "started" if result.started else "running"
        if result.restarted:
            how = "restarted (it ran another version)"
        version = endpoint.code_version or "unknown"
        click.echo(f"The gateway is {how} (pid {result.pid}, version {version}).")
        if endpoint.ws_url:
            click.echo(f"Attach at {endpoint.ws_url}")
        else:
            click.echo("Its attach endpoint is not available yet.")
        if result.skew:
            err("Its version differs from this command's; restart it to update.")
        return 0
    lines = {
        "absent": "No gateway runs for this profile.",
        "stopped": "The gateway was stopped on purpose; run `indus gateway ensure` to start it.",
        "starting": "The gateway has not answered yet; it may still be starting.",
    }
    first = lines.get(result.state, f"The gateway is not ready ({result.reason}).")
    err(first, result.detail) if result.detail else err(first)
    return 1
