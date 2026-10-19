"""The classic CLI's /goal, /subgoal, /loop, /heartbeat, /btw and /moa handlers (P10-21..26)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from indus.cli import launch as launch_mod
from indus.cli.repl.slash_bridge import complete_for
from indus.cli.slash.automation_cmds import NOT_HERE, HostAutomation
from indus.cli.slash.context import AutomationReply
from indus.cli.slash.dispatch import dispatch
from indus.foundation.scope import Scope
from indus.protocol.methods import SessionCreateParams
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn
from tests.unit.cli.slash._fakes import make_ctx, with_config
from tests.unit.service.test_host_inprocess import host_for


@dataclass
class FakeAutomation:
    reply: AutomationReply = field(default_factory=lambda: AutomationReply(("ok",)))
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def run(self, command: str, session_id: str, args: str) -> AutomationReply:
        self.calls.append((command, session_id, args))
        return self.reply


@pytest.mark.parametrize(
    ("line", "command", "args"),
    [
        ('/goal ship it --gate "pytest -q"', "goal", 'ship it --gate "pytest -q"'),
        ("/subgoal add docs", "subgoal", "add docs"),
        ("/loop 5m check", "loop", "5m check"),
        ("/proactive 5m check", "loop", "5m check"),
        ("/hb every 10m look", "heartbeat", "every 10m look"),
        ("/btw what did we decide?", "btw", "what did we decide?"),
        ("/moa review the plan", "moa", "review the plan"),
    ],
)
def test_lines_are_forwarded(scope: Scope, line: str, command: str, args: str) -> None:
    port = FakeAutomation()
    ctx = make_ctx(scope, automation=port)
    result = dispatch(line, ctx)
    assert port.calls == [(command, "s1", args)]
    assert ctx.lines == ["ok"] and result.submit is None


def test_a_reply_with_text_is_sent_as_the_next_message(scope: Scope) -> None:
    port = FakeAutomation(AutomationReply(("Goal set (20 turns): ship it",), "ship it"))
    ctx = make_ctx(scope, automation=port)
    result = dispatch("/goal ship it", ctx)
    assert result.submit == "ship it"
    assert ctx.lines == ["Goal set (20 turns): ship it"]


def test_without_automation_the_command_says_so(scope: Scope) -> None:
    ctx = make_ctx(scope)
    dispatch("/loop 5m x", ctx)
    assert ctx.lines == [NOT_HERE.format(name="loop")]


def test_btw_runs_while_a_turn_is_busy(scope: Scope) -> None:
    port = FakeAutomation(AutomationReply(("Blue.",)))
    ctx = make_ctx(scope, automation=port, is_busy=lambda: True)
    dispatch("/btw colour?", ctx)
    assert ctx.lines == ["Blue."]


def test_moa_completes_preset_names(scope: Scope) -> None:
    scope = with_config(
        scope,
        "moa:\n  presets:\n"
        "    review:\n      reference_models: [{provider: openai, model: m1}]\n"
        "    research:\n      reference_models: [{provider: openai, model: m2}]\n",
    )
    complete = complete_for(make_ctx(scope))
    texts = [c.text for c in complete("/moa re")]
    assert texts == ["research", "review"]
    assert {c.text for c in complete("/moa ")} >= {"on", "off", "status", "research", "review"}
    assert complete("/moa review now ") == []


def test_host_automation_over_a_real_host(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    host = host_for(scope, fake_clock, tmp_path, text_turn("we chose blue"), text_turn("Blue."))
    with host:
        sid = host.create(SessionCreateParams()).session_id
        host.submit(sid, "pick a colour")
        host.wait(sid)

        @dataclass
        class Launch:
            host: object

        ctx = make_ctx(scope, session_id=sid)
        launch_mod.start_automation(Launch(host), ctx, {})  # type: ignore[arg-type]
        assert isinstance(ctx.automation, HostAutomation)
        dispatch("/btw what did we pick?", ctx)
        assert ctx.lines == ["Blue."]
        ctx.lines.clear()
        dispatch("/heartbeat every 30s x", ctx)
        assert ctx.lines == [
            "A heartbeat runs at most once a minute; 30s is too often (at least 60 s)."
        ]
        assert host.run(lambda: host.service.is_idle(sid))
