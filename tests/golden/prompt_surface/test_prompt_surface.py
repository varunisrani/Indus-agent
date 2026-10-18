"""Golden prompt surfaces (P04-22): the stock prompt and tool array of each surface, byte for byte.

A difference fails with a unified diff. ``--update-golden`` (see ``tests/golden/conftest.py``)
rewrites the fixtures instead; a regenerated fixture lands with a note saying why it changed.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from indus.core.prompt.builder import BuildContext, PromptParts
from indus.core.prompt.lifecycle import PromptLifecycle, SessionPrompts
from indus.foundation.scope import Scope
from indus.state import registry, sessions
from tests.golden.prompt_surface import generate as gen

REPO_ROOT = Path(__file__).resolve().parents[3]
SURFACES = pytest.mark.parametrize("surface", gen.SURFACES, ids=gen.SURFACE_NAMES)
HISTORY: list[dict[str, Any]] = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hello back"},
]


def checked_in(surface: gen.Surface) -> dict[str, str]:
    return {
        name: gen.read_fixture(gen.fixture_path(surface, name))
        for name in (gen.PROMPT_FILE, gen.TOOLS_FILE)
    }


@SURFACES
def test_surface_matches_golden(surface: gen.Surface, update_golden: bool) -> None:
    rendered = gen.render(surface)
    if update_golden:
        for path in gen.write(rendered):
            print(f"rewrote {path}")
    problems = gen.check(rendered)
    assert not problems, "\n".join(problems)
    # The fixtures are what the model gets: the prompt plus one newline, a JSON array.
    files = checked_in(surface)
    assert files[gen.PROMPT_FILE].endswith("\n") and not files[gen.PROMPT_FILE].endswith("\n\n")
    assert isinstance(json.loads(files[gen.TOOLS_FILE]), list)
    assert "\r" not in files[gen.PROMPT_FILE] + files[gen.TOOLS_FILE]


def test_stale_fixture_reports_a_diff() -> None:
    rendered = gen.render(gen.surface_named("cli"))
    edited = rendered.prompt.replace("Platform: cli", "Platform: tui")
    report = gen.drift(edited, rendered.prompt, "cli/prompt.txt")
    assert "-Platform: tui\n" in report and "+Platform: cli\n" in report
    assert "cli/prompt.txt (checked in)" in report and gen.REGENERATE in report
    assert gen.drift(rendered.prompt, rendered.prompt, "cli/prompt.txt") == ""
    assert "(checked in)" in gen.drift("", rendered.tools, "cli/tools.json")


def test_rendering_twice_is_stable() -> None:
    for surface in gen.SURFACES:
        assert gen.render(surface) == gen.render(surface)


def test_another_process_renders_the_same_bytes() -> None:
    env = {**os.environ, "PYTHONHASHSEED": "4242"}
    done = subprocess.run(
        [sys.executable, "-m", "tests.golden.prompt_surface.generate"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


# -- save / resume with the real store (M1) -------------------------------------------------------


class RefusingBuilder:
    """A resumed session must reuse the stored bytes, never build."""

    async def build(self, ctx: BuildContext) -> PromptParts:
        raise AssertionError("the resumed session rebuilt its prompt")


@SURFACES
def test_save_and_resume_keep_the_bytes(surface: gen.Surface, scope: Scope) -> None:
    # The store lives in the test's isolated profile; the prompt uses the pinned golden scope.
    expected = checked_in(surface)
    session_id = f"golden-{surface.name}"
    pinned, route = gen.golden_scope(), gen.golden_route()
    tools = gen.tool_array(surface, pinned, route)
    ctx = gen.build_context(surface, pinned, route, tools, session_id=session_id)

    handle = registry.open_unshared(scope)
    try:
        assert sessions.create(handle, session_id, surface.platform)
        first = PromptLifecycle(gen.golden_builder(), store=SessionPrompts(handle))
        built = asyncio.run(first.restore_or_build(ctx))
    finally:
        handle.close()
    assert gen.prompt_text(built) == expected[gen.PROMPT_FILE]

    # A new process: a reopened store, a fresh registry, a lifecycle that may not build.
    handle = registry.open_unshared(scope)
    try:
        row = sessions.get(handle, session_id)
        assert row is not None and row["system_prompt"] == built
        resumed = PromptLifecycle(RefusingBuilder(), store=SessionPrompts(handle))
        restored = asyncio.run(resumed.restore_or_build(ctx, history=HISTORY))
    finally:
        handle.close()
    assert restored.encode("utf-8") == built.encode("utf-8")
    assert resumed.notes.pop() == ""  # same surface: nothing to announce
    again = gen.tool_array(surface, gen.golden_scope(), gen.golden_route())
    assert gen.tools_text(again) == expected[gen.TOOLS_FILE]
