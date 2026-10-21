"""Shared pieces for the delivery tests: a store, a recording adapter, a ledgered sender."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from indus.foundation.scope import Scope
from indus.gateway.delivery.ledger import DeliveryLedger
from indus.gateway.outbound.sender import OutboundSender
from indus.state.delivery_ledger import Owner
from indus.state.registry import StoreHandle
from tests.fakes.clock import FakeClock, checkpoint, hold_for_off_loop
from tests.unit.gateway.outbound.recording import RecordingAdapter, recording_adapter

US: Owner = (4242, 777)


async def until(check: Callable[[], object], clock: FakeClock | None = None) -> None:
    """Let tasks and worker threads run (moving ``clock`` a little) until ``check()`` holds."""
    loop = asyncio.get_running_loop()
    end = loop.time() + 10.0
    while not check():
        if loop.time() > end:
            raise AssertionError("the condition never held")
        await checkpoint()
        idle = loop.create_future()
        await asyncio.wait({idle}, timeout=0.002)
        idle.cancel()
        if clock is not None:
            await hold_for_off_loop(clock)  # never move the clock under an awaited call
            clock.advance(0.01)


async def settle(rounds: int = 30) -> None:
    """Run for a while without moving any clock (for checks that something did not happen)."""
    loop = asyncio.get_running_loop()
    for _ in range(rounds):
        await checkpoint()
        idle = loop.create_future()
        await asyncio.wait({idle}, timeout=0.002)
        idle.cancel()


def ledgered(
    scope: Scope,
    store: StoreHandle,
    *,
    name: str = "telegram",
    max_len: int = 4096,
    owner: Owner = US,
    adapter: RecordingAdapter | None = None,
) -> tuple[RecordingAdapter, OutboundSender, DeliveryLedger]:
    clock = scope.clock
    assert isinstance(clock, FakeClock)
    port = adapter or recording_adapter(name=name, max_len=max_len, clock=clock)
    book = DeliveryLedger(store, clock=clock, owner=owner, is_alive=lambda _p, _s: False)
    sender = OutboundSender(port, scope=scope, platform=name, ledger=book, rng=lambda: 0.0)
    book.bind(lambda platform: sender if platform == name else None)
    return port, sender, book
