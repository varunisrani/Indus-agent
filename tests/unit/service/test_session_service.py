"""SessionService: create, resume, submit, admission, control, server requests (P04-21)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.core.exit_reasons import ExitReason
from indus.core.turn_state import TurnState
from indus.foundation import yamlio
from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.protocol.errors import (
    IDEMPOTENCY_CONFLICT,
    INTERNAL_ERROR,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
    SESSION_REFUSED,
    TEXT_EMPTY,
    ErrorCode,
)
from indus.protocol.events import (
    CancelReason,
    EventBase,
    MessageDelta,
    ServerRequestCancelled,
    TurnDone,
    TurnStarted,
)
from indus.protocol.methods import ProtocolError, RedirectOutcome, SessionCreateParams
from indus.protocol.requests_port import RequestNotAnswered
from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice, ApprovalRequest
from indus.service.session_service import BUSY_COPY, SessionService, protocol_error_for
from indus.state.session_lock import lock_file_name
from indus.tools.calls import ToolCall, ToolResult
from tests.fakes.agent import eventually, fake_options
from tests.fakes.clock import FakeClock
from tests.fakes.model import (
    FakeModelServer,
    Finish,
    Stall,
    Text,
    Turn,
    fake_route,
    script_of,
    text_turn,
    tool_batch,
)
from tests.fakes.turn import check_alternation

ROOT = Path(__file__).resolve().parents[3]


def stalled(text: str) -> Turn:
    return Turn((Text((text,)), Stall(1000.0), Finish("stop")))


async def confirm(call: ToolCall, state: TurnState) -> ToolResult:
    """An inline tool that asks the person for approval through the broker."""
    broker = state.control.broker
    assert broker is not None
    try:
        answer = await broker.ask(
            "approval", {"command": "rm -r build", "description": "clean up"}, turn_id=state.turn_id
        )
    except RequestNotAnswered as exc:
        return ToolResult(f"not approved: {exc}", is_error=True)
    assert isinstance(answer, ApprovalAnswer)
    return ToolResult(f"approved: {answer.choice.value}")


def service(
    scope: Scope,
    clock: FakeClock,
    tmp_path: Path,
    *turns: Turn,
    server: FakeModelServer | None = None,
    kinds: tuple[str, ...] = (),
    inline: Mapping[str, Any] | None = None,
) -> tuple[SessionService, FakeModelServer]:
    server = server or FakeModelServer(script_of(*turns), clock=clock)
    options = fake_options(server, session_id=None, inline=dict(inline or {}))
    svc = SessionService(
        scope,
        fake_route(server),
        toolsets=[],
        options=options,
        lock_dir=tmp_path / "locks",
        answerable_kinds=kinds,
    )
    return svc, server


def code_of(info: pytest.ExceptionInfo[ProtocolError]) -> ErrorCode:
    return info.value.error


async def first_event(svc: SessionService, sid: str, kind: type[EventBase]) -> EventBase:
    def found() -> list[EventBase]:
        return [e for e in svc.events(sid) if isinstance(e, kind)]

    await eventually(found)
    return found()[0]


# -- create / submit / events ---------------------------------------------------------------------


def test_create_submit_and_stream_events(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        svc, _ = service(scope, fake_clock, tmp_path, text_turn("Hel", "lo"))
        created = await svc.create()
        sid = created.session_id
        assert created.created is True
        ticket = await svc.submit(sid, "hi")
        assert ticket.generation == 1
        result = await svc.wait(sid)
        assert result.final_text == "Hello" and result.turn_id == ticket.turn_id
        events = svc.events(sid)
        assert isinstance(events[0], TurnStarted) and events[0].turn_id == ticket.turn_id
        assert isinstance(events[-1], TurnDone)
        assert "".join(e.text for e in events if isinstance(e, MessageDelta)) == "Hello"
        later = svc.events(sid, since=events[1].seq)
        assert [e.seq for e in later] == [e.seq for e in events[2:]]
        assert await svc.wait(sid, ticket.turn_id) is result
        await svc.aclose()

    asyncio.run(main())


def test_a_second_submit_while_busy_is_refused(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        svc, _ = service(scope, fake_clock, tmp_path, stalled("slow"), text_turn("next"))
        sid = (await svc.create()).session_id
        first = await svc.submit(sid, "one")
        with pytest.raises(ProtocolError) as busy:
            await svc.submit(sid, "two")
        assert code_of(busy) is SESSION_BUSY and busy.value.error.code == 3001
        assert busy.value.message == f"{BUSY_COPY['what']} {BUSY_COPY['todo']}"
        await first_event(svc, sid, MessageDelta)
        assert svc.interrupt(sid, first.generation).interrupted
        await svc.wait(sid)
        second = await svc.submit(sid, "two")
        assert second.generation == 2
        assert (await svc.wait(sid)).final_text == "next"
        await svc.aclose()

    asyncio.run(main())


def test_two_services_on_one_session(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        server = FakeModelServer(
            script_of(text_turn("from a"), text_turn("from b")), clock=fake_clock
        )
        a, _ = service(scope, fake_clock, tmp_path, server=server)
        b, _ = service(scope, fake_clock, tmp_path, server=server)
        b.store = a.store  # one store, two surfaces
        sid = (await a.create()).session_id
        await a.submit(sid, "hi")
        await a.wait(sid)
        with pytest.raises(ProtocolError) as busy:
            await b.submit(sid, "me too")
        assert code_of(busy) is SESSION_BUSY
        with pytest.raises(ProtocolError) as busy_resume:
            await b.resume(sid)
        assert code_of(busy_resume) is SESSION_BUSY
        await a.close(sid)
        resumed = await b.resume(sid)
        assert resumed.message_count == 2
        await b.submit(sid, "again")
        assert (await b.wait(sid)).final_text == "from b"
        await b.aclose()
        await a.aclose()

    asyncio.run(main())


def test_resume_of_an_unknown_session(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        svc, _ = service(scope, fake_clock, tmp_path)
        with pytest.raises(ProtocolError) as missing:
            await svc.resume("20260101_000000_nothing")
        assert code_of(missing) is SESSION_NOT_FOUND
        with pytest.raises(ProtocolError) as missing_submit:
            await svc.submit("20260101_000000_nothing", "hi")
        assert code_of(missing_submit) is SESSION_NOT_FOUND
        with pytest.raises(ProtocolError) as not_attached:
            svc.interrupt("nope")
        assert code_of(not_attached) is SESSION_NOT_FOUND
        await svc.aclose()

    asyncio.run(main())


def test_resume_reattaches_and_reconnects(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        svc, _ = service(scope, fake_clock, tmp_path, text_turn("one"))
        sid = (await svc.create()).session_id
        await svc.submit(sid, "hi")
        await svc.wait(sid)
        again = await svc.resume(sid)  # still attached: a reconnect
        assert again.message_count == 2 and again.last_seq == svc.events(sid)[-1].seq
        await svc.close(sid)
        assert not (tmp_path / "locks" / lock_file_name(sid)).exists()
        resumed = await svc.resume(sid)
        assert resumed.message_count == 2 and resumed.pending_requests == []
        await svc.aclose()

    asyncio.run(main())


def test_blank_text_is_refused(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        svc, _ = service(scope, fake_clock, tmp_path)
        sid = (await svc.create()).session_id
        for call in (svc.submit(sid, "  "),):
            with pytest.raises(ProtocolError) as blank:
                await call
            assert code_of(blank) is TEXT_EMPTY
        with pytest.raises(ProtocolError):
            svc.steer(sid, "")
        with pytest.raises(ProtocolError):
            svc.redirect(sid, "\n")
        await svc.aclose()

    asyncio.run(main())


def test_idempotent_create(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        svc, _ = service(scope, fake_clock, tmp_path)
        first = await svc.create(SessionCreateParams(idempotency_key="k1"))
        again = await svc.create(SessionCreateParams(idempotency_key="k1"))
        assert again.session_id == first.session_id and again.created is False
        with pytest.raises(ProtocolError) as clash:
            await svc.create(SessionCreateParams(idempotency_key="k1", cwd="/elsewhere"))
        assert code_of(clash) is IDEMPOTENCY_CONFLICT
        await svc.aclose()
        with pytest.raises(ProtocolError) as closed:
            await svc.create()
        assert code_of(closed) is INTERNAL_ERROR

    asyncio.run(main())


# -- control ---------------------------------------------------------------------------------------


def test_interrupt_checks_the_generation(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        svc, _ = service(scope, fake_clock, tmp_path, stalled("partial"))
        sid = (await svc.create()).session_id
        ticket = await svc.submit(sid, "go")
        await first_event(svc, sid, MessageDelta)
        assert svc.interrupt(sid, ticket.generation + 1).interrupted is False
        assert svc.interrupt(sid, ticket.generation).interrupted is True
        result = await svc.wait(sid)
        assert result.exit_reason is ExitReason.INTERRUPTED_BY_USER
        check_alternation(result.rows)
        assert svc.interrupt(sid).interrupted is False  # nothing runs any more
        await svc.aclose()

    asyncio.run(main())


def test_steer_left_after_a_text_answer_starts_the_next_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        svc, server = service(
            scope,
            fake_clock,
            tmp_path,
            Turn((Text(("thinking",)), Stall(5.0), Finish("stop"))),
            text_turn("follow-up done"),
        )
        sid = (await svc.create()).session_id
        await svc.submit(sid, "go")
        await first_event(svc, sid, MessageDelta)
        steered = svc.steer(sid, "also check the tests")
        assert steered.queued and steered.ticket
        fake_clock.advance(10.0)
        first = await svc.wait(sid)
        assert first.exit_reason is ExitReason.COMPLETED
        await eventually(lambda: len([e for e in svc.events(sid) if isinstance(e, TurnDone)]) == 2)
        second = await svc.wait(sid)
        assert second.final_text == "follow-up done" and second.generation == 2
        last_user = server.model_requests[-1].body["messages"][-1]
        assert "also check the tests" in str(last_user["content"])
        await svc.aclose()

    asyncio.run(main())


def test_steer_and_redirect_without_a_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        svc, _ = service(scope, fake_clock, tmp_path)
        sid = (await svc.create()).session_id
        assert svc.steer(sid, "more").queued is False
        assert svc.redirect(sid, "other").outcome is RedirectOutcome.REJECTED
        await svc.aclose()

    asyncio.run(main())


def test_redirect_during_a_model_call(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        svc, _ = service(
            scope, fake_clock, tmp_path, stalled("going left"), text_turn("going right")
        )
        sid = (await svc.create()).session_id
        await svc.submit(sid, "go")
        await first_event(svc, sid, MessageDelta)
        assert svc.redirect(sid, "go right instead").outcome is RedirectOutcome.REDIRECTED
        result = await svc.wait(sid)
        assert result.final_text == "going right"
        check_alternation(result.rows)
        await svc.aclose()

    asyncio.run(main())


# -- server requests ------------------------------------------------------------------------------


def test_interrupt_withdraws_a_pending_approval(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        svc, _ = service(
            scope,
            fake_clock,
            tmp_path,
            tool_batch(("confirm", {})),
            kinds=("approval",),
            inline={"confirm": confirm},
        )
        sid = (await svc.create()).session_id
        asked: list[object] = []
        svc.on_request(sid, asked.append)
        await svc.submit(sid, "clean up")
        await eventually(lambda: asked)
        request = asked[0]
        assert isinstance(request, ApprovalRequest)
        resumed = await svc.resume(sid)
        assert [r.request_id for r in resumed.pending_requests] == [request.request_id]
        assert svc.interrupt(sid).interrupted
        result = await svc.wait(sid)
        assert result.exit_reason is ExitReason.INTERRUPTED_BY_USER
        cancelled = [e for e in svc.events(sid) if isinstance(e, ServerRequestCancelled)]
        assert [(e.request_id, e.reason) for e in cancelled] == [
            (request.request_id, CancelReason.INTERRUPTED)
        ]
        with pytest.raises(ProtocolError):  # the request is gone
            svc.answer_server_request(sid, request.request_id, {"choice": "once"})
        check_alternation(result.rows)
        await svc.aclose()

    asyncio.run(main())


def test_an_answered_approval_lets_the_turn_finish(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        svc, _ = service(
            scope,
            fake_clock,
            tmp_path,
            tool_batch(("confirm", {})),
            text_turn("cleaned"),
            kinds=("approval",),
            inline={"confirm": confirm},
        )
        sid = (await svc.create()).session_id
        asked: list[object] = []
        svc.on_request(sid, asked.append)
        await svc.submit(sid, "clean up")
        await eventually(lambda: asked)
        request = asked[0]
        assert isinstance(request, ApprovalRequest)
        answered = svc.answer_server_request(
            sid, request.request_id, ApprovalAnswer(choice=ApprovalChoice.ONCE)
        )
        assert answered.accepted
        result = await svc.wait(sid)
        assert result.final_text == "cleaned"
        tool_rows = [r for r in result.rows if r.role == "tool"]
        assert tool_rows and "approved: once" in str(tool_rows[0].content)
        await svc.aclose()

    asyncio.run(main())


def test_a_kind_the_client_cannot_answer_fails_at_once(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    """R05.3: nothing is sent and the tool gets its answer without waiting."""

    async def main() -> None:
        svc, _ = service(
            scope,
            fake_clock,
            tmp_path,
            tool_batch(("confirm", {})),
            text_turn("ok, skipped"),
            inline={"confirm": confirm},
        )
        sid = (await svc.create()).session_id
        asked: list[object] = []
        svc.on_request(sid, asked.append)
        await svc.submit(sid, "clean up")
        result = await svc.wait(sid)
        assert asked == []
        tool_rows = [r for r in result.rows if r.role == "tool"]
        assert "not approved" in str(tool_rows[0].content)
        assert result.final_text == "ok, skipped"
        await svc.aclose()

    asyncio.run(main())


# -- errors and copy ------------------------------------------------------------------------------


def test_product_errors_map_onto_the_protocol_table() -> None:
    assert protocol_error_for(IndusError("store.session_not_found", "gone")).error is (
        SESSION_NOT_FOUND
    )
    assert protocol_error_for(IndusError("store.tombstoned", "deleted")).error is SESSION_REFUSED
    assert protocol_error_for(IndusError("session.busy", "held")).error is SESSION_BUSY
    assert protocol_error_for(RuntimeError("boom")).error is INTERNAL_ERROR
    same = ProtocolError(TEXT_EMPTY)
    assert protocol_error_for(same) is same


def test_busy_copy_matches_the_english_catalogue() -> None:
    catalogue = yamlio.load_safe(ROOT / "locales" / "en.yaml")
    assert catalogue["errors"]["session"]["busy"] == BUSY_COPY
