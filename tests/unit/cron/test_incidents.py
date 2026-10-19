"""P14-15: incident grouping, alert-once, reminders, resolve and acknowledge."""

from __future__ import annotations

import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from indus.cron import ports
from indus.cron.incidents import (
    MAX_ERROR_CHARS,
    IncidentLedger,
    decide_failure_alert,
    error_signature,
    resolve_after_success,
)
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from tests.fakes.clock import FakeClock

JOB = "a1b2c3d4e5f6"
FAKE_KEY = "ghp_" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe9Dc8"


def with_config(scope: Scope, text: str) -> Scope:
    path = loader.config_path(scope)
    path.write_text(text, encoding="utf-8")
    loader.invalidate(path)
    return scope.with_config(loader.load(scope))


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("timeout after 31.2s", "Timeout  after 45s", True),
        ("idle for 603 seconds", "idle for 12 seconds", True),
        ("HTTP 401 unauthorized", "HTTP 403 forbidden", False),
        ("x" * 200 + "tail one", "x" * 200 + "tail two", True),  # only the first 200 chars
        ("boom", "boom\n\n", True),
    ],
)
def test_signature_normalisation(a: str, b: str, same: bool) -> None:
    assert (error_signature(JOB, a) == error_signature(JOB, b)) is same
    assert error_signature(JOB, a) != error_signature("ffffffffffff", a)


def test_first_alerts_repeats_silent_until_the_six_hour_reminder(
    scope: Scope, fake_clock: FakeClock
) -> None:
    first = decide_failure_alert(scope, JOB, "timeout after 31.2s")
    assert first.alert and first.reason == "new"
    fake_clock.advance(3600)
    second = decide_failure_alert(scope, JOB, "Timeout after 45s")
    assert not second.alert and second.incident_id == first.incident_id
    fake_clock.advance(5 * 3600 + 1)  # 6 h after the alert
    reminder = decide_failure_alert(scope, JOB, "timeout after 2s")
    assert reminder.alert and reminder.reason == "reminder"
    fake_clock.advance(60)
    assert not decide_failure_alert(scope, JOB, "timeout after 9s").alert


def test_zero_hours_alerts_every_run(scope: Scope) -> None:
    scope = with_config(scope, "cron:\n  failure_repeat_alert_hours: 0\n")
    assert decide_failure_alert(scope, JOB, "boom").alert
    assert decide_failure_alert(scope, JOB, "boom").alert


def test_different_error_alerts_at_once(scope: Scope) -> None:
    assert decide_failure_alert(scope, JOB, "boom").alert
    assert decide_failure_alert(scope, JOB, "a different failure").alert
    ledger = IncidentLedger.for_scope(scope)
    assert len(ledger.list(job_id=JOB)) == 2


def test_success_resolves_and_a_repeat_reopens(scope: Scope) -> None:
    decide_failure_alert(scope, JOB, "boom")
    assert resolve_after_success(scope, JOB) == 1
    ledger = IncidentLedger.for_scope(scope)
    (row,) = ledger.list()
    assert row.state == "resolved"
    again = decide_failure_alert(scope, JOB, "boom")
    assert again.alert and again.reason == "new"
    assert ledger.get(row.id).occurrences == 2  # type: ignore[union-attr]


def test_ack_silences_for_good(scope: Scope, fake_clock: FakeClock) -> None:
    first = decide_failure_alert(scope, JOB, "boom")
    ledger = IncidentLedger.for_scope(scope)
    assert first.incident_id is not None and ledger.ack(first.incident_id)
    assert not ledger.ack(first.incident_id)
    assert not ledger.ack("nope")
    fake_clock.advance(7 * 24 * 3600)
    later = decide_failure_alert(scope, JOB, "boom")
    assert not later.alert and later.reason == "acknowledged"
    assert resolve_after_success(scope, JOB) == 0  # an acknowledged row is never resolved
    assert ledger.list("closed")[0].state == "closed"


def test_corrupt_ledger_still_alerts(scope: Scope) -> None:
    path = ports.cron_dir(scope) / "executions.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a database" * 100)
    decision = decide_failure_alert(scope, JOB, "boom")
    assert decision.alert and decision.incident_id is None
    assert resolve_after_success(scope, JOB) == 0


def test_stored_error_is_masked_and_bounded(scope: Scope) -> None:
    decide_failure_alert(scope, JOB, f"token {FAKE_KEY} refused " + "y" * 900)
    (row,) = IncidentLedger.for_scope(scope).list()
    assert FAKE_KEY not in row.error and len(row.error) == MAX_ERROR_CHARS


def test_reading_never_creates_the_file(scope: Scope) -> None:
    ledger = IncidentLedger.for_scope(scope)
    assert ledger.list() == [] and ledger.get("x") is None
    assert not ledger.path.exists()


def test_file_modes_and_forget(scope: Scope) -> None:
    decide_failure_alert(scope, JOB, "boom")
    decide_failure_alert(scope, "ffffffffffff", "boom")
    path = ports.cron_dir(scope) / "executions.db"
    assert (os.stat(path).st_mode & 0o777) == 0o600
    ledger = IncidentLedger.for_scope(scope)
    assert ledger.forget_job(JOB) == 1
    assert [i.job_id for i in ledger.list()] == ["ffffffffffff"]
    assert ledger.list("bogus") == []


def test_alerted_row_without_a_stamp_alerts(scope: Scope, tmp_path: Path) -> None:
    first = decide_failure_alert(scope, JOB, "boom")
    path = ports.cron_dir(scope) / "executions.db"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE cron_incidents SET alerted_at=NULL WHERE id=?", (first.incident_id,))
    assert decide_failure_alert(scope, JOB, "boom").alert


def test_reminder_window_counts_from_the_last_alert(scope: Scope, fake_clock: FakeClock) -> None:
    decide_failure_alert(scope, JOB, "boom")
    fake_clock.advance(int(timedelta(hours=6).total_seconds()))
    assert decide_failure_alert(scope, JOB, "boom").alert
    fake_clock.advance(int(timedelta(hours=5).total_seconds()))
    assert not decide_failure_alert(scope, JOB, "boom").alert
