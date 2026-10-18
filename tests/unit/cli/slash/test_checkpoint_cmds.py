"""P11-17: `/rollback` and `/checkpoints` in the classic CLI."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.backends.checkpoints.store import ShadowStore, Snapshot
from indus.backends.checkpoints.trigger import TRIGGER
from indus.cli.slash.dispatch import dispatch
from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock
from tests.unit.backends.checkpoints._helpers import write
from tests.unit.cli.slash._fakes import Ctx, make_ctx


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write(root, "a.txt", "v1\n")
    return root


@pytest.fixture
def ctx(scope: Scope, folder: Path) -> Iterator[Ctx]:
    made = make_ctx(scope, session_cwd=lambda: str(folder))
    yield made
    TRIGGER.forget(scope, made.session_id)


def snap(scope: Scope, folder: Path, clock: FakeClock) -> None:
    clock.advance(60)
    assert isinstance(
        ShadowStore.for_scope(scope).snapshot(folder, "before file changes"), Snapshot
    )


def test_list_diff_restore_and_file(
    ctx: Ctx, scope: Scope, folder: Path, fake_clock: FakeClock
) -> None:
    dispatch("/rollback", ctx)
    assert ctx.lines[0].startswith(f"No checkpoints for {folder}.")
    snap(scope, folder, fake_clock)
    write(folder, "a.txt", "v2\n")
    write(folder, "b.txt", "new\n")
    ctx.lines.clear()
    dispatch("/rollback", ctx)
    assert ctx.lines[0] == f"Checkpoints for {folder} (newest first):"
    assert "before file changes  (1 file)" in ctx.lines[1] and ctx.lines[1].lstrip().startswith(
        "1."
    )
    ctx.lines.clear()
    dispatch("/rollback diff 1", ctx)
    assert "+v2" in ctx.text and "-v1" in ctx.text
    ctx.lines.clear()
    dispatch("/rollback 1 a.txt", ctx)
    assert len(ctx.lines) == 1 and ctx.lines[0].startswith("Restored a.txt from checkpoint 1 (")
    assert (folder / "a.txt").read_text() == "v1\n" and (folder / "b.txt").exists()
    ctx.lines.clear()
    dispatch("/rollback 2", ctx)  # 1 is now the safety checkpoint taken by the file restore
    assert ctx.lines[0].startswith(f"Restored {folder} to checkpoint 2")
    assert "Removed files created since: b.txt" in ctx.text
    assert not (folder / "b.txt").exists()
    ctx.lines.clear()
    dispatch("/rollback --all", ctx)
    assert ctx.lines[0] == "Checkpoints of every folder (newest first):"
    ctx.lines.clear()
    dispatch("/rollback 9", ctx)
    assert ctx.lines[0].startswith("choose a checkpoint from 1 to")
    ctx.lines.clear()
    dispatch("/rollback please", ctx)
    assert ctx.lines[0].startswith("Usage: /rollback")


def test_restore_asks_when_configured(scope: Scope, folder: Path, fake_clock: FakeClock) -> None:
    asking = scope.with_config(DictConfig({"approvals": {"destructive_slash_confirm": True}}))
    snap(asking, folder, fake_clock)
    write(folder, "a.txt", "v2\n")
    ctx = make_ctx(asking, session_cwd=lambda: str(folder), confirm=lambda _q: False)
    dispatch("/rollback 1", ctx)
    assert ctx.lines == ["Nothing was restored."]
    assert (folder / "a.txt").read_text() == "v2\n"


def test_other_backends_refuse(scope: Scope, folder: Path) -> None:
    remote = scope.with_config(DictConfig({"terminal": {"backend": "docker"}}))
    ctx = make_ctx(remote, session_cwd=lambda: str(folder))
    dispatch("/rollback", ctx)
    assert ctx.lines == ["Checkpoints are taken only on the local backend (this one is docker)."]


def test_checkpoints_on_off(ctx: Ctx, scope: Scope) -> None:
    dispatch("/checkpoints", ctx)
    assert ctx.lines == ["Checkpoints are off for this session (/checkpoints on|off)."]
    dispatch("/checkpoints on", ctx)
    assert TRIGGER.enabled(scope, ctx.session_id)
    assert not TRIGGER.enabled(scope, "another-session")
    dispatch("/checkpoints off", ctx)
    assert not TRIGGER.enabled(scope, ctx.session_id)
    dispatch("/checkpoints maybe", ctx)
    assert ctx.lines[-1] == "Usage: /checkpoints [on|off|status]"
