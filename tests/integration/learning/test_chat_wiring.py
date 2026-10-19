"""P09 wave 1 host wiring: a CLI chat turn carries the learning tools and the frozen memory block.

Runs ``indus -z`` in process (``oneshot.run_from_cli``) against the fake model, so the path is the
real one: ``cli.runtime.build_service`` → ``agent_factory`` → ``learning.host.wire_session``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.cli import oneshot
from indus.cli.args import CliState, GlobalArgs, HostOverrides
from indus.foundation.scope import Scope
from indus.learning.memory.store import DELIMITER
from tests.fakes.agent import any_window
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch

LEARNING_TOOLS = {"memory", "session_search", "skills_list", "skill_view"}
NOTE = "The user drinks green tea while reviewing code."
SKILL = """---
name: tea-break
description: Plan a short break between long review sessions
license: MIT
---

# Tea break

## When to use
Long review sessions.

## Procedure
Stand up, make tea, come back.
"""


@pytest.fixture(autouse=True)
def in_tmp(tmp_path: Path) -> Iterator[None]:
    here = os.getcwd()
    os.chdir(tmp_path)
    yield
    os.chdir(here)


@pytest.fixture
def home(scope: Scope) -> Scope:
    memories = scope.home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text(NOTE + "\n", encoding="utf-8")
    skill = scope.home / "skills" / "leisure" / "tea-break"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL, encoding="utf-8")
    return scope


def run_z(scope: Scope, server: FakeModelServer, prompt: str = "hi", **flags: Any) -> int:
    state = CliState(
        scope=scope,
        overrides=HostOverrides(
            route=fake_route(server),
            client_factory=lambda route: server.client(),
            list_models=any_window,
        ),
    )
    state.globals = GlobalArgs(oneshot=prompt, **flags)
    return oneshot.run_from_cli(state)


def tool_names(server: FakeModelServer, index: int = 0) -> set[str]:
    tools = server.model_requests[index].body.get("tools", [])
    return {t["function"]["name"] for t in tools}


def system_prompt(server: FakeModelServer, index: int = 0) -> str:
    messages = server.model_requests[index].body["messages"]
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


def test_chat_turn_carries_learning_tools_and_frozen_memory(home: Scope) -> None:
    server = FakeModelServer(script_of(text_turn("ok")))
    assert run_z(home, server) == 0
    assert tool_names(server) >= LEARNING_TOOLS
    prompt = system_prompt(server)
    assert "<saved-notes" in prompt and NOTE in prompt
    assert "<skills_index>" in prompt and "- tea-break: " in prompt
    # P09-04 composition: the memory block sits before the skills index in the volatile tier.
    assert prompt.index("<saved-notes") < prompt.index("<skills_index>")
    assert "# Memory" in prompt  # the memory tool's guidance rides with the tool


def test_memory_write_runs_inline_and_leaves_the_prompt_frozen(home: Scope) -> None:
    added = "Prefers short answers."
    server = FakeModelServer(
        script_of(
            tool_batch(("memory", {"action": "add", "target": "memory", "content": added})),
            text_turn("saved"),
        )
    )
    assert run_z(home, server) == 0
    stored = (home.home / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    assert stored.rstrip("\n").split(DELIMITER) == [NOTE, added]
    # R07.1: the write is on disk and in the tool result, never in this session's prompt.
    assert system_prompt(server, 0) == system_prompt(server, 1)
    assert added not in system_prompt(server, 1)
    tool_rows = [m for m in server.model_requests[1].body["messages"] if m["role"] == "tool"]
    assert tool_rows and '"success": true' in tool_rows[0]["content"]


def test_unselected_learning_tools_get_no_executor_and_no_block(home: Scope) -> None:
    server = FakeModelServer(
        script_of(
            tool_batch(("memory", {"action": "add", "target": "memory", "content": "x"})),
            text_turn("done"),
        )
    )
    assert run_z(home, server, toolsets="web") == 0
    assert tool_names(server).isdisjoint(LEARNING_TOOLS)
    prompt = system_prompt(server)
    assert "<saved-notes" not in prompt and "<skills_index>" not in prompt
    # the model calls memory anyway: no executor, nothing written
    stored = (home.home / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    assert stored.strip() == NOTE


def test_skill_preload_flag_loads_the_skill_into_the_prompt(home: Scope) -> None:
    server = FakeModelServer(script_of(text_turn("ok")))
    assert run_z(home, server, skills=("tea-break",)) == 0
    prompt = system_prompt(server)
    assert "## Loaded skills" in prompt and "Stand up, make tea, come back." in prompt
    assert prompt.index("<skills_index>") < prompt.index("## Loaded skills")
