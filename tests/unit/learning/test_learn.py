"""P09-17: ``/learn`` and ``/refine``."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from indus.cli.slash.context import SlashContext
from indus.cli.slash.learning_cmds import HANDLERS, cmd_learn, cmd_refine
from indus.core.agent import Agent
from indus.foundation.scope import Scope
from indus.learning import pending
from indus.learning.learn import (
    KNOWLEDGE_BASE_RULES,
    SKILL_STANDARDS,
    SOURCE_RULES,
    build_learn_prompt,
    learn_turn,
    refine,
)
from indus.learning.memory.store import MemoryStore, Op
from indus.learning.review.fork import ReviewRunner
from indus.learning.review.prompts import NOTHING
from indus.learning.skills.manage_tool import register_tools as register_skill_manage
from indus.learning.skills.usage import SkillUsage
from indus.learning.skills.view_tools import register_tools as register_skill_views
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import fake_options, run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.unit.learning._helpers_a import with_config
from tests.unit.learning.review.helpers import (
    ScriptedCaller,
    calls,
    make_parent,
    skill_write_origins,
    text,
)

# -- /learn ---------------------------------------------------------------------------------------


def test_learn_prompt_carries_the_request_and_our_standards() -> None:
    prompt = build_learn_prompt("./docs focus on the auth flow")
    assert prompt.startswith("[/learn]")
    assert "The request:\n./docs focus on the auth flow" in prompt
    for block in (SKILL_STANDARDS, KNOWLEDGE_BASE_RULES, SOURCE_RULES):
        assert block in prompt
    order = ["## When to use", "## Procedure", "## Pitfalls", "## Verification"]
    positions = [SKILL_STANDARDS.index(h) for h in order]
    assert positions == sorted(positions)
    assert "at most 60 characters" in SKILL_STANDARDS
    assert "Never" in SKILL_STANDARDS and "invent" in SKILL_STANDARDS


def test_empty_learn_means_this_conversation() -> None:
    assert "the work we just did in this conversation" in build_learn_prompt("  ")


def test_cli_learn_submits_one_user_turn_with_the_standards(scope: Scope) -> None:
    assert HANDLERS["learn"] is cmd_learn and HANDLERS["refine"] is cmd_refine
    result = cmd_learn(cast(SlashContext, None), "./docs")
    assert result.submit is not None

    async def body() -> None:
        caller = ScriptedCaller([text("Saved the skill docs-guide.")])
        parent = make_parent(scope, caller)
        assert result.submit is not None
        await parent.turn(result.submit)
        sent = caller.requests[0].messages
        users = [m for m in sent if m["role"] == "user"]
        assert len(users) == 1
        assert SKILL_STANDARDS in users[0]["content"] and "./docs" in users[0]["content"]

    asyncio.run(body())


def test_learn_turn_creates_a_learn_origin_skill(scope: Scope, fake_clock: FakeClock) -> None:
    skill = "---\nname: docs-guide\ndescription: Find answers in the project docs.\n---\n\nBody.\n"
    server = FakeModelServer(
        script_of(
            tool_batch(
                ("skill_manage", {"action": "create", "name": "docs-guide", "content": skill})
            ),
            text_turn("Saved docs-guide."),
        ),
        clock=fake_clock,
    )
    registry = ToolRegistry(clock=fake_clock)
    register_skill_views(registry)
    register_skill_manage(registry)

    async def main() -> None:
        agent = await Agent.start(
            scope, fake_route(server), ["skills"], fake_options(server, registry=registry)
        )
        running = await learn_turn(agent, "./docs")
        assert pending.current_origin() == "foreground"  # the binding stayed with the turn
        result = await running.task
        assert result.final_text == "Saved docs-guide."
        user = result.rows[result.current_turn_user_index]
        assert user.content == "/learn ./docs"
        assert SKILL_STANDARDS in str(user.api_content)
        await agent.close()

    with skill_write_origins() as origins:
        asyncio.run(run_driven(main(), fake_clock))
    assert origins == ["learn"]  # the turn ran with the write origin ``learn``
    usage = SkillUsage(scope.home / "skills", clock=fake_clock)
    assert usage.get("docs-guide")["origin"] == "learn"
    assert not usage.is_curator_managed("docs-guide")


# -- /refine --------------------------------------------------------------------------------------


async def _session(scope: Scope, fork: ScriptedCaller, **values: Any) -> tuple[Any, ReviewRunner]:
    parent = make_parent(scope, ScriptedCaller([text("hi")]), **values)
    await parent.turn("we deploy with make release")
    return parent, ReviewRunner(parent.review_parent(caller=fork))


def test_refine_runs_with_reviews_disabled_and_puts_the_focus_first(scope: Scope) -> None:
    off = with_config(scope, {"learning": {"review": {"enabled": False}}})

    async def body() -> None:
        fork = ScriptedCaller([text(NOTHING)])
        _, runner = await _session(off, fork)
        started = await refine(runner, focus="  the deploy steps ")
        assert started.started and started.task is not None
        assert started.message.endswith("(focus: the deploy steps).")
        outcome = await started.task
        assert outcome.status == "done" and outcome.kinds == {"memory", "skills"}
        prompt = fork.requests[0].messages[-1]["content"]
        assert "Put it ahead of everything above:\nthe deploy steps" in prompt
        assert prompt.index("the deploy steps") < prompt.index("In this review you may call")

    asyncio.run(body())


def test_refine_memory_remove_is_applied_directly(scope: Scope) -> None:
    async def body() -> None:
        MemoryStore.for_scope(scope).apply("memory", [Op("add", "Deploys use make ship.")])
        remove = {"action": "remove", "target": "memory", "old_text": "make ship"}
        fork = ScriptedCaller([calls(("memory", remove)), text("Removed the stale fact.")])
        _, runner = await _session(scope, fork)
        started = await refine(runner)
        assert started.task is not None
        outcome = await started.task
        assert MemoryStore.for_scope(scope).entries("memory") == []
        assert pending.PendingStore(scope.home, scope.clock).count("memory") == 0
        assert outcome.summary_lines == ("Memory updated",)

    asyncio.run(body())


def test_refine_refusals(scope: Scope) -> None:
    async def body() -> None:
        empty = make_parent(scope, ScriptedCaller([]))
        runner = ReviewRunner(empty.review_parent())
        assert not (await refine(runner)).started  # no messages yet

        bare = make_parent(scope, ScriptedCaller([text("hi")]), tools=("read_file",))
        await bare.turn("hello")
        result = await refine(ReviewRunner(bare.review_parent()))
        assert not result.started and "neither memory nor skill_manage" in result.message

    asyncio.run(body())


class _Port:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def refine(self, session_id: str, focus: str | None) -> str:
        self.calls.append((session_id, focus))
        return "Reviewing this conversation in the background."


def test_cli_refine_uses_the_port_when_it_can(scope: Scope) -> None:
    said: list[str] = []
    port = _Port()
    ctx = SlashContext(scope=scope, port=cast(Any, port), session_id="s1", out=said.append)
    cmd_refine(ctx, "  deploys ")
    assert port.calls == [("s1", "deploys")]
    assert said == ["Reviewing this conversation in the background."]

    said.clear()
    bare = SlashContext(scope=scope, port=cast(Any, object()), session_id="s1", out=said.append)
    cmd_refine(bare, "")
    assert said == ["/refine is not available in this session yet."]
