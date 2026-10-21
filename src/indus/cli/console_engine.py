"""``indus console``: the admin console in a terminal (P24-36).

The engine — the curated command list, the refusals, confirmation, limits — is
``indus.service.console``, shared with the dashboard's ``/api/console`` socket (``indus.web``
may not import ``indus.cli``). This module is the local REPL over it:

- in a terminal: a prompt; a confirm-first command asks ``[y/N]``; ``exit``, ``quit`` or end of
  input leaves (exit 0);
- ``indus console --run '<command>'`` runs one line and exits (1 when it failed or was refused);
  a confirm-first command needs a terminal to confirm, else it is not run (exit 1);
- without a terminal and without ``--run`` it says how to use ``--run`` and exits 1 (R04.19).
"""

from __future__ import annotations

import sys
from typing import TextIO

import click

from indus.service.console import PROMPT, ConsoleEngine, ConsoleResult

__all__ = ["console_command", "run_repl"]


def _show(result: ConsoleResult, stdout: TextIO, stderr: TextIO) -> None:
    if result.output:
        print(result.output, file=stdout)
    if result.errors:
        print(result.errors, file=stderr)
    if result.status == "error" and result.message:
        print(result.message, file=stderr)


def _ask(question: str, stdin: TextIO, stdout: TextIO) -> bool:
    """A yes/no question on the terminal; only ``y`` or ``yes`` is a yes."""
    stdout.write(f"{question} [y/N] ")
    stdout.flush()
    return stdin.readline().strip().lower() in ("y", "yes")


def run_repl(
    engine: ConsoleEngine,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    interactive: bool,
) -> int:
    """Read lines until ``exit`` or end of input; without a terminal a confirm-first command
    stops the run with exit 1."""
    if interactive:
        stdout.write("Indus console. `help` lists the commands, `exit` leaves.\n")
    while True:
        if interactive:
            stdout.write(PROMPT)
            stdout.flush()
        line = stdin.readline()
        if not line:
            return 0
        result = engine.execute(line)
        if result.status == "confirm_required":
            if not interactive:
                stderr.write(f"{result.message} (needs a terminal to confirm)\n")
                return 1
            if _ask(result.message, stdin, stdout):
                result = engine.execute(result.command, confirmed=True)
            else:
                stdout.write("Not run.\n")
                continue
        if result.status == "exit":
            _show(result, stdout, stderr)
            return 0
        if result.status == "clear":
            stdout.write("\033[2J\033[H")
            stdout.flush()
        else:
            _show(result, stdout, stderr)


@click.command("console")
@click.option(
    "--run",
    "one",
    default=None,
    metavar="LINE",
    help="Run one console command and exit.",
)
def console_command(one: str | None) -> None:
    """The admin console: curated indus commands, one per line."""
    from indus.cli.cmdkit import cli_state

    state = cli_state()
    engine = ConsoleEngine(state.scope)
    tty = state.stdin_isatty() and state.stdout_isatty()
    if one is not None:
        result = engine.execute(one)
        if result.status == "confirm_required":
            if not tty or not click.confirm(result.message, default=False):
                click.echo(f"{result.message} Not run.", err=True)
                raise click.exceptions.Exit(1)
            result = engine.execute(one, confirmed=True)
        if result.output:
            click.echo(result.output)
        if result.errors:
            click.echo(result.errors, err=True)
        if result.status == "error":
            if result.message:
                click.echo(result.message, err=True)
            raise click.exceptions.Exit(1)
        return
    if not tty:
        click.echo(
            "indus console needs a terminal. Run one command with: indus console --run '<command>'",
            err=True,
        )
        raise click.exceptions.Exit(1)
    code = run_repl(engine, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, interactive=True)
    if code:
        raise click.exceptions.Exit(code)
