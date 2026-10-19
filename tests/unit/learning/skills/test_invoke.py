"""Skills as slash commands, preprocessing and auto-load (P09-09)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from indus.core.prompt.builder import BuildContext, PromptBuilder
from indus.core.prompt.lifecycle import PromptLifecycle
from indus.foundation.scope import Scope
from indus.learning.skills.invoke import (
    INLINE_SHELL_TIMEOUT_S,
    CommandTable,
    Expansion,
    auto_load_block,
    expand_skills,
    preprocess,
    reset_warnings,
    skill_command_names,
    split_stack,
)
from indus.learning.skills.prompt_index import SkillsIndexBuilder, catalog_index_source
from indus.learning.skills.resolver import clear_scan_cache, load_catalog
from indus.learning.skills.usage import usage_for
from tests.unit.learning.skills.helpers import FakeConfig, make_skill

NAMES = {"a", "b", "c", "d", "e", "f"}


@pytest.fixture(autouse=True)
def _fresh() -> None:
    clear_scan_cache()
    reset_warnings()


class Runner:
    def __init__(self, output: str = "ran", fail: BaseException | None = None) -> None:
        self.calls: list[tuple[str, Path, float]] = []
        self.output = output
        self.fail = fail

    def __call__(self, command: str, cwd: Path, timeout: float) -> str:
        self.calls.append((command, cwd, timeout))
        if self.fail is not None:
            raise self.fail
        return self.output


def test_stack_of_five() -> None:
    names, rest = split_stack("/a /b /c /d /e /f do it", NAMES.__contains__) or ([], "")
    assert names == ["a", "b", "c", "d", "e"]
    assert rest == "/f do it"  # the sixth slash word stays text


def test_stack_stop_rules() -> None:
    assert split_stack("/notaskill/path a b", NAMES.__contains__) is None
    assert split_stack("/zzz /a", NAMES.__contains__) is None
    assert split_stack("/a /a again", NAMES.__contains__) == (["a"], "/a again")
    assert split_stack("/a /usr/bin/env x", NAMES.__contains__) == (["a"], "/usr/bin/env x")
    assert split_stack("/A\nmulti\nline", NAMES.__contains__) == (["a"], "multi\nline")
    assert split_stack("plain text", NAMES.__contains__) is None


def test_expansion_is_one_message_with_instruction(scope: Scope) -> None:
    root = scope.home / "skills"
    folder = make_skill(root, "alpha", body="Do alpha things.\n")
    (folder / "references").mkdir()
    (folder / "references" / "r.md").write_text("ref")
    make_skill(root, "beta", body="Do beta things.\n")
    out = expand_skills(["alpha", "beta"], "fix the build", scope=scope, session_id="s1")
    assert out is not None and out.loaded == ("alpha", "beta") and out.missing == ()
    text = out.text
    assert text.startswith("[The user ran these skills together: /alpha, /beta.")
    assert f'<skill name="alpha" path="{folder}">' in text
    assert "Do alpha things." in text and "Do beta things." in text
    assert "references/r.md" in text
    assert text.endswith("User instruction: fix the build")
    assert "name: alpha" not in text  # frontmatter is not repeated


def test_missing_skill_is_named(scope: Scope) -> None:
    make_skill(scope.home / "skills", "alpha")
    out = expand_skills(["alpha", "ghost"], "", scope=scope)
    assert out is not None and out.missing == ("ghost",)
    assert "Not loaded" in out.text and "ghost" in out.text
    assert "none given" in out.text
    assert expand_skills(["ghost"], "", scope=scope) is None


def test_template_tokens(scope: Scope) -> None:
    body = "Run ${INDUS_SKILL_DIR}/scripts/x.sh for ${INDUS_SESSION_ID}; keep ${OTHER}.\n"
    folder = make_skill(scope.home / "skills", "tok", body=body)
    out = expand_skills(["tok"], "", scope=scope, session_id="sess-9")
    assert out is not None
    assert f"Run {folder}/scripts/x.sh for sess-9; keep ${{OTHER}}." in out.text
    off = scope.with_config(FakeConfig({"skills": {"template_vars": False}}))
    out_off = expand_skills(["tok"], "", scope=off, session_id="sess-9")
    assert out_off is not None and "${INDUS_SKILL_DIR}" in out_off.text


def test_inline_shell_off_by_default(scope: Scope) -> None:
    make_skill(scope.home / "skills", "sh", body="Branch: !`git branch --show-current`\n")
    runner = Runner()
    out = expand_skills(["sh"], "", scope=scope, runner=runner)
    assert out is not None and runner.calls == []
    assert "!`git branch --show-current`" in out.text


def test_inline_shell_on_runs_once_with_limit(scope: Scope) -> None:
    folder = make_skill(scope.home / "skills", "sh", body="Branch: !`git branch --show-current`\n")
    on = scope.with_config(FakeConfig({"skills": {"inline_shell": True}}))
    runner = Runner(output="main")
    out = expand_skills(["sh"], "", scope=on, runner=runner)
    assert out is not None and "Branch: main" in out.text
    assert runner.calls == [("git branch --show-current", folder, INLINE_SHELL_TIMEOUT_S)]
    assert INLINE_SHELL_TIMEOUT_S == 10.0


def test_inline_shell_timeout_marker(scope: Scope) -> None:
    make_skill(scope.home / "skills", "sh", body="Slow: !`sleep 99`\n")
    on = scope.with_config(FakeConfig({"skills": {"inline_shell": True}}))
    out = expand_skills(["sh"], "", scope=on, runner=Runner(fail=TimeoutError("sleep 99")))
    assert out is not None and "[inline command timed out after 10 s: sleep 99]" in out.text


def test_inline_shell_never_for_community_skills(scope: Scope) -> None:
    root = scope.home / "skills"
    make_skill(root, "hubbed", body="X: !`id`\n")
    (root / ".hub").mkdir()
    (root / ".hub" / "lock.json").write_text(
        json.dumps({"installed": {"hubbed": {"trust": "community"}}})
    )
    on = scope.with_config(FakeConfig({"skills": {"inline_shell": True}}))
    runner = Runner()
    out = expand_skills(["hubbed"], "", scope=on, runner=runner)
    assert out is not None and runner.calls == [] and "!`id`" in out.text


def test_default_runner_runs_real_bash(scope: Scope) -> None:
    make_skill(scope.home / "skills", "sh", body="Echo: !`printf hello`\n")
    on = scope.with_config(FakeConfig({"skills": {"inline_shell": True}}))
    out = expand_skills(["sh"], "", scope=on)
    assert out is not None and "Echo: hello" in out.text


def test_preprocess_leaves_empty_snippet() -> None:
    assert (
        preprocess(
            "a !` ` b", skill_dir=Path("/x"), session_id=None, inline_shell=True, runner=Runner()
        )
        == "a  b"
    )


def test_skill_config_block(scope: Scope) -> None:
    extra = (
        "metadata:\n  indus:\n    config:\n"
        "      - key: demo.path\n        description: where\n        default: /tmp/d\n"
        "      - key: demo.mode\n        description: how\n"
    )
    make_skill(scope.home / "skills", "conf", extra=extra)
    out = expand_skills(["conf"], "", scope=scope)
    assert out is not None and "[Skill config for conf:\n- demo.path: /tmp/d\n]" in out.text
    configured = scope.with_config(
        FakeConfig({"skills": {"config": {"demo": {"path": "/srv", "mode": "fast"}}}})
    )
    out2 = expand_skills(["conf"], "", scope=configured)
    assert out2 is not None and "- demo.path: /srv\n- demo.mode: fast" in out2.text


def test_use_count_bumped_per_load(scope: Scope) -> None:
    make_skill(scope.home / "skills", "alpha")
    expand_skills(["alpha"], "", scope=scope)
    expand_skills(["alpha"], "", scope=scope)
    assert usage_for(scope).get("alpha")["use_count"] == 2


def test_system_prompt_unchanged_after_invocation(scope: Scope, tmp_path: Path) -> None:
    make_skill(scope.home / "skills", "alpha")
    builder = SkillsIndexBuilder(PromptBuilder(install=None), catalog_index_source(scope))
    lifecycle = PromptLifecycle(builder)
    ctx = BuildContext(
        scope=scope, model="m", provider="p", tool_names=frozenset({"skill_view"}), cwd=tmp_path
    )
    before = asyncio.run(lifecycle.restore_or_build(ctx))
    table = CommandTable(skills=skill_command_names(load_catalog(scope)))
    out = table.run("/alpha go", scope=scope)
    assert out is not None and out.loaded == ("alpha",)
    assert asyncio.run(lifecycle.restore_or_build(ctx)) == before


def test_command_table_bundle_wins(scope: Scope) -> None:
    make_skill(scope.home / "skills", "deploy")
    seen: list[str] = []

    def bundle(instruction: str) -> Expansion:
        seen.append(instruction)
        return Expansion(text="bundle text", loaded=("deploy",))

    table = CommandTable(skills={"deploy": "d"}, bundles={"deploy": bundle})
    out = table.run("/deploy now please", scope=scope)
    assert out is not None and out.text == "bundle text" and seen == ["now please"]
    assert table.names()["deploy"] == "skill bundle"
    assert CommandTable(skills={}).run("/nothing", scope=scope) is None


def test_ambiguous_skills_are_not_commands(scope: Scope) -> None:
    root = scope.home / "skills"
    make_skill(root, "x/twin", description="one")
    make_skill(root, "y/twin", description="two")
    make_skill(root, "solo")
    assert skill_command_names(load_catalog(scope)) == {"solo": "The solo skill."}


def test_auto_load_block_and_missing_warns_once(
    scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    make_skill(scope.home / "skills", "always", body="Always do this.\n")
    configured = scope.with_config(FakeConfig({"skills": {"auto_load": ["always", "ghost"]}}))
    with caplog.at_level(logging.WARNING):
        block = auto_load_block(configured, session_id="s1")
        auto_load_block(configured, session_id="s1")
    assert block.startswith("## Loaded skills") and "Always do this." in block
    assert caplog.text.count("'ghost'") == 1
    assert usage_for(scope).get("always")["use_count"] == 2


def test_auto_load_suppressed_by_ignore_rules(scope: Scope) -> None:
    make_skill(scope.home / "skills", "always")
    configured = scope.with_config(FakeConfig({"skills": {"auto_load": ["always"]}}))
    ignored = configured.with_env(configured.env.with_overlay({"INDUS_IGNORE_RULES": "1"}))
    assert auto_load_block(ignored) == ""
    assert "always" in auto_load_block(ignored, extra=["always"])  # -s preload still loads
    assert auto_load_block(scope) == ""
