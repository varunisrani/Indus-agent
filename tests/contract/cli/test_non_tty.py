"""P07-26: every command behaves without a terminal (M2, R04.19).

Each visible command and subcommand runs with stdin and stdout not terminals and no further
arguments in a fresh profile. It must finish within the watchdog, never read stdin, never raise,
and exit with the code ``non_tty_table.yaml`` declares (0 informational, 1 guidance, 2 usage).
The table must name exactly the commands the app registers.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.state import registry
from tests.contract.cli._walk import CommandPath, commands, key, load_table, run_without_terminal

TABLE = load_table()


@pytest.fixture
def fresh(scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Scope]:
    """A fresh profile whose HOME (the user's folder) and working dir are temp folders too."""
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    loader.reset()
    overlay = {"HOME": str(user_home), "USERPROFILE": str(user_home), "SHELL": "/bin/bash"}
    yield scope.with_env(scope.env.with_overlay(overlay))
    registry.close_all()
    loader.reset()


def test_table_names_every_command_and_nothing_else() -> None:
    registered = {key(path) for path in commands()}
    listed = set(TABLE.commands)
    assert registered - listed == set(), "commands without a row in non_tty_table.yaml"
    assert listed - registered == set(), "rows for commands the app does not register"


def _check(fresh: Scope, path: CommandPath, expected: int) -> None:
    outcome = run_without_terminal(fresh, path)
    shown = f"`indus {key(path)}`\nstdout:\n{outcome.out}\nstderr:\n{outcome.err}"
    assert outcome.error is None, f"{shown}\nraised {outcome.error!r}"
    assert not outcome.read_stdin, f"{shown}\nread stdin without a terminal"
    assert outcome.code == expected, f"{shown}\nexit {outcome.code}, the table says {expected}"
    assert "Traceback" not in outcome.out + outcome.err, shown
    if expected == 1:
        assert (outcome.out + outcome.err).strip(), f"{shown}\nexit 1 without any guidance"


@pytest.mark.parametrize(("name", "expected"), sorted(TABLE.commands.items()))
def test_without_arguments(fresh: Scope, name: str, expected: int) -> None:
    _check(fresh, tuple(name.split()), expected)


@pytest.mark.parametrize(("argv", "expected"), sorted(TABLE.with_arguments.items()))
def test_guidance_with_arguments(fresh: Scope, argv: str, expected: int) -> None:
    _check(fresh, tuple(argv.split()), expected)
