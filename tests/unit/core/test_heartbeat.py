"""Session heartbeats (P10-26): grammar, firing on idle sessions only, pause/resume, restart."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from indus.core import heartbeat as hb
from indus.core.session_scheduler import MemoryMeta, SessionScheduler
from indus.protocol.events import HeartbeatFired
from tests.fakes.clock import FakeClock
from tests.unit.core._session_host import FakeHost


def make(
    clock: FakeClock, meta: MemoryMeta | None = None, host: FakeHost | None = None
) -> tuple[hb.HeartbeatDriver, FakeHost, MemoryMeta, SessionScheduler]:
    host = host if host is not None else FakeHost()
    meta = meta if meta is not None else MemoryMeta()
    driver = hb.HeartbeatDriver(host, meta, clock)
    return driver, host, meta, SessionScheduler(host, meta, clock, [driver])


async def stored(meta: MemoryMeta, hb_id: str = "h1") -> tuple[hb.HeartbeatState, float | None]:
    value, due = meta.rows[("s1", hb.NAMESPACE, hb_id)]
    state = hb.HeartbeatState.from_json(value)
    assert state is not None
    return state, due


TOO_OFTEN = "A heartbeat runs at most once a minute; {shown} is too often (at least 60 s)."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("every 90s look", (90, "look")),
        ("90s look", (90, "look")),
        ("every 10 minutes check mail", (600, "check mail")),
        ("every 2h sum up", (7200, "sum up")),
        ("1d tidy", (86400, "tidy")),
        ("every 30s look", TOO_OFTEN.format(shown="30s")),
        ("every 59 s look", TOO_OFTEN.format(shown="59s")),
        ("every 5m", "Say what the heartbeat should do. " + hb.USAGE),
        ("every 5 weeks look", "Start with how often, e.g. every 10m. " + hb.USAGE),
        ("look at things", "Start with how often, e.g. every 10m. " + hb.USAGE),
    ],
)
def test_grammar(text: str, expected: object) -> None:
    assert hb.parse_heartbeat_args(text) == expected


def test_fires_at_the_due_tick_on_an_idle_session(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)

    async def main() -> None:
        reply = await driver.command("s1", "every 2m check the inbox")
        assert reply.lines[0] == (
            "Heartbeat h1 set: every 2m, while the session is idle: check the inbox"
        )
        assert await scheduler.poll_once() == 0
        fake_clock.advance(119)
        assert await scheduler.poll_once() == 0
        fake_clock.advance(1)
        assert await scheduler.poll_once() == 1
        await host.end_turn("s1", "nothing new")
        assert await scheduler.poll_once() == 0
        fake_clock.advance(120)
        assert await scheduler.poll_once() == 1

    asyncio.run(main())
    first = host.texts()[0]
    assert first.startswith("[Heartbeat h1 — a recurring instruction, every 2m]\ncheck the inbox")
    assert "one short line" in first
    fired = host.of_type(HeartbeatFired)
    assert [(e.heartbeat_id, e.fire_count) for e in fired] == [("h1", 1), ("h1", 2)]
    assert [o for _s, _t, o in host.injected] == ["heartbeat", "heartbeat"]


def test_busy_skips_then_fires_when_idle(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)

    async def main() -> list[Any]:
        await driver.command("s1", "every 1m x")
        fake_clock.advance(60)
        host.submit("s1")  # a turn is running
        skipped = await scheduler.poll_once()
        fake_clock.advance(5)
        still = await scheduler.poll_once()
        await host.end_turn("s1", "answer")
        fake_clock.advance(5)
        fired = await scheduler.poll_once()
        return [skipped, still, fired, await stored(meta)]

    skipped, still, fired, (state, due) = asyncio.run(main())
    assert (skipped, still, fired) == (0, 0, 1)
    assert state.fire_count == 1
    # missed ticks collapse: the next one is a full interval after this fire
    assert due == state.last_fired_at + 60


def test_a_refused_injection_does_not_count(fake_clock: FakeClock) -> None:
    driver, host, meta, _scheduler = make(fake_clock)

    async def main() -> tuple[hb.HeartbeatState, float | None]:
        await driver.command("s1", "every 1m x")
        fake_clock.advance(60)
        (entry,) = await meta.due(hb.NAMESPACE, fake_clock.now().timestamp())
        host.busy.add("s1")  # a turn started between the check and the fire
        assert not await driver.fire(entry, fake_clock.now().timestamp())
        return await stored(meta)

    state, due = asyncio.run(main())
    assert state.fire_count == 0 and due == state.created_at + 60


def test_pause_and_resume(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)

    async def main() -> list[Any]:
        await driver.command("s1", "every 1m x")
        paused = await driver.command("s1", "pause")
        fake_clock.advance(600)
        quiet = await scheduler.poll_once()
        resumed = await driver.command("s1", "resume h1")
        now_fire = await scheduler.poll_once()
        fake_clock.advance(60)
        later = await scheduler.poll_once()
        listed = await driver.command("s1", "list")
        stopped = await driver.command("s1", "stop")
        return [paused, quiet, resumed, now_fire, later, listed, stopped]

    paused, quiet, resumed, now_fire, later, listed, stopped = asyncio.run(main())
    assert paused.lines == ("Heartbeat h1 paused.",)
    assert quiet == 0
    assert resumed.lines == ("Heartbeat h1 active, next in 1m.",)
    assert now_fire == 0  # a stale beat does not fire the moment it resumes
    assert later == 1
    assert listed.lines == ("h1 [active] every 1m, next in 1m, fired 1×: x",)
    assert stopped.lines == ("Heartbeat h1 stopped.",)
    assert asyncio.run(driver.command("s1", "list")).lines[0] == "No heartbeats in this session."


def test_restart_restores_and_counts_from_the_last_fire(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)

    async def main() -> list[Any]:
        await driver.command("s1", "every 10m x")
        fake_clock.advance(600)
        await scheduler.poll_once()
        fired_at = fake_clock.now().timestamp()
        # a new process: the stored due time is lost, restore recomputes it from the last fire
        value, _due = meta.rows[("s1", hb.NAMESPACE, "h1")]
        meta.rows[("s1", hb.NAMESPACE, "h1")] = (value, None)
        host2 = FakeHost()
        driver2, _h, _m, scheduler2 = make(fake_clock, meta=meta, host=host2)
        restored = await driver2.restore("s1")
        fake_clock.advance(300)
        early = await scheduler2.poll_once()
        fake_clock.advance(300)
        on_time = await scheduler2.poll_once()
        return [fired_at, restored, early, on_time, host2]

    fired_at, restored, early, on_time, host2 = asyncio.run(main())
    assert restored == 1 and (early, on_time) == (0, 1)
    assert host2.of_type(HeartbeatFired)[0].fire_count == 2
    state, due = asyncio.run(stored(meta))
    assert state.last_fired_at == fired_at + 600 and due == fired_at + 1200


def test_sessions_not_attached_here_are_left_alone(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)

    async def main() -> int:
        await driver.command("other", "every 1m x")
        fake_clock.advance(120)
        return await scheduler.poll_once()

    assert asyncio.run(main()) == 0 and host.injected == []


def test_one_fire_per_session_per_poll(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)

    async def main() -> int:
        await driver.command("s1", "every 1m a")
        await driver.command("s1", "every 1m b")
        fake_clock.advance(60)
        return await scheduler.poll_once()

    assert asyncio.run(main()) == 1 and len(host.injected) == 1
