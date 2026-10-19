"""Skill discovery tiers and name resolution (P09-06, R07.27)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.learning.skills.invoke import expand_skills, skill_command_names
from indus.learning.skills.prompt_index import IndexOptions, build_index, index_rows
from indus.learning.skills.resolver import (
    TIER_CREATE_DIR,
    TIER_EXTERNAL,
    TIER_LOCAL,
    AmbiguousSkill,
    SkillBroken,
    SkillDisabled,
    SkillNotFound,
    SkillsSettings,
    clear_scan_cache,
    load_catalog,
)
from indus.learning.skills.view_tools import clear_list_cache, skill_view, skills_list
from tests.unit.learning.skills.helpers import FIXTURES, make_skill, with_skills_config


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_scan_cache()


@pytest.fixture
def tiered(scope: Scope, tmp_path: Path) -> Scope:
    shutil.copytree(FIXTURES / "tiers" / "local", scope.home / "skills")
    shutil.copytree(FIXTURES / "tiers" / "create", tmp_path / "create")
    shutil.copytree(FIXTURES / "tiers" / "external", tmp_path / "external")
    return with_skills_config(
        scope,
        create_dir=str(tmp_path / "create"),
        external_dirs=[str(tmp_path / "external"), str(tmp_path / "missing")],
    )


def test_settings_roots_in_order(tiered: Scope, tmp_path: Path) -> None:
    settings = SkillsSettings.from_scope(tiered)
    assert settings.roots() == [
        (TIER_LOCAL, tiered.home / "skills"),
        (TIER_CREATE_DIR, tmp_path / "create"),
        (TIER_EXTERNAL, tmp_path / "external"),
        (TIER_EXTERNAL, tmp_path / "missing"),
    ]
    assert settings.write_root == tmp_path / "create"


def test_missing_external_dir_is_ignored(tiered: Scope) -> None:
    catalog = load_catalog(tiered)
    assert {r.name for r in catalog.visible()} == {"alpha", "beta", "twin", "same", "gamma"}


def test_cross_tier_shadowing(tiered: Scope, tmp_path: Path) -> None:
    catalog = load_catalog(tiered)
    alpha = catalog.resolve("alpha")
    assert alpha.skill_dir == tiered.home / "skills" / "alpha"
    assert alpha.tier == TIER_LOCAL
    beta = catalog.resolve("beta")
    assert beta.skill_dir == tmp_path / "create" / "beta"
    shadowed = [r for r in catalog.records if r.status == "shadowed" and r.name == "alpha"]
    assert [r.skill_dir for r in shadowed] == [tmp_path / "external" / "alpha"]
    # a shadowed copy cannot be reached by its own relative path either
    assert catalog.resolve("beta").tier == TIER_CREATE_DIR
    with pytest.raises(SkillNotFound):
        catalog.resolve("tools/beta")


def test_shadow_is_logged_once(tiered: Scope, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="indus.learning.skills.resolver"):
        load_catalog(tiered)
        load_catalog(tiered)
    hits = [r for r in caplog.records if "hidden by a copy" in r.getMessage()]
    assert len(hits) == 2  # alpha and beta, once each


def test_same_tier_duplicate_is_ambiguous(tiered: Scope) -> None:
    catalog = load_catalog(tiered)
    with pytest.raises(AmbiguousSkill) as info:
        catalog.resolve("twin")
    assert "ambiguous skill name 'twin'" in str(info.value)
    assert set(info.value.choices) == {"dev/twin", "ops/twin"}
    assert catalog.resolve("dev/twin").description == "Twin used for development."
    assert catalog.resolve("ops/twin").description == "Twin used for operations."
    listed = [r.load_name for r in catalog.visible() if r.name == "twin"]
    assert sorted(listed) == ["dev/twin", "ops/twin"]  # type: ignore[type-var]
    assert set(catalog.ambiguous_names()) == {"twin"}


def test_identical_copies_resolve_to_shallowest(tiered: Scope) -> None:
    catalog = load_catalog(tiered)
    same = catalog.resolve("same")
    assert same.relative_path == "same"
    assert [r.name for r in catalog.visible()].count("same") == 1


def test_symlink_view_is_one_skill(scope: Scope, tmp_path: Path) -> None:
    real = make_skill(tmp_path / "elsewhere", "linked", description="Real one.")
    local = scope.home / "skills"
    (local / "a").mkdir(parents=True)
    os.symlink(real, local / "a" / "linked")
    os.symlink(real, local / "linked")
    catalog = load_catalog(scope)
    assert catalog.resolve("linked").relative_path == "linked"
    assert [r.name for r in catalog.visible()] == ["linked"]


def test_disabled_hidden_and_refused(tiered: Scope, tmp_path: Path) -> None:
    scope = with_skills_config(
        tiered,
        create_dir=str(tmp_path / "create"),
        external_dirs=[str(tmp_path / "external")],
        disabled=["gamma"],
        platform_disabled={"telegram": ["alpha"]},
    )
    catalog = load_catalog(scope)
    assert "gamma" not in {r.name for r in catalog.visible()}
    with pytest.raises(SkillDisabled):
        catalog.resolve("gamma")
    assert catalog.resolve("alpha")  # only disabled on telegram
    on_telegram = load_catalog(scope, platform="telegram")
    with pytest.raises(SkillDisabled):
        on_telegram.resolve("alpha")


def test_disabled_by_relative_path_of_a_duplicate(tiered: Scope, tmp_path: Path) -> None:
    scope = with_skills_config(tiered, disabled=["ops/twin"])
    catalog = load_catalog(scope)
    assert [r.load_name for r in catalog.visible() if r.name == "twin"] == ["dev/twin"]


def test_env_and_home_expansion(scope: Scope, tmp_path: Path) -> None:
    make_skill(tmp_path / "from-var", "via-var")
    fake_home = tmp_path / "fakehome"
    make_skill(fake_home / "my-skills", "via-tilde")
    env = scope.env.with_overlay(
        {"SKILLS_EXTRA": str(tmp_path / "from-var"), "HOME": str(fake_home)}
    )
    scoped = with_skills_config(
        scope.with_env(env), external_dirs=["${SKILLS_EXTRA}", "~/my-skills"]
    )
    names = {r.name for r in load_catalog(scoped).visible()}
    assert names == {"via-var", "via-tilde"}


def test_broken_skill_is_recorded_not_listed(scope: Scope) -> None:
    folder = scope.home / "skills" / "broken"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: [oops\n---\nbody\n")
    catalog = load_catalog(scope)
    assert catalog.visible() == []
    assert [r.status for r in catalog.records] == ["broken"]
    with pytest.raises(SkillBroken, match="cannot be loaded"):
        catalog.resolve("broken")


def test_support_dirs_and_hidden_dirs_are_not_skills(scope: Scope) -> None:
    root = scope.home / "skills"
    make_skill(root, "outer")
    make_skill(root / "outer" / "references", "inner")  # inside a skill: not a skill
    make_skill(root / ".archive", "old")
    make_skill(root / "node_modules", "dep")
    assert [r.name for r in load_catalog(scope).visible()] == ["outer"]


def test_scan_cache_rereads_only_changed_files(scope: Scope) -> None:
    root = scope.home / "skills"
    make_skill(root, "one", description="First text.")
    assert load_catalog(scope).resolve("one").description == "First text."
    skill = root / "one" / "SKILL.md"
    skill.write_text(skill.read_text().replace("First text.", "Second, longer text."))
    assert load_catalog(scope).resolve("one").description == "Second, longer text."


def test_declared_name_differs_from_folder(scope: Scope) -> None:
    make_skill(scope.home / "skills", "folder-name", name="declared")
    catalog = load_catalog(scope)
    assert catalog.resolve("declared").relative_path == "folder-name"
    assert catalog.resolve("folder-name").name == "declared"


def test_plugin_skills_never_collide(scope: Scope, tmp_path: Path) -> None:
    make_skill(scope.home / "skills", "deploy")
    make_skill(tmp_path / "plug", "deploy", description="Plugin deploy.")
    settings = SkillsSettings(
        local_root=scope.home / "skills", plugin_roots={"acme": tmp_path / "plug"}
    )
    catalog = load_catalog(settings=settings)
    assert catalog.resolve("deploy").plugin is None
    assert catalog.resolve("acme:deploy").description == "Plugin deploy."


def test_category_from_path_and_labels(tiered: Scope) -> None:
    catalog = load_catalog(tiered)
    assert catalog.resolve("dev/twin").category == "dev"
    assert catalog.resolve("alpha").category is None
    assert catalog.category_labels["dev"] == "Software development helpers"


def test_absolute_path_lookup(tiered: Scope) -> None:
    catalog = load_catalog(tiered)
    target = tiered.home / "skills" / "ops" / "twin"
    assert catalog.resolve(str(target)).load_name == "ops/twin"


def test_symlink_loop_is_not_followed(scope: Scope) -> None:
    root = scope.home / "skills"
    make_skill(root, "cat/one")
    os.symlink(root / "cat", root / "cat" / "loop")
    names = [r.name for r in load_catalog(scope).visible()]
    assert names == ["one"]


def test_cross_tier_shadowing_is_the_same_for_every_consumer(tiered: Scope) -> None:
    clear_list_cache()
    local_alpha = tiered.home / "skills" / "alpha"
    catalog = load_catalog(tiered)
    index = index_rows(build_index(catalog, IndexOptions(tools=frozenset({"skill_view"}))))
    assert index["alpha"] == "- alpha: Local copy of alpha."
    rows = json.loads(skills_list({}, scope=tiered))["skills"]
    assert [r["description"] for r in rows if r["name"] == "alpha"] == ["Local copy of alpha."]
    viewed = json.loads(asyncio.run(skill_view({"name": "alpha"}, scope=tiered)))
    assert viewed["path"] == str(local_alpha)
    assert skill_command_names(catalog)["alpha"] == "Local copy of alpha."
    expansion = expand_skills(["alpha"], "", scope=tiered)
    assert expansion is not None and f'path="{local_alpha}"' in expansion.text
