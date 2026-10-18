"""Summary failure cooldowns and durable counters (P08-05, R01.9)."""

from __future__ import annotations

import asyncio

import pytest

from indus.core.compression.anchor import SessionBlobStore
from indus.core.compression.cooldown import CooldownBook, MemoryCounters, StoreCounters
from indus.core.compression.summarize import FailureKind
from indus.foundation.scope import Scope
from indus.state import compression, registry, sessions
from tests.fakes.clock import FakeClock


def book(clock: FakeClock, **kw: object) -> CooldownBook:
    return CooldownBook(MemoryCounters(), clock, **kw)  # type: ignore[arg-type]


def fail(b: CooldownBook, kind: FailureKind) -> tuple[float, bool]:
    decision = asyncio.run(b.failure(kind, f"{kind} happened"))
    return decision.cooldown_s, decision.commit_fallback


def test_ladder_per_failure_class(fake_clock: FakeClock) -> None:
    b = book(fake_clock)
    # stalls climb 60 → 300 → 900, never below timeout_s (120); the second commits the fallback
    assert fail(b, "stall") == (120.0, False)
    assert fail(b, "stall") == (300.0, True)
    assert fail(b, "stall") == (900.0, True)
    assert fail(b, "stall") == (900.0, True)
    quick = book(fake_clock, timeout_s=30.0)
    assert fail(quick, "stall") == (60.0, False)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("decode", (30.0, False)),
        ("empty", (30.0, False)),
        ("access", (60.0, False)),
        ("error", (60.0, True)),
        ("unavailable", (600.0, True)),
        ("truncated", (120.0, False)),
    ],
)
def test_flat_and_other_classes(
    fake_clock: FakeClock, kind: FailureKind, expected: tuple[float, bool]
) -> None:
    assert fail(book(fake_clock), kind) == expected


def test_truncation_climbs_its_own_rung(fake_clock: FakeClock) -> None:
    b = book(fake_clock)
    assert fail(b, "truncated") == (120.0, False)
    assert fail(b, "truncated") == (300.0, False)
    assert fail(b, "stall") == (120.0, False)  # the stall ladder starts at its own first rung
    assert fail(b, "truncated") == (900.0, False)


def test_three_overloads_commit_the_fallback(fake_clock: FakeClock) -> None:
    b = book(fake_clock)
    assert fail(b, "overload") == (60.0, False)
    assert fail(b, "overload") == (60.0, False)
    assert fail(b, "overload") == (60.0, True)
    asyncio.run(b.success(fallback=True))
    counters = asyncio.run(b.read())
    assert counters.overload_streak == 0 and counters.fallback_streak == 1


def test_abort_on_summary_failure_never_commits(fake_clock: FakeClock) -> None:
    b = book(fake_clock, abort_on_failure=True)
    for kind in ("stall", "stall", "overload", "overload", "overload", "error", "unavailable"):
        assert fail(b, kind)[1] is False  # type: ignore[arg-type]


def test_cooldown_runs_on_the_clock_and_manual_clears(fake_clock: FakeClock) -> None:
    b = book(fake_clock)
    fail(b, "stall")
    fail(b, "stall")
    fail(b, "stall")
    assert asyncio.run(b.remaining()) == pytest.approx(900.0)
    fake_clock.advance(400)
    assert asyncio.run(b.remaining()) == pytest.approx(500.0)
    fail(b, "decode")  # a shorter cooldown never shortens a longer live one
    assert asyncio.run(b.remaining()) == pytest.approx(500.0)
    asyncio.run(b.clear())
    assert asyncio.run(b.remaining()) == 0.0
    fake_clock.advance(10_000)
    assert asyncio.run(b.remaining()) == 0.0


def test_success_resets_the_rungs(fake_clock: FakeClock) -> None:
    b = book(fake_clock)
    fail(b, "stall")
    fail(b, "truncated")
    asyncio.run(b.success(fallback=False))
    counters = asyncio.run(b.read())
    assert (counters.stall_rung, counters.truncated_rung, counters.fallback_streak) == (0, 0, 0)
    assert fail(b, "stall") == (120.0, False)


def test_counters_survive_a_restart(scope: Scope, fake_clock: FakeClock) -> None:
    handle = registry.open_unshared(scope)
    try:
        sessions.create(handle, "s1", "cli")
        blobs = SessionBlobStore(handle)
        first = CooldownBook(StoreCounters(handle, "s1", blobs), fake_clock)
        assert fail(first, "stall") == (120.0, False)
        asyncio.run(first.set_ineffective(2))
        stored = compression.active_cooldown(handle, "s1")
        assert stored is not None and stored.ineffective_count == 2
        assert stored.cooldown_error == "stall happened"
        again = CooldownBook(StoreCounters(handle, "s1", blobs), fake_clock)
        assert fail(again, "stall") == (300.0, True)  # the ladder resumes, not restarts
        counters = asyncio.run(again.read())
        assert counters.stall_rung == 2 and counters.ineffective_count == 2
    finally:
        handle.close()
