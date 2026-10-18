"""Chat Completions transport (P03-09): request goldens, stream fixtures, compatibility retry."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from indus.foundation.errors import ProviderHTTPError, ProviderStreamError
from indus.providers.profile import ProviderProfile
from indus.providers.transports import open_call, stream_events
from indus.providers.transports.chat_completions import ChatCompletionsTransport
from indus.providers.types import (
    ChatRequest,
    Finish,
    KeepAlive,
    ReasoningDelta,
    ReasoningDetail,
    TextDelta,
    ToolCall,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
    UsageReport,
    assemble,
)
from tests.unit.providers._support import (
    FAKE_KEY,
    body_of,
    make_profile,
    make_route,
    parse,
    run,
    sse,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Weather for a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]
HELLO = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi"}]


def build(
    req: ChatRequest, profile: ProviderProfile | None = None, **route_kw: Any
) -> httpx.Request:
    return ChatCompletionsTransport().build_request(make_route(profile=profile, **route_kw), req)


# -- request goldens ---------------------------------------------------------------------------


def test_text_only_request_golden() -> None:
    request = build(ChatRequest(model="m-1", messages=HELLO))
    assert request.method == "POST"
    assert str(request.url) == "https://api.fake.invalid/v1/chat/completions"
    assert body_of(request) == {
        "model": "m-1",
        "messages": HELLO,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "text/event-stream"


def test_tools_request_golden() -> None:
    req = ChatRequest(
        model="m-1",
        messages=HELLO,
        tools=TOOLS,
        tool_choice="auto",
        max_tokens=256,
        temperature=0.2,
    )
    assert body_of(build(req)) == {
        "model": "m-1",
        "messages": HELLO,
        "tools": TOOLS,
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0.2,
        "max_tokens": 256,
    }


def test_reasoning_effort_clamped_to_profile() -> None:
    profile = make_profile(reasoning_efforts=("low", "medium", "high"))
    body = body_of(build(ChatRequest(model="m", messages=HELLO, reasoning_effort="xhigh"), profile))
    assert body["reasoning_effort"] == "high"
    off = body_of(build(ChatRequest(model="m", messages=HELLO, reasoning_effort="none"), profile))
    assert "reasoning_effort" not in off


def test_max_completion_tokens_profile_and_default() -> None:
    profile = make_profile(max_tokens_param="max_completion_tokens", default_max_tokens=4096)
    body = body_of(build(ChatRequest(model="m", messages=HELLO), profile))
    assert body["max_completion_tokens"] == 4096 and "max_tokens" not in body
    body = body_of(build(ChatRequest(model="m", messages=HELLO, max_tokens=100), profile))
    assert body["max_completion_tokens"] == 100


def test_fixed_temperature_extra_body_and_prepare_hook() -> None:
    def prepare(body: dict[str, Any], model: str) -> dict[str, Any]:
        return {**body, "prepared_for": model}

    profile = make_profile(fixed_temperature=0.6, prepare_body=prepare)
    req = ChatRequest(model="m", messages=HELLO, temperature=1.5, extra_body={"top_k": 20})
    body = body_of(build(req, profile))
    assert body["temperature"] == 0.6
    assert body["top_k"] == 20 and body["prepared_for"] == "m"


def test_keyless_local_server_and_non_stream() -> None:
    req = ChatRequest(model="m", messages=HELLO, stream=False)
    request = build(req, key=None, base_url="http://127.0.0.1:1234/v1/")
    assert "authorization" not in request.headers
    assert str(request.url) == "http://127.0.0.1:1234/v1/chat/completions"
    body = body_of(request)
    assert body["stream"] is False and "stream_options" not in body


def test_reasoning_fields_stripped_from_history() -> None:
    history = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "a",
            "reasoning": "hidden",
            "reasoning_content": "hidden",
            "reasoning_details": [{"type": "x"}],
        },
    ]
    sent = body_of(build(ChatRequest(model="m", messages=history)))["messages"]
    assert sent == [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    assert "reasoning" in history[1]  # the caller's list is not touched


def test_no_identity_headers_besides_ours() -> None:
    request = build(ChatRequest(model="m", messages=HELLO))
    names = {k.lower() for k in request.headers}
    assert not any(n.startswith("x-") for n in names)
    assert "user-agent" not in names or request.headers["user-agent"].startswith("indus/")


# -- stream fixtures ---------------------------------------------------------------------------


def chunk(
    delta: dict[str, Any] | None = None, finish: str | None = None, **extra: Any
) -> dict[str, Any]:
    choices = [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]
    return {"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": choices, **extra}


def tc(
    index: int | None, *, id: Any = None, name: str | None = None, args: str | None = None
) -> dict[str, Any]:
    call: dict[str, Any] = {"function": {}}
    if index is not None:
        call["index"] = index
    if id is not None:
        call["id"] = id
        call["type"] = "function"
    if name is not None:
        call["function"]["name"] = name
    if args is not None:
        call["function"]["arguments"] = args
    return call


T = ChatCompletionsTransport()


def test_interleaved_text_and_two_parallel_tool_calls() -> None:
    body = sse(
        chunk({"role": "assistant", "content": "Let me "}),
        chunk({"content": "check."}),
        chunk({"tool_calls": [tc(0, id="call_a", name="get_weather", args="")]}),
        chunk({"tool_calls": [tc(1, id="call_b", name="get_time", args='{"tz"')]}),
        chunk({"tool_calls": [tc(0, args='{"city": ')]}),
        chunk({"tool_calls": [tc(1, args=': "UTC"}')]}),
        chunk({"tool_calls": [tc(0, args='"Oslo"}')]}),
        chunk({}, finish="tool_calls"),
        chunk(None, usage={"prompt_tokens": 12, "completion_tokens": 30, "total_tokens": 42}),
        "[DONE]",
    )
    events = parse(T, [body[:50], body[50:51], body[51:]])
    assert events[:2] == [TextDelta("Let me "), TextDelta("check.")]
    assert events[2] == ToolCallStart(0, "call_a", "get_weather")
    assert events[-1] == Finish("tool_calls")
    response = assemble(events)
    assert response.content == "Let me check."
    assert response.tool_calls == (
        ToolCall("call_a", "get_weather", '{"city": "Oslo"}'),
        ToolCall("call_b", "get_time", '{"tz": "UTC"}'),
    )
    assert response.usage == Usage(prompt_tokens=12, completion_tokens=30)
    assert response.finish_reason == "tool_calls"


def test_tool_call_id_in_second_fragment() -> None:
    body = sse(
        chunk({"tool_calls": [tc(0, name="search", args='{"q":')]}),
        chunk({"tool_calls": [tc(0, id="call_late", args=' "x"}')]}),
        chunk({}, finish="tool_calls"),
        "[DONE]",
    )
    events = parse(T, [body])
    assert events[:3] == [
        ToolCallStart(0, None, "search"),
        ToolCallArgsDelta(0, '{"q":'),
        ToolCallStart(0, "call_late", "search"),
    ]
    assert assemble(events).tool_calls == (ToolCall("call_late", "search", '{"q": "x"}'),)


def test_arguments_before_name_and_resent_names_and_missing_index() -> None:
    body = sse(
        chunk({"tool_calls": [tc(None, id=7, args='{"a"')]}),
        chunk({"tool_calls": [tc(None, name="act", args=": 1")]}),
        chunk({"tool_calls": [tc(None, name="act", args="}")]}),
        chunk({}, finish="tool_calls"),
        "[DONE]",
    )
    events = parse(T, [body])
    assert events[0] == ToolCallStart(0, "7", "act")
    assert assemble(events).tool_calls == (ToolCall("7", "act", '{"a": 1}'),)


def test_reused_index_with_fresh_id_opens_a_new_call() -> None:
    body = sse(
        chunk({"tool_calls": [tc(0, id="c1", name="read", args='{"p": "a"}')]}),
        chunk({"tool_calls": [tc(0, id="c2", name="read", args='{"p": "b"}')]}),
        chunk({}, finish="tool_calls"),
        "[DONE]",
    )
    calls = assemble(parse(T, [body])).tool_calls
    assert [(c.id, c.arguments) for c in calls] == [("c1", '{"p": "a"}'), ("c2", '{"p": "b"}')]


def test_usage_only_last_chunk_with_details() -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 10},
        "completion_tokens_details": {"reasoning_tokens": 5},
    }
    body = sse(chunk({"content": "ok"}, finish="stop"), chunk(None, usage=usage), "[DONE]")
    events = parse(T, [body])
    assert events == [
        TextDelta("ok"),
        UsageReport(
            Usage(100, 20, cache_read_tokens=60, cache_write_tokens=10, reasoning_tokens=5)
        ),
        Finish("stop"),
    ]


def test_reasoning_variants_and_details() -> None:
    details = [{"type": "reasoning.text", "text": "t", "signature": "sig"}]
    body = sse(
        chunk({"reasoning_content": "step 1. "}),
        chunk({"reasoning": "step 2.", "reasoning_details": details}),
        chunk({"content": "done"}, finish="stop"),
        "[DONE]",
    )
    events = parse(T, [body])
    assert events[:3] == [
        ReasoningDelta("step 1. "),
        ReasoningDelta("step 2."),
        ReasoningDetail(details[0]),
    ]
    response = assemble(events)
    assert response.reasoning == "step 1. step 2."
    assert response.reasoning_details == (details[0],)


def test_keepalives_and_empty_frames() -> None:
    body = b": OPENROUTER PROCESSING\n\ndata:\n\n" + sse(
        chunk({"content": "x"}, finish="stop"), "[DONE]"
    )
    events = parse(T, [body])
    assert events == [
        KeepAlive("OPENROUTER PROCESSING"),
        KeepAlive(),
        TextDelta("x"),
        Finish("stop"),
    ]


def test_error_object_mid_stream() -> None:
    error = {"error": {"message": "upstream overloaded", "code": 503, "type": "server_error"}}
    body = sse(chunk({"content": "par"}), error)
    with pytest.raises(ProviderStreamError) as caught:
        parse(T, [body])
    err = caught.value
    assert err.status == 503 and err.code == "provider.stream"
    assert err.body["error"]["message"] == "upstream overloaded"  # type: ignore[index]
    assert err.body["error"]["type"] == "server_error"  # type: ignore[index]


def test_error_event_type() -> None:
    body = sse({"message": "quota exceeded", "code": "insufficient_quota"}, event="error")
    with pytest.raises(ProviderStreamError) as caught:
        parse(T, [body])
    assert caught.value.body["error"]["code"] == "insufficient_quota"  # type: ignore[index]


def test_plain_text_error_line() -> None:
    body = sse(chunk({"content": "a"}), "Internal error: model crashed")
    with pytest.raises(ProviderStreamError) as caught:
        parse(T, [body])
    err = caught.value
    assert err.body["error"]["code"] == "non_json_data"  # type: ignore[index]
    assert "model crashed" in err.body["error"]["message"]  # type: ignore[index]


def test_finish_reason_error() -> None:
    body = sse(chunk({"content": "Rate limit hit upstream"}, finish="error"), "[DONE]")
    with pytest.raises(ProviderStreamError) as caught:
        parse(T, [body])
    assert "Rate limit" in str(caught.value)


def test_missing_done_after_finish_ends_cleanly() -> None:
    events = parse(T, [sse(chunk({"content": "hi"}, finish="stop"))])
    assert events == [TextDelta("hi"), Finish("stop")]


def test_missing_done_without_finish_is_incomplete() -> None:
    with pytest.raises(ProviderStreamError) as caught:
        parse(T, [sse(chunk({"content": "hi"}))])
    assert caught.value.body["error"]["code"] == "incomplete_stream"  # type: ignore[index]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("STOP", "stop"),
        ("length", "length"),
        ("MAX_TOKENS", "length"),
        ("function_call", "tool_calls"),
        ("content_filter", "content_filter"),
        ("surprise", "stop"),
    ],
)
def test_finish_reason_spellings(raw: str, expected: str) -> None:
    events = parse(T, [sse(chunk({"content": "x"}, finish=raw), "[DONE]")])
    assert events[-1] == Finish(expected)  # type: ignore[arg-type]


def test_key_never_appears_in_stream_error() -> None:
    leaked = "sk-" + "or-v1-" + "f" * 40
    body = sse(f"bad request with key {leaked}")
    with pytest.raises(ProviderStreamError) as caught:
        parse(T, [body])
    assert leaked not in str(caught.value) and leaked not in json.dumps(caught.value.body)


# -- non-streaming -----------------------------------------------------------------------------


def test_parse_response_json() -> None:
    payload = {
        "id": "chatcmpl-9",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "thought",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": '{"x":1}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }
    response = T.parse_response(payload)
    assert response.content is None and response.reasoning == "thought"
    assert response.tool_calls == (ToolCall("c1", "f", '{"x":1}'),)
    assert response.finish_reason == "tool_calls"
    assert response.usage == Usage(3, 4)
    assert response.provider_data == {"id": "chatcmpl-9", "model": "m"}


def test_parse_response_refusal_only() -> None:
    payload = {
        "choices": [
            {"message": {"content": "", "refusal": "I can't help."}, "finish_reason": "stop"}
        ]
    }
    response = T.parse_response(payload)
    assert (response.content, response.finish_reason) == ("I can't help.", "content_filter")


def test_parse_response_error_body() -> None:
    with pytest.raises(ProviderStreamError):
        T.parse_response({"error": {"message": "nope", "code": 400}})


# -- stream_options compatibility retry --------------------------------------------------------


def test_stream_options_rejection_retried_once_without_it() -> None:
    transport = ChatCompletionsTransport()
    route = make_route(base_url="https://strict.fake.invalid/v1")
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "stream_options" in body:
            message = "Unrecognized request argument supplied: stream_options"
            return httpx.Response(400, json={"error": {"message": message}})
        return httpx.Response(200, content=sse(chunk({"content": "ok"}, finish="stop"), "[DONE]"))

    async def call() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            req = ChatRequest(model="m", messages=HELLO)
            response = await open_call(client, transport, route, req)
            try:
                return [e async for e in stream_events(transport, response)]
            finally:
                await response.aclose()

    assert run(call())[-1] == Finish("stop")
    assert ["stream_options" in b for b in seen] == [True, False]
    run(call())  # the flag is remembered for this route: one request, no rejection
    assert ["stream_options" in b for b in seen] == [True, False, False]


def test_other_400_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "messages: field required"}})

    async def call() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await open_call(client, T, make_route(), ChatRequest(model="m", messages=HELLO))

    with pytest.raises(ProviderHTTPError) as caught:
        run(call())
    assert caught.value.status == 400 and calls == 1


def test_fresh_id_on_every_fragment_stays_one_call() -> None:
    body = sse(
        chunk({"tool_calls": [tc(0, id="a1", name="write", args='{"text": ')]}),
        chunk({"tool_calls": [tc(0, id="a2", args='"hi"')]}),
        chunk({"tool_calls": [tc(0, id="a3", name="write", args="}")]}),
        chunk({}, finish="tool_calls"),
        "[DONE]",
    )
    calls = assemble(parse(T, [body])).tool_calls
    assert calls == (ToolCall("a1", "write", '{"text": "hi"}'),)


def test_choiceless_error_chunk() -> None:
    body = sse({"choices": [], "error_type": "invalid_request", "error_message": "bad tools"})
    with pytest.raises(ProviderStreamError) as caught:
        parse(T, [body])
    assert caught.value.body["error"]["message"] == "bad tools"  # type: ignore[index]


def test_streamed_refusal_only_reads_as_content_filter() -> None:
    body = sse(
        chunk({"refusal": "I can't "}), chunk({"refusal": "do that."}, finish="stop"), "[DONE]"
    )
    events = parse(T, [body])
    assert events[-2:] == [TextDelta("I can't do that."), Finish("content_filter")]
