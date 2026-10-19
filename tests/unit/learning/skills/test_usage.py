"""Skill usage telemetry and write provenance (P09-12, R07.6)."""

from __future__ import annotations

import json
import logging
import multiprocessing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from indus.learning.pending import Origin, bind_origin, unattended_review
from indus.learning.skills.usage import (
    BACKGROUND_REVIEW,
    LEARN,
    SkillUsage,
    creation_origin,
)
from tests.fakes.clock import FakeClock


@pytest.fixture
def usage(tmp_path: Path, fake_clock: FakeClock) -> SkillUsage:
    return SkillUsage(tmp_path / "skills", clock=fake_clock)


def test_counters_and_times(usage: SkillUsage, fake_clock: FakeClock) -> None:
    usage.bump_use("demo")
    fake_clock.advance(60)
    usage.bump_view("demo")
    usage.bump_view("demo")
    fake_clock.advance(60)
    rec = usage.bump_patch("demo")
    assert rec is not None
    assert (rec["use_count"], rec["view_count"], rec["patch_count"]) == (1, 2, 1)
    assert rec["last_used_at"] == "2026-01-01T09:00:00+00:00"
    assert rec["last_viewed_at"] == "2026-01-01T09:01:00+00:00"
    assert rec["last_patched_at"] == rec["last_activity_at"] == "2026-01-01T09:02:00+00:00"
    assert rec["state"] == "active" and rec["pinned"] is False and rec["origin"] is None
    on_disk = json.loads(usage.path.read_text())
    assert on_disk["demo"] == rec
    assert set(rec) == {
        "use_count",
        "view_count",
        "patch_count",
        "last_used_at",
        "last_viewed_at",
        "last_patched_at",
        "created_at",
        "state",
        "pinned",
        "archived_at",
        "origin",
        "last_activity_at",
    }


def test_state_pin_and_origin_setters(usage: SkillUsage, fake_clock: FakeClock) -> None:
    usage.set_pinned("demo", True)
    assert usage.is_pinned("demo")
    rec = usage.set_state("demo", "archived")
    assert rec is not None and rec["archived_at"] == "2026-01-01T09:00:00+00:00"
    rec = usage.set_state("demo", "active")
    assert rec is not None and rec["archived_at"] is None
    with pytest.raises(ValueError):
        usage.set_state("demo", "gone")
    usage.set_origin("demo", "agent")
    assert usage.is_curator_managed("demo")
    with pytest.raises(ValueError):
        usage.set_origin("demo", "robot")


@pytest.mark.parametrize(
    ("origin", "recorded"),
    [("foreground", "learn"), (LEARN, "learn"), (BACKGROUND_REVIEW, "agent")],
)
def test_origin_per_context(usage: SkillUsage, origin: Origin, recorded: str) -> None:
    with bind_origin(origin, attended=False):
        rec = usage.record_created("demo")
    assert rec is not None and rec["origin"] == recorded
    assert usage.is_curator_managed("demo") is (recorded == "agent")


def test_default_origin_and_attended_flag() -> None:
    """One write-origin context for memory and skills: ``learning.pending`` (integration)."""
    assert creation_origin() == "learn"
    assert not unattended_review()
    with bind_origin(BACKGROUND_REVIEW, attended=False):
        assert creation_origin() == "agent"
        assert unattended_review()
        with bind_origin(BACKGROUND_REVIEW, attended=True):
            assert creation_origin() == "agent"
            assert not unattended_review()


def test_bundled_and_hub_not_written(usage: SkillUsage) -> None:
    usage.root.mkdir(parents=True)
    (usage.root / ".bundled_manifest").write_text("git-workflow:abc123\n")
    (usage.root / ".hub").mkdir()
    (usage.root / ".hub" / "lock.json").write_text(json.dumps({"installed": {"hubbed": {}}}))
    assert usage.bump_use("git-workflow") is None
    assert usage.bump_view("hubbed") is None
    assert not usage.path.exists()
    assert usage.get("git-workflow")["use_count"] == 0
    assert usage.bump_use("mine") is not None


def test_corrupt_sidecar_rebuilt_empty(usage: SkillUsage, caplog: pytest.LogCaptureFixture) -> None:
    usage.root.mkdir(parents=True)
    usage.path.write_text("{not json")
    with caplog.at_level(logging.WARNING):
        assert usage.load() == {}
        rec = usage.bump_use("demo")
    assert rec is not None and rec["use_count"] == 1
    assert "damaged" in caplog.text
    assert (usage.root / ".usage.json.corrupt").read_text() == "{not json"
    assert json.loads(usage.path.read_text())["demo"]["use_count"] == 1


def test_bad_field_types_are_backfilled(usage: SkillUsage) -> None:
    usage.root.mkdir(parents=True)
    usage.path.write_text(
        json.dumps({"demo": {"use_count": -3, "state": "weird", "pinned": "yes"}})
    )
    rec = usage.get("demo")
    assert rec["use_count"] == 0 and rec["state"] == "active" and rec["pinned"] is False


def test_write_failure_never_raises(usage: SkillUsage, caplog: pytest.LogCaptureFixture) -> None:
    usage.root.parent.mkdir(parents=True, exist_ok=True)
    usage.root.write_text("a file where the folder should be")
    with caplog.at_level(logging.WARNING):
        assert usage.bump_use("demo") is None
    assert "not recorded" in caplog.text


def test_forget(usage: SkillUsage) -> None:
    usage.bump_use("demo")
    usage.forget("demo")
    assert usage.load() == {}


def _bump_many(root: str, times: int) -> None:
    worker = SkillUsage(Path(root))
    for _ in range(times):
        assert worker.bump_use("shared") is not None


def test_two_processes_lose_no_increment(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_bump_many, args=(str(root), 40)) for _ in range(2)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=120)
        assert proc.exitcode == 0
    assert SkillUsage(root).get("shared")["use_count"] == 80


def test_clock_is_utc_text(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 3, 1, tzinfo=UTC))
    clock.advance(timedelta(hours=1).total_seconds())
    rec = SkillUsage(tmp_path, clock=clock).bump_view("x")
    assert rec is not None and rec["last_viewed_at"] == "2026-03-01T01:00:00+00:00"
