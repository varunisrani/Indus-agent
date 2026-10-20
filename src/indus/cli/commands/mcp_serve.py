"""``indus mcp serve``: run Indus Agent as an MCP server over stdio (P19-14).

An MCP client starts it as a child process (for example ``{"command": "indus", "args": ["mcp",
"serve"]}``) and talks MCP over its stdin and stdout. The server itself lives in
``indus.mcp_serve`` and is loaded by name (``SERVER_TARGET``): a surface never imports another
(layer rule D-16.246), as with ``tui-bridge`` and ``gateway run``.

The CLI entry starts this command in the stdio logging mode, so stdout carries MCP frames only;
diagnostics go to stderr at WARNING (``--verbose``: DEBUG). Without the MCP SDK it prints how to
install it and exits 1.
"""

from __future__ import annotations

import importlib
import sys

import click

from indus.cli.cmdkit import cli_state

__all__ = ["SERVER_TARGET", "missing_sdk_hint", "serve_command"]

SERVER_TARGET = "indus.mcp_serve.server:serve"
"""``module:attr`` of the server's entry, ``serve(scope, *, verbose) -> int``."""


def missing_sdk_hint() -> str:
    return (
        "indus mcp serve needs the MCP SDK (the `mcp` package), which this install lacks.\n"
        f"Install it with: {sys.executable} -m pip install 'mcp'  (or reinstall Indus Agent)"
    )


def _sdk_present() -> bool:
    try:
        importlib.import_module("mcp.server")
    except ImportError:
        return False
    return True


@click.command("serve")
@click.option("--verbose", is_flag=True, help="Log at DEBUG level (to stderr).")
def serve_command(verbose: bool) -> int:
    """Serve this profile's messaging conversations to an MCP client over stdio."""
    scope = cli_state().scope
    if not _sdk_present():
        click.echo(missing_sdk_hint(), err=True)
        raise click.exceptions.Exit(1)
    module_name, _, attr = SERVER_TARGET.partition(":")
    serve = getattr(importlib.import_module(module_name), attr)
    return int(serve(scope, verbose=verbose))
