"""``indus skills`` verbs (P09-24)."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.learning import pending
from indus.learning.skills import bundled_sync
from indus.learning.skills.resolver import clear_scan_cache
from indus.learning.skills.view_tools import clear_list_cache
from tests.unit.cli.commands._helpers_d import run
from tests.unit.learning.skills.helpers import make_skill, skill_md, with_skills_config

INVALID = Path(__file__).resolve().parents[3] / "fixtures" / "skills" / "invalid"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    clear_scan_cache()
    clear_list_cache()
    yield
    clear_scan_cache()


@pytest.fixture
def shipped(tmp_path: Path) -> Path:
    """A shipped skills tree with one skill, ``dev/shipped``."""
    source = tmp_path / "shipped-tree"
    make_skill(source, "dev/shipped", description="Ships with the product.")
    return source


@pytest.fixture
def home_scope(scope: Scope, shipped: Path) -> Scope:
    """The test profile, pointed at the fake shipped tree and seeded from it."""
    seeded = scope.with_env(scope.env.with_overlay({bundled_sync.SOURCE_ENV: str(shipped)}))
    report = bundled_sync.sync_bundled(seeded.home, source=shipped)
    assert report.added == ["shipped"]
    return seeded


def test_list_json_and_source(home_scope: Scope, tmp_path: Path) -> None:
    make_skill(home_scope.home / "skills", "notes", description="My own notes.")
    external = tmp_path / "ext"
    make_skill(external, "outside", description="From elsewhere.")
    scoped = with_skills_config(home_scope, external_dirs=[str(external)])
    result = run(scoped, "skills", "list", "--json")
    assert result.code == 0, result.err
    rows = {r["name"]: r for r in json.loads(result.out)}
    assert {n: r["source"] for n, r in rows.items()} == {
        "notes": "local",
        "shipped": "bundled",
        "outside": "external",
    }
    assert rows["notes"]["state"] == "active" and rows["notes"]["status"] == "ok"
    result = run(scoped, "skills", "list", "--source", "bundled")
    assert result.code == 0
    assert "shipped" in result.out and "notes" not in result.out
    assert result.out.strip().endswith("1 skill.")


def test_view(home_scope: Scope) -> None:
    folder = make_skill(home_scope.home / "skills", "notes", body="Do the thing.\n")
    (folder / "references").mkdir()
    (folder / "references" / "a.md").write_text("A\n", encoding="utf-8")
    result = run(home_scope, "skills", "view", "notes")
    assert result.code == 0
    assert "Do the thing." in result.out
    assert "references/a.md" in result.out
    missing = run(home_scope, "skills", "view", "nope")
    assert missing.code == 1 and "no skill named 'nope'" in missing.err


def test_check_passes_with_warnings(home_scope: Scope) -> None:
    body = "Rule.\n" + ("word " * 5000) + "\n"
    make_skill(home_scope.home / "skills", "long", body=body)
    result = run(home_scope, "skills", "check")
    assert result.code == 0, result.out
    assert "lint.oversized" in result.out
    assert "0 errors, 1 warnings" in result.out


def test_check_exits_1_on_an_invalid_skill(home_scope: Scope) -> None:
    shutil.copytree(
        INVALID / "missing-description", home_scope.home / "skills" / "missing-description"
    )
    result = run(home_scope, "skills", "check")
    assert result.code == 1
    assert "error    missing-description" in result.out
    assert "description" in result.out


def test_check_flags_ambiguous_names(home_scope: Scope) -> None:
    make_skill(home_scope.home / "skills", "a/twin", name="twin", body="One.\n")
    make_skill(home_scope.home / "skills", "b/twin", name="twin", body="Two.\n")
    result = run(home_scope, "skills", "check")
    assert result.code == 1
    assert "twin: the name is used by several skills" in result.out


def test_list_modified_and_diff(home_scope: Scope) -> None:
    result = run(home_scope, "skills", "list-modified")
    assert result.out.strip() == "No shipped skill has been edited."
    assert run(home_scope, "skills", "diff", "shipped").out.strip() == (
        "No changes from the shipped version."
    )
    copy = home_scope.home / "skills" / "dev" / "shipped" / "SKILL.md"
    copy.write_text(skill_md("shipped", "Ships with the product.", body="Mine now.\n"), "utf-8")
    assert run(home_scope, "skills", "list-modified").out.strip() == "shipped"
    diff = run(home_scope, "skills", "diff", "shipped")
    assert diff.code == 0
    assert "-Steps go here." in diff.out and "+Mine now." in diff.out
    assert run(home_scope, "skills", "diff", "notes").code == 1


def test_sync_adds_new_shipped_skills(home_scope: Scope, shipped: Path) -> None:
    make_skill(shipped, "ops/second")
    result = run(home_scope, "skills", "sync")
    assert result.code == 0
    assert "1 added" in result.out
    assert (home_scope.home / "skills" / "ops" / "second" / "SKILL.md").is_file()


def test_opt_out_remove_needs_confirmation(home_scope: Scope) -> None:
    result = run(home_scope, "skills", "opt-out", "--remove")
    assert result.code == 2
    assert "-y" in result.err
    assert (home_scope.home / "skills" / "dev" / "shipped").is_dir()
    assert not bundled_sync.is_opted_out(home_scope.home)
    result = run(home_scope, "skills", "opt-out", "--remove", "-y")
    assert result.code == 0, result.err
    assert "Deleted 1: shipped" in result.out
    assert bundled_sync.is_opted_out(home_scope.home)
    assert not (home_scope.home / "skills" / "dev" / "shipped").exists()


def test_opt_out_with_tty_confirmation(home_scope: Scope) -> None:
    result = run(home_scope, "skills", "opt-out", "--remove", tty=True, input="n\n")
    assert result.code == 1 and "Nothing changed." in result.out
    result = run(home_scope, "skills", "opt-out", "--remove", tty=True, input="y\n")
    assert result.code == 0
    assert not (home_scope.home / "skills" / "dev" / "shipped").exists()


def test_opt_in_syncs(home_scope: Scope) -> None:
    run(home_scope, "skills", "opt-out", "--remove", "-y")
    result = run(home_scope, "skills", "opt-in")
    assert result.code == 0
    assert not bundled_sync.is_opted_out(home_scope.home)
    assert (home_scope.home / "skills" / "dev" / "shipped" / "SKILL.md").is_file()


def test_reset_and_restore(home_scope: Scope) -> None:
    copy = home_scope.home / "skills" / "dev" / "shipped" / "SKILL.md"
    copy.write_text(skill_md("shipped", body="Edited.\n"), encoding="utf-8")
    refused = run(home_scope, "skills", "reset", "shipped", "--restore")
    assert refused.code == 2
    assert "Edited." in copy.read_text(encoding="utf-8")
    restored = run(home_scope, "skills", "reset", "shipped", "--restore", "-y")
    assert restored.code == 0, restored.err
    assert "Steps go here." in copy.read_text(encoding="utf-8")
    untracked = run(home_scope, "skills", "reset", "shipped")
    assert untracked.code == 0
    assert "shipped" not in bundled_sync.read_manifest(home_scope.home)
    assert run(home_scope, "skills", "reset", "nothing-like-it").code == 1


def test_pending_approve_reject(home_scope: Scope) -> None:
    make_skill(home_scope.home / "skills", "notes")
    store = pending.PendingStore(home_scope.home, home_scope.clock)
    patch = {"action": "patch", "name": "notes", "old_string": "Steps", "new_string": "Moves"}
    first = store.stage("skills", {"operations": [patch]}, summary="patch notes", origin="learn")
    second = store.stage(
        "skills",
        {"operations": [{"action": "delete", "name": "notes"}]},
        summary="delete notes",
        origin="learn",
    )
    listed = run(home_scope, "skills", "pending")
    assert first.id in listed.out and second.id in listed.out
    assert run(home_scope, "skills", "reject", second.id).code == 0
    approved = run(home_scope, "skills", "approve", "all")
    assert approved.code == 0, approved.err
    assert "+Moves go here." in approved.out
    assert "Moves" in (home_scope.home / "skills" / "notes" / "SKILL.md").read_text("utf-8")
    assert store.list("skills") == []
    assert run(home_scope, "skills", "reject", "all").code == 1
