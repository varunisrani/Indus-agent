"""P09-10: skill bundle files, loading and precedence over same-slug skills."""

from __future__ import annotations

from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.learning.skills.bundles import (
    BUNDLE_INSTRUCTION,
    BundleError,
    bundle_expanders,
    bundles_dir,
    delete_bundle,
    expand_bundle,
    load_bundle,
    parse_bundle,
    reload_bundles,
    save_bundle,
    scan_bundles,
    slugify,
)
from indus.learning.skills.invoke import MISSING_NOTE, CommandTable, skill_command_names
from indus.learning.skills.resolver import load_catalog


def write_skill(home: Path, name: str, body: str) -> None:
    folder = home / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: about {name}\n---\n\n{body}\n", encoding="utf-8"
    )


def write_bundle(home: Path, stem: str, text: str) -> Path:
    folder = bundles_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.yaml"
    path.write_text(text, encoding="utf-8")
    reload_bundles()
    return path


def test_slug_defaults_to_the_file_stem(tmp_path: Path) -> None:
    path = write_bundle(tmp_path, "Release Day", "skills: [a, b, a]\n")
    bundle = load_bundle(path)
    assert bundle.slug == "release-day" and bundle.skills == ("a", "b")
    named = parse_bundle({"name": "Ship It!", "skills": ["a"]}, stem="ignored")
    assert named.slug == "ship-it"
    assert slugify("  --Odd__Name.. ") == "odd__name"


@pytest.mark.parametrize(
    "data",
    [
        {"skills": []},
        {"skills": "a"},
        {"skills": ["a", ""]},
        {"skills": [3]},
        {"name": "!!!", "skills": ["a"]},
        {"skills": ["a"], "instruction": ["not text"]},
        ["skills", "a"],
    ],
)
def test_invalid_bundles_rejected(data: object) -> None:
    with pytest.raises(BundleError):
        parse_bundle(data, stem="x")


def test_scan_skips_bad_files_and_duplicate_slugs(tmp_path: Path) -> None:
    write_bundle(tmp_path, "good", "skills: [a]\n")
    write_bundle(tmp_path, "empty", "skills: []\n")
    write_bundle(tmp_path, "zz-dup", "name: good\nskills: [b]\n")
    (bundles_dir(tmp_path) / "notes.txt").write_text("ignored", encoding="utf-8")
    found = scan_bundles(tmp_path)
    assert set(found) == {"good"} and found["good"].skills == ("a",)


def test_save_refuses_overwrite_and_delete_removes(tmp_path: Path) -> None:
    path = save_bundle(tmp_path, "ops", ["a"], description="Ops work")
    assert path == bundles_dir(tmp_path) / "ops.yaml"
    with pytest.raises(BundleError, match="--force"):
        save_bundle(tmp_path, "ops", ["b"])
    save_bundle(tmp_path, "ops", ["b"], overwrite=True)
    assert scan_bundles(tmp_path)["ops"].skills == ("b",)
    with pytest.raises(BundleError):
        save_bundle(tmp_path, "empty", [])
    assert delete_bundle(tmp_path, "OPS") == path and not path.exists()
    with pytest.raises(BundleError):
        delete_bundle(tmp_path, "ops")


def test_missing_skill_is_skipped_and_named(scope: Scope) -> None:
    write_skill(scope.home, "brew-tea", "Boil water first.")
    write_skill(scope.home, "fold-map", "Fold along the creases.")
    bundle = parse_bundle(
        {"skills": ["brew-tea", "gone-skill", "fold-map"], "instruction": "Morning first."},
        stem="morning",
    )
    expansion = expand_bundle(bundle, "then lunch", scope=scope, session_id="s1")
    assert expansion is not None
    assert expansion.loaded == ("brew-tea", "fold-map") and expansion.missing == ("gone-skill",)
    text = expansion.text
    assert MISSING_NOTE.format(names="gone-skill") in text
    assert "Boil water first." in text and "Fold along the creases." in text
    # the bundle's instruction comes first, then the user's text, before the skill bodies
    said = BUNDLE_INSTRUCTION.format(text="Morning first.")
    assert text.index(said) < text.index("then lunch") < text.index("Boil water first.")


def test_bundle_of_only_missing_skills_expands_to_nothing(scope: Scope) -> None:
    bundle = parse_bundle({"skills": ["gone"]}, stem="b")
    assert expand_bundle(bundle, "", scope=scope) is None


def test_bundle_beats_same_slug_skill(scope: Scope) -> None:
    write_skill(scope.home, "deploy", "The deploy skill body.")
    write_skill(scope.home, "checks", "Run the checks.")
    save_bundle(scope.home, "deploy", ["checks"])
    table = CommandTable(
        skills=skill_command_names(load_catalog(scope)),
        bundles=bundle_expanders(scope, "s1"),
    )
    assert table.names()["deploy"] == "skill bundle"
    expansion = table.run("/deploy now", scope=scope)
    assert expansion is not None
    assert "Run the checks." in expansion.text and "The deploy skill body." not in expansion.text
    # a stack never takes the bundle's slug as a skill
    stacked = table.run("/checks /deploy go", scope=scope)
    assert stacked is not None and stacked.loaded == ("checks",)
