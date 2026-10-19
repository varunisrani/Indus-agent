"""Pending writes and the write-approval gate (P09-03)."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import stat
import sys

import pytest

from indus.foundation.scope import Scope
from indus.learning import pending
from indus.learning.memory.store import MemoryStore, Op
from indus.learning.memory.tool import apply_pending
from indus.learning.pending import Applied, PendingStore, PendingWrite
from tests.fakes.clock import FakeClock
from tests.unit.learning._helpers_a import with_config


@pytest.fixture
def store(scope: Scope) -> PendingStore:
    return PendingStore(scope.home, scope.clock)


def stage_remove(staged: PendingStore, entry: str, old_text: str) -> PendingWrite:
    op = {"action": "remove", "old_text": old_text, "content": "", "pinned_entry": entry}
    return staged.stage(
        "memory",
        {"target": "memory", "operations": [op]},
        summary=f"remove {old_text}",
        origin="background_review",
        pinned_entry=entry,
    )


def test_stage_survives_restart(scope: Scope, store: PendingStore) -> None:
    record = store.stage(
        "memory", {"target": "user", "operations": []}, summary=" s ", origin="foreground"
    )
    again = PendingStore(scope.home, scope.clock)  # a new process would build a new store
    (listed,) = again.list("memory")
    assert listed == record and listed.summary == "s"
    path = scope.home / "pending" / "memory" / f"{record.id}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["origin"] == "foreground"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert again.count("memory") == 1 and again.count("skills") == 0


def test_approve_applies_and_removes_record(scope: Scope, store: PendingStore) -> None:
    memory = MemoryStore(scope.home)
    memory.apply("memory", [Op("add", "old: port 8080"), Op("add", "other")])
    record = stage_remove(store, "old: port 8080", "8080")
    (result,) = pending.approve(store, "memory", record.id, lambda r: apply_pending(memory, r))
    assert result.ok
    assert result.applied.overwritten == ("old: port 8080",)
    assert memory.entries("memory") == ["other"]
    assert store.list("memory") == []
    text = pending.render_approval(result)
    assert "applied" in text and "old: port 8080" in text


def test_edited_target_refuses_and_keeps_record(scope: Scope, store: PendingStore) -> None:
    memory = MemoryStore(scope.home)
    memory.apply("memory", [Op("add", "old: port 8080")])
    record = stage_remove(store, "old: port 8080", "8080")
    memory.apply("memory", [Op("patch", "old: port 8080 (still)", "old: port 8080")])
    (result,) = pending.approve(store, "memory", record.id, lambda r: apply_pending(memory, r))
    assert not result.ok and "changed after it was staged" in result.applied.message
    assert [r.id for r in store.list("memory")] == [record.id]
    assert memory.entries("memory") == ["old: port 8080 (still)"]
    assert "not applied" in pending.render_approval(result)


def test_reject_deletes_record_only(scope: Scope, store: PendingStore) -> None:
    memory = MemoryStore(scope.home)
    memory.apply("memory", [Op("add", "keep")])
    record = stage_remove(store, "keep", "keep")
    assert pending.reject(store, "memory", record.id) == [record.id]
    assert store.list("memory") == [] and memory.entries("memory") == ["keep"]
    assert pending.reject(store, "memory", "nope") == []


def test_all_applies_in_creation_order(scope: Scope) -> None:
    clock = FakeClock(scope.clock.now())  # time never moves: order comes from ``seq``
    staged = PendingStore(scope.home, clock)
    ids = [
        staged.stage("skills", {"n": n}, summary=f"w{n}", origin="foreground").id for n in range(5)
    ]
    seen: list[int] = []

    def apply(record: PendingWrite) -> Applied:
        seen.append(int(record.payload["n"]))
        return Applied(record.payload["n"] != 3, "ok" if record.payload["n"] != 3 else "refused")

    results = pending.approve(staged, "skills", "all", apply)
    assert seen == [0, 1, 2, 3, 4]
    assert [r.id for r in results] == ids
    assert [r.id for r in staged.list("skills")] == [ids[3]]  # the refused one stays


def test_unknown_id_and_raising_apply(store: PendingStore) -> None:
    record = store.stage("skills", {"x": 1}, summary="w", origin="foreground")

    def explode(_: PendingWrite) -> Applied:
        raise RuntimeError("boom")

    results = pending.approve(store, "skills", [record.id, "missing"], explode)
    assert not results[0].ok and "boom" in results[0].applied.message
    assert not results[1].found
    assert store.get("skills", record.id) is not None


def test_unreadable_record_is_skipped(scope: Scope, store: PendingStore) -> None:
    store.stage("memory", {"a": 1}, summary="good", origin="foreground")
    (scope.home / "pending" / "memory" / "bad.json").write_text("{not json", encoding="utf-8")
    assert [r.summary for r in store.list("memory")] == ["good"]
    assert store.get("memory", "../escape") is None


def test_skill_diff_truncated_for_chat() -> None:
    before = "".join(f"line {i}\n" for i in range(400))
    after = "".join(f"line {i} changed\n" for i in range(400))
    full = pending.skill_diff(before, after, "SKILL.md", surface="cli")
    chat = pending.skill_diff(before, after, "SKILL.md", surface="chat")
    assert len(full) > pending.CHAT_DIFF_LIMIT
    assert len(chat) == pending.CHAT_DIFF_LIMIT and "diff cut" in chat
    assert full.startswith("--- a/SKILL.md\n+++ b/SKILL.md\n")
    assert pending.skill_diff("same\n", "same\n", "x") == "(no change)\n"


def test_render_approval_cuts_skill_diff_on_chat() -> None:
    record = PendingWrite("abc", "skills", {}, "patch skill", "foreground", 0.0)
    diff = "+" + "x" * 5000 + "\n"
    approval = pending.Approval("abc", True, Applied(True, "", diff=diff), record)
    assert len(pending.render_approval(approval, surface="cli")) > 5000
    assert len(pending.render_approval(approval, surface="chat")) < 3200


@pytest.mark.parametrize(
    ("subsystem", "gate", "origin", "can_ask", "expected"),
    [
        ("memory", False, "foreground", True, "allow"),
        ("memory", True, "foreground", True, "ask"),
        ("memory", True, "foreground", False, "stage"),
        ("memory", True, "background_review", True, "stage"),
        ("memory", True, "learn", True, "ask"),  # a /learn turn is a person's turn (compare)
        ("memory", True, "learn", False, "stage"),
        ("skills", True, "foreground", True, "stage"),
        ("skills", False, "background_review", False, "allow"),
    ],
)
def test_decision_table(
    subsystem: pending.Subsystem,
    gate: bool,
    origin: pending.Origin,
    can_ask: bool,
    expected: str,
) -> None:
    assert pending.decide(subsystem, gate=gate, origin=origin, can_ask=can_ask) == expected


def test_gate_reads_config(scope: Scope) -> None:
    assert not pending.gate_on(scope, "memory")
    assert pending.gate_on(with_config(scope, {"memory": {"write_approval": True}}), "memory")
    assert pending.gate_on(with_config(scope, {"skills": {"write_approval": "on"}}), "skills")
    assert not pending.gate_on(with_config(scope, {"skills": {"write_approval": 7}}), "skills")


def test_origin_binding_follows_tasks() -> None:
    assert pending.current_origin() == "foreground" and not pending.unattended_review()

    async def inside() -> tuple[str, bool]:
        return pending.current_origin(), pending.unattended_review()

    async def main() -> tuple[str, bool]:
        with pending.bind_origin("background_review", attended=False):
            return await asyncio.create_task(inside())

    assert asyncio.run(main()) == ("background_review", True)
    with pending.bind_origin("background_review", attended=True):
        copied = contextvars.copy_context()
        assert not pending.unattended_review()
    assert copied.run(pending.current_origin) == "background_review"
    assert pending.current_origin() == "foreground"
    with pytest.raises(ValueError), pending.bind_origin("somebody"):  # type: ignore[arg-type]
        pass
