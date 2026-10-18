"""The ``!command`` bang shell (P06-11, R04.27)."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from indus.cli import bang
from indus.foundation.scope import Scope
from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice, ApprovalRequest
from indus.safety.approvals import APPROVALS, session_key
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.clock import FakeClock
from tests.unit.cli.slash._fakes import FakePort, make_ctx, with_config

pytestmark = pytest.mark.skipif(os.name != "posix", reason="runs POSIX shell commands")

FAKE_PROVIDER_KEY = "sk-" + "Tq7" * 12  # built at runtime: no credential-shaped literal


class AdvancingClock(FakeClock):
    """Each monotonic read moves time ``step`` seconds (a deadline passes after a few polls)."""

    def __init__(self, step: float) -> None:
        super().__init__(FAKE_CLOCK_START)
        self.step = step

    def monotonic(self) -> float:
        self.advance(self.step)
        return super().monotonic()


@pytest.fixture(autouse=True)
def _forget_approvals(scope: Scope) -> Any:
    yield
    APPROVALS.clear_session(session_key(scope, "s1"))


def test_parse_rules() -> None:
    assert bang.is_bang("!ls")
    assert bang.is_bang("   !git status")
    assert not bang.is_bang("fix it!")
    assert not bang.is_bang("")
    assert not bang.is_bang(None)
    assert bang.parse_bang("!  ls -la ") == "ls -la"
    assert bang.parse_bang("!!") == "!"
    assert bang.parse_bang("!") == ""
    assert bang.parse_bang("fix it!") == ""


def test_bare_bang_prints_usage(scope: Scope) -> None:
    ctx = make_ctx(scope)
    assert bang.run_bang("!", ctx) == 0
    assert ctx.lines == [bang.USAGE]


def test_runs_streams_and_keeps_out_of_history(scope: Scope, tmp_path: Path) -> None:
    port = FakePort()
    ctx = make_ctx(scope, port=port, session_cwd=lambda: str(tmp_path))
    code = bang.run_bang("!echo one; echo two >&2; exit 3", ctx)
    assert code == 3
    assert ctx.lines == ["one", "two", "!: exit 3"]
    assert port.calls == []  # no turn, nothing stored


def test_floor_blocks_rm_rf_root(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a blocked command must not start")

    monkeypatch.setattr(bang, "run_shell", never)
    APPROVALS.set_auto_approve(session_key(scope, "s1"), True)  # floors beat auto-approve
    ctx = make_ctx(scope)
    assert bang.run_bang("!rm -rf /", ctx) == bang.BLOCKED_RC
    assert len(ctx.lines) == 1 and ctx.lines[0].startswith("!: not run:")


def test_dangerous_command_asks_and_deny_blocks(scope: Scope, tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    asked: list[ApprovalRequest] = []

    def deny(request: ApprovalRequest) -> ApprovalAnswer:
        asked.append(request)
        return ApprovalAnswer(choice=ApprovalChoice.DENY, reason="not now")

    ctx = make_ctx(scope, ask_approval=deny, session_cwd=lambda: str(tmp_path))
    assert bang.run_bang("!chmod -R 777 ./x", ctx) == bang.BLOCKED_RC
    assert asked[0].command == "chmod -R 777 ./x" and asked[0].session_id == "s1"
    assert ctx.lines == ["!: not run: You denied the command."]

    def once(request: ApprovalRequest) -> ApprovalAnswer:
        return ApprovalAnswer(choice=ApprovalChoice.ONCE)

    ctx = make_ctx(scope, ask_approval=once, session_cwd=lambda: str(tmp_path))
    assert bang.run_bang("!chmod -R 777 ./x && echo ok", ctx) == 0
    assert ctx.lines == ["ok"]


def test_dangerous_command_without_a_way_to_ask_is_not_run(scope: Scope) -> None:
    ctx = make_ctx(scope)
    assert bang.run_bang("!chmod -R 777 ./nothing-here", ctx) == bang.BLOCKED_RC
    assert ctx.lines == [
        "!: not run: The approval request was not answered; the command did not run."
    ]


def test_guard_missing_refuses(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bang, "GUARD_MODULE", "indus.safety.no_such_guard")
    ctx = make_ctx(scope)
    assert bang.run_bang("!echo hi", ctx) == bang.BLOCKED_RC
    assert ctx.lines == ["!: not run: the safety check could not be loaded, so nothing ran"]


def test_env_failure_refuses(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.safety import child_env

    def broken(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise RuntimeError("sanitiser gone")

    monkeypatch.setattr(child_env, "child_env", broken)
    ctx = make_ctx(scope)
    assert bang.run_bang("!echo hi", ctx) == bang.LAUNCH_FAILED_RC
    assert ctx.lines == ["!: not run: a safe environment could not be built (RuntimeError)"]


def test_env_has_no_provider_key(scope: Scope) -> None:
    env = scope.env.with_overlay({"OPENAI_API_KEY": FAKE_PROVIDER_KEY, "PLAIN_SETTING": "kept"})
    ctx = make_ctx(replace(scope, env=env))
    assert bang.run_bang("!env", ctx) == 0
    assert "PLAIN_SETTING=kept" in ctx.lines
    assert not any(line.startswith("OPENAI_API_KEY=") for line in ctx.lines)
    assert FAKE_PROVIDER_KEY not in ctx.text


def test_timeout_kills_with_124(scope: Scope) -> None:
    ctx = make_ctx(scope, clock=AdvancingClock(50.0))
    code = bang.run_bang("!echo started; sleep 30; echo never", ctx)
    assert code == bang.TIMED_OUT_RC
    assert ctx.lines == ["started", "!: stopped after 120s (timed out)"]


def test_ctrl_c_kills_with_130(scope: Scope) -> None:
    pressed: list[str] = []

    def out(line: str) -> None:
        if not pressed:
            pressed.append(line)
            raise KeyboardInterrupt
        pressed.append(line)

    ctx = make_ctx(scope, out=out)
    code = bang.run_bang("!echo first; sleep 30", ctx)
    assert code == bang.INTERRUPTED_RC
    assert pressed == ["first", "!: interrupted (exit 130)"]


def test_cancel_kills_with_130(scope: Scope) -> None:
    ctx = make_ctx(scope)
    assert bang.run_bang("!sleep 30", ctx, cancel=lambda: True) == bang.INTERRUPTED_RC
    assert ctx.lines == ["!: interrupted (exit 130)"]


def test_launch_failure_127(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_shell(*args: Any, **kwargs: Any) -> Any:
        raise OSError("no shell here")

    monkeypatch.setattr(subprocess, "Popen", no_shell)
    ctx = make_ctx(scope)
    assert bang.run_bang("!echo hi", ctx) == bang.LAUNCH_FAILED_RC
    assert ctx.lines == ["!: could not start the command: no shell here"]


def test_cwd_order(scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_dir = tmp_path / "session"
    config_dir = tmp_path / "configured"
    session_dir.mkdir()
    config_dir.mkdir()
    configured = with_config(scope, f"terminal:\n  cwd: {config_dir}\n")
    ctx = make_ctx(configured, session_cwd=lambda: str(session_dir))
    bang.run_bang("!pwd", ctx)
    assert ctx.lines == [str(session_dir)]
    ctx = make_ctx(configured, session_cwd=lambda: str(tmp_path / "gone"))
    bang.run_bang("!pwd", ctx)
    assert ctx.lines == [str(config_dir)]
    monkeypatch.chdir(tmp_path)
    ctx = make_ctx(scope, session_cwd=lambda: None)
    bang.run_bang("!pwd", ctx)
    assert ctx.lines == [str(tmp_path)]


def test_only_interactive_local_sessions(scope: Scope) -> None:
    ctx = make_ctx(scope, interactive=False)
    assert bang.run_bang("!echo hi", ctx) == bang.BLOCKED_RC
    assert ctx.lines == [
        "Shell commands with ! only work in an interactive session on this machine."
    ]
    for marker in ({"INDUS_SESSION_PLATFORM": "telegram"}, {"INDUS_CRON_SESSION": "1"}):
        gated = replace(scope, env=scope.env.with_overlay(marker))
        assert not bang.bang_allowed(gated)
    assert bang.bang_allowed(scope)
