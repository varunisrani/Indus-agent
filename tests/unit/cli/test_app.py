"""P06-02: the click app, lazy commands, global flags and the unknown-command answer."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from indus.cli import app as app_mod
from indus.cli import chat as chat_mod
from indus.cli.args import (
    BARE_CONTINUE,
    CHAT_VALUE_FLAGS,
    FALLBACK_VALUE_FLAGS,
    ChatArgs,
    CliState,
    GlobalArgs,
)
from indus.cli.lazy import LazyGroup
from indus.cli.unknown import unknown_command_lines
from indus.foundation.scope import Scope

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[GlobalArgs, ChatArgs]]]:
    seen: list[tuple[GlobalArgs, ChatArgs]] = []

    def fake_run_chat(state: CliState, args: GlobalArgs, chat: ChatArgs) -> int:
        seen.append((args, chat))
        return 0

    monkeypatch.setattr(chat_mod, "run_chat", fake_run_chat)
    yield seen


def run(scope: Scope, *argv: str) -> tuple[int, CliState]:
    state = CliState(scope=scope)
    return app_mod.dispatch(list(argv), state), state


# -- -c / -r coalescing ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["-c", "model", "chat"], ["--continue=model", "chat"]),
        (["-c", "my", "long", "name"], ["--continue=my long name"]),
        (["-c"], [f"--continue={BARE_CONTINUE}"]),
        (["-c", "chat"], [f"--continue={BARE_CONTINUE}", "chat"]),
        (["-c", "-m", "x"], [f"--continue={BARE_CONTINUE}", "-m", "x"]),
        (["--continue", "a", "b", "chat", "-q", "hi"], ["--continue=a b", "chat", "-q", "hi"]),
        (["-r", "chat"], ["--resume=chat"]),
        (["-r", "two", "words", "chat"], ["--resume=two words", "chat"]),
        (["-z", "-c", "chat"], ["-z", "-c", "chat"]),
        (["chat", "-q", "-c something"], ["chat", "-q", "-c something"]),
        (["--", "-c", "x"], ["--", "-c", "x"]),
        (["--continue=keep me"], ["--continue=keep me"]),
    ],
)
def test_rewrite_session_flags(argv: list[str], expected: list[str]) -> None:
    assert app_mod.rewrite_session_flags(argv, ["chat", "model", "sessions"]) == expected


def test_continue_model_chat_names_a_session(scope: Scope, calls: list[Any]) -> None:
    code, _ = run(scope, "-c", "model", "chat")
    assert code == 0
    assert calls[0][0].continue_ == "model"


def test_continue_multi_word_name(scope: Scope, calls: list[Any]) -> None:
    run(scope, "-c", "my", "long", "name")
    assert calls[0][0].continue_ == "my long name"


def test_bare_continue_is_the_latest_marker(scope: Scope, calls: list[Any]) -> None:
    run(scope, "-c")
    assert calls[0][0].continue_ == BARE_CONTINUE


def test_model_before_chat_is_kept(scope: Scope, calls: list[Any]) -> None:
    run(scope, "-m", "x", "chat")
    assert calls[0][0].model == "x"


def test_flag_after_chat_wins_and_flags_before_survive(scope: Scope, calls: list[Any]) -> None:
    run(scope, "-m", "x", "--yolo", "-s", "a", "chat", "-m", "y", "-s", "b", "-q", "hello")
    args, chat = calls[0]
    assert args.model == "y" and args.yolo is True
    assert args.skills == ("a", "b")
    assert chat.query == "hello"


def test_reasoning_value_is_not_a_command(scope: Scope, calls: list[Any]) -> None:
    code, _ = run(scope, "--reasoning", "high", "chat")
    assert code == 0 and calls[0][0].reasoning == "high"


def test_bare_indus_runs_chat(scope: Scope, calls: list[Any]) -> None:
    code, state = run(scope)
    assert code == 0 and len(calls) == 1
    assert state.globals == GlobalArgs()


def test_global_flags_present() -> None:
    names = {opt for p in app_mod.cli.params for opt in (*p.opts, *p.secondary_opts)}
    for flag in (
        "-V", "--version", "-p", "--profile", "-z", "--oneshot", "--usage-file", "-m",
        "--model", "--provider", "--reasoning", "-t", "--toolsets", "-r", "--resume", "-c",
        "--continue", "--in", "--no-restore-cwd", "-s", "--skills", "--yolo",
        "--pass-session-id", "--ignore-user-config", "--ignore-rules", "--safe-mode", "--tui",
        "--cli",
    ):  # fmt: skip
        assert flag in names, flag


def test_value_flags_match_fallback() -> None:
    required, optional = app_mod.value_flag_sets()
    assert required == FALLBACK_VALUE_FLAGS
    assert optional == {"-c", "--continue"}


def test_chat_value_flags_match_the_chat_command() -> None:
    command = chat_mod.chat_command
    found = {
        opt
        for p in command.params
        if isinstance(p, click.Option) and not p.is_flag
        for opt in p.opts
    }
    assert found >= CHAT_VALUE_FLAGS
    assert found - CHAT_VALUE_FLAGS <= FALLBACK_VALUE_FLAGS | {"-c", "--continue"}


# -- unknown command -------------------------------------------------------------------------------


def test_unknown_command_three_lines_exit_2(scope: Scope) -> None:
    runner = CliRunner()
    result = runner.invoke(app_mod.cli, ["chta"], obj=CliState(scope=scope))
    assert result.exit_code == 2
    lines = result.stderr.splitlines()
    assert lines == [
        "'chta' is not an `indus` command.",
        "Did you mean: chat?",
        "Run `indus --help` to see all commands.",
    ]
    assert result.stdout == ""


def test_unknown_command_without_close_match_still_three_lines() -> None:
    lines = unknown_command_lines("indus", "zzzz", ["chat", "model"])
    assert len(lines) == 3 and lines[0] == "'zzzz' is not an `indus` command."


def test_unknown_command_suggests_at_most_three() -> None:
    lines = unknown_command_lines("indus", "sess", ["sessions", "session", "sessionz", "sesss"])
    assert lines[1].startswith("Did you mean: ") and lines[1].count(",") == 2


def test_unknown_inside_nested_group(scope: Scope) -> None:
    @click.command("list")
    def listing() -> None:
        click.echo("listed")

    group = LazyGroup("sessions", registry=())
    group.add_command(listing)
    outer = LazyGroup("indus", registry=())
    outer.add_command(group)
    result = CliRunner().invoke(outer, ["sessions", "lst"], obj=CliState(scope=scope))
    assert result.exit_code == 2
    assert result.stderr.splitlines() == [
        "'lst' is not an `indus sessions` command.",
        "Did you mean: list?",
        "Run `indus sessions --help` to see all commands.",
    ]


def test_moved_command_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.cli import unknown

    monkeypatch.setattr(unknown, "MOVED", {"oldcmd": "oldcmd moved to `indus newcmd`."})
    assert unknown.unknown_command_lines("indus", "oldcmd", ["chat"])[1] == (
        "oldcmd moved to `indus newcmd`."
    )


def test_plugin_hint_seam() -> None:
    from indus.cli import unknown

    unknown.set_plugin_hint(lambda path, word: f"install it with `indus plugins add {word}`")
    try:
        assert "plugins add honk" in unknown.unknown_command_lines("indus", "honk", [])[1]
    finally:
        unknown.set_plugin_hint(None)


# -- lazy loading and help -------------------------------------------------------------------------


def test_help_imports_no_subcommand_module(tmp_path: Path) -> None:
    probe = (
        "import sys\n"
        "from indus.cli.app import cli, REGISTRY\n"
        "from click.testing import CliRunner\n"
        "r = CliRunner().invoke(cli, ['--help'])\n"
        "assert r.exit_code == 0, r.output\n"
        "mods = {t.split(':')[0] for _, t, _ in REGISTRY}\n"
        "print(sorted(m for m in mods if m in sys.modules))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=tmp_path, check=True
    )
    assert out.stdout.strip() == "[]"


def test_help_order_follows_registration(scope: Scope) -> None:
    result = CliRunner().invoke(app_mod.cli, ["--help"], obj=CliState(scope=scope))
    assert result.exit_code == 0
    section = result.output.split("Commands:", 1)[1]
    listed = [line.split()[0] for line in section.splitlines() if line.strip()]
    assert listed == [name for name, _, _ in app_mod.REGISTRY]


def test_lazy_group_refuses_duplicates() -> None:
    with pytest.raises(ValueError):
        LazyGroup("x", registry=[("a", "m:a", ""), ("a", "m:b", "")])


def test_usage_error_is_exit_2(scope: Scope, calls: list[Any]) -> None:
    code, _ = run(scope, "--no-such-flag")
    assert code == 2 and calls == []
