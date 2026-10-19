"""P17-05: controls name their generation; stale ones are refused, never re-aimed (R01.4, R06.2)."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from typing import Any

import pytest

from indus.foundation.errors import ERROR_CODES
from indus.foundation.scope import Scope
from indus.protocol.events import CancelReason
from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice
from indus.service.admissions import (
    CANCELLED,
    INTERRUPTED,
    SYSTEM_INTERRUPT,
    AdmissionService,
    Receipt,
    TurnOutcome,
    TurnTicket,
)
from indus.service.broker import RequestWithdrawn, ServerRequestBroker
from indus.service.fences import (
    STALE_GENERATION,
    ControlApplied,
    ControlStatus,
    GenerationFences,
    RequestNotOpen,
    Responded,
    StaleGeneration,
    StopOutcome,
)
from indus.service.request_redelivery import Offer, RequestBook, RequestViewer
from indus.service.turn_lease import TurnLeases
from indus.state import registry, turn_leases
from indus.state import runtime_ledger as ledger
from indus.state.registry import StoreHandle
from indus.state.runtime_ledger import AdmissionState, LedgerRefusal
from indus.state.turn_leases import LeaseHolder, ProcessProbe
from tests.fakes.clock import FakeClock
from tests.unit.backends._aio import aio

APPROVAL = {"command": "git push --force", "description": "rewrites the remote branch"}


class Controls:
    """A running turn's controls that record every call."""

    def __init__(self, generation: int, *, accept: bool = True) -> None:
        self.generation = generation
        self.accept = accept
        self.calls: list[tuple[Any, ...]] = []

    def interrupt(self, generation: int, *, hard: bool = False, by_system: bool = False) -> bool:
        self.calls.append(("interrupt", generation, hard, by_system))
        return self.accept and generation == self.generation

    def steer(self, text: str) -> bool:
        self.calls.append(("steer", text))
        return self.accept


def applied(result: ControlApplied | StaleGeneration) -> ControlApplied:
    assert isinstance(result, ControlApplied), result
    return result


def running_turn(fences: GenerationFences, generation: int, session: str = "s1") -> Controls:
    controls = Controls(generation)
    fences.begin(session, generation)
    assert fences.adopt(session, generation, controls)
    return controls


# -- interrupt and steer -------------------------------------------------------------------------


def test_interrupt_with_older_generation_is_stale_and_leaves_turn_running() -> None:
    fences = GenerationFences()
    controls = running_turn(fences, 4)
    result = fences.interrupt("s1", 3)
    assert isinstance(result, StaleGeneration) and result == StaleGeneration("s1", 3, 4)
    assert result.to_data() == {"reason": STALE_GENERATION, "running": True, "generation": 4}
    assert controls.calls == [] and fences.running("s1") == 4


def test_interrupt_of_running_generation_is_delivered() -> None:
    fences = GenerationFences()
    controls = running_turn(fences, 4)
    assert fences.interrupt("s1", 4, hard=True) == ControlApplied("s1", 4, ControlStatus.DELIVERED)
    assert controls.calls == [("interrupt", 4, True, False)]
    controls.accept = False  # already stopping
    assert applied(fences.interrupt("s1", 4)).status is ControlStatus.REJECTED


def test_interrupt_when_idle_or_for_a_future_generation_is_stale() -> None:
    fences = GenerationFences()
    idle = fences.interrupt("s1", 1)
    assert isinstance(idle, StaleGeneration) and idle.current is None
    assert idle.to_data() == {"reason": STALE_GENERATION, "running": False}
    running_turn(fences, 2)
    future = fences.interrupt("s1", 3)
    assert isinstance(future, StaleGeneration) and future.current == 2


def test_steer_names_its_generation() -> None:
    fences = GenerationFences()
    controls = running_turn(fences, 9)
    assert isinstance(fences.steer("s1", 8, "use the other file"), StaleGeneration)
    assert applied(fences.steer("s1", 9, "use the other file")).status is ControlStatus.DELIVERED
    assert controls.calls == [("steer", "use the other file")]
    with pytest.raises(ValueError):
        fences.steer("s1", 9, "   ")


def test_sessions_are_fenced_separately() -> None:
    fences = GenerationFences()
    a = running_turn(fences, 1, "a")
    running_turn(fences, 5, "b")
    assert isinstance(fences.interrupt("a", 5), StaleGeneration)
    assert a.calls == []


# -- lifecycle -----------------------------------------------------------------------------------


def test_generations_only_grow() -> None:
    fences = GenerationFences()
    fences.begin("s1", 3)
    with pytest.raises(ValueError):
        fences.begin("s1", 3)
    with pytest.raises(ValueError):
        fences.begin("s1", 2)
    assert fences.latest("s1") == 3 and fences.latest("other") is None


def test_displaced_turn_cannot_release_its_successor() -> None:
    fences = GenerationFences()
    old = running_turn(fences, 1)
    new = running_turn(fences, 2)  # the old claim was displaced (lease lost and re-claimed)
    assert not fences.release("s1", 1)
    assert fences.running("s1") == 2
    assert isinstance(fences.interrupt("s1", 1), StaleGeneration)
    assert old.calls == [] and new.calls == []
    assert fences.release("s1", 2)
    assert fences.running("s1") is None
    assert not fences.release("s1", 2)
    assert not fences.release("never", 1)


def test_latched_interrupt_delivered_on_adopt() -> None:
    fences = GenerationFences()
    fences.begin("s1", 6)
    assert applied(fences.interrupt("s1", 6, hard=True)).status is ControlStatus.LATCHED
    controls = Controls(6)
    assert fences.adopt("s1", 6, controls)
    assert controls.calls == [("interrupt", 6, True, False)]


def test_latched_steers_delivered_in_order_on_adopt() -> None:
    fences = GenerationFences()
    fences.begin("s1", 2)
    assert applied(fences.steer("s1", 2, "first")).status is ControlStatus.LATCHED
    fences.steer("s1", 2, "second")
    fences.interrupt("s1", 2, by_system=True)
    controls = Controls(2)
    fences.adopt("s1", 2, controls)
    assert controls.calls == [
        ("steer", "first"),
        ("steer", "second"),
        ("interrupt", 2, False, True),
    ]


def test_latched_control_dropped_when_generation_released() -> None:
    fences = GenerationFences()
    fences.begin("s1", 1)
    fences.interrupt("s1", 1)
    fences.steer("s1", 1, "late")
    assert fences.release("s1", 1)
    later = running_turn(fences, 2)
    assert later.calls == []  # never reaches a later generation


def test_adopt_for_a_generation_that_is_not_running_is_refused() -> None:
    fences = GenerationFences()
    assert not fences.adopt("s1", 1, Controls(1))
    fences.begin("s1", 2)
    assert not fences.adopt("s1", 1, Controls(1))


# -- approval and clarify answers ----------------------------------------------------------------


class Session:
    """A broker with a request book bound to the fence, one viewer attached."""

    def __init__(self, fences: GenerationFences) -> None:
        self.clock = FakeClock(datetime(2026, 10, 19, 9, 0, tzinfo=UTC))
        self.offers: list[Offer] = []
        self.book = RequestBook("s1", emit=lambda e: None, generation=lambda: fences.running("s1"))
        self.book.attach(RequestViewer.of("tui", ["approval", "clarify"], self.offers.append))
        seqs = itertools.count(1)
        self.broker = ServerRequestBroker(
            session_id="s1",
            deliver=lambda r: None,
            emit=lambda e: None,
            next_seq=lambda: next(seqs),
            clock=self.clock,
            book=self.book,
        )
        fences.bind_requests("s1", self.book, self.broker.answer)


def test_respond_with_running_generation_answers_once() -> None:
    async def body() -> None:
        fences = GenerationFences()
        running_turn(fences, 5)
        session = Session(fences)
        task = asyncio.ensure_future(session.broker.ask("approval", APPROVAL))
        await session.clock.run_until_idle()
        (offer,) = session.offers
        assert offer.generation == 5
        request_id = offer.request.request_id
        assert fences.respond("s1", request_id, 5, {"choice": "once"}) == Responded(
            request_id, True
        )
        answer = await task
        assert isinstance(answer, ApprovalAnswer) and answer.choice is ApprovalChoice.ONCE
        # a second answer from an outdated card has no effect
        second = fences.respond("s1", request_id, 5, {"choice": "deny"})
        assert second == RequestNotOpen(request_id)

    asyncio.run(body())


def test_respond_with_stale_generation_is_refused() -> None:
    async def body() -> None:
        fences = GenerationFences()
        running_turn(fences, 5)
        session = Session(fences)
        task = asyncio.ensure_future(session.broker.ask("approval", APPROVAL))
        await session.clock.run_until_idle()
        request_id = session.offers[0].request.request_id
        stale = fences.respond("s1", request_id, 4, {"choice": "always"})
        assert stale == StaleGeneration("s1", 4, 5)
        assert session.book.lookup(request_id) is not None and not task.done()
        # the asking turn was displaced: its request can no longer be answered by generation 5
        running_turn(fences, 6)
        assert fences.respond("s1", request_id, 5, {"choice": "once"}) == StaleGeneration(
            "s1", 5, 6
        )
        task.cancel()
        await session.clock.run_until_idle()

    asyncio.run(body())


def test_approval_answered_for_withdrawn_request_is_a_no_op() -> None:
    async def body() -> None:
        fences = GenerationFences()
        running_turn(fences, 5)
        session = Session(fences)
        task = asyncio.ensure_future(session.broker.ask("approval", APPROVAL))
        await session.clock.run_until_idle()
        request_id = session.offers[0].request.request_id
        session.broker.cancel_all(CancelReason.INTERRUPTED)
        with pytest.raises(RequestWithdrawn):
            await task
        assert fences.respond("s1", request_id, 5, {"choice": "once"}) == RequestNotOpen(request_id)
        assert fences.respond("s1", "srq-000000000000", 5, {"choice": "once"}) == RequestNotOpen(
            "srq-000000000000"
        )

    asyncio.run(body())


def test_respond_without_bound_requests_is_not_open() -> None:
    fences = GenerationFences()
    running_turn(fences, 1)
    assert isinstance(fences.respond("s1", "srq-1", 1, {"choice": "once"}), RequestNotOpen)


def test_request_asked_outside_a_turn_has_no_generation_check() -> None:
    async def body() -> None:
        fences = GenerationFences()
        session = Session(fences)  # nothing running: offers carry no generation
        task = asyncio.ensure_future(session.broker.ask("approval", APPROVAL))
        await session.clock.run_until_idle()
        offer = session.offers[0]
        assert offer.generation is None
        result = fences.respond("s1", offer.request.request_id, 0, {"choice": "deny"})
        assert isinstance(result, Responded) and result.accepted
        await task

    asyncio.run(body())


# -- stop(admission): the real admission service (P17-02) -------------------------------------


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


async def inline[T](fn: Callable[[], T]) -> T:
    return fn()


class Model:
    """A turn runner that holds each turn until released or stopped."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.tickets: dict[str, TurnTicket] = {}
        self.gates: dict[str, asyncio.Event] = {}

    async def __call__(self, ticket: TurnTicket) -> TurnOutcome:
        self.calls.append(ticket.text)
        self.tickets[ticket.text] = ticket
        gate = self.gates.setdefault(ticket.text, asyncio.Event())
        waits = {asyncio.ensure_future(gate.wait()), asyncio.ensure_future(ticket.stopped.wait())}
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for task in waits:
            task.cancel()
        if ticket.stop_reason == CANCELLED:
            return TurnOutcome("cancelled")
        if ticket.stop_reason:
            return TurnOutcome("interrupted")
        return TurnOutcome("completed", final_text=f"done: {ticket.text}")

    def release(self, text: str) -> None:
        self.gates.setdefault(text, asyncio.Event()).set()


class Daemon:
    """One admission service with the fence wired in, as the daemon builds them."""

    def __init__(self, store: StoreHandle) -> None:
        self.store = store
        self.model = Model()
        self.fences = GenerationFences()
        self.service: AdmissionService | None = None

    async def open(self, **options: Any) -> AdmissionService:
        self.service = await AdmissionService.open(
            self.store,
            instance_id="daemon-1",
            runner=self.model,
            offload=inline,
            fences=self.fences,
            **options,
        )
        self.fences.bind_admissions(self.service)
        return self.service

    async def admit(self, request_id: str, text: str, session_id: str = "s1") -> Receipt:
        assert self.service is not None
        return await self.service.admit("cli:alice", session_id, request_id, {"text": text})

    async def settle(self) -> None:
        clock = self.store.scope.clock
        assert isinstance(clock, FakeClock)
        await clock.run_until_idle()


@aio
async def test_stop_queued_admission_cancels_it(store: StoreHandle) -> None:
    daemon = Daemon(store)
    service = await daemon.open()
    running = await daemon.admit("r1", "long job")
    queued = await daemon.admit("r2", "next")
    await daemon.settle()
    result = await daemon.fences.stop("s1", queued.admission_id)
    assert result.outcome is StopOutcome.CANCELLED
    after = await service.receipt(queued.admission_id)
    assert after.state == "terminal" and after.outcome == "cancelled"
    assert (await service.receipt(running.admission_id)).state == "started"
    assert daemon.model.tickets["long job"].stop_reason is None
    await service.aclose()


@aio
async def test_stop_started_admission_interrupts_exactly_its_generation(
    store: StoreHandle,
) -> None:
    daemon = Daemon(store)
    service = await daemon.open()
    receipt = await daemon.admit("r1", "long job")
    await daemon.settle()
    assert daemon.fences.running("s1") == 1
    result = await daemon.fences.stop("s1", receipt.admission_id)
    assert result.outcome is StopOutcome.INTERRUPTED and result.generation == 1
    assert result.status is ControlStatus.DELIVERED
    done = await service.wait(receipt.admission_id)
    assert done.outcome == "cancelled"
    await daemon.settle()
    assert daemon.fences.running("s1") is None  # released when the turn ended
    await service.aclose()


@aio
async def test_stop_of_a_finished_admission_never_hits_the_next_turn(store: StoreHandle) -> None:
    daemon = Daemon(store)
    service = await daemon.open()
    first = await daemon.admit("r1", "one")
    second = await daemon.admit("r2", "two")
    await daemon.settle()
    daemon.model.release("one")
    await service.wait(first.admission_id)
    await daemon.settle()
    assert daemon.fences.running("s1") == 2
    result = await daemon.fences.stop("s1", first.admission_id)
    assert result.outcome is StopOutcome.ALREADY_SETTLED
    assert daemon.model.tickets["two"].stop_reason is None
    assert (await service.receipt(second.admission_id)).state == "started"
    await service.aclose()


@aio
async def test_interrupts_through_the_fence_reach_only_the_named_generation(
    store: StoreHandle,
) -> None:
    daemon = Daemon(store)
    service = await daemon.open()
    first = await daemon.admit("r1", "one")
    second = await daemon.admit("r2", "two")
    await daemon.settle()
    daemon.model.release("one")
    await service.wait(first.admission_id)
    await daemon.settle()
    stale = daemon.fences.interrupt("s1", 1)  # an old card of the finished turn
    assert stale == StaleGeneration("s1", 1, 2)
    assert daemon.model.tickets["two"].stop_reason is None
    assert applied(daemon.fences.interrupt("s1", 2)).status is ControlStatus.DELIVERED
    done = await service.wait(second.admission_id)
    assert done.outcome == "interrupted"
    row = ledger.get(store, second.admission_id)
    assert row is not None and row.detail == INTERRUPTED
    await service.aclose()


@aio
async def test_a_system_interrupt_is_recorded_as_one(store: StoreHandle) -> None:
    daemon = Daemon(store)
    service = await daemon.open()
    receipt = await daemon.admit("r1", "one")
    await daemon.settle()
    assert applied(daemon.fences.interrupt("s1", 1, by_system=True)).status is (
        ControlStatus.DELIVERED
    )
    assert (await service.wait(receipt.admission_id)).outcome == "interrupted"
    row = ledger.get(store, receipt.admission_id)
    assert row is not None and row.detail == SYSTEM_INTERRUPT
    await service.aclose()


@aio
async def test_stop_names_its_session(store: StoreHandle) -> None:
    daemon = Daemon(store)
    service = await daemon.open()
    mine = await daemon.admit("r1", "mine")
    theirs = await daemon.admit("r2", "theirs", session_id="s2")
    await daemon.settle()
    # an admission of another session is never stopped through this one, queued or running
    assert (await daemon.fences.stop("s1", theirs.admission_id)).outcome is StopOutcome.NOT_FOUND
    assert (await daemon.fences.stop("s2", mine.admission_id)).outcome is StopOutcome.NOT_FOUND
    assert (await daemon.fences.stop("s1", "missing")).outcome is StopOutcome.NOT_FOUND
    assert daemon.model.tickets["mine"].stop_reason is None
    assert daemon.model.tickets["theirs"].stop_reason is None
    with pytest.raises(RuntimeError):
        await GenerationFences().stop("s1", mine.admission_id)
    await service.aclose()


@aio
async def test_stop_of_an_unknown_admission_changes_nothing(store: StoreHandle) -> None:
    first = Daemon(store)
    old = await first.open()
    lost = await first.admit("lost", "risky")
    later = await first.admit("later", "after that")
    await first.settle()
    for task in list(old._workers.values()):  # pyright: ignore[reportPrivateUsage]
        task.cancel()  # the daemon dies mid-turn
    await first.settle()
    second = Daemon(store)
    service = await second.open()
    await second.settle()
    result = await second.fences.stop("s1", lost.admission_id)
    assert result.outcome is StopOutcome.UNKNOWN and result.generation == 1
    assert (await service.receipt(lost.admission_id)).state == "unknown"
    cancelled = await second.fences.stop("s1", later.admission_id)  # queued behind it
    assert cancelled.outcome is StopOutcome.CANCELLED
    assert second.model.calls == []  # nothing ran again
    await service.aclose()


@aio
async def test_stop_while_the_turn_waits_for_the_lease_ends_the_wait(store: StoreHandle) -> None:
    other = LeaseHolder("pid=9:pidns=ns:box:start=7:lease=x", 9, 7, "ns", "box")
    turn_leases.try_acquire(store, "s1", other, ttl=3600)
    probe = ProcessProbe(
        alive=lambda pid: True, start_time=lambda pid: 7, namespace=lambda: "ns", host=lambda: "box"
    )
    daemon = Daemon(store)
    service = await daemon.open(leases=TurnLeases(store, probe=probe))
    receipt = await daemon.admit("r1", "waits")
    clock = store.scope.clock
    assert isinstance(clock, FakeClock)
    await clock.advance_and_settle(3)
    assert (await service.receipt(receipt.admission_id)).state == "started"
    result = await daemon.fences.stop("s1", receipt.admission_id)
    assert result.outcome is StopOutcome.INTERRUPTED and result.status is ControlStatus.DELIVERED
    done = await service.wait(receipt.admission_id)
    assert done.outcome == "cancelled" and daemon.model.calls == []
    await service.aclose()


class ClaimedMeanwhile(AdmissionService):
    """The real service, with the worker claiming the head between ``stop``'s read and cancel."""

    before_cancel: Callable[[], Awaitable[None]] | None = None

    async def cancel(self, admission_id: str) -> Receipt:
        hook, self.before_cancel = self.before_cancel, None
        if hook is not None:
            await hook()
        return await super().cancel(admission_id)


@aio
async def test_stop_queued_claimed_meanwhile_stops_that_claim(store: StoreHandle) -> None:
    daemon = Daemon(store)
    model = daemon.model
    service = await ClaimedMeanwhile.open(
        store, instance_id="daemon-1", runner=model, offload=inline, fences=daemon.fences
    )
    assert isinstance(service, ClaimedMeanwhile)
    daemon.service = service
    daemon.fences.bind_admissions(service)
    first = await daemon.admit("r1", "another surface's turn")
    queued = await daemon.admit("r2", "mine")

    async def claim() -> None:
        model.release("another surface's turn")
        await service.wait(first.admission_id)
        await daemon.settle()

    service.before_cancel = claim
    await daemon.settle()
    result = await daemon.fences.stop("s1", queued.admission_id)
    assert result.outcome is StopOutcome.INTERRUPTED and result.generation == 2
    done = await service.wait(queued.admission_id)
    assert done.outcome == "cancelled"
    assert (await service.receipt(first.admission_id)).outcome == "completed"
    await service.aclose()


# -- stop(admission): races the real service cannot be paused in --------------------------------


class Receipts:
    """A double of ``AdmissionService.receipt``/``cancel``: K1's ``Receipt`` and refusals."""

    def __init__(self) -> None:
        self.rows: dict[str, Receipt] = {}
        self.calls: list[tuple[str, str]] = []
        self.before_cancel: Callable[[str], None] | None = None

    def add(self, admission_id: str, state: AdmissionState, generation: int | None = None) -> None:
        self.rows[admission_id] = Receipt(admission_id, state, "s1", generation=generation)

    def claim(self, admission_id: str, generation: int) -> None:
        self.rows[admission_id] = dc_replace(
            self.rows[admission_id], state="started", generation=generation
        )

    def _get(self, admission_id: str) -> Receipt:
        row = self.rows.get(admission_id)
        if row is None:
            raise LedgerRefusal("not_found", field="admission_id")
        return row

    async def receipt(self, admission_id: str) -> Receipt:
        self.calls.append(("receipt", admission_id))
        return self._get(admission_id)

    async def cancel(self, admission_id: str) -> Receipt:
        self.calls.append(("cancel", admission_id))
        if self.before_cancel is not None:
            self.before_cancel(admission_id)
        row = self._get(admission_id)
        if row.state == "started":  # the ledger's own race answer
            raise LedgerRefusal("admission_started", field="admission_id")
        if row.state == "queued":
            row = self.rows[admission_id] = dc_replace(row, state="terminal", outcome="cancelled")
        return row


class PlainReceipts(Receipts):
    """The same calls as plain (non-async) methods."""

    def receipt(self, admission_id: str) -> Receipt:  # type: ignore[override]
        self.calls.append(("receipt", admission_id))
        return self._get(admission_id)

    def cancel(self, admission_id: str) -> Receipt:  # type: ignore[override]
        self.calls.append(("cancel", admission_id))
        row = self._get(admission_id)
        if row.state == "queued":
            row = self.rows[admission_id] = dc_replace(row, state="terminal", outcome="cancelled")
        return row


def test_stop_when_the_ledger_refuses_the_cancel_as_started() -> None:
    async def body() -> None:
        admissions = Receipts()
        admissions.add("adm-2", "queued")
        fences = GenerationFences(admissions=admissions)
        other = running_turn(fences, 3)  # another surface's turn runs now
        claimed: list[Controls] = []

        def claim(admission_id: str) -> None:  # the worker claims between our read and cancel
            admissions.claim(admission_id, 4)
            fences.release("s1", 3)
            claimed.append(running_turn(fences, 4))

        admissions.before_cancel = claim
        result = await fences.stop("s1", "adm-2")
        assert result.outcome is StopOutcome.INTERRUPTED and result.generation == 4
        assert result.status is ControlStatus.DELIVERED
        assert claimed[0].calls == [("interrupt", 4, True, False)]
        assert other.calls == []

    asyncio.run(body())


def test_stop_of_a_generation_released_after_the_read_is_already_settled() -> None:
    async def body() -> None:
        admissions = Receipts()
        admissions.add("adm-4", "started", 7)  # receipt read just before the turn settled
        fences = GenerationFences(admissions=admissions)
        running_turn(fences, 7)
        fences.release("s1", 7)
        successor = running_turn(fences, 8)
        result = await fences.stop("s1", "adm-4")
        assert result.outcome is StopOutcome.ALREADY_SETTLED
        assert successor.calls == []

    asyncio.run(body())


def test_stop_before_the_claim_reaches_the_fence_is_kept_for_its_begin() -> None:
    async def body() -> None:
        admissions = Receipts()
        admissions.add("adm-5", "started", 2)
        fences = GenerationFences(admissions=admissions)
        running_turn(fences, 1)
        result = await fences.stop("s1", "adm-5")
        assert result.outcome is StopOutcome.INTERRUPTED
        assert result.status is ControlStatus.LATCHED
        fences.release("s1", 1)
        controls = running_turn(fences, 2)
        assert controls.calls == [("interrupt", 2, True, False)]

    asyncio.run(body())


def test_stop_works_with_a_plain_admission_service() -> None:
    async def body() -> None:
        admissions = PlainReceipts()
        admissions.add("adm-6", "queued")
        fences = GenerationFences(admissions=admissions)
        assert (await fences.stop("s1", "adm-6")).outcome is StopOutcome.CANCELLED
        assert (await fences.stop("s1", "adm-7")).outcome is StopOutcome.NOT_FOUND

    asyncio.run(body())


def test_stale_generation_shares_the_ledgers_error_code() -> None:
    assert StaleGeneration.error_code in ERROR_CODES
    assert StaleGeneration.code == LedgerRefusal("stale_generation").reason


def test_forget_drops_session_state() -> None:
    fences = GenerationFences()
    running_turn(fences, 3)
    fences.forget("s1")
    assert fences.running("s1") is None and fences.latest("s1") is None
