"""`indus curator` verbs and `/curator` (P09-20)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from indus.cli.commands import curator as curator_cmd
from indus.cli.slash.learning_cmds import curator_cmd as slash_curator
from indus.foundation.scope import Scope
from indus.learning.curator.archive import archive_skill, list_archived
from indus.learning.curator.backup import list_backups, snapshot
from indus.learning.curator.state import CuratorState
from indus.providers.types import Message, NormalizedResponse
from tests.fakes.clock import FakeClock
from tests.unit.cli.commands.cmdrun import run_cli
from tests.unit.cli.slash._fakes import make_ctx
from tests.unit.learning.curator._helpers_f import (
    DAY,
    add_skill,
    clean_registries,  # noqa: F401  (autouse fixture)
    ledger_rows,
    mark_bundled,
    mark_hub,
    record,
    skills_root,
    with_settings,
)


def cli(capsys: pytest.CaptureFixture[str], scope: Scope, *argv: str, tty: bool = False) -> Any:
    return run_cli(capsys, scope, "curator", *argv, tty=tty)


def test_status(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    add_skill(scope, "managed-a", created_days=5, active_days=3, use_count=2)
    add_skill(scope, "managed-b", created_days=5, pinned=True, state="stale")
    add_skill(scope, "taught", origin="learn")
    add_skill(scope, "legacy", origin=None)
    ran = cli(capsys, scope, "status")
    assert ran.code == 0
    out = ran.out
    assert "curator: on" in out and "runs every:    7 d (after 2 h idle)" in out
    assert "managed skills: 2 (active 1, stale 1); archived: 0" in out
    assert "pinned (1): managed-b" in out
    assert "least recently used:" in out and "managed-a" in out
    assert "taught                           created in the foreground" in out
    assert "legacy                           created before tracking" in out


def test_run_dry_and_real(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    folder = add_skill(scope, "old", created_days=60, active_days=60, use_count=1)
    ran = cli(capsys, scope, "run", "--dry-run")
    assert ran.code == 0 and "would change: stale 0, archived 1" in ran.out
    assert folder.exists()
    ran = cli(capsys, scope, "run")
    assert ran.code == 0 and "changed: stale 0, archived 1" in ran.out
    assert not folder.exists() and "report in" in ran.out


def test_run_disabled(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = cli(capsys, with_settings(scope, curator={"enabled": False}), "run")
    assert ran.code == 1 and "switched off" in ran.out


def test_run_consolidate_uses_step(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Step:
        model, provider = "fake-model", "fake"

        async def __call__(
            self, messages: Sequence[Message], tools: Sequence[Mapping[str, Any]]
        ) -> NormalizedResponse:
            return NormalizedResponse(content="nothing to merge")

    monkeypatch.setattr(curator_cmd, "STEP_FACTORY", lambda _scope: Step())
    add_skill(scope, "alpha")
    ran = cli(capsys, scope, "run", "--consolidate")
    assert ran.code == 0 and "consolidation: nothing to merge" in ran.out
    assert len(list_backups(scope)) == 1


def test_pause_resume(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    assert cli(capsys, scope, "pause").out.strip() == "curator: paused"
    assert CuratorState(scope).paused is True
    assert "curator: paused" in cli(capsys, scope, "status").out
    assert cli(capsys, scope, "resume").out.strip() == "curator: resumed"
    assert CuratorState(scope).paused is False


def test_pin_unpin(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    add_skill(scope, "managed")
    assert cli(capsys, scope, "pin", "managed").code == 0
    assert record(scope, "managed")["pinned"] is True
    assert cli(capsys, scope, "unpin", "managed").code == 0
    assert record(scope, "managed")["pinned"] is False


def test_pin_refuses_bundled(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    add_skill(scope, "shipped", origin=None, record=False)
    mark_bundled(scope, "shipped")
    add_skill(scope, "from-hub", origin=None, record=False)
    mark_hub(scope, "from-hub")
    add_skill(scope, "taught", origin="learn")
    bundled = cli(capsys, scope, "pin", "shipped")
    assert bundled.code == 1 and "ships with Indus Agent" in bundled.err
    assert cli(capsys, scope, "pin", "from-hub").code == 1
    unmanaged = cli(capsys, scope, "pin", "taught")
    assert unmanaged.code == 1 and "adopt taught" in unmanaged.err


def test_adopt(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    add_skill(scope, "taught", origin="learn", created_days=20, active_days=9, use_count=1)
    before = record(scope, "taught")
    ran = cli(capsys, scope, "adopt", "taught")
    assert ran.code == 0 and "adopted taught" in ran.out
    after = record(scope, "taught")
    assert after["origin"] == "agent"
    assert after["last_activity_at"] == before["last_activity_at"]  # the clock is kept
    assert "already managed" in cli(capsys, scope, "adopt", "taught").out


def test_adopt_refusals(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    add_skill(scope, "shipped", origin=None, record=False)
    mark_bundled(scope, "shipped")
    ran = cli(capsys, scope, "adopt", "shipped")
    assert ran.code == 1 and "ships with Indus Agent" in ran.err
    assert record(scope, "shipped")["origin"] is None
    assert cli(capsys, scope, "adopt", "ghost").code == 1


def test_list_unmanaged(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    add_skill(scope, "taught", origin="learn")
    out = cli(capsys, scope, "list-unmanaged").out
    assert "taught" in out and "created in the foreground" in out


def test_archive_restore_list(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    add_skill(scope, "x")
    add_skill(scope, "pinned-one", pinned=True)
    assert cli(capsys, scope, "archive", "x").code == 0
    assert "x" in cli(capsys, scope, "list-archived").out
    refused = cli(capsys, scope, "archive", "pinned-one")
    assert refused.code == 1 and "unpin pinned-one" in refused.err
    assert cli(capsys, scope, "restore", "x").code == 0
    assert (skills_root(scope) / "x" / "SKILL.md").is_file()
    assert "archive is empty" in cli(capsys, scope, "list-archived").out


def test_restore_blocked_by_bundled(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    add_skill(scope, "git-workflow")
    archive_skill(scope, "git-workflow")
    add_skill(scope, "git-workflow", origin=None, record=False)
    mark_bundled(scope, "git-workflow")
    ran = cli(capsys, scope, "restore", "git-workflow")
    assert ran.code == 1 and "bundled skill now owns" in ran.err
    assert len(list_archived(scope)) == 1


def test_prune(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    old = add_skill(scope, "old", created_days=20, active_days=12, use_count=1)
    keep = add_skill(scope, "kept", created_days=20, active_days=12, use_count=1, pinned=True)
    young = add_skill(scope, "young", created_days=2, active_days=1, use_count=1)
    assert cli(capsys, scope, "prune", "--days", "0").code == 2
    dry = cli(capsys, scope, "prune", "--days", "10", "--dry-run")
    assert dry.code == 0 and "old" in dry.out and old.exists()
    assert cli(capsys, scope, "prune", "--days", "10").code == 2  # no terminal, no -y
    ran = cli(capsys, scope, "prune", "--days", "10", "-y")
    assert ran.code == 0 and "archived 1 of 1" in ran.out
    assert not old.exists() and keep.exists() and young.exists()


def test_purge_ttl_zero_noop(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    add_skill(scope, "x")
    archive_skill(scope, "x")
    ran = cli(capsys, scope, "purge", "-y")
    assert ran.code == 0 and "purge is off" in ran.out
    assert len(list_archived(scope)) == 1


def test_purge_with_ttl(
    capsys: pytest.CaptureFixture[str], scope: Scope, fake_clock: FakeClock
) -> None:
    add_skill(scope, "first")
    archive_skill(scope, "first")
    fake_clock.advance(10 * DAY)
    add_skill(scope, "second")
    archive_skill(scope, "second")
    fake_clock.advance(1 * DAY)
    timed = with_settings(scope, curator={"archive_ttl_days": 5})
    assert cli(capsys, timed, "purge").code == 2
    ran = cli(capsys, timed, "purge", "-y")
    assert ran.code == 0 and "deleted 1 archived skill(s)" in ran.out
    assert [a.name for a in list_archived(scope)] == ["second"]
    last = ledger_rows(scope)[-1]
    assert last["action"] == "purge" and last["skill"] == "first" and last["actor"] == "user"


def test_backup_and_rollback(
    capsys: pytest.CaptureFixture[str], scope: Scope, fake_clock: FakeClock
) -> None:
    add_skill(scope, "alpha", body="v1\n")
    ran = cli(capsys, scope, "backup")
    assert ran.code == 0 and "snapshot 2026-01-01T09-00-00Z" in ran.out
    fake_clock.advance(60)
    (skills_root(scope) / "alpha" / "SKILL.md").write_text("changed", encoding="utf-8")
    listing = cli(capsys, scope, "rollback", "--list")
    assert "2026-01-01T09-00-00Z" in listing.out and "manual" in listing.out
    ran = cli(capsys, scope, "rollback", "-y")
    assert ran.code == 0 and "restored from 2026-01-01T09-00-00Z" in ran.out
    assert "v1" in (skills_root(scope) / "alpha" / "SKILL.md").read_text()


def test_rollback_non_tty_needs_yes(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    add_skill(scope, "alpha")
    snapshot(scope)
    ran = cli(capsys, scope, "rollback")
    assert ran.code == 2 and "pass -y" in ran.err


def test_rollback_refusals(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    add_skill(scope, "alpha")
    none = cli(capsys, scope, "rollback", "-y")
    assert none.code == 1 and "no snapshots yet" in none.err
    off = with_settings(scope, curator={"backup": {"enabled": False}})
    assert cli(capsys, off, "backup").code == 1
    disabled = cli(capsys, off, "rollback", "-y")
    assert disabled.code == 1 and "switched off" in disabled.err


def test_ledger(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    assert "the ledger is empty" in cli(capsys, scope, "ledger").out
    add_skill(scope, "x")
    archive_skill(scope, "x", actor="user")
    rows = cli(capsys, scope, "ledger", "--skill", "x").out
    entry_id = ledger_rows(scope)[0]["id"]
    assert entry_id in rows and "archive" in rows
    assert cli(capsys, scope, "ledger", "--rollback", entry_id).code == 2
    ran = cli(capsys, scope, "ledger", "--rollback", entry_id, "-y")
    assert ran.code == 0 and (skills_root(scope) / "x" / "SKILL.md").is_file()
    assert cli(capsys, scope, "ledger", "--rollback", "nope", "-y").code == 1


def test_slash_curator(scope: Scope) -> None:
    ctx = make_ctx(scope)
    slash_curator(ctx, "")
    assert ctx.lines[0] == "curator: on"
    slash_curator(ctx, "pause")
    assert CuratorState(scope).paused
    slash_curator(ctx, "resume")
    slash_curator(ctx, "bogus")
    assert ctx.lines[-2].startswith("Usage: /curator")
    slash_curator(ctx, "run --wat")
    assert ctx.lines[-1].startswith("Usage: /curator")


def test_status_json_free_of_secrets(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    # status reads only the state file and usage sidecar; a damaged state file still prints
    path = skills_root(scope) / ".curator_state"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"paused": True, "run_count": 3}), encoding="utf-8")
    out = cli(capsys, scope, "status").out
    assert "curator: paused" in out and "runs:          3" in out
