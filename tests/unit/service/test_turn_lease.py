"""P17-03: the durable turn lease (acquire, wait, reclaim, refresh, loss, release)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from indus.foundation.errors import StoreLocked
from indus.foundation.scope import Scope
from indus.service.turn_lease import (
    LEASE_LOST,
    STILL_WAITING_TEXT,
    WAIT_TEXT,
    HeldLease,
    LeaseSettings,
    NotAcquired,
    TurnLeases,
    WaitInterrupt,
    wait_text,
)
from indus.state import registry, turn_leases
from indus.state import runtime_ledger as ledger
from indus.state.registry import StoreHandle
from indus.state.turn_leases import LeaseHolder, ProcessProbe
from indus.state.writer import Budget
from tests.fakes.clock import FakeClock
from tests.unit.backends._aio import aio

HOST = "box"
NS = "4026531836"


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


@pytest.fixture
def clock(store: StoreHandle) -> FakeClock:
    clock = store.scope.clock
    assert isinstance(clock, FakeClock)
    return clock


class Processes:
    """A fake process table for the liveness probe."""

    def __init__(self) -> None:
        self.alive: dict[int, int | None] = {}  # pid -> start time
        self.namespace = NS
        self.host = HOST

    def probe(self) -> ProcessProbe:
        return ProcessProbe(
            alive=lambda pid: pid in self.alive,
            start_time=lambda pid: self.alive.get(pid),
            namespace=lambda: self.namespace,
            host=lambda: self.host,
        )


def holder(pid: int, *, start: int | None = 7, ns: str = NS, host: str = HOST) -> LeaseHolder:
    return LeaseHolder(
        f"pid={pid}:pidns={ns}:{host}:start={start}:lease=t{pid}", pid, start, ns, host
    )


class Writes:
    """The inline write seam (the loop never waits on a thread here); can fail on demand."""

    def __init__(self, store: StoreHandle) -> None:
        self.store = store
        self.fail: Callable[[str], BaseException | None] = lambda name: None
        self.calls: list[str] = []

    async def __call__(self, fn: Callable[[sqlite3.Connection], Any], budget: Budget) -> Any:
        self.calls.append(budget.name)
        failure = self.fail(budget.name)
        if failure is not None:
            raise failure
        return self.store.write(fn, budget=budget)


def leases_for(
    store: StoreHandle, procs: Processes, *, settings: LeaseSettings | None = None
) -> tuple[TurnLeases, Writes]:
    writes = Writes(store)
    return TurnLeases(store, settings=settings, probe=procs.probe(), write=writes), writes


async def settle(clock: FakeClock, seconds: float = 0.0, *, step: float = 1.0) -> None:
    """Advance in ``step`` slices, letting the loop run before and after each."""
    await clock.run_until_idle()
    left = seconds
    while left > 0:
        hop = min(step, left)
        await clock.advance_and_settle(hop)
        left -= hop
    await clock.run_until_idle()


def test_settings_match_the_spec() -> None:
    cfg = LeaseSettings()
    assert (cfg.ttl, cfg.refresh_every, cfg.write_patience, cfg.expiry_margin) == (300, 60, 20, 2)
    assert (cfg.wait, cfg.notice_every, cfg.max_local) == (1800, 15, 512)
    assert wait_text(0.2) == WAIT_TEXT
    assert wait_text(31.7) == STILL_WAITING_TEXT.format(seconds=31)


def test_holder_stamp_keeps_the_p02_shape() -> None:
    me = LeaseHolder.current()
    from indus.state import holders

    info = holders.parse_holder(me.holder_id)
    assert info is not None and info.pid == me.pid
    assert LeaseHolder.current().holder_id != me.holder_id  # one nonce per lease


@aio
async def test_acquire_release_and_generations(store: StoreHandle) -> None:
    procs = Processes()
    leases, _ = leases_for(store, procs)
    held = await leases.acquire("s1", holder=holder(100))
    assert isinstance(held, HeldLease) and held.generation == 1
    row = turn_leases.get(store, "s1")
    assert row is not None and row.holder_id == held.holder_id and row.generation == 1
    assert (row.holder_pid, row.holder_start, row.holder_ns, row.holder_host) == (100, 7, NS, HOST)
    assert held.deadline == row.expires_at == row.acquired_at + 300
    # a claimed admission passes its own generation; the same holder re-enters
    again = await leases.acquire("s1", holder=holder(100), generation=9)
    assert isinstance(again, HeldLease) and again.generation == 1
    assert await held.release() and not await held.release()
    assert turn_leases.get(store, "s1") is None
    second = await leases.acquire("s1", holder=holder(100))
    assert isinstance(second, HeldLease) and second.generation == 2
    assert ledger.generation_of(store, "s1") == 2
    third = await leases.acquire("s2", holder=holder(100), generation=41)
    assert isinstance(third, HeldLease) and third.generation == 41


@aio
async def test_second_holder_waits_is_told_and_proceeds_after_release(
    store: StoreHandle, clock: FakeClock
) -> None:
    procs = Processes()
    procs.alive[100] = 7
    leases, _ = leases_for(store, procs)
    first = await leases.acquire("s1", holder=holder(100))
    assert isinstance(first, HeldLease)
    notices: list[float] = []
    waiter = asyncio.ensure_future(leases.acquire("s1", holder=holder(200), on_wait=notices.append))
    await settle(clock)
    assert not waiter.done() and notices == [0.0]
    await settle(clock, 14)
    assert notices == [0.0]
    await settle(clock, 1)
    assert notices == [0.0, 15.0]
    await first.release()  # wakes the waiter in this process at once
    await clock.run_until_idle()
    second = waiter.result()
    assert isinstance(second, HeldLease) and second.holder_id == holder(200).holder_id
    assert second.generation == 2 and second.waited and not first.waited


@aio
async def test_wait_times_out_without_running(store: StoreHandle, clock: FakeClock) -> None:
    procs = Processes()
    procs.alive[100] = 7
    leases, _ = leases_for(store, procs, settings=LeaseSettings(wait=5))
    assert isinstance(await leases.acquire("s1", holder=holder(100)), HeldLease)
    waiter = asyncio.ensure_future(leases.acquire("s1", holder=holder(200)))
    await settle(clock, 6)
    result = waiter.result()
    assert result == NotAcquired("timeout", waited=True)


@pytest.mark.parametrize(("kind", "carried"), [("stop", ()), ("follow_up", ("first message",))])
@aio
async def test_follow_up_interrupt_carries_the_waiting_input(
    store: StoreHandle, clock: FakeClock, kind: str, carried: tuple[str, ...]
) -> None:
    procs = Processes()
    procs.alive[100] = 7
    leases, _ = leases_for(store, procs)
    assert isinstance(await leases.acquire("s1", holder=holder(100)), HeldLease)
    interrupt = WaitInterrupt()
    waiter = asyncio.ensure_future(
        leases.acquire("s1", holder=holder(200), interrupt=interrupt, pending_input="first message")
    )
    await settle(clock, 3)
    interrupt.stop() if kind == "stop" else interrupt.follow_up()
    await clock.run_until_idle()
    assert waiter.result() == NotAcquired("interrupted", waited=True, carried=carried)
    assert leases.local_slot_count == 1


@aio
async def test_expired_row_is_reclaimed(store: StoreHandle, clock: FakeClock) -> None:
    procs = Processes()
    procs.alive[100] = 7  # alive but silent: only the expiry frees it
    leases, _ = leases_for(store, procs)
    stale = await leases.acquire("s1", holder=holder(100))
    assert isinstance(stale, HeldLease)
    waiter = asyncio.ensure_future(leases.acquire("s1", holder=holder(200)))
    await settle(clock, 299, step=10)
    assert not waiter.done()
    await settle(clock, 1)
    taken = waiter.result()
    assert isinstance(taken, HeldLease) and taken.generation == 2
    # the displaced holder can neither renew nor clear its successor's row
    assert not await stale.refresh_once() and stale.lost_reason == LEASE_LOST
    assert not await stale.release()
    row = turn_leases.get(store, "s1")
    assert row is not None and row.holder_id == taken.holder_id


@aio
async def test_dead_holder_is_reclaimed_before_expiry(store: StoreHandle) -> None:
    procs = Processes()
    leases, _ = leases_for(store, procs)
    assert isinstance(await leases.acquire("s1", holder=holder(100)), HeldLease)
    # pid 100 is not in the process table: provably gone (same host, same namespace)
    taken = await leases.acquire("s1", holder=holder(200))
    assert isinstance(taken, HeldLease)


@aio
async def test_reused_pid_with_another_start_time_is_dead(store: StoreHandle) -> None:
    procs = Processes()
    procs.alive[100] = 99  # the pid runs, but a process started at another time
    leases, _ = leases_for(store, procs)
    assert isinstance(await leases.acquire("s1", holder=holder(100, start=7)), HeldLease)
    assert isinstance(await leases.acquire("s1", holder=holder(200)), HeldLease)


@pytest.mark.parametrize(
    "other", [holder(100, ns="4026532999"), holder(100, host="elsewhere"), holder(100, start=None)]
)
@aio
async def test_holder_in_another_namespace_waits_for_expiry(
    store: StoreHandle, clock: FakeClock, other: LeaseHolder
) -> None:
    """A pid from another container (or host, or without a start time) is never probed: whether
    it looks dead or alive here means nothing; only the expiry frees the row (R03.20)."""
    procs = Processes()
    leases, _ = leases_for(store, procs, settings=LeaseSettings(ttl=30))
    assert isinstance(await leases.acquire("s1", holder=other), HeldLease)
    procs.alive[100] = 12345  # a local pid 100 with another start time would look "reused"
    waiter = asyncio.ensure_future(leases.acquire("s1", holder=holder(200)))
    await settle(clock, 29)
    assert not waiter.done()
    await settle(clock, 1)
    assert isinstance(waiter.result(), HeldLease)


@aio
async def test_refresh_moves_the_deadline(store: StoreHandle, clock: FakeClock) -> None:
    procs = Processes()
    leases, _ = leases_for(store, procs)
    held = await leases.acquire("s1", holder=holder(100))
    assert isinstance(held, HeldLease)
    start = held.deadline - 300
    lost: list[str] = []
    held.start(on_lost=lost.append)
    await settle(clock, 60, step=60)
    assert held.deadline == start + 360
    row = turn_leases.get(store, "s1")
    assert row is not None and row.expires_at == start + 360 and row.refreshed_at == start + 60
    await settle(clock, 600, step=60)
    assert held.deadline == start + 960 and lost == []
    await held.release()


@aio
async def test_lock_errors_near_the_deadline_lose_the_lease(
    store: StoreHandle, clock: FakeClock
) -> None:
    procs = Processes()
    leases, writes = leases_for(store, procs)
    held = await leases.acquire("s1", holder=holder(100))
    assert isinstance(held, HeldLease)
    lost: list[str] = []
    held.start(on_lost=lost.append)
    writes.fail = lambda name: StoreLocked("busy") if name == "lease_refresh" else None
    await settle(clock, 180, step=60)  # t=60, 120, 180: a missed tick still lands in time
    assert lost == [] and not held.lost
    await settle(clock, 60, step=60)  # t=240: the next tick (300) would land past expiry
    assert lost == [LEASE_LOST] and held.lost_reason == LEASE_LOST
    assert writes.calls.count("lease_refresh") == 4
    await settle(clock, 120, step=60)
    assert lost == [LEASE_LOST]  # once
    await held.release()


@aio
async def test_refresh_failure_past_expiry_interrupts_with_lease_lost(
    store: StoreHandle, clock: FakeClock
) -> None:
    procs = Processes()
    leases, _ = leases_for(store, procs)
    held = await leases.acquire("s1", holder=holder(100))
    assert isinstance(held, HeldLease)
    interrupts: list[str] = []
    held.start(on_lost=interrupts.append)
    await clock.run_until_idle()
    clock.advance(400)  # the loop was frozen past the expiry: the renewal cannot be trusted
    await clock.run_until_idle()
    assert interrupts == [LEASE_LOST]


@aio
async def test_a_reowned_row_is_a_loss(store: StoreHandle, clock: FakeClock) -> None:
    procs = Processes()
    leases, _ = leases_for(store, procs)
    held = await leases.acquire("s1", holder=holder(100))
    assert isinstance(held, HeldLease)
    lost: list[str] = []
    held.start(on_lost=lost.append)
    store.write(lambda conn: conn.execute("UPDATE turn_leases SET holder = 'someone-else'"))
    await settle(clock, 60, step=60)
    assert lost == [LEASE_LOST]


@aio
async def test_other_failures_lose_the_lease_at_once(store: StoreHandle, clock: FakeClock) -> None:
    procs = Processes()
    leases, writes = leases_for(store, procs)
    held = await leases.acquire("s1", holder=holder(100))
    assert isinstance(held, HeldLease)
    lost: list[str] = []
    held.start(on_lost=lost.append)
    writes.fail = lambda name: OSError("disk gone") if name == "lease_refresh" else None
    await settle(clock, 60, step=60)
    assert lost == [LEASE_LOST]


@aio
async def test_refresh_after_release_is_not_a_loss(store: StoreHandle, clock: FakeClock) -> None:
    procs = Processes()
    leases, _ = leases_for(store, procs)
    held = await leases.acquire("s1", holder=holder(100))
    assert isinstance(held, HeldLease)
    lost: list[str] = []
    held.start(on_lost=lost.append)
    await held.release()
    assert not await held.refresh_once()
    held.stop_refresh()
    await settle(clock, 120, step=60)
    assert lost == [] and held.lost_reason is None


@aio
async def test_a_busy_store_is_not_a_holder(store: StoreHandle) -> None:
    procs = Processes()
    leases, writes = leases_for(store, procs)
    failures = iter([StoreLocked("busy"), StoreLocked("busy")])
    writes.fail = lambda name: next(failures, None) if name == "lease_acquire" else None
    notices: list[float] = []
    held = await leases.acquire("s1", holder=holder(100), on_wait=notices.append)
    assert isinstance(held, HeldLease) and notices == []
    assert held.waited  # the busy writer may have been a holder's last flush: reload
    assert writes.calls.count("lease_acquire") == 3


def test_release_is_keyed_by_holder_and_generation(store: StoreHandle) -> None:
    me = holder(100)
    got = turn_leases.try_acquire(store, "s1", me, ttl=300, generation=4)
    assert isinstance(got, turn_leases.Acquired)
    assert not turn_leases.release(store, "s1", me.holder_id, 3)
    assert not turn_leases.release(store, "s1", holder(200).holder_id, 4)
    assert not turn_leases.refresh(store, "s1", me.holder_id, 3, ttl=300)
    assert turn_leases.refresh(store, "s1", me.holder_id, 4, ttl=300)
    assert turn_leases.release(store, "s1", me.holder_id, 4)
    assert turn_leases.get(store, "s1") is None


def test_deletion_guards_read_lease_rows(store: StoreHandle) -> None:
    """The P02 deletion guard sees a live lease row (the holder keeps the stamp shape)."""
    from indus.state import deletion

    me = LeaseHolder.current()
    turn_leases.try_acquire(store, "s1", me, ttl=300)
    now = store.scope.clock.now().timestamp()
    with store.read() as conn:
        assert deletion.guarded_ids_in(conn, ["s1"], now=now, reap=False) == {"s1"}


@aio
async def test_local_slots_are_capped_and_evict_idle_only(store: StoreHandle) -> None:
    procs = Processes()
    leases, _ = leases_for(store, procs, settings=LeaseSettings(max_local=3))
    for n in range(6):
        held = await leases.acquire(f"idle{n}", holder=holder(100))
        assert isinstance(held, HeldLease)
        await held.release()
    assert leases.local_slot_count <= 3
    kept = []
    for n in range(5):
        held = await leases.acquire(f"live{n}", holder=holder(100))
        assert isinstance(held, HeldLease)
        kept.append(held)
    assert leases.local_slot_count == 5  # live slots are never evicted
    for held in kept:
        await held.release()
    assert isinstance(await leases.acquire("again", holder=holder(100)), HeldLease)
    assert leases.local_slot_count <= 5


def test_a_live_holder_cannot_be_declared_dead_without_proof() -> None:
    """Only a provably gone holder loses its row early (integration review, R03.20)."""

    def row(**over: Any) -> turn_leases.LeaseRow:
        base: dict[str, Any] = {
            "session_id": "s1",
            "holder_id": "pid=4242:pidns=ns:box:start=7:lease=x",
            "acquired_at": 0.0,
            "expires_at": 1e12,
            "holder_pid": 4242,
            "holder_start": 7,
            "holder_ns": "ns",
            "holder_host": "box",
            "generation": 1,
            "refreshed_at": 0.0,
        }
        base.update(over)
        return turn_leases.LeaseRow(**base)

    def probe(*, alive: bool = True, start: int | None = 7, boom: bool = False) -> ProcessProbe:
        def started(pid: int) -> int | None:
            if boom:
                raise OSError("proc unreadable")
            return start

        return ProcessProbe(
            alive=lambda pid: alive, start_time=started, namespace=lambda: "ns", host=lambda: "box"
        )

    dead = turn_leases.holder_is_dead
    assert not dead(row(), probe())  # alive, same start time
    assert not dead(row(holder_start=None), probe(start=99))  # start unknown: no proof
    assert not dead(row(), probe(start=None))  # cannot read the start time now
    assert not dead(row(), probe(boom=True))  # probing failed: any doubt keeps the row
    assert not dead(row(holder_host="other"), probe(alive=False))  # another machine
    assert not dead(row(holder_ns="other-ns"), probe(alive=False))  # another container
    assert not dead(row(holder_ns=None), probe(alive=False))  # no namespace recorded
    assert not dead(row(holder_pid=os.getpid(), holder_start=1), probe(start=2))  # this process
    assert dead(row(), probe(alive=False))
    assert dead(row(), probe(start=8))  # the pid was reused by a later process
