"""P17-02: the admission service (receipts, FIFO workers, unknown turns, controls, lease, stall)."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.service.admissions import (
    CANCELLED,
    LEASE_TIMEOUT,
    LOST_TURN_NOTE,
    SHUTDOWN,
    AdmissionService,
    Receipt,
    TurnOutcome,
    TurnTicket,
    close_lost_turn,
)
from indus.service.turn_lease import LEASE_LOST, LeaseSettings, TurnLeases
from indus.service.turn_liveness import STALLED, LivenessSettings
from indus.state import messages, registry, turn_leases
from indus.state import runtime_ledger as ledger
from indus.state.registry import StoreHandle
from indus.state.runtime_ledger import AdmissionRow, LedgerRefusal
from indus.state.turn_leases import LeaseHolder, ProcessProbe
from indus.state.writer import Budget
from tests.fakes.clock import FakeClock
from tests.unit.backends._aio import aio
from tests.unit.state._helpers_d import add_message, add_session

ME = "cli:alice"


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


async def inline[T](fn: Callable[[], T]) -> T:
    return fn()


class Model:
    """A fake turn runner: answers at once, or holds each turn until released or stopped."""

    def __init__(self, *, hold: bool = False) -> None:
        self.hold = hold
        self.calls: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.steers: list[str] = []
        self.accept_steer = True
        self.fail_on: set[str] = set()
        self.tickets: list[TurnTicket] = []

    def _steer(self, text: str) -> bool:
        if self.accept_steer:
            self.steers.append(text)
        return self.accept_steer

    async def __call__(self, ticket: TurnTicket) -> TurnOutcome:
        self.calls.append(ticket.text)
        self.tickets.append(ticket)
        ticket.bind(steer=self._steer)
        if ticket.text in self.fail_on:
            raise RuntimeError("the model\nexploded")
        if ticket.text == "leaky":
            secret = "sk-" + "z" * 26  # built at runtime: no credential literal in the tree
            raise RuntimeError(f"POST failed with {secret} reading /srv/indus/home/auth.json")
        if self.hold:
            gate = self.gates.setdefault(ticket.text, asyncio.Event())
            waits = {
                asyncio.ensure_future(gate.wait()),
                asyncio.ensure_future(ticket.stopped.wait()),
            }
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for task in waits:
                task.cancel()
            if ticket.stop_reason == CANCELLED:
                return TurnOutcome("cancelled")
            if ticket.stop_reason:
                return TurnOutcome("interrupted")
        return TurnOutcome("completed", final_text=f"answer: {ticket.text}")

    def release(self, text: str) -> None:
        self.gates.setdefault(text, asyncio.Event()).set()


async def opened(store: StoreHandle, model: Model, **options: Any) -> AdmissionService:
    return await AdmissionService.open(
        store, instance_id="daemon-1", runner=model, offload=inline, **options
    )


async def admit(service: AdmissionService, request_id: str, text: str, **kw: Any) -> Receipt:
    session_id = kw.pop("session_id", "s1")
    return await service.admit(ME, session_id, request_id, {"text": text}, **kw)


def clock_of(store: StoreHandle) -> FakeClock:
    clock = store.scope.clock
    assert isinstance(clock, FakeClock)
    return clock


@aio
async def test_admit_runs_the_turn_and_settles_it(store: StoreHandle) -> None:
    model = Model()
    service = await opened(store, model)
    receipt = await admit(service, "r1", "hello")
    assert receipt.state == "queued" and receipt.position == 1 and receipt.final_text is None
    done = await service.wait(receipt.admission_id)
    assert done == Receipt(
        receipt.admission_id, "terminal", "s1", generation=1, outcome="completed"
    )
    row = ledger.get(store, receipt.admission_id)
    assert row is not None and row.final_text == "answer: hello"
    assert model.calls == ["hello"]
    await service.aclose()


@aio
async def test_inputs_run_in_fifo_order(store: StoreHandle) -> None:
    model = Model(hold=True)
    service = await opened(store, model)
    receipts = [await admit(service, f"r{n}", f"m{n}") for n in range(3)]
    await clock_of(store).run_until_idle()
    assert model.calls == ["m0"]
    second = await service.receipt(receipts[1].admission_id)
    assert second.state == "queued" and second.position == 1
    first = await service.receipt(receipts[0].admission_id)
    assert first.state == "started" and first.generation == 1
    for n in range(3):
        model.release(f"m{n}")
        await service.wait(receipts[n].admission_id)
        await clock_of(store).run_until_idle()
    assert model.calls == ["m0", "m1", "m2"]
    gens = [(await service.receipt(r.admission_id)).generation for r in receipts]
    assert gens == [1, 2, 3]
    await service.aclose()


@aio
async def test_two_concurrent_admits_of_one_request_make_one_row(store: StoreHandle) -> None:
    model = Model()
    service = await AdmissionService.open(store, instance_id="daemon-1", runner=model)
    one, two = await asyncio.gather(
        admit(service, "same", "only once"), admit(service, "same", "only once")
    )
    assert one.admission_id == two.admission_id
    await service.wait(one.admission_id)
    assert model.calls == ["only once"]
    assert len([r for r in [ledger.find(store, ME, "s1", "same")] if r is not None]) == 1
    await service.aclose()


@aio
async def test_exact_retry_after_completion_replays_text_without_running(
    store: StoreHandle,
) -> None:
    model = Model()
    service = await opened(store, model)
    first = await admit(service, "r1", "what time is it")
    await service.wait(first.admission_id)
    assert len(model.calls) == 1
    retry = await admit(service, "r1", "what time is it")
    assert retry.replayed and retry.state == "terminal" and retry.outcome == "completed"
    assert retry.final_text == "answer: what time is it"
    assert len(model.calls) == 1  # no second model call
    plain = await service.receipt(first.admission_id)
    assert plain.final_text is None and not plain.replayed
    await service.aclose()


@aio
async def test_refusals_keep_their_reason(store: StoreHandle) -> None:
    service = await opened(store, Model())
    await admit(service, "r1", "a")
    with pytest.raises(LedgerRefusal) as exc:
        await admit(service, "r1", "b")
    assert exc.value.reason == "admission_conflict"
    with pytest.raises(LedgerRefusal) as exc:
        await service.receipt("missing")
    assert exc.value.reason == "not_found"
    await service.aclose()


async def _lost_turn(store: StoreHandle) -> tuple[AdmissionRow, Receipt]:
    """A first daemon starts a turn and dies with it; one more input waits behind it."""
    model = Model(hold=True)
    first = await opened(store, model)
    lost = await admit(first, "lost", "risky")
    later = await admit(first, "later", "after that")
    await clock_of(store).run_until_idle()
    assert model.calls == ["risky"]
    # the daemon dies: its tasks never run again (we only stop them for the test)
    for task in list(first._workers.values()):  # pyright: ignore[reportPrivateUsage]
        task.cancel()
    await clock_of(store).run_until_idle()
    row = ledger.get(store, lost.admission_id)
    assert row is not None and row.state == "started"
    return row, later


@aio
async def test_restart_parks_the_lost_turn_and_tells_once(store: StoreHandle) -> None:
    lost, later = await _lost_turn(store)
    told: list[tuple[str, str]] = []
    model = Model()
    second = await AdmissionService.open(
        store,
        instance_id="daemon-2",
        runner=model,
        offload=inline,
        on_blocked=lambda sid, row: told.append((sid, row.admission_id)),
    )
    await clock_of(store).run_until_idle()
    assert told == [("s1", lost.admission_id)]
    unknown = await second.receipt(lost.admission_id)
    assert unknown.state == "unknown" and unknown.generation == 1
    waiting = await second.receipt(later.admission_id)
    assert waiting.state == "queued" and waiting.blocked_by == lost.admission_id
    more = await admit(second, "more", "and this")
    await clock_of(store).run_until_idle()
    assert more.blocked_by == lost.admission_id and told == [("s1", lost.admission_id)]
    assert model.calls == []  # nothing runs behind an unknown turn, the lost one never again
    retry = await admit(second, "lost", "risky")
    assert retry.state == "unknown" and not retry.replayed and model.calls == []
    await second.aclose()


@aio
async def test_discard_with_an_old_generation_is_refused(store: StoreHandle) -> None:
    lost, later = await _lost_turn(store)
    model = Model()
    second = await opened(store, model)
    with pytest.raises(LedgerRefusal) as exc:
        await second.discard_unknown(lost.admission_id, expected_generation=0)
    assert exc.value.reason == "stale_generation"
    done = await second.discard_unknown(lost.admission_id, expected_generation=1)
    assert done.state == "terminal" and done.outcome == "discarded"
    finished = await second.wait(later.admission_id)
    assert finished.outcome == "completed" and model.calls == ["after that"]
    await second.aclose()


@aio
async def test_discard_closes_the_lost_turn_in_its_transcript(store: StoreHandle) -> None:
    lost, later = await _lost_turn(store)
    add_session(store, "s1")
    add_message(store, "s1", "risky", role="user")
    model = Model()
    second = await opened(store, model)
    now = lambda: 1_700_000_000.0  # noqa: E731
    seen: list[str] = []

    def broken(conn: sqlite3.Connection, row: AdmissionRow) -> None:
        seen.append(row.admission_id)
        raise OSError("transcript unwritable")

    with pytest.raises(OSError):
        await second.discard_unknown(lost.admission_id, 1, terminal_write=broken)
    still = await second.receipt(lost.admission_id)
    assert still.state == "unknown" and seen == [lost.admission_id]  # rolled back with it

    done = await second.discard_unknown(
        lost.admission_id, 1, terminal_write=close_lost_turn(now=now)
    )
    assert done.outcome == "discarded"
    rows = messages.get(store, "s1")
    assert [(m["role"], m["content"]) for m in rows] == [
        ("user", "risky"),
        ("assistant", LOST_TURN_NOTE),
    ]
    finished = await second.wait(later.admission_id)
    assert finished.outcome == "completed"
    # A second close of the same transcript writes nothing more (the last message is ours).
    store.write(lambda conn: close_lost_turn(now=now)(conn, lost))
    assert len(messages.get(store, "s1")) == 2
    await second.aclose()


def test_closing_a_lost_turn_skips_a_transcript_that_needs_no_note(store: StoreHandle) -> None:
    lost = AdmissionRow(
        "a1", 1, ME, "s2", "r1", {"text": "x"}, "d", "unknown", None, 1, 1, None, None, None, 0, 0
    )
    close = close_lost_turn(now=lambda: 1.0)
    store.write(lambda conn: close(conn, lost))  # no session row: nothing
    add_session(store, "s2")
    store.write(lambda conn: close(conn, lost))  # no messages: nothing
    add_message(store, "s2", "hi", role="user")
    add_message(store, "s2", "the reply", role="assistant")
    store.write(lambda conn: close(conn, lost))  # a reply already closes it
    assert [m["role"] for m in messages.get(store, "s2")] == ["user", "assistant"]
    add_session(store, "s3", ended_at=2.0, end_reason="compression")
    add_message(store, "s3", "hi", role="user")
    store.write(lambda conn: close(conn, replace(lost, session_id="s3")))
    assert [m["role"] for m in messages.get(store, "s3")] == ["user"]


@aio
async def test_cancel_queued_and_running(store: StoreHandle) -> None:
    model = Model(hold=True)
    service = await opened(store, model)
    running = await admit(service, "r1", "long job")
    queued = await admit(service, "r2", "next")
    await clock_of(store).run_until_idle()
    dropped = await service.cancel(queued.admission_id)
    assert dropped.state == "terminal" and dropped.outcome == "cancelled"
    still = await service.cancel(running.admission_id)
    assert still.state == "started"
    done = await service.wait(running.admission_id)
    assert done.outcome == "cancelled" and model.calls == ["long job"]
    await service.aclose()


@aio
async def test_steer_reaches_only_the_running_turn(store: StoreHandle) -> None:
    model = Model(hold=True)
    service = await opened(store, model)
    running = await admit(service, "r1", "first")
    queued = await admit(service, "r2", "second")
    await clock_of(store).run_until_idle()
    assert await service.steer(running.admission_id, "also mind the tests")
    assert not await service.steer(queued.admission_id, "not running")
    assert model.steers == ["also mind the tests"]
    model.release("first")
    model.release("second")
    await service.wait(queued.admission_id)
    await service.aclose()


@aio
async def test_steer_queued_transfers_into_the_running_turn(store: StoreHandle) -> None:
    model = Model(hold=True)
    service = await opened(store, model)
    with pytest.raises(LedgerRefusal):
        await service.steer_queued("nope")
    running = await admit(service, "r1", "first")
    queued = await admit(service, "r2", "fold this in")
    await clock_of(store).run_until_idle()
    moved = await service.steer_queued(queued.admission_id)
    assert moved.state == "terminal" and moved.outcome == "steering"
    assert model.steers == ["fold this in"]
    model.release("first")
    await service.wait(running.admission_id)
    steered = await service.receipt(queued.admission_id)
    assert steered.outcome == "steered" and model.calls == ["first"]
    await service.aclose()


@aio
async def test_undelivered_steer_goes_back_to_its_place(store: StoreHandle) -> None:
    model = Model(hold=True)
    model.accept_steer = False
    service = await opened(store, model)
    running = await admit(service, "r1", "first")
    queued = await admit(service, "r2", "second")
    await clock_of(store).run_until_idle()
    back = await service.steer_queued(queued.admission_id)
    assert back.state == "queued" and back.position == 1
    model.release("first")
    model.release("second")
    await service.wait(running.admission_id)
    await service.wait(queued.admission_id)
    assert model.calls == ["first", "second"]
    await service.aclose()


@aio
async def test_a_failing_runner_settles_failed_and_the_fifo_moves_on(store: StoreHandle) -> None:
    model = Model()
    model.fail_on.add("bad")
    service = await opened(store, model)
    bad = await admit(service, "r1", "bad")
    good = await admit(service, "r2", "good")
    failed = await service.wait(bad.admission_id)
    assert failed.outcome == "failed"
    row = ledger.get(store, bad.admission_id)
    assert row is not None and row.detail == "the model exploded"
    assert (await service.wait(good.admission_id)).outcome == "completed"
    await service.aclose()


@aio
async def test_a_failure_detail_is_masked_and_paths_are_cut(store: StoreHandle) -> None:
    service = await opened(store, Model())
    leaky = await admit(service, "r1", "leaky")
    assert (await service.wait(leaky.admission_id)).outcome == "failed"
    row = ledger.get(store, leaky.admission_id)
    assert row is not None and row.detail is not None
    assert "z" * 26 not in row.detail and "/srv/indus" not in row.detail
    assert row.detail.endswith("\u2026/auth.json")
    await service.aclose()


def _probe() -> ProcessProbe:
    return ProcessProbe(
        alive=lambda pid: True,
        start_time=lambda pid: 7,
        namespace=lambda: "ns",
        host=lambda: "box",
    )


class InlineWrites:
    def __init__(self, store: StoreHandle) -> None:
        self.store = store
        self.fail: Callable[[str], BaseException | None] = lambda name: None

    async def __call__(self, fn: Callable[[sqlite3.Connection], Any], budget: Budget) -> Any:
        failure = self.fail(budget.name)
        if failure is not None:
            raise failure
        return self.store.write(fn, budget=budget)


@aio
async def test_each_turn_holds_the_lease_for_its_generation(store: StoreHandle) -> None:
    seen: list[tuple[int, int | None]] = []

    async def runner(ticket: TurnTicket) -> TurnOutcome:
        row = turn_leases.get(store, ticket.session_id)
        seen.append((ticket.generation, None if row is None else row.generation))
        return TurnOutcome("completed", final_text="ok")

    leases = TurnLeases(store, probe=_probe(), write=InlineWrites(store))
    service = await AdmissionService.open(
        store, instance_id="d", runner=runner, offload=inline, leases=leases
    )
    a = await admit(service, "r1", "one")
    b = await admit(service, "r2", "two")
    await service.wait(a.admission_id)
    await service.wait(b.admission_id)
    assert seen == [(1, 1), (2, 2)]
    await clock_of(store).run_until_idle()
    assert turn_leases.get(store, "s1") is None  # released after each settle
    await service.aclose()


@aio
async def test_lease_loss_interrupts_the_turn(store: StoreHandle) -> None:
    model = Model(hold=True)
    writes = InlineWrites(store)
    leases = TurnLeases(store, probe=_probe(), write=writes)
    service = await opened(store, model, leases=leases)
    receipt = await admit(service, "r1", "slow")
    clock = clock_of(store)
    await clock.run_until_idle()
    writes.fail = lambda name: OSError("disk gone") if name == "lease_refresh" else None
    await clock.advance_and_settle(60)
    done = await service.wait(receipt.admission_id)
    assert done.outcome == "interrupted"
    row = ledger.get(store, receipt.admission_id)
    assert row is not None and row.detail == LEASE_LOST
    await service.aclose()


@aio
async def test_lease_timeout_fails_without_running(store: StoreHandle) -> None:
    other = LeaseHolder("pid=9:pidns=ns:box:start=7:lease=x", 9, 7, "ns", "box")
    turn_leases.try_acquire(store, "s1", other, ttl=3600)
    model = Model()
    leases = TurnLeases(
        store, probe=_probe(), write=InlineWrites(store), settings=LeaseSettings(wait=5)
    )
    service = await opened(store, model, leases=leases)
    receipt = await admit(service, "r1", "blocked")
    clock = clock_of(store)
    await clock.run_until_idle()
    for _ in range(6):
        await clock.advance_and_settle(1)
    done = await service.wait(receipt.admission_id)
    assert done.outcome == "failed" and model.calls == []
    row = ledger.get(store, receipt.admission_id)
    assert row is not None and row.detail == LEASE_TIMEOUT
    await service.aclose()


@aio
async def test_a_stalled_turn_is_stopped(store: StoreHandle) -> None:
    model = Model(hold=True)
    asking = {"s1": False}
    service = await opened(
        store,
        model,
        liveness=LivenessSettings(timeout_s=600.0, poll_s=15.0),
        waiting=lambda sid: asking[sid],
    )
    receipt = await admit(service, "r1", "wedged")
    clock = clock_of(store)
    await clock.run_until_idle()
    asking["s1"] = True  # a person is being asked: no stall however long
    for _ in range(80):
        await clock.advance_and_settle(15)
    assert (await service.receipt(receipt.admission_id)).state == "started"
    model.tickets[0].touch("still streaming")
    asking["s1"] = False
    for _ in range(40):
        await clock.advance_and_settle(15)
    done = await service.wait(receipt.admission_id)
    assert done.outcome == "interrupted"
    row = ledger.get(store, receipt.admission_id)
    assert row is not None and row.detail == STALLED
    await service.aclose()


@aio
async def test_aclose_interrupts_running_turns(store: StoreHandle) -> None:
    model = Model(hold=True)
    service = await opened(store, model)
    receipt = await admit(service, "r1", "running at shutdown")
    await clock_of(store).run_until_idle()
    await service.aclose()
    assert model.tickets[0].stop_reason == SHUTDOWN
    row = ledger.get(store, receipt.admission_id)
    assert row is not None and row.outcome == "interrupted" and row.detail == SHUTDOWN
    with pytest.raises(RuntimeError):
        await admit(service, "r2", "too late")


@aio
async def test_the_owner_gate_refuses_new_work(store: StoreHandle) -> None:
    draining = [False]

    def gate(conn: sqlite3.Connection) -> None:
        if draining[0]:
            raise RuntimeError("draining")

    model = Model()
    service = await opened(store, model, gate=gate)
    done = await admit(service, "r1", "before")
    await service.wait(done.admission_id)
    draining[0] = True
    with pytest.raises(RuntimeError):
        await admit(service, "r2", "during the drain")
    assert ledger.find(store, ME, "s1", "r2") is None
    retry = await admit(service, "r1", "before")  # an exact retry still answers
    assert retry.replayed and model.calls == ["before"]
    await service.aclose()


@aio
async def test_stopping_a_turn_that_waits_for_the_lease_ends_the_wait(store: StoreHandle) -> None:
    other = LeaseHolder("pid=9:pidns=ns:box:start=7:lease=x", 9, 7, "ns", "box")
    turn_leases.try_acquire(store, "s1", other, ttl=3600)
    model = Model()
    leases = TurnLeases(store, probe=_probe(), write=InlineWrites(store))
    service = await opened(store, model, leases=leases)
    first = await admit(service, "r1", "waits for the lease")
    clock = clock_of(store)
    await clock.advance_and_settle(3)
    assert (await service.receipt(first.admission_id)).state == "started"
    await service.cancel(first.admission_id)
    await clock.run_until_idle()
    done = await service.wait(first.admission_id)
    assert done.outcome == "cancelled" and model.calls == []
    second = await admit(service, "r2", "shutdown while waiting")
    await clock.advance_and_settle(3)
    await service.aclose()  # returns at once: the wait is ended, not sat out
    row = ledger.get(store, second.admission_id)
    assert row is not None and row.outcome == "interrupted" and row.detail == SHUTDOWN
