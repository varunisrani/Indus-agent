"""The hidden ``indus tui-bridge`` subcommand (P12-28): the terminal UI spawns it as its backend."""

from __future__ import annotations

from typing import Any

import click

from indus.foundation import log as logsetup
from indus.foundation.scope import Scope

__all__ = ["tui_bridge_command"]


@click.command(
    "tui-bridge",
    hidden=True,
    help="Serve the terminal UI over stdin/stdout (started by `indus --tui`, not by people).",
)
@click.option("--stdio", "stdio", is_flag=True, help="Speak JSON-RPC over stdin and stdout.")
@click.pass_context
def tui_bridge_command(ctx: click.Context, stdio: bool) -> int:
    """Run the bridge until the terminal UI closes its end."""
    if not stdio:
        click.echo("indus tui-bridge: only --stdio is supported", err=True)
        return 2
    state: Any = ctx.obj
    scope = getattr(state, "scope", None)
    if not isinstance(scope, Scope):
        raise click.UsageError("tui-bridge was started without the process scope")
    logsetup.setup(scope, "stdio")
    from indus.tui_bridge.stdio_server import run

    return run(scope)
