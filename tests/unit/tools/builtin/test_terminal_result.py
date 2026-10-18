"""``terminal`` foreground run and result shapes (P05-12)."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import ExecResult
from indus.backends.registry import BackendStatus
from indus.foundation.scope import Scope
from indus.tools.builtin import terminal_result as tr
from tests.fakes.terminal import ScriptedEnv, forge_token, terminal_scope
from tests.unit.tools.builtin._terminal_rig import Rig


@pytest.fixture
def scope(tmp_path: Path) -> Scope:
    return terminal_scope(tmp_path)


def finish(scope: Scope, command: str, result: ExecResult, **kw: Any) -> str:
    values: dict[str, Any] = {"scope": scope, "start_cwd": "/work", "workdir": None}
    values.update(kw)
    return tr.finish(command, result, **values)


# -- golden shapes (byte-exact, key order) ------------------------------------------------------


def test_golden_finished_minimal(scope: Scope) -> None:
    assert finish(scope, "ls", ExecResult("a\nb\n", 0)) == (
        '{"output": "a\\nb", "exit_code": 0, "error": null}'
    )


def test_golden_finished_with_every_optional_field(scope: Scope, tmp_path: Path) -> None:
    spill = tmp_path / "out.log"
    spill.write_text("all of it", encoding="utf-8")
    result = ExecResult(
        "no hits\n",
        1,
        cwd_observed="/elsewhere",
        environment_recreated=True,
        full_output_path=str(spill),
        output_total_chars=60_000,
    )
    text = finish(scope, "cd /elsewhere && grep x f", result, approval_note="approved by the user")
    body = json.loads(text)
    assert list(body) == [
        "output",
        "exit_code",
        "error",
        "cwd",
        "environment_recreated",
        "full_output_path",
        "output_total_chars",
        "approval",
        "exit_code_meaning",
    ]
    assert body["cwd"] == "/elsewhere" and body["error"] is None
    assert body["environment_recreated"] == tr.ENV_RECREATED_NOTE
    assert body["exit_code_meaning"] == "no matches were found (not an error)"


@dataclass(frozen=True, slots=True)
class SudoResult(ExecResult):
    """The ExecResult fields the sudo layer (P05-10) adds."""

    sudo_auth_failed: bool = False
    sudo_cache_cleared: bool = False


def test_golden_hint_and_sudo_flags(scope: Scope) -> None:
    result = SudoResult(
        "bash: frobnicate: command not found\n",
        127,
        sudo_auth_failed=True,
        sudo_cache_cleared=True,
    )
    text = finish(scope, "frobnicate", result)
    body = json.loads(text)
    assert list(body) == [
        "output",
        "exit_code",
        "error",
        "hint",
        "sudo_auth_failed",
        "sudo_cache_cleared",
    ]
    assert "`frobnicate` is not installed" in body["hint"]


def test_golden_timed_out_by_exception() -> None:
    assert tr.timed_out(30) == (
        '{"output": "", "exit_code": 124, "error": "Command timed out after 30 seconds"}'
    )


def test_golden_deadline_hit(scope: Scope) -> None:
    result = ExecResult("partial\n[Command timed out after 5s]", 124, timed_out=True)
    body = json.loads(finish(scope, "./slow", result))
    assert list(body) == ["output", "exit_code", "error", "hint"]
    assert body["exit_code"] == 124 and body["output"].endswith("[Command timed out after 5s]")
    assert "ran out of time" in body["hint"]


def test_golden_background_shapes() -> None:
    assert tr.background_started("proc_0123456789ab", 77, {"approval": "ok"}) == (
        '{"output": "Background process started", "session_id": "proc_0123456789ab", '
        '"pid": 77, "exit_code": 0, "error": null, "approval": "ok"}'
    )
    assert tr.background_failed("no bash") == (
        '{"output": "", "exit_code": -1, "error": "Failed to start background process: no bash"}'
    )


def test_golden_refusal_shapes() -> None:
    assert tr.rejected("bad") == '{"error": "bad", "status": "error"}'
    assert tr.blocked("no") == '{"output": "", "exit_code": -1, "error": "no", "status": "blocked"}'
    assert tr.blocked("no", user_summary="s") == (
        '{"output": "", "exit_code": -1, "error": "no", "status": "blocked", "user_summary": "s"}'
    )
    assert tr.pending("git push -f", "force push", "git_force") == (
        '{"status": "pending_approval", "approval_pending": true, "command": "git push -f", '
        '"description": "force push", "pattern_key": "git_force", "smart_denied": false, '
        '"allow_permanent": true}'
    )


def test_golden_backend_shapes() -> None:
    degraded = BackendStatus(
        "degraded", "Terminal backend degraded: down", reason="down", retry_hint="later"
    )
    assert tr.backend_status(degraded) == (
        '{"output": "", "exit_code": -1, "status": "degraded", "reason": "down", '
        '"retry_hint": "later", "error": "Terminal backend degraded: down"}'
    )
    disabled = BackendStatus("disabled", "Terminal backend disabled: no docker")
    assert tr.backend_status(disabled) == (
        '{"output": "", "exit_code": -1, "status": "disabled", '
        '"error": "Terminal backend disabled: no docker"}'
    )


# -- exit codes and hints -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "code", "expected"),
    [
        ("ls", 0, None),
        ("grep foo file", 1, "no matches"),
        ("cat x | rg foo", 1, "no matches"),
        ("FOO=1 /usr/bin/grep x y", 1, "no matches"),
        ("diff a b", 1, "differ"),
        ("test -f x", 1, "false"),
        ("[ -f x ]", 1, "false"),
        ("find / -name x", 1, "could not be read"),
        ("curl https://example.invalid", 6, "resolved"),
        ("curl x", 7, "connection"),
        ("curl -f x", 22, "HTTP error"),
        ("curl x", 28, "timed out"),
        ("git diff --exit-code", 1, "git reported"),
        ("grep foo file", 2, None),
        ("./crash", -11, "signal 11: SIGSEGV"),
        ("./x", -2, None),
        ("./x", -40, "signal 40"),
        ("./oom", 137, "usually means signal 9"),
        ("./segv", 139, "usually means signal 11"),
        ("yes | head", 141, "usually means signal 13"),
        ("./x", 143, "usually means signal 15"),
        ("./x", 152, "usually means signal 24"),
        ("./x", 153, "usually means signal 25"),
        ("./x", 130, None),
        ("./x", 134, None),
        ("make", 2, None),
    ],
)
def test_exit_code_meaning_table(command: str, code: int, expected: str | None) -> None:
    meaning = tr.exit_code_meaning(command, code)
    if expected is None:
        assert meaning is None
    else:
        assert meaning is not None and expected in meaning


@pytest.mark.parametrize(
    ("command", "output", "fires"),
    [
        ("cargo build 2>&1 | tail -20", "error[E0308]: mismatched types", True),
        ("pytest | tail -5", "==== 2 failed, 3 passed ====", True),
        ("python app.py || echo done", "Traceback (most recent call last):\n  x", True),
        ("make || true", "make: *** [all] Error 2", True),
        ("pytest | tail -5", "==== 5 passed ====", False),
        ("grep -r Traceback logs | head", "Traceback (most recent call last)", False),
        ("python app.py", "Traceback (most recent call last)", False),
        ("cat log | tail", "", False),
    ],
)
def test_masked_failure_hint(command: str, output: str, fires: bool) -> None:
    assert (tr.masked_failure_hint(command, output) is not None) is fires


def test_masked_failure_hint_reaches_the_result(scope: Scope) -> None:
    body = json.loads(finish(scope, "pytest | tail -3", ExecResult("=== 1 failed in 2s ===\n", 0)))
    assert body["exit_code"] == 0 and "without the pipe" in body["hint"]


@pytest.mark.parametrize(
    ("output", "code", "needle"),
    [
        ("bash: python: command not found", 127, "python3"),
        ("ModuleNotFoundError: No module named 'requests'", 1, "virtual environment"),
        ("open: Permission denied", 1, "Permission denied"),
        ("", 126, "Exit 126"),
        ("weird", 3, None),
    ],
)
def test_failure_hints(output: str, code: int, needle: str | None) -> None:
    hint = tr.failure_hint("x", code, output)
    assert (hint is None) if needle is None else (hint is not None and needle in hint)


# -- finishing ----------------------------------------------------------------------------------


def test_ansi_stripped_and_redacted(scope: Scope) -> None:
    token = forge_token()
    result = ExecResult(f"\x1b[31mred\x1b[0m and \x1b]0;title\x07{token}\n", 0)
    body = json.loads(finish(scope, "./show", result))
    assert "\x1b" not in body["output"] and token not in body["output"]
    assert body["output"].startswith("red and ")


def test_output_cut_to_the_configured_size(scope: Scope) -> None:
    body = json.loads(finish(scope, "./big", ExecResult("x" * 60_000, 0), max_chars=1_000))
    assert "shortened" in body["output"] and len(body["output"]) < 1_200


def test_overflow_log_is_redacted(scope: Scope, tmp_path: Path) -> None:
    token = forge_token()
    spill = tmp_path / "out-1.log"
    spill.write_text(f"\x1b[1mstart\x1b[0m {token} end", encoding="utf-8")
    result = ExecResult("start ... end", 0, full_output_path=str(spill), output_total_chars=99)
    body = json.loads(finish(scope, "./big", result))
    assert body["full_output_path"] == str(spill) and body["output_total_chars"] == 99
    text = spill.read_text(encoding="utf-8")
    assert token not in text and "\x1b" not in text and "start" in text
    assert stat.S_IMODE(os.stat(spill).st_mode) == 0o600


def test_unreadable_overflow_log_is_dropped(scope: Scope, tmp_path: Path) -> None:
    gone = tmp_path / "missing.log"
    body = json.loads(
        finish(scope, "./big", ExecResult("x", 0, full_output_path=str(gone), output_total_chars=9))
    )
    assert "full_output_path" not in body and "output_total_chars" not in body


def test_approved_then_interrupted(scope: Scope) -> None:
    result = ExecResult("partial\n[Command interrupted]", 130, interrupted=True)
    body = json.loads(finish(scope, "./job", result, approval_note="Approved by the user."))
    assert body["approval"] == "Approved by the user, then interrupted."
    assert "exit_code_meaning" not in body and "hint" not in body


def test_command_exiting_130_itself_is_not_an_interrupt(scope: Scope) -> None:
    body = json.loads(finish(scope, "./job", ExecResult("bye", 130), approval_note="Approved."))
    assert body["approval"] == "Approved."


def test_cwd_echo_only_when_it_moved(scope: Scope) -> None:
    same = json.loads(finish(scope, "pwd", ExecResult("/work", 0, cwd_observed="/work")))
    assert "cwd" not in same
    moved = json.loads(finish(scope, "cd /tmp", ExecResult("", 0, cwd_observed="/tmp")))
    assert moved["cwd"] == "/tmp"
    with_workdir = json.loads(
        finish(scope, "cd /tmp", ExecResult("", 0, cwd_observed="/tmp"), workdir="/srv")
    )
    assert "cwd" not in with_workdir


# -- the run: session cwd and retries -----------------------------------------------------------


def test_cwd_recorded_from_marker(tmp_path: Path) -> None:
    env = ScriptedEnv([ExecResult("", 0, cwd_observed="/work/sub"), ExecResult("hi", 0)])
    rig = Rig(tmp_path, env=env)
    first = rig.run(rig.call(command="cd sub"))
    assert first["cwd"] == "/work/sub"
    assert rig.tool.session_cwd(rig.scope, "s1") == "/work/sub"
    rig.run(rig.call(command="ls"))
    assert env.calls[1].cwd is None  # the environment already stands there


def test_cwd_not_recorded_with_workdir(tmp_path: Path) -> None:
    env = ScriptedEnv([ExecResult("", 0, cwd_observed="/srv/x")])
    rig = Rig(tmp_path, env=env)
    body = rig.run(rig.call(command="cd x", workdir="/srv"))
    assert "cwd" not in body
    assert rig.tool.session_cwd(rig.scope, "s1") is None
    assert env.calls[0].cwd == "/srv"


def test_cwd_not_recorded_without_marker(tmp_path: Path) -> None:
    env = ScriptedEnv([ExecResult("[Command interrupted]", 130, interrupted=True)])
    rig = Rig(tmp_path, env=env)
    rig.run(rig.call(command="cd /tmp && sleep 100"))
    assert rig.tool.session_cwd(rig.scope, "s1") is None


def test_rebuilt_environment_starts_where_the_session_was(tmp_path: Path) -> None:
    env = ScriptedEnv([ExecResult("", 0, cwd_observed="/work/deep"), ExecResult("", 0)])
    rig = Rig(tmp_path, env=env)
    rig.run(rig.call(command="cd deep"))
    env._cwd = "/work"  # pyright: ignore[reportPrivateUsage] - as if rebuilt at the start dir
    rig.run(rig.call(command="ls"))
    assert env.calls[1].cwd == "/work/deep"


def test_cwd_is_per_session_and_profile(tmp_path: Path) -> None:
    env = ScriptedEnv([ExecResult("", 0, cwd_observed="/a")])
    rig = Rig(tmp_path, env=env)
    rig.run(rig.call(command="cd /a"))
    assert rig.tool.session_cwd(rig.scope, "s2") is None
    other = terminal_scope(tmp_path, name="other")
    assert rig.tool.session_cwd(other, "s1") is None


def test_retries_then_success(tmp_path: Path) -> None:
    env = ScriptedEnv(
        [RuntimeError("socket hiccup"), RuntimeError("socket hiccup"), ExecResult("ok", 0)]
    )
    rig = Rig(tmp_path, env=env)
    body = rig.run(rig.call(command="ls"))
    assert body == {"output": "ok", "exit_code": 0, "error": None}
    assert len(env.calls) == 3
    assert rig.clock.monotonic() >= 6.0  # backed off 2 s, then 4 s


def test_retries_exhausted_error_is_redacted(tmp_path: Path) -> None:
    token = forge_token()
    env = ScriptedEnv([RuntimeError(f"bad {token}") for _ in range(4)])
    rig = Rig(tmp_path, env=env)
    body = rig.run(rig.call(command="ls"))
    assert body["exit_code"] == -1 and body["error"].startswith("Command execution failed")
    assert token not in body["error"]
    assert len(env.calls) == 4 and rig.clock.monotonic() >= 14.0


def test_timeout_exception_is_124(tmp_path: Path) -> None:
    env = ScriptedEnv([TimeoutError("read timed out")])
    rig = Rig(tmp_path, env=env)
    body = rig.run(rig.call(command="ls", timeout=30))
    assert body == {"output": "", "exit_code": 124, "error": "Command timed out after 30 seconds"}
    assert len(env.calls) == 1


def test_interrupt_during_backoff_is_not_lost(tmp_path: Path) -> None:
    env = ScriptedEnv([RuntimeError("blip"), ExecResult("never", 0)])
    rig = Rig(tmp_path, env=env)
    stop = threading.Event()
    stop.set()

    async def go() -> str:
        return await rig.tool({"command": "ls"}, scope=rig.scope, session_id="s1", cancel=stop)

    body = json.loads(rig.run(go()))
    assert body["exit_code"] == 130 and "[Command interrupted]" in body["output"]
    assert len(env.calls) == 1


def test_runs_inside_the_sudo_caller_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered: list[tuple[object, str | None, bool]] = []

    @contextlib.contextmanager
    def caller(
        *, broker: object, turn_id: str | None = None, interactive: bool = True
    ) -> Iterator[None]:
        entered.append((broker, turn_id, interactive))
        yield

    monkeypatch.setattr(tr, "use_sudo_caller", caller)
    rig = Rig(tmp_path, env=ScriptedEnv([ExecResult("ok", 0), ExecResult("ok", 0)]))
    broker = object()

    async def go(run_context: str | None) -> str:
        return await rig.tool(
            {"command": "sudo -n true"},
            scope=rig.scope,
            session_id="s1",
            turn_id="t7",
            broker=broker,
            run_context=run_context,
        )

    assert json.loads(rig.run(go(None)))["output"] == "ok"
    assert json.loads(rig.run(go("cron")))["output"] == "ok"
    # An unattended run never prompts for a sudo password (headless caller).
    assert entered == [(broker, "t7", True), (broker, "t7", False)]
