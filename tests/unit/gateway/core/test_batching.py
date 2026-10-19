"""P15-12: quiet-window batching (split pastes and busy text) on the fake clock."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.core.batching import Batcher, TextBatchPolicy, merge_events, merge_text
from indus.gateway.core.events import Attachment, MessageKind
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn
from tests.unit.gateway.core._kit import build_kit, dm, model_inputs


def run(coro: Any) -> None:
    asyncio.run(coro)


def make(
    clock: FakeClock, *, quiet: float = 0.3, cap: float | None = None
) -> tuple[Batcher[str], list[tuple[str, str]]]:
    flushed: list[tuple[str, str]] = []

    async def flush(key: str, value: str) -> None:
        flushed.append((key, value))

    batcher: Batcher[str] = Batcher(
        clock, flush=flush, merge=merge_text, delay_for=lambda _last: quiet, cap=cap
    )
    return batcher, flushed


def test_items_inside_the_quiet_window_merge_into_one_flush(fake_clock: FakeClock) -> None:
    async def main() -> None:
        batcher, flushed = make(fake_clock)
        batcher.add("k", "one")
        await fake_clock.advance_and_settle(0.2)
        batcher.add("k", "two")
        await fake_clock.advance_and_settle(0.2)
        batcher.add("k", "three")
        await fake_clock.advance_and_settle(0.29)
        assert flushed == []  # each piece restarted the window
        await fake_clock.advance_and_settle(0.02)
        assert flushed == [("k", "one\ntwo\nthree")]
        assert batcher.pending("k") is None

    run(main())


def test_keys_batch_independently(fake_clock: FakeClock) -> None:
    async def main() -> None:
        batcher, flushed = make(fake_clock)
        batcher.add("a", "x")
        await fake_clock.advance_and_settle(0.2)
        batcher.add("b", "y")
        await fake_clock.advance_and_settle(0.11)
        assert flushed == [("a", "x")]
        await fake_clock.advance_and_settle(0.2)
        assert flushed == [("a", "x"), ("b", "y")]

    run(main())


def test_the_hard_cap_bounds_the_total_wait(fake_clock: FakeClock) -> None:
    async def main() -> None:
        batcher, flushed = make(fake_clock, quiet=0.35, cap=1.0)
        for n in range(5):
            batcher.add("k", f"p{n}")
            await fake_clock.advance_and_settle(0.3)  # never quiet for 0.35 s
            if flushed:
                break
        assert flushed and flushed[0][1].startswith("p0")
        assert fake_clock.monotonic() <= 1.2

    run(main())


def test_discard_and_flush_now(fake_clock: FakeClock) -> None:
    async def main() -> None:
        batcher, flushed = make(fake_clock)
        batcher.add("k", "dropped")
        assert batcher.discard("k") == "dropped"
        await fake_clock.advance_and_settle(1.0)
        assert flushed == []
        batcher.add("k", "now")
        assert await batcher.flush_now("k") is True
        assert flushed == [("k", "now")]
        await batcher.aclose()

    run(main())


def test_a_chunk_near_the_split_threshold_extends_the_window() -> None:
    policy = TextBatchPolicy()
    assert policy.delay_for(10) == pytest.approx(0.3)
    assert policy.delay_for(3899) == pytest.approx(0.3)
    assert policy.delay_for(3900) == pytest.approx(1.0)  # within 100 units of 4,000
    assert policy.delay_for(4096) == pytest.approx(1.0)
    small = TextBatchPolicy.from_extra({}, split_threshold=2000)
    assert small.delay_for(1950) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("extra", "quiet", "split"),
    [
        ({}, 0.3, 1.0),
        ({"text_batch_delay_seconds": 0.5}, 0.5, 1.0),
        ({"text_batch_delay_seconds": 9}, 2.0, 2.0),  # ceiling, and split never below quiet
        ({"text_batch_split_delay_seconds": 9}, 0.3, 4.0),
        ({"text_batch_delay_seconds": -1, "text_batch_split_delay_seconds": "x"}, 0.3, 1.0),
        ({"text_batch_delay_seconds": math.nan}, 0.3, 1.0),
        ({"text_batch_delay_seconds": True}, 0.3, 1.0),
    ],
)
def test_configured_windows_are_clamped(extra: dict[str, Any], quiet: float, split: float) -> None:
    policy = TextBatchPolicy.from_extra(extra)
    assert policy.quiet == pytest.approx(quiet)
    assert policy.split_quiet == pytest.approx(split)


def test_merge_events_joins_text_and_media() -> None:
    first = dm("caption one")
    photo = dm("", kind=MessageKind.PHOTO, media=(Attachment(ref="/tmp/a.jpg"),))
    merged = merge_events(first, photo)
    assert merged.text == "caption one"
    assert merged.kind is MessageKind.PHOTO
    assert [m.ref for m in merged.media] == ["/tmp/a.jpg"]
    again = merge_events(merged, dm("more"))
    assert again.text == "caption one\nmore" and again.kind is MessageKind.PHOTO
    assert again.metadata["merged"] == 3
    assert again.message_id == first.message_id


# -- in the intake ---------------------------------------------------------------------------------


def test_three_split_pastes_become_one_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            text_turn("got it all"),
            batching=lambda _platform: TextBatchPolicy(),
        )
        try:
            for n, piece in enumerate(("part one", "part two", "part three")):
                await kit.pipeline.accept(dm(piece, message_id=str(n)))
                await fake_clock.advance_and_settle(0.1)
            await kit.until(lambda: kit.wire())
            assert model_inputs(kit.server) == ["part one\npart two\npart three"]
            assert kit.wire() == ["got it all"]
        finally:
            await kit.close()

    run(main())


def test_a_near_threshold_paste_waits_longer_for_its_continuation(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            text_turn("whole paste"),
            batching=lambda _platform: TextBatchPolicy(),
        )
        try:
            long_piece = "x" * 3950
            await kit.pipeline.accept(dm(long_piece, message_id="1"))
            await fake_clock.advance_and_settle(0.6)  # past 0.3 s, inside 1.0 s
            assert kit.pipeline._paste.pending(kit.key()) is not None  # pyright: ignore[reportPrivateUsage]
            await kit.pipeline.accept(dm("tail", message_id="2"))
            await kit.until(lambda: kit.wire())
            assert model_inputs(kit.server) == [f"{long_piece}\ntail"]
        finally:
            await kit.close()

    run(main())


def test_commands_are_never_batched(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope, fake_clock, tmp_path, batching=lambda _platform: TextBatchPolicy()
        )
        try:
            await kit.pipeline.accept(dm("/version"))
            await kit.until(lambda: kit.wire())
            assert kit.wire()[0].startswith("Indus Agent ")
        finally:
            await kit.close()

    run(main())
