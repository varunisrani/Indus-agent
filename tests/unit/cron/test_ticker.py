"""The ticker: tick lock, fire claims, pool, global pause, heartbeats, daemon (P14-07, R07.13)."""

from __future__ import annotations

import asyncio
import dataclasses
import errno
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from indus.cron.models import CronJob
from indus.cron.ports import DueSlot
from indus.cron.preflight import PreflightProbes
from indus.cron.runner import AgentRunRequest, AgentRunResult, RunEnv
from indus.cron.ticker import (
    BUSY_MESSAGE,
    CLAIM_TTL_S,
    HEARTBEAT_MARKER,
    SUCCESS_MARKER,
    AlreadyRunning,
    JobNotFound,
    Ticker,
    TickLock,
    _take_claim,  # pyright: ignore[reportPrivateUsage]
    claim_is_live,
    global_pause,
    pause_all,
    read_marker,
    resume_all,
)
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig, run_driven
from tests.fakes.clock import FakeClock, checkpoint
from tests.unit.cron.world import World, make_world

KEYS_OK = PreflightProbes(key=lambda *_: None)


class Agent:
    """A scripted agent runner: answers at once, or waits for ``release`` when ``hold``."""

    def __init__(
        self, *, hold: bool = False, on_call: Callable[[AgentRunRequest], None] | None = None
    ):
        self.hold = hold
        self.release = asyncio.Event()
        self.calls: list[str] = []
        self.running = 0
        self.max_running = 0
        self.on_call = on_call

    async def __call__(self, request: AgentRunRequest) -> AgentRunResult:
        self.calls.append(request.job.id)
        if self.on_call is not None:
            self.on_call(request)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if self.hold:
                await self.release.wait()
            else:
                await checkpoint()  # one loop turn, so overlap is visible
            return AgentRunResult(f"report for {request.job.id}", completed=True)
        finally:
            self.running -= 1


@pytest.fixture
def real(scope: Scope) -> Scope:
    return dataclasses.replace(scope, clock=SystemClock())


def setup(scope: Scope, agent: Agent, **config: Any) -> tuple[World, Ticker]:
    if config:
        scope = scope.with_config(DictConfig({"cron": config}))
    world = make_world(scope)
    env = RunEnv(scope=scope, ports=world.ports, agent=agent, probes=KEYS_OK)
    return world, Ticker(env)


def due_job(world: World, now: datetime, **fields: Any) -> CronJob:
    return world.add(next_run_at=now - timedelta(seconds=5), **fields)


def now_of(scope: Scope) -> datetime:
    return scope.clock.now().astimezone(UTC)


# -- claims ---------------------------------------------------------------------------------------


def test_claim_liveness() -> None:
    now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

    def claim(age: float) -> dict[str, str]:
        at = (now - timedelta(seconds=age)).isoformat()
        return {"owner": "other:1:ab", "claimed_at": at, "heartbeat_at": at}

    assert claim_is_live(claim(0), now) and claim_is_live(claim(CLAIM_TTL_S - 1), now)
    assert not claim_is_live(claim(CLAIM_TTL_S), now)
    assert not claim_is_live(claim(-30), now)  # future-dated: stale, can never wedge a job
    assert not claim_is_live({"owner": "x"}, now) and not claim_is_live(None, now)
    assert not claim_is_live({"claimed_at": now.isoformat()}, now)  # no owner


def test_take_claim_advances_a_recurring_job(scope: Scope) -> None:
    now = datetime(2026, 1, 1, 9, 0, 5, tzinfo=UTC)
    world = make_world(scope)
    record = world.add(next_run_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    spec = _take_claim(record, owner="me:1:aa", now=now, next_run=world.ports.next_run)
    assert spec is not None and spec.id == record.id
    assert record.fire_claim is not None and record.fire_claim.owner == "me:1:aa"
    assert record.next_run_at == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    assert _take_claim(record, owner="you:2:bb", now=now, next_run=world.ports.next_run) is None


def test_take_claim_writes_the_due_rules_updates(scope: Scope) -> None:
    now = datetime(2026, 1, 1, 9, 0, 5, tzinfo=UTC)
    world = make_world(scope)
    record = world.add(next_run_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    later = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    _take_claim(
        record,
        owner="me:1:aa",
        now=now,
        next_run=world.ports.next_run,
        updates={"next_run_at": later},
    )
    assert record.next_run_at == later  # the due rules' slot wins over the fallback step


def test_young_claim_blocks_another_process_and_stale_claim_is_reclaimed(real: Scope) -> None:
    agent = Agent()
    world, ticker = setup(real, agent)
    now = now_of(real)
    young = (now - timedelta(seconds=100)).isoformat()
    stale = (now - timedelta(seconds=CLAIM_TTL_S + 10)).isoformat()
    held = due_job(
        world, now, fire_claim={"owner": "h:9:aa", "claimed_at": young, "heartbeat_at": young}
    )
    old = due_job(
        world, now, fire_claim={"owner": "h:9:bb", "claimed_at": stale, "heartbeat_at": stale}
    )

    report = asyncio.run(ticker.tick())
    assert report.status == "ran"
    assert agent.calls == [old.id]
    assert held.id not in report.dispatched
    after = world.get(old.id)
    assert after is not None and after.fire_claim is None  # released after the run


# -- tick lock, store health, markers -------------------------------------------------------------


def test_second_concurrent_tick_exits(real: Scope) -> None:
    agent = Agent()
    world, ticker = setup(real, agent)
    due_job(world, now_of(real))
    lock = TickLock(real.home)
    assert lock.acquire() == "held"
    try:
        report = asyncio.run(ticker.tick())
    finally:
        lock.release()
    assert report.status == "busy" and report.message == BUSY_MESSAGE
    assert report.exit_code == 0 and agent.calls == []


def test_ticks_inside_one_process_conflict_too(real: Scope) -> None:
    first, second = TickLock(real.home), TickLock(real.home)
    assert first.acquire() == "held"
    assert second.acquire() == "busy"
    first.release()
    assert second.acquire() == "held"
    second.release()


def test_unwritable_store_fires_nothing(real: Scope) -> None:
    agent = Agent()
    world, ticker = setup(real, agent)
    due_job(world, now_of(real))
    world.writable[0] = False
    report = asyncio.run(ticker.tick())
    assert report.status == "unwritable" and agent.calls == []
    assert world.rows() == []
    world.writable[0] = True
    assert asyncio.run(ticker.tick()).status == "ran" and len(agent.calls) == 1


def test_unwritable_lock_folder_fires_nothing(real: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agent()
    world, ticker = setup(real, agent)
    due_job(world, now_of(real))
    import indus.cron.ticker as ticker_mod

    def refuse(*_args: Any, **_kw: Any) -> int:
        raise OSError(errno.EROFS, "read-only file system")

    monkeypatch.setattr(ticker_mod.os, "open", refuse)
    report = asyncio.run(ticker.tick())
    assert report.status == "unwritable" and agent.calls == []


def test_markers_and_attempt_recorded_before_dispatch(real: Scope) -> None:
    seen: list[list[dict[str, Any]]] = []
    world: World | None = None

    def check(_request: AgentRunRequest) -> None:
        assert world is not None
        seen.append(world.rows())

    agent = Agent(on_call=check)
    world, ticker = setup(real, agent)
    job = due_job(world, now_of(real))
    report = asyncio.run(ticker.tick())
    assert report.dispatched == [job.id]
    assert seen and seen[0][0]["job_id"] == job.id and seen[0][0]["state"] == "running"
    rows = world.rows()
    assert rows[0]["state"] == "completed" and rows[0]["result_status"] == "ok"
    assert rows[0]["scheduled_for"] is not None and rows[0]["kind"] == "scheduled"
    assert read_marker(real.home, HEARTBEAT_MARKER) is not None
    assert read_marker(real.home, SUCCESS_MARKER) is not None
    assert world.finished.outcomes[0].message == f"report for {job.id}"
    assert world.reaped == 1  # reaped on every pass


def test_completed_slot_is_never_replayed(real: Scope) -> None:
    agent = Agent()
    world, ticker = setup(real, agent)
    now = now_of(real)
    job = due_job(world, now)
    slot = job.next_run_at
    attempt = world.ledger.begin(job.id, slot, "scheduled", now)
    assert attempt is not None
    world.ledger.finish(attempt, state="completed", result_status="ok", now=now)
    report = asyncio.run(ticker.tick())  # a stale jobs file still says the slot is due
    assert agent.calls == [] and report.dispatched == []
    record = world.get(job.id)
    assert record is not None and record.fire_claim is None
    assert record.next_run_at is not None and record.next_run_at > now  # moved past the slot


# -- pool and in-flight guard ---------------------------------------------------------------------


def test_pool_size_one_serialises(real: Scope) -> None:
    agent = Agent()
    world, ticker = setup(real, agent, max_parallel_jobs=1)
    due_job(world, now_of(real))
    due_job(world, now_of(real))
    report = asyncio.run(ticker.tick())
    assert len(report.outcomes) == 2 and agent.max_running == 1


def test_unbounded_pool_runs_together(real: Scope) -> None:
    agent = Agent()
    world, ticker = setup(real, agent)
    due_job(world, now_of(real))
    due_job(world, now_of(real))
    asyncio.run(ticker.tick())
    assert agent.max_running == 2


def test_env_override_of_the_pool(real: Scope) -> None:
    agent = Agent()
    scoped = real.with_env(real.env.with_overlay({"INDUS_CRON_MAX_PARALLEL": "1"}))
    world, ticker = setup(scoped, agent)
    due_job(world, now_of(real))
    due_job(world, now_of(real))
    asyncio.run(ticker.tick())
    assert agent.max_running == 1


def test_job_in_flight_here_is_not_dispatched_again(real: Scope) -> None:
    agent = Agent(hold=True)
    world, ticker = setup(real, agent)
    job = due_job(world, now_of(real))

    async def main() -> list[str]:
        first = await ticker.tick(wait=False)
        await checkpoint()  # let the run start
        # Pretend the claim vanished and the slot is due again: only the in-flight guard is left.
        world.store.patch(job.id, fire_claim=None, next_run_at=job.next_run_at)
        slot = DueSlot(world.get(job.id), job.next_run_at)
        again = ticker._dispatch(slot, now_of(real))  # pyright: ignore[reportPrivateUsage]
        agent.release.set()
        await ticker.drain(5)
        return [*first.dispatched, *(["again"] if again is not None else [])]

    assert asyncio.run(main()) == [job.id]
    assert agent.calls == [job.id]


# -- global pause and manual runs -----------------------------------------------------------------


def test_pause_all_stops_scheduled_fires_but_not_manual_runs(real: Scope) -> None:
    agent = Agent()
    world, ticker = setup(real, agent)
    job = due_job(world, now_of(real), name="build check")
    pause_all(real.home, "maintenance window", now_of(real))
    assert global_pause(real.home) is not None
    report = asyncio.run(ticker.tick())
    assert report.status == "paused" and "maintenance window" in report.message
    assert agent.calls == []
    outcome = asyncio.run(ticker.run_now("Build Check", run_context="just once"))
    assert outcome.status == "ok" and outcome.kind == "manual" and agent.calls == [job.id]
    manual = world.rows()[0]
    assert manual["kind"] == "manual" and manual["scheduled_for"] is None
    assert resume_all(real.home) and global_pause(real.home) is None
    assert asyncio.run(ticker.tick()).dispatched == [job.id]


def test_manual_run_refusals(real: Scope) -> None:
    agent = Agent()
    world, ticker = setup(real, agent)
    with pytest.raises(JobNotFound):
        asyncio.run(ticker.run_now("nope"))
    now = now_of(real)
    claimed = {"owner": "x:1:aa", "claimed_at": now.isoformat(), "heartbeat_at": now.isoformat()}
    job = due_job(world, now, fire_claim=claimed)
    with pytest.raises(AlreadyRunning):
        asyncio.run(ticker.run_now(job.id))


# -- heartbeats and the daemon (fake clock) -------------------------------------------------------


def test_lost_claim_stops_the_run(scope: Scope, fake_clock: FakeClock) -> None:
    agent = Agent(hold=True)
    world, ticker = setup(scope, agent)
    job = due_job(world, fake_clock.now())

    async def main() -> Any:
        report_task = asyncio.ensure_future(ticker.tick(wait=True))
        for _ in range(200):
            if agent.calls:
                break
            await checkpoint()
        stamp = fake_clock.now()
        world.store.patch(
            job.id, fire_claim={"owner": "thief:1:cc", "claimed_at": stamp, "heartbeat_at": stamp}
        )
        return await report_task

    report = asyncio.run(run_driven(main(), fake_clock, step=5.0))
    outcome = report.outcomes[job.id]
    assert outcome.status == "failed" and outcome.error == "claim lost"
    record = world.get(job.id)
    assert record is not None and record.fire_claim is not None
    assert record.fire_claim.owner == "thief:1:cc"  # not ours to free


def test_claim_heartbeat_keeps_a_long_run_alive(scope: Scope, fake_clock: FakeClock) -> None:
    agent = Agent(hold=True)
    world, ticker = setup(scope, agent)
    job = due_job(world, fake_clock.now())

    async def main() -> Any:
        report_task = asyncio.ensure_future(ticker.tick(wait=True))
        start = fake_clock.monotonic()
        while fake_clock.monotonic() - start < 900:  # three claim lifetimes
            await asyncio.wait({report_task}, timeout=0.0005)
            fake_clock.advance(5)
        record = world.get(job.id)
        assert record is not None and claim_is_live(record.fire_claim, fake_clock.now())
        agent.release.set()
        # The run's after-steps wait on the fake clock too: keep it moving until the tick ends.
        return await run_driven(_wrap(report_task), fake_clock, step=0.05)

    start = fake_clock.now()
    report = asyncio.run(main())
    assert report.outcomes[job.id].status == "ok"
    beat = world.rows()[0]["heartbeat_at"]
    assert beat is not None and beat - start >= timedelta(seconds=800)  # kept fresh all along


def test_daemon_fires_an_every_30m_job_twice_in_an_hour(
    scope: Scope, fake_clock: FakeClock
) -> None:
    agent = Agent()
    world, ticker = setup(scope, agent)
    start = fake_clock.now()
    world.add(next_run_at=start + timedelta(minutes=30))

    async def main() -> None:
        stop = asyncio.Event()
        daemon = asyncio.ensure_future(ticker.run_daemon(stop))
        # An interval steps from the end of the last run (drift kept, P14-02), so the second
        # slot lands a little after the hour; the 60 s tick fires it within the next minute.
        while fake_clock.now() < start + timedelta(minutes=61, seconds=30):
            await asyncio.wait({daemon}, timeout=0.0005)
            fake_clock.advance(10)
        stop.set()
        await run_driven(_wrap(daemon), fake_clock, step=1.0)

    asyncio.run(main())
    assert len(agent.calls) == 2
    slots = [row["scheduled_for"] for row in world.rows()]
    assert slots[0] == start + timedelta(minutes=30)
    drift = slots[1] - (start + timedelta(minutes=60))
    assert timedelta(0) <= drift < timedelta(minutes=1)


async def _wrap(task: asyncio.Future[Any]) -> Any:
    return await task


def test_daemon_drains_then_cancels(scope: Scope, fake_clock: FakeClock) -> None:
    agent = Agent(hold=True)
    world, ticker = setup(scope, agent)
    job = due_job(world, fake_clock.now())

    async def main() -> None:
        stop = asyncio.Event()
        daemon = asyncio.ensure_future(ticker.run_daemon(stop))
        for _ in range(200):
            if agent.calls:
                break
            await checkpoint()
        stop.set()
        await run_driven(_wrap(daemon), fake_clock, step=1.0)

    asyncio.run(main())
    assert agent.calls == [job.id]
    outcome = world.finished.outcomes[0]
    assert outcome.status == "failed" and outcome.error == "cancelled" and not outcome.deliver
    assert world.rows()[0]["state"] == "failed"


def test_claim_that_cannot_be_written_fires_nothing(
    real: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent()
    world, ticker = setup(real, agent)
    due_job(world, now_of(real))

    def refuse(*_args: Any, **_kw: Any) -> Any:
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(world.ports.store, "edit", refuse)
    report = asyncio.run(ticker.tick())
    assert report.status == "ran" and report.dispatched == [] and agent.calls == []
    assert world.rows() == []
