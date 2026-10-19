"""Server requests over the bridge (P12-30): capabilities gate, deadlines, cancels, clarify locks,
secrets kept out of logs and frames, and an approval answered during a real (fake-model) turn."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.protocol.errors import SERVER_REQUEST_NOT_FOUND
from indus.protocol.events import CancelReason
from indus.protocol.requests_port import RequestNotAnswered, RequestUnsupported, RequestWithdrawn
from indus.protocol.server_requests import ApprovalAnswer, ClarifyAnswer, SecretAnswer
from indus.tools.calls import ToolCall, ToolResult
from indus.tui_bridge.server_requests import SUDO_DEADLINE_S, BridgePort
from tests.contract.tui_bridge._harness import Rig, open_rig
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.model import text_turn, tool_batch

QUESTIONS = {
    "questions": [
        {"id": "q1", "text": "Which branch?", "choices": ["main", "dev"]},
        {"id": "q2", "text": "Anything else?"},
    ]
}


async def session_port(rig: Rig, *features: str) -> tuple[str, BridgePort]:
    if features:
        await rig.ok("client.capabilities", features=list(features))
    sid = (await rig.ok("session.create"))["session_id"]
    service = await rig.service()
    port = service.agent(sid).broker
    assert isinstance(port, BridgePort)
    return sid, port


def test_without_capabilities_nothing_is_sent(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            _sid, port = await session_port(rig)
            started = time.perf_counter()
            with pytest.raises(RequestUnsupported):
                await port.ask("approval", {"command": "rm -r x", "description": "deletes"})
            assert time.perf_counter() - started < 0.01
            assert rig.recorder.of("server_request") == []
            accepted = await rig.ok("client.capabilities", features=["server_requests.approval"])
            assert accepted == {"features": ["server_requests.approval"]}
            with pytest.raises(RequestUnsupported):
                await port.ask("secret", {"name": "DEMO_KEY", "prompt": "paste"})

    asyncio.run(main())


def test_sudo_times_out_at_120_seconds_with_one_cancel(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            _sid, port = await session_port(rig, "server_requests")
            ask = asyncio.ensure_future(port.ask("sudo", {"reason": "install"}, timeout=45.0))
            await eventually(lambda: bool(rig.recorder.of("server_request")))
            request = rig.recorder.of("server_request")[0]
            assert request["kind"] == "sudo" and request["deadline_s"] == SUDO_DEADLINE_S
            fake_clock.advance(SUDO_DEADLINE_S - 1)
            for _ in range(5):
                await checkpoint()
            assert not ask.done()
            fake_clock.advance(1)
            with pytest.raises(RequestWithdrawn) as info:
                await ask
            assert info.value.reason is CancelReason.TIMED_OUT
            cancels = rig.recorder.of("server_request.cancelled")
            assert len(cancels) == 1 and cancels[0]["request_id"] == request["request_id"]
            assert cancels[0]["reason"] == "timed_out"
            rig.recorder.check_all()

    asyncio.run(main())


def test_late_answer_is_refused(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            sid, port = await session_port(rig, "server_requests")
            ask = asyncio.ensure_future(
                port.ask("approval", {"command": "ls", "description": "lists"}, timeout=10.0)
            )
            await eventually(lambda: bool(rig.recorder.of("server_request")))
            request_id = rig.recorder.of("server_request")[0]["request_id"]
            fake_clock.advance(10.0)
            with pytest.raises(RequestWithdrawn):
                await ask
            late = await rig.err(
                "server_request.answer",
                session_id=sid,
                request_id=request_id,
                answer={"kind": "approval", "choice": "once"},
            )
            assert late["code"] == SERVER_REQUEST_NOT_FOUND.code
            assert len(rig.recorder.of("server_request.cancelled")) == 1

    asyncio.run(main())


def test_clarify_locks_resolve_on_the_last_answer(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            sid, port = await session_port(rig, "server_requests.clarify")
            ask = asyncio.ensure_future(port.ask("clarify", QUESTIONS))
            await eventually(lambda: bool(rig.recorder.of("server_request")))
            request = rig.recorder.of("server_request")[0]
            assert request["deadline_s"] == 300.0
            rid = request["request_id"]
            first = await rig.ok(
                "clarify.lock", session_id=sid, request_id=rid, question_id="q1", answer=["dev"]
            )
            assert first == {"remaining": ["q2"]} and not ask.done()
            bad = await rig.err(
                "clarify.lock", session_id=sid, request_id=rid, question_id="q9", answer=[]
            )
            assert bad["data"]["path"] == "question_id"
            last = await rig.ok(
                "clarify.lock", session_id=sid, request_id=rid, question_id="q2", answer=["no"]
            )
            assert last == {"remaining": []}
            answer = await ask
            assert isinstance(answer, ClarifyAnswer)
            assert answer.answers == {"q1": ["dev"], "q2": ["no"]}

    asyncio.run(main())


def test_clarify_timeout_keeps_locked_answers(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            sid, port = await session_port(rig, "server_requests")
            ask = asyncio.ensure_future(port.ask("clarify", QUESTIONS, timeout=60.0))
            await eventually(lambda: bool(rig.recorder.of("server_request")))
            rid = rig.recorder.of("server_request")[0]["request_id"]
            await rig.ok(
                "clarify.lock", session_id=sid, request_id=rid, question_id="q1", answer=["main"]
            )
            fake_clock.advance(60.0)
            answer = await ask
            assert isinstance(answer, ClarifyAnswer)
            assert answer.answers == {"q1": ["main"], "q2": []}
            assert len(rig.recorder.of("server_request.cancelled")) == 1

    asyncio.run(main())


def test_secret_for_a_closed_session_is_refused(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            sid, port = await session_port(rig, "server_requests")
            await rig.ok("session.close", session_id=sid)
            with pytest.raises(RequestWithdrawn) as info:
                await port.ask("secret", {"name": "DEMO_KEY", "prompt": "paste it"})
            assert info.value.reason is CancelReason.SESSION_CLOSED
            assert rig.recorder.of("server_request") == []

    asyncio.run(main())


def test_secret_value_never_logged_or_emitted(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    value = "sv-" + "Zq81Lm42Xa77Pp03"

    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            sid, port = await session_port(rig, "server_requests")
            ask = asyncio.ensure_future(port.ask("secret", {"name": "DEMO_KEY", "prompt": "key?"}))
            await eventually(lambda: bool(rig.recorder.of("server_request")))
            rid = rig.recorder.of("server_request")[0]["request_id"]
            bad = await rig.err(
                "server_request.answer",
                session_id=sid,
                request_id=rid,
                answer={"kind": "secret", "value": value, "extra": value},
            )
            assert value not in str(bad)
            done = await rig.ok(
                "server_request.answer",
                session_id=sid,
                request_id=rid,
                answer={"kind": "secret", "value": value},
            )
            assert done == {"accepted": True}
            answer = await ask
            assert isinstance(answer, SecretAnswer) and answer.value == value
            assert value not in repr(rig.recorder.frames)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(main())
    assert value not in caplog.text
    assert all(value not in str(r.args) for r in caplog.records)


def test_approval_answered_during_a_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def confirm(call: ToolCall, state: TurnState) -> ToolResult:
        broker: Any = state.control.broker
        try:
            answer = await broker.ask(
                "approval",
                {"command": "make clean", "description": "cleans"},
                turn_id=state.turn_id,
            )
        except RequestNotAnswered as exc:
            return ToolResult(f"not approved: {exc}", is_error=True)
        assert isinstance(answer, ApprovalAnswer)
        return ToolResult(f"approved: {answer.choice.value}")

    async def main() -> None:
        turns = (tool_batch(("confirm", {})), text_turn("cleaned"))
        async with open_rig(
            scope, fake_clock, tmp_path, *turns, inline={"confirm": confirm}
        ) as rig:
            await rig.ok("client.capabilities", features=["server_requests"])
            sid = (await rig.ok("session.create"))["session_id"]
            await rig.ok("prompt.submit", session_id=sid, text="clean up")
            await eventually(lambda: bool(rig.recorder.of("server_request")))
            request = rig.recorder.of("server_request")[0]
            assert request["kind"] == "approval" and request["turn_id"]
            answer = {"kind": "approval", "choice": "once"}
            done = await rig.ok(
                "server_request.answer",
                session_id=sid,
                request_id=request["request_id"],
                answer=answer,
            )
            assert done == {"accepted": True}
            await rig.turn_done()
            assert rig.recorder.of("message.complete")[0]["text"] == "cleaned"
            rig.recorder.check_all()

    asyncio.run(main())


def test_decline_answers_with_the_kinds_refusal(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            sid, port = await session_port(rig, "server_requests")
            asks = {
                "approval": {"command": "ls", "description": "lists"},
                "clarify": QUESTIONS,
                "sudo": {"reason": "install"},
                "secret": {"name": "DEMO_KEY", "prompt": "key?"},
            }
            for kind, payload in asks.items():
                ask = asyncio.ensure_future(port.ask(kind, payload))
                await eventually(
                    lambda kind=kind: (
                        rig.recorder.of("server_request")[-1:] != []
                        and rig.recorder.of("server_request")[-1]["kind"] == kind
                    )
                )
                rid = rig.recorder.of("server_request")[-1]["request_id"]
                done = await rig.ok(
                    "server_request.decline", session_id=sid, request_id=rid, reason="no card"
                )
                assert done == {"accepted": True}
                answer = await ask
                if isinstance(answer, ApprovalAnswer):
                    assert answer.choice.value == "deny" and answer.reason == "no card"
                elif isinstance(answer, ClarifyAnswer):
                    assert answer.answers == {"q1": [], "q2": []}
                else:
                    assert getattr(answer, "password", None) is None
                    assert getattr(answer, "value", None) is None
                again = await rig.err("server_request.decline", session_id=sid, request_id=rid)
                assert again["code"] == SERVER_REQUEST_NOT_FOUND.code

    asyncio.run(main())


def test_ping(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        async with open_rig(scope, fake_clock, tmp_path) as rig:
            assert await rig.ok("gateway.ping") == {"pong": True, "protocol_version": 1}

    asyncio.run(main())
