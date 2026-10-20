"""The single writer (P02-03): time budgets, jitter, I/O retry rule, checkpoints, async."""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from indus.foundation.errors import StoreLocked
from indus.state.errors import Bucket, classify
from indus.state.writer import (
    CHECKPOINT_EVERY,
    FAST_JITTER,
    OBSERVATION,
    ROUTINE,
    SLOW_AFTER,
    SLOW_JITTER,
    TRANSCRIPT,
    Budget,
    Writer,
)
from tests.fakes.clock import checkpoint
from tests.unit.state.conftest import AutoClock, FakeConnection, locked_error


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "w.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, x TEXT)")
    conn.close()
    return path


@pytest.fixture
def fake(db: Path) -> Iterator[FakeConnection]:
    conn = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    yield FakeConnection(conn)
    conn.close()


def _writer(fake: FakeConnection, clock: AutoClock, **kwargs: object) -> Writer:
    return Writer(
        Path("w.db"),
        cast(sqlite3.Connection, fake),
        clock=clock,
        rng=random.Random(7),
        **kwargs,  # type: ignore[arg-type]
    )


def _insert(value: str):  # type: ignore[no-untyped-def]
    def fn(conn: sqlite3.Connection) -> int:
        return int(conn.execute("INSERT INTO t(x) VALUES (?)", (value,)).lastrowid or 0)

    return fn


def _rows(fake: FakeConnection) -> list[str]:
    return [r[0] for r in fake.real.execute("SELECT x FROM t ORDER BY id").fetchall()]


def _check_bands(sleeps: list[float], budget: float) -> None:
    elapsed = 0.0
    for pause in sleeps:
        low, high = SLOW_JITTER if elapsed >= SLOW_AFTER else FAST_JITTER
        remaining = budget - elapsed
        assert pause <= high + 1e-9
        assert pause >= low - 1e-9 or abs(pause - remaining) < 1e-9
        elapsed += pause


def test_uncontended_write_does_not_sleep(fake: FakeConnection, fake_clock: AutoClock) -> None:
    writer = _writer(fake, fake_clock)
    assert writer.write(_insert("a")) == 1
    assert _rows(fake) == ["a"] and fake_clock.sleeps == []


def test_lock_held_for_three_seconds_is_waited_out(
    fake: FakeConnection, fake_clock: AutoClock
) -> None:
    start = fake_clock.monotonic()
    fake.fail("BEGIN IMMEDIATE", locked_error, while_=lambda: fake_clock.monotonic() - start < 3.0)
    writer = _writer(fake, fake_clock)
    writer.write(_insert("late"))
    assert _rows(fake) == ["late"]
    assert 3.0 <= fake_clock.monotonic() - start < 4.0
    _check_bands(fake_clock.sleeps, ROUTINE.seconds)
    assert any(p >= SLOW_JITTER[0] for p in fake_clock.sleeps)  # crossed into the slow band


@pytest.mark.parametrize("budget", [ROUTINE, OBSERVATION, TRANSCRIPT])
def test_lock_held_forever_raises_store_locked_at_the_deadline(
    fake: FakeConnection, fake_clock: AutoClock, budget: Budget
) -> None:
    start = fake_clock.monotonic()
    fake.fail("BEGIN IMMEDIATE", locked_error)
    writer = _writer(fake, fake_clock)
    with pytest.raises(StoreLocked) as caught:
        writer.write(_insert("never"), budget=budget)
    assert fake_clock.monotonic() - start == pytest.approx(budget.seconds)
    _check_bands(fake_clock.sleeps, budget.seconds)
    assert classify(caught.value) is Bucket.LOCKED
    assert classify(str(caught.value)) is Bucket.LOCKED
    assert "contention" in str(caught.value)
    assert _rows(fake) == []


def test_patiently_waits_out_a_lock_then_gives_up_at_the_deadline(
    fake: FakeConnection, fake_clock: AutoClock
) -> None:
    """``patiently`` (the VACUUM path, outside a transaction) waits like a write."""
    writer = _writer(fake, fake_clock)
    start = fake_clock.monotonic()
    calls: list[float] = []

    def busy_for_three_seconds(conn: sqlite3.Connection) -> str:
        calls.append(fake_clock.monotonic() - start)
        if fake_clock.monotonic() - start < 3.0:
            raise locked_error()
        return "done"

    assert writer.patiently(fake.real, busy_for_three_seconds, budget=ROUTINE) == "done"
    assert len(calls) > 1 and 3.0 <= calls[-1] < 4.0
    _check_bands(fake_clock.sleeps, ROUTINE.seconds)

    def always_busy(conn: sqlite3.Connection) -> str:
        raise locked_error()

    begun = fake_clock.monotonic()
    with pytest.raises(StoreLocked):
        writer.patiently(fake.real, always_busy, budget=OBSERVATION)
    assert fake_clock.monotonic() - begun == pytest.approx(OBSERVATION.seconds)

    def broken(conn: sqlite3.Connection) -> str:
        raise sqlite3.OperationalError("no such table: nowhere")

    with pytest.raises(sqlite3.OperationalError):
        writer.patiently(fake.real, broken, budget=ROUTINE)


def test_callback_error_rolls_back(fake: FakeConnection, fake_clock: AutoClock) -> None:
    writer = _writer(fake, fake_clock)

    def half(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO t(x) VALUES ('partial')")
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        writer.write(half)
    assert _rows(fake) == []
    assert not fake.real.in_transaction
    writer.write(_insert("after"))
    assert _rows(fake) == ["after"]


def _io_error() -> sqlite3.OperationalError:
    return sqlite3.OperationalError("disk I/O error")


def test_begin_io_error_once_is_retried(fake: FakeConnection, fake_clock: AutoClock) -> None:
    fake.fail("BEGIN IMMEDIATE", _io_error, times=1)
    _writer(fake, fake_clock).write(_insert("ok"))
    assert _rows(fake) == ["ok"]


def test_begin_io_error_twice_is_raised(fake: FakeConnection, fake_clock: AutoClock) -> None:
    fake.fail("BEGIN IMMEDIATE", _io_error, times=2)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        _writer(fake, fake_clock).write(_insert("no"))
    assert _rows(fake) == []


def test_io_error_after_the_callback_ran_is_not_retried(
    fake: FakeConnection, fake_clock: AutoClock
) -> None:
    rule = fake.fail("^COMMIT$", _io_error, times=1)
    calls: list[int] = []

    def fn(conn: sqlite3.Connection) -> None:
        calls.append(1)
        conn.execute("INSERT INTO t(x) VALUES ('x')")

    with pytest.raises(sqlite3.OperationalError):
        _writer(fake, fake_clock).write(fn)
    assert calls == [1] and rule.hits == 1


def test_no_more_rows_is_retried_like_busy(fake: FakeConnection, fake_clock: AutoClock) -> None:
    fake.fail(
        "BEGIN IMMEDIATE",
        lambda: sqlite3.InterfaceError("no more rows available"),
        times=3,
    )
    _writer(fake, fake_clock).write(_insert("ok"))
    assert _rows(fake) == ["ok"] and len(fake_clock.sleeps) == 3


def test_fifty_writes_make_one_passive_checkpoint(
    fake: FakeConnection, fake_clock: AutoClock
) -> None:
    writer = _writer(fake, fake_clock)
    for i in range(CHECKPOINT_EVERY):
        writer.write(_insert(str(i)))
    checkpoints = [s for s in fake.statements if "wal_checkpoint" in s]
    assert checkpoints == ["PRAGMA wal_checkpoint(PASSIVE)"]
    writer.close()
    assert not any("TRUNCATE" in s.upper() for s in fake.statements)


def test_periodic_hook_runs_every_n_writes(fake: FakeConnection, fake_clock: AutoClock) -> None:
    writer = _writer(fake, fake_clock)
    seen: list[int] = []
    writer.add_periodic(3, lambda _conn: seen.append(writer.write_count))
    for i in range(7):
        writer.write(_insert(str(i)))
    assert seen == [3, 6]


def test_failing_periodic_hook_never_fails_the_write(
    fake: FakeConnection, fake_clock: AutoClock
) -> None:
    writer = _writer(fake, fake_clock)

    def bad(_conn: sqlite3.Connection) -> None:
        raise RuntimeError("merge failed")

    writer.add_periodic(1, bad)
    writer.write(_insert("x"))
    assert _rows(fake) == ["x"]


def test_nested_write_is_refused(fake: FakeConnection, fake_clock: AutoClock) -> None:
    writer = _writer(fake, fake_clock)

    def outer(conn: sqlite3.Connection) -> None:
        writer.write(_insert("inner"))

    with pytest.raises(RuntimeError, match="inside another write"):
        writer.write(outer)
    assert _rows(fake) == []


def test_borrow_inside_a_write_sees_the_transaction(
    fake: FakeConnection, fake_clock: AutoClock
) -> None:
    writer = _writer(fake, fake_clock)

    def fn(conn: sqlite3.Connection) -> int:
        conn.execute("INSERT INTO t(x) VALUES ('y')")
        with writer.borrow() as same:
            return int(same.execute("SELECT count(*) FROM t").fetchone()[0])

    assert writer.write(fn) == 1


def test_write_after_teardown_reopens_with_a_warning(
    db: Path, fake: FakeConnection, fake_clock: AutoClock, caplog: pytest.LogCaptureFixture
) -> None:
    reopened: list[sqlite3.Connection] = []

    def reopen() -> sqlite3.Connection:
        conn = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
        reopened.append(conn)
        return conn

    writer = _writer(fake, fake_clock, reopen=reopen)
    writer.close()
    caplog.set_level(logging.WARNING)
    writer.write(_insert("late"))
    assert len(reopened) == 1
    assert any("reopening" in r.getMessage() for r in caplog.records)
    assert [r[0] for r in reopened[0].execute("SELECT x FROM t").fetchall()] == ["late"]
    writer.close()


def test_awrite_serialises_without_blocking_the_loop(
    fake: FakeConnection, fake_clock: AutoClock
) -> None:
    writer = _writer(fake, fake_clock)
    in_flight = 0
    max_in_flight = 0
    guard = threading.Lock()
    release = threading.Event()

    def slow(i: int):  # type: ignore[no-untyped-def]
        def fn(conn: sqlite3.Connection) -> int:
            nonlocal in_flight, max_in_flight
            with guard:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            release.wait(5)
            conn.execute("INSERT INTO t(x) VALUES (?)", (str(i),))
            with guard:
                in_flight -= 1
            return i

        return fn

    async def main() -> tuple[list[int], int]:
        ticks = 0
        tasks = [asyncio.ensure_future(writer.awrite(slow(i))) for i in range(20)]

        async def heartbeat() -> None:
            nonlocal ticks
            while not all(t.done() for t in tasks):
                ticks += 1
                if ticks == 50:
                    release.set()  # the loop kept running while the first write was blocked
                await checkpoint()

        await asyncio.gather(heartbeat(), *tasks)
        return [t.result() for t in tasks], ticks

    results, ticks = asyncio.run(main())
    assert sorted(int(x) for x in _rows(fake)) == list(range(20))
    writer.close()
    assert results == list(range(20))
    assert max_in_flight == 1
    assert ticks >= 50
