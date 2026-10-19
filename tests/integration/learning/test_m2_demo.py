"""The P09 "Done when" demo (M2 learning slice), on the fake model and the injected clock.

A 10-turn session triggers the background review, which saves a memory entry and creates a skill
while the session's own prompt stays the same; a new ``indus -z`` session shows the memory block
and lists the skill; ``/release-notes`` runs it; ``indus curator status`` counts it as managed;
31 idle days later the curator archives it and ``indus curator restore`` brings it back.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from indus.cli import oneshot
from indus.cli.args import CliState, GlobalArgs, HostOverrides
from indus.cli.slash.dispatch import dispatch
from indus.cli.slash.skill_cmds import skill_commands
from indus.core.agent import Agent
from indus.foundation.scope import Scope
from indus.learning.curator.archive import list_archived
from indus.learning.host import tool_registry
from indus.learning.memory.tool import memory_wiring
from indus.learning.review.fork import install_review
from indus.learning.skills.resolver import clear_scan_cache
from tests.fakes.agent import any_window, fake_options, run_driven
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.unit.cli.commands.cmdrun import run_cli
from tests.unit.cli.slash._fakes import make_ctx

FACT = "Releases are tagged vX.Y."
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


@pytest.fixture(autouse=True)
def in_tmp(tmp_path: Path) -> Iterator[None]:
    here = os.getcwd()
    os.chdir(tmp_path)
    yield
    os.chdir(here)
    clear_scan_cache()


def review_session(scope: Scope, clock: FakeClock) -> None:
    turns = [text_turn(f"Done with step {n}.") for n in range(10)]
    review = [
        tool_batch(
            ("memory", {"action": "add", "target": "memory", "content": FACT}),
            ("skill_manage", {"action": "create", "name": "release-notes", "content": SKILL}),
        ),
        text_turn("Saved one fact and one skill."),
    ]
    server = FakeModelServer(script_of(*turns, *review), clock=clock)
    wiring = memory_wiring(scope)
    options = fake_options(server, registry=tool_registry(clock), inline={"memory": wiring.execute})

    async def main() -> None:
        agent = await Agent.start(scope, fake_route(server), ["memory", "skills"], options)
        runner = install_review(agent)
        agent.runtime.before_turn.append(runner.cancel_for_live_turn)
        for n in range(10):
            await agent.run_turn(f"step {n}")
        prompt = agent.runtime.prompts.prompt
        while runner.busy:
            await checkpoint()
        assert runner.last is not None and runner.last.summary_lines == (
            "Memory updated",
            "Skill release-notes created",
        )
        assert agent.runtime.prompts.prompt == prompt  # this session's prompt never changes
        await agent.close()

    asyncio.run(run_driven(main(), clock))


def new_cli_session(scope: Scope) -> str:
    server = FakeModelServer(script_of(text_turn("ok")))
    state = CliState(
        scope=scope,
        overrides=HostOverrides(
            route=fake_route(server),
            client_factory=lambda route: server.client(),
            list_models=any_window,
        ),
    )
    state.globals = GlobalArgs(oneshot="what do you know?")
    assert oneshot.run_from_cli(state) == 0
    return server.model_requests[0].body["messages"][0]["content"]


def test_m2_learning_slice(
    scope: Scope, fake_clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    review_session(scope, fake_clock)
    clear_scan_cache()

    prompt = new_cli_session(scope)
    assert "<saved-notes" in prompt and FACT in prompt
    assert "- release-notes: Draft release notes from merged changes." in prompt

    ctx = make_ctx(scope)
    ctx.extensions.skill_commands = skill_commands(scope, ctx.session_id)
    ran = dispatch("/release-notes for 2.1", ctx)
    assert ran.submit is not None and "Group them by user-facing effect." in ran.submit

    status = run_cli(capsys, scope, "curator", "status")
    assert status.code == 0 and "managed skills: 1 (active 1, stale 0)" in status.out
    assert "release-notes" in status.out

    fake_clock.advance(timedelta(days=31).total_seconds())
    clear_scan_cache()
    assert run_cli(capsys, scope, "curator", "run").code == 0
    assert [a.name for a in list_archived(scope)] == ["release-notes"]
    assert not (scope.home / "skills" / "release-notes").exists()

    restored = run_cli(capsys, scope, "curator", "restore", "release-notes")
    assert restored.code == 0
    assert (scope.home / "skills" / "release-notes" / "SKILL.md").read_text("utf-8") == SKILL
