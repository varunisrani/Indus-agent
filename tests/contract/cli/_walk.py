"""Shared bits of the P07-26 contract: walk the command tree, run a command with no terminal.

``commands()`` lists every visible command and subcommand of the ``indus`` app as a path of
words (``("auth", "add")``). ``run_without_terminal`` runs one path with stdin and stdout not
terminals, no further arguments and a stdin that trips when anything reads it, under a watchdog.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

import click
from click.testing import CliRunner

from indus.cli import app as app_mod
from indus.cli.args import CliState
from indus.foundation import yamlio
from indus.foundation.scope import Scope

TABLE = Path(__file__).with_name("non_tty_table.yaml")
WATCHDOG_SECONDS = 5.0

type CommandPath = tuple[str, ...]


def walk(
    group: click.Group, prefix: CommandPath = ()
) -> Iterator[tuple[CommandPath, click.Command]]:
    ctx = click.Context(group, info_name=" ".join(("indus", *prefix)))
    for name in group.list_commands(ctx):
        command = group.get_command(ctx, name)
        if command is None or command.hidden:
            continue
        path = (*prefix, name)
        yield path, command
        if isinstance(command, click.Group):
            yield from walk(command, path)


def commands() -> list[CommandPath]:
    return [path for path, _ in walk(app_mod.cli)]


def key(path: CommandPath) -> str:
    return " ".join(path)


@dataclass(frozen=True)
class Table:
    """``commands``: command path → exit code with no arguments; ``with_arguments``: argv → code;
    ``long_running``: commands that run until stopped (only their ``--help`` is run)."""

    commands: dict[str, int]
    with_arguments: dict[str, int]
    long_running: frozenset[str] = frozenset()


def _codes(raw: object, section: str) -> dict[str, int]:
    assert isinstance(raw, dict), f"{TABLE.name}: `{section}` must map a command to an exit code"
    out: dict[str, int] = {}
    for name, code in cast(dict[object, object], raw).items():
        assert isinstance(code, int) and code in (0, 1, 2), f"{section}: {name!r} → {code!r}"
        out[str(name)] = code
    return out


def load_table() -> Table:
    raw = yamlio.load_safe(TABLE.read_bytes())
    assert isinstance(raw, dict), f"{TABLE} must hold `commands` and `with_arguments`"
    doc = cast(dict[str, object], raw)
    loops = doc.get("long_running") or []
    assert isinstance(loops, list), f"{TABLE.name}: `long_running` must list command paths"
    return Table(
        commands=_codes(doc.get("commands"), "commands"),
        with_arguments=_codes(doc.get("with_arguments") or {}, "with_arguments"),
        long_running=frozenset(str(name) for name in cast(list[object], loops)),
    )


class Tripwire(io.RawIOBase):
    """A stdin that records any read: a command run without a terminal must not wait for input."""

    def __init__(self) -> None:
        super().__init__()
        self.touched = False

    def readable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def readinto(self, buffer: memoryview | bytearray) -> int:  # type: ignore[override]
        if len(buffer) == 0:
            return 0
        self.touched = True
        return 0  # end of input: a prompt that slipped through fails fast instead of hanging


@dataclass
class Outcome:
    code: int | None
    out: str
    err: str
    read_stdin: bool
    error: BaseException | None


def run_without_terminal(scope: Scope, path: CommandPath) -> Outcome:
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    stdin = Tripwire()
    result: dict[str, object] = {}

    def target() -> None:
        try:
            result["code"] = app_mod.dispatch(list(path), state)
        except BaseException as exc:
            result["error"] = exc

    with CliRunner().isolation(input=cast(IO[Any], stdin)) as (out, err, _mixed):
        worker = threading.Thread(target=target, name=f"non-tty {key(path)}", daemon=True)
        worker.start()
        worker.join(WATCHDOG_SECONDS)
        hung = worker.is_alive()
        captured_out = out.getvalue().decode(errors="replace")
        captured_err = err.getvalue().decode(errors="replace")
    if hung:
        raise AssertionError(
            f"`indus {key(path)}` still running after {WATCHDOG_SECONDS:.0f} s without a "
            f"terminal\nstdout:\n{captured_out}\nstderr:\n{captured_err}"
        )
    code = result.get("code")
    error = result.get("error")
    return Outcome(
        code=code if isinstance(code, int) else None,
        out=captured_out,
        err=captured_err,
        read_stdin=stdin.touched,
        error=error if isinstance(error, BaseException) else None,
    )
