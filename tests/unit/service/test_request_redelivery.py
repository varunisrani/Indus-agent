"""P17-07: server requests offered to viewers, re-offered on reattach, settled exactly once."""

from __future__ import annotations

import asyncio
import itertools
import re
import time
from collections.abc import Callable, Coroutine, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from indus.protocol.errors import SERVER_REQUEST_NOT_FOUND, SERVER_REQUEST_UNSUPPORTED
from indus.protocol.events import CancelReason, ServerRequestCancelled
from indus.protocol.methods import ProtocolError
from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice, ApprovalRequest
from indus.service.broker import (
    AnswerModel,
    RequestUnsupported,
    RequestWithdrawn,
    ServerRequestBroker,
)
from indus.service.request_redelivery import (
    NOT_SHOWN_CODE,
    UNRENDERABLE_CODE,
    Offer,
    RequestBook,
    RequestSettlement,
    RequestViewer,
    new_request_id,
)
from tests.fakes.clock import FakeClock

ALL_KINDS = ("approval", "clarify", "sudo", "secret")
APPROVAL = {"command": "rm -rf build", "description": "removes build output"}
SUDO = {"reason": "install a package"}


def approval_request(request_id: str) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=request_id,
        session_id="s1",
        command=APPROVAL["command"],
        description=APPROVAL["description"],
    )


class Viewer:
    """A fake viewer that records what it was offered."""

    def __init__(self, viewer_id: str, kinds: Iterable[str] = ALL_KINDS, **kw: Any) -> None:
        self.offers: list[Offer] = []
        self.viewer = RequestViewer.of(viewer_id, kinds, self.offers.append, **kw)

    @property
    def ids(self) -> list[str]:
        return [o.request.request_id for o in self.offers]


class Rig:
    def __init__(self, generation: int | None = 7) -> None:
        self.clock = FakeClock(datetime(2026, 10, 19, 9, 0, tzinfo=UTC))
        self.settled: list[Mapping[str, Any]] = []
        self.cancelled: list[ServerRequestCancelled] = []
        self.delivered: list[object] = []
        self.book = RequestBook("s1", emit=self.settled.append, generation=lambda: generation)
        seqs = itertools.count(1)
        self.broker = ServerRequestBroker(
            session_id="s1",
            deliver=self.delivered.append,
            emit=self.cancelled.append,
            next_seq=lambda: next(seqs),
            clock=self.clock,
            book=self.book,
        )

    def ask(self, kind: str = "approval", payload: Mapping[str, Any] = APPROVAL, **kw: Any):
        return asyncio.ensure_future(self.broker.ask(kind, payload, **kw))

    async def settle(self) -> None:
        await self.clock.run_until_idle()


def run(body: Callable[[Rig], Coroutine[Any, Any, None]], rig: Rig | None = None) -> Rig:
    rig = rig or Rig()
    asyncio.run(body(rig))
    return rig


def outcomes(rig: Rig) -> list[tuple[str, str]]:
    return [(e["request_id"], e["outcome"]) for e in rig.settled]


# -- ids and offers ------------------------------------------------------------------------------


def test_request_ids_are_srq_with_twelve_hex() -> None:
    ids = {new_request_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(re.fullmatch(r"srq-[0-9a-f]{12}", i) for i in ids)


def test_offer_goes_to_capable_viewers_with_generation() -> None:
    async def body(rig: Rig) -> None:
        cli, gui = Viewer("cli", ["approval"]), Viewer("gui", ["sudo"])
        rig.book.attach(cli.viewer)
        rig.book.attach(gui.viewer)
        task = rig.ask()
        await rig.settle()
        (offer,) = cli.offers
        assert gui.offers == [] and rig.delivered == []  # the book delivers, not ``deliver``
        assert offer.replayed is False and offer.generation == 7
        assert re.fullmatch(r"srq-[0-9a-f]{12}", offer.request.request_id)
        wire = offer.to_wire()
        assert wire["replayed"] is False and wire["generation"] == 7
        assert wire["request"]["request_id"] == offer.request.request_id
        task.cancel()
        await rig.settle()

    run(body)


def test_offer_wire_without_generation_omits_it() -> None:
    request = approval_request("srq-000000000001")
    assert "generation" not in Offer(request, None, replayed=True).to_wire()


# -- re-offer ------------------------------------------------------------------------------------


def test_reattach_reoffers_open_approval_with_same_id() -> None:
    async def body(rig: Rig) -> None:
        tui = Viewer("tui")
        rig.book.attach(tui.viewer)
        task = rig.ask()
        await rig.settle()
        (first,) = tui.offers
        assert rig.book.detach("tui")
        assert rig.book.lookup(first.request.request_id) is not None  # detaching refuses nothing
        back = Viewer("tui")
        assert rig.book.attach(back.viewer) == 1
        (again,) = back.offers
        assert again.request.request_id == first.request.request_id and again.replayed
        assert again.generation == first.generation
        rig.broker.answer(again.request.request_id, {"choice": "once"})
        answer = await task
        assert isinstance(answer, ApprovalAnswer) and answer.choice is ApprovalChoice.ONCE

    run(body)


def test_events_since_reoffers_still_open_requests() -> None:
    async def body(rig: Rig) -> None:
        tui = Viewer("tui")
        rig.book.attach(tui.viewer)
        tasks = [rig.ask(), rig.ask("sudo", SUDO)]
        await rig.settle()
        rig.broker.answer(tui.offers[0].request.request_id, {"choice": "deny"})
        await rig.settle()
        assert rig.book.reoffer("tui") == 1
        assert tui.offers[-1].replayed and tui.offers[-1].request.kind == "sudo"
        assert rig.book.reoffer("nobody") == 0
        rig.broker.cancel_all(CancelReason.INTERRUPTED)
        await asyncio.gather(*tasks, return_exceptions=True)

    run(body)


def test_viewer_that_refused_is_not_reoffered() -> None:
    async def body(rig: Rig) -> None:
        a, b = Viewer("a"), Viewer("b")
        rig.book.attach(a.viewer)
        rig.book.attach(b.viewer)
        task = rig.ask()
        await rig.settle()
        request_id = a.ids[0]
        assert rig.book.refuse("a", request_id, UNRENDERABLE_CODE)
        assert rig.book.reoffer("a") == 0
        assert rig.book.reoffer("b") == 1
        rig.broker.answer(request_id, {"choice": "once"})
        await task

    run(body)


# -- first answer wins ---------------------------------------------------------------------------


def test_two_answers_first_wins_and_one_settled_event() -> None:
    async def body(rig: Rig) -> None:
        cli, tui = Viewer("cli"), Viewer("tui")
        rig.book.attach(cli.viewer)
        rig.book.attach(tui.viewer)
        task = rig.ask()
        await rig.settle()
        request_id = cli.ids[0]
        assert tui.ids == [request_id]
        assert rig.broker.answer(request_id, {"choice": "session"}).accepted
        with pytest.raises(ProtocolError) as err:
            rig.broker.answer(request_id, {"choice": "deny"})
        assert err.value.error is SERVER_REQUEST_NOT_FOUND
        answer = await task
        assert isinstance(answer, ApprovalAnswer) and answer.choice is ApprovalChoice.SESSION
        assert outcomes(rig) == [(request_id, "answered")]
        assert rig.settled[0]["type"] == "request.settled" and rig.settled[0]["kind"] == "approval"
        assert rig.book.lookup(request_id) is None and rig.book.reoffer("tui") == 0

    run(body)


def test_book_settle_is_first_wins() -> None:
    rig = Rig()
    request = approval_request("srq-0000000000aa")
    rig.book.attach(Viewer("v").viewer)
    assert rig.book.offer(request) is not None
    with pytest.raises(ValueError):
        rig.book.offer(request)
    assert rig.book.settle(request.request_id, RequestSettlement.ANSWERED)
    assert not rig.book.settle(request.request_id, RequestSettlement.WITHDRAWN)
    assert outcomes(rig) == [(request.request_id, "answered")]


# -- unrenderable and declined -------------------------------------------------------------------


def test_all_viewers_unrenderable_settles_undeliverable_at_once() -> None:
    async def body(rig: Rig) -> None:
        loop = asyncio.get_running_loop()

        def refusing(viewer_id: str) -> RequestViewer:
            def send(offer: Offer) -> None:  # answers -32601 as soon as the frame arrives
                loop.call_soon(rig.book.refuse, viewer_id, offer.request.request_id, -32601)

            return RequestViewer.of(viewer_id, ALL_KINDS, send)

        rig.book.attach(refusing("old-tui"))
        rig.book.attach(refusing("old-gui"))
        started = time.perf_counter()
        task = rig.ask()
        with pytest.raises(RequestUnsupported) as err:
            await task
        elapsed = time.perf_counter() - started
        assert elapsed < 0.05
        assert err.value.kind == "approval"
        assert err.value.error.code == SERVER_REQUEST_UNSUPPORTED.code
        (event,) = rig.settled
        assert event["outcome"] == "undeliverable" and event["reason"] == "unsupported"
        assert rig.cancelled == []  # not a withdrawal and not a timeout
        assert not rig.broker.waiting

    run(body)


def test_unrenderable_by_one_viewer_answered_by_another() -> None:
    async def body(rig: Rig) -> None:
        a, b = Viewer("a"), Viewer("b")
        rig.book.attach(a.viewer)
        rig.book.attach(b.viewer)
        task = rig.ask()
        await rig.settle()
        request_id = a.ids[0]
        assert rig.book.refuse("a", request_id, UNRENDERABLE_CODE)
        assert rig.book.lookup(request_id) is not None
        rig.broker.answer(request_id, {"choice": "once"})
        assert isinstance(await task, ApprovalAnswer)

    run(body)


def test_declined_by_one_viewer_answered_by_another() -> None:
    async def body(rig: Rig) -> None:
        window1 = Viewer("window-1", can_decline=True)
        window2 = Viewer("window-2", can_decline=True)
        rig.book.attach(window1.viewer)
        rig.book.attach(window2.viewer)
        task = rig.ask()
        await rig.settle()
        request_id = window1.ids[0]
        assert rig.book.refuse("window-1", request_id, NOT_SHOWN_CODE)
        entry = rig.book.lookup(request_id)
        assert entry is not None and entry.refused == {"window-1": NOT_SHOWN_CODE}
        rig.broker.answer(request_id, {"choice": "always"})
        answer = await task
        assert isinstance(answer, ApprovalAnswer) and answer.choice is ApprovalChoice.ALWAYS
        assert outcomes(rig) == [(request_id, "answered")]

    run(body)


def test_all_declined_is_undeliverable_not_shown() -> None:
    async def body(rig: Rig) -> None:
        rig.book.attach(Viewer("w1", can_decline=True).viewer)
        rig.book.attach(Viewer("w2", can_decline=True).viewer)
        task = rig.ask()
        await rig.settle()
        (entry,) = rig.book.open_requests()
        rig.book.refuse("w1", entry.request_id, NOT_SHOWN_CODE)
        rig.book.refuse("w2", entry.request_id, NOT_SHOWN_CODE)
        with pytest.raises(RequestUnsupported) as err:
            await task
        assert "declined" in err.value.error.message
        assert rig.settled[0]["reason"] == "not_shown"

    run(body)


def test_decline_without_capability_is_ignored() -> None:
    async def body(rig: Rig) -> None:
        cli = Viewer("cli")  # never advertised decline
        rig.book.attach(cli.viewer)
        task = rig.ask()
        await rig.settle()
        assert not rig.book.refuse("cli", cli.ids[0], NOT_SHOWN_CODE)
        assert rig.book.lookup(cli.ids[0]) is not None and not task.done()
        assert not rig.book.refuse("cli", "srq-ffffffffffff", UNRENDERABLE_CODE)
        rig.broker.answer(cli.ids[0], {"choice": "deny"})
        await task

    run(body)


def test_other_viewer_error_counts_as_refusal() -> None:
    async def body(rig: Rig) -> None:
        crashy = Viewer("crashy")
        rig.book.attach(crashy.viewer)
        task = rig.ask()
        await rig.settle()
        assert rig.book.refuse("crashy", crashy.ids[0], -32603)  # its handler crashed
        with pytest.raises(RequestUnsupported):
            await task
        assert rig.settled[0]["reason"] == "unsupported"

    run(body)


def test_kind_no_viewer_renders_is_refused_up_front() -> None:
    async def body(rig: Rig) -> None:
        with pytest.raises(RequestUnsupported):
            await rig.broker.ask("approval", APPROVAL)  # nobody attached
        rig.book.attach(Viewer("gui", ["approval"]).viewer)
        assert rig.broker.answerable_kinds == frozenset({"approval"})
        assert rig.book.can_render("approval") and not rig.book.can_render("sudo")
        with pytest.raises(RequestUnsupported):
            await rig.broker.ask("sudo", SUDO)
        assert rig.settled == [] and rig.book.open_requests() == []

    run(body)


def test_book_offer_with_no_capable_viewer_settles_undeliverable() -> None:
    rig = Rig()
    hooked: list[str] = []
    rig.book.bind_undeliverable(lambda request_id, error: hooked.append(request_id))
    request = approval_request("srq-0000000000bb")
    assert rig.book.offer(request) is None
    assert hooked == [request.request_id]
    assert outcomes(rig) == [(request.request_id, "undeliverable")]


# -- withdrawal and failures ---------------------------------------------------------------------


def test_withdrawal_emits_settled_withdrawn() -> None:
    async def body(rig: Rig) -> None:
        tui = Viewer("tui")
        rig.book.attach(tui.viewer)
        task = rig.ask()
        await rig.settle()
        assert rig.broker.cancel_all(CancelReason.INTERRUPTED) == 1
        with pytest.raises(RequestWithdrawn):
            await task
        (event,) = rig.settled
        assert event["outcome"] == "withdrawn" and event["reason"] == "interrupted"
        assert [e.request_id for e in rig.cancelled] == tui.ids  # the P04 event stays

    run(body)


def test_timeout_withdraws_and_settles_once() -> None:
    async def body(rig: Rig) -> None:
        rig.book.attach(Viewer("tui").viewer)
        task = rig.ask(timeout=30)
        await rig.settle()
        await rig.clock.advance_and_settle(30)
        with pytest.raises(RequestWithdrawn):
            await task
        assert [e["reason"] for e in rig.settled] == ["timed_out"]

    run(body)


def test_failing_viewer_send_keeps_request_for_others(caplog: pytest.LogCaptureFixture) -> None:
    async def body(rig: Rig) -> None:
        def broken(_: Offer) -> None:
            raise ConnectionError("socket gone")

        rig.book.attach(RequestViewer.of("dead", ALL_KINDS, broken))
        good = Viewer("good")
        rig.book.attach(good.viewer)
        task = rig.ask()
        await rig.settle()
        assert "could not offer server request" in caplog.text
        rig.broker.answer(good.ids[0], {"choice": "once"})
        answer: AnswerModel = await task
        assert isinstance(answer, ApprovalAnswer)

    run(body)


def test_viewer_kinds_filter_unknown_names() -> None:
    viewer = RequestViewer.of("v", ["approval", "telepathy"], lambda offer: None)
    assert viewer.kinds == frozenset({"approval"})
    rig = Rig()
    rig.book.attach(viewer)
    assert rig.book.viewers() == ["v"] and rig.book.answerable_kinds() == {"approval"}
    assert not rig.book.detach("nobody")
