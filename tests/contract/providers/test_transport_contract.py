"""Transport contract (P03-12): every transport × every capture and fake-model script.

Adding a transport means adding it to ``TRANSPORTS``, giving it captures and teaching the fake
model server its wire (P08 added ``responses``, ``bedrock`` and ``gemini``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.errors import ProviderHTTPError
from indus.foundation.scope import make_scope, use_scope
from indus.providers.eventstream import encode_message
from indus.providers.route import RuntimeRoute
from indus.providers.transports import get_transport, iter_call
from indus.providers.types import (
    FINISH_REASONS,
    ChatRequest,
    KeepAlive,
    NormalizedResponse,
    StreamEvent,
    TextDelta,
    UsageReport,
    arguments_object,
    assemble,
)
from tests.fakes.model import Finish, Reasoning, Script, Text, ToolCall, Turn, Usage
from tests.fakes.model.replay import ReplayTransport, chat_request_from, load_capture
from tests.fakes.model.server import FakeModelServer
from tests.unit.providers._support import make_route

TRANSPORTS = ["chat_completions", "anthropic_messages", "responses", "bedrock", "gemini"]
CAPTURES = ["text", "tool_call"]
EXPECTED_CAPTURE_FINISH = {"text": "stop", "tool_call": "tool_calls"}
# Wires whose server always reports usage, scripted or not.
ALWAYS_USAGE = frozenset({"anthropic_messages", "responses", "bedrock", "gemini"})
# Recordings that carry frames with no content (pings, comments, lifecycle events).
KEEPALIVE_CAPTURES = frozenset({"chat_completions", "anthropic_messages", "responses", "bedrock"})

SCRIPTS: dict[str, tuple[Turn, str]] = {
    "text": (Turn((Text(("Hel", "lo", "!")), Finish("stop"))), "stop"),
    "reasoning_text": (
        Turn((Reasoning(("think ", "more")), Text(("ok",)), Finish("stop"))),
        "stop",
    ),
    "one_tool": (
        Turn((ToolCall("read_file", {"path": "a.txt"}, id="call_1"), Finish("tool_calls"))),
        "tool_calls",
    ),
    "two_tools": (
        Turn(
            (
                ToolCall("a", {"x": 1}, id="c_a", fragments=2),
                ToolCall("b", {"y": [1, 2]}, id="c_b"),
                Finish("tool_calls"),
            )
        ),
        "tool_calls",
    ),
    "text_tool_usage": (
        Turn(
            (
                Text(("Looking.",)),
                ToolCall("find", {"q": "é ✓"}, id="c_f", fragments=4),
                Usage(prompt=120, completion=30, cached=100),
                Finish("tool_calls"),
            )
        ),
        "tool_calls",
    ),
    "length": (Turn((Text(("cut",)), Usage(prompt=5, completion=1), Finish("length"))), "length"),
}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _collect(
    client: httpx.AsyncClient, mode: str, route: RuntimeRoute, req: ChatRequest
) -> list[StreamEvent]:
    return [event async for event in iter_call(client, get_transport(mode), route, req)]


def check_contract(
    events: list[StreamEvent], *, expect_usage: bool, expect_finish: str
) -> NormalizedResponse:
    response = assemble(events)
    deltas = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert (response.content or "") == deltas
    for call in response.tool_calls:
        assert call.id and call.name
        assert arguments_object(call.arguments) is not None, call.arguments
    assert (response.usage is not None) == expect_usage
    assert response.finish_reason in FINISH_REASONS
    assert response.finish_reason == expect_finish
    keepalives = [e for e in events if isinstance(e, KeepAlive)]
    empty = assemble(keepalives)
    assert (empty.content, empty.tool_calls, empty.reasoning, empty.usage) == (None, (), None, None)
    return response


@pytest.mark.parametrize("scenario", CAPTURES)
@pytest.mark.parametrize("mode", TRANSPORTS)
def test_capture_contract(mode: str, scenario: str, tmp_path: Path) -> None:
    capture = load_capture(f"{mode}/{scenario}")
    meta = capture["meta"]
    route = RuntimeRoute(
        provider=meta["provider"],
        model=meta["model"],
        api_mode=mode,
        base_url=meta["base_url"],
        api_key=None,
    )

    async def main() -> list[StreamEvent]:
        async with httpx.AsyncClient(transport=ReplayTransport(capture)) as client:
            return await _collect(client, mode, route, chat_request_from(capture))

    # Bedrock signs with AWS credentials from the scope (fake ones; the replay ignores auth).
    aws = {
        "AWS_ACCESS_KEY_ID": "AKID" + "CONTRACT00",
        "AWS_SECRET_ACCESS_KEY": "contract" + "-secret",
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "none"),
    }
    with use_scope(make_scope(tmp_path / "home", env=aws)):
        events = run(main())
    response = check_contract(
        events, expect_usage=True, expect_finish=EXPECTED_CAPTURE_FINISH[scenario]
    )
    if mode in KEEPALIVE_CAPTURES:
        assert any(isinstance(e, KeepAlive) for e in events)
    if scenario == "tool_call":
        assert [c.name for c in response.tool_calls] == ["get_weather"]
        assert json.loads(response.tool_calls[0].arguments) == {"city": "Oslo"}
    else:
        assert response.content and "hello" in response.content.lower()


def _script_response(mode: str, name: str) -> NormalizedResponse:
    turn, finish = SCRIPTS[name]
    server = FakeModelServer(Script((turn,)))
    route = make_route(mode)
    req = ChatRequest(model=route.model, messages=[{"role": "user", "content": "go"}])

    async def main() -> list[StreamEvent]:
        async with httpx.AsyncClient(transport=server.transport) as client:
            return await _collect(client, mode, route, req)

    events = run(main())
    has_usage = any(isinstance(s, Usage) for s in turn.steps)
    assert any(isinstance(e, UsageReport) for e in events) or not has_usage
    return check_contract(
        events, expect_usage=has_usage or mode in ALWAYS_USAGE, expect_finish=finish
    )


@pytest.mark.parametrize("name", sorted(SCRIPTS))
@pytest.mark.parametrize("mode", TRANSPORTS)
def test_script_contract(mode: str, name: str) -> None:
    _script_response(mode, name)


@pytest.mark.parametrize("name", sorted(SCRIPTS))
@pytest.mark.parametrize("mode", [m for m in TRANSPORTS if m != "chat_completions"])
def test_same_script_same_shape_on_every_wire(mode: str, name: str) -> None:
    chat = _script_response("chat_completions", name)
    other = _script_response(mode, name)

    def shape(r: NormalizedResponse) -> tuple[Any, ...]:
        calls = [(c.name, json.loads(c.arguments)) for c in r.tool_calls]
        return (r.content, calls, r.finish_reason, r.reasoning)

    assert shape(chat) == shape(other)


class _NeverEnding(httpx.AsyncByteStream):
    def __init__(self, first: bytes) -> None:
        self.first = first
        self.closed = asyncio.Event()

    async def __aiter__(self):  # noqa: ANN204
        yield self.first
        await asyncio.Event().wait()  # the server goes silent forever
        yield b""

    async def aclose(self) -> None:
        self.closed.set()


FIRST_FRAME = {
    "chat_completions": b'data: {"choices": [{"index": 0, "delta": {"content": "x"}}]}\n\n',
    "anthropic_messages": (
        b'event: content_block_start\ndata: {"type": "content_block_start", "index": 0, '
        b'"content_block": {"type": "text", "text": "x"}}\n\n'
    ),
    "responses": (
        b"event: response.output_text.delta\ndata: "
        b'{"type": "response.output_text.delta", "output_index": 0, "delta": "x"}\n\n'
    ),
    "bedrock": encode_message(
        {":event-type": "contentBlockDelta", ":message-type": "event"},
        b'{"contentBlockIndex": 0, "delta": {"text": "x"}}',
    ),
    "gemini": (
        b'data: {"candidates": [{"content": {"role": "model", "parts": [{"text": "x"}]}}]}\n\n'
    ),
}


@pytest.mark.parametrize("mode", TRANSPORTS)
def test_cancellation_mid_stream_closes_the_response(mode: str) -> None:
    stream = _NeverEnding(FIRST_FRAME[mode])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async def main() -> None:
        got_first = asyncio.Event()
        received: list[StreamEvent] = []

        async def consume() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                async for event in iter_call(
                    client,
                    get_transport(mode),
                    make_route(mode),
                    ChatRequest(model="m", messages=[]),
                ):
                    received.append(event)
                    got_first.set()

        task = asyncio.create_task(consume())
        await got_first.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed.is_set()
        assert received == [TextDelta("x")]  # nothing partial is promoted to a finished reply

    run(main())


@pytest.mark.parametrize("mode", TRANSPORTS)
def test_rate_limit_surfaces_retry_after(mode: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "7"},
            json={"error": {"type": "rate_limit_error", "message": "slow down"}},
        )

    async def main() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await _collect(client, mode, make_route(mode), ChatRequest(model="m", messages=[]))

    with pytest.raises(ProviderHTTPError) as caught:
        run(main())
    assert (caught.value.status, caught.value.retry_after) == (429, 7.0)
