"""The skill mutation ledger: entries, blobs, rollback, size cap (P09-13)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.learning.skills import ledger as ledger_mod
from indus.learning.skills.ledger import (
    BLOB_GRACE_S,
    LEDGER_FILE,
    FileRef,
    SkillLedger,
    bind_actor,
    current_actor,
)
from tests.fakes.clock import FakeClock
from tests.unit.learning.skills.helpers import make_skill, skill_md, with_skills_config


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.relative_to(root).parts[0].startswith(".")
    }


def ledger_for(scope: Scope, max_bytes: int = ledger_mod.DEFAULT_MAX_BYTES) -> SkillLedger:
    return SkillLedger(scope.home, clock=scope.clock, max_bytes=max_bytes)


def test_entry_shape_and_blobs(scope: Scope) -> None:
    root = scope.home / "skills"
    folder = make_skill(root, "dev/notes")
    led = ledger_for(scope)
    before = led.capture(folder)
    (folder / "SKILL.md").write_text(skill_md("notes", body="New steps.\n"), encoding="utf-8")
    entry_id = led.record(
        "patch", "notes", before=before, after_dir=folder, evidence={"session_id": "s1"}
    )
    assert entry_id is not None
    line = json.loads((root / LEDGER_FILE).read_text(encoding="utf-8").strip())
    assert set(line) == {"id", "ts", "actor", "action", "skill", "evidence", "before", "after"}
    assert line["actor"] == "agent"
    assert line["action"] == "patch"
    assert line["evidence"] == {"session_id": "s1"}
    assert line["before"][0]["path"] == str(folder / "SKILL.md")
    for ref in (*line["before"], *line["after"]):
        blob = led.read_blob(ref["sha256"])
        assert blob is not None
    # the same bytes are stored once
    led.capture(folder)
    assert len(list(led.blobs.iterdir())) == 2


def test_actor_binding() -> None:
    assert current_actor() == "agent"
    with bind_actor("user"):
        assert current_actor() == "user"
    with pytest.raises(ValueError, match="unknown ledger actor"), bind_actor("someone"):  # type: ignore[arg-type]
        pass


def test_patch_then_rollback_restores_bytes(scope: Scope) -> None:
    root = scope.home / "skills"
    folder = make_skill(root, "notes")
    (folder / "references").mkdir()
    (folder / "references" / "a.md").write_text("alpha\n", encoding="utf-8")
    original = tree_bytes(root)
    led = ledger_for(scope)
    before = led.capture(folder)
    (folder / "SKILL.md").write_text(skill_md("notes", body="Changed.\n"), encoding="utf-8")
    entry_id = led.record("patch", "notes", before=before, after_dir=folder)
    assert entry_id is not None

    result = led.rollback(entry_id)
    assert result.ok, result.message
    assert result.restored == 1 and result.removed == 0
    assert tree_bytes(root) == original
    newest = led.entries()[0]
    assert newest.action == "rollback"
    assert newest.evidence == {"rollback_of": entry_id}
    # the rollback itself can be undone
    undo = led.rollback(newest.id)
    assert undo.ok, undo.message
    assert b"Changed." in (folder / "SKILL.md").read_bytes()


def test_rollback_of_create_removes_the_file(scope: Scope) -> None:
    root = scope.home / "skills"
    root.mkdir(parents=True)
    led = ledger_for(scope)
    folder = make_skill(root, "tools/fresh")
    entry_id = led.record("create", "fresh", before=[], after_dir=folder)
    assert entry_id is not None
    result = led.rollback(entry_id)
    assert result.ok, result.message
    assert result.removed == 1
    assert not folder.exists()
    assert not (root / "tools").exists()
    assert root.is_dir()


def test_safety_capture_failure_refuses(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    root = scope.home / "skills"
    folder = make_skill(root, "notes")
    led = ledger_for(scope)
    before = led.capture(folder)
    (folder / "SKILL.md").write_text(skill_md("notes", body="Edited.\n"), encoding="utf-8")
    entry_id = led.record("patch", "notes", before=before, after_dir=folder)
    assert entry_id is not None
    edited = tree_bytes(root)
    lines_before = (root / LEDGER_FILE).read_text(encoding="utf-8")

    def broken(_self: SkillLedger, _data: bytes) -> str:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(SkillLedger, "store_blob", broken)
    result = led.rollback(entry_id)
    assert not result.ok
    assert "nothing changed" in result.message
    assert tree_bytes(root) == edited
    assert (root / LEDGER_FILE).read_text(encoding="utf-8") == lines_before


def test_missing_blob_refuses(scope: Scope) -> None:
    root = scope.home / "skills"
    folder = make_skill(root, "notes")
    led = ledger_for(scope)
    before = led.capture(folder)
    (folder / "SKILL.md").write_text(skill_md("notes", body="Edited.\n"), encoding="utf-8")
    entry_id = led.record("patch", "notes", before=before, after_dir=folder)
    assert entry_id is not None
    (led.blobs / before[0].sha256).unlink()
    result = led.rollback(entry_id)
    assert not result.ok and "missing" in result.message
    assert b"Edited." in (folder / "SKILL.md").read_bytes()


def test_rollback_refuses_outside_paths(scope: Scope, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("keep me\n", encoding="utf-8")
    led = ledger_for(scope)
    digest = led.store_blob(b"overwritten\n")
    entry_id = led.record(
        "patch",
        "evil",
        before=[FileRef(str(outside), digest)],
        after=[FileRef(str(outside), "0" * 64)],
    )
    assert entry_id is not None
    result = led.rollback(entry_id)
    assert not result.ok and "outside the skill folders" in result.message
    assert outside.read_text(encoding="utf-8") == "keep me\n"


def test_external_root_is_allowed(scope: Scope, tmp_path: Path) -> None:
    external = tmp_path / "shared-skills"
    folder = make_skill(external, "shared")
    scoped = with_skills_config(scope, external_dirs=[str(external)])
    led = SkillLedger.for_scope(scoped)
    before = led.capture(folder)
    (folder / "SKILL.md").write_text(skill_md("shared", body="Other.\n"), encoding="utf-8")
    entry_id = led.record("patch", "shared", before=before, after_dir=folder)
    assert entry_id is not None
    assert led.rollback(entry_id).ok


def test_record_never_raises(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    led = ledger_for(scope)

    def broken(_self: SkillLedger, _entry: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(SkillLedger, "_append", broken)
    assert led.record("create", "x", before=[], after=[]) is None


def _fill(led: SkillLedger, folder: Path, count: int) -> list[str]:
    ids: list[str] = []
    for n in range(count):
        before = led.capture(folder)
        (folder / "SKILL.md").write_text(skill_md("big", body=f"Round {n}.\n"), encoding="utf-8")
        entry_id = led.record("patch", "big", before=before, after_dir=folder)
        assert entry_id is not None
        ids.append(entry_id)
    return ids


def test_size_cap_keeps_newest(scope: Scope) -> None:
    root = scope.home / "skills"
    folder = make_skill(root, "big")
    led = ledger_for(scope, max_bytes=1500)
    ids = _fill(led, folder, 12)
    size = (root / LEDGER_FILE).stat().st_size
    assert size <= 1500
    kept = [e.id for e in led.entries()]
    assert kept[0] == ids[-1]
    assert len(kept) < len(ids)


def test_size_cap_zero_never_maintains(scope: Scope) -> None:
    root = scope.home / "skills"
    folder = make_skill(root, "big")
    led = ledger_for(scope, max_bytes=0)
    ids = _fill(led, folder, 12)
    assert [e.id for e in led.entries()] == list(reversed(ids))


def test_newest_entry_survives_a_tiny_cap(scope: Scope) -> None:
    root = scope.home / "skills"
    folder = make_skill(root, "big")
    led = ledger_for(scope, max_bytes=10)
    ids = _fill(led, folder, 3)
    assert [e.id for e in led.entries()] == [ids[-1]]


def test_compaction_drops_unchanged_pairs_first(scope: Scope) -> None:
    root = scope.home / "skills"
    folder = make_skill(root, "pkg")
    refs = folder / "references"
    refs.mkdir()
    for n in range(20):
        (refs / f"note-{n:02d}.md").write_text(f"note {n}\n", encoding="utf-8")
    led = ledger_for(scope, max_bytes=0)
    ids = _fill(led, folder, 3)
    full = (root / LEDGER_FILE).stat().st_size
    led.max_bytes = full - 1
    led.maintain()
    entries = led.entries()
    assert [e.id for e in entries] == list(reversed(ids))  # nothing trimmed
    for entry in entries:
        assert [Path(r.path).name for r in entry.before] == ["SKILL.md"]
    assert (root / LEDGER_FILE).stat().st_size < full


def test_blob_sweep_respects_one_hour(scope: Scope, fake_clock: FakeClock) -> None:
    led = ledger_for(scope)
    young = led.store_blob(b"young")
    old = led.store_blob(b"old")
    kept = led.store_blob(b"referenced")
    led.record("create", "x", before=[], after=[FileRef(str(scope.home / "skills/x"), kept)])
    now = fake_clock.now().timestamp()
    os.utime(led.blobs / young, (now - BLOB_GRACE_S + 60, now - BLOB_GRACE_S + 60))
    os.utime(led.blobs / old, (now - BLOB_GRACE_S - 60, now - BLOB_GRACE_S - 60))
    os.utime(led.blobs / kept, (now - 10 * BLOB_GRACE_S, now - 10 * BLOB_GRACE_S))
    assert led.sweep_blobs() == 1
    assert sorted(p.name for p in led.blobs.iterdir()) == sorted([young, kept])
    fake_clock.advance(120)
    assert led.sweep_blobs() == 1
    assert [p.name for p in led.blobs.iterdir()] == [kept]


def test_malformed_lines_skip_listing_and_block_sweep(scope: Scope) -> None:
    led = ledger_for(scope)
    entry_id = led.record("create", "x", before=[], after=[])
    with open(led.path, "a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    orphan = led.store_blob(b"orphan")
    os.utime(led.blobs / orphan, (0, 0))
    assert [e.id for e in led.entries()] == [entry_id]
    assert led.sweep_blobs() == 0
    assert (led.blobs / orphan).exists()


def test_caches_are_not_captured(scope: Scope) -> None:
    folder = make_skill(scope.home / "skills", "tool")
    (folder / "scripts" / "__pycache__").mkdir(parents=True)
    (folder / "scripts" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (folder / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
    names = sorted(Path(r.path).name for r in ledger_for(scope).capture(folder))
    assert names == ["SKILL.md", "run.py"]


def test_for_scope_reads_the_cap(scope: Scope) -> None:
    led = SkillLedger.for_scope(with_skills_config(scope, ledger_max_bytes=1234))
    assert led.max_bytes == 1234
    assert SkillLedger.for_scope(scope).max_bytes == ledger_mod.DEFAULT_MAX_BYTES
