"""P14-19: `indus cron doctor` findings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from indus.cron import ports
from indus.cron.doctor import findings_for, is_overdue, run_doctor
from indus.cron.grammar import IntervalSchedule
from indus.cron.models import CronJob
from indus.cron.ports import StoreHealthView
from indus.foundation.scope import Scope
from indus.service.cron_host import CronHost
from tests.unit.cron.world import make_world

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
EVERY_30M = IntervalSchedule(minutes=30, display="every 30m")


def stamp(lateness: str, seconds: float) -> dict[str, Any]:
    slot = NOW - timedelta(seconds=seconds)
    return {
        "scheduled_for": slot,
        "dispatched_at": NOW,
        "lateness_s": seconds,
        "lateness": lateness,
    }


def job(**fields: Any) -> CronJob:
    fields.setdefault("next_run_at", ports.iso(NOW + timedelta(minutes=5)))
    fields.setdefault("prompt", "check the build")
    return CronJob(
        id=fields.pop("id", "aaaaaaaaaaaa"),
        created_at=NOW - timedelta(days=1),
        schedule=EVERY_30M,
        name="j",
        **fields,
    )


def check(scripts: Path, **fields: Any) -> list[str]:
    return findings_for(job(**fields), now=NOW, scripts=scripts)


def test_healthy_job_has_no_findings(tmp_path: Path) -> None:
    assert check(tmp_path, last_status="ok") == []
    assert check(tmp_path, last_status="silent") == []


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"last_status": "error", "last_error": "model said no"}, "last run failed"),
        ({"last_status": "blocked_config", "last_error": "no key"}, "last run failed"),
        ({"last_delivery_error": "telegram: 403"}, "not delivered"),
        ({"last_delivery_unverified": ["slack:C1"]}, "not confirmed"),
        ({"last_dispatch": stamp("catch_up", 7200)}, "catch-up"),
        ({"last_dispatch": stamp("late", 400)}, "late"),
        ({"next_run_at": None}, "no next run"),
        ({"next_run_at": ports.iso(NOW - timedelta(minutes=16))}, "16m overdue"),
        ({"no_agent": True}, "script-only job has no script"),
        ({"script": "gone.sh"}, "missing"),
        ({"script": "../outside.sh"}, "outside"),
        ({"workdir": "/no/such/folder/anywhere"}, "no longer exists"),
    ],
)
def test_findings(tmp_path: Path, fields: dict[str, Any], expected: str) -> None:
    found = check(tmp_path, **fields)
    assert any(expected in f for f in found), found


def test_overdue_needs_more_than_fifteen_minutes(tmp_path: Path) -> None:
    assert check(tmp_path, next_run_at=ports.iso(NOW - timedelta(minutes=14))) == []
    assert not is_overdue(job(next_run_at=ports.iso(NOW - timedelta(minutes=14))), NOW)
    assert is_overdue(job(next_run_at=ports.iso(NOW - timedelta(minutes=16))), NOW)
    paused = job(next_run_at=ports.iso(NOW - timedelta(hours=3)), state="paused", enabled=False)
    assert not is_overdue(paused, NOW)


def test_script_inside_the_folder_is_fine(tmp_path: Path) -> None:
    (tmp_path / "ok.sh").write_text("echo", encoding="utf-8")
    assert check(tmp_path, script="ok.sh", no_agent=True) == []


def test_run_doctor_skips_inactive_jobs_and_reports_the_store(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = make_world(scope)
    world.add(name="paused", state="paused", enabled=False, workdir="/gone")
    world.add(name="fine", next_run_at=scope.clock.now() + timedelta(minutes=1))
    backend = CronHost(scope)
    report = run_doctor(backend, scope)
    assert report.ok and report.checked == 1
    monkeypatch.setattr(
        backend, "store_health", lambda: StoreHealthView(False, "No space left on device")
    )
    assert not run_doctor(backend, scope).ok
