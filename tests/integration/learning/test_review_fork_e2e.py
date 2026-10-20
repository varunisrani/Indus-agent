"""P09-15 end to end (the M2 learning slice): a real ``Agent`` on the fake model runs ten turns,
the trigger starts a background review, the scripted review saves a memory entry and creates a
skill, the session's own prompt stays the same, and a new session's skills index lists the skill.
"""

from __future__ import annotations

import asyncio

from indus.core.agent import Agent
from indus.foundation.scope import Scope
from indus.learning.memory.store import MemoryStore
from indus.learning.memory.tool import memory_wiring
from indus.learning.memory.tool import register_tools as register_memory
from indus.learning.review.fork import install_review
from indus.learning.skills.manage_tool import register_tools as register_skill_manage
from indus.learning.skills.prompt_index import IndexOptions, build_index, index_rows
from indus.learning.skills.resolver import clear_scan_cache, load_catalog
from indus.learning.skills.usage import SkillUsage
from indus.learning.skills.view_tools import register_tools as register_skill_views
from indus.protocol.events import EventBase, LearningReviewDone
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import fake_options
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.unit.learning.review.helpers import skill_write_origins

SKILL = """---
name: release-notes
description: Draft release notes from merged changes.
license: MIT
---

# Release notes

## When to use
- The user asks for release notes.

## Procedure
1. List the merged changes since the last tag.
2. Group them by user-facing effect.

## Pitfalls
- Leave internal refactors out unless they change behaviour.

## Verification
- Every line names a change a user can notice.
"""

# A real-time bound that only turns a hung session into a failure.
SESSION_LIMIT_S = 60.0


def test_review_writes_memory_and_a_skill_the_next_session_sees(
    scope: Scope, fake_clock: FakeClock
) -> None:
    turns = [text_turn(f"Done with step {n}.") for n in range(10)]
    review = [
        tool_batch(
            (
                "memory",
                {"action": "add", "target": "memory", "content": "Releases are tagged vX.Y."},
            ),
            ("skill_manage", {"action": "create", "name": "release-notes", "content": SKILL}),
        ),
        text_turn("Saved one fact and one skill."),
    ]
    server = FakeModelServer(script_of(*turns, *review), clock=fake_clock)
    registry = ToolRegistry(clock=fake_clock)
    register_memory(registry)
    register_skill_views(registry)
    register_skill_manage(registry)
    wiring = memory_wiring(scope)
    events: list[EventBase] = []

    async def main() -> None:
        agent = await Agent.start(
            scope,
            fake_route(server),
            ["memory", "skills"],
            fake_options(server, registry=registry, inline={"memory": wiring.execute}),
            events.append,
        )
        runner = install_review(agent)
        for n in range(10):
            await runner.cancel_for_live_turn()
            await agent.run_turn(f"step {n}")
        prompt = agent.runtime.prompts.prompt
        assert runner.busy  # the tenth turn started the review in the background
        while runner.busy:
            await checkpoint()
        outcome = runner.last
        assert outcome is not None and outcome.status == "done"
        assert outcome.summary_lines == ("Memory updated", "Skill release-notes created")
        assert agent.runtime.prompts.prompt == prompt  # this session's prompt never changes
        assert agent.usage.side_calls.get("review") == 2
        done = [e for e in events if isinstance(e, LearningReviewDone)]
        assert [e.summary_lines for e in done] == [
            ["Memory updated", "Skill release-notes created"]
        ]
        await agent.close()

    with skill_write_origins() as origins:
        # The fake clock stays still: driving it while skill_manage runs on a worker thread lets
        # the tool's 420 s call deadline (on this clock) expire within a fraction of a real second
        # under load. Nothing here waits on it but watchdogs and deadlines.
        asyncio.run(asyncio.wait_for(main(), SESSION_LIMIT_S))

    assert MemoryStore.for_scope(scope).entries("memory") == ["Releases are tagged vX.Y."]
    assert origins == ["background_review"]
    usage = SkillUsage(scope.home / "skills", clock=fake_clock)
    assert usage.is_curator_managed("release-notes")  # a review-made skill is the curator's
    clear_scan_cache()
    index = build_index(load_catalog(scope), IndexOptions(tools=frozenset({"skill_view"})))
    assert "release-notes" in index_rows(index)  # a new session's prompt lists the skill
