"""Curator jurisdiction, the archive and restore (P09-18, P09-20; R07.6)."""

from __future__ import annotations

import pytest

from indus.foundation.scope import Scope
from indus.learning import curator
from indus.learning.curator.archive import (
    Refused,
    archive_skill,
    list_archived,
    managed_skills,
    ownership,
    restore_skill,
    unmanaged_skills,
)
from indus.learning.skills.usage import SkillUsage
from tests.fakes.clock import FakeClock
from tests.fakes.skill_refs import FakeReferenceSource
from tests.unit.learning.curator._helpers_f import (
    add_skill,
    clean_registries,  # noqa: F401  (autouse fixture)
    ledger_rows,
    mark_bundled,
    mark_hub,
    record,
    skills_root,
    with_settings,
)
from tests.unit.learning.skills.helpers import make_skill


def test_ownership_by_marker_only(scope: Scope) -> None:
    add_skill(scope, "made-by-review", origin="agent")
    add_skill(scope, "taught", origin="learn")
    add_skill(scope, "old-one", origin=None)
    add_skill(scope, "shipped", origin="agent")
    mark_bundled(scope, "shipped")
    add_skill(scope, "from-hub", origin="agent")
    mark_hub(scope, "from-hub")
    assert [o.name for o in managed_skills(scope)] == ["made-by-review"]
    reasons = {o.name: o.reason for o in unmanaged_skills(scope)}
    assert reasons == {"old-one": "created before tracking", "taught": "created in the foreground"}
    assert ownership(scope, "shipped").owner == "bundled"
    assert ownership(scope, "from-hub").owner == "hub"
    assert ownership(scope, "nothing").owner == "missing"


def test_external_skill_is_not_managed(scope: Scope) -> None:
    outside = scope.home.parent / "shared-skills"
    make_skill(outside, "team-skill")
    shared = with_settings(scope, skills={"external_dirs": [str(outside)]})
    SkillUsage(skills_root(scope), clock=scope.clock).set_origin("team-skill", "agent")
    assert ownership(shared, "team-skill").owner == "external"
    assert managed_skills(shared) == []
    with pytest.raises(Refused, match="external skills folder"):
        archive_skill(shared, "team-skill")


def test_archive_and_restore_round_trip(scope: Scope) -> None:
    folder = add_skill(scope, "tool-tips", rel="dev/tool-tips")
    (folder / "references").mkdir()
    (folder / "references" / "notes.md").write_text("more\n", encoding="utf-8")
    dest = archive_skill(scope, "tool-tips")
    assert dest == skills_root(scope) / ".archive" / "tool-tips"
    assert (dest / "references" / "notes.md").read_text() == "more\n"
    assert [a.name for a in list_archived(scope)] == ["tool-tips"]
    back = restore_skill(scope, "tool-tips")
    assert back == skills_root(scope) / "tool-tips"  # flat: the nesting is not rebuilt
    assert (back / "references" / "notes.md").is_file()
    assert record(scope, "tool-tips")["state"] == "active"
    assert list_archived(scope) == []


def test_archive_collision_gets_suffix(scope: Scope, fake_clock: FakeClock) -> None:
    add_skill(scope, "dup")
    first = archive_skill(scope, "dup")
    add_skill(scope, "dup")
    second = archive_skill(scope, "dup")
    assert first.name == "dup"
    assert second.name == f"dup-{fake_clock.now().strftime('%Y%m%d%H%M%S')}"
    assert {a.name for a in list_archived(scope)} == {"dup"}
    restored = restore_skill(scope, "dup")
    assert restored.is_dir() and not first.exists()  # the exact-name copy wins


def test_archive_refusals(scope: Scope) -> None:
    add_skill(scope, "shipped")
    mark_bundled(scope, "shipped")
    add_skill(scope, "from-hub")
    mark_hub(scope, "from-hub")
    with pytest.raises(Refused, match="ships with Indus Agent"):
        archive_skill(scope, "shipped")
    with pytest.raises(Refused, match="skills hub"):
        archive_skill(scope, "from-hub")
    with pytest.raises(Refused, match="no skill named"):
        archive_skill(scope, "ghost")
    assert archive_skill(scope, "shipped", allow_bundled=True).is_dir()


def test_restore_refusals(scope: Scope) -> None:
    add_skill(scope, "taken")
    archive_skill(scope, "taken")
    add_skill(scope, "taken")
    with pytest.raises(Refused, match="already exists"):
        restore_skill(scope, "taken")
    add_skill(scope, "now-bundled")
    archive_skill(scope, "now-bundled")
    mark_bundled(scope, "now-bundled")
    with pytest.raises(Refused, match="bundled skill now owns"):
        restore_skill(scope, "now-bundled")
    add_skill(scope, "now-hub")
    archive_skill(scope, "now-hub")
    mark_hub(scope, "now-hub")
    with pytest.raises(Refused, match="skills-hub skill now owns"):
        restore_skill(scope, "now-hub")
    with pytest.raises(Refused, match="no archived skill"):
        restore_skill(scope, "never-was")


def test_restore_is_ledgered_as_user(scope: Scope) -> None:
    add_skill(scope, "x")
    archive_skill(scope, "x", actor="user")
    restore_skill(scope, "x")
    assert [(r["action"], r["actor"]) for r in ledger_rows(scope)] == [
        ("archive", "user"),
        ("restore", "user"),
    ]


def test_registry_add_remove() -> None:
    source = FakeReferenceSource(["a"])
    remove = curator.register_reference_source(source)
    assert curator.referenced_skills() == {"a"}
    remove()
    assert curator.referenced_skills() == set()
    assert curator.rewrite_references({}, []) == []
