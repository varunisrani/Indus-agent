"""P15-21: the cron ticker hosted by the gateway daemon (H3, R07.17)."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from typing import Any

import pytest
from click.testing import CliRunner

from indus.cli.args import CliState
from indus.cli.commands import cron as cron_cli
from indus.cron import delivery, ports, settings
from indus.cron.classify import RunOutcome
from indus.cron.delivery import DeliveryReceipt, DeliveryRequest
from indus.cron.ticker import (
    OWNER_FILE,
    OWNER_TTL_S,
    Ticker,
    claim_ticker,
    release_ticker,
    ticker_owner,
)
from indus.foundation.scope import Scope
from indus.gateway.cron_host import narrates_silence, screen_outcome
from indus.service.cron_host import CronHost
from indus.state import holders
from tests.fakes.agent import run_driven
from tests.fakes.clock import FakeClock
from tests.unit.gateway.cron_world import CronWorld, cron_scope


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def world(scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> CronWorld:
    found = CronWorld(cron_scope(scope), fake_clock)
    monkeypatch.setattr(ports, "BACKEND_FACTORY", lambda s: CronHost(s, agent=found.agent))
    return found


@pytest.fixture
def captured() -> Any:
    """Every local delivery, still delivered by P14's own local deliverer."""
    local = delivery.deliverer_for("local")
    assert local is not None
    sent: list[DeliveryRequest] = []

    async def capture(request: DeliveryRequest) -> DeliveryReceipt:
        sent.append(request)
        return await local(request)

    delivery.register_deliverer("local", capture)
    try:
        yield sent
    finally:
        delivery.register_deliverer("local", local)


def cli_tick(scope: Scope, *argv: str) -> tuple[int, str]:
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    result = CliRunner().invoke(cron_cli.cron_group, list(argv or ["tick"]), obj=state)
    return result.exit_code, result.stdout


# -- the lease -------------------------------------------------------------------------------------


def test_a_fresh_lease_from_a_live_process_owns_the_ticker(world: CronWorld) -> None:
    home, now = world.scope.home, world.clock.now()
    assert ticker_owner(home, now) is None
    owner = claim_ticker(home, now)
    assert owner.pid == os.getpid() and owner.kind == "gateway"
    path = settings.cron_dir(home) / OWNER_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    found = ticker_owner(home, now + timedelta(seconds=OWNER_TTL_S - 1))
    assert found is not None and found.pid == os.getpid()
    # three missed passes: stale
    assert ticker_owner(home, now + timedelta(seconds=OWNER_TTL_S + 1)) is None
    assert release_ticker(home) and not path.exists()
    assert not release_ticker(home)


def test_a_dead_holder_a_reused_pid_or_a_bad_file_owns_nothing(
    world: CronWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, now = world.scope.home, world.clock.now()
    path = settings.cron_dir(home) / OWNER_FILE
    claim_ticker(home, now)
    data = json.loads(path.read_text())

    stamp = holders.holder_stamp().replace(f"pid={os.getpid()}", "pid=999999")
    path.write_text(json.dumps({**data, "holder": stamp, "pid": 999999, "start": None}))
    monkeypatch.setattr(holders, "_pid_gone", lambda pid: pid == 999999)
    if holders.local_pid_namespace() is not None:
        assert ticker_owner(home, now) is None  # provably dead

    if data["start"] is not None:
        path.write_text(json.dumps({**data, "start": data["start"] + 1}))
        assert ticker_owner(home, now) is None  # the pid now belongs to another process

    path.write_text("{not json")
    assert ticker_owner(home, now) is None
    path.write_text(json.dumps({**data, "pid": True}))
    assert ticker_owner(home, now) is None
    path.write_text(json.dumps(data))
    assert ticker_owner(home, now) is not None
    # someone else's lease is never removed by release
    path.write_text(json.dumps({**data, "pid": 999999}))
    assert not release_ticker(home) and path.exists()


def test_cron_tick_refuses_while_a_live_gateway_owns_the_profile(world: CronWorld) -> None:
    job = world.create(schedule="every 30m", prompt="check the build")
    world.clock.advance(31 * 60)  # the job is due
    claim_ticker(world.scope.home, world.clock.now())
    code, out = cli_tick(world.scope)
    assert code == 0
    assert "the gateway (pid" in out and "runs the scheduler" in out
    assert world.runs == []
    # the gateway stops: the next tick runs the due job
    assert release_ticker(world.scope.home)
    backend = world.backend()
    with backend.opened() as ctx:
        report = run(run_driven(Ticker(backend.env(ctx)).tick(wait=True), world.clock, step=0.5))
    assert report.status == "ran" and report.outcomes[job.id].status == "ok"
    assert world.runs == [job.id]


def test_cron_daemon_defers_then_takes_over(world: CronWorld) -> None:
    async def main() -> None:
        claim_ticker(world.scope.home, world.clock.now())
        backend = world.backend()
        with backend.opened() as ctx:
            ticker = Ticker(backend.env(ctx), clock=world.clock)
            assert (await ticker.tick()).status == "deferred"
            assert (await ticker.tick()).status == "deferred"
            release_ticker(world.scope.home)
            assert (await ticker.tick()).status == "ran"
            hosted = Ticker(backend.env(ctx), clock=world.clock, hosted=True)
            claim_ticker(world.scope.home, world.clock.now())
            assert (await hosted.tick()).status == "ran"  # the host never defers to itself

    run(main())


# -- the hosted loop -------------------------------------------------------------------------------


def test_a_due_job_fires_once_at_the_first_pass_after_its_time(
    world: CronWorld, captured: list[DeliveryRequest]
) -> None:
    async def main() -> None:
        job = world.create(schedule="every 30m", prompt="check the build")
        assert job.next_run_at is not None
        due = job.next_run_at
        host = world.gateway_host()
        await host.start()
        try:
            assert host.running and host.owner is not None
            assert ticker_owner(world.scope.home, world.clock.now()) is not None
            await world.idle()
            assert world.runs == []
            # up to one second before the slot: nothing fires
            await world.advance((due - world.clock.now()).total_seconds() - 1)
            assert world.runs == []
            # the first pass at or after the slot fires it, once
            await world.until(lambda: world.runs == [job.id])
            await world.until(lambda: world.attempts(job.id)[-1].terminal)
            assert world.clock.now() >= due
            assert world.clock.now() - due < timedelta(seconds=61)
            await world.advance(120)
            assert world.runs == [job.id]
            attempt = world.attempts(job.id)[-1]
            assert attempt.result_status == "ok" and attempt.delivery_status == "delivered"
            assert [r.job_id for r in captured] == [job.id]
        finally:
            await host.stop()
        assert not host.running
        assert ticker_owner(world.scope.home, world.clock.now()) is None

    run(main())


def test_a_delivery_failure_leaves_the_run_ok(world: CronWorld) -> None:
    async def refuse(request: DeliveryRequest) -> DeliveryReceipt:
        return DeliveryReceipt(ok=False, error="chat not found")

    undo = delivery.register_deliverer("telegram", refuse)

    async def main() -> None:
        job = world.create(schedule="every 30m", prompt="report", deliver="telegram:123")
        world.replies[job.id] = ["the build is green"]
        host = world.gateway_host()
        await host.start()
        try:
            await world.advance(31 * 60, step=10)
            await world.until(
                lambda: bool(world.attempts(job.id)) and world.attempts(job.id)[-1].terminal
            )
            await world.until(lambda: world.attempts(job.id)[-1].delivery_status is not None)
        finally:
            await host.stop()
        attempt = world.attempts(job.id)[-1]
        assert attempt.result_status == "ok"
        assert attempt.delivery_status == "delivery_failed"
        record = world.job(job.id)
        # P14's split: the run succeeded, its delivery did not; no failure streak
        assert record.last_status == "delivery_failed" and record.failure_streak == 0
        assert record.last_error is None
        assert record.last_delivery_error and "chat not found" in record.last_delivery_error

    try:
        run(main())
    finally:
        undo()


def test_stop_drains_then_cancels_a_run_in_flight(world: CronWorld) -> None:
    async def main() -> None:
        job = world.create(schedule="every 30m", prompt="slow")
        world.hang.add(job.id)
        host = world.gateway_host(drain_s=30)
        await host.start()
        await world.advance(31 * 60, step=10)
        await world.until(lambda: world.runs == [job.id])
        stopping = asyncio.ensure_future(host.stop())
        await world.idle()
        assert not stopping.done()  # waiting for the run
        await world.until(stopping.done, step=5)
        await stopping
        assert ticker_owner(world.scope.home, world.clock.now()) is None
        await world.until(lambda: world.attempts(job.id)[-1].terminal)

    run(main())


# -- silence narration -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "dropped"),
    [
        ("(silent)", True),
        ("*(silent)*", True),
        ("_Staying silent._", True),
        ("🔇", True),
        ("...", True),
        ("Nothing to report.", True),
        ("I'm staying quiet for now", True),
        ("No updates today!", True),
        ("[no reply]", True),
        ("The build failed: nothing to report about the tests yet", False),
        ("Silent alarm triggered on server 3", False),
        ("All 42 checks passed.", False),
        ("silent " * 12, False),  # over 64 characters
        ("", False),
    ],
)
def test_silence_narration_table(text: str, dropped: bool) -> None:
    assert narrates_silence(text) is dropped


def test_only_successful_delivering_narrations_are_screened() -> None:
    narration = RunOutcome("ok", message="(silent)", deliver=True)
    assert screen_outcome(narration).status == "silent"
    assert not screen_outcome(narration).deliver
    failed = RunOutcome("failed", message="(silent)", deliver=True)
    assert screen_outcome(failed) is failed
    report = RunOutcome("ok", message="disk is 91% full", deliver=True)
    assert screen_outcome(report) is report


def test_a_narrated_silence_is_not_delivered(
    world: CronWorld, captured: list[DeliveryRequest]
) -> None:
    async def main() -> None:
        job = world.create(schedule="every 30m", prompt="watch")
        world.replies[job.id] = ["*(staying silent)*"]
        host = world.gateway_host()
        await host.start()
        try:
            await world.advance(31 * 60, step=10)
            await world.until(
                lambda: bool(world.attempts(job.id)) and world.attempts(job.id)[-1].terminal
            )
        finally:
            await host.stop()
        assert world.runs == [job.id]
        assert captured == []
        record = world.job(job.id)
        assert record.last_status == "silent" and record.last_delivery_error is None

    run(main())
