"""P24-36: the admin console engine — curated commands, refusals, confirmation, limits, REPL."""

from __future__ import annotations

import io
import os
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import click
import pytest

from indus.cli import app as app_mod
from indus.cli.console_engine import run_repl
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.service.console import (
    COMMANDS,
    CancelToken,
    ConsoleCommand,
    ConsoleEngine,
    RunOutput,
    SubprocessRunner,
)


def _cli_paths() -> set[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()

    def walk(group: click.Group, prefix: tuple[str, ...]) -> None:
        ctx = click.Context(group)
        for name in group.list_commands(ctx):
            command = group.get_command(ctx, name)
            if command is None:
                continue
            path = (*prefix, name)
            found.add(path)
            if isinstance(command, click.Group):
                walk(command, path)

    walk(app_mod.cli, ())
    return found


_CLOCK = SystemClock()


def _echo(text: str) -> ConsoleCommand:
    return ConsoleCommand(("say",), "say", "test", handler=lambda _e, _a, _t: text)


def test_every_curated_path_is_a_real_command() -> None:
    paths = _cli_paths()
    missing = [" ".join(c.path) for c in COMMANDS if c.path not in paths]
    assert missing == []
    # a command never appears twice and every mutating one asks a question
    assert len({c.path for c in COMMANDS}) == len(COMMANDS)
    assert all(c.confirm.endswith("?") for c in COMMANDS if c.mutating)


@pytest.mark.parametrize(
    "line",
    [
        "status | cat",
        "status || true",
        "status && doctor",
        "status ; doctor",
        "status > out.txt",
        "status >> out.txt",
        "logs < input",
        "status &",
        "status $(whoami)",
        "status `whoami`",
    ],
)
def test_shell_syntax_refused(scope: Scope, line: str) -> None:
    result = ConsoleEngine(scope).execute(line)
    assert result.status == "error"
    assert "shell syntax" in result.message


def test_unknown_command_suggests(scope: Scope) -> None:
    engine = ConsoleEngine(scope)
    result = engine.execute("sessions lst")
    assert result.status == "error"
    assert "sessions list" in result.message
    flag = engine.execute("--profile other status")
    assert flag.status == "error" and "not available" in flag.message
    gone = engine.execute("gateway run")
    assert gone.status == "error" and "not a console command" in gone.message


def test_refused_arguments(scope: Scope) -> None:
    engine = ConsoleEngine(scope)
    for line in ("logs -f", "logs --follow", "doctor --fix"):
        result = engine.execute(line)
        assert result.status == "error", line
        assert "not available" in result.message


def test_mutating_needs_confirmation(scope: Scope) -> None:
    ran: list[list[str]] = []
    engine = ConsoleEngine(scope)
    engine.register(
        ConsoleCommand(
            ("config", "set"),
            "config set <k> <v>",
            "x",
            confirm="Change this setting?",
            handler=lambda _e, args, _t: ran.append(args) or "done",
        )
    )
    first = engine.execute("config set agent.max_turns 3")
    assert first.status == "confirm_required"
    assert first.message == "Change this setting?"
    assert first.command == "config set agent.max_turns 3"
    assert ran == []
    second = engine.execute(first.command, confirmed=True)
    assert second.status == "ok" and second.output == "done"
    assert ran == [["agent.max_turns", "3"]]


def test_leading_indus_word_and_empty(scope: Scope) -> None:
    engine = ConsoleEngine(scope)
    engine.register(_echo("hi"))
    assert engine.execute("indus say").output == "hi"
    assert engine.execute("   ").status == "ok"
    assert engine.execute('say "unclosed').message.startswith("Could not read the line")


def test_output_cap_marker(scope: Scope) -> None:
    engine = ConsoleEngine(scope)
    engine.register(_echo("x" * 50_001))
    result = engine.execute("say")
    assert result.status == "ok"
    assert result.output.startswith("x" * 50_000)
    assert result.output.endswith("1 more characters were left out")
    exact = ConsoleEngine(scope)
    exact.register(_echo("y" * 50_000))
    assert exact.execute("say").output == "y" * 50_000


def test_output_is_masked(scope: Scope) -> None:
    fake = "sk-" + "a1B2" * 10
    engine = ConsoleEngine(scope)
    engine.register(_echo(f"key {fake}"))
    assert fake not in engine.execute("say").output


def test_builtins(scope: Scope) -> None:
    engine = ConsoleEngine(scope)
    help_text = engine.execute("help").output
    assert "config set <key> <value>" in help_text and "* asks before it runs" in help_text
    assert "asks before it runs" in engine.execute("help cron pause").output
    assert engine.execute("help nonsense").status == "error"
    assert engine.execute("history").output == "No history yet."
    assert engine.execute("clear").status == "clear"
    assert engine.execute("version").output.startswith("Indus Agent ")
    assert engine.execute("history").output.splitlines() == ["1: clear", "2: version"]
    assert engine.execute("exit").status == "exit"
    assert engine.execute("quit").status == "exit"


class FakeRunner:
    def __init__(self, output: RunOutput) -> None:
        self.output = output
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, scope: Scope, words: Sequence[str], token: CancelToken) -> RunOutput:
        self.calls.append((str(scope.home), list(words)))
        return self.output


def test_commands_run_for_the_engine_scope(scope: Scope) -> None:
    runner = FakeRunner(RunOutput(0, "the path\n", ""))
    result = ConsoleEngine(scope, runner=runner).execute("indus config path")
    assert result.status == "ok" and result.output == "the path"
    assert runner.calls == [(str(scope.home), ["config", "path"])]
    failing = FakeRunner(RunOutput(2, "", "no such job\n"))
    bad = ConsoleEngine(scope, runner=failing).execute("cron runs nope")
    assert bad.status == "error" and "exit code 2" in bad.message and bad.errors == "no such job"


def test_real_command_runs_as_its_own_process(scope: Scope) -> None:
    result = ConsoleEngine(scope).execute("config path")
    assert result.status == "ok", (result.message, result.errors)
    assert str(scope.home) in result.output


def test_cancel_kills_the_command_and_what_it_started(scope: Scope, tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {marker}\nwait\n", encoding="utf-8")
    fake_python.chmod(0o755)
    token = CancelToken()
    out: list[RunOutput] = []
    worker = threading.Thread(
        target=lambda: out.append(SubprocessRunner(str(fake_python))(scope, ["cron", "run"], token))
    )
    started = time.monotonic()
    worker.start()
    deadline = time.monotonic() + 10
    while not marker.exists() or not marker.read_text().strip():
        assert time.monotonic() < deadline, "the fake command never started"
        _CLOCK.sleep_sync(0.02)
    child = int(marker.read_text().strip())
    token.cancel()
    worker.join(10)
    assert not worker.is_alive()
    assert out and out[0].cancelled
    assert time.monotonic() - started < 10
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        _CLOCK.sleep_sync(0.02)
    assert not _alive(child), "the grandchild survived the cancel"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and ") Z" in stat.read_text())


def test_cancelled_run_is_reported(scope: Scope) -> None:
    runner = FakeRunner(RunOutput(None, "partial", "", cancelled=True))
    result = ConsoleEngine(scope, runner=runner).execute("status")
    assert result.status == "error" and result.message.startswith("Cancelled")


def test_repl_non_tty_refuses_mutating(scope: Scope) -> None:
    out, err = io.StringIO(), io.StringIO()
    engine = ConsoleEngine(scope)
    code = run_repl(
        engine,
        stdin=io.StringIO("version\nconfig set a.b 1\nversion\n"),
        stdout=out,
        stderr=err,
        interactive=False,
    )
    assert code == 1
    assert out.getvalue().count("Indus Agent") == 1
    assert "needs a terminal" in err.getvalue()


def test_repl_interactive_confirm_and_exit(scope: Scope) -> None:
    ran: list[str] = []
    engine = ConsoleEngine(scope)
    engine.register(
        ConsoleCommand(
            ("cron", "pause"),
            "cron pause <job>",
            "x",
            confirm="Pause this job?",
            handler=lambda _e, a, _t: ran.append(a[0]) or "paused",
        )
    )
    out = io.StringIO()
    code = run_repl(
        engine,
        stdin=io.StringIO("cron pause j1\nn\ncron pause j2\ny\nexit\n"),
        stdout=out,
        stderr=io.StringIO(),
        interactive=True,
    )
    assert code == 0
    assert ran == ["j2"]
    assert "Not run." in out.getvalue() and "paused" in out.getvalue()
