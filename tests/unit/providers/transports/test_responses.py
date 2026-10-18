"""Responses transport (P08-09): conversion, stream parsing, status mapping, replay policy."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from indus.foundation.errors import ProviderHTTPError, ProviderStreamError
from indus.providers.classify import classify
from indus.providers.transports.responses import (
    MAX_CALL_ID,
    REPLAY_TYPE,
    ResponseCancelled,
    ResponsesTransport,
    convert_input,
    same_model,
    wire_call_id,
)
from indus.providers.types import (
    ChatRequest,
    Finish,
    KeepAlive,
    ReasoningDetail,
    TextDelta,
    assemble,
)
from tests.fakes.model.replay import capture_chunks, chat_request_from, load_capture
from tests.unit.providers._support import body_of, make_route, parse

MODEL = "gpt-5"
SEALED = "gAAAAABo" + "Zm9vYmFy" * 40 + "=="  # opaque bytes the server sealed


def sse(kind: str, **payload: Any) -> bytes:
    data = json.dumps({"type": kind, **payload})
    return f"event: {kind}\ndata: {data}\n\n".encode()


def route(**kw: Any):  # noqa: ANN201
    return make_route("responses", base_url="https://api.openai.invalid/v1", model=MODEL, **kw)


def build(req: ChatRequest, transport: ResponsesTransport | None = None) -> dict[str, Any]:
    return body_of((transport or ResponsesTransport()).build_request(route(), req))


def reasoning_item(index: int = 0) -> dict[str, Any]:
    return {
        "type": "reasoning",
        "id": f"rs_{index}",
        "summary": [{"type": "summary_text", "text": "thinking"}],
        "encrypted_content": SEALED,
    }


def completed(**fields: Any) -> dict[str, Any]:
    return {"id": "resp_1", "model": "gpt-5-2025-08-07", "status": "completed", **fields}


# -- request ---------------------------------------------------------------------------------------


def test_conversion_golden() -> None:
    stored = {"type": REPLAY_TYPE, "model": "gpt-5-2025-08-07", "item": reasoning_item()}
    messages = [
        {"role": "system", "content": "be brief"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
        {
            "role": "assistant",
            "content": "Let me look.",
            "reasoning_details": [stored, {"type": "thinking", "thinking": "other wire"}],
            "tool_calls": [
                {"id": "call_a", "function": {"name": "read", "arguments": '{"p":"x"}'}},
                {"id": "call_b", "function": {"name": "list", "arguments": ""}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "text of x"},
        {"role": "tool", "tool_call_id": "call_b", "content": [{"type": "text", "text": "a b"}]},
    ]
    body = build(
        ChatRequest(
            model=MODEL,
            messages=messages,
            max_tokens=500,
            tools=[
                {
                    "type": "function",
                    "function": {"name": "read", "description": "Read.", "parameters": {}},
                }
            ],
            tool_choice="auto",
        )
    )
    assert body == {
        "model": MODEL,
        "instructions": "be brief",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "what is this"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                ],
            },
            reasoning_item(),
            {"type": "message", "role": "assistant", "content": "Let me look."},
            {
                "type": "function_call",
                "call_id": "call_a",
                "name": "read",
                "arguments": '{"p":"x"}',
            },
            {"type": "function_call", "call_id": "call_b", "name": "list", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_a", "output": "text of x"},
            {"type": "function_call_output", "call_id": "call_b", "output": "a b"},
        ],
        "tools": [
            {
                "type": "function",
                "name": "read",
                "description": "Read.",
                "parameters": {},
                "strict": False,
            }
        ],
        "tool_choice": "auto",
        "store": False,
        "stream": True,
        "max_output_tokens": 500,
    }


def test_url_auth_and_no_reasoning_when_off() -> None:
    request = ResponsesTransport().build_request(
        route(), ChatRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}])
    )
    assert str(request.url) == "https://api.openai.invalid/v1/responses"
    assert request.headers["authorization"].startswith("Bearer ")
    body = body_of(request)
    assert "reasoning" not in body and "include" not in body


@pytest.mark.parametrize(
    ("asked", "sent"),
    [("low", "low"), ("xhigh", "high"), ("ultra", "high"), ("minimal", "minimal")],
)
def test_effort_clamped_and_encrypted_reasoning_requested(asked: str, sent: str) -> None:
    body = build(ChatRequest(model=MODEL, messages=[], reasoning_effort=asked))
    assert body["reasoning"] == {"effort": sent, "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]


def test_long_call_ids_clamped_on_both_sides() -> None:
    long_id = "mcp__server__tool__" + "x" * 80
    assert len(wire_call_id(long_id)) <= MAX_CALL_ID
    assert wire_call_id(long_id) == wire_call_id(long_id)
    assert wire_call_id("short") == "short"
    _, items = convert_input(
        [
            {"role": "assistant", "tool_calls": [{"id": long_id, "function": {"name": "t"}}]},
            {"role": "tool", "tool_call_id": long_id, "content": "ok"},
        ],
        model=MODEL,
    )
    assert items[0]["call_id"] == items[1]["call_id"] == wire_call_id(long_id)


def test_reasoning_only_for_the_model_that_sealed_it() -> None:
    assert same_model("gpt-5-2025-08-07", "gpt-5")
    assert same_model("gpt-5", "gpt-5")
    assert not same_model("gpt-5-mini-2025-08-07", "gpt-5")
    stored = {"type": REPLAY_TYPE, "model": "o4-mini", "item": reasoning_item()}
    _, items = convert_input(
        [{"role": "assistant", "content": "x", "reasoning_details": [stored]}], model=MODEL
    )
    assert [i["type"] for i in items] == ["message"]


# -- stream ----------------------------------------------------------------------------------------


def stream_with_reasoning_and_text() -> list[bytes]:
    return [
        sse("response.created", response={"model": "gpt-5-2025-08-07", "status": "in_progress"}),
        sse("response.output_item.added", output_index=0, item={"type": "reasoning", "id": "rs_0"}),
        sse(
            "response.reasoning_summary_text.delta",
            item_id="rs_0",
            output_index=0,
            summary_index=0,
            delta="thinking",
        ),
        sse("response.output_item.done", output_index=0, item=reasoning_item()),
        sse(
            "response.output_item.added",
            output_index=1,
            item={"type": "message", "id": "msg_1", "role": "assistant", "content": []},
        ),
        sse("response.output_text.delta", item_id="msg_1", output_index=1, delta="Hel"),
        sse("response.output_text.delta", item_id="msg_1", output_index=1, delta="lo"),
        sse(
            "response.completed",
            response=completed(
                usage={
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 40},
                    "output_tokens": 20,
                    "output_tokens_details": {"reasoning_tokens": 12},
                }
            ),
        ),
    ]


def test_stream_golden() -> None:
    events = parse(ResponsesTransport(), stream_with_reasoning_and_text())
    response = assemble(events)
    assert response.content == "Hello"
    assert response.reasoning == "thinking"
    assert response.finish_reason == "stop"
    assert response.usage is not None
    assert (response.usage.prompt_tokens, response.usage.cache_read_tokens) == (100, 40)
    assert response.usage.reasoning_tokens == 12
    assert any(isinstance(e, KeepAlive) for e in events)
    assert response.reasoning_details == (
        {"type": REPLAY_TYPE, "item": reasoning_item(), "model": "gpt-5-2025-08-07"},
    )


def test_reasoning_replayed_byte_identical() -> None:
    response = assemble(parse(ResponsesTransport(), stream_with_reasoning_and_text()))
    assistant = {
        "role": "assistant",
        "content": response.content,
        "reasoning_details": list(response.reasoning_details),
    }
    body = build(ChatRequest(model=MODEL, messages=[assistant, {"role": "user", "content": "go"}]))
    replayed = body["input"][0]
    assert replayed == reasoning_item()
    assert replayed["encrypted_content"] == SEALED  # the sealed bytes, unchanged


def test_tool_call_stream_and_done_without_deltas() -> None:
    call = {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "read"}
    chunks = [
        sse("response.output_item.added", output_index=0, item={**call, "arguments": ""}),
        sse("response.function_call_arguments.delta", item_id="fc_1", output_index=0, delta='{"a"'),
        sse("response.function_call_arguments.delta", item_id="fc_1", output_index=0, delta=":1}"),
        sse("response.output_item.done", output_index=0, item={**call, "arguments": '{"a":1}'}),
        # a second call announced only by its done event
        sse(
            "response.output_item.done",
            output_index=1,
            item={"type": "function_call", "call_id": "call_10", "name": "ls", "arguments": "{}"},
        ),
        sse("response.completed", response=completed()),
    ]
    response = assemble(parse(ResponsesTransport(), chunks))
    assert [(c.id, c.name, c.arguments) for c in response.tool_calls] == [
        ("call_9", "read", '{"a":1}'),
        ("call_10", "ls", "{}"),
    ]
    assert response.finish_reason == "tool_calls"


def test_refusal_counts_as_text() -> None:
    chunks = [
        sse("response.refusal.delta", output_index=0, delta="I can't help with that."),
        sse("response.completed", response=completed()),
    ]
    assert assemble(parse(ResponsesTransport(), chunks)).content == "I can't help with that."


@pytest.mark.parametrize(
    ("details", "finish"),
    [({"reason": "max_output_tokens"}, "length"), ({"reason": "content_filter"}, "content_filter")],
)
def test_status_mapping(details: dict[str, str], finish: str) -> None:
    chunks = [
        sse("response.output_text.delta", output_index=0, delta="part"),
        sse(
            "response.incomplete",
            response=completed(status="incomplete", incomplete_details=details),
        ),
    ]
    assert parse(ResponsesTransport(), chunks)[-1] == Finish(finish)  # type: ignore[arg-type]
    payload = completed(
        status="incomplete",
        incomplete_details=details,
        output=[{"type": "message", "content": [{"type": "output_text", "text": "part"}]}],
    )
    assert ResponsesTransport().parse_response(payload).finish_reason == finish


def test_failed_event_carries_embedded_code() -> None:
    error = {"code": "rate_limit_exceeded", "message": "Rate limit reached for gpt-5"}
    chunks = [sse("response.failed", response=completed(status="failed", error=error))]
    with pytest.raises(ProviderStreamError) as caught:
        parse(ResponsesTransport(), chunks)
    assert caught.value.body["error"]["code"] == "rate_limit_exceeded"  # type: ignore[index]
    assert classify(caught.value).reason == "rate_limit"
    with pytest.raises(ProviderStreamError):
        ResponsesTransport().parse_response(completed(status="failed", error=error, output=[]))


def test_error_event_and_cancelled() -> None:
    error = sse("error", code="server_error", message="The server had an error")
    with pytest.raises(ProviderStreamError) as caught:
        parse(ResponsesTransport(), [error])
    assert classify(caught.value).reason == "server_error"
    with pytest.raises(ResponseCancelled) as cancelled:
        parse(
            ResponsesTransport(),
            [sse("response.cancelled", response=completed(status="cancelled"))],
        )
    assert cancelled.value.interrupted
    with pytest.raises(ResponseCancelled):
        ResponsesTransport().parse_response(completed(status="cancelled", output=[]))


def test_stream_without_terminal_event_is_incomplete() -> None:
    with pytest.raises(ProviderStreamError) as caught:
        parse(ResponsesTransport(), [sse("response.output_text.delta", output_index=0, delta="x")])
    assert classify(caught.value).reason == "incomplete_response"


def _reasoning_only() -> list[bytes]:
    return [
        sse("response.created", response={"model": "gpt-5-2025-08-07"}),
        sse("response.output_item.done", output_index=0, item=reasoning_item()),
        sse(
            "response.incomplete",
            response=completed(
                status="incomplete", incomplete_details={"reason": "max_output_tokens"}
            ),
        ),
    ]


def test_reasoning_only_three_times_in_a_row_fails() -> None:
    transport = ResponsesTransport()
    assert parse(transport, _reasoning_only())[-1] == Finish("length")
    assert parse(transport, _reasoning_only())[-1] == Finish("length")
    with pytest.raises(ProviderStreamError) as caught:
        parse(transport, _reasoning_only())
    assert classify(caught.value).reason == "incomplete_response"
    # an answer in between resets the streak
    parse(transport, _reasoning_only())
    parse(transport, stream_with_reasoning_and_text())
    parse(transport, _reasoning_only())
    assert parse(transport, _reasoning_only())[-1] == Finish("length")


# -- compatibility retries -------------------------------------------------------------------------


def _rejection(message: str, status: int = 400) -> ProviderHTTPError:
    return ProviderHTTPError(f"HTTP {status}", status=status, body={"error": {"message": message}})


def test_rejected_encrypted_reasoning_is_dropped_for_that_route() -> None:
    transport = ResponsesTransport()
    stored = {"type": REPLAY_TYPE, "item": reasoning_item()}
    req = ChatRequest(
        model=MODEL, messages=[{"role": "assistant", "content": "x", "reasoning_details": [stored]}]
    )
    assert body_of(transport.build_request(route(), req))["input"][0]["type"] == "reasoning"
    error = _rejection("The encrypted content for item rs_0 could not be verified.")
    assert transport.on_request_rejected(route(), error) is True
    assert transport.on_request_rejected(route(), error) is False  # only once
    assert [i["type"] for i in body_of(transport.build_request(route(), req))["input"]] == [
        "message"
    ]


def test_rejected_item_ids_are_dropped_but_items_kept() -> None:
    transport = ResponsesTransport()
    stored = {"type": REPLAY_TYPE, "item": reasoning_item()}
    req = ChatRequest(
        model=MODEL, messages=[{"role": "assistant", "content": "x", "reasoning_details": [stored]}]
    )
    error = _rejection(
        "Item with id 'rs_0' not found. Items are not persisted when store is false.", 404
    )
    assert transport.on_request_rejected(route(), error) is True
    item = body_of(transport.build_request(route(), req))["input"][0]
    assert "id" not in item and item["encrypted_content"] == SEALED
    assert transport.on_request_rejected(route(), _rejection("something else")) is False


# -- captures --------------------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["text", "tool_call"])
def test_capture_replays_and_round_trips(scenario: str) -> None:
    capture = load_capture(f"responses/{scenario}")
    transport = ResponsesTransport()
    req = chat_request_from(capture)
    sent = transport.build_request(route(), req)
    assert body_of(sent) == capture["request"]["body"]
    response = assemble(parse(transport, capture_chunks(capture)))
    if scenario == "tool_call":
        call = response.tool_calls[0]
        follow_up = [
            *req.messages,
            {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {"id": call.id, "function": {"name": call.name, "arguments": call.arguments}}
                ],
            },
            {"role": "tool", "tool_call_id": call.id, "content": "sunny"},
        ]
        items = body_of(
            transport.build_request(route(), ChatRequest(model=MODEL, messages=follow_up))
        )
        assert items["input"][-2:] == [
            {
                "type": "function_call",
                "call_id": call.id,
                "name": "get_weather",
                "arguments": '{"city":"Oslo"}',
            },
            {"type": "function_call_output", "call_id": call.id, "output": "sunny"},
        ]
    else:
        assert response.content == "hello"


def test_capture_chunks_are_valid_base64() -> None:
    for scenario in ("text", "tool_call"):
        capture = load_capture(f"responses/{scenario}")
        assert all(base64.b64decode(c) for c in capture["response"]["chunks"])


def test_non_stream_json_round_trip() -> None:
    payload = completed(
        output=[
            reasoning_item(),
            {"type": "message", "content": [{"type": "output_text", "text": "done"}]},
        ],
        usage={"input_tokens": 5, "output_tokens": 2},
    )
    response = ResponsesTransport().parse_response(payload)
    assert response.content == "done" and response.finish_reason == "stop"
    assert response.reasoning_details[0]["item"]["encrypted_content"] == SEALED
    assert isinstance(ReasoningDetail(response.reasoning_details[0]), ReasoningDetail)
    assert response.provider_data["id"] == "resp_1"


def test_keepalive_comments_pass_through() -> None:
    events = parse(
        ResponsesTransport(), [b": ping\n\n", sse("response.completed", response=completed())]
    )
    assert isinstance(events[0], KeepAlive)
    assert not any(isinstance(e, TextDelta) for e in events)


def test_request_is_plain_httpx() -> None:
    request = ResponsesTransport().build_request(route(), ChatRequest(model=MODEL, messages=[]))
    assert isinstance(request, httpx.Request) and request.method == "POST"
