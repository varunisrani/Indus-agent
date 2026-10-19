"""Curator phase 1: when it runs and how it ages managed skills (P09-18, R07.6-R07.8)."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.learning import curator
from indus.learning.curator import ledger_port
from indus.learning.curator.lifecycle import (
    FIRST_LOOK,
    apply_transitions,
    maybe_run,
    run_curator,
    should_run,
)
from indus.learning.curator.settings import CuratorSettings
from indus.learning.curator.state import CuratorState
from tests.fakes.clock import FakeClock
from tests.fakes.skill_refs import FakeReferenceSource
from tests.unit.learning.curator._helpers_f import (
    DAY,
    add_skill,
    clean_registries,  # noqa: F401  (autouse fixture)
    ledger_rows,
    mark_bundled,
    record,
    skills_root,
    with_settings,
)

HOUR = 3600.0


# -- when ---------------------------------------------------------------------------------------


def test_fresh_install_seeds_only(scope: Scope, fake_clock: FakeClock) -> None:
    assert should_run(scope, idle_s=10 * HOUR) is False
    state = CuratorState(scope).load()
    assert state["last_run_at"] == fake_clock.now().isoformat()
    assert state["last_summary"] == FIRST_LOOK
    assert state["run_count"] == 0
    fake_clock.advance(169 * HOUR)
    assert should_run(scope, idle_s=3 * HOUR) is True
    assert should_run(scope, idle_s=1 * HOUR) is False
    assert should_run(scope) is True  # idle not measured: not a gate


def test_interval_not_elapsed(scope: Scope, fake_clock: FakeClock) -> None:
    should_run(scope)
    fake_clock.advance(167 * HOUR)
    assert should_run(scope, idle_s=10 * HOUR) is False


def test_paused_and_disabled(scope: Scope, fake_clock: FakeClock) -> None:
    should_run(scope)
    fake_clock.advance(200 * HOUR)
    CuratorState(scope).set_paused(True)
    assert should_run(scope) is False
    CuratorState(scope).set_paused(False)
    assert should_run(scope) is True
    off = with_settings(scope, curator={"enabled": False})
    assert should_run(off) is False


def test_bad_numbers_fall_back(scope: Scope, caplog: pytest.LogCaptureFixture) -> None:
    odd = with_settings(
        scope, curator={"interval_hours": 0, "stale_after_days": -3, "archive_after_days": "x"}
    )
    with caplog.at_level(logging.WARNING):
        config = CuratorSettings.from_scope(odd)
    assert (config.interval_hours, config.stale_after_days, config.archive_after_days) == (
        168,
        14,
        30,
    )
    assert "curator.interval_hours must be 1 or more" in caplog.text


def test_defaults_follow_code() -> None:
    config = CuratorSettings()
    assert (config.interval_hours, config.min_idle_hours) == (168, 2.0)
    assert (config.stale_after_days, config.archive_after_days) == (14, 30)
    assert config.consolidate is False and config.backup_keep == 2


# -- transitions ----------------------------------------------------------------------------------


def test_stale_then_archived(scope: Scope, fake_clock: FakeClock) -> None:
    folder = add_skill(scope, "old-trick", created_days=40, active_days=15, use_count=2)
    out = apply_transitions(scope)
    assert out.marked_stale == 1 and record(scope, "old-trick")["state"] == "stale"
    fake_clock.advance(16 * DAY)  # 31 days idle now
    out = apply_transitions(scope)
    assert out.archived == 1
    assert not folder.exists()
    assert (skills_root(scope) / ".archive" / "old-trick" / "SKILL.md").is_file()
    rec = record(scope, "old-trick")
    assert rec["state"] == "archived" and rec["archived_at"] is not None


def test_hand_written_and_learn_untouched(scope: Scope) -> None:
    learn = add_skill(scope, "taught", origin="learn", created_days=400, active_days=400)
    hand = add_skill(scope, "mine", origin=None, created_days=400, active_days=400)
    bare = add_skill(scope, "untracked", record=False)
    out = apply_transitions(scope)
    assert out.checked == 0
    assert learn.exists() and hand.exists() and bare.exists()
    assert record(scope, "taught")["state"] == "active"


def test_bundled_untouched_unless_prune_builtins(scope: Scope, fake_clock: FakeClock) -> None:
    folder = add_skill(scope, "shipped", origin=None, record=False)
    mark_bundled(scope, "shipped")
    assert apply_transitions(scope).checked == 0
    pruning = with_settings(scope, curator={"prune_builtins": True})
    first = apply_transitions(pruning)
    assert first.seeded == 1 and folder.exists()  # the clock starts at first sight
    fake_clock.advance(31 * DAY)
    assert apply_transitions(pruning).archived == 1
    assert not folder.exists()


def test_pinned_skipped(scope: Scope) -> None:
    folder = add_skill(scope, "keeper", created_days=90, active_days=90, use_count=1, pinned=True)
    out = apply_transitions(scope)
    assert out.pinned == 1 and out.archived == 0 and folder.exists()


def test_reference_source_protects(scope: Scope) -> None:
    folder = add_skill(scope, "nightly-report", created_days=90, active_days=90, use_count=1)
    curator.register_reference_source(FakeReferenceSource(["nightly-report"]))
    out = apply_transitions(scope)
    assert out.referenced == 1 and folder.exists()
    assert record(scope, "nightly-report")["state"] == "active"


def test_failing_source_blocks_archive(scope: Scope) -> None:
    folder = add_skill(scope, "maybe-used", created_days=90, active_days=90, use_count=1)
    curator.register_reference_source(FakeReferenceSource(fail=True))
    out = apply_transitions(scope)
    assert out.protection_unknown is True and out.archived == 0
    assert folder.exists()
    assert record(scope, "maybe-used")["state"] == "stale"  # staleness still recorded


def test_never_used_young_skill_kept(scope: Scope) -> None:
    folder = add_skill(scope, "fresh", created_days=10, active_days=None, use_count=0)
    out = apply_transitions(scope)
    assert out.archived == 0 and out.marked_stale == 0 and folder.exists()


def test_never_used_old_skill_archived(scope: Scope) -> None:
    folder = add_skill(scope, "forgotten", created_days=31, active_days=None, use_count=0)
    assert apply_transitions(scope).archived == 1
    assert not folder.exists()


def test_reactivated_after_use(scope: Scope) -> None:
    add_skill(scope, "back", created_days=60, active_days=2, use_count=3, state="stale")
    out = apply_transitions(scope)
    assert out.reactivated == 1 and record(scope, "back")["state"] == "active"


def test_no_dates_seeded(scope: Scope, fake_clock: FakeClock) -> None:
    add_skill(scope, "undated", created_days=None, active_days=None)
    out = apply_transitions(scope)
    assert out.seeded == 1
    assert record(scope, "undated")["created_at"] == fake_clock.now().isoformat()


def test_dry_run_changes_nothing(scope: Scope) -> None:
    folder = add_skill(scope, "old", created_days=60, active_days=60, use_count=1)
    out = apply_transitions(scope, dry_run=True)
    assert out.archived == 1 and folder.exists()
    assert record(scope, "old")["state"] == "active"


def test_archive_is_ledgered(scope: Scope) -> None:
    add_skill(scope, "old", created_days=60, active_days=60, use_count=1)
    apply_transitions(scope)
    assert [(r["action"], r["actor"], r["skill"]) for r in ledger_rows(scope)] == [
        ("archive", "curator", "old")
    ]


def test_ledger_failure_never_blocks(scope: Scope) -> None:
    class Broken(ledger_port.SkillLedgerPort):
        def capture(self, skill_dir: Path, *, skill: str) -> object:
            raise OSError("disk full")

        def record(self, *args: object, **kwargs: object) -> str | None:
            raise OSError("disk full")

    ledger_port.set_ledger(Broken(scope))
    folder = add_skill(scope, "old", created_days=60, active_days=60, use_count=1)
    assert apply_transitions(scope).archived == 1 and not folder.exists()


# -- whole runs -----------------------------------------------------------------------------------


def test_run_writes_state_and_report(scope: Scope, fake_clock: FakeClock) -> None:
    add_skill(scope, "old", created_days=60, active_days=60, use_count=1)
    outcome = run_curator(scope)
    state = CuratorState(scope).load()
    assert state["run_count"] == 1 and state["last_run_at"] == fake_clock.now().isoformat()
    assert outcome.report is not None and state["last_report"] == outcome.report
    run = json.loads((scope.home / "logs" / "curator").glob("*/run.json").__next__().read_text())
    assert run["transitions"]["archived"] == 1
    assert run["state_changes"] == [{"name": "old", "from": "active", "to": "archived"}]


def test_report_without_candidates(scope: Scope) -> None:
    outcome = run_curator(scope)
    assert outcome.report is not None
    folder = scope.home / "logs" / "curator"
    [run_dir] = list(folder.iterdir())
    report = (run_dir / "REPORT.md").read_text()
    assert "(not resolved)" in report and "duration: 0 s" in report
    payload = json.loads((run_dir / "run.json").read_text())
    assert payload["duration_s"] == 0 and payload["consolidation"]["ran"] is False


def test_two_runs_same_second_get_two_folders(scope: Scope) -> None:
    run_curator(scope)
    run_curator(scope)
    assert len(list((scope.home / "logs" / "curator").iterdir())) == 2


def test_dry_run_keeps_last_run(scope: Scope) -> None:
    add_skill(scope, "old", created_days=60, active_days=60, use_count=1)
    run_curator(scope, dry_run=True)
    assert CuratorState(scope).load()["last_run_at"] is None
    assert (skills_root(scope) / "old").exists()


def test_maybe_run_claims_and_releases(scope: Scope, fake_clock: FakeClock) -> None:
    assert maybe_run(scope) is None  # the first look
    fake_clock.advance(169 * HOUR)
    add_skill(scope, "old", created_days=60, active_days=60, use_count=1)
    outcome = maybe_run(scope, idle_s=3 * HOUR)
    assert outcome is not None and outcome.transitions.archived == 1
    assert CuratorState(scope).load()["claim"] is None
    assert maybe_run(scope, idle_s=3 * HOUR) is None  # the interval starts again


def test_gateway_running_leaves_it(scope: Scope, fake_clock: FakeClock) -> None:
    should_run(scope)
    fake_clock.advance(169 * HOUR)
    assert maybe_run(scope, gateway_running=lambda: True) is None


def test_live_claim_blocks_stale_claim_reclaimed(scope: Scope, fake_clock: FakeClock) -> None:
    state = CuratorState(scope)
    assert state.claim(fake_clock.now()) is True
    should_run(scope)
    fake_clock.advance(169 * HOUR)
    state.update(claim={"pid": 1, "at": (fake_clock.now() - timedelta(seconds=10)).isoformat()})
    assert maybe_run(scope) is None
    state.update(claim={"pid": 1, "at": (fake_clock.now() - timedelta(seconds=3601)).isoformat()})
    assert maybe_run(scope) is not None


def test_damaged_state_file_reads_empty(scope: Scope) -> None:
    path = skills_root(scope) / ".curator_state"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert should_run(scope) is False  # seeds over the damaged file
    assert CuratorState(scope).load()["last_run_at"] is not None
