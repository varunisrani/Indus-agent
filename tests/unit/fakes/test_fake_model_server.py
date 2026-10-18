"""The scriptable fake model server (P03-18): both wire formats, scripts, request log."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from indus.foundation.errors import ProviderStreamError
from indus.providers.classify import classify
from indus.providers.sse import aiter_sse
from indus.providers.transports import get_transport, http_error_from_response
from indus.providers.types import ChatRequest, KeepAlive, NormalizedResponse, assemble
from tests.fakes.clock import FakeClock
from tests.fakes.model import (
    FakeModelServer,
    FakeScriptExhausted,
    MidStreamError,
    Script,
    ScriptError,
    ToolCall,
    fake_route,
    load_script,
    parse_script,
)
from tests.fakes.model import (
    KeepAlive as KeepAliveStep,
)
from tests.fakes.model.server import FAKE_HOST, one_off_router

MODES = ["chat_completions", "anthropic_messages"]
START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def script(*turns: list[dict[str, Any]]) -> Script:
    return parse_script({"turns": [{"steps": steps} for steps in turns]})


def request_for(server: FakeModelServer, mode: str, *, stream: bool = True) -> httpx.Request:
    req = ChatRequest(
        model=server.model, messages=[{"role": "user", "content": "go"}], stream=stream
    )
    return get_transport(mode).build_request(fake_route(server, mode), req)


async def stream_events(client: httpx.AsyncClient, server: FakeModelServer, mode: str) -> list[Any]:
    transport = get_transport(mode)
    response = await client.send(request_for(server, mode), stream=True)
    try:
        assert response.status_code == 200
        return [e async for e in transport.parse_stream(aiter_sse(response))]
    finally:
        await response.aclose()


def shape(response: NormalizedResponse) -> tuple[Any, ...]:
    calls = [(c.name, json.loads(c.arguments)) for c in response.tool_calls]
    return (response.content, calls, response.finish_reason, response.reasoning)


@pytest.mark.parametrize("mode", MODES)
def test_three_request_tool_conversation_parses_with_the_transports(mode: str) -> None:
    server = FakeModelServer(load_script("tool_then_text"))

    async def go() -> list[NormalizedResponse]:
        async with server.client() as client:
            return [assemble(await stream_events(client, server, mode)) for _ in range(3)]

    first, second, third = asyncio.run(go())
    assert first.content == "Let me look at the notes."
    assert first.reasoning == "Need the file first."
    assert [c.name for c in first.tool_calls] == ["read_file"]
    assert json.loads(first.tool_calls[0].arguments) == {"path": "notes.txt", "limit": 20}
    assert first.tool_calls[0].id == "call_notes_1"
    assert first.finish_reason == "tool_calls"
    assert first.usage is not None
    assert [c.name for c in second.tool_calls] == ["search", "read_file"]
    assert [c.id for c in second.tool_calls] == ["call_search_1", "call_plan_1"]  # one id late
    assert third.content == "The notes say ship on Friday." and third.finish_reason == "stop"
    assert server.turns_left == 0


def test_same_script_same_shape_on_both_wires() -> None:
    shapes: dict[str, list[tuple[Any, ...]]] = {}
    for mode in MODES:
        server = FakeModelServer(load_script("tool_then_text"))

        async def go(server: FakeModelServer = server, mode: str = mode) -> list[Any]:
            async with server.client() as client:
                return [
                    shape(assemble(await stream_events(client, server, mode))) for _ in range(3)
                ]

        shapes[mode] = asyncio.run(go())
    assert shapes["chat_completions"] == shapes["anthropic_messages"]


@pytest.mark.parametrize("mode", MODES)
def test_non_stream_json_answers(mode: str) -> None:
    server = FakeModelServer(load_script("tool_then_text"))

    async def go() -> NormalizedResponse:
        async with server.client() as client:
            response = await client.send(request_for(server, mode, stream=False))
            assert response.headers["content-type"] == "application/json"
            return get_transport(mode).parse_response(response.json())

    result = asyncio.run(go())
    assert result.content == "Let me look at the notes."
    assert [c.name for c in result.tool_calls] == ["read_file"]
    assert result.finish_reason == "tool_calls"
    assert server.model_requests[0].body["stream"] is False


def test_rate_limited_script_reaches_the_classifier() -> None:
    server = FakeModelServer(load_script("rate_limited"))
    clock = FakeClock(START)

    async def go() -> httpx.Response:
        async with server.client() as client:
            response = await client.send(request_for(server, "chat_completions"))
            await response.aread()
            return response

    response = asyncio.run(go())
    assert response.status_code == 429
    error = http_error_from_response(
        response.status_code, response.headers, response.content, now=clock.now()
    )
    verdict = classify(error)
    assert verdict.reason == "rate_limit" and verdict.retry_after == 2.0


def test_extra_request_names_the_script() -> None:
    server = FakeModelServer(load_script("hello"))

    async def go() -> None:
        async with server.client() as client:
            await stream_events(client, server, "chat_completions")
            with pytest.raises(FakeScriptExhausted, match="'hello' has 1 turn"):
                await client.send(request_for(server, "chat_completions"))

    asyncio.run(go())
    assert len(server.model_requests) == 2 and server.model_requests[1].turn is None


def test_request_log_keeps_parsed_bodies_and_headers() -> None:
    server = FakeModelServer(load_script("hello"))

    async def go() -> None:
        async with server.client() as client:
            await stream_events(client, server, "anthropic_messages")

    asyncio.run(go())
    (logged,) = server.requests
    assert logged.method == "POST" and logged.path == "/v1/messages" and logged.turn == 1
    assert logged.body["messages"] == [{"role": "user", "content": "go"}]
    assert logged.stream is True
    assert "anthropic-version" in logged.headers


def test_models_listing_and_unknown_path() -> None:
    server = FakeModelServer(
        load_script("hello"), models=[{"id": "small", "context_length": 32768}]
    )

    async def go() -> tuple[httpx.Response, httpx.Response]:
        async with server.client() as client:
            listing = await client.get(f"https://{FAKE_HOST}/v1/models")
            missing = await client.post(f"https://{FAKE_HOST}/v1/embeddings", json={})
            return listing, missing

    listing, missing = asyncio.run(go())
    assert listing.json()["data"] == [{"id": "small", "context_length": 32768}]
    assert missing.status_code == 404
    assert server.turns_left == 1  # neither request used a turn


@pytest.mark.parametrize("mode", MODES)
def test_keep_alive_frames_carry_no_content(mode: str) -> None:
    server = FakeModelServer(script([{"keep_alive": 2}, {"text": "x"}, {"finish": "stop"}]))

    async def go() -> list[Any]:
        async with server.client() as client:
            return await stream_events(client, server, mode)

    events = asyncio.run(go())
    assert sum(isinstance(e, KeepAlive) for e in events) == 2
    assert assemble(events).content == "x"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "body", [{"error": {"message": "Overloaded", "type": "overloaded_error"}}, "upstream exploded"]
)
def test_mid_stream_error_frames(mode: str, body: Any) -> None:
    server = FakeModelServer(script([{"text": "partial"}, {"mid_stream_error": body}]))

    async def go() -> None:
        async with server.client() as client:
            with pytest.raises(ProviderStreamError):
                await stream_events(client, server, mode)

    asyncio.run(go())
    assert server.streams[0].closed_at is not None


@pytest.mark.parametrize("mode", MODES)
def test_disconnect_cuts_the_body(mode: str) -> None:
    server = FakeModelServer(script([{"text": "cut"}, {"disconnect": None}]))

    async def go() -> None:
        async with server.client() as client:
            with pytest.raises(httpx.RemoteProtocolError):
                await stream_events(client, server, mode)

    asyncio.run(go())
    assert server.streams[0].completed is False


@pytest.mark.parametrize("mode", MODES)
def test_missing_finish_is_an_incomplete_stream(mode: str) -> None:
    server = FakeModelServer(script([{"text": "no end"}, {"eof": None}]))

    async def go() -> None:
        async with server.client() as client:
            with pytest.raises(ProviderStreamError) as info:
                await stream_events(client, server, mode)
            assert classify(info.value).reason == "incomplete_response"

    asyncio.run(go())


def test_stall_waits_on_the_clock() -> None:
    clock = FakeClock(START)
    server = FakeModelServer(
        script([{"text": "a"}, {"stall": 200}, {"text": "b"}, {"finish": "stop"}]), clock=clock
    )

    async def go() -> list[Any]:
        async with server.client() as client:
            task = asyncio.ensure_future(stream_events(client, server, "chat_completions"))
            await clock.run_until_idle()
            assert not task.done() and clock.pending == 1
            clock.advance(199)
            await clock.run_until_idle()
            assert not task.done()
            clock.advance(1)
            await clock.run_until_idle()
            return await task

    assert assemble(asyncio.run(go())).content == "ab"


def test_late_id_goes_with_the_second_fragment() -> None:
    server = FakeModelServer(
        script(
            [
                {"tool_call": {"name": "f", "arguments": {"a": 1}, "id": "c9", "late_id": True}},
                {"finish": "tool_calls"},
            ]
        )
    )

    async def go() -> list[dict[str, Any]]:
        async with server.client() as client:
            response = await client.send(request_for(server, "chat_completions"))
            frames = [line[6:] for line in response.text.splitlines() if line.startswith("data: {")]
            return [json.loads(f)["choices"][0]["delta"].get("tool_calls", [{}])[0] for f in frames]

    calls = asyncio.run(go())
    assert "id" not in calls[0] and calls[0]["function"]["name"] == "f"
    assert calls[1]["id"] == "c9"


def test_script_errors_at_build_time() -> None:
    with pytest.raises(ScriptError, match="first step"):
        FakeModelServer(script([{"text": "x"}, {"fail": {"status": 500}}]))
    with pytest.raises(ScriptError, match="clock"):
        FakeModelServer(script([{"stall": 5}]))


def test_new_step_kinds_load_from_yaml(tmp_path: Any) -> None:
    path = tmp_path / "kinds.yaml"
    path.write_text(
        "turns:\n"
        "  - steps:\n"
        "      - keep_alive: 3\n"
        "      - keep_alive:\n"
        "      - tool_call: {name: t, late_id: true}\n"
        "      - mid_stream_error: {error: {message: boom}}\n"
        "  - steps:\n"
        "      - mid_stream_error: plain text\n",
        encoding="utf-8",
    )
    loaded = load_script(path)
    assert loaded.name == "kinds"
    first, second = loaded.turns
    assert first.steps[0] == KeepAliveStep(3) and first.steps[1] == KeepAliveStep(1)
    assert first.steps[2] == ToolCall("t", late_id=True)
    assert first.steps[3] == MidStreamError({"error": {"message": "boom"}})
    assert second.steps[0] == MidStreamError("plain text")
    with pytest.raises(ScriptError, match="keep_alive"):
        parse_script({"turns": [{"steps": [{"keep_alive": 0}]}]})


def test_fake_route_points_at_the_fake_host() -> None:
    server = FakeModelServer(load_script("hello"))
    chat = fake_route(server, "chat_completions")
    anthropic = fake_route(server, "anthropic_messages")
    assert chat.base_url == f"https://{FAKE_HOST}/v1" and chat.api_mode == "chat_completions"
    assert anthropic.base_url == f"https://{FAKE_HOST}"
    assert chat.model == server.model


def test_one_off_router() -> None:
    router = one_off_router()
    router.get("/models").respond(200, json={"object": "list", "data": [{"id": "m"}]})

    async def go() -> Any:
        transport = httpx.MockTransport(router.async_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return (await client.get(f"https://{FAKE_HOST}/v1/models")).json()

    assert asyncio.run(go())["data"] == [{"id": "m"}]
