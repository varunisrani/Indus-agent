"""Sampling and elicitation (P13-10): limits, conversion, routing to a person."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import mcp_types as types
import pytest
from mcp.client.session import ClientRequestContext

from indus.foundation.clock import SystemClock
from indus.mcp.config import ElicitationSettings, McpServer, SamplingSettings
from indus.mcp.connect import ConnectOptions, StderrLog, open_session
from indus.mcp.sampling import (
    AUX_TASK,
    ElicitationHandler,
    Requesters,
    SamplingHandler,
    SlidingWindow,
    aux_backend,
    session_kwargs,
)
from indus.protocol.envelope import ErrorObject
from indus.protocol.events import CancelReason
from indus.protocol.requests_port import RequestUnsupported, RequestWithdrawn
from indus.protocol.server_requests import ClarifyAnswer
from indus.providers.types import NormalizedResponse, ToolCall, Usage
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.terminal import terminal_scope

START = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
CTX = cast(ClientRequestContext, None)
ASKING = Path(__file__).resolve().parents[2] / "fakes" / "mcp" / "asking_server.py"


class FakeBackend:
    def __init__(self, answers: list[NormalizedResponse | BaseException] | None = None) -> None:
        self.answers = list(answers or [])
        self.calls: list[dict[str, Any]] = []
        self.hang = False

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None,
        max_tokens: int,
        temperature: float | None,
        tools: Sequence[Mapping[str, Any]],
        timeout_s: float,
    ) -> tuple[NormalizedResponse, str]:
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "model": model,
                "max_tokens": max_tokens,
                "tools": list(tools),
            }
        )
        if self.hang:
            await asyncio.Event().wait()
        answer = self.answers.pop(0) if self.answers else NormalizedResponse(content="ok")
        if isinstance(answer, BaseException):
            raise answer
        return answer, model or "default-model"


def ask(
    text: str = "hello", *, max_tokens: int = 100, hint: str | None = None, **extra: Any
) -> types.CreateMessageRequestParams:
    prefs = types.ModelPreferences(hints=[types.ModelHint(name=hint)]) if hint is not None else None
    return types.CreateMessageRequestParams(
        messages=extra.pop(
            "messages",
            [types.SamplingMessage(role="user", content=types.TextContent(type="text", text=text))],
        ),
        max_tokens=max_tokens,
        model_preferences=prefs,
        **extra,
    )


def handler(
    backend: FakeBackend, clock: FakeClock | None = None, **settings: Any
) -> SamplingHandler:
    return SamplingHandler(
        "files", SamplingSettings(**settings), backend, clock=clock or FakeClock(START)
    )


# -- sampling limits ----------------------------------------------------------------------------


def test_the_eleventh_request_in_a_minute_is_refused() -> None:
    clock = FakeClock(START)
    backend = FakeBackend()
    sampler = handler(backend, clock)

    async def go() -> list[object]:
        answers: list[object] = []
        for _ in range(11):
            answers.append(await sampler(CTX, ask()))
            clock.advance(1)
        return answers

    answers = asyncio.run(go())
    assert all(isinstance(a, types.CreateMessageResult) for a in answers[:10])
    refused = answers[10]
    assert isinstance(refused, types.ErrorData) and "10 sampling requests" in refused.message
    assert len(backend.calls) == 10
    clock.advance(50)  # the first request is now more than 60 s old
    assert isinstance(asyncio.run(sampler(CTX, ask())), types.CreateMessageResult)
    assert sampler.metrics.refused == 1 and sampler.metrics.requests == 12


def test_sliding_window_counts_only_the_last_minute() -> None:
    clock = FakeClock(START)
    window = SlidingWindow(2, clock)
    assert window.admit() and window.admit() and not window.admit()
    clock.advance(59.9)
    assert not window.admit()
    clock.advance(0.2)
    assert window.admit()


def test_max_tokens_is_capped() -> None:
    backend = FakeBackend()
    asyncio.run(handler(backend)(CTX, ask(max_tokens=50_000)))
    asyncio.run(handler(backend)(CTX, ask(max_tokens=200)))
    assert [c["max_tokens"] for c in backend.calls] == [4096, 200]


def test_a_model_outside_allowed_models_is_refused() -> None:
    backend = FakeBackend()
    sampler = handler(backend, allowed_models=["small-model"])
    refused = asyncio.run(sampler(CTX, ask(hint="huge-model")))
    assert isinstance(refused, types.ErrorData) and "huge-model" in refused.message
    assert backend.calls == []
    answer = asyncio.run(sampler(CTX, ask(hint="small-model")))
    assert isinstance(answer, types.CreateMessageResult) and answer.model == "small-model"
    asyncio.run(sampler(CTX, ask()))  # no hint: the first allowed model
    assert [c["model"] for c in backend.calls] == ["small-model", "small-model"]


def test_configured_model_wins_over_the_hint_and_any_model_by_default() -> None:
    backend = FakeBackend()
    asyncio.run(handler(backend, model="pinned")(CTX, ask(hint="other")))
    asyncio.run(handler(backend)(CTX, ask(hint="other")))
    asyncio.run(handler(backend)(CTX, ask()))
    assert [c["model"] for c in backend.calls] == ["pinned", "other", None]


def test_disabled_sampling_is_refused() -> None:
    backend = FakeBackend()
    answer = asyncio.run(handler(backend, enabled=False)(CTX, ask()))
    assert isinstance(answer, types.ErrorData) and backend.calls == []


def tool_round(index: int) -> list[types.SamplingMessage]:
    return [
        types.SamplingMessage(
            role="assistant",
            content=[types.ToolUseContent(type="tool_use", id=f"c{index}", name="look", input={})],
        ),
        types.SamplingMessage(
            role="user",
            content=[
                types.ToolResultContent(
                    type="tool_result",
                    tool_use_id=f"c{index}",
                    content=[types.TextContent(type="text", text=f"seen {index}")],
                )
            ],
        ),
    ]


def test_tool_loops_are_bounded() -> None:
    backend = FakeBackend(
        [
            NormalizedResponse(
                content=None, tool_calls=(ToolCall("c9", "look", "{}"),), finish_reason="tool_calls"
            )
        ]
    )
    sampler = handler(backend, max_tool_rounds=2)
    first = [types.SamplingMessage(role="user", content=types.TextContent(type="text", text="go"))]
    deep = first + tool_round(1) + tool_round(2) + tool_round(3)
    refused = asyncio.run(sampler(CTX, ask(messages=deep)))
    assert isinstance(refused, types.ErrorData) and "2 rounds" in refused.message
    assert backend.calls == []
    # At the limit, an answer that would start another round is refused too.
    at_limit = first + tool_round(1) + tool_round(2)
    refused = asyncio.run(sampler(CTX, ask(messages=at_limit)))
    assert isinstance(refused, types.ErrorData)


def test_tool_use_answer_and_message_conversion() -> None:
    backend = FakeBackend(
        [
            NormalizedResponse(
                content=None,
                tool_calls=(
                    ToolCall("c2", "look", '{"where": "here"}'),
                    ToolCall("c3", "x", "{bad"),
                ),
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=7, completion_tokens=3),
            )
        ]
    )
    sampler = handler(backend)
    image = types.ImageContent(type="image", data="aGk=", mime_type="image/png")
    messages = [
        types.SamplingMessage(
            role="user", content=[types.TextContent(type="text", text="what is this"), image]
        ),
        *tool_round(1),
    ]
    tool = types.Tool(name="look", description="Look around", input_schema={"type": "object"})
    answer = asyncio.run(
        sampler(CTX, ask(messages=messages, system_prompt="be brief", tools=[tool]))
    )
    assert isinstance(answer, types.CreateMessageResultWithTools)
    assert answer.stop_reason == "toolUse"
    uses = answer.content if isinstance(answer.content, list) else [answer.content]
    assert [(u.id, u.input) for u in uses if isinstance(u, types.ToolUseContent)] == [
        ("c2", {"where": "here"}),
        ("c3", {"_raw": "{bad"}),
    ]
    sent = backend.calls[0]
    chat = sent["messages"]
    assert chat[0] == {"role": "system", "content": "be brief"}
    assert chat[1]["content"][1]["image_url"]["url"] == "data:image/png;base64,aGk="
    assert chat[2]["tool_calls"][0]["function"]["name"] == "look"
    assert chat[3] == {"role": "tool", "tool_call_id": "c1", "content": "seen 1"}
    assert sent["tools"][0]["function"]["name"] == "look"
    assert sampler.metrics.tokens == 10 and sampler.metrics.tool_use == 1


def test_text_answer_maps_the_stop_reason() -> None:
    backend = FakeBackend([NormalizedResponse(content="cut", finish_reason="length")])
    answer = asyncio.run(handler(backend)(CTX, ask()))
    assert isinstance(answer, types.CreateMessageResult)
    assert answer.stop_reason == "maxTokens" and answer.model == "default-model"


def test_failures_become_error_data() -> None:
    clock = FakeClock(START)
    backend = FakeBackend([RuntimeError("provider down")])
    sampler = handler(backend, clock)
    answer = asyncio.run(sampler(CTX, ask()))
    assert isinstance(answer, types.ErrorData) and "provider down" in answer.message
    backend.hang = True

    async def go() -> object:
        task = asyncio.ensure_future(sampler(CTX, ask()))
        while not clock.pending:
            await checkpoint()
        clock.advance(30)
        return await task

    timed_out = asyncio.run(go())
    assert isinstance(timed_out, types.ErrorData) and "30 s" in timed_out.message
    assert sampler.metrics.errors == 2


class FakeAux:
    """The part of ``AuxClient`` the sampling backend uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def route_for(self, candidate: Any) -> Any:
        return type("Route", (), {"model": candidate.model})()

    async def call(self, task: str, messages: Any, **kwargs: Any) -> Any:
        self.calls.append(("call", task, kwargs))
        return self._result("aux-default")

    async def call_route(self, task: str, route: Any, messages: Any, **kwargs: Any) -> Any:
        self.calls.append(("call_route", task, kwargs))
        return self._result(route.model)

    def _result(self, model: str) -> Any:
        route = type("Route", (), {"model": model})()
        return type("Result", (), {"response": NormalizedResponse(content="hi"), "route": route})()


def test_the_auxiliary_call_is_labelled_mcp_sampling() -> None:
    aux = FakeAux()
    backend = aux_backend(aux)
    answer, model = asyncio.run(
        backend.complete(
            [{"role": "user", "content": "x"}],
            model=None,
            max_tokens=5,
            temperature=None,
            tools=[],
            timeout_s=30,
        )
    )
    assert answer.content == "hi" and model == "aux-default"
    asyncio.run(
        backend.complete([], model="pinned", max_tokens=5, temperature=0.2, tools=[], timeout_s=3)
    )
    assert [(kind, task) for kind, task, _ in aux.calls] == [
        ("call", AUX_TASK),
        ("call_route", AUX_TASK),
    ]
    assert aux.calls[1][2]["temperature"] == 0.2 and AUX_TASK == "mcp_sampling"


# -- elicitation --------------------------------------------------------------------------------


class FakePort:
    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.asked: list[tuple[str, dict[str, Any], float | None]] = []

    async def ask(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        turn_id: str | None = None,
    ) -> Any:
        self.asked.append((kind, dict(payload), timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def cancel_all(self, reason: CancelReason) -> int:
        return 0

    def human_wait_seconds(self) -> float:
        return 0.0


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email": {"type": "string", "title": "Email", "description": "where to write"},
        "age": {"type": "integer"},
        "subscribe": {"type": "boolean"},
        "plan": {"type": "string", "enum": ["free", "pro"]},
    },
    "required": ["email", "age"],
}


def form(schema: dict[str, Any] | None = None) -> types.ElicitRequestFormParams:
    return types.ElicitRequestFormParams(
        message="Who are you?", requested_schema=schema if schema is not None else SCHEMA
    )


def elicitor(requesters: Requesters, **settings: Any) -> ElicitationHandler:
    return ElicitationHandler(
        "files", ElicitationSettings(**settings), requesters, clock=FakeClock(START)
    )


def test_form_elicitation_is_answered_through_clarify() -> None:
    requesters = Requesters()
    port = FakePort(
        ClarifyAnswer(
            answers={"email": ["a@b.test"], "age": ["41"], "subscribe": ["yes"], "plan": []}
        )
    )
    with requesters.serving(port):
        result = asyncio.run(elicitor(requesters)(CTX, form()))
    assert isinstance(result, types.ElicitResult) and result.action == "accept"
    assert result.content == {"email": "a@b.test", "age": 41, "subscribe": True}
    [(kind, payload, timeout)] = port.asked
    assert kind == "clarify" and timeout == 300.0
    questions = {q["id"]: q for q in payload["questions"]}
    assert (
        "Who are you?" in questions["email"]["text"]
        and "where to write" in questions["email"]["text"]
    )
    assert questions["subscribe"]["choices"] == ["yes", "no"]
    assert questions["plan"]["choices"] == ["free", "pro"]
    assert questions["plan"]["text"].endswith("(optional)")


def test_skipped_required_field_or_bad_number_declines() -> None:
    requesters = Requesters()
    for answers in ({"email": ["a@b.test"], "age": []}, {"email": ["a@b.test"], "age": ["old"]}):
        with requesters.serving(FakePort(ClarifyAnswer(answers=answers))):
            result = asyncio.run(elicitor(requesters)(CTX, form()))
        assert isinstance(result, types.ElicitResult) and result.action == "decline"


def test_empty_schema_is_a_plain_accept_or_decline() -> None:
    requesters = Requesters()
    with requesters.serving(FakePort(ClarifyAnswer(answers={"_confirm": ["Accept"]}))):
        result = asyncio.run(elicitor(requesters)(CTX, form({"type": "object", "properties": {}})))
    assert isinstance(result, types.ElicitResult)
    assert result.action == "accept" and result.content == {}


def test_url_mode_is_declined() -> None:
    requesters = Requesters()
    port = FakePort(ClarifyAnswer(answers={}))
    params = types.ElicitRequestURLParams(
        message="Sign in", url="https://example.test", elicitation_id="e1"
    )
    with requesters.serving(port):
        result = asyncio.run(elicitor(requesters)(CTX, params))
    assert isinstance(result, types.ElicitResult) and result.action == "decline"
    assert port.asked == []


def test_unattended_or_missing_requester_declines() -> None:
    requesters = Requesters()
    port = FakePort(ClarifyAnswer(answers={"email": ["x"], "age": ["1"]}))
    assert asyncio.run(elicitor(requesters)(CTX, form())).action == "decline"  # type: ignore[union-attr]
    with requesters.serving(port, unattended=True):  # a cron session
        result = asyncio.run(elicitor(requesters)(CTX, form()))
    assert isinstance(result, types.ElicitResult) and result.action == "decline"
    assert port.asked == []


def test_timeouts_cancel_and_unsupported_clients_decline() -> None:
    requesters = Requesters()
    withdrawn = RequestWithdrawn("r1", CancelReason.TIMED_OUT)
    with requesters.serving(FakePort(withdrawn)):
        result = asyncio.run(elicitor(requesters)(CTX, form()))
    assert isinstance(result, types.ElicitResult) and result.action == "cancel"
    unsupported = RequestUnsupported("clarify", ErrorObject(code=4003, message="no"))
    with requesters.serving(FakePort(unsupported)):
        result = asyncio.run(elicitor(requesters)(CTX, form()))
    assert isinstance(result, types.ElicitResult) and result.action == "decline"


def test_latest_call_in_flight_answers() -> None:
    requesters = Requesters()
    older, newer = FakePort(None), FakePort(None)
    with requesters.serving(older), requesters.serving(newer) as entry:
        current = requesters.current()
        assert current is not None and current is entry and current.port is newer
    assert requesters.current() is None


# -- wiring and the real SDK --------------------------------------------------------------------


def test_disabled_features_are_not_advertised() -> None:
    clock = FakeClock(START)
    on = session_kwargs(
        "files", McpServer(command="x"), clock=clock, backend=FakeBackend(), requesters=Requesters()
    )
    assert set(on.kwargs) == {"sampling_callback", "sampling_capabilities", "elicitation_callback"}
    off = session_kwargs(
        "files",
        McpServer(command="x", sampling=SamplingSettings(enabled=False)),
        clock=clock,
        backend=FakeBackend(),
        requesters=None,
    )
    assert off.kwargs == {}


def test_a_real_server_samples_and_asks_through_the_session(tmp_path: Path) -> None:
    scope = terminal_scope(tmp_path)
    server = McpServer(command=sys.executable, args=[str(ASKING)], connect_timeout=30)
    backend = FakeBackend([NormalizedResponse(content="short", finish_reason="stop")])
    requesters = Requesters()
    handlers = session_kwargs(
        "asking", server, clock=SystemClock(), backend=backend, requesters=requesters
    )
    options = ConnectOptions(
        session_kwargs=handlers.kwargs, stderr_log=StderrLog(tmp_path / "stderr.log")
    )
    port = FakePort(
        ClarifyAnswer(answers={"email": ["me@x.test"], "age": ["30"], "subscribe": ["no"]})
    )

    def text(result: types.CallToolResult) -> str:
        return "".join(p.text for p in result.content if isinstance(p, types.TextContent))

    async def go() -> None:
        async with open_session(
            "asking", server, scope, clock=SystemClock(), options=options
        ) as live:
            summary = await live.call_tool("summarize", {"text": "a long text"})
            assert text(summary) == "small-model|endTurn|short"
            with requesters.serving(port):
                signed = await live.call_tool("signup", {})
                assert text(signed) == "accept|me@x.test|30|False"
                opened = await live.call_tool("open_page", {})
                assert text(opened) == "decline"
            refused = await live.call_tool("signup", {})  # nobody is asking now
            assert text(refused).startswith("decline")

    asyncio.run(go())
    assert backend.calls[0]["max_tokens"] == 4096


@pytest.mark.parametrize("level", ["debug", "warning", "nonsense"])
def test_log_level_setting_is_accepted(level: str) -> None:
    assert handler(FakeBackend(), log_level=level).settings.log_level == level
