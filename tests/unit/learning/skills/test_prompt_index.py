"""Skills index in the prompt and the mid-session delta note (P09-07, R07.29)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from indus.core.notes import NoteChannel
from indus.core.prompt.builder import BuildContext, PromptBuilder
from indus.core.prompt.lifecycle import PromptLifecycle
from indus.foundation.scope import Scope
from indus.learning.skills.prompt_index import (
    BLOCK_END,
    BLOCK_START,
    GUIDANCE,
    HEADING,
    IndexDeltaTracker,
    IndexOptions,
    SkillsIndexBuilder,
    build_index,
    catalog_index_source,
    clip_description,
    index_rows,
    render_delta_note,
    stage_index_delta,
)
from indus.learning.skills.resolver import clear_scan_cache, load_catalog
from tests.unit.learning.skills.helpers import FIXTURES, make_skill, with_skills_config

TOOLS = frozenset({"skill_view", "skills_list", "read_file"})


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_scan_cache()


def test_clip_edges() -> None:
    assert clip_description("x" * 60) == "x" * 60
    clipped = clip_description("x" * 61)
    assert clipped == "x" * 59 + "…" and len(clipped) == 60
    assert clip_description("  two\n lines   here ") == "two lines here"
    assert clip_description("word " * 20).endswith("…")


def test_golden_index(scope: Scope, tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / "tiers" / "local", scope.home / "skills")
    make_skill(
        scope.home / "skills",
        "research/deep-dig",
        description="Search several sources, read them closely, and cite every claim made.",
    )
    make_skill(scope.home / "skills", "ops/runbook", description="Follow the runbook.")
    catalog = load_catalog(scope)
    text = build_index(catalog, IndexOptions(tools=TOOLS, compact_categories=frozenset({"ops"})))
    expected = "\n".join(
        [
            HEADING,
            GUIDANCE,
            "",
            BLOCK_START,
            "### general",
            "- alpha: Local copy of alpha.",
            "- same: Identical copy test.",
            "### dev — Software development helpers",
            "- twin: [2 skills share this name; view one by path (dev/twin, ops/twin) and rename"
            " the other]",
            "[names only] ops: runbook, twin",
            "### research",
            "- deep-dig: Search several sources, read them closely, and cite every c…",
            BLOCK_END,
        ]
    )
    assert text == expected
    rows = index_rows("prefix\n" + text + "\nsuffix")
    assert set(rows) == {"alpha", "same", "twin", "runbook", "deep-dig"}
    assert rows["runbook"] == ""


def test_hidden_and_disabled_skills_are_not_listed(scope: Scope) -> None:
    root = scope.home / "skills"
    make_skill(root, "shown")
    make_skill(root, "needs-browser", extra="metadata:\n  indus:\n    requires_tools: [browser]\n")
    make_skill(root, "turned-off")
    catalog = load_catalog(with_skills_config(scope, disabled=["turned-off"]))
    assert set(index_rows(build_index(catalog, IndexOptions(tools=TOOLS)))) == {"shown"}


def test_empty_without_visible_skills(scope: Scope) -> None:
    assert build_index(load_catalog(scope), IndexOptions(tools=TOOLS)) == ""


def test_plugin_rows_parse_back() -> None:
    text = f"{BLOCK_START}\n- acme:deploy: Ship it.\n{BLOCK_END}"
    assert index_rows(text) == {"acme:deploy": "- acme:deploy: Ship it."}


def test_delta_note_rendering() -> None:
    note = render_delta_note({"new-one": "- new-one: Does new things."}, ["gone"])
    assert "Added" in note and "- new-one: Does new things." in note
    assert note.endswith("No longer available: gone]")
    assert "accurate again" in render_delta_note({}, [])


def _ctx(scope: Scope, tmp_path: Path) -> BuildContext:
    return BuildContext(
        scope=scope,
        model="fake-model",
        provider="fake",
        platform="cli",
        session_id="s-1",
        tool_names=TOOLS,
        cwd=tmp_path,
        context_files=False,
    )


def test_mid_session_add_and_remove(scope: Scope, tmp_path: Path) -> None:
    root = scope.home / "skills"
    make_skill(root, "first", description="The first skill.")
    builder = SkillsIndexBuilder(PromptBuilder(install=None), catalog_index_source(scope))
    lifecycle = PromptLifecycle(builder)
    ctx = _ctx(scope, tmp_path)
    tracker = IndexDeltaTracker()

    prompt = asyncio.run(lifecycle.restore_or_build(ctx))
    assert "- first: The first skill." in prompt
    assert stage_index_delta(lifecycle.notes, tracker, prompt, builder.current_index(ctx)) is None

    make_skill(root, "second", description="Added mid-session.")
    turn2 = asyncio.run(lifecycle.restore_or_build(ctx))
    assert turn2 == prompt  # prompt bytes unchanged
    note = stage_index_delta(lifecycle.notes, tracker, turn2, builder.current_index(ctx))
    assert note is not None and "- second: Added mid-session." in note
    assert lifecycle.notes.pop() == note  # exactly one note on the next user message
    assert stage_index_delta(lifecycle.notes, tracker, turn2, builder.current_index(ctx)) is None
    assert lifecycle.notes.pop() == ""  # the message after carries none

    shutil.rmtree(root / "first")
    note = stage_index_delta(lifecycle.notes, tracker, turn2, builder.current_index(ctx))
    assert note is not None and note.endswith("No longer available: first]")
    assert asyncio.run(lifecycle.restore_or_build(ctx)) == prompt


def test_compaction_rebuild_lists_new_skill(scope: Scope, tmp_path: Path) -> None:
    root = scope.home / "skills"
    make_skill(root, "first")
    builder = SkillsIndexBuilder(PromptBuilder(install=None), catalog_index_source(scope))
    lifecycle = PromptLifecycle(builder, notes=NoteChannel())
    ctx = _ctx(scope, tmp_path)
    tracker = IndexDeltaTracker()
    prompt = asyncio.run(lifecycle.restore_or_build(ctx))
    stage_index_delta(lifecycle.notes, tracker, prompt, builder.current_index(ctx))
    make_skill(root, "later", description="Arrived later.")
    assert stage_index_delta(lifecycle.notes, tracker, prompt, builder.current_index(ctx))
    lifecycle.notes.pop()
    lifecycle.invalidate("compaction")
    rebuilt = asyncio.run(lifecycle.restore_or_build(ctx))
    assert "- later: Arrived later." in rebuilt
    # the rebuilt prompt already carries the change: no stale note
    assert stage_index_delta(lifecycle.notes, tracker, rebuilt, builder.current_index(ctx)) is None


def test_no_index_without_view_tool(scope: Scope, tmp_path: Path) -> None:
    make_skill(scope.home / "skills", "first")
    source = catalog_index_source(scope)
    ctx = BuildContext(scope=scope, tool_names=frozenset({"read_file"}), cwd=tmp_path)
    assert source(ctx) == ""


def test_extra_blocks_follow_the_index(scope: Scope, tmp_path: Path) -> None:
    make_skill(scope.home / "skills", "first")
    builder = SkillsIndexBuilder(
        PromptBuilder(install=None),
        catalog_index_source(scope),
        extra_blocks=[lambda _ctx: "## Loaded skill\nbody"],
    )
    parts = asyncio.run(builder.build(_ctx(scope, tmp_path)))
    assert parts.text.index(BLOCK_END) < parts.text.index("## Loaded skill")


def test_tracker_ignores_missing_prompt() -> None:
    assert stage_index_delta(NoteChannel(), IndexDeltaTracker(), None, "") is None
