"""Shared fixtures for backend unit tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterator

import pytest

from indus.backends.wait import FOREGROUND
from tests.fakes.clock import FakeClock


@pytest.fixture(autouse=True)
def open_fence() -> Iterator[None]:
    """Every test starts and ends with the process-wide exit fence open."""
    FOREGROUND.reset_for_tests()
    yield
    FOREGROUND.reset_for_tests()


async def drive[T](
    clock: FakeClock, work: Awaitable[T], *, step: float = 0.05, max_steps: int = 20_000
) -> T:
    """Run ``work`` while moving ``clock`` forward ``step`` seconds at a time.

    Between steps the loop gets a short real wait so worker threads (output readers) can hand
    their results back.
    """
    task = asyncio.ensure_future(work)
    for _ in range(max_steps):
        await asyncio.wait({task}, timeout=0.002)
        if task.done():
            return task.result()
        clock.advance(step)
    task.cancel()
    raise AssertionError("the work did not finish")
