"""P09 wave 1 host wiring (``learning.host``): registry, inline executors, builder stack, notes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from indus.core.agent import Agent
from indus.core.prompt import texts
from indus.core.prompt.builder import BuildContext, PromptBuilder
from indus.foundation.scope import Scope
from indus.learning import host
from indus.learning.memory.prompt_block import MemoryPromptBuilder
from indus.learning.skills.prompt_index import NOTE_ADDED, NOTE_HEAD, SkillsIndexBuilder
from indus.tools import toolsets
from tests.fakes.agent import fake_options, run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn

LEARNING = {"memory", "session_search", "skills_list", "skill_view"}


def write_skill(home: Path, name: str, description: str, category: str = "misc") -> None:
    folder = home / "skills" / category / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nDo it.\n",
        encoding="utf-8",
    )


def no_files(path: Path) -> str:
    raise FileNotFoundError(path)


def test_registry_holds_builtins_and_learning_tools(scope: Scope) -> None:
    registry = host.tool_registry(scope.clock)
    assert set(registry.names()) >= LEARNING
    assert "read_file" in registry.names()
    chosen = toolsets.select(["indus-cli"], registry=registry, scope=scope, platform="cli")
    assert chosen.tools >= LEARNING


def test_inline_executors_only_for_selected_tools(scope: Scope) -> None:
    both = host.wire_session(scope, {"memory", "session_search", "skill_view"}, session_id="s")
    assert set(both.inline) == {"memory", "session_search"}
    assert isinstance(both.builder, MemoryPromptBuilder)
    only_search = host.wire_session(scope, {"session_search"}, session_id="s")
    assert set(only_search.inline) == {"session_search"}
    assert only_search.memory is None
    # no memory tool → no memory block and no guidance: the builder is the skills index alone
    assert isinstance(only_search.builder, SkillsIndexBuilder)
    assert host.wire_session(scope, set(), session_id="s").inline == {}


def test_builder_stack_order(scope: Scope, tmp_path: Path) -> None:
    """Memory wrapper outside the skills index wrapper; in the text, memory comes first."""
    (scope.home / "memories").mkdir(parents=True)
    (scope.home / "memories" / "MEMORY.md").write_text("Likes tea.\n", encoding="utf-8")
    (scope.home / "memories" / "USER.md").write_text("Name: Asha.\n", encoding="utf-8")
    write_skill(scope.home, texts.HELP_SKILL_NAME, "How to use and configure Indus")
    tools = {"memory", "skill_view", "skills_list"}
    learning = host.wire_session(
        scope, tools, session_id="s", inner=PromptBuilder(reader=no_files, install=None)
    )
    assert isinstance(learning.builder, MemoryPromptBuilder)
    assert learning.builder.inner is learning.index
    ctx = BuildContext(
        scope=scope,
        model="m",
        provider="fake",
        platform="cli",
        session_id="s",
        tool_names=frozenset(tools),
        cwd=tmp_path,
    )
    text = asyncio.run(learning.builder.build(ctx)).text
    notes, profile, index = (text.index(m) for m in ("Likes tea.", "Name: Asha.", "<skills_index>"))
    assert notes < profile < index
    # the help pointer names the bundled guide skill once it is in the index
    assert texts.HELP_POINTER_WITH_SKILL in text


def test_skills_delta_note_rides_the_next_user_message_once(
    scope: Scope, fake_clock: FakeClock
) -> None:
    write_skill(scope.home, "first-skill", "The skill that was there at the start")
    server = FakeModelServer(
        script_of(text_turn("one"), text_turn("two"), text_turn("three")), clock=fake_clock
    )
    selection = ["skills", "memory"]
    registry = host.tool_registry(fake_clock)
    selected = toolsets.select(selection, registry=registry, scope=scope, platform="cli").tools
    learning = host.wire_session(
        scope,
        selected,
        session_id="s1",
        registry=registry,
        inner=PromptBuilder(reader=no_files, install=None),
    )
    options = fake_options(
        server,
        registry=registry,
        builder=learning.builder,
        inline=learning.inline,
        note_sources=learning.note_sources,
    )

    async def main() -> None:
        agent = await Agent.start(scope, fake_route(server), selection, options)
        await agent.run_turn("first")
        write_skill(scope.home, "late-skill", "Added while the session ran")
        await agent.run_turn("second")
        await agent.run_turn("third")
        await agent.close()

    asyncio.run(run_driven(main(), fake_clock))
    requests = [r.body["messages"] for r in server.model_requests]
    prompts = [m[0]["content"] for m in requests]
    assert prompts[0] == prompts[1] == prompts[2]  # R07.29: the index stays frozen
    assert "- late-skill:" not in prompts[0]

    def last_user(messages: list[dict[str, str]]) -> str:
        return next(m["content"] for m in reversed(messages) if m["role"] == "user")

    assert NOTE_HEAD not in last_user(requests[0])
    second = last_user(requests[1])
    assert NOTE_HEAD in second and NOTE_ADDED in second and "- late-skill:" in second
    assert NOTE_HEAD not in last_user(requests[2])
