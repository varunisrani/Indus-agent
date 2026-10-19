"""Store health (P14-05): an unwritable store fires nothing, re-probes each minute, notices once."""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Iterator
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from indus.cron import store_health
from indus.cron.due import collect_due, evaluate_all
from indus.cron.ledger import ExecutionLedger, ExecutionsDB
from indus.cron.models import JobSpec
from indus.cron.store import CronStore
from indus.cron.store_health import (
    PROBE_INTERVAL_S,
    HealthEvent,
    StoreHealth,
    probe_dir,
    probe_report,
)
from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock
from tests.fakes.pids import FakePids


class SwitchProbe:
    """A probe whose answer the test flips; counts calls."""

    def __init__(self) -> None:
        self.error: OSError | None = None
        self.calls = 0

    def __call__(self, cron_dir: Path) -> OSError | None:
        self.calls += 1
        return self.error

    def fail(self, code: int = errno.ENOSPC) -> None:
        self.error = OSError(code, os.strerror(code))

    def heal(self) -> None:
        self.error = None


@pytest.fixture
def cron_scope(scope: Scope) -> Scope:
    return scope.with_config(DictConfig({"timezone": "UTC"}))


@pytest.fixture
def probe() -> SwitchProbe:
    return SwitchProbe()


@pytest.fixture
def events() -> list[HealthEvent]:
    return []


@pytest.fixture
def health(
    cron_scope: Scope, fake_clock: FakeClock, probe: SwitchProbe, events: list[HealthEvent]
) -> StoreHealth:
    return StoreHealth(
        cron_scope.home / "cron", clock=fake_clock, notify=events.append, probe=probe
    )


@pytest.fixture
def ledger(cron_scope: Scope) -> Iterator[ExecutionLedger]:
    db = ExecutionsDB(cron_scope)
    yield ExecutionLedger(db, procs=FakePids())
    db.close()


def tick(
    store: CronStore, health: StoreHealth, ledger: ExecutionLedger, now_clock: FakeClock
) -> list[str]:
    """A minimal tick: gate, due pass, claim + write updates for each firing job."""
    if not health.gate():
        return []
    now = now_clock.now()
    decisions = evaluate_all(
        store.load(), now=now, tz=UTC, slots=ledger, outage_since=health.outage_since
    )
    fired: list[str] = []
    for decision in decisions:
        if decision.updates and not decision.fire:
            store.patch(decision.job_id, **dict(decision.updates))
    for decision in collect_due(decisions):
        attempt = ledger.claim(decision.job_id, scheduled_for=decision.scheduled_for)
        store.patch(decision.job_id, **dict(decision.updates))
        ledger.finish(attempt.attempt_id, ok=True)
        fired.append(decision.job_id)
    return fired


def test_enospc_blocks_every_fire(
    cron_scope: Scope,
    health: StoreHealth,
    probe: SwitchProbe,
    ledger: ExecutionLedger,
    fake_clock: FakeClock,
    events: list[HealthEvent],
) -> None:
    store = CronStore(cron_scope)
    store.create(JobSpec(schedule="every 1m", prompt="ping"))
    probe.fail(errno.ENOSPC)
    fired: list[str] = []
    for _ in range(10):
        fake_clock.advance(60)
        fired += tick(store, health, ledger, fake_clock)
    assert fired == []
    assert ledger.recent() == []
    assert [e.kind for e in events] == ["unwritable"]
    condition = health.condition
    assert condition is not None and "ENOSPC" in condition.error
    assert str(cron_scope.home / "cron") in condition.fix


def test_reprobe_at_most_once_a_minute(
    health: StoreHealth, probe: SwitchProbe, fake_clock: FakeClock
) -> None:
    probe.fail(errno.EROFS)
    assert health.gate() is False and probe.calls == 1
    for _ in range(5):
        fake_clock.advance(10)
        assert health.gate() is False
    assert probe.calls == 1  # 50 s: still throttled
    fake_clock.advance(PROBE_INTERVAL_S - 50)
    assert health.gate() is False and probe.calls == 2
    probe.heal()
    fake_clock.advance(30)
    assert health.gate() is False and probe.calls == 2  # not re-probed yet
    fake_clock.advance(30)
    assert health.gate() is True and probe.calls == 3
    assert health.gate() is True and probe.calls == 4  # writable: probe before every fire


def test_recovery_fires_each_due_job_once(
    cron_scope: Scope,
    health: StoreHealth,
    probe: SwitchProbe,
    ledger: ExecutionLedger,
    fake_clock: FakeClock,
) -> None:
    store = CronStore(cron_scope)
    hourly = store.create(JobSpec(schedule="every 1h", prompt="hourly"))
    daily = store.create(JobSpec(schedule="daily at 10:00", prompt="daily"))
    oneshot = store.create(JobSpec(schedule="in 20m", prompt="once"))
    probe.fail()
    for _ in range(3 * 60):  # three hours of minute ticks
        fake_clock.advance(60)
        assert tick(store, health, ledger, fake_clock) == []
    probe.heal()
    fake_clock.advance(60)
    fired = tick(store, health, ledger, fake_clock)
    assert sorted(fired) == sorted([hourly.id, daily.id, oneshot.id])
    for _ in range(5):
        fake_clock.advance(60)
        assert tick(store, health, ledger, fake_clock) == []
    assert all(j.next_run_at is None or j.next_run_at > fake_clock.now() for j in store.load()
               if j.recurring)  # fmt: skip


def test_notices_once_each_way(
    health: StoreHealth, probe: SwitchProbe, fake_clock: FakeClock, events: list[HealthEvent]
) -> None:
    probe.fail()
    health.gate()
    fake_clock.advance(120)
    health.gate()
    probe.heal()
    fake_clock.advance(60)
    assert health.gate() is True
    assert [e.kind for e in events] == ["unwritable"]
    # Flapping inside the hour: same outage, no second "unwritable".
    fake_clock.advance(600)
    probe.fail()
    assert health.gate() is False
    fake_clock.advance(60)
    probe.heal()
    assert health.gate() is True
    assert [e.kind for e in events] == ["unwritable"]
    since = health.outage_since
    assert since is not None
    # Writable for 59 minutes: no recovery notice yet.
    fake_clock.advance(59 * 60)
    health.gate()
    assert [e.kind for e in events] == ["unwritable"]
    fake_clock.advance(60)
    health.gate()
    assert [e.kind for e in events] == ["unwritable", "recovered"]
    assert events[1].condition.since == since
    assert health.outage_since is None
    fake_clock.advance(3 * 3600)
    health.gate()
    assert len(events) == 2


def test_failing_listener_does_not_break_gate(
    cron_scope: Scope, fake_clock: FakeClock, probe: SwitchProbe
) -> None:
    def boom(event: HealthEvent) -> None:
        raise RuntimeError("listener down")

    health = StoreHealth(cron_scope.home / "cron", clock=fake_clock, notify=boom, probe=probe)
    probe.fail()
    assert health.gate() is False


def test_other_errors_also_block(health: StoreHealth, probe: SwitchProbe) -> None:
    probe.fail(errno.EIO)
    assert health.gate() is False and not health.writable


# -- the real probe -----------------------------------------------------------------------------


def test_probe_missing_dir_is_writable(tmp_path: Path) -> None:
    assert probe_dir(tmp_path / "nope") is None


@pytest.fixture
def cron_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "cron"
    folder.mkdir()
    return folder


def test_probe_writes_and_cleans_up(cron_dir: Path) -> None:
    (cron_dir / "jobs.json").write_text("{}" * 1000)
    assert probe_dir(cron_dir) is None
    assert sorted(p.name for p in cron_dir.iterdir()) == ["jobs.json"]


def test_probe_reports_enospc(cron_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = os.write

    def full(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(store_health.os, "write", full)
    error = probe_dir(cron_dir)
    monkeypatch.setattr(store_health.os, "write", real_write)
    assert error is not None and error.errno == errno.ENOSPC
    assert list(cron_dir.iterdir()) == []  # temp file removed even on failure
    report = probe_report(cron_dir)
    assert report is None


def test_probe_report_fields(cron_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (cron_dir / "jobs.json").write_text("{}")

    def readonly(fd: int, data: bytes) -> int:
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(store_health.os, "write", readonly)
    report = probe_report(cron_dir)
    assert report is not None
    assert report["error"].startswith("EROFS")
    assert report["store"] == str(cron_dir) and report["since"] != "unknown"
    assert "remount" in report["fix"]


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="root bypasses file modes",
)
def test_probe_readonly_file(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs.json"
    jobs.write_text("{}")
    jobs.chmod(stat.S_IRUSR)
    error = probe_dir(tmp_path)
    assert error is not None and error.errno == errno.EACCES


def test_oneshot_due_in_outage_keeps_grace_after(
    cron_scope: Scope,
    health: StoreHealth,
    probe: SwitchProbe,
    ledger: ExecutionLedger,
    fake_clock: FakeClock,
) -> None:
    store = CronStore(cron_scope)
    job = store.create(JobSpec(schedule="in 5m", prompt="once"))
    probe.fail()
    fake_clock.advance(60)
    assert tick(store, health, ledger, fake_clock) == []
    fake_clock.advance(timedelta(hours=1).total_seconds())
    probe.heal()
    assert tick(store, health, ledger, fake_clock) == [job.id]
