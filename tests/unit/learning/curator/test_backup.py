"""Skill snapshots and whole-tree rollback (P09-19)."""

from __future__ import annotations

import io
import tarfile

import pytest

from indus.foundation.scope import Scope
from indus.learning.curator.backup import (
    BackupsDisabled,
    RollbackFailed,
    list_backups,
    rollback,
    snapshot,
)
from tests.fakes.clock import FakeClock
from tests.unit.learning.curator._helpers_f import (
    add_skill,
    clean_registries,  # noqa: F401  (autouse fixture)
    skills_root,
    with_settings,
)


def _tree(scope: Scope) -> dict[str, bytes]:
    root = skills_root(scope)
    out: dict[str, bytes] = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        skipped = rel.parts[0] in (".curator_backups", ".archive") or rel.name.endswith(".lock")
        if path.is_file() and not skipped:
            out[rel.as_posix()] = path.read_bytes()
    return out


def test_snapshot_scope_and_manifest(scope: Scope) -> None:
    add_skill(scope, "alpha")
    root = skills_root(scope)
    (root / ".archive" / "gone").mkdir(parents=True)
    (root / ".archive" / "gone" / "SKILL.md").write_text("x", encoding="utf-8")
    (root / "alpha" / "__pycache__").mkdir()
    (root / "alpha" / "__pycache__" / "m.pyc").write_bytes(b"\0")
    (root / ".ledger.jsonl").write_text("{}\n", encoding="utf-8")
    info = snapshot(scope, "test")
    assert info is not None and info.reason == "test" and info.skill_files == 1
    with tarfile.open(info.path / "skills.tar.gz") as tar:
        names = set(tar.getnames())
    assert "alpha/SKILL.md" in names and ".usage.json" in names
    assert not any(n.startswith((".archive", ".ledger", ".curator_backups")) for n in names)
    assert not any("__pycache__" in n for n in names)
    assert info.id == "2026-01-01T09-00-00Z"


def test_keep_prunes_oldest(scope: Scope, fake_clock: FakeClock) -> None:
    add_skill(scope, "alpha")
    ids = []
    for _ in range(3):
        info = snapshot(scope)
        assert info is not None
        ids.append(info.id)
        fake_clock.advance(60)
    assert [b.id for b in list_backups(scope)] == [ids[2], ids[1]]


def test_same_second_ids(scope: Scope) -> None:
    add_skill(scope, "alpha")
    first, second = snapshot(scope), snapshot(scope)
    assert first is not None and second is not None
    assert second.id == f"{first.id}-01"


def test_rollback_restores(scope: Scope, fake_clock: FakeClock) -> None:
    add_skill(scope, "alpha", body="first version\n")
    before = _tree(scope)
    target = snapshot(scope)
    assert target is not None
    fake_clock.advance(60)
    (skills_root(scope) / "alpha" / "SKILL.md").write_text("broken", encoding="utf-8")
    add_skill(scope, "beta")
    restored, safety = rollback(scope)
    assert restored.id == target.id
    assert _tree(scope) == before
    assert safety.reason == f"before rollback to {target.id}"
    ids = [b.id for b in list_backups(scope)]
    assert target.id in ids and safety.id in ids
    # the safety snapshot can itself be rolled back to
    fake_clock.advance(60)
    rollback(scope, safety.id)
    assert (skills_root(scope) / "beta" / "SKILL.md").is_file()


def test_disabled_refuses(scope: Scope) -> None:
    add_skill(scope, "alpha")
    off = with_settings(scope, curator={"backup": {"enabled": False}})
    assert snapshot(off) is None
    with pytest.raises(BackupsDisabled):
        rollback(off)


def test_no_snapshot(scope: Scope) -> None:
    add_skill(scope, "alpha")
    with pytest.raises(RollbackFailed, match="no skills snapshot"):
        rollback(scope)
    with pytest.raises(RollbackFailed, match="named 'nope'"):
        rollback(scope, "nope")


def test_unsafe_member_refused(scope: Scope) -> None:
    add_skill(scope, "alpha")
    info = snapshot(scope)
    assert info is not None
    archive = info.path / "skills.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        data = b"evil"
        member = tarfile.TarInfo("../escape.txt")
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
    before = _tree(scope)
    with pytest.raises(RollbackFailed, match="unsafe path"):
        rollback(scope, info.id)
    assert _tree(scope) == before
    assert not (scope.home / "escape.txt").exists()


def test_rollback_keeps_left_out_folders(scope: Scope, fake_clock: FakeClock) -> None:
    folder = add_skill(scope, "alpha")
    (folder / ".git").mkdir()
    (folder / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
    (folder / "scripts" / "__pycache__").mkdir(parents=True)
    target = snapshot(scope)
    assert target is not None
    fake_clock.advance(60)
    rollback(scope, target.id)
    assert (folder / ".git" / "HEAD").read_text() == "ref\n"
    assert (folder / "scripts" / "__pycache__").is_dir()


def test_prune_only_run_ages_snapshots(scope: Scope, fake_clock: FakeClock) -> None:
    from indus.learning.curator.lifecycle import run_curator

    add_skill(scope, "alpha")
    for _ in range(3):
        snapshot(scope, settings=None)
        fake_clock.advance(60)
    keep_one = with_settings(scope, curator={"backup": {"keep": 1}})
    run_curator(keep_one)
    assert len(list_backups(scope)) == 1
