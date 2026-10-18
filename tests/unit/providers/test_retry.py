"""Retry-After parsing, jittered backoff and the retry loop (P03-16)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from indus.foundation.errors import ProviderHTTPError
from indus.providers.classify import ClassifiedError
from indus.providers.retry import (
    RetryFailed,
    RetryWait,
    backoff,
    clamp_max_retries,
    parse_retry_after,
    retry_call,
)
from tests.fakes.clock import FakeClock

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

PARSE_TABLE: list[tuple[str, Any, float | None]] = [
    ("number-int", 7, 7.0),
    ("number-text", "7", 7.0),
    ("number-float-text", " 2.5 ", 2.5),
    ("header-mixed-case", {"Retry-After": "12"}, 12.0),
    ("header-lower", {"retry-after": "3"}, 3.0),
    ("header-ms-wins", {"retry-after-ms": "1500", "retry-after": "9"}, 1.5),
    ("http-date", "Thu, 01 Jan 2026 09:00:45 GMT", 45.0),
    ("http-date-header", {"Retry-After": "Thu, 01 Jan 2026 09:01:00 GMT"}, 60.0),
    ("http-date-past", "Thu, 01 Jan 2026 08:59:00 GMT", 0.0),
    ("text-try-again-in", "Rate limit reached. Please try again in 20s.", 20.0),
    ("text-try-again-decimal", "Please try again in 6.5s", 6.5),
    ("text-try-again-ms", "try again in 450ms", 0.45),
    ("text-resets-in-hm-capped", "Your usage limit resets in 1h 5m.", 600.0),
    ("text-resets-in-m", "resets in 4 minutes", 240.0),
    ("text-retry-after-seconds", "Too many requests, retry after 3 seconds", 3.0),
    ("text-retry-after-bare", "please retry after 30", 30.0),
    ("text-available-again", "The model is busy; available again in 90s", 90.0),
    ("text-wait-minutes", "Please wait 2 minutes before sending more requests", 120.0),
    ("text-duration-only", "1m 30s", 90.0),
    ("json-retry-after", {"error": {"retry_after": 4}}, 4.0),
    ("json-retry-delay", {"error": {"details": [{"retryDelay": "20s"}]}}, 20.0),
    ("json-resets-in-seconds", {"resets_in_seconds": 42}, 42.0),
    ("json-retry-after-ms", {"retry_after_ms": 2500}, 2.5),
    ("json-text", '{"error": {"retry_delay": 8}}', 8.0),
    ("json-message", {"error": {"message": "slow down, try again in 5s"}}, 5.0),
    ("cap-9999", "9999", 600.0),
    ("negative", "-5", 0.0),
    ("none", None, None),
    ("empty", "", None),
    ("no-wait-named", "Internal server error", None),
    ("bool", True, None),
    ("unrelated-number", "error 500 from upstream", None),
]


@pytest.mark.parametrize(
    ("value", "expected"), [(v, e) for _, v, e in PARSE_TABLE], ids=[i for i, _, _ in PARSE_TABLE]
)
def test_parse_table(value: Any, expected: float | None) -> None:
    assert parse_retry_after(value, now=NOW) == expected


def test_parse_without_cap_gives_the_full_reset() -> None:
    assert parse_retry_after("resets in 1h 5m", cap=None) == 3900.0
    assert parse_retry_after("9999", cap=None) == 9999.0


def test_backoff_bounds_and_seeded_sequence() -> None:
    rng = random.Random(1234)
    waits = [backoff(n, rng=rng) for n in range(1, 9)]
    bases = [2, 4, 8, 16, 32, 60, 60, 60]
    for wait, base in zip(waits, bases, strict=True):
        assert base <= wait <= base * 1.5
    again = random.Random(1234)
    assert waits == [backoff(n, rng=again) for n in range(1, 9)]
    assert backoff(500, rng=random.Random(0)) <= 90.0


@pytest.mark.parametrize(
    ("raw", "expected"), [(3, 3), (1, 1), (0, 1), (-2, 1), ("5", 3), (None, 3), (True, 3)]
)
def test_clamp_max_retries(raw: object, expected: int) -> None:
    assert clamp_max_retries(raw) == expected


# -- the loop -------------------------------------------------------------------------------------


def verdict(reason: str, *, retryable: bool = True, retry_after: float | None = None) -> Any:
    def classify(err: BaseException) -> ClassifiedError:
        return ClassifiedError(
            reason=reason,  # type: ignore[arg-type]
            message=str(err),
            retryable=retryable,
            retry_after=retry_after,
        )

    return classify


class Flaky:
    """Fails ``failures`` times with ``error`` then returns "ok"; records call times."""

    def __init__(self, clock: FakeClock, failures: int, error: BaseException) -> None:
        self.clock = clock
        self.failures = failures
        self.error = error
        self.calls: list[float] = []

    async def __call__(self) -> str:
        self.calls.append(self.clock.monotonic())
        if len(self.calls) <= self.failures:
            raise self.error
        return "ok"


async def drive[T](clock: FakeClock, work: Awaitable[T], *, step: float = 0.2) -> T:
    task = asyncio.ensure_future(work)
    for _ in range(100_000):
        await clock.run_until_idle()
        if task.done():
            return task.result()
        clock.advance(step)
    raise AssertionError("work never finished")


def http_error(status: int = 503) -> ProviderHTTPError:
    return ProviderHTTPError("busy", status=status, body="Service Unavailable")


def run(coro_fn: Callable[[FakeClock], Coroutine[Any, Any, Any]]) -> Any:
    clock = FakeClock(NOW)
    return asyncio.run(coro_fn(clock))


def test_three_failures_then_success_makes_four_calls_with_computed_waits() -> None:
    async def scenario(clock: FakeClock) -> tuple[Flaky, list[float]]:
        flaky = Flaky(clock, 3, http_error())
        result = await drive(
            clock,
            retry_call(
                flaky,
                max_retries=3,
                clock=clock,
                classify=verdict("server_error"),
                rng=random.Random(7),
            ),
        )
        assert result == "ok"
        expected_rng = random.Random(7)
        return flaky, [backoff(n, rng=expected_rng) for n in (1, 2, 3)]

    flaky, expected = run(scenario)
    assert len(flaky.calls) == 4
    gaps = [b - a for a, b in zip(flaky.calls, flaky.calls[1:], strict=False)]
    for gap, wait in zip(gaps, expected, strict=True):
        assert wait <= gap < wait + 0.2 + 1e-9


def test_retry_after_used_instead_of_backoff() -> None:
    async def scenario(clock: FakeClock) -> Flaky:
        flaky = Flaky(clock, 1, http_error(429))
        await drive(
            clock,
            retry_call(
                flaky, max_retries=3, clock=clock, classify=verdict("rate_limit", retry_after=7.0)
            ),
        )
        return flaky

    flaky = run(scenario)
    assert len(flaky.calls) == 2
    assert 7.0 <= flaky.calls[1] - flaky.calls[0] < 7.2 + 1e-9


def test_real_classifier_reads_retry_after_header() -> None:
    error = ProviderHTTPError(
        "slow", status=429, body="Too Many Requests", headers={"Retry-After": "7"}
    )

    async def scenario(clock: FakeClock) -> Flaky:
        flaky = Flaky(clock, 1, error)
        await drive(clock, retry_call(flaky, max_retries=3, clock=clock))
        return flaky

    flaky = run(scenario)
    assert 7.0 <= flaky.calls[1] - flaky.calls[0] < 7.2 + 1e-9


@pytest.mark.parametrize(
    "reason", ["auth_permanent", "format_error", "content_policy", "model_not_found"]
)
def test_non_retryable_makes_one_call(reason: str) -> None:
    waits: list[RetryWait] = []

    async def scenario(clock: FakeClock) -> Flaky:
        flaky = Flaky(clock, 5, http_error(400))
        with pytest.raises(RetryFailed) as info:
            await drive(
                clock,
                retry_call(
                    flaky,
                    max_retries=3,
                    clock=clock,
                    classify=verdict(reason, retryable=False),
                    on_wait=waits.append,
                ),
            )
        assert info.value.classified.reason == reason
        assert info.value.attempts == 1
        return flaky

    assert len(run(scenario).calls) == 1
    assert waits == []


def test_exhausted_retries_flush_buffered_waits_and_carry_the_verdict() -> None:
    waits: list[RetryWait] = []

    async def scenario(clock: FakeClock) -> RetryFailed:
        flaky = Flaky(clock, 10, http_error())
        with pytest.raises(RetryFailed) as info:
            await drive(
                clock,
                retry_call(
                    flaky,
                    max_retries=2,
                    clock=clock,
                    classify=verdict("overloaded"),
                    on_wait=waits.append,
                    rng=random.Random(1),
                ),
            )
        assert len(flaky.calls) == 3
        return info.value

    failed = run(scenario)
    assert failed.classified.reason == "overloaded"
    assert isinstance(failed.error, ProviderHTTPError)
    assert failed.__cause__ is failed.error
    assert [w.attempt for w in waits] == [1, 2]
    assert not any(w.announced for w in waits)
    assert "overloaded" in waits[0].describe()


def test_buffered_waits_dropped_on_success() -> None:
    waits: list[RetryWait] = []

    async def scenario(clock: FakeClock) -> None:
        flaky = Flaky(clock, 2, http_error())
        await drive(
            clock,
            retry_call(
                flaky,
                max_retries=3,
                clock=clock,
                classify=verdict("server_error"),
                on_wait=waits.append,
            ),
        )

    run(scenario)
    assert waits == []


def test_long_wait_announced_immediately() -> None:
    waits: list[RetryWait] = []

    async def scenario(clock: FakeClock) -> None:
        flaky = Flaky(clock, 1, http_error(429))
        task = asyncio.ensure_future(
            retry_call(
                flaky,
                max_retries=3,
                clock=clock,
                classify=verdict("rate_limit", retry_after=120.0),
                on_wait=waits.append,
            )
        )
        await clock.run_until_idle()
        # The first call failed and the wait started: it is announced before any time passes.
        assert len(waits) == 1 and waits[0].announced and waits[0].seconds == 120.0
        assert waits[0].from_server
        await drive(clock, task, step=1.0)
        assert len(flaky.calls) == 2

    run(scenario)


def test_cancel_during_wait_exits_within_one_slice() -> None:
    async def scenario(clock: FakeClock) -> None:
        flaky = Flaky(clock, 5, http_error())
        task = asyncio.ensure_future(
            retry_call(
                flaky,
                max_retries=3,
                clock=clock,
                classify=verdict("server_error", retry_after=30.0),
            )
        )
        await clock.run_until_idle()
        assert clock.pending == 1  # parked on one 200 ms slice
        clock.advance(1.0)
        await clock.run_until_idle()
        task.cancel()
        await clock.run_until_idle()
        assert task.cancelled()
        assert clock.pending == 0
        assert len(flaky.calls) == 1

    run(scenario)


def test_bugs_are_not_retried_or_wrapped() -> None:
    async def scenario(clock: FakeClock) -> Flaky:
        flaky = Flaky(clock, 3, TypeError("bad argument"))
        with pytest.raises(TypeError):
            await drive(clock, retry_call(flaky, max_retries=3, clock=clock))
        return flaky

    assert len(run(scenario).calls) == 1


def test_transport_errors_are_retried_with_the_real_classifier() -> None:
    async def scenario(clock: FakeClock) -> Flaky:
        flaky = Flaky(clock, 2, httpx.ConnectError("[Errno 111] Connection refused"))
        assert await drive(clock, retry_call(flaky, max_retries=3, clock=clock)) == "ok"
        return flaky

    assert len(run(scenario).calls) == 3


def test_failing_wait_callback_does_not_break_the_loop() -> None:
    def broken(wait: RetryWait) -> None:
        raise RuntimeError("display gone")

    async def scenario(clock: FakeClock) -> None:
        flaky = Flaky(clock, 1, http_error(429))
        result = await drive(
            clock,
            retry_call(
                flaky,
                max_retries=1,
                clock=clock,
                classify=verdict("rate_limit", retry_after=90.0),
                on_wait=broken,
            ),
            step=1.0,
        )
        assert result == "ok"

    run(scenario)
