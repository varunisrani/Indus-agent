"""P24-16 / P24-17: the stop-gate registry, the LSP gate, verify-on-stop and the pre_verify hook."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import ExecResult
from indus.core.stop_gates import (
    FINISH_REASON,
    PreVerifyGate,
    StopContext,
    StopDecision,
    StopGates,
    VerifyOnStopGate,
    code_paths,
    continuation_rows,
    is_prose,
)
from indus.core.stop_gates_lsp import HEADER, MAX_LSP_NUDGES, LspStopGate
from indus.foundation.clock import SystemClock
from tests.fakes.terminal import ScriptedEnv
from tests.unit.tools.lsp._scripted import checker

WORKSPACES = Path(__file__).resolve().parents[2] / "fixtures" / "verify" / "workspaces"


def ctx(turn: str = "t1", edited: tuple[str, ...] = (), answer: str = "Done.") -> StopContext:
    return StopContext(session_id="s1", turn_id=turn, answer=answer, edited=edited)


# -- the LSP gate ---------------------------------------------------------------------------------


def test_lsp_gate_re_enters_exactly_once_for_a_new_error(tmp_path: Path) -> None:
    path = str(tmp_path / "a.fk")

    async def scenario() -> None:
        check, manager = checker()
        manager.client.gate = asyncio.Event()
        outcome = await check.check_edit(path, "a\n", "a\nBAD(late)\n", owner="s1")
        assert outcome.status == "deferred"
        gates = StopGates([LspStopGate(lambda: check)])
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, manager.client.gate.set)  # the verdict lands while the gate waits
        decision = await gates.run(ctx(edited=(path,)))
        assert decision is not None and decision.gate == "lsp" and decision.durable
        assert decision.nudge.startswith(HEADER) and "bad late" in decision.nudge
        assert await gates.run(ctx(edited=(path,))) is None  # delivered: the answer now stands
        assert gates.fired(ctx()) == {"lsp": 1}

    asyncio.run(scenario())


def test_lsp_gate_quiet_without_checker_owner_or_notes() -> None:
    async def scenario() -> None:
        assert await LspStopGate(lambda: None).check(ctx(), 0) is None
        check, _ = checker()
        anonymous = StopContext(session_id=None, turn_id="t", answer="x")
        assert await LspStopGate(lambda: check).check(anonymous, 0) is None
        assert await LspStopGate(lambda: check).check(ctx(), 0) is None

    asyncio.run(scenario())


def test_lsp_gate_fires_at_most_three_times_a_turn() -> None:
    async def scenario() -> None:
        check, _ = checker()
        gates = StopGates([LspStopGate(lambda: check)])
        fired = 0
        for n in range(5):
            check.post("s1", f"/w/{n}.fk", f"note {n}", None)
            if await gates.run(ctx()) is not None:
                fired += 1
        assert fired == MAX_LSP_NUDGES == 3
        check.post("s1", "/w/x.fk", "next turn", None)
        assert await gates.run(ctx(turn="t2")) is not None  # a new turn starts from zero

    asyncio.run(scenario())


def test_continuation_rows_keep_roles_alternating() -> None:
    durable = StopDecision("lsp", "fix it", True, 1)
    interim, nudge = continuation_rows(durable, "Done.", reasoning="r")
    assert (interim.role, interim.content, interim.finish_reason) == (
        "assistant",
        "Done.",
        FINISH_REASON,
    )
    assert interim.reasoning == "r" and not interim.scaffold
    assert (nudge.role, nudge.content, nudge.scaffold) == ("user", "fix it", False)
    _, scaffold = continuation_rows(StopDecision("verify_on_stop", "x", False, 1), "Done.")
    assert scaffold.scaffold and scaffold.extra["stop_gate"] == "verify_on_stop"


# -- the registry ---------------------------------------------------------------------------------


class Gate:
    def __init__(self, name: str, answer: str | None, limit: int = 1, boom: bool = False) -> None:
        self.name, self.answer, self.limit, self.boom = name, answer, limit, boom
        self.durable = False
        self.calls = 0

    async def check(self, ctx: StopContext, attempt: int) -> str | None:
        self.calls += 1
        if self.boom:
            raise RuntimeError("broken gate")
        return self.answer


def test_gates_run_in_order_and_a_failing_gate_is_skipped() -> None:
    async def scenario() -> None:
        broken = Gate("broken", None, boom=True)
        quiet = Gate("quiet", None)
        loud = Gate("loud", "go on", limit=2)
        later = Gate("later", "never asked")
        gates = StopGates([broken, quiet])
        gates.register(loud)
        gates.register(later)
        with pytest.raises(ValueError, match="registered already"):
            gates.register(Gate("loud", None))
        assert gates.names == ["broken", "quiet", "loud", "later"]
        first = await gates.run(ctx())
        assert first is not None and (first.gate, first.attempt) == ("loud", 1)
        assert later.calls == 0
        second = await gates.run(ctx())
        third = await gates.run(ctx())
        assert second is not None and second.attempt == 2
        assert third is not None and third.gate == "later"  # loud used up its two
        assert await gates.run(ctx()) is None
        gates.end_turn(ctx())
        assert gates.fired(ctx()) == {}

    asyncio.run(scenario())


# -- verify-on-stop -------------------------------------------------------------------------------


def test_prose_paths() -> None:
    assert is_prose("/w/README.md") and is_prose("/w/LICENSE") and is_prose("notes.TXT")
    assert not is_prose("/w/app.py") and not is_prose("/w/Makefile")
    assert code_paths(["/w/a.md", "/w/b.py", "/w/b.py"]) == ["/w/b.py"]


def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    shutil.copytree(WORKSPACES / "make_test", ws)
    (ws / ".git").mkdir()
    return ws


def verify_gate(env: ScriptedEnv, enabled: bool = True) -> VerifyOnStopGate:
    async def host_for(ctx: StopContext) -> Any:
        return env

    return VerifyOnStopGate(host_for, enabled=enabled)


def test_verify_on_stop_is_off_by_default(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    env = ScriptedEnv([ExecResult(output="boom", returncode=1)])

    async def scenario() -> None:
        gate = VerifyOnStopGate(verify_gate(env).__dict__["_host_for"])
        assert not gate.enabled()
        assert await gate.check(ctx(edited=(str(ws / "main.c"),)), 0) is None
        assert env.calls == []

    asyncio.run(scenario())


def test_verify_on_stop_sends_the_failure_back(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    env = ScriptedEnv(
        [
            ExecResult(output="build ok", returncode=0),
            ExecResult(output="assert 1 == 2", returncode=2),
        ]
    )

    async def scenario() -> None:
        gates = StopGates([verify_gate(env)])
        decision = await gates.run(ctx(edited=(str(ws / "main.c"), str(ws / "README.md"))))
        assert decision is not None and decision.gate == "verify_on_stop" and not decision.durable
        assert "[test] make test (exit code 2)" in decision.nudge
        assert "assert 1 == 2" in decision.nudge and str(ws / "main.c") in decision.nudge
        assert [c.command for c in env.calls] == ["make build", "make test"]
        assert all(c.cwd == str(ws) for c in env.calls)

    asyncio.run(scenario())


def test_verify_on_stop_passes_skips_prose_and_is_capped(tmp_path: Path) -> None:
    ws = workspace(tmp_path)

    async def scenario() -> None:
        passing = ScriptedEnv()
        assert await verify_gate(passing).check(ctx(edited=(str(ws / "a.c"),)), 0) is None
        assert len(passing.calls) == 2
        prose = ScriptedEnv()
        assert await verify_gate(prose).check(ctx(edited=(str(ws / "NOTES.md"),)), 0) is None
        assert prose.calls == []
        failing = ScriptedEnv([ExecResult(output="x", returncode=1) for _ in range(10)])
        gates = StopGates([verify_gate(failing)])
        edited = (str(ws / "a.c"),)
        results = [await gates.run(ctx(edited=edited)) for _ in range(4)]
        assert [r is not None for r in results] == [True, True, False, False]  # two per turn
        no_host = VerifyOnStopGate(lambda c: _none(), enabled=True)
        assert await no_host.check(ctx(edited=edited), 0) is None

    asyncio.run(scenario())


async def _none() -> None:
    return None


# -- pre_verify -----------------------------------------------------------------------------------


def test_pre_verify_hook_after_code_edits_only_and_capped() -> None:
    asked: list[dict[str, Any]] = []

    async def hook(**payload: Any) -> str | None:
        asked.append(payload)
        return "run the integration tests first"

    async def scenario() -> None:
        gates = StopGates([PreVerifyGate(hook)])
        assert await gates.run(ctx(edited=("/w/README.md",))) is None and asked == []
        edited = ("/w/app.py",)
        fired = [await gates.run(ctx(edited=edited)) for _ in range(5)]
        assert [f is not None for f in fired] == [True, True, True, False, False]
        assert asked[0]["changed_paths"] == ["/w/app.py"]
        assert asked[0]["final_response"] == "Done." and [a["attempt"] for a in asked] == [0, 1, 2]
        assert fired[0] is not None and fired[0].nudge == "run the integration tests first"

    asyncio.run(scenario())


def test_gate_order_lsp_then_verify_then_hook(tmp_path: Path) -> None:
    ws = workspace(tmp_path)

    async def hook(**payload: Any) -> str | None:
        return "hook says"

    async def scenario() -> None:
        check, _ = checker(SystemClock())
        env = ScriptedEnv([ExecResult(output="x", returncode=1) for _ in range(4)])
        gates = StopGates([LspStopGate(lambda: check), verify_gate(env), PreVerifyGate(hook)])
        check.post("s1", str(ws / "a.c"), "lsp says", None)
        edited = (str(ws / "a.c"),)
        order = [await gates.run(ctx(edited=edited)) for _ in range(3)]
        assert [d.gate for d in order if d is not None] == [
            "lsp",
            "verify_on_stop",
            "verify_on_stop",
        ]
        fourth = await gates.run(ctx(edited=edited))
        assert fourth is not None and fourth.gate == "pre_verify"

    asyncio.run(scenario())
