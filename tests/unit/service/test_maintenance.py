"""The service's maintenance timer (P09-18): first tick after 90 s, then hourly."""

from __future__ import annotations

import asyncio

from indus.foundation.scope import Scope
from indus.learning.curator.lifecycle import curator_check
from indus.learning.curator.state import CuratorState
from indus.service.maintenance import MaintenanceTimer, learning_timer, profile_idle_s
from tests.fakes.clock import FakeClock


def test_first_tick_after_90s_then_hourly(fake_clock: FakeClock) -> None:
    seen: list[float] = []

    async def scenario() -> None:
        timer = MaintenanceTimer(fake_clock, in_thread=False)
        timer.register("probe", lambda: seen.append(fake_clock.monotonic()))
        task = asyncio.create_task(timer.run())
        await fake_clock.run_until_idle()
        await fake_clock.advance_and_settle(89)
        assert seen == []
        await fake_clock.advance_and_settle(1)
        assert len(seen) == 1
        await fake_clock.advance_and_settle(3599)
        assert len(seen) == 1
        await fake_clock.advance_and_settle(1)
        assert len(seen) == 2
        timer.stop()
        await fake_clock.advance_and_settle(3600)
        await task

    asyncio.run(scenario())
    assert seen[1] - seen[0] == 3600


def test_failing_task_does_not_stop_others(fake_clock: FakeClock) -> None:
    ran: list[str] = []

    def broken() -> None:
        raise RuntimeError("boom")

    async def scenario() -> None:
        timer = MaintenanceTimer(fake_clock, in_thread=False)
        timer.register("broken", broken)
        remove = timer.register("ok", lambda: ran.append("ok"))
        await timer.tick()
        remove()
        await timer.tick()
        assert timer.names == ["broken"] and timer.ticks == 2

    asyncio.run(scenario())
    assert ran == ["ok"]


def test_curator_check_task(scope: Scope, fake_clock: FakeClock) -> None:
    check = curator_check(scope, idle_s=lambda: 10 * 3600.0)

    async def scenario() -> None:
        timer = MaintenanceTimer(fake_clock)  # in a worker thread, as the service runs it
        timer.register("curator", check)
        await timer.tick()

    asyncio.run(scenario())
    assert CuratorState(scope).load()["last_run_at"] == fake_clock.now().isoformat()


def test_learning_timer_and_idle_measure(scope: Scope, fake_clock: FakeClock) -> None:
    from indus.state import registry, sessions

    timer = learning_timer(scope, idle_s=lambda: 3 * 3600.0)
    assert timer.names == ["curator"]
    store = registry.acquire(scope)
    try:
        assert profile_idle_s(store, fake_clock) is None  # nothing stored yet
        assert profile_idle_s(None, fake_clock) is None
        sessions.create(store, "s1", "cli")
        store.write(
            lambda conn: conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                ("s1", "user", "hi", fake_clock.now().timestamp()),
            )
        )
        fake_clock.advance(7200)
        assert profile_idle_s(store, fake_clock) == 7200
    finally:
        registry.release(store)

    async def scenario() -> None:
        await timer.tick()

    asyncio.run(scenario())
    assert CuratorState(scope).load()["last_run_at"] is not None  # first look recorded


def test_service_runs_the_timer_while_open(scope: Scope, fake_clock: FakeClock) -> None:
    from indus.service.session_service import SessionService
    from tests.fakes.agent import fake_options
    from tests.fakes.model import FakeModelServer, fake_route, script_of

    seen: list[str] = []
    server = FakeModelServer(script_of(), clock=fake_clock)
    timer = MaintenanceTimer(fake_clock, in_thread=False)
    timer.register("probe", lambda: seen.append("tick"))
    service = SessionService(
        scope,
        fake_route(server),
        toolsets=[],
        options=fake_options(server, session_id=None),
        maintenance=timer,
    )

    async def scenario() -> None:
        await fake_clock.advance_and_settle(600)
        assert seen == []  # not started before the first session
        await service.create()
        await fake_clock.run_until_idle()
        await fake_clock.advance_and_settle(90)
        assert seen == ["tick"]
        await service.aclose()
        await fake_clock.advance_and_settle(3600)
        assert seen == ["tick"]

    asyncio.run(scenario())
