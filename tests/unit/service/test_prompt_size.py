"""The prompt-size breakdown (P18-16 follow-up): ``service.prompt_size.measure``."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.core.compression.anchor import estimate_text
from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope
from indus.learning.memory.prompt_block import render
from indus.learning.memory.store import MemoryStore
from indus.service import prompt_size
from indus.service.prompt_size import SessionFacts, measure, session_facts
from tests.unit.backends._aio import aio


@pytest.fixture
def home(scope: Scope) -> Iterator[Scope]:
    config_loader.reset()
    yield scope.with_config(config_loader.load(scope))
    config_loader.reset()


def _write_config(scope: Scope, text: str) -> Scope:
    (scope.home / "config.yaml").write_text(text, encoding="utf-8")
    config_loader.reset()
    return scope.with_config(config_loader.load(scope))


def _seed(scope: Scope, work: Path) -> None:
    (scope.home / "PERSONA.md").write_text("You are Pat, a careful helper.\n", encoding="utf-8")
    memories = scope.home / "memories"
    memories.mkdir(exist_ok=True)
    (memories / "MEMORY.md").write_text("the user likes green tea\n", encoding="utf-8")
    skill = scope.home / "skills" / "kitchen" / "brew"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: brew\ndescription: Make a good pot of tea.\n---\nSteps.\n", encoding="utf-8"
    )
    work.mkdir(exist_ok=True)
    (work / "AGENTS.md").write_text("Indent with four spaces.\n", encoding="utf-8")


@aio
async def test_an_empty_profile_has_the_built_in_identity_and_no_optional_parts(
    home: Scope, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    got = await measure(home, cwd=empty)
    assert got.platform == "cli" and got.session_id is None and got.stored_prompt is None
    assert got.sections["persona"].chars == 0
    assert got.sections["context_files"].chars == 0
    assert got.sections["memory"].chars == 0
    assert got.system_prompt.chars > 0 and got.tiers["stable"].chars > 0
    assert got.tool_count > 0 and got.sections["tools"].chars > 0


@aio
async def test_persona_context_memory_and_skills_have_their_sections(
    home: Scope, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    _seed(home, work)
    got = await measure(home, cwd=work)
    assert got.sections["persona"].chars == len("You are Pat, a careful helper.")
    assert got.sections["context_files"].chars > len("Indent with four spaces.")
    assert got.sections["memory"].chars > len("the user likes green tea")
    assert got.sections["skills"].chars > 0
    # The tiers are the builder's three parts: together they are the prompt (joined by blank
    # lines), and each part lands in its tier.
    tiers = got.tiers
    assert (
        got.system_prompt.chars
        == tiers["stable"].chars + tiers["context"].chars + (tiers["volatile"].chars) + 2 * 2
    )
    assert tiers["context"].chars >= got.sections["context_files"].chars
    assert tiers["volatile"].chars >= got.sections["memory"].chars + got.sections["skills"].chars
    assert tiers["stable"].chars >= got.sections["persona"].chars
    assert got.request.chars == got.system_prompt.chars + got.sections["tools"].chars


@aio
async def test_tokens_are_the_chars_over_four_estimate(home: Scope, tmp_path: Path) -> None:
    got = await measure(home, cwd=tmp_path)
    for size in (got.system_prompt, got.request, *got.tiers.values(), *got.sections.values()):
        assert size.tokens == (size.chars + 3) // 4
    assert estimate_text("abcde") == 2
    answer = got.to_json()
    assert answer["estimate"] == "chars/4"
    assert set(answer["sections"]) == set(prompt_size.SECTIONS)
    assert set(answer["tiers"]) == {"stable", "context", "volatile"}
    json.dumps(answer)  # plain JSON


@aio
async def test_tools_follow_the_selection(home: Scope, tmp_path: Path) -> None:
    everything = await measure(home, cwd=tmp_path)
    narrow = await measure(_write_config(home, "platform_toolsets:\n  cli: [file]\n"), cwd=tmp_path)
    assert 0 < narrow.tool_count < everything.tool_count
    assert narrow.sections["tools"].chars < everything.sections["tools"].chars
    # Without the skills tools there is no skills index either.
    assert narrow.sections["skills"].chars == 0


@aio
async def test_no_model_call_and_no_provider_needed(home: Scope, tmp_path: Path) -> None:
    got = await measure(home, cwd=tmp_path)
    assert (got.model, got.provider) == ("", "")
    assert got.system_prompt.chars > 0


@aio
async def test_a_session_s_stored_memory_block_wins_over_the_files(
    home: Scope, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    _seed(home, work)
    frozen = render(MemoryStore(home.home))
    stored = f"Identity text.\n\n{frozen.memory}\n\nMore text."
    long_note = "a much longer note about what the user prefers " * 20
    (home.home / "memories" / "MEMORY.md").write_text(long_note, encoding="utf-8")
    facts = session_facts(
        {
            "id": "s-1",
            "source": "telegram",
            "model": "",
            "cwd": str(work),
            "system_prompt": stored,
        }
    )
    got = await measure(home, session=facts)
    assert got.session_id == "s-1" and got.platform == "telegram"
    assert got.sections["memory"].chars == len(frozen.memory.strip())
    assert got.stored_prompt is not None and got.stored_prompt.chars == len(stored)
    assert got.sections["context_files"].chars > 0  # the session's stored folder
    fresh = await measure(home, cwd=work)
    assert fresh.sections["memory"].chars > len(long_note.strip())


def test_session_facts_take_only_text_fields() -> None:
    facts = session_facts({"id": "s", "source": None, "model": 3, "system_prompt": ""})
    assert facts == SessionFacts(session_id="s")


@aio
async def test_a_missing_session_folder_falls_back_to_the_profile_home(
    home: Scope, tmp_path: Path
) -> None:
    gone = SessionFacts(session_id="s-2", cwd=str(tmp_path / "gone"))
    got = await measure(home, session=gone)
    assert got.sections["context_files"].chars == 0


@aio
async def test_hosts_that_are_not_started_are_named(home: Scope, tmp_path: Path) -> None:
    configured = _write_config(
        home,
        "mcp_servers:\n  files:\n    command: echo\nmemory:\n  provider: other\n",
    )
    got = await measure(configured, cwd=tmp_path)
    assert any("MCP" in w for w in got.warnings)
    assert any("memory provider" in w for w in got.warnings)
