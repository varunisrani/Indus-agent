"""The agent's stop gates and edit seam (P24-16/17 wiring, D-16.799): what every host gets.

- the tool seam is wrapped in ``LspToolHooks`` once, so a turn's edits are known with no language
  server (the ``pre_verify`` hook sees them);
- the loop asks the gates before a text answer is final; a non-durable gate's rows are scaffold and
  leave the stored turn alternating; the gated answer is the turn's answer when the budget runs out;
- a gateway session's chat reaches tool handlers as ``delivery_origin``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from indus.core.agent import Agent
from indus.core.exit_reasons import ExitReason
from indus.core.stop_gates import FINISH_REASON, StopContext, StopGates
from indus.core.turn_state import LoopOptions, TurnResult
from indus.foundation.scope import Scope
from indus.tools.builtin import write_file
from indus.tools.hooks import NO_HOOKS
from indus.tools.lsp import diagnostics
from indus.tools.lsp.diagnostics import LspToolHooks
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import fake_options, run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.fakes.turn import check_alternation


async def _open(
    scope: Scope, server: FakeModelServer, selection: list[str], **values: Any
) -> Agent:
    return await Agent.start(scope, fake_route(server), selection, fake_options(server, **values))


def test_the_tool_seam_is_wrapped_once(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> tuple[Any, Any]:
        server = FakeModelServer(script_of(text_turn("hi")), clock=fake_clock)
        plain = await _open(scope, server, [])
        wrapped = await _open(scope, server, [], hooks=LspToolHooks(NO_HOOKS))
        seams = plain.runtime.hooks, wrapped.runtime.hooks
        await plain.close()
        await wrapped.close()
        return seams

    plain, wrapped = asyncio.run(main())
    assert isinstance(plain, LspToolHooks) and plain.inner is NO_HOOKS
    assert isinstance(wrapped, LspToolHooks) and wrapped.inner is NO_HOOKS
    assert not isinstance(wrapped.inner, LspToolHooks)


def test_pre_verify_sees_the_turn_edits_without_a_language_server(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    target = tmp_path / "app.py"
    asked: list[dict[str, Any]] = []

    async def pre_verify(**payload: Any) -> str | None:
        asked.append(payload)
        return "Run the tests before you finish." if len(asked) == 1 else None

    registry = ToolRegistry(clock=scope.clock)
    write_file.register_tools(registry)
    server = FakeModelServer(
        script_of(
            tool_batch(("write_file", {"path": str(target), "content": "x = 1\n"})),
            text_turn("Done."),
            text_turn("Tests pass; done."),
        ),
        clock=fake_clock,
    )

    async def main() -> TurnResult:
        assert diagnostics.active() is None  # no language-server tier in this process
        agent = await _open(scope, server, ["file"], registry=registry, pre_verify=pre_verify)
        result = await run_driven(agent.run_turn("write app.py"), fake_clock)
        await agent.close()
        return result

    result = asyncio.run(main())
    assert result.exit_reason == ExitReason.COMPLETED
    assert result.final_text == "Tests pass; done."
    assert [a["changed_paths"] for a in asked] == [[str(target)], [str(target)]]
    assert asked[0]["final_response"] == "Done." and asked[0]["attempt"] == 0
    # pre_verify is not durable: its rows were scaffold and left the stored turn alternating
    stored = [r for r in result.rows if not r.scaffold]
    assert all(r.finish_reason != FINISH_REASON for r in stored)
    check_alternation(stored)
    third = server.model_requests[2].body or {}
    assert third["messages"][-1]["content"] == "Run the tests before you finish."
    # the turn is over: its edit list is gone
    assert diagnostics.turn_edits(result.session_id, result.turn_id) == []


class AlwaysBack:
    name = "always"
    durable = True
    limit = 10

    async def check(self, ctx: StopContext, attempt: int) -> str | None:
        return f"try again ({attempt})"


def test_the_gated_answer_stands_when_the_budget_runs_out(
    scope: Scope, fake_clock: FakeClock
) -> None:
    server = FakeModelServer(
        script_of(text_turn("First answer."), text_turn("Second answer.")), clock=fake_clock
    )

    async def main() -> TurnResult:
        agent = await _open(
            scope,
            server,
            [],
            stop_gates=StopGates([AlwaysBack()]),
            loop=LoopOptions(max_iterations=2, grace=False),
        )
        result = await run_driven(agent.run_turn("go"), fake_clock)
        await agent.close()
        return result

    result = asyncio.run(main())
    assert result.exit_reason == ExitReason.MAX_ITERATIONS
    assert result.final_text == "Second answer."
    stored = [r for r in result.rows if not r.scaffold]
    assert [r.content for r in stored if r.finish_reason == FINISH_REASON] == [
        "First answer.",
        "Second answer.",
    ]
    assert stored[-1].role == "assistant" and stored[-1].content == "Second answer."


def test_a_gateway_chat_reaches_tools_as_delivery_origin(
    scope: Scope, fake_clock: FakeClock
) -> None:
    seen: list[Mapping[str, Any] | None] = []

    async def probe(args: Mapping[str, Any], *, delivery_origin: Any = None) -> str:
        seen.append(delivery_origin)
        return "ok"

    registry = ToolRegistry(clock=scope.clock)
    schema = {"name": "probe", "description": "p", "parameters": {"type": "object"}}
    registry.register("probe", "probe", schema, probe)

    async def origin_of(session_id: str) -> Mapping[str, Any] | None:
        return {"platform": "telegram", "chat_id": "42", "thread_id": "", "user_id": "7"}

    async def main() -> None:
        for lookup in (origin_of, None):
            server = FakeModelServer(
                script_of(tool_batch(("probe", {})), text_turn("ok")), clock=fake_clock
            )
            agent = await _open(scope, server, ["probe"], registry=registry, delivery_origin=lookup)
            await run_driven(agent.run_turn("go"), fake_clock)
            await agent.close()

    asyncio.run(main())
    assert seen == [{"platform": "telegram", "chat_id": "42"}, None]
