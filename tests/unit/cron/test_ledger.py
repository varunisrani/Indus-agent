"""Execution ledger (P14-04): record before dispatch, no slot replayed, reaping, final states."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from indus.cron.due import evaluate
from indus.cron.errors import AttemptFinal, SlotTaken
from indus.cron.ledger import (
    KEEP_PER_JOB,
    ExecutionLedger,
    ExecutionsDB,
    SystemProcesses,
    silence_bound,
)
from indus.cron.models import JobSpec
from indus.cron.store import CronStore
from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock
from tests.fakes.pids import FakePids

SLOT = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)


@pytest.fixture
def cron_scope(scope: Scope) -> Scope:
    return scope.with_config(DictConfig({"timezone": "UTC"}))


@pytest.fixture
def pids() -> FakePids:
    return FakePids(me=(100, "boot:100"))


@pytest.fixture
def db(cron_scope: Scope) -> Iterator[ExecutionsDB]:
    handle = ExecutionsDB(cron_scope)
    yield handle
    handle.close()


@pytest.fixture
def ledger(db: ExecutionsDB, pids: FakePids) -> ExecutionLedger:
    return ExecutionLedger(db, procs=pids)


def test_database_location_and_tables(db: ExecutionsDB, cron_scope: Scope) -> None:
    assert db.path == cron_scope.home / "cron" / "executions.db"
    tables = db.read(
        lambda c: {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    )
    assert {"attempts", "job_notes"} <= tables


def test_lifecycle(ledger: ExecutionLedger, fake_clock: FakeClock) -> None:
    attempt = ledger.claim("job1", scheduled_for=SLOT)
    assert attempt.state == "claimed" and attempt.owner_pid == 100
    assert attempt.scheduled_for == SLOT and attempt.kind == "scheduled"
    fake_clock.advance(5)
    running = ledger.start(attempt.attempt_id)
    assert running.state == "running" and running.started_at == fake_clock.now()
    fake_clock.advance(5)
    assert ledger.heartbeat(attempt.attempt_id)
    done = ledger.finish(
        attempt.attempt_id, ok=True, result_status="ok", delivery_status="delivered"
    )
    assert done.state == "completed" and done.finished_at == fake_clock.now()
    assert done.terminal and done.error is None


def test_record_exists_before_dispatch(ledger: ExecutionLedger) -> None:
    dispatched: list[str] = []

    def dispatcher(attempt_id: str) -> None:
        row = ledger.get(attempt_id)
        assert row is not None and row.state == "claimed"  # the record is already durable
        dispatched.append(attempt_id)

    attempt = ledger.claim("job1", scheduled_for=SLOT)
    dispatcher(attempt.attempt_id)
    assert dispatched == [attempt.attempt_id]


def test_slot_claimed_once(ledger: ExecutionLedger) -> None:
    ledger.claim("job1", scheduled_for=SLOT)
    with pytest.raises(SlotTaken):
        ledger.claim("job1", scheduled_for=SLOT)
    # Another job, another slot, retries and manual runs are separate.
    ledger.claim("job2", scheduled_for=SLOT)
    ledger.claim("job1", scheduled_for=SLOT + timedelta(minutes=30))
    ledger.claim("job1", scheduled_for=SLOT, kind="retry")
    ledger.claim("job1", scheduled_for=None, kind="manual")
    ledger.claim("job1", scheduled_for=None, kind="manual")
    with pytest.raises(ValueError):
        ledger.claim("job1", scheduled_for=None, kind="scheduled")


def test_slot_identity_is_the_instant(ledger: ExecutionLedger) -> None:
    ledger.claim("job1", scheduled_for=SLOT)
    from zoneinfo import ZoneInfo

    same = SLOT.astimezone(ZoneInfo("Asia/Kolkata"))
    assert ledger.slot_attempted("job1", same)
    with pytest.raises(SlotTaken):
        ledger.claim("job1", scheduled_for=same)
    assert not ledger.slot_attempted("job1", SLOT + timedelta(microseconds=1))


def test_completed_slot_not_replayed_after_old_jobs_file(
    cron_scope: Scope, ledger: ExecutionLedger, fake_clock: FakeClock
) -> None:
    store = CronStore(cron_scope)
    job = store.create(JobSpec(schedule="every 30m", prompt="check the build"))
    old_file = store.paths.jobs_file.read_bytes()
    fake_clock.advance(30 * 60)
    now = fake_clock.now()
    decision = evaluate(store.resolve(job.id), now=now, tz=UTC, slots=ledger)
    assert decision.fire and decision.scheduled_for is not None
    attempt = ledger.claim(job.id, scheduled_for=decision.scheduled_for)
    store.patch(job.id, **dict(decision.updates))
    ledger.finish(attempt.attempt_id, ok=True)
    # Someone restores yesterday's jobs.json: next_run_at points at the slot that already ran.
    store.paths.jobs_file.write_bytes(old_file)
    stale = store.resolve(job.id)
    assert stale.next_run_at == decision.scheduled_for
    again = evaluate(stale, now=now + timedelta(seconds=30), tz=UTC, slots=ledger)
    assert again.fire is False
    assert again.updates["next_run_at"] > now
    assert "already attempted" in (again.note or "")


def test_terminal_states_are_final(ledger: ExecutionLedger) -> None:
    attempt = ledger.claim("job1", scheduled_for=SLOT)
    ledger.finish(attempt.attempt_id, ok=False, error="model unreachable")
    with pytest.raises(AttemptFinal):
        ledger.finish(attempt.attempt_id, ok=True)
    with pytest.raises(AttemptFinal):
        ledger.start(attempt.attempt_id)
    assert ledger.heartbeat(attempt.attempt_id) is False
    row = ledger.get(attempt.attempt_id)
    assert row is not None and row.state == "failed" and row.error == "model unreachable"


def test_delivery_status_filled_once(ledger: ExecutionLedger) -> None:
    attempt = ledger.claim("job1", scheduled_for=SLOT)
    ledger.finish(attempt.attempt_id, ok=True)
    assert ledger.note_delivery(attempt.attempt_id, "delivered")
    assert not ledger.note_delivery(attempt.attempt_id, "delivery_failed")
    row = ledger.get(attempt.attempt_id)
    assert row is not None and row.delivery_status == "delivered" and row.state == "completed"


# -- reaping ------------------------------------------------------------------------------------


def _other(ledger: ExecutionLedger, pids: FakePids, pid: int, fingerprint: str) -> ExecutionLedger:
    pids.spawn(pid, fingerprint)
    return ExecutionLedger(ledger.db, procs=pids.as_process(pid))


def test_dead_owner_becomes_unknown(
    ledger: ExecutionLedger, pids: FakePids, fake_clock: FakeClock
) -> None:
    other = _other(ledger, pids, 200, "boot:200")
    attempt = other.claim("job1", scheduled_for=SLOT)
    other.start(attempt.attempt_id)
    assert ledger.reap() == []  # alive, recent heartbeat
    pids.kill(200)
    (reaped,) = ledger.reap()
    assert reaped.attempt_id == attempt.attempt_id and reaped.state == "unknown"
    assert reaped.finished_at == fake_clock.now() and "unknown" in (reaped.error or "")
    with pytest.raises(AttemptFinal):
        other.finish(attempt.attempt_id, ok=True)  # the slow owner cannot overwrite it


def test_reused_pid_is_proof_of_death(ledger: ExecutionLedger, pids: FakePids) -> None:
    other = _other(ledger, pids, 300, "boot:300")
    attempt = other.claim("job1", scheduled_for=SLOT)
    pids.reuse(300, "boot:999")
    (reaped,) = ledger.reap()
    assert reaped.attempt_id == attempt.attempt_id


def test_unreadable_fingerprint_is_not_proof(ledger: ExecutionLedger, pids: FakePids) -> None:
    other = _other(ledger, pids, 400, "boot:400")
    other.claim("job1", scheduled_for=SLOT)
    pids.unreadable(400)
    assert ledger.reap() == []


def test_silent_live_owner_after_two_hours(
    ledger: ExecutionLedger, pids: FakePids, fake_clock: FakeClock
) -> None:
    other = _other(ledger, pids, 500, "boot:500")
    attempt = other.claim("job1", scheduled_for=SLOT)
    other.start(attempt.attempt_id)
    fake_clock.advance(7200 - 60)
    assert ledger.reap() == []
    other.heartbeat(attempt.attempt_id)  # progress resets the silence clock
    fake_clock.advance(7200)
    assert ledger.reap() == []
    fake_clock.advance(1)
    (reaped,) = ledger.reap()
    assert reaped.state == "unknown" and "stopped reporting" in (reaped.error or "")


def test_silence_bound_formula() -> None:
    assert silence_bound(600, 3600) == 7200
    assert silence_bound(3000, 3600) == 9000
    assert silence_bound(600, 10_000) == 10_000
    assert silence_bound(0, 0) == 7200


def test_silence_uses_configured_timeouts(
    ledger: ExecutionLedger, pids: FakePids, fake_clock: FakeClock
) -> None:
    other = _other(ledger, pids, 600, "boot:600")
    other.claim("job1", scheduled_for=SLOT)
    fake_clock.advance(7300)
    assert ledger.reap(inactivity_timeout_s=3000, script_timeout_s=60) == []
    assert len(ledger.reap(inactivity_timeout_s=600, script_timeout_s=60)) == 1


def test_own_attempts_left_alone(ledger: ExecutionLedger, fake_clock: FakeClock) -> None:
    attempt = ledger.claim("job1", scheduled_for=SLOT)
    fake_clock.advance(10 * 3600)
    assert ledger.reap() == []
    assert ledger.get(attempt.attempt_id).state == "claimed"  # type: ignore[union-attr]


def test_unknown_never_rerun(
    cron_scope: Scope, ledger: ExecutionLedger, pids: FakePids, fake_clock: FakeClock
) -> None:
    store = CronStore(cron_scope)
    job = store.create(JobSpec(schedule="in 5m", prompt="once"))
    fake_clock.advance(300)
    other = _other(ledger, pids, 700, "boot:700")
    assert job.next_run_at is not None
    other.claim(job.id, scheduled_for=job.next_run_at)
    pids.kill(700)
    assert ledger.reap()[0].state == "unknown"
    decision = evaluate(store.resolve(job.id), now=fake_clock.now(), tz=UTC, slots=ledger)
    assert decision.fire is False and decision.updates.get("state") == "completed"
    with pytest.raises(SlotTaken):
        ledger.claim(job.id, scheduled_for=job.next_run_at)


# -- history ------------------------------------------------------------------------------------


def test_history_bounded_active_kept(ledger: ExecutionLedger, fake_clock: FakeClock) -> None:
    active = ledger.claim("job1", scheduled_for=SLOT - timedelta(days=30))
    for i in range(KEEP_PER_JOB + 5):
        fake_clock.advance(1)
        a = ledger.claim("job1", scheduled_for=SLOT + timedelta(minutes=i))
        ledger.finish(a.attempt_id, ok=True)
    other = ledger.claim("job2", scheduled_for=SLOT)
    ledger.finish(other.attempt_id, ok=True)
    rows = ledger.history("job1", limit=10_000)
    assert len([r for r in rows if r.terminal]) == KEEP_PER_JOB
    assert any(r.attempt_id == active.attempt_id for r in rows)
    assert [a.attempt_id for a in ledger.active()] == [active.attempt_id]
    assert len(ledger.history("job2")) == 1
    # The newest kept, the oldest pruned.
    kept_slots = {r.scheduled_for for r in rows if r.terminal}
    assert SLOT + timedelta(minutes=KEEP_PER_JOB + 4) in kept_slots
    assert SLOT not in kept_slots
    assert len(ledger.recent(limit=3)) == 3


def test_reopen_keeps_rows(cron_scope: Scope, pids: FakePids) -> None:
    first = ExecutionsDB(cron_scope)
    ExecutionLedger(first, procs=pids).claim("job1", scheduled_for=SLOT)
    first.close()
    second = ExecutionsDB(cron_scope)
    try:
        assert ExecutionLedger(second, procs=pids).slot_attempted("job1", SLOT)
    finally:
        second.close()


def test_db_file_owner_only(db: ExecutionsDB) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX modes")
    assert (db.path.stat().st_mode & 0o077) == 0


# -- real process probe -------------------------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc fingerprint")
def test_system_processes_on_linux(tmp_path: Path) -> None:
    probe = SystemProcesses()
    pid, fingerprint = probe.current()
    assert pid == os.getpid() and fingerprint and ":" in fingerprint
    assert probe.exists(pid) and probe.fingerprint(pid) == fingerprint
    assert not probe.exists(0)
    # A fake /proc whose command name holds spaces and parentheses.
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("b00t\n")
    (proc / "42").mkdir()
    fields = " ".join(str(n) for n in range(3, 25))
    (proc / "42" / "stat").write_text(f"42 (odd ) name) {fields}\n")
    assert SystemProcesses(proc).fingerprint(42) == "b00t:22"
    assert SystemProcesses(proc).fingerprint(43) is None


def test_unreadable_store_error_surfaces(cron_scope: Scope) -> None:
    folder = cron_scope.home / "cron"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "executions.db").write_bytes(b"not a database at all" * 10)
    db = ExecutionsDB(cron_scope)  # moved aside by the P02 quarantine, then created fresh
    try:
        assert db.read(lambda c: c.execute("SELECT count(*) FROM attempts").fetchone()[0]) == 0
        assert any(p.name.startswith("executions.db") and p.name != "executions.db"
                   for p in folder.iterdir())  # fmt: skip
    finally:
        db.close()
    shutil.rmtree(folder)


def test_sqlite_row_round_trip(ledger: ExecutionLedger) -> None:
    attempt = ledger.claim("job1", scheduled_for=SLOT)
    row = ledger.db.read(
        lambda c: c.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt.attempt_id,)
        ).fetchone()
    )
    assert isinstance(row, sqlite3.Row) and row["kind"] == "scheduled"
