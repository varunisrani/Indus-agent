"""``indus gateway run``: the gateway in the foreground (P15-24).

The ``indus gateway`` group (P15-28) registers this command by its ``"module:attr"`` string,
``indus.gateway.daemon.command:run_command``, so the CLI never imports the gateway (D-16.246).
The exit code is the daemon's: 0 clean, 75 restart me, 78 fatal configuration.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import click

from indus.foundation import log as logsetup
from indus.foundation.scope import Scope

__all__ = ["main", "run_command"]


def main(scope: Scope, *, replace: bool = False) -> int:
    """Run the daemon for ``scope`` on a fresh event loop; the exit code."""
    from indus.gateway.daemon.process import DaemonOptions, GatewayDaemon

    daemon = GatewayDaemon(scope, options=DaemonOptions(replace=replace))
    code = asyncio.run(daemon.run())
    verdict = daemon.attach_verdict
    if verdict is not None:  # a multiplexing host gateway serves (or refused) this profile
        print(f"indus gateway: {verdict.message}", file=sys.stderr)
    elif daemon.holder is not None and code in (0, 75):
        print(
            f"indus gateway: already running for this profile (pid {daemon.holder.pid})",
            file=sys.stderr,
        )
    return code


@click.command(
    "run",
    help="Run the messaging gateway in the foreground (one per profile, or one serving every "
    "profile with gateway.multiplex_profiles).",
)
@click.option(
    "--replace",
    is_flag=True,
    help="Stop a gateway already serving this profile and take over.",
)
@click.pass_context
def run_command(ctx: click.Context, replace: bool) -> None:
    state: Any = ctx.obj
    scope = getattr(state, "scope", None)
    if not isinstance(scope, Scope):
        raise click.UsageError("gateway run was started without the process scope")
    logsetup.setup(scope, "gateway")
    ctx.exit(main(scope, replace=replace))
