"""``browser_exec`` (P24-39) over a fake CLI runner: argv, env, URL policy, caps, spill."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from indus.foundation.scope import Scope
from indus.tools.builtin.browser.exec_cli import (
    CLI_NAME,
    MAX_RESULT_CHARS,
    STEP_OUTPUT_CAP,
    BrowserExec,
    RunOutcome,
    clamp_timeout,
    register_exec_tool,
)
from indus.tools.registry import ToolRegistry
from indus.tools.spill import OPEN_TAG, HostSpill, spill_if_needed
from tests.fakes.resolver import FakeResolver
from tests.fakes.secret_store import MemorySecretStore
from tests.unit.tools.builtin.browser._rig import PUBLIC, make_clock, make_scope_for

PROVIDER_KEY = "OPENAI_API_" + "KEY"
FAKE_VALUE = "sk-" + "w" * 24
CLI = Path("/opt/indus/pm/agent-browser")


class FakeRunner:
    def __init__(self, answers: list[RunOutcome] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str], float]] = []
        self.answers = list(answers or [])

    async def run(
        self, argv: Sequence[str], env: Mapping[str, str], timeout_s: float
    ) -> RunOutcome:
        self.calls.append((list(argv), dict(env), timeout_s))
        if self.answers:
            return self.answers.pop(0)
        return RunOutcome(0, json.dumps({"success": True, "data": "ok"}), "")


def make(tmp_path: Path, runner: FakeRunner, *, cli: bool = True) -> tuple[BrowserExec, Scope]:
    resolver = FakeResolver({"example.com": [PUBLIC], "intranet.example": ["10.0.0.5"]})
    tool = BrowserExec(
        runner=runner, locate=lambda s, n: CLI if cli and n == CLI_NAME else None, resolver=resolver
    )
    scope = make_scope_for(tmp_path, make_clock()).with_secrets(
        MemorySecretStore({PROVIDER_KEY: FAKE_VALUE})
    )
    return tool, scope


def run(
    tool: BrowserExec, scope: Scope, args: dict[str, Any], task_id: str = "t1"
) -> dict[str, Any]:
    return json.loads(asyncio.run(tool(args, task_id=task_id, scope=scope)))


def test_steps_become_cli_calls_with_a_clean_env(tmp_path: Path) -> None:
    runner = FakeRunner()
    tool, scope = make(tmp_path, runner)
    out = run(
        tool,
        scope,
        {
            "steps": [
                {"action": "open", "url": "https://example.com"},
                {"action": "snapshot"},
                {"action": "click", "ref": "@e3"},
                {"action": "type", "ref": "e4", "text": "hello"},
                {"action": "press", "key": "Enter"},
                {"action": "scroll", "direction": "down"},
                {"action": "back"},
                {"action": "screenshot"},
            ]
        },
    )
    assert out["success"] is True and out["session"] == "indus-t1"
    tails = [call[0][1:] for call in runner.calls]
    prefix = ["--session", "indus-t1", "--json"]
    assert tails[0] == [*prefix, "open", "https://example.com"]
    assert tails[1] == [*prefix, "snapshot"]
    assert tails[2] == [*prefix, "click", "@e3"]
    assert tails[3] == [*prefix, "fill", "@e4", "hello"]
    assert tails[4] == [*prefix, "press", "Enter"]
    assert tails[5] == [*prefix, "scroll", "down"]
    assert tails[6] == [*prefix, "back"]
    assert tails[7][:4] == [*prefix, "screenshot"]
    assert Path(tails[7][4]).parent == tmp_path / "home" / "cache" / "browser" / "exec"
    assert out["steps"][7]["path"] == tails[7][4]
    assert all(call[0][0] == str(CLI) for call in runner.calls)
    env = runner.calls[0][1]
    assert PROVIDER_KEY not in env and FAKE_VALUE not in env.values()


def test_missing_cli_gives_an_install_hint(tmp_path: Path) -> None:
    runner = FakeRunner()
    tool, scope = make(tmp_path, runner, cli=False)
    out = run(tool, scope, {"steps": [{"action": "snapshot"}]})
    assert out["success"] is False and "indus pm install agent-browser" in out["error"]
    assert runner.calls == []
    assert not tool.ready(scope)


def test_refused_url_stops_the_call_before_the_cli_runs(tmp_path: Path) -> None:
    runner = FakeRunner()
    tool, scope = make(tmp_path, runner)
    out = run(
        tool,
        scope,
        {"steps": [{"action": "snapshot"}, {"action": "open", "url": "http://intranet.example/"}]},
    )
    assert out["success"] is False and out["error"].startswith("Blocked:")
    assert runner.calls == []
    out = run(
        tool,
        scope,
        {"steps": [{"action": "eval", "code": "fetch('http://169.254.169.254/latest')"}]},
    )
    assert out["success"] is False and "metadata" in out["error"]
    assert runner.calls == []


def test_step_output_is_capped(tmp_path: Path) -> None:
    runner = FakeRunner([RunOutcome(0, json.dumps({"success": True, "data": "x" * 9000}), "")])
    tool, scope = make(tmp_path, runner)
    out = run(tool, scope, {"steps": [{"action": "snapshot"}]})
    text = out["steps"][0]["output"]
    assert len(text) < STEP_OUTPUT_CAP + 100 and "more characters cut" in text


def test_failing_step_stops_the_rest(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            RunOutcome(0, json.dumps({"success": True, "data": "opened"}), ""),
            RunOutcome(1, json.dumps({"success": False, "error": "no element @e9"}), "trace"),
        ]
    )
    tool, scope = make(tmp_path, runner)
    out = run(
        tool,
        scope,
        {
            "steps": [
                {"action": "open", "url": "https://example.com"},
                {"action": "click", "ref": "@e9"},
                {"action": "snapshot"},
            ]
        },
    )
    assert out["success"] is False and out["skipped_steps"] == 1
    assert out["steps"][1] == {
        "action": "click",
        "ok": False,
        "error": "no element @e9",
        "stderr": "trace",
    }
    assert len(runner.calls) == 2


def test_timed_out_step_is_reported_and_stops(tmp_path: Path) -> None:
    runner = FakeRunner([RunOutcome(None, "", "", timed_out=True)])
    tool, scope = make(tmp_path, runner)
    out = run(tool, scope, {"steps": [{"action": "snapshot"}, {"action": "back"}], "timeout_s": 7})
    assert out["success"] is False and "7 s" in out["steps"][0]["error"]
    assert runner.calls[0][2] == 7
    assert len(runner.calls) == 1


def test_timeout_is_clamped() -> None:
    assert clamp_timeout(1) == 5
    assert clamp_timeout(99_999) == 1800
    assert clamp_timeout("oops") == 300
    assert clamp_timeout(True) == 300


def test_invalid_input_is_refused_before_running(tmp_path: Path) -> None:
    runner = FakeRunner()
    tool, scope = make(tmp_path, runner)
    assert run(tool, scope, {"steps": []})["success"] is False
    assert "unknown action" in run(tool, scope, {"steps": [{"action": "rm"}]})["error"]
    assert "at most 25" in run(tool, scope, {"steps": [{"action": "back"}] * 26})["error"]
    bad = run(tool, scope, {"steps": [{"action": "back"}], "session": "../etc"})
    assert "session must be" in bad["error"]
    named = run(tool, scope, {"steps": [{"action": "back"}], "session": "r7k2"})
    assert named["session"] == "indus-r7k2"
    assert run(tool, scope, {"steps": [{"action": "click", "ref": "button"}]})["success"] is False
    assert len(runner.calls) == 1


def test_secrets_in_cli_output_are_masked(tmp_path: Path) -> None:
    runner = FakeRunner([RunOutcome(0, json.dumps({"success": True, "data": FAKE_VALUE}), "")])
    tool, scope = make(tmp_path, runner)
    text = asyncio.run(tool({"steps": [{"action": "snapshot"}]}, task_id="t", scope=scope))
    assert FAKE_VALUE not in text


def test_output_past_the_budget_is_spilled(tmp_path: Path) -> None:
    pages = [RunOutcome(0, json.dumps({"success": True, "data": "y" * 3900}), "")] * 25
    runner = FakeRunner(list(pages))
    tool, scope = make(tmp_path, runner)
    text = asyncio.run(tool({"steps": [{"action": "snapshot"}] * 25}, task_id="t", scope=scope))
    assert len(text) > MAX_RESULT_CHARS
    registry = ToolRegistry()
    register_exec_tool(registry, tool)
    entry = registry.get("browser_exec")
    assert entry is not None and entry.max_result_chars == MAX_RESULT_CHARS
    spilled = spill_if_needed(
        text,
        "browser_exec",
        "call-1",
        sink=HostSpill(scope.home),
        registered_cap=entry.max_result_chars,
    )
    assert spilled.startswith(OPEN_TAG)
    saved = scope.home / "cache" / "spill" / "call-1.txt"
    assert saved.read_text() == text
