"""``indus memory`` verbs (P09-24)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from indus.cli.commands import memory as memory_cmd
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.learning import pending
from indus.learning.memory.store import DELIMITER, MemoryStore
from tests.unit.cli.commands._helpers_d import run


def seed(scope: Scope, *entries: str, target: str = "MEMORY.md") -> Path:
    folder = scope.home / "memories"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / target
    path.write_text(DELIMITER.join(entries), encoding="utf-8")
    return path


def stage_add(scope: Scope, text: str) -> pending.PendingWrite:
    return pending.PendingStore(scope.home, scope.clock).stage(
        "memory",
        {"target": "memory", "operations": [{"action": "add", "content": text}]},
        summary=f"add: {text}",
        origin="background_review",
    )


def test_show(scope: Scope) -> None:
    seed(scope, "Prefers short answers.", "Uses uv.\nNever pip.")
    result = run(scope, "memory", "show", "memory")
    assert result.code == 0
    assert "saved notes (MEMORY.md):" in result.out
    assert "  1. Prefers short answers." in result.out
    assert "  2. Uses uv.\n     Never pip." in result.out
    both = run(scope, "memory", "show")
    assert "user profile (USER.md):" in both.out and "(empty)" in both.out


def test_status(scope: Scope) -> None:
    seed(scope, "x" * 100)
    stage_add(scope, "later")
    result = run(scope, "memory", "status")
    assert result.code == 0
    assert "memory  saved notes" in result.out and "on" in result.out
    assert "100/2,200 chars" in result.out
    assert "Write approval: off" in result.out
    assert "Waiting for approval: 1" in result.out


def test_approval_on_off_writes_config(scope: Scope) -> None:
    result = run(scope, "memory", "approval", "on")
    assert result.code == 0, result.err
    assert loader.load(scope).get("memory.write_approval") is True
    assert "Write approval: on" in run(scope, "memory", "status").out
    run(scope, "memory", "approval", "off")
    assert loader.load(scope).get("memory.write_approval") is False


def test_approve_all_applies_in_creation_order(scope: Scope) -> None:
    first = stage_add(scope, "First fact.")
    second = stage_add(scope, "Second fact.")
    listed = run(scope, "memory", "pending")
    assert listed.out.index(first.id) < listed.out.index(second.id)
    result = run(scope, "memory", "approve", "all")
    assert result.code == 0, result.err
    assert result.out.index(first.id) < result.out.index(second.id)
    assert MemoryStore(scope.home).entries("memory") == ["First fact.", "Second fact."]
    assert run(scope, "memory", "pending").out.strip() == "Nothing is waiting for approval."


def test_approve_shows_replaced_entry_and_refuses_stale(scope: Scope) -> None:
    seed(scope, "Old fact.", "Other fact.")
    store = pending.PendingStore(scope.home, scope.clock)
    ops = [{"action": "remove", "old_text": "Old fact", "pinned_entry": "Old fact."}]
    record = store.stage(
        "memory", {"target": "memory", "operations": ops}, summary="remove", origin="foreground"
    )
    stale = store.stage(
        "memory",
        {
            "target": "memory",
            "operations": [{"action": "remove", "old_text": "Gone", "pinned_entry": "Gone."}],
        },
        summary="remove gone",
        origin="foreground",
    )
    result = run(scope, "memory", "approve", record.id, stale.id)
    assert result.code == 0
    assert "replaced or removed:\n    Old fact." in result.out
    assert "not applied" in result.out
    assert [r.id for r in store.list("memory")] == [stale.id]


def test_approve_unknown_exits_1(scope: Scope) -> None:
    result = run(scope, "memory", "approve", "abc123")
    assert result.code == 1
    assert "no pending write abc123" in result.out


def test_reject(scope: Scope) -> None:
    record = stage_add(scope, "Nope.")
    assert run(scope, "memory", "reject", record.id).out.strip() == f"Rejected {record.id}."
    assert MemoryStore(scope.home).entries("memory") == []
    assert run(scope, "memory", "reject", "all").code == 1


def test_edit_needs_a_terminal(scope: Scope) -> None:
    result = run(scope, "memory", "edit", "memory")
    assert result.code == 2
    assert "needs a terminal" in result.err


def _editor(text: str, calls: list[Sequence[str]]) -> Any:
    def edit(command: Sequence[str], path: Path) -> int:
        calls.append(command)
        path.write_text(text, encoding="utf-8")
        return 0

    return edit


def test_edit_saves(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    path = seed(scope, "One.")
    calls: list[Sequence[str]] = []
    monkeypatch.setattr(memory_cmd, "launch_editor", _editor("One.\n§\nTwo.\n", calls))
    edited = scope.with_env(scope.env.with_overlay({"EDITOR": "nano -w", "VISUAL": None}))
    result = run(edited, "memory", "edit", "memory", tty=True)
    assert result.code == 0, result.err
    assert calls == [["nano", "-w"]]
    assert path.read_text(encoding="utf-8") == "One.\n§\nTwo."
    assert "Saved 2 entries" in result.out
    assert not list(path.parent.glob(".*.edit.md"))


def test_edit_refuses_when_the_file_changed(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    path = seed(scope, "One.")

    def racing(_command: Sequence[str], draft: Path) -> int:
        path.write_text("Agent wrote this.", encoding="utf-8")
        draft.write_text("Mine.", encoding="utf-8")
        return 0

    monkeypatch.setattr(memory_cmd, "launch_editor", racing)
    result = run(scope, "memory", "edit", "memory", tty=True)
    assert result.code == 1
    assert "changed while it was open" in result.err
    assert path.read_text(encoding="utf-8") == "Agent wrote this."


def test_edit_no_change_and_failed_editor(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(scope, "One.")
    monkeypatch.setattr(memory_cmd, "launch_editor", _editor("One.", []))
    assert run(scope, "memory", "edit", "memory", tty=True).out.strip() == "No changes."
    monkeypatch.setattr(memory_cmd, "launch_editor", lambda _c, _p: 3)
    failed = run(scope, "memory", "edit", "user", tty=True)
    assert failed.code == 1 and "exited with 3" in failed.err
