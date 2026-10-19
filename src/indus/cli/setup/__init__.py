"""``indus setup``: the guided first-time setup (wizard in ``wizard``, sections in ``sections``).

Only the click command lives here so ``indus setup --help`` imports nothing heavy.
"""

from __future__ import annotations

import click

__all__ = ["setup_command"]


@click.command(
    "setup",
    help="Set up Indus Agent step by step: model and key, terminal, tools, agent defaults. "
    "SECTION (model, terminal, tools, agent, gateway) runs only that part.",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.argument("section", required=False, metavar="[SECTION]")
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Do not ask anything: print how to set things up with commands instead.",
)
@click.option(
    "--reset",
    is_flag=True,
    help="Start from the built-in settings (the current file is backed up first).",
)
@click.option(
    "--quick", is_flag=True, help="On a set-up install, ask only about what is still missing."
)
@click.option(
    "--terminal-backend",
    "terminal_backend",
    metavar="NAME",
    default=None,
    help="Select this terminal backend without asking; its credentials are read from the "
    "environment and kept in the secret store.",
)
@click.pass_context
def setup_command(
    ctx: click.Context,
    section: str | None,
    non_interactive: bool,
    reset: bool,
    quick: bool,
    terminal_backend: str | None,
) -> int:
    from indus.cli.args import CliState
    from indus.cli.setup.wizard import SetupOptions, run_setup

    state = ctx.find_object(CliState)
    if state is None:
        raise click.UsageError("setup was started without its process state")
    options = SetupOptions(
        section=section,
        non_interactive=non_interactive,
        reset=reset,
        quick=quick,
        terminal_backend=terminal_backend,
    )
    return run_setup(state, options)
