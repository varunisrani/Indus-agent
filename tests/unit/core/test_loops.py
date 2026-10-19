"""In-session loops (P10-25): grammar, self-paced cadence, the stop marker, --times, --until,
the tick budget, busy sessions and the scheduler."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from indus.core import loops
from indus.core.session_scheduler import MemoryMeta, SessionScheduler
from indus.protocol.events import LoopUpdate, Notice
from tests.fakes.clock import FakeClock
from tests.unit.core._session_host import FakeHost
from tests.unit.core.test_goals import DONE, FakeJudge


def make(
    clock: FakeClock, *, judge: FakeJudge | None = None, max_ticks: int = 100
) -> tuple[loops.LoopDriver, FakeHost, MemoryMeta, SessionScheduler]:
    host, meta = FakeHost(), MemoryMeta()
    driver = loops.LoopDriver(host, meta, clock, judge=judge, max_ticks=max_ticks)
    driver.attach()
    return driver, host, meta, SessionScheduler(host, meta, clock, [driver])


async def state_of(meta: MemoryMeta, loop_id: str = "l1") -> loops.LoopState:
    found = loops.LoopState.from_json(await meta.get("s1", loops.NAMESPACE, loop_id))
    assert found is not None
    return found


@pytest.mark.parametrize(
    ("token", "seconds"),
    [
        ("30s", 30),
        ("5m", 300),
        ("2h", 7200),
        ("1h30m", 5400),
        ("1m5s", 65),
        ("1H", 3600),
        ("0s", None),
        ("10", None),
        ("five", None),
        ("m5", None),
        ("", None),
    ],
)
def test_interval_tokens(token: str, seconds: int | None) -> None:
    assert loops.parse_interval(token) == seconds


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5m check the deploy", loops.LoopSpec("check the deploy", 300)),
        ("10s poll", loops.LoopSpec("poll", 30, asked_s=10)),
        ("auto keep fixing", loops.LoopSpec("keep fixing", None)),
        ("every 1h30m tidy up", loops.LoopSpec("tidy up", 5400)),
        ("2m poll CI --times 3", loops.LoopSpec("poll CI", 120, times=3)),
        (
            '5m watch the queue --until "queue is empty"',
            loops.LoopSpec("watch the queue", 300, until="queue is empty"),
        ),
        ("5m watch --until it is green", loops.LoopSpec("watch", 300, until="it is green")),
        ("3 things to do", "Start with an interval (30s, 5m, 2h, 1h30m) or auto. " + loops.USAGE),
        ("5m", "Say what to repeat. " + loops.USAGE),
        ("5m x --times 0", "--times needs a whole number above 0, not '0'"),
    ],
)
def test_parse_loop_args(text: str, expected: object) -> None:
    assert loops.parse_loop_args(text) == expected


def test_floor_is_configurable_but_never_below_five() -> None:
    assert loops.parse_loop_args("10s x", floor_s=10) == loops.LoopSpec("x", 10)
    assert loops.parse_loop_args("2s x", floor_s=1) == loops.LoopSpec("x", 5, asked_s=2)


@pytest.mark.parametrize(
    ("reply", "ends"),
    [
        ("All done.\nLOOP_COMPLETE", True),
        ("done\n  loop_complete!  \n\n", True),
        ("finished\nLOOP_COMPLETE.", True),
        ("I will say LOOP_COMPLETE when it is done.", False),
        ("LOOP_COMPLETE\nbut one more thing", False),
        ("LOOP_COMPLETE now", False),
        ("", False),
    ],
)
def test_marker_must_be_the_last_line(reply: str, ends: bool) -> None:
    assert loops.ends_with_marker(reply) is ends


def test_first_run_is_due_at_once_and_fires_when_idle(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)

    async def main() -> None:
        reply = await driver.command("s1", "5m check the deploy")
        assert reply.lines[0] == "Loop l1 set (every 5m): check the deploy"
        assert await scheduler.poll_once() == 1
        assert await scheduler.poll_once() == 0  # in flight: never twice
        await host.end_turn("s1", "deploy is fine")
        assert await scheduler.poll_once() == 0
        fake_clock.advance(299)
        assert await scheduler.poll_once() == 0
        fake_clock.advance(1)
        assert await scheduler.poll_once() == 1

    asyncio.run(main())
    first, second = host.texts()
    assert first.startswith("[Loop l1, run 1 — every 5m]\ncheck the deploy")
    assert loops.MARKER in first
    assert second.startswith("[Loop l1, run 2 — every 5m]")
    assert [o for _s, _t, o in host.injected] == ["loop", "loop"]


def test_self_paced_doubles_while_unchanged_and_snaps_back(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)
    delays: list[float] = []

    async def run_once(reply: str) -> None:
        assert await scheduler.poll_once() == 1
        await host.end_turn("s1", reply)
        state = await state_of(meta)
        delays.append(state.delay_s)
        fake_clock.advance(state.delay_s)

    async def main() -> None:
        await driver.command("s1", "auto watch the build")
        for reply in ["same", "same", "  SAME ", "different", "different"]:
            await run_once(reply)

    asyncio.run(main())
    assert delays == [60, 120, 240, 60, 120]


def test_self_paced_caps_at_fifteen_minutes(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)

    async def main() -> float:
        await driver.command("s1", "auto x")
        for _ in range(8):
            await scheduler.poll_once()
            await host.end_turn("s1", "nothing new")
            fake_clock.advance((await state_of(meta)).delay_s)
        return (await state_of(meta)).delay_s

    assert asyncio.run(main()) == loops.AUTO_CAP_S


def test_marker_on_its_own_line_ends_the_loop(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)

    async def main() -> None:
        await driver.command("s1", "1m x")
        await scheduler.poll_once()
        await host.end_turn("s1", "I will print LOOP_COMPLETE later")
        assert (await state_of(meta)).status == "active"
        fake_clock.advance(60)
        await scheduler.poll_once()
        await host.end_turn("s1", "All finished.\nLOOP_COMPLETE")
        fake_clock.advance(600)
        assert await scheduler.poll_once() == 0

    asyncio.run(main())
    state = asyncio.run(state_of(meta))
    assert state.status == "done" and state.ticks == 2
    assert host.of_type(LoopUpdate)[-1].status == "done"
    assert "done" in host.of_type(Notice)[-1].text


def test_times_stops_after_n_runs(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)

    async def main() -> None:
        await driver.command("s1", "30s x --times 3")
        for _ in range(5):
            await scheduler.poll_once()
            await host.end_turn("s1", f"r{len(host.injected)}")
            fake_clock.advance(30)

    asyncio.run(main())
    assert len(host.injected) == 3
    assert asyncio.run(state_of(meta)).status == "done"
    assert "run 3 of 3" in host.texts()[-1]


def test_busy_session_delays_the_tick(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)

    async def main() -> None:
        await driver.command("s1", "1m x")
        host.busy.add("s1")
        assert await scheduler.poll_once() == 0
        host.busy.clear()
        host.waiting.add("s1")  # a queued message also counts as busy
        assert await scheduler.poll_once() == 0
        host.waiting.clear()
        fake_clock.advance(5)
        assert await scheduler.poll_once() == 1

    asyncio.run(main())
    assert len(host.injected) == 1


def test_tick_budget_pauses_and_resume_goes_on(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock, max_ticks=2)

    async def main() -> list[Any]:
        await driver.command("s1", "30s x")
        for _ in range(3):
            await scheduler.poll_once()
            await host.end_turn("s1", "r")
            fake_clock.advance(30)
        paused = await state_of(meta)
        reply = await driver.command("s1", "resume")
        fake_clock.advance(5)
        fired = await scheduler.poll_once()
        return [paused, reply, fired]

    paused, reply, fired = asyncio.run(main())
    assert paused.status == "paused" and "2/2 runs used" in paused.reason
    assert reply.lines == ("Loop l1 resumed (every 30s).",)
    assert fired == 1 and len(host.injected) == 3


def test_until_judge_ends_the_loop(fake_clock: FakeClock) -> None:
    judge = FakeJudge([DONE])
    driver, host, meta, scheduler = make(fake_clock, judge=judge)

    async def main() -> None:
        await driver.command("s1", '1m watch CI --until "the build is green"')
        await scheduler.poll_once()
        await host.end_turn("s1", "build #4 passed")

    asyncio.run(main())
    assert judge.calls == [("the build is green", [], "build #4 passed")]
    state = asyncio.run(state_of(meta))
    assert state.status == "done" and "stop condition holds" in state.reason
    assert "The loop should stop once this holds: the build is green" in host.texts()[0]


def test_several_loops_list_and_stop(fake_clock: FakeClock) -> None:
    driver, _host, _meta, scheduler = make(fake_clock)

    async def main() -> list[Any]:
        await driver.command("s1", "5m a")
        await driver.command("s1", "auto b")
        listed = await driver.command("s1", "list")
        one = await driver.command("s1", "stop l1")
        missing = await driver.command("s1", "stop l9")
        rest = await driver.command("s1", "stop")
        empty = await driver.command("s1", "")
        return [listed, one, missing, rest, empty, await scheduler.poll_once()]

    listed, one, missing, rest, empty, fired = asyncio.run(main())
    assert listed.lines == (
        "l1 [active] every 5m, 0 run(s), budget 100, due now: a",
        "l2 [active] auto, now every 1m, 0 run(s), budget 100, due now: b",
    )
    assert one.lines == ("Loop l1 stopped.",)
    assert missing.lines == ("No loop l9 here. /loop list shows them.",)
    assert rest.lines == ("Loop l2 stopped.",)
    assert empty.lines[0] == "No loops in this session."
    assert fired == 0


def test_a_run_in_flight_at_a_restart_is_not_fired_again(fake_clock: FakeClock) -> None:
    driver, host, meta, scheduler = make(fake_clock)

    async def main() -> int:
        await driver.command("s1", "5m x")
        assert await scheduler.poll_once() == 1
        # the process stops mid-run; a new one starts over the same store
        host2 = FakeHost()
        driver2 = loops.LoopDriver(host2, meta, fake_clock)
        driver2.attach()
        scheduler2 = SessionScheduler(host2, meta, fake_clock, [driver2])
        assert await scheduler2.poll_once() == 0
        fake_clock.advance(300)
        return await scheduler2.poll_once()

    assert asyncio.run(main()) == 1


def test_scheduler_task_polls_on_the_clock(fake_clock: FakeClock) -> None:
    driver, host, _meta, scheduler = make(fake_clock)

    async def main() -> None:
        await driver.command("s1", "5m x")
        scheduler.start()
        await fake_clock.run_until_idle()
        assert host.injected == []
        await fake_clock.advance_and_settle(5.0)
        assert len(host.injected) == 1
        await scheduler.stop()
        assert not scheduler.running

    asyncio.run(main())


def test_an_active_goal_holds_the_loop(fake_clock: FakeClock) -> None:
    host, meta = FakeHost(), MemoryMeta()
    goal_on = {"s1"}

    async def held(session_id: str) -> bool:
        return session_id in goal_on

    driver = loops.LoopDriver(host, meta, fake_clock, held=held)
    scheduler = SessionScheduler(host, meta, fake_clock, [driver])

    async def main() -> tuple[int, int]:
        await driver.command("s1", "1m x")
        held_back = await scheduler.poll_once()
        goal_on.clear()
        return held_back, await scheduler.poll_once()

    assert asyncio.run(main()) == (0, 1)
