"""The per-tool approval gate (P13-07) and the plugin ``approve`` directive that uses it."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.blocking import BlockingPool
from indus.foundation.scope import Scope
from indus.plugins.hooks import HookBus, PluginToolHooks
from indus.safety.approvals.decision import ApprovalState, session_key
from indus.safety.approvals.tool_gate import ToolGateVerdict, approve_tool_call
from indus.tools.calls import ToolCall, ToolContext
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry
from tests.fakes.broker import FakeBroker, Reply
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import terminal_scope


def gate(
    scope: Scope | None,
    broker: FakeBroker | None,
    state: ApprovalState,
    **kwargs: Any,
) -> ToolGateVerdict:
    async def go() -> ToolGateVerdict:
        return await approve_tool_call(
            "do the thing",
            "it changes things",
            "test:thing",
            scope=scope,
            session_id="s1",
            broker=broker,
            state=state,
            **kwargs,
        )

    return asyncio.run(go())


@pytest.fixture
def state() -> ApprovalState:
    return ApprovalState()


def test_no_profile_blocks(fake_clock: FakeClock, state: ApprovalState) -> None:
    verdict = gate(None, FakeBroker(fake_clock), state)
    assert not verdict.approved and verdict.outcome == "no_scope"


def test_the_person_decides(tmp_path: Path, fake_clock: FakeClock, state: ApprovalState) -> None:
    scope = terminal_scope(tmp_path, clock=fake_clock)
    broker = FakeBroker(
        fake_clock, replies=[Reply(choice="deny", reason="no"), Reply(choice="session")]
    )
    denied = gate(scope, broker, state)
    assert not denied.approved and denied.outcome == "denied" and '"no"' in (denied.message or "")
    assert gate(scope, broker, state).approved
    assert state.pattern_approved(session_key(scope, "s1"), "test:thing")
    again = gate(scope, broker, state)
    assert again.approved and again.outcome == "session_approved"
    assert len(broker.requests) == 2


def test_nobody_answering_is_not_a_refusal(
    tmp_path: Path, fake_clock: FakeClock, state: ApprovalState
) -> None:
    scope = terminal_scope(tmp_path, {"approvals": {"timeout": 30}}, clock=fake_clock)
    broker = FakeBroker(fake_clock, default=Reply(action="timeout"))

    async def go() -> ToolGateVerdict:
        return await broker.run(
            approve_tool_call(
                "x", "y", "test:x", scope=scope, session_id="s1", broker=broker, state=state
            )
        )

    verdict = asyncio.run(go())
    assert not verdict.approved and verdict.outcome == "timeout"


@pytest.mark.parametrize(
    ("config", "run_context", "with_broker", "approved"),
    [
        ({}, "cron", True, False),
        ({"approvals": {"cron_mode": "approve"}}, "cron", True, True),
        ({}, "single_query", True, False),
        ({}, "interactive", False, False),
        ({"approvals": {"unattended_mode": "approve"}}, "interactive", False, True),
        ({"approvals": {"mode": "off"}}, "cron", False, True),
        ({}, "weird", False, False),
    ],
)
def test_order_without_a_person(
    tmp_path: Path,
    fake_clock: FakeClock,
    state: ApprovalState,
    config: dict[str, Any],
    run_context: str,
    with_broker: bool,
    approved: bool,
) -> None:
    scope = terminal_scope(tmp_path, config, clock=fake_clock)
    broker = FakeBroker(fake_clock)
    verdict = gate(scope, broker if with_broker else None, state, run_context=run_context)
    assert verdict.approved is approved
    assert broker.requests == []


def test_auto_approve_switch(tmp_path: Path, fake_clock: FakeClock, state: ApprovalState) -> None:
    scope = terminal_scope(tmp_path, clock=fake_clock)
    state.set_auto_approve(session_key(scope, "s1"), True)
    assert gate(scope, None, state).outcome == "auto_approved"


def test_smart_judge_answers_first(
    tmp_path: Path, fake_clock: FakeClock, state: ApprovalState
) -> None:
    scope = terminal_scope(tmp_path, {"approvals": {"mode": "smart"}}, clock=fake_clock)
    broker = FakeBroker(fake_clock)

    async def judge(messages: Any, **_: Any) -> str:
        return "APPROVE"

    verdict = gate(scope, broker, state, judge=judge)
    assert verdict.approved and verdict.outcome == "smart_approved"
    assert broker.requests == []


def test_a_failing_gate_blocks(tmp_path: Path, fake_clock: FakeClock, state: ApprovalState) -> None:
    scope = terminal_scope(tmp_path, clock=fake_clock)

    class Broken:
        async def ask(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("surface gone")

    verdict = gate(scope, Broken(), state)  # type: ignore[arg-type]
    assert not verdict.approved and verdict.outcome == "error"


# -- the plugin approve directive ---------------------------------------------------------------


@pytest.fixture
def pool() -> Iterator[BlockingPool]:
    made = BlockingPool("tool-gate-test", 4, queue_limit=4)
    yield made
    made.shutdown()


@pytest.mark.parametrize(("choice", "runs"), [("once", True), ("deny", False)])
def test_plugin_approve_asks_through_the_gate(
    tmp_path: Path, fake_clock: FakeClock, pool: BlockingPool, choice: str, runs: bool
) -> None:
    scope = terminal_scope(tmp_path, clock=fake_clock)
    bus = HookBus(clock=fake_clock, pool=pool)
    bus.register("pre_tool_call", lambda **_: {"action": "approve", "message": "risky?"})
    hooks = PluginToolHooks(bus)
    ran: list[Mapping[str, Any]] = []
    registry = ToolRegistry()

    def probe(args: Mapping[str, Any]) -> str:
        ran.append(dict(args))
        return json.dumps({"ok": True})

    registry.register("probe", "test", {"description": "p", "parameters": {}}, probe)
    broker = FakeBroker(fake_clock, default=Reply(choice=choice))
    ctx = ToolContext(scope=scope, session_id="s-plugin", extras={"broker": broker})

    async def go() -> Any:
        return await dispatch(ToolCall("probe", {}), ctx, registry=registry, hooks=hooks)

    result = asyncio.run(go())
    assert result.is_error is not runs
    assert (ran != []) is runs
    (request,) = broker.requests
    assert "probe (a plugin asked for approval)" in getattr(request, "command", "")
    assert getattr(request, "description", "") == "risky?"
