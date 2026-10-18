"""Server-request broker (P04-21): futures per request, withdrawal events, R05.3, wait clock."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Coroutine, Iterable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from indus.protocol.errors import (
    ANSWER_KIND_MISMATCH,
    INVALID_PARAMS,
    SERVER_REQUEST_NOT_FOUND,
    SERVER_REQUEST_UNSUPPORTED,
    ErrorCode,
)
from indus.protocol.events import CancelReason, ServerRequestCancelled
from indus.protocol.methods import ProtocolError, SessionResumeResult
from indus.protocol.server_requests import (
    ApprovalAnswer,
    ApprovalChoice,
    ClarifyAnswer,
    SudoAnswer,
)
from indus.service.broker import (
    AnswerModel,
    RequestModel,
    RequestUnsupported,
    RequestWithdrawn,
    ServerRequestBroker,
    ServerRequestPort,
)
from tests.fakes.clock import FakeClock

ALL_KINDS = ("approval", "clarify", "sudo", "secret")
APPROVAL = {"command": "make clean", "description": "removes build output"}
SUDO = {"reason": "install a package"}
CLARIFY = {
    "questions": [
        {"id": "lang", "text": "Which language?", "choices": ["py", "ts"]},
        {"id": "extras", "text": "Extras?", "choices": ["a", "b", "c"], "multi_select": True},
        {"id": "note", "text": "Anything else?"},
    ]
}


class Rig:
    def __init__(self, kinds: Iterable[str] = ALL_KINDS) -> None:
        self.clock = FakeClock(datetime(2026, 10, 18, 9, 0, tzinfo=UTC))
        self.sent: list[RequestModel] = []
        self.events: list[ServerRequestCancelled] = []
        seqs = itertools.count(1)
        ids = itertools.count(1)
        self.broker = ServerRequestBroker(
            session_id="s1",
            deliver=self.sent.append,
            emit=self.events.append,
            next_seq=lambda: next(seqs),
            clock=self.clock,
            answerable_kinds=kinds,
            new_id=lambda: f"sr_{next(ids)}",
        )

    def start(self, kind: str, payload: dict[str, Any], **kw: Any) -> asyncio.Task[AnswerModel]:
        return asyncio.ensure_future(self.broker.ask(kind, payload, **kw))

    async def settle(self) -> None:
        await self.clock.run_until_idle()


def run(body: Callable[[Rig], Coroutine[Any, Any, None]], rig: Rig | None = None) -> Rig:
    rig = rig or Rig()
    asyncio.run(body(rig))
    return rig


def _code(exc: pytest.ExceptionInfo[ProtocolError]) -> ErrorCode:
    return exc.value.error


def test_the_broker_fits_the_port() -> None:
    port: ServerRequestPort = Rig().broker
    assert port.human_wait_seconds() == 0.0


def test_an_answer_resolves_the_waiting_ask() -> None:
    async def body(rig: Rig) -> None:
        task = rig.start("approval", APPROVAL, turn_id="t1")
        await rig.settle()
        (request,) = rig.sent
        assert request.request_id == "sr_1" and request.session_id == "s1"
        assert request.turn_id == "t1" and request.deadline_s is None
        assert rig.broker.waiting and rig.broker.pending_requests() == [request]
        result = rig.broker.answer("sr_1", {"choice": "once"})
        assert result.accepted
        assert await task == ApprovalAnswer(choice=ApprovalChoice.ONCE)
        assert not rig.broker.waiting and rig.events == []

    run(body)


def test_answer_may_be_a_model() -> None:
    async def body(rig: Rig) -> None:
        task = rig.start("sudo", SUDO)
        await rig.settle()
        rig.broker.answer("sr_1", SudoAnswer(password=None))
        assert await task == SudoAnswer()

    run(body)


def test_kinds_the_client_cannot_answer_fail_at_once() -> None:
    async def body(rig: Rig) -> None:
        with pytest.raises(RequestUnsupported) as caught:
            await rig.broker.ask("clarify", CLARIFY, timeout=300)
        assert caught.value.kind == "clarify"
        assert caught.value.error.code == SERVER_REQUEST_UNSUPPORTED.code
        assert rig.sent == [] and rig.events == [] and not rig.broker.waiting
        assert rig.broker.human_wait_seconds() == 0.0

    run(body, Rig(kinds=["approval"]))


def test_a_client_that_declared_nothing_gets_no_requests() -> None:
    async def body(rig: Rig) -> None:
        for kind, payload in (("approval", APPROVAL), ("sudo", SUDO)):
            with pytest.raises(RequestUnsupported):
                await rig.broker.ask(kind, payload)
        assert rig.sent == []
        rig.broker.set_answerable_kinds(["sudo", "telepathy"])
        assert rig.broker.answerable_kinds == frozenset({"sudo"})

    run(body, Rig(kinds=()))


def test_a_timeout_withdraws_the_request_once() -> None:
    async def body(rig: Rig) -> None:
        task = rig.start("approval", APPROVAL, timeout=120)
        await rig.settle()
        assert rig.sent[0].deadline_s == 120
        await rig.clock.advance_and_settle(119.5)
        assert not task.done()
        await rig.clock.advance_and_settle(0.5)
        with pytest.raises(RequestWithdrawn) as caught:
            await task
        assert caught.value.reason is CancelReason.TIMED_OUT
        assert [(e.request_id, e.reason, e.seq) for e in rig.events] == [
            ("sr_1", CancelReason.TIMED_OUT, 1)
        ]
        with pytest.raises(ProtocolError) as late:
            rig.broker.answer("sr_1", {"choice": "once"})
        assert _code(late) is SERVER_REQUEST_NOT_FOUND
        assert rig.broker.cancel_all(CancelReason.TURN_ENDED) == 0
        assert len(rig.events) == 1

    run(body)


def test_an_answer_before_the_timeout_stops_the_timer() -> None:
    async def body(rig: Rig) -> None:
        task = rig.start("approval", APPROVAL, timeout=60)
        await rig.settle()
        rig.broker.answer("sr_1", {"choice": "deny"})
        assert (await task) == ApprovalAnswer(choice=ApprovalChoice.DENY)
        await rig.settle()
        assert rig.clock.pending == 0 and rig.events == []

    run(body)


def test_cancel_all_sends_one_event_per_open_request_in_order() -> None:
    async def body(rig: Rig) -> None:
        tasks = [
            rig.start("approval", APPROVAL),
            rig.start("clarify", CLARIFY, timeout=300),
            rig.start("sudo", SUDO),
        ]
        await rig.settle()
        assert rig.broker.cancel_all(CancelReason.INTERRUPTED) == 3
        assert [(e.request_id, e.seq, e.reason) for e in rig.events] == [
            ("sr_1", 1, CancelReason.INTERRUPTED),
            ("sr_2", 2, CancelReason.INTERRUPTED),
            ("sr_3", 3, CancelReason.INTERRUPTED),
        ]
        assert all(e.session_id == "s1" for e in rig.events)
        for task in tasks:
            with pytest.raises(RequestWithdrawn) as caught:
                await task
            assert caught.value.reason is CancelReason.INTERRUPTED
        assert rig.broker.cancel_all(CancelReason.TURN_ENDED) == 0
        assert len(rig.events) == 3 and rig.broker.pending_requests() == []

    run(body)


def test_a_cancelled_asker_withdraws_its_request_once() -> None:
    async def body(rig: Rig) -> None:
        task = rig.start("approval", APPROVAL, timeout=300)
        await rig.settle()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [(e.request_id, e.reason) for e in rig.events] == [
            ("sr_1", CancelReason.INTERRUPTED)
        ]
        assert rig.broker.cancel_all(CancelReason.INTERRUPTED) == 0
        assert len(rig.events) == 1

    run(body)


def test_an_answer_racing_a_hard_interrupt_is_not_accepted() -> None:
    async def body(rig: Rig) -> None:
        task = rig.start("approval", APPROVAL)
        await rig.settle()
        task.cancel()  # the asker has not run its handler yet
        assert not rig.broker.answer("sr_1", {"choice": "once"}).accepted
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rig.events == [] and not rig.broker.waiting

    run(body)


@pytest.mark.parametrize(
    ("request_id", "answer", "expected", "path"),
    [
        ("sr_9", {"choice": "once"}, SERVER_REQUEST_NOT_FOUND, "request_id"),
        ("sr_1", {"kind": "sudo"}, ANSWER_KIND_MISMATCH, "answer.kind"),
        ("sr_1", {"choice": "maybe"}, INVALID_PARAMS, "answer.choice"),
        ("sr_1", {"choice": "once", "extra": 1}, INVALID_PARAMS, "answer.extra"),
        ("sr_1", {"choice": "always"}, INVALID_PARAMS, "answer.choice"),
        ("sr_1", {}, INVALID_PARAMS, "answer.choice"),
    ],
)
def test_bad_answers_are_refused_and_the_request_stays_open(
    request_id: str, answer: dict[str, Any], expected: ErrorCode, path: str
) -> None:
    async def body(rig: Rig) -> None:
        payload = {**APPROVAL, "choices": ["once", "deny"]}
        task = rig.start("approval", payload)
        await rig.settle()
        with pytest.raises(ProtocolError) as caught:
            rig.broker.answer(request_id, answer)
        assert _code(caught) is expected and caught.value.path == path
        assert rig.broker.waiting
        rig.broker.answer("sr_1", {"choice": "once"})
        assert (await task).kind == "approval"

    run(body)


def test_a_second_answer_finds_nothing() -> None:
    async def body(rig: Rig) -> None:
        task = rig.start("approval", APPROVAL)
        await rig.settle()
        rig.broker.answer("sr_1", {"choice": "once"})
        with pytest.raises(ProtocolError) as caught:
            rig.broker.answer("sr_1", {"choice": "deny"})
        assert _code(caught) is SERVER_REQUEST_NOT_FOUND
        assert (await task) == ApprovalAnswer(choice=ApprovalChoice.ONCE)

    run(body)


def test_clarify_answers_are_checked_and_gaps_filled_as_skipped() -> None:
    async def body(rig: Rig) -> None:
        task = rig.start("clarify", CLARIFY)
        await rig.settle()
        for bad, path in (
            ({"answers": {"colour": ["red"]}}, "answer.answers.colour"),
            ({"answers": {"lang": ["py", "ts"]}}, "answer.answers.lang"),
        ):
            with pytest.raises(ProtocolError) as caught:
                rig.broker.answer("sr_1", bad)
            assert _code(caught) is INVALID_PARAMS and caught.value.path == path
        rig.broker.answer("sr_1", {"answers": {"extras": ["a", "c"]}})
        assert await task == ClarifyAnswer(answers={"lang": [], "extras": ["a", "c"], "note": []})

    run(body)


def test_human_wait_counts_time_with_any_request_open_once() -> None:
    async def body(rig: Rig) -> None:
        first = rig.start("approval", APPROVAL)
        await rig.settle()
        await rig.clock.advance_and_settle(10)
        second = rig.start("sudo", SUDO)
        await rig.settle()
        await rig.clock.advance_and_settle(5)
        assert rig.broker.human_wait_seconds() == 15
        rig.broker.answer("sr_1", {"choice": "once"})
        await rig.clock.advance_and_settle(5)
        assert rig.broker.human_wait_seconds() == 20
        rig.broker.answer("sr_2", {"password": None})
        await rig.clock.advance_and_settle(100)
        assert rig.broker.human_wait_seconds() == 20
        await first
        await second
        third = rig.start("approval", APPROVAL, timeout=3)
        await rig.settle()
        await rig.clock.advance_and_settle(3)
        with pytest.raises(RequestWithdrawn):
            await third
        assert rig.broker.human_wait_seconds() == 23

    run(body)


def test_failing_transport_and_sink_are_not_fatal() -> None:
    def broken(_: object) -> None:
        raise ConnectionError("gone")

    async def body(rig: Rig) -> None:
        broker = ServerRequestBroker(
            session_id="s1",
            deliver=broken,
            emit=broken,
            next_seq=lambda: 1,
            clock=rig.clock,
            answerable_kinds=ALL_KINDS,
        )
        task = asyncio.ensure_future(broker.ask("approval", APPROVAL))
        await rig.settle()
        (open_request,) = broker.pending_requests()
        assert open_request.request_id.startswith("sr_")
        assert broker.cancel_all(CancelReason.TURN_ENDED) == 1
        with pytest.raises(RequestWithdrawn):
            await task

    run(body)


def test_open_requests_ride_along_on_resume() -> None:
    async def body(rig: Rig) -> None:
        tasks = [rig.start("approval", APPROVAL), rig.start("clarify", CLARIFY)]
        await rig.settle()
        resumed = SessionResumeResult(
            session_id="s1",
            message_count=0,
            last_seq=0,
            pending_requests=rig.broker.pending_requests(),
        )
        assert [r.kind for r in resumed.pending_requests] == ["approval", "clarify"]
        rig.broker.cancel_all(CancelReason.TURN_ENDED)
        await asyncio.gather(*tasks, return_exceptions=True)

    run(body)


def test_close_withdraws_and_refuses_later_asks() -> None:
    async def body(rig: Rig) -> None:
        task = rig.start("approval", APPROVAL)
        await rig.settle()
        assert rig.broker.close() == 1 and rig.broker.closed
        assert rig.events[0].reason is CancelReason.SESSION_CLOSED
        with pytest.raises(RequestWithdrawn):
            await task
        with pytest.raises(RequestWithdrawn) as caught:
            await rig.broker.ask("approval", APPROVAL)
        assert caught.value.reason is CancelReason.SESSION_CLOSED
        assert len(rig.sent) == 1 and len(rig.events) == 1

    run(body)


def test_programming_errors_send_nothing() -> None:
    async def body(rig: Rig) -> None:
        with pytest.raises(ValueError, match="unknown server request kind"):
            await rig.broker.ask("telepathy", {})
        with pytest.raises(ValueError, match="request_id"):
            await rig.broker.ask("approval", {**APPROVAL, "request_id": "mine"})
        with pytest.raises(ValidationError):
            await rig.broker.ask("approval", {"command": "x"})
        with pytest.raises(ValidationError):
            await rig.broker.ask("approval", APPROVAL, timeout=0)
        assert rig.sent == [] and not rig.broker.waiting

    run(body)
