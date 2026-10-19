"""``skill_manage``: actions, guards, batches, side effects (P09-11)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.learning import pending
from indus.learning.skills import manage_tool
from indus.learning.skills.bundled_sync import list_modified, tree_hash
from indus.learning.skills.ledger import SkillLedger
from indus.learning.skills.manage_tool import (
    SUCCESS_LISTENERS,
    SkillWriteEvent,
    apply_pending_skill,
    lock_path,
    register_tools,
    schema_overrides,
    skill_manage,
)
from indus.learning.skills.resolver import clear_scan_cache
from indus.learning.skills.usage import SkillUsage, usage_for
from indus.learning.skills.view_tools import READS, clear_list_cache, skill_view
from indus.tools.registry import ToolRegistry
from tests.unit.learning.skills.helpers import make_skill, skill_md, with_skills_config


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    clear_scan_cache()
    clear_list_cache()
    READS.clear()
    yield
    SUCCESS_LISTENERS.clear()
    manage_tool.LIFECYCLE.clear()


def call(scope: Scope, session_id: str | None = None, **args: Any) -> dict[str, Any]:
    return json.loads(skill_manage(args, scope=scope, session_id=session_id))


def tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` except our own bookkeeping (ledger, locks, usage)."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and not rel.parts[0].startswith("."):
            out[rel.as_posix()] = path.read_bytes()
    return out


def view(scope: Scope, session_id: str, **args: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(skill_view(args, scope=scope, session_id=session_id)))


# -- actions -------------------------------------------------------------------------------------


def test_create_happy_path(scope: Scope) -> None:
    out = call(scope, action="create", name="notes", category="writing", content=skill_md("notes"))
    assert out["success"] is True, out
    folder = scope.home / "skills" / "writing" / "notes"
    assert (folder / "SKILL.md").read_text(encoding="utf-8") == skill_md("notes")
    assert out["results"] == [{"action": "create", "name": "notes", "path": str(folder)}]
    record = usage_for(scope).get("notes")
    assert record["origin"] == "learn"
    entry = SkillLedger.for_scope(scope).entries()[0]
    assert (entry.action, entry.skill, entry.actor) == ("create", "notes", "agent")
    assert [Path(r.path).name for r in entry.after] == ["SKILL.md"]


def test_create_goes_to_create_dir(scope: Scope, tmp_path: Path) -> None:
    target = tmp_path / "shared"
    scoped = with_skills_config(scope, create_dir=str(target))
    assert call(scoped, action="create", name="notes", content=skill_md("notes"))["success"]
    assert (target / "notes" / "SKILL.md").is_file()
    overrides = schema_overrides(scoped)
    assert overrides is not None and str(target) in overrides["description"]


def test_create_refuses_existing_names_and_folders(scope: Scope) -> None:
    make_skill(scope.home / "skills", "dev/notes")
    out = call(scope, action="create", name="notes", content=skill_md("notes"))
    assert "already exists" in out["error"]
    busy = scope.home / "skills" / "other"
    busy.mkdir(parents=True)
    (busy / "stray.txt").write_text("x", encoding="utf-8")
    out = call(scope, action="create", name="other", content=skill_md("other"))
    assert "already holds files" in out["error"]


def test_patch_targeted_replace_all_and_delete_text(scope: Scope) -> None:
    folder = make_skill(scope.home / "skills", "notes", body="Step one.\nStep one.\nEnd.\n")
    out = call(scope, action="patch", name="notes", old_string="Step one.", new_string="X")
    assert "occurs 2 times" in out["error"]
    out = call(
        scope,
        action="patch",
        name="notes",
        old_string="Step one.",
        new_string="Step 1.",
        replace_all=True,
    )
    assert out["success"], out
    out = call(scope, action="patch", name="notes", old_string="End.\n", new_string="")
    assert out["success"], out
    body = (folder / "SKILL.md").read_text(encoding="utf-8")
    assert "Step 1.\nStep 1.\n" in body and "End." not in body
    assert usage_for(scope).get("notes")["patch_count"] == 2


def test_patch_not_found_shows_preview(scope: Scope) -> None:
    make_skill(scope.home / "skills", "notes")
    out = call(scope, action="patch", name="notes", old_string="missing", new_string="x")
    assert "was not found" in out["error"]
    assert out["file_preview"].startswith("---")


def test_full_rewrite_and_edit_alias(scope: Scope) -> None:
    folder = make_skill(scope.home / "skills", "notes")
    out = call(scope, action="patch", name="notes", content=skill_md("notes", body="New.\n"))
    assert out["results"][0]["action"] == "edit"
    out = call(scope, action="edit", name="notes", content=skill_md("notes", body="Newer.\n"))
    assert out["success"], out
    assert "Newer." in (folder / "SKILL.md").read_text(encoding="utf-8")


def test_rewrite_must_keep_the_name_and_format(scope: Scope) -> None:
    make_skill(scope.home / "skills", "notes")
    out = call(scope, action="edit", name="notes", content=skill_md("renamed"))
    assert "does not match" in out["error"]
    out = call(scope, action="edit", name="notes", content="no frontmatter here\n")
    assert "frontmatter" in out["error"]


def test_write_and_remove_support_files(scope: Scope) -> None:
    folder = make_skill(scope.home / "skills", "notes")
    out = call(
        scope, action="write_file", name="notes", file_path="references/a.md", file_content="A\n"
    )
    assert out["success"], out
    assert (folder / "references" / "a.md").read_text(encoding="utf-8") == "A\n"
    out = call(
        scope,
        action="patch",
        name="notes",
        file_path="references/a.md",
        old_string="A",
        new_string="B",
    )
    assert out["success"], out
    assert (folder / "references" / "a.md").read_text(encoding="utf-8") == "B\n"
    out = call(scope, action="remove_file", name="notes", file_path="references/a.md")
    assert out["success"], out
    assert not (folder / "references").exists()
    out = call(scope, action="remove_file", name="notes", file_path="references/a.md")
    assert "has no file" in out["error"]


def test_delete_foreground(scope: Scope) -> None:
    folder = make_skill(scope.home / "skills", "dev/notes")
    make_skill(scope.home / "skills", "dev/keep")
    usage_for(scope).bump_use("notes")
    out = call(scope, action="delete", name="notes")
    assert out["success"], out
    assert not folder.exists()
    assert (scope.home / "skills" / "dev" / "keep").is_dir()
    assert "notes" not in usage_for(scope).load()
    entry = SkillLedger.for_scope(scope).entries()[0]
    assert entry.action == "delete" and entry.after == ()
    assert SkillLedger.for_scope(scope).rollback(entry.id).ok
    assert (folder / "SKILL.md").is_file()


def test_delete_absorbed_into_must_exist(scope: Scope) -> None:
    make_skill(scope.home / "skills", "notes")
    out = call(scope, action="delete", name="notes", absorbed_into="umbrella")
    assert "does not exist" in out["error"]
    make_skill(scope.home / "skills", "umbrella")
    assert call(scope, action="delete", name="notes", absorbed_into="umbrella")["success"]
    entry = SkillLedger.for_scope(scope).entries()[0]
    assert entry.evidence["absorbed_into"] == "umbrella"


# -- shapes ---------------------------------------------------------------------------------------


def test_patch_both_shapes_refused(scope: Scope) -> None:
    make_skill(scope.home / "skills", "notes")
    out = call(
        scope,
        action="patch",
        name="notes",
        content=skill_md("notes"),
        old_string="Steps",
        new_string="Moves",
    )
    assert "not both" in out["error"]


def test_wrong_text_slot_names_the_key(scope: Scope) -> None:
    out = call(scope, action="create", name="notes", file_content=skill_md("notes"))
    assert "'file_content'" in out["error"] and "move that text to 'content'" in out["error"]
    assert out["misplaced"] == ["file_content"]


def test_unknown_action_and_missing_name(scope: Scope) -> None:
    assert "action must be one of" in call(scope, action="rename", name="x")["error"]
    assert "name is required" in call(scope, action="delete")["error"]
    assert "operations" in call(scope)["error"]


def test_limits_refused(scope: Scope) -> None:
    out = call(scope, action="create", name="Bad Name", content=skill_md("bad"))
    assert "not allowed" in out["error"]
    long_desc = "d" * 1025
    out = call(scope, action="create", name="big", content=skill_md("big", long_desc))
    assert "description" in out["error"]
    make_skill(scope.home / "skills", "notes")
    out = call(
        scope,
        action="write_file",
        name="notes",
        file_path="references/huge.bin",
        file_content="x" * (1024 * 1024 + 1),
    )
    assert "1 MiB" in out["error"]
    out = call(
        scope, action="write_file", name="notes", file_path="examples/a.md", file_content="x"
    )
    assert "support files go under" in out["error"]


# -- batches --------------------------------------------------------------------------------------


def test_batch_applies_together(scope: Scope) -> None:
    out = call(
        scope,
        operations=[
            {"action": "create", "name": "notes", "content": skill_md("notes")},
            {
                "action": "write_file",
                "name": "notes",
                "file_path": "templates/t.md",
                "file_content": "T\n",
            },
            {"action": "patch", "name": "notes", "old_string": "Steps", "new_string": "Moves"},
        ],
    )
    assert out["success"], out
    folder = scope.home / "skills" / "notes"
    assert "Moves go here." in (folder / "SKILL.md").read_text(encoding="utf-8")
    assert (folder / "templates" / "t.md").is_file()
    assert [r["action"] for r in out["results"]] == ["create", "write_file", "patch"]


def test_batch_with_bad_op_leaves_tree_identical(scope: Scope) -> None:
    root = scope.home / "skills"
    make_skill(root, "notes")
    make_skill(root, "other")
    before = tree(root)
    out = call(
        scope,
        operations=[
            {"action": "patch", "name": "notes", "old_string": "Steps", "new_string": "Moves"},
            {"action": "create", "name": "fresh", "content": skill_md("fresh")},
            {"action": "patch", "name": "other", "old_string": "nowhere", "new_string": "x"},
        ],
    )
    assert out["failed_index"] == 2
    assert out["error"].startswith("operations[2] (patch other)")
    assert tree(root) == before
    assert not (root / "fresh").exists()


def test_batch_shape_error_names_op_and_key(scope: Scope) -> None:
    root = scope.home / "skills"
    make_skill(root, "notes")
    before = tree(root)
    out = call(
        scope,
        operations=[
            {"action": "patch", "name": "notes", "old_string": "Steps", "new_string": "Moves"},
            {
                "action": "write_file",
                "name": "notes",
                "file_path": "references/a.md",
                "content": "x",
            },  # fmt: skip
        ],
    )
    assert out["error"].startswith("operations[1]: write_file on 'notes' carries 'content'")
    assert tree(root) == before


def test_batch_runtime_failure_leaves_tree_identical(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = scope.home / "skills"
    make_skill(root, "notes")
    make_skill(root, "other")
    before = tree(root)
    real = manage_tool.atomic_write_bytes
    calls = {"n": 0}

    def flaky(path: Path, data: bytes, **kwargs: Any) -> Path:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        return real(path, data, **kwargs)

    monkeypatch.setattr(manage_tool, "atomic_write_bytes", flaky)
    out = call(
        scope,
        operations=[
            {"action": "patch", "name": "notes", "old_string": "Steps", "new_string": "Moves"},
            {"action": "create", "name": "fresh", "content": skill_md("fresh")},
            {"action": "patch", "name": "other", "old_string": "Steps", "new_string": "Moves"},
        ],
    )
    assert "nothing was saved" in out["error"]
    monkeypatch.setattr(manage_tool, "atomic_write_bytes", real)
    assert tree(root) == before
    assert not (root / "fresh").exists()


def test_delete_must_be_last_for_its_skill(scope: Scope) -> None:
    make_skill(scope.home / "skills", "notes")
    out = call(
        scope,
        operations=[
            {"action": "delete", "name": "notes"},
            {"action": "patch", "name": "notes", "old_string": "Steps", "new_string": "x"},
        ],
    )
    assert "delete must be the last operation" in out["error"]


def test_too_many_operations(scope: Scope) -> None:
    ops = [{"action": "delete", "name": f"s{n}"} for n in range(manage_tool.MAX_OPS + 1)]
    assert "at most" in call(scope, operations=ops)["error"]


# -- guards ---------------------------------------------------------------------------------------


def test_pinned_delete_refused_patch_allowed(scope: Scope) -> None:
    make_skill(scope.home / "skills", "notes")
    usage_for(scope).set_pinned("notes", True)
    out = call(scope, action="delete", name="notes")
    assert "pinned" in out["error"] and "indus curator unpin notes" in out["error"]
    out = call(scope, action="patch", name="notes", old_string="Steps", new_string="Moves")
    assert out["success"], out


def test_review_patch_needs_fresh_view(scope: Scope) -> None:
    make_skill(scope.home / "skills", "notes")
    with pending.bind_origin("background_review", attended=False):
        out = call(scope, "review-1", action="patch", name="notes", old_string="Steps",
                   new_string="Moves")  # fmt: skip
        assert out["view_required"] is True
        assert "skill_view(name='notes')" in out["error"]
        view(scope, "review-1", name="notes")
        out = call(scope, "review-1", action="patch", name="notes", old_string="Steps",
                   new_string="Moves")  # fmt: skip
        assert out["success"], out
        # the view is of the old bytes now: a second patch needs another view
        out = call(scope, "review-1", action="patch", name="notes", old_string="Moves",
                   new_string="Runs")  # fmt: skip
        assert out["view_required"] is True


def test_review_support_file_view_is_per_file(scope: Scope) -> None:
    folder = make_skill(scope.home / "skills", "notes")
    (folder / "references").mkdir()
    (folder / "references" / "a.md").write_text("alpha\n", encoding="utf-8")
    with pending.bind_origin("background_review", attended=False):
        view(scope, "r", name="notes")
        out = call(scope, "r", action="patch", name="notes", file_path="references/a.md",
                   old_string="alpha", new_string="beta")  # fmt: skip
        assert "skill_view(name='notes', file_path='references/a.md')" in out["error"]
        view(scope, "r", name="notes", file_path="references/a.md")
        out = call(scope, "r", action="patch", name="notes", file_path="references/a.md",
                   old_string="alpha", new_string="beta")  # fmt: skip
        assert out["success"], out
        # a new file needs no view
        out = call(scope, "r", action="write_file", name="notes", file_path="references/b.md",
                   file_content="new\n")  # fmt: skip
        assert out["success"], out


def test_review_delete_of_learn_origin_refused(scope: Scope) -> None:
    assert call(scope, action="create", name="mine", content=skill_md("mine"))["success"]
    with pending.bind_origin("background_review", attended=False):
        out = call(scope, "r", action="delete", name="mine")
    assert "the review may not delete 'mine'" in out["error"]
    assert (scope.home / "skills" / "mine").is_dir()


def test_review_delete_of_agent_skill_is_archived(scope: Scope) -> None:
    with pending.bind_origin("background_review", attended=False):
        assert call(scope, "r", action="create", name="made", content=skill_md("made"))["success"]
        assert usage_for(scope).get("made")["origin"] == "agent"
        out = call(scope, "r", action="delete", name="made")
    assert out["success"], out
    archived = scope.home / "skills" / ".archive" / "made"
    assert out["results"][0]["archived_to"] == str(archived)
    assert (archived / "SKILL.md").is_file()
    assert not (scope.home / "skills" / "made").exists()
    assert usage_for(scope).get("made")["state"] == "archived"
    entry = SkillLedger.for_scope(scope).entries()[0]
    assert entry.action == "archive" and entry.evidence["archived"] is True


def test_review_delete_of_pinned_agent_skill_refused(scope: Scope) -> None:
    with pending.bind_origin("background_review", attended=False):
        call(scope, "r", action="create", name="made", content=skill_md("made"))
        usage_for(scope).set_pinned("made", True)
        assert "pinned" in call(scope, "r", action="delete", name="made")["error"]


def test_path_escapes_refused(scope: Scope, tmp_path: Path) -> None:
    folder = make_skill(scope.home / "skills", "notes")
    out = call(scope, action="write_file", name="notes", file_path="../evil.md", file_content="x")
    assert "'..'" in out["error"]
    out = call(
        scope, action="write_file", name="notes", file_path="references/../../x", file_content="x"
    )
    assert "'..'" in out["error"]
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, folder / "references")
    out = call(
        scope, action="write_file", name="notes", file_path="references/a.md", file_content="x"
    )
    assert "leads outside" in out["error"]
    assert list(outside.iterdir()) == []


def test_symlinked_skill_outside_roots_refused(scope: Scope, tmp_path: Path) -> None:
    elsewhere = make_skill(tmp_path / "elsewhere", "linked")
    root = scope.home / "skills"
    root.mkdir(parents=True)
    os.symlink(elsewhere, root / "linked")
    out = call(scope, action="patch", name="linked", old_string="Steps", new_string="x")
    assert "outside the skill folders" in out["error"]
    assert "Steps" in (elsewhere / "SKILL.md").read_text(encoding="utf-8")


def test_lock_lives_outside_the_skill(scope: Scope) -> None:
    path = lock_path(scope.home, "dev/notes")
    assert path == lock_path(scope.home, "notes")
    assert path.parent == scope.home / "skills" / ".locks"
    assert len(path.stem) == 16


def test_concurrent_patches_lose_nothing(scope: Scope) -> None:
    lines = "".join(f"line {n}\n" for n in range(8))
    folder = make_skill(scope.home / "skills", "notes", body=lines)
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def worker(numbers: range) -> None:
        barrier.wait()
        for n in numbers:
            out = call(scope, action="patch", name="notes", old_string=f"line {n}\n",
                       new_string=f"done {n}\n")  # fmt: skip
            if not out.get("success"):
                errors.append(str(out))

    threads = [threading.Thread(target=worker, args=(r,)) for r in (range(0, 4), range(4, 8))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    body = (folder / "SKILL.md").read_text(encoding="utf-8")
    assert all(f"done {n}\n" in body for n in range(8))


# -- side effects ---------------------------------------------------------------------------------


def test_lint_findings_present_but_write_succeeds(scope: Scope) -> None:
    body = "Rule.\n" + ("word " * 5000) + "\n"
    out = call(scope, action="create", name="long", content=skill_md("long", body=body))
    assert out["success"], out
    assert [f["rule"] for f in out["lint"]] == ["lint.oversized"]
    assert (scope.home / "skills" / "long" / "SKILL.md").is_file()


def test_listeners_get_the_event(scope: Scope) -> None:
    seen: list[SkillWriteEvent] = []
    SUCCESS_LISTENERS.append(seen.append)
    manage_tool.LIFECYCLE.append(seen.append)
    call(scope, "s1", action="create", name="notes", content=skill_md("notes"))
    assert len(seen) == 2
    assert seen[0].origin == "foreground" and seen[0].session_id == "s1"
    assert seen[0].created == ("notes",) and seen[0].actions == ("create",)


def test_side_effect_failures_do_not_fail_the_call(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(SkillUsage, "record_created", boom)
    monkeypatch.setattr(manage_tool, "lint_skill", boom)
    monkeypatch.setattr(SkillLedger, "for_scope", boom)
    SUCCESS_LISTENERS.append(boom)
    out = call(scope, action="create", name="notes", content=skill_md("notes"))
    assert out["success"], out
    assert (scope.home / "skills" / "notes" / "SKILL.md").is_file()


def test_bundled_edit_marks_user_modified(scope: Scope) -> None:
    root = scope.home / "skills"
    folder = make_skill(root, "dev/shipped")
    (root / ".bundled_manifest").write_text(f"shipped:{tree_hash(folder)}\n", encoding="utf-8")
    assert list_modified(scope.home) == []
    out = call(scope, action="patch", name="shipped", old_string="Steps", new_string="Moves")
    assert out["success"], out
    assert "indus skills sync" in out["notes"][0]
    assert list_modified(scope.home) == ["shipped"]


def test_scan_hook_refuses_and_restores(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = make_skill(scope.home / "skills", "notes")
    original = (folder / "SKILL.md").read_bytes()
    scoped = with_skills_config(scope, scan_agent_created=True)
    monkeypatch.setattr(manage_tool, "SKILL_SCANNER", lambda _path: "found a secret")
    out = call(scoped, action="patch", name="notes", old_string="Steps", new_string="Moves")
    assert "content scan" in out["error"]
    assert (folder / "SKILL.md").read_bytes() == original
    monkeypatch.setattr(manage_tool, "SKILL_SCANNER", None)
    assert call(scoped, action="patch", name="notes", old_string="Steps", new_string="Moves")[
        "success"
    ]


# -- approval gate --------------------------------------------------------------------------------


def test_gate_stages_and_approve_replays_with_diff(scope: Scope) -> None:
    make_skill(scope.home / "skills", "notes")
    gated = with_skills_config(scope, write_approval=True)
    out = call(gated, action="patch", name="notes", old_string="Steps", new_string="Moves")
    assert out["staged"] is True
    store = pending.PendingStore(scope.home, scope.clock)
    (record,) = store.list("skills")
    assert record.summary == "patch notes"
    assert "Steps" in (scope.home / "skills" / "notes" / "SKILL.md").read_text(encoding="utf-8")
    (approval,) = pending.approve(store, "skills", "all", lambda r: apply_pending_skill(gated, r))
    assert approval.ok, approval.applied.message
    assert "-Steps go here." in approval.applied.diff
    assert "+Moves go here." in approval.applied.diff
    assert store.list("skills") == []


def test_register_tools(scope: Scope) -> None:
    registry = ToolRegistry(clock=scope.clock)
    register_tools(registry)
    entry = registry.get("skill_manage")
    assert entry is not None and entry.toolset == "skills"
