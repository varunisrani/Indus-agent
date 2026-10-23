"""``terminal`` normalisation, planning, guards and the approval gate (P05-11)."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import BackendFlags, EnvironmentConnectionError, ExecResult
from indus.backends.registry import BackendEntry, EnvironmentRegistry
from indus.foundation.scope import Scope
from indus.safety import approvals
from indus.safety.approvals.decision import (
    ApprovalContext,
    ApprovalDecision,
    StaleInterruptReset,
)
from indus.tools.builtin import terminal as terminal_module
from indus.tools.builtin.terminal import (
    TERMINAL_SCHEMA,
    Call,
    GateRequest,
    GateVerdict,
    GuardInput,
    Refusal,
    foreground_guidance,
    normalize,
    pipeline_gate,
    plan,
    trailing_amp_command,
    workdir_guard,
)
from indus.tools.definitions import definitions
from indus.tools.registry import ToolRegistry
from tests.fakes.terminal import ScriptedEnv, terminal_scope
from tests.unit.tools.builtin._terminal_rig import Rig


def refused(args: dict[str, Any]) -> dict[str, Any]:
    with pytest.raises(Refusal) as caught:
        normalize(args)
    return json.loads(caught.value.result)


@pytest.fixture
def scope(tmp_path: Path) -> Scope:
    return terminal_scope(tmp_path)


# -- normalisation ------------------------------------------------------------------------------


def test_stray_code_argument_points_to_execute_code() -> None:
    body = refused({"code": "print(1)"})
    assert "execute_code" in body["error"] and body["status"] == "error"


@pytest.mark.parametrize(
    ("args", "named"),
    [
        ({"command": "ls", "notify": True}, "background=true, notify"),
        ({"command": "ls", "notify": ["ready"]}, "background=true, notify"),
        ({"command": "ls", "notify_on_complete": True}, "background=true, notify"),
        ({"command": "ls", "watch_patterns": ["x"]}, "background=true, notify"),
        ({"command": "ls", "pty": True}, "background=true, pty=true"),
        ({"command": "ls", "persist_on_release": True}, "persist_on_release=true"),
    ],
)
def test_foreground_background_only_args_refused(args: dict[str, Any], named: str) -> None:
    assert named in refused(args)["error"]


def test_foreground_heartbeat_is_dropped() -> None:
    call = normalize({"command": "ls", "heartbeat": 60})
    assert call.heartbeat == 0 and not call.notify_on_exit and not call.background


def test_foreground_notify_false_is_harmless() -> None:
    assert normalize({"command": "ls", "notify": False}).notify_on_exit is False


@pytest.mark.parametrize("beat", [-1, "60", True, 2.5])
def test_bad_heartbeat_refused(beat: object) -> None:
    assert "heartbeat" in refused({"command": "x", "background": True, "heartbeat": beat})["error"]


def test_notify_forms() -> None:
    on = normalize({"command": "make", "background": True, "notify": True})
    assert on.notify_on_exit and on.watch_patterns == ()
    watch = normalize({"command": "srv", "background": True, "notify": ["ready", ""]})
    assert not watch.notify_on_exit and watch.watch_patterns == ("ready",)
    explicit_wins = normalize(
        {"command": "x", "background": True, "notify": True, "watch_patterns": ["a"]}
    )
    assert explicit_wins.notify_on_exit and explicit_wins.watch_patterns == ()
    beat = normalize({"command": "x", "background": True, "heartbeat": 90})
    assert beat.notify_on_exit and beat.heartbeat == 90  # a heartbeat implies notify
    assert (
        "notify is true or false"
        in refused({"command": "x", "background": True, "notify": {"a": 1}})["error"]
    )


def test_workdir_must_be_text() -> None:
    assert "workdir" in refused({"command": "ls", "workdir": 5})["error"]


# -- planning -----------------------------------------------------------------------------------


def _plan(scope: Scope, **args: Any) -> Any:
    return plan(normalize({"command": "true", **args}), scope)


def _plan_refusal(scope: Scope, **args: Any) -> dict[str, Any]:
    with pytest.raises(Refusal) as caught:
        _plan(scope, **args)
    return json.loads(caught.value.result)


@pytest.mark.parametrize("command", [None, 5, ["ls"]])
def test_command_must_be_text(scope: Scope, command: object) -> None:
    with pytest.raises(Refusal) as caught:
        plan(Call(command=command), scope)
    assert "command must be text" in json.loads(caught.value.result)["error"]


def test_empty_command_refused(scope: Scope) -> None:
    assert "empty" in _plan_refusal(scope, command="   ")["error"]


@pytest.mark.parametrize("timeout", [0, -5])
def test_timeout_must_be_positive(scope: Scope, timeout: int) -> None:
    assert "positive" in _plan_refusal(scope, timeout=timeout)["error"]


def test_default_timeout_from_config(tmp_path: Path) -> None:
    configured = terminal_scope(tmp_path, {"terminal": {"timeout": 42}})
    assert _plan(configured).timeout == 42
    assert _plan(terminal_scope(tmp_path, name="other")).timeout == 180


def test_relative_workdir_refused(scope: Scope) -> None:
    assert "absolute" in _plan_refusal(scope, workdir="src")["error"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python -m http.server 8000 &", "python -m http.server 8000"),
        ("make build && ./serve | tee log &", "make build && ./serve | tee log"),
        ("node app.js 2>&1 &", "node app.js 2>&1"),
        ("echo 'a & b' &", "echo 'a & b'"),
        ("run trailing\\  &", "run trailing\\ "),
    ],
)
def test_trailing_amp_becomes_background(scope: Scope, command: str, expected: str) -> None:
    assert trailing_amp_command(command) == expected
    steps = _plan(scope, command=command)
    assert steps.background and steps.auto_background and steps.command == expected


@pytest.mark.parametrize(
    "command",
    [
        "sleep 9 & echo $(sleep 1 &) &",
        "a & b &",
        "a; b &",
        "a\nb &",
        "nohup srv &",
        "srv & # comment",
        "`x` &",
        "cat <(ls) &",
        "srv --help &",
    ],
)
def test_trailing_amp_with_other_backgrounding_is_not_converted(command: str) -> None:
    assert trailing_amp_command(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "srv & echo started",
        "nohup ./server",
        "setsid ./worker",
        "npm run dev",
        "pnpm dev",
        "docker compose up",
        "python3 -m http.server",
        "uvicorn app:main --reload",
    ],
)
def test_backgrounding_shapes_refused_with_guidance(scope: Scope, command: str) -> None:
    body = _plan_refusal(scope, command=command)
    assert "background=true" in body["error"] and body["status"] == "error"


@pytest.mark.parametrize(
    "command",
    [
        "npm run dev --help",
        "uvicorn --version",
        "docker compose up -d",
        'echo "a & b"',
        "ls && pwd",
        "cmd 2>&1 | tee out",
        "cmd &> out.log",
        "grep 'nohup' notes.txt",
    ],
)
def test_ordinary_commands_run_in_the_foreground(scope: Scope, command: str) -> None:
    assert foreground_guidance(command) is None
    steps = _plan(scope, command=command)
    assert not steps.background


def test_overcap_foreground_is_promoted(scope: Scope) -> None:
    steps = _plan(scope, command="pytest -x", timeout=900)
    assert steps.background and steps.promoted_from == 900 and not steps.auto_background
    assert not _plan(scope, command="pytest -x", timeout=600).background


# -- guards -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "workdir", ["/tmp/a;rm -rf x", "/tmp/$(id)", "/tmp/a\nb", "/tmp/`x`", "/a|b"]
)
def test_workdir_guard_blocks_shell_syntax(workdir: str) -> None:
    assert workdir_guard(GuardInput("ls", workdir, "/", "local")) is not None


@pytest.mark.parametrize(
    "workdir", ["/home/u/My Project", "/srv/app-1.2_x/~me", "/data/a+b@c=d,e", "/données/日本"]
)
def test_workdir_guard_allows_plain_paths(workdir: str) -> None:
    assert workdir_guard(GuardInput("ls", workdir, "/", "local")) is None


def test_workdir_with_metacharacters_blocked(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    body = rig.run(rig.call(command="ls", workdir="/tmp/x;id"))
    assert body["status"] == "blocked" and body["exit_code"] == -1
    assert rig.env.calls == []  # nothing ran


def test_guard_deadline_overrun_refuses(tmp_path: Path) -> None:
    release = threading.Event()

    def stuck(data: GuardInput) -> str | None:
        release.wait(30)
        return None

    rig = Rig(tmp_path, guards=[stuck])
    try:
        # The guard is stuck in real time: the clock must move past its deadline meanwhile.
        body = rig.run(rig.call(command="ls", timeout=5), hold=False)
    finally:
        release.set()
    assert "did not finish within 30s" in body["error"] and "again" in body["error"]
    assert rig.env.calls == []


def test_guard_crash_fails_closed(tmp_path: Path) -> None:
    def broken(data: GuardInput) -> str | None:
        raise RuntimeError("boom")

    rig = Rig(tmp_path, guards=[broken])
    body = rig.run(rig.call(command="ls"))
    assert body["status"] == "blocked" and rig.env.calls == []


# -- the approval gate --------------------------------------------------------------------------


def test_gate_outcomes(tmp_path: Path) -> None:
    verdicts = {
        "rm -rf build": GateVerdict("blocked", error="User denied: not now", user_summary="denied"),
        "git push -f": GateVerdict(
            "pending", description="force push", pattern_key="git_force_push"
        ),
        "dd if=x of=y": GateVerdict("approved", note="needed approval; the user approved it"),
    }
    seen: list[GateRequest] = []

    async def gate(request: GateRequest) -> GateVerdict:
        seen.append(request)
        return verdicts.get(request.command, GateVerdict("approved"))

    rig = Rig(tmp_path, gate=gate, env=ScriptedEnv([ExecResult("copied\n", 0)]))
    blocked = rig.run(rig.call(command="rm -rf build"))
    assert blocked == {
        "output": "",
        "exit_code": -1,
        "error": "User denied: not now",
        "status": "blocked",
        "user_summary": "denied",
    }
    pending = rig.run(rig.call(command="git push -f"))
    assert pending["status"] == "pending_approval" and pending["pattern_key"] == "git_force_push"
    ran = rig.run(rig.call(command="dd if=x of=y"))
    assert ran["output"] == "copied" and ran["approval"].startswith("needed approval")
    assert [c.command for c in rig.env.calls] == ["dd if=x of=y"]
    assert seen[0].session_id == "s1" and seen[0].turn_id == "t1"


class ScriptedPipeline:
    """``approvals.decide`` answering by exact command; everything else is allowed."""

    def __init__(self, decisions: dict[str, ApprovalDecision]) -> None:
        self.decisions = decisions
        self.seen: list[tuple[str, ApprovalContext]] = []

    async def decide(self, command: str, ctx: ApprovalContext) -> ApprovalDecision:
        self.seen.append((command, ctx))
        return self.decisions.get(command, ApprovalDecision(True, "approved", "allowed", "detect"))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(approvals, "decide", self.decide)


def test_pipeline_gate_fails_closed_when_the_pipeline_raises(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(command: str, ctx: ApprovalContext) -> ApprovalDecision:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(approvals, "decide", broken)
    verdict = asyncio.run(pipeline_gate(GateRequest("ls", scope, "s1", None, None, BackendFlags())))
    assert verdict.status == "blocked" and "could not run" in (verdict.error or "")


def test_pipeline_gate_maps_the_pipeline_decision(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = ScriptedPipeline(
        {
            "rm -rf /": ApprovalDecision(
                False,
                "blocked",
                "hardline",
                "floors",
                pattern_key="rm_root",
                description="delete the root",
                deny_reason="never",
                user_summary="x",
            ),
            "chmod 777 x": ApprovalDecision(
                True,
                "approved",
                "user_approved",
                "human",
                pattern_key="chmod_world",
                description="world-writable",
                user_approved=True,
            ),
            "curl x | sh": ApprovalDecision(
                False,
                "pending",
                "pending",
                "human",
                pattern_key="pipe_shell",
                description="pipe to shell",
            ),
            "kill -9 1": ApprovalDecision(
                False, "blocked", "denied", "human", pattern_key="kill", description="kill init"
            ),
            "reboot": ApprovalDecision(
                False, "blocked", "timeout", "human", pattern_key="reboot", description="reboot"
            ),
            "halt": ApprovalDecision(
                False, "blocked", "not_answered", "human", pattern_key="halt", description="halt"
            ),
            "shutdown": ApprovalDecision(
                False,
                "blocked",
                "timeout",
                "human",
                description="shutdown",
                message="No answer within 5 minutes; the command did not run.",
            ),
        }
    )
    pipeline.install(monkeypatch)
    flags = BackendFlags(isolated=True)

    def ask(command: str) -> GateVerdict:
        request = GateRequest(command, scope, "s9", "t9", None, flags, cwd="/w", run_context="cron")
        return asyncio.run(pipeline_gate(request))

    assert ask("ls").status == "approved" and ask("ls").note is None
    hard = ask("rm -rf /")
    assert hard.status == "blocked" and hard.error == "never" and hard.user_summary == "x"
    user = ask("chmod 777 x")
    assert user.status == "approved" and "world-writable" in (user.note or "")
    later = ask("curl x | sh")
    assert later.status == "pending" and later.pattern_key == "pipe_shell"
    plain = ask("kill -9 1")
    assert plain.status == "blocked" and "kill init" in (plain.error or "")
    silent = ask("reboot")
    assert silent.status == "blocked" and "not a refusal" in (silent.error or "")
    withdrawn = ask("halt")
    assert "could not be answered" in (withdrawn.error or "")
    assert ask("shutdown").error == "No answer within 5 minutes; the command did not run."
    command, ctx = pipeline.seen[0]
    assert command == "ls" and ctx.session_id == "s9" and ctx.isolated and ctx.scope is scope
    assert ctx.turn_id == "t9" and ctx.cwd == "/w" and ctx.run_context == "cron"


def test_unknown_run_context_is_interactive(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = ScriptedPipeline({})
    pipeline.install(monkeypatch)
    for given in (None, "weird", 3):
        request = GateRequest("ls", scope, "s1", None, None, BackendFlags(), run_context=given)
        asyncio.run(pipeline_gate(request))
    assert [ctx.run_context for _, ctx in pipeline.seen] == ["interactive"] * 3


def test_the_real_pipeline_blocks_a_hardline_command_unattended(scope: Scope) -> None:
    request = GateRequest(
        "rm -rf /", scope, "s1", None, None, BackendFlags(), run_context="unattended"
    )
    verdict = asyncio.run(pipeline_gate(request))
    assert verdict.status == "blocked" and verdict.decision is not None
    assert verdict.decision.outcome == "hardline"


# -- the whole call -----------------------------------------------------------------------------


def test_no_profile_scope_is_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    text = asyncio.run(rig.tool({"command": "ls"}, scope=None, session_id="s1"))
    assert "No profile" in json.loads(text)["error"]
    assert rig.created == []


def test_trailing_amp_runs_as_tracked_background_job(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    body = rig.run(rig.call(command="python3 -m http.server 8000 &"))
    assert body["output"] == "Background process started" and body["exit_code"] == 0
    assert body["session_id"].startswith("proc_") and "auto_backgrounded" in body
    assert "hint" not in body
    assert rig.specs[0].argv[-1] == "python3 -m http.server 8000"
    assert rig.env.calls == []


def test_overcap_foreground_promoted_with_note(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.deliverable()
    body = rig.run(rig.call(command="pytest -x", timeout=900))
    assert body["notify_on_complete"] is True
    assert "Do not run it again" in body["promoted_from_foreground"]
    assert "A message with its exit code" in body["promoted_from_foreground"]


def test_promoted_note_poll_only_when_not_deliverable(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    body = rig.run(rig.call(command="pytest -x", timeout=900))
    assert body["notify_on_complete"] is False
    assert "process_manage poll or wait" in body["promoted_from_foreground"]
    assert "notify_unsupported" in body


def test_notify_kept_only_where_deliverable(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.deliverable("s1")
    told = rig.run(rig.call(command="make", background=True, notify=True, heartbeat=60))
    assert told["notify_on_complete"] is True and told["heartbeat_seconds"] == 60
    cron = rig.run(rig.call("cron-1", command="make", background=True, notify=True, heartbeat=60))
    assert cron["notify_on_complete"] is False
    assert "heartbeat_ignored" in cron and "notify_unsupported" in cron


def test_notify_and_watch_together_drop_watch(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.deliverable()
    body = rig.run(
        rig.call(command="srv", background=True, notify_on_complete=True, watch_patterns=["up"])
    )
    assert "watch_patterns_ignored" in body and "watch_patterns" not in body


def test_silent_background_gets_a_hint(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    body = rig.run(rig.call(command="make", background=True))
    assert "notify=true" in body["hint"]


def test_pty_disabled_for_piped_stdin_commands(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    body = rig.run(rig.call(command="gh auth login --with-token", background=True, pty=True))
    assert "process_manage close" in body["pty_note"]
    assert rig.specs[0].use_pty is False


def test_background_on_sandbox_backends_starts_inside_them(tmp_path: Path) -> None:
    # Sandbox backends start the job through the backend itself (P11-12); one that prints no
    # process id is a failed start.
    env = ScriptedEnv(flags=BackendFlags(isolated=True))
    rig = Rig(tmp_path, env=env)
    body = rig.run(rig.call(command="make", background=True))
    assert body["exit_code"] == -1 and "process id" in body["error"]
    assert "nohup bash -lc make" in env.calls[0].command


def test_unknown_backend_is_disabled(tmp_path: Path) -> None:
    rig = Rig(tmp_path, config={"terminal": {"backend": "warp-drive"}})
    body = rig.run(rig.call(command="ls"))
    assert body["status"] == "disabled" and "warp-drive" in body["error"]


def test_unreachable_backend_is_degraded(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.fail_with = EnvironmentConnectionError("ssh: connection refused")
    body = rig.run(rig.call(command="ls"))
    assert body["status"] == "degraded" and body["reason"] == "ssh: connection refused"
    assert "retry_hint" in body


def test_registered_with_schema_and_probe(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    registry = ToolRegistry()
    rig.tool.register(registry)
    entry = registry.get("terminal")
    assert entry is not None and entry.is_async and entry.toolset == "terminal"
    assert entry.parameters is not None and entry.parameters["required"] == ["command"]
    assert rig.tool.probe(rig.scope)
    assert not rig.tool.probe(terminal_scope(tmp_path, {"terminal": {"backend": "none"}}, name="x"))


def test_environments_and_cancel_come_from_the_tool_context(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    other_env = ScriptedEnv([ExecResult("from extras", 0)])

    async def factory(request: Any) -> Any:
        return other_env

    extras_registry = EnvironmentRegistry(
        [BackendEntry("scripted", factory, other_env.flags)], clock=rig.clock
    )

    async def go() -> str:
        return await rig.tool(
            {"command": "ls"}, scope=rig.scope, session_id="s1", environments=extras_registry
        )

    body = json.loads(rig.run(go()))
    assert body["output"] == "from extras"
    assert rig.created == [] and rig.env.calls == []


def test_stale_interrupt_cleared_once_before_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset = StaleInterruptReset()
    decision = ApprovalDecision(
        True,
        "approved",
        "user_approved",
        "human",
        pattern_key="x",
        description="risky",
        user_approved=True,
        stale_interrupt=reset,
    )
    ScriptedPipeline({"risky": decision}).install(monkeypatch)
    env = ScriptedEnv([RuntimeError("blip"), ExecResult("done", 0)])
    rig = Rig(tmp_path, gate=pipeline_gate, env=env)
    stop = threading.Event()
    stop.set()  # landed while the user was being asked

    async def go() -> str:
        return await rig.tool({"command": "risky"}, scope=rig.scope, session_id="s1", cancel=stop)

    body = json.loads(rig.run(go()))
    assert body["output"] == "done" and "user approved" in body["approval"]
    assert reset.used and not stop.is_set()  # cleared once, before the retried run
    assert not decision.clear_stale_interrupt(stop)  # a second call does nothing
    assert len(env.calls) == 2


# -- native Windows: no background offer on a host backend (wave 48) ----------------------------

BACKGROUND_ONLY = {"background", "pty", "notify", "heartbeat", "persist_on_release"}


def _offered(rig: Rig) -> dict[str, Any]:
    registry = ToolRegistry()
    rig.tool.register(registry)
    (tool,) = definitions(registry, ["terminal"], scope=rig.scope)
    return tool["function"]


def test_native_windows_host_backend_is_not_offered_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Host jobs refuse on native Windows (W47-e-1), so the schema stops offering them there.
    monkeypatch.setattr(terminal_module, "os_family", lambda: "win32", raising=False)
    fn = _offered(Rig(tmp_path))  # the scripted backend is host-local, like `local`
    props = fn["parameters"]["properties"]
    assert not BACKGROUND_ONLY & set(props), sorted(BACKGROUND_ONLY & set(props))
    assert set(props) == {"command", "timeout", "workdir"}
    assert fn["parameters"]["required"] == ["command"]
    assert "background=true" not in fn["description"] and "WSL2" in fn["description"]
    assert "background" not in props["timeout"]["description"]
    assert "maximum 600" in props["timeout"]["description"]
    # the static schema itself is untouched
    assert "background" in TERMINAL_SCHEMA["parameters"]["properties"]


def test_windows_sandbox_backends_and_other_hosts_keep_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "os_family", lambda: "win32", raising=False)
    sandbox = Rig(tmp_path / "a", env=ScriptedEnv(flags=BackendFlags(isolated=True)))
    assert set(_offered(sandbox)["parameters"]["properties"]) >= BACKGROUND_ONLY
    monkeypatch.setattr(terminal_module, "os_family", lambda: "linux", raising=False)
    fn = _offered(Rig(tmp_path / "b"))
    assert set(fn["parameters"]["properties"]) >= BACKGROUND_ONLY
    assert "background=true" in fn["description"]


# -- native Windows: plan() turns nothing into a background job (wave 50 c) -----------------------


def _plan_fg_only(scope: Scope, **args: Any) -> Any:
    return plan(normalize({"command": "true", **args}), scope, foreground_only=True)


def _fg_only_refusal(scope: Scope, **args: Any) -> dict[str, Any]:
    with pytest.raises(Refusal) as caught:
        _plan_fg_only(scope, **args)
    return json.loads(caught.value.result)


@pytest.mark.parametrize(
    "command", ["python -m http.server 8000 &", "make build && ./serve | tee log &", "srv 2>&1 &"]
)
def test_foreground_only_refuses_a_trailing_amp_with_a_foreground_hint(
    scope: Scope, command: str
) -> None:
    assert trailing_amp_command(command) is not None  # POSIX would make it a job
    body = _fg_only_refusal(scope, command=command)
    assert body["status"] == "error"
    assert "without the '&'" in body["error"] and "foreground" in body["error"]
    assert "WSL2" in body["error"] and "background=true" not in body["error"]


@pytest.mark.parametrize(
    "command",
    ["srv & echo started", "nohup ./server", "setsid ./worker", "npm run dev", "uvicorn app:m"],
)
def test_foreground_only_guidance_names_no_background_option(scope: Scope, command: str) -> None:
    body = _fg_only_refusal(scope, command=command)
    assert "WSL2" in body["error"] and "background=true" not in body["error"]
    assert foreground_guidance(command, foreground_only=True) == body["error"]
    assert "background=true" in (foreground_guidance(command) or "")  # POSIX text unchanged


def test_foreground_only_caps_an_overcap_timeout_in_the_foreground(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)
    steps = _plan_fg_only(scope, command="pytest -x", timeout=900)
    assert not steps.background and steps.promoted_from is None
    assert steps.timeout == 600 and steps.capped_from == 900
    at_cap = _plan_fg_only(scope, command="pytest -x", timeout=600)
    assert (at_cap.background, at_cap.timeout, at_cap.capped_from) == (False, 600, None)
    custom = terminal_scope(tmp_path, {"terminal": {"max_foreground_timeout": 120}}, name="c")
    steps = _plan_fg_only(custom, command="pytest -x", timeout=300)
    assert (steps.background, steps.timeout, steps.capped_from) == (False, 120, 300)


def test_foreground_only_keeps_ordinary_and_explicit_background_plans(scope: Scope) -> None:
    for command in ("ls && pwd", "cmd 2>&1 | tee out", "srv --help &", "uvicorn --version"):
        steps = _plan_fg_only(scope, command=command)
        assert not steps.background and steps.command == command
    # an explicit background=true is planned as asked and meets the backend's refusal (W47-e-1)
    assert _plan_fg_only(scope, command="srv", background=True).background


def test_windows_host_backend_refuses_trailing_amp_before_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "os_family", lambda: "win32", raising=False)
    rig = Rig(tmp_path)  # the scripted backend is host-local, like `local`
    body = rig.run(rig.call(command="python3 -m http.server 8000 &"))
    assert body["status"] == "error" and "without the '&'" in body["error"]
    assert rig.specs == [] and rig.env.calls == [] and rig.created == []


def test_windows_host_backend_runs_an_overcap_call_in_the_foreground_at_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "os_family", lambda: "win32", raising=False)
    rig = Rig(tmp_path, env=ScriptedEnv([ExecResult("ok", 0)]))
    rig.deliverable()
    body = rig.run(rig.call(command="pytest -x", timeout=900))
    assert body["output"] == "ok" and body["exit_code"] == 0
    assert "900s" in body["timeout_capped"] and "600s" in body["timeout_capped"]
    assert "promoted_from_foreground" not in body and rig.specs == []
    assert [call.timeout for call in rig.env.calls] == [600]
    # within the cap: no note
    rig.env.answers.append(ExecResult("ok", 0))
    assert "timeout_capped" not in rig.run(rig.call(command="pytest -x", timeout=60))


def test_windows_sandbox_backend_and_posix_hosts_still_promote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "os_family", lambda: "win32", raising=False)
    sandbox = Rig(tmp_path / "a", env=ScriptedEnv(flags=BackendFlags(isolated=True)))
    assert not sandbox.tool.foreground_only(sandbox.scope)
    body = sandbox.run(sandbox.call(command="pytest -x", timeout=900))
    # promoted: started as a job inside the sandbox (the scripted one prints no process id)
    assert "process id" in body["error"] and "timeout_capped" not in body
    assert "nohup bash -lc" in sandbox.env.calls[0].command
    monkeypatch.setattr(terminal_module, "os_family", lambda: "linux", raising=False)
    host = Rig(tmp_path / "b")
    assert not host.tool.foreground_only(host.scope)
    body = host.run(host.call(command="python3 -m http.server 8000 &"))
    assert "auto_backgrounded" in body
