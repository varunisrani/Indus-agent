"""The ``indus acp`` click command (P19-01), loaded by name from the CLI's registry.

It lives beside the adapter (as ``indus tui-bridge`` does) because ``indus.cli`` and ``indus.acp``
are independent siblings in the layer contract.
"""

from __future__ import annotations

from typing import Any

import click

from indus.foundation.scope import Scope

__all__ = ["acp_command"]


@click.command(
    "acp",
    help=(
        "Run Indus Agent inside an editor that speaks the Agent Client Protocol (Zed, VS Code, "
        "JetBrains). The editor starts this command; stdin and stdout carry the protocol. By "
        "default sessions run in the profile's gateway daemon; --local runs them in this process."
    ),
)
@click.option("--local", is_flag=True, help="Run sessions in this process instead of the daemon.")
@click.option("--version", "show_version", is_flag=True, help="Print the version and exit.")
@click.option("--check", is_flag=True, help="Check that the ACP support is installed, then exit.")
@click.option("--setup", is_flag=True, help="Set up a provider and model in this terminal.")
@click.option("--setup-browser", is_flag=True, help="Install the browser tools, then exit.")
@click.option("--yes", "-y", is_flag=True, help="With --setup-browser: accept the downloads.")
@click.pass_context
def acp_command(
    ctx: click.Context,
    local: bool,
    show_version: bool,
    check: bool,
    setup: bool,
    setup_browser: bool,
    yes: bool,
) -> int:
    state: Any = ctx.obj
    scope = getattr(state, "scope", None)
    if not isinstance(scope, Scope):
        raise click.UsageError("acp was started without the process scope")
    from indus.acp.entry import Options, run

    return run(
        scope,
        Options(
            local=local,
            version=show_version,
            check=check,
            setup=setup,
            setup_browser=setup_browser,
            yes=yes,
        ),
    )
