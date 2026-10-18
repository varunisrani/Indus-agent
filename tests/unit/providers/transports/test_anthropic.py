"""Anthropic Messages transport (P03-10): conversion goldens, thinking, stream events."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from indus.foundation.errors import ProviderStreamError
from indus.providers.profile import ProviderProfile
from indus.providers.transports.anthropic import AnthropicTransport, convert_messages
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
    anthropic_sse,
    body_of,
    make_profile,
    make_route,
    parse,
)

T = AnthropicTransport()
SIGNATURE = "EqQBCgIYAhIM+/=" + "x" * 40 + "=="


def build(req: ChatRequest, profile: ProviderProfile | None = None, **route_kw: Any):  # noqa: ANN201
    return T.build_request(make_route("anthropic_messages", profile=profile, **route_kw), req)


def call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


# -- conversion --------------------------------------------------------------------------------


def test_two_tool_results_merged_into_one_user_turn_in_order() -> None:
    history = [
        {"role": "user", "content": "weather and time?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [call("c1", "weather", '{"city": "Oslo"}'), call("c2", "clock", "{}")],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "sunny"},
        {"role": "tool", "tool_call_id": "c2", "content": "12:00"},
    ]
    system, messages = convert_messages(history)
    assert system is None
    assert messages == [
        {"role": "user", "content": "weather and time?"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "c1", "name": "weather", "input": {"city": "Oslo"}},
                {"type": "tool_use", "id": "c2", "name": "clock", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "sunny"},
                {"type": "tool_result", "tool_use_id": "c2", "content": "12:00"},
            ],
        },
    ]


def test_system_messages_joined_and_extracted() -> None:
    system, messages = convert_messages(
        [
            {"role": "system", "content": "Rule one."},
            {"role": "system", "content": [{"type": "text", "text": "Rule two."}]},
            {"role": "user", "content": "hi"},
        ]
    )
    assert system == "Rule one.\n\nRule two."
    assert messages == [{"role": "user", "content": "hi"}]


def test_empty_text_blocks_dropped_and_turns_merged() -> None:
    _, messages = convert_messages(
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": "  "}, {"type": "text", "text": "a"}],
            },
            {"role": "assistant", "content": "   "},
            {"role": "user", "content": "b"},
        ]
    )
    assert messages == [
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    ]


def test_invalid_tool_json_wrapped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    bad = '{"path": "a.txt", '
    with caplog.at_level(logging.WARNING):
        _, messages = convert_messages(
            [
                {"role": "user", "content": "go"},
                {"role": "assistant", "tool_calls": [call("c1", "read", bad)]},
            ]
        )
    assert messages[1]["content"][0]["input"] == {"_raw": bad}
    assert "read" in caplog.text and bad not in caplog.text


def test_tool_ids_sanitised_consistently_and_empty_result() -> None:
    _, messages = convert_messages(
        [
            {"role": "user", "content": "x"},
            {"role": "assistant", "tool_calls": [call("functions.read:0", "read", "{}")]},
            {"role": "tool", "tool_call_id": "functions.read:0", "content": ""},
        ]
    )
    assert messages[1]["content"][0]["id"] == "functions_read_0"
    assert messages[2]["content"] == [{"type": "tool_result", "tool_use_id": "functions_read_0"}]


def test_images_become_image_blocks() -> None:
    _, messages = convert_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    {"type": "image_url", "image_url": {"url": "https://img.fake.invalid/a.jpg"}},
                ],
            }
        ]
    )
    assert messages[0]["content"][1:] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {"type": "image", "source": {"type": "url", "url": "https://img.fake.invalid/a.jpg"}},
    ]


def test_signature_replayed_byte_exact() -> None:
    thinking = {"type": "thinking", "thinking": "let me think\n", "signature": SIGNATURE}
    request = build(
        ChatRequest(
            model="claude-test",
            messages=[
                {"role": "user", "content": "q"},
                {
                    "role": "assistant",
                    "content": "a",
                    "reasoning_details": [thinking, {"type": "other"}],
                },
                {"role": "user", "content": "q2"},
            ],
        )
    )
    blocks = body_of(request)["messages"][1]["content"]
    assert blocks[0] == thinking
    assert blocks[1] == {"type": "text", "text": "a"}
    assert SIGNATURE.encode() in request.content


# -- request ----------------------------------------------------------------------------------


def test_request_golden_with_tools() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Weather.",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    req = ChatRequest(
        model="claude-test",
        messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}],
        tools=tools,
        tool_choice="required",
        max_tokens=1000,
        temperature=0.3,
    )
    request = build(req)
    assert str(request.url) == "https://anthropic.fake.invalid/v1/messages"
    assert body_of(request) == {
        "model": "claude-test",
        "max_tokens": 1000,
        "system": "S",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "name": "get_weather",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                "description": "Weather.",
            }
        ],
        "tool_choice": {"type": "any"},
        "stream": True,
        "temperature": 0.3,
    }
    assert request.headers["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
        ("https://api.minimax.io/anthropic", "https://api.minimax.io/anthropic/v1/messages"),
        ("https://gw.fake.invalid/anthropic/v1/", "https://gw.fake.invalid/anthropic/v1/messages"),
    ],
)
def test_messages_url_not_doubled(base_url: str, expected: str) -> None:
    assert str(build(ChatRequest(model="m", messages=[]), base_url=base_url).url) == expected


def test_auth_header_per_host() -> None:
    official = build(ChatRequest(model="m", messages=[]), base_url="https://api.anthropic.com")
    assert official.headers["x-api-key"] == FAKE_KEY and "authorization" not in official.headers
    bearer = build(
        ChatRequest(model="m", messages=[]),
        make_profile(bearer_auth=True),
        base_url="https://api.minimax.io/anthropic",
    )
    assert bearer.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert "x-api-key" not in bearer.headers
    # even if a profile says bearer, the official host gets its own header
    official_bearer = build(
        ChatRequest(model="m", messages=[]),
        make_profile(bearer_auth=True),
        base_url="https://api.anthropic.com",
    )
    assert "x-api-key" in official_bearer.headers


def test_no_header_names_another_product() -> None:
    request = build(ChatRequest(model="m", messages=[{"role": "user", "content": "x"}]))
    allowed = {"content-type", "accept", "anthropic-version", "x-api-key", "content-length", "host"}
    assert {k.lower() for k in request.headers} <= allowed
    assert "anthropic-beta" not in request.headers
    joined = " ".join(f"{k}: {v}" for k, v in request.headers.items()).lower()
    assert "claude-code" not in joined and "cli" not in joined


@pytest.mark.parametrize(
    ("effort", "budget"),
    [
        ("low", 4000),
        ("medium", 8000),
        ("high", 16000),
        ("xhigh", 32000),
        ("max", 32000),
        ("odd", 8000),
    ],
)
def test_thinking_budgets(effort: str, budget: int) -> None:
    body = body_of(
        build(
            ChatRequest(
                model="claude-test",
                messages=[],
                max_tokens=4000,
                temperature=0.2,
                reasoning_effort=effort,
            )
        )
    )
    assert body["thinking"] == {"type": "enabled", "budget_tokens": budget}
    assert body["temperature"] == 1
    assert body["max_tokens"] == max(4000, budget + 4096)


def test_high_effort_golden_numbers() -> None:
    body = body_of(
        build(
            ChatRequest(model="claude-test", messages=[], max_tokens=8192, reasoning_effort="high")
        )
    )
    assert body["thinking"]["budget_tokens"] == 16000
    assert body["max_tokens"] == 20096
    assert body["temperature"] == 1


def test_large_max_tokens_kept_and_off_means_no_thinking() -> None:
    body = body_of(
        build(ChatRequest(model="m", messages=[], max_tokens=64000, reasoning_effort="low"))
    )
    assert body["max_tokens"] == 64000
    off = body_of(build(ChatRequest(model="m", messages=[], reasoning_effort="none")))
    assert "thinking" not in off and off["max_tokens"] == 8192


def test_adaptive_models_use_effort_field() -> None:
    profile = make_profile(adaptive_thinking_models=("claude-new",))
    body = body_of(
        build(ChatRequest(model="claude-new-1", messages=[], reasoning_effort="ultra"), profile)
    )
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "max"}
    assert "temperature" not in body and body["max_tokens"] == 8192


# -- stream ------------------------------------------------------------------------------------


def _ev(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, **fields}


STREAM = anthropic_sse(
    _ev(
        "message_start",
        message={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": "m",
            "usage": {
                "input_tokens": 50,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 7,
                "output_tokens": 1,
            },
        },
    ),
    _ev(
        "content_block_start",
        index=0,
        content_block={"type": "thinking", "thinking": "", "signature": ""},
    ),
    _ev(
        "content_block_delta",
        index=0,
        delta={"type": "thinking_delta", "thinking": "Need weather."},
    ),
    _ev("content_block_delta", index=0, delta={"type": "signature_delta", "signature": SIGNATURE}),
    _ev("content_block_stop", index=0),
    _ev("ping"),
    _ev("content_block_start", index=1, content_block={"type": "text", "text": ""}),
    _ev("content_block_delta", index=1, delta={"type": "text_delta", "text": "Checking"}),
    _ev("content_block_delta", index=1, delta={"type": "text_delta", "text": "."}),
    _ev("content_block_stop", index=1),
    _ev(
        "content_block_start",
        index=2,
        content_block={"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}},
    ),
    _ev("content_block_delta", index=2, delta={"type": "input_json_delta", "partial_json": ""}),
    _ev(
        "content_block_delta",
        index=2,
        delta={"type": "input_json_delta", "partial_json": '{"city":'},
    ),
    _ev(
        "content_block_delta",
        index=2,
        delta={"type": "input_json_delta", "partial_json": ' "Oslo"}'},
    ),
    _ev("content_block_stop", index=2),
    _ev(
        "message_delta",
        delta={"stop_reason": "tool_use", "stop_sequence": None},
        usage={"output_tokens": 89},
    ),
    _ev("message_stop"),
)


def test_stream_event_sequence_golden() -> None:
    events = parse(T, [STREAM[:333], STREAM[333:334], STREAM[334:]])
    first_usage = Usage(
        prompt_tokens=157, completion_tokens=1, cache_read_tokens=100, cache_write_tokens=7
    )
    assert events == [
        UsageReport(first_usage),
        ReasoningDelta("Need weather."),
        ReasoningDetail({"type": "thinking", "thinking": "Need weather.", "signature": SIGNATURE}),
        KeepAlive("ping"),
        TextDelta("Checking"),
        TextDelta("."),
        ToolCallStart(0, "toolu_1", "get_weather"),
        ToolCallArgsDelta(0, '{"city":'),
        ToolCallArgsDelta(0, ' "Oslo"}'),
        UsageReport(Usage(157, 89, cache_read_tokens=100, cache_write_tokens=7)),
        Finish("tool_calls"),
    ]
    response = assemble(events)
    assert response.tool_calls == (ToolCall("toolu_1", "get_weather", '{"city": "Oslo"}'),)
    assert response.reasoning_details[0]["signature"] == SIGNATURE


def test_tool_use_without_input_fragments_is_empty_object() -> None:
    body = anthropic_sse(
        _ev("message_start", message={"usage": {"input_tokens": 1, "output_tokens": 0}}),
        _ev(
            "content_block_start",
            index=0,
            content_block={"type": "tool_use", "id": "t", "name": "now", "input": {}},
        ),
        _ev("content_block_stop", index=0),
        _ev("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 3}),
        _ev("message_stop"),
    )
    assert assemble(parse(T, [body])).tool_calls == (ToolCall("t", "now", "{}"),)


def test_error_event_keeps_type() -> None:
    body = anthropic_sse(
        _ev("message_start", message={"usage": {"input_tokens": 1}}),
        _ev("error", error={"type": "overloaded_error", "message": "Overloaded"}),
    )
    with pytest.raises(ProviderStreamError) as caught:
        parse(T, [body])
    assert caught.value.body == {
        "error": {"message": "Overloaded", "type": "overloaded_error", "code": "overloaded_error"}
    }


@pytest.mark.parametrize(
    ("stop", "finish"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("tool_use", "tool_calls"),
        ("max_tokens", "length"),
        ("refusal", "content_filter"),
        ("model_context_window_exceeded", "length"),
        ("pause_turn", "stop"),
    ],
)
def test_stop_reason_map(stop: str, finish: str) -> None:
    body = anthropic_sse(_ev("message_delta", delta={"stop_reason": stop}), _ev("message_stop"))
    assert parse(T, [body])[-1] == Finish(finish)  # type: ignore[arg-type]


def test_unknown_events_tolerated_and_eof_rules() -> None:
    body = anthropic_sse(
        _ev("content_block_start", index=0, content_block={"type": "text", "text": "hi"}),
        _ev("brand_new_event", payload=1),
        _ev("message_delta", delta={"stop_reason": "end_turn"}),
    )
    assert parse(T, [body]) == [TextDelta("hi"), Finish("stop")]  # EOF after a stop reason: clean
    with pytest.raises(ProviderStreamError) as caught:
        parse(
            T,
            [
                anthropic_sse(
                    _ev(
                        "content_block_start", index=0, content_block={"type": "text", "text": "hi"}
                    )
                )
            ],
        )
    assert caught.value.body["error"]["code"] == "incomplete_stream"  # type: ignore[index]


def test_redacted_thinking_kept_as_detail() -> None:
    body = anthropic_sse(
        _ev(
            "content_block_start",
            index=0,
            content_block={"type": "redacted_thinking", "data": "opaque"},
        ),
        _ev("content_block_stop", index=0),
        _ev("message_delta", delta={"stop_reason": "end_turn"}),
        _ev("message_stop"),
    )
    assert parse(T, [body])[0] == ReasoningDetail({"type": "redacted_thinking", "data": "opaque"})


def test_parse_response_json() -> None:
    payload = {
        "id": "msg_9",
        "type": "message",
        "model": "m",
        "content": [
            {"type": "thinking", "thinking": "hm", "signature": SIGNATURE},
            {"type": "text", "text": "Answer"},
            {"type": "tool_use", "id": "toolu_2", "name": "f", "input": {"a": 1}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2},
    }
    response = T.parse_response(payload)
    assert response.content == "Answer" and response.reasoning == "hm"
    assert response.reasoning_details == (
        {"type": "thinking", "thinking": "hm", "signature": SIGNATURE},
    )
    assert response.tool_calls == (ToolCall("toolu_2", "f", '{"a": 1}'),)
    assert response.finish_reason == "tool_calls"
    assert response.usage == Usage(12, 5, cache_read_tokens=2)
    with pytest.raises(ProviderStreamError):
        T.parse_response(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
        )


def test_merged_assistant_turns_drop_the_later_thinking() -> None:
    thinking = {"type": "thinking", "thinking": "t", "signature": SIGNATURE}
    _, messages = convert_messages(
        [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a", "reasoning_details": [thinking]},
            {"role": "assistant", "content": "b", "reasoning_details": [thinking]},
        ]
    )
    assert messages[1]["content"] == [
        thinking,
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]
