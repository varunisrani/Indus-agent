"""The shared per-chat send slot (P15-05, R06.13), on a fake clock."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from indus.gateway.core.send_slot import SendSlots
from tests.fakes.clock import FakeClock


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


def test_five_sends_to_one_chat_start_one_second_apart() -> None:
    async def main() -> list[float]:
        clock = _clock()
        slots = SendSlots(clock, spacing=1.0)
        starts: list[float] = []

        async def send() -> None:
            async with slots.hold("c1"):
                starts.append(clock.monotonic())

        tasks = [asyncio.ensure_future(send()) for _ in range(5)]
        await clock.run_until_idle()
        for _ in range(20):
            await clock.advance_and_settle(0.25)
        assert all(t.done() for t in tasks)
        return starts

    assert asyncio.run(main()) == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_interim_edit_while_a_send_waits_is_dropped_and_final_goes_through() -> None:
    async def main() -> None:
        clock = _clock()
        slots = SendSlots(clock, spacing=1.0)
        log: list[tuple[str, float]] = []

        async def send(name: str) -> None:
            async with slots.hold("c1"):
                log.append((name, clock.monotonic()))

        await send("first")
        waiting = asyncio.ensure_future(send("second"))  # must wait 1.0 s for spacing
        await clock.run_until_idle()
        assert slots.busy("c1")
        assert slots.try_interim("c1") is None  # interim edit skipped
        slots.mark("c1")  # the final edit is never gated
        log.append(("final", clock.monotonic()))
        await clock.advance_and_settle(1.0)
        await waiting
        assert [n for n, _ in log] == ["first", "final", "second"]
        assert log[1][1] == 0.0

    asyncio.run(main())


def test_interim_edit_needs_the_spacing_and_then_holds_the_slot() -> None:
    async def main() -> None:
        clock = _clock()
        slots = SendSlots(clock, spacing=1.0)
        async with slots.hold("c1"):
            assert slots.try_interim("c1") is None  # busy
        assert slots.try_interim("c1") is None  # spacing not passed
        await clock.advance_and_settle(1.0)
        held = slots.try_interim("c1")
        assert held is not None
        async with held:
            assert slots.busy("c1")
            assert slots.try_interim("c1") is None
        assert slots.try_interim("c1") is None  # the edit stamped the time

    asyncio.run(main())


def test_two_chats_do_not_block_each_other() -> None:
    async def main() -> None:
        clock = _clock()
        slots = SendSlots(clock, spacing=1.0)
        gate = asyncio.Event()

        async def slow() -> None:
            async with slots.hold("a"):
                await gate.wait()

        task = asyncio.ensure_future(slow())
        await clock.run_until_idle()
        assert slots.busy("a")
        async with slots.hold("b"):
            assert clock.monotonic() == 0.0
        assert slots.try_interim("c") is not None
        gate.set()
        await task

    asyncio.run(main())


def test_pace_spaces_chunks_inside_one_hold() -> None:
    async def main() -> list[float]:
        clock = _clock()
        slots = SendSlots(clock, spacing=1.0)
        starts: list[float] = []

        async def split_send() -> None:
            async with slots.hold("c"):
                starts.append(clock.monotonic())
                for _ in range(2):
                    await slots.pace("c")
                    starts.append(clock.monotonic())

        task = asyncio.ensure_future(split_send())
        await clock.run_until_idle()
        for _ in range(8):
            await clock.advance_and_settle(0.5)
        assert task.done()
        return starts

    assert asyncio.run(main()) == [0.0, 1.0, 2.0]


def test_zero_spacing_never_sleeps() -> None:
    async def main() -> None:
        clock = _clock()
        slots = SendSlots(clock)
        for _ in range(3):
            async with slots.hold("c"):
                pass
            assert slots.try_interim("c") is not None
        assert clock.pending == 0

    asyncio.run(main())


def test_idle_chats_are_pruned() -> None:
    async def main() -> None:
        clock = _clock()
        slots = SendSlots(clock)
        for n in range(600):
            async with slots.hold(str(n)):
                pass
        assert len(slots._chats) <= 300  # pyright: ignore[reportPrivateUsage]

    asyncio.run(main())


def test_negative_spacing_refused() -> None:
    with pytest.raises(ValueError):
        SendSlots(_clock(), spacing=-1)
