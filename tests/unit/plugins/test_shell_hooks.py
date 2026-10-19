"""P13-19: shell hooks — config, answers, the process group, consent (R07.18, R02.22)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.blocking import BlockingPool
from indus.foundation.scope import Scope
from indus.plugins import shell_hooks as sh
from indus.plugins.hooks import HookBus, PluginToolHooks
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig, eventually
from tests.fakes.clock import FakeClock

posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX here")


@pytest.fixture
def pool() -> Iterator[BlockingPool]:
    made = BlockingPool("shell-hooks-test", 4)
    yield made
    made.shutdown()


def script(folder: Path, name: str, body: str) -> str:
    """A Python hook script; returns the command line that runs it."""
    path = folder / name
    path.write_text(body, encoding="utf-8")
    return f"{json.dumps(sys.executable)} {json.dumps(str(path))}"


def configured(scope: Scope, hooks: Mapping[str, Any], **more: Any) -> Scope:
    return scope.with_config(DictConfig({"hooks": hooks, **more}))


def registered(scope: Scope, bus: HookBus, **kwargs: Any) -> sh.ShellHookReport:
    kwargs.setdefault("interactive", False)
    kwargs.setdefault("accept", True)
    return sh.register_shell_hooks(bus, scope, **kwargs)


def recording_registry(ran: list[dict[str, Any]]) -> ToolRegistry:
    registry = ToolRegistry()

    def probe(args: Mapping[str, Any]) -> str:
        ran.append(dict(args))
        return "ran"

    registry.register("terminal", "test", {"description": "t", "parameters": {}}, probe)
    return registry


# -- config and answers ----------------------------------------------------------------------------


def test_parse_hooks_keeps_good_entries_and_warns() -> None:
    specs, warnings = sh.parse_hooks(
        {
            "pre_tool_call": [
                {"command": "a.sh", "matcher": " terminal ", "timeout": 5},
                {"matcher": "x"},
                "not a mapping",
                {"command": "b.sh", "timeout": 9999, "fail_closed": True},
            ],
            "pre_llm_call": [{"command": "c.sh", "matcher": "x", "fail_closed": True}],
            "pre_tool_cal": [{"command": "typo.sh"}],
            "post_tool_call": "nope",
        }
    )
    assert [(s.event, s.command, s.matcher, s.timeout_s, s.fail_closed) for s in specs] == [
        ("pre_tool_call", "a.sh", "terminal", 5.0, False),
        ("pre_tool_call", "b.sh", None, sh.MAX_TIMEOUT_S, True),
        ("pre_llm_call", "c.sh", None, sh.DEFAULT_TIMEOUT_S, False),
    ]
    text = "\n".join(warnings)
    for piece in ("has no command", "must be a mapping", "clamped", "only applies to tool events"):
        assert piece in text
    assert "hooks.pre_tool_cal: not an event" in text
    assert "hooks.post_tool_call must be a list" in text
    assert sh.parse_hooks(None) == ([], [])


def test_matcher() -> None:
    spec = sh.HookSpec("pre_tool_call", "x", matcher="terminal|write_file")
    assert spec.matches("terminal") and spec.matches("write_file")
    assert not spec.matches("terminal2") and not spec.matches(None)
    broken = sh.HookSpec("pre_tool_call", "x", matcher="read[")
    assert broken.matches("read[") and not broken.matches("read")
    assert sh.HookSpec("pre_tool_call", "x", matcher="*").matches("anything")
    assert sh.HookSpec("pre_llm_call", "x").matches(None)


def run_of(stdout: str = "", code: int = 0, stderr: str = "") -> sh.HookRun:
    return sh.HookRun(exit_code=code, stdout=stdout, stderr=stderr)


def test_block_dialects() -> None:
    guard = sh.HookSpec("pre_tool_call", "g")
    for answer, message in (
        ({"decision": "block", "reason": "no curl"}, "no curl"),
        ({"action": "block", "message": "stop"}, "stop"),
        (
            {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "x"}},
            "x",
        ),
        ({"decision": "block"}, sh.DEFAULT_BLOCK),
    ):
        assert sh.interpret(guard, run_of(json.dumps(answer))) == {
            "action": "block",
            "message": message,
        }
    ask = sh.interpret(
        guard, run_of(json.dumps({"hookSpecificOutput": {"permissionDecision": "ask"}}))
    )
    assert ask == {"action": "approve"}
    modify = sh.interpret(
        guard, run_of(json.dumps({"hookSpecificOutput": {"updatedInput": {"a": 1}}}))
    )
    assert modify == {"action": "modify", "args": {"a": 1}}
    assert sh.interpret(guard, run_of("not json")) is None
    assert sh.interpret(guard, run_of("")) is None
    # A crash without a decision allows the call (Claude Code convention) unless fail_closed.
    assert sh.interpret(guard, run_of(code=1, stderr="oops")) is None
    closed = sh.HookSpec("pre_tool_call", "g", fail_closed=True)
    assert sh.interpret(closed, run_of(code=1))["action"] == "block"  # type: ignore[index]
    assert sh.interpret(closed, run_of("garbage"))["action"] == "block"  # type: ignore[index]
    # The shell's "cannot run" codes are a start failure: the guard fails closed.
    assert sh.interpret(guard, run_of(code=127, stderr="x.sh: not found"))["action"] == "block"  # type: ignore[index]
    # Other events: context only, and failures fail open.
    llm = sh.HookSpec("pre_llm_call", "c")
    assert sh.interpret(llm, run_of(json.dumps({"additionalContext": " ctx "}))) == {
        "context": "ctx"
    }
    assert sh.interpret(llm, run_of(json.dumps({"decision": "block"}))) is None
    assert sh.interpret(llm, sh.HookRun(timed_out=True)) is None
    assert sh.interpret(llm, run_of(code=127)) is None


def test_payload_shape() -> None:
    pre = sh.build_payload(
        "pre_tool_call",
        {
            "tool_name": "terminal",
            "args": {"command": "ls"},
            "session_id": "s",
            "tool_call_id": "c",
        },
        "/work",
    )
    assert pre == {
        "hook_event_name": "pre_tool_call",
        "session_id": "s",
        "cwd": "/work",
        "profile": "",
        "tool_name": "terminal",
        "tool_input": {"command": "ls"},
        "extra": {"tool_call_id": "c"},
    }
    post = sh.build_payload("post_tool_call", {"tool_name": "t", "args": {}, "result": "ok"})
    assert post["tool_response"] == "ok"
    llm = sh.build_payload(
        "pre_llm_call",
        {
            "user_message": "hi",
            "conversation_history": [{"role": "user"}],
            "model": "m",
            "cwd": "/c",
        },
    )
    assert llm["prompt"] == "hi" and llm["cwd"] == "/c"
    assert "conversation_history" not in llm["extra"] and llm["extra"] == {"model": "m"}


# -- through the bus ------------------------------------------------------------------------------


def test_fake_hook_blocks_a_tool(scope: Scope, tmp_path: Path, pool: BlockingPool) -> None:
    command = script(
        tmp_path,
        "guard.py",
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "if 'curl' in payload['tool_input'].get('command', ''):\n"
        "    print(json.dumps({'decision': 'block', 'reason': 'no curl here'}))\n",
    )
    hooked = configured(scope, {"pre_tool_call": [{"command": command, "matcher": "terminal"}]})
    bus = HookBus(pool=pool)
    assert len(registered(hooked, bus).registered) == 1
    ran: list[dict[str, Any]] = []
    registry = recording_registry(ran)
    seam = PluginToolHooks(bus)

    async def main() -> tuple[str, str]:
        ctx = ToolContext(scope=hooked, session_id="s1")
        blocked = await dispatch(
            ToolCall("terminal", {"command": "curl evil"}), ctx, registry=registry, hooks=seam
        )
        allowed = await dispatch(
            ToolCall("terminal", {"command": "ls"}), ctx, registry=registry, hooks=seam
        )
        return blocked.text, allowed.text

    blocked, allowed = asyncio.run(main())
    assert "no curl here" in blocked
    assert ran == [{"command": "ls"}] and allowed == "ran"


def test_modify_rewrites_args(scope: Scope, tmp_path: Path, pool: BlockingPool) -> None:
    command = script(
        tmp_path,
        "modify.py",
        "import json\nprint(json.dumps({'action': 'modify', 'args': {'command': 'ls -la'}}))\n",
    )
    bus = HookBus(pool=pool)
    registered(configured(scope, {"pre_tool_call": [{"command": command}]}), bus)
    directive = asyncio.run(bus.pre_tool_call("terminal", {"command": "ls", "keep": 1}))
    assert directive.action is None
    assert directive.args == {"command": "ls -la", "keep": 1}


def test_exit_two_blocks_with_stderr(scope: Scope, tmp_path: Path, pool: BlockingPool) -> None:
    command = script(
        tmp_path, "deny.py", "import sys\nsys.stderr.write('policy says no')\nsys.exit(2)\n"
    )
    bus = HookBus(pool=pool)
    registered(configured(scope, {"pre_tool_call": [{"command": command}]}), bus)
    directive = asyncio.run(bus.pre_tool_call("terminal", {}))
    assert directive.action == "block" and directive.message == "policy says no"


def test_context_reaches_pre_llm_call(scope: Scope, tmp_path: Path, pool: BlockingPool) -> None:
    command = script(
        tmp_path,
        "ctx.py",
        "import json, sys\np = json.load(sys.stdin)\n"
        "print(json.dumps({'context': 'you asked: ' + p['prompt']}))\n",
    )
    bus = HookBus(pool=pool)
    registered(configured(scope, {"pre_llm_call": [{"command": command}]}), bus)
    assert asyncio.run(bus.pre_llm_call(user_message="weather?")) == "you asked: weather?"


def test_env_is_scrubbed(scope: Scope, tmp_path: Path, pool: BlockingPool) -> None:
    secret_name = "ANTHROPIC" + "_API_KEY"
    leaky = scope.with_env(
        scope.env.with_overlay({secret_name: "value-for-test", "INDUS_DEBUG_X": "1", "PLAIN": "ok"})
    )
    out = tmp_path / "env.json"
    command = script(
        tmp_path,
        "env.py",
        f"import json, os\nopen({str(out)!r}, 'w').write(json.dumps(dict(os.environ)))\n",
    )
    bus = HookBus(pool=pool)
    registered(configured(leaky, {"pre_llm_call": [{"command": command}]}), bus)
    asyncio.run(bus.pre_llm_call(session_id="s9"))
    seen = json.loads(out.read_text(encoding="utf-8"))
    assert secret_name not in seen and "INDUS_DEBUG_X" not in seen
    assert seen.get("PLAIN") == "ok" and seen.get("INDUS_SESSION_ID") == "s9"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return True


def _spawner(tmp_path: Path, pidfile: Path) -> str:
    return script(
        tmp_path,
        "slow.py",
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n",
    )


def _wait_gone(pid: int) -> bool:
    end = time.monotonic() + 10
    pause = threading.Event()
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        pause.wait(0.05)
    return False


@posix_only
def test_timeout_blocks_and_kills_the_group(
    scope: Scope, tmp_path: Path, pool: BlockingPool
) -> None:
    pidfile = tmp_path / "child.pid"
    command = _spawner(tmp_path, pidfile)
    bus = HookBus(pool=pool)
    registered(configured(scope, {"pre_tool_call": [{"command": command, "timeout": 1}]}), bus)
    directive = asyncio.run(bus.pre_tool_call("terminal", {}))
    assert directive.action == "block"
    assert "did not finish within 1 s" in (directive.message or "")
    assert _wait_gone(int(pidfile.read_text(encoding="utf-8")))


@posix_only
def test_bus_deadline_cancels_and_kills_the_group(
    scope: Scope, tmp_path: Path, pool: BlockingPool, fake_clock: FakeClock
) -> None:
    pidfile = tmp_path / "child.pid"
    command = _spawner(tmp_path, pidfile)
    bus = HookBus(clock=fake_clock, pool=pool)
    registered(configured(scope, {"pre_tool_call": [{"command": command, "timeout": 600}]}), bus)

    async def main() -> Any:
        task = asyncio.ensure_future(bus.pre_tool_call("terminal", {}))
        await eventually(lambda: pidfile.exists() and fake_clock.pending >= 1, limit=20)
        fake_clock.advance(bus.timeout_s)
        return await task

    directive = asyncio.run(main())
    assert directive.action == "block"
    assert _wait_gone(int(pidfile.read_text(encoding="utf-8")))


def test_post_hook_failure_fails_open(scope: Scope, tmp_path: Path, pool: BlockingPool) -> None:
    command = script(tmp_path, "bad.py", "raise SystemExit(3)\n")
    missing = str(tmp_path / "no-such-hook.sh")
    bus = HookBus(pool=pool)
    registered(
        configured(
            scope,
            {
                "post_tool_call": [{"command": command}],
                "pre_llm_call": [{"command": missing}],
            },
        ),
        bus,
    )
    asyncio.run(bus.observe("post_tool_call", tool_name="terminal", args={}, result="x"))
    assert asyncio.run(bus.pre_llm_call(user_message="hi")) is None
    guard_bus = HookBus(pool=pool)
    registered(configured(scope, {"pre_tool_call": [{"command": missing}]}), guard_bus)
    directive = asyncio.run(guard_bus.pre_tool_call("terminal", {}))
    assert directive.action == "block" and "could not start" in (directive.message or "")


# -- consent ---------------------------------------------------------------------------------------


def test_unattended_surface_skips_unconsented(
    scope: Scope, pool: BlockingPool, caplog: pytest.LogCaptureFixture
) -> None:
    hooked = configured(scope, {"pre_tool_call": [{"command": "true"}]})
    asked: list[sh.HookSpec] = []
    bus = HookBus(pool=pool)
    with caplog.at_level(logging.WARNING):
        report = sh.register_shell_hooks(
            bus, hooked, interactive=False, surface="cron", ask=lambda s: asked.append(s) is None
        )
    assert report.registered == [] and len(report.skipped) == 1
    assert asked == [] and not bus.has("pre_tool_call")
    assert "cron never asks" in caplog.text
    assert not (scope.home / sh.CONSENT_FILE).exists()


def test_interactive_consent_recorded_then_revoke_asks_again(
    scope: Scope, pool: BlockingPool
) -> None:
    hooked = configured(scope, {"pre_tool_call": [{"command": "true"}]})
    asked: list[str] = []

    def ask(spec: sh.HookSpec) -> bool:
        asked.append(spec.command)
        return True

    report = sh.register_shell_hooks(HookBus(pool=pool), hooked, interactive=True, ask=ask)
    assert len(report.registered) == 1 and asked == ["true"]
    sh.register_shell_hooks(HookBus(pool=pool), hooked, interactive=True, ask=ask)
    assert asked == ["true"]  # remembered
    store = sh.ConsentStore(scope.home)
    grant = store.granted(report.registered[0])
    assert grant is not None and grant.id == report.registered[0].grant_id
    assert oct((scope.home / sh.CONSENT_FILE).stat().st_mode & 0o777) == "0o600"
    assert [g.id for g in store.revoke(grant.id[:6])] == [grant.id]
    sh.register_shell_hooks(HookBus(pool=pool), hooked, interactive=True, ask=lambda s: False)
    assert store.granted(report.registered[0]) is None
    # A changed command is a new pair: asked again.
    edited = configured(scope, {"pre_tool_call": [{"command": "true --edited"}]})
    sh.register_shell_hooks(HookBus(pool=pool), edited, interactive=True, ask=ask)
    assert asked == ["true", "true --edited"]


def test_auto_accept_channels(scope: Scope, pool: BlockingPool) -> None:
    hooks = {"pre_tool_call": [{"command": "true"}]}
    by_env = configured(scope, hooks).with_env(scope.env.with_overlay({sh.ACCEPT_ENV: "1"}))
    assert sh.accept_from(by_env)
    assert sh.accept_from(configured(scope, hooks, hooks_auto_accept=True))
    assert not sh.accept_from(configured(scope, hooks))
    report = sh.register_shell_hooks(HookBus(pool=pool), by_env, interactive=False)
    assert len(report.registered) == 1
    assert sh.ConsentStore(scope.home).granted(report.registered[0]) is not None


def test_broken_consent_file_counts_for_nothing(scope: Scope, pool: BlockingPool) -> None:
    path = scope.home / sh.CONSENT_FILE
    path.write_text("{broken", encoding="utf-8")
    hooked = configured(scope, {"pre_tool_call": [{"command": "true"}]})
    report = sh.register_shell_hooks(
        HookBus(pool=pool), hooked, interactive=True, ask=lambda s: True
    )
    assert len(report.registered) == 1  # approved for this run
    assert path.read_text(encoding="utf-8") == "{broken"  # never overwritten
    forged = {"grants": [{"hash": "0" * 64, "event": "pre_tool_call", "command": "true"}]}
    path.write_text(json.dumps(forged), encoding="utf-8")
    assert sh.ConsentStore(scope.home).read() == ({}, True)
