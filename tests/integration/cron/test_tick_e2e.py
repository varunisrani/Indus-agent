"""A real `indus cron tick` process runs a script-only job and writes its output document
(P14-07, H5)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from indus.cron.context import CronContext
from indus.cron.models import JobSpec
from indus.cron.output import documents, read_document
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope, make_scope

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "cron" / "scripts"
TIMEOUT = 90


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("INDUS_")}
    env.update({"INDUS_HOME": str(home), "PYTHONPATH": str(ROOT)})
    return env


def indus(home: Path, cwd: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "indus", *argv],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=_env(home),
        timeout=TIMEOUT,
    )


def _scope(home: Path) -> Scope:
    return make_scope(home, env={"INDUS_HOME": str(home)}, clock=SystemClock())


def test_tick_process_runs_a_script_only_job(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "scripts").mkdir(parents=True)
    shutil.copy(FIXTURES / "report.sh", home / "scripts" / "report.sh")
    created = indus(
        home, tmp_path, "cron", "create", "every 30m", "--script", "report.sh", "--no-agent"
    )
    assert created.returncode == 0, created.stderr
    ctx = CronContext.open(_scope(home))
    try:
        (job,) = ctx.store.load()
        due = datetime.now(UTC) - timedelta(minutes=1)
        ctx.store.patch(job.id, next_run_at=due)
    finally:
        ctx.close()

    first = indus(home, tmp_path, "cron", "tick")
    assert first.returncode == 0, first.stderr
    assert f"{job.id} ok" in first.stdout

    docs = documents(home, job.id)
    assert len(docs) == 1
    doc = read_document(docs[0])
    assert doc is not None and doc.status == "ok"
    assert doc.section("response") == "disk usage 71% on /srv"
    ctx = CronContext.open(_scope(home))
    try:
        (attempt,) = ctx.ledger.history(job.id)
        assert attempt.state == "completed" and attempt.kind == "scheduled"
        assert attempt.delivery_status == "delivered"
        after = ctx.store.get(job.id)
        assert after is not None and after.last_status == "ok" and after.repeat.completed == 1
        assert after.next_run_at is not None and after.next_run_at > datetime.now(UTC)
    finally:
        ctx.close()
    assert (home / "cron" / "ticker_heartbeat").exists()
    assert (home / "cron" / "ticker_last_success").exists()

    second = indus(home, tmp_path, "cron", "tick")  # the slot moved on: nothing is due now
    assert second.returncode == 0, second.stderr
    assert len(documents(home, job.id)) == 1


def test_create_refuses_a_command_line_script(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    ctx = CronContext.open(_scope(home))
    try:
        refused = indus(home, tmp_path, "cron", "create", "30m", "--script", "rm -rf x")
        assert refused.returncode == 1 and "command line" in refused.stderr
        try:
            ctx.store.create(JobSpec(schedule="30m", script="ls -la", no_agent=True))
        except ValueError as exc:  # the store's own rule (P14-09), not only the CLI's
            assert "command line" in str(exc)
        else:  # pragma: no cover - the assertion above is the test
            raise AssertionError("the store accepted a command line as a script")
    finally:
        ctx.close()
