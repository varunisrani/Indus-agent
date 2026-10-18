"""Gemini native transport (P08-11): key handling, thinking config, conversion, stream parsing."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from indus.foundation.errors import ProviderStreamError
from indus.providers.classify import classify
from indus.providers.route import RuntimeRoute, Secret
from indus.providers.transports import iter_call
from indus.providers.transports.gemini import (
    FOREIGN_CALL_SIGNATURE,
    SIGNATURE_TYPE,
    GeminiTransport,
    bare_model,
    convert_contents,
    generation,
    thinking_config,
)
from indus.providers.types import (
    ChatRequest,
    Finish,
    ReasoningDetail,
    StreamEvent,
    ToolCallArgsDelta,
    ToolCallStart,
    assemble,
)
from tests.fakes.model.replay import ReplayTransport, chat_request_from, load_capture
from tests.unit.providers._support import body_of, parse

KEY = "test-" + "gemini-key-0000"
MODEL = "gemini-2.5-flash"


def route(model: str = MODEL, *, key_env: str = "GEMINI_API_KEY") -> RuntimeRoute:
    return RuntimeRoute(
        provider="gemini",
        model=model,
        api_mode="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key=Secret(KEY),
        key_env=key_env,
    )


def chunk(parts: list[dict[str, Any]], **candidate: Any) -> bytes:
    payload = {"candidates": [{"content": {"role": "model", "parts": parts}, **candidate}]}
    return f"data: {json.dumps(payload)}\r\n\r\n".encode()


def data(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


# -- request ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [True, False])
def test_key_never_in_url(stream: bool) -> None:
    req = ChatRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}], stream=stream)
    request = GeminiTransport().build_request(route(), req)
    assert KEY not in str(request.url)
    assert "key=" not in str(request.url)
    assert request.headers["x-goog-api-key"] == KEY
    method = "streamGenerateContent" if stream else "generateContent"
    assert request.url.path == f"/v1beta/models/{MODEL}:{method}"
    assert (request.url.params.get("alt") == "sse") is stream


def test_openai_key_is_not_sent_to_google() -> None:
    request = GeminiTransport().build_request(
        route(key_env="OPENAI_API_KEY"), ChatRequest(model=MODEL, messages=[])
    )
    assert "x-goog-api-key" not in request.headers


def test_model_names() -> None:
    assert bare_model("models/gemini-2.5-pro") == "gemini-2.5-pro"
    assert bare_model("google/gemini-3-flash") == "gemini-3-flash"
    assert generation("gemini-2.5-flash-lite") == (2, 5)
    assert generation("gemini-3.1-pro-preview") == (3, 1)
    assert generation("gemma-3-27b-it") == (0, 0)


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("gemini-2.5-flash", None, {"includeThoughts": False, "thinkingBudget": 0}),
        ("gemini-2.5-pro", None, {"includeThoughts": False}),
        ("gemini-2.5-flash", "minimal", {"includeThoughts": True, "thinkingBudget": 512}),
        ("gemini-2.5-flash", "low", {"includeThoughts": True, "thinkingBudget": 2048}),
        ("gemini-2.5-pro", "medium", {"includeThoughts": True, "thinkingBudget": 8192}),
        ("gemini-2.5-pro", "high", {"includeThoughts": True, "thinkingBudget": 16384}),
        ("gemini-2.5-pro", "ultra", {"includeThoughts": True, "thinkingBudget": 24576}),
        ("gemini-3-pro-preview", None, {"includeThoughts": False, "thinkingLevel": "low"}),
        ("gemini-3-pro-preview", "low", {"includeThoughts": True, "thinkingLevel": "low"}),
        ("gemini-3-pro-preview", "medium", {"includeThoughts": True, "thinkingLevel": "high"}),
        ("gemini-3-flash", "medium", {"includeThoughts": True, "thinkingLevel": "medium"}),
        ("gemini-3-flash", "minimal", {"includeThoughts": True, "thinkingLevel": "low"}),
        ("gemini-4-argon", "xhigh", {"includeThoughts": True, "thinkingLevel": "high"}),
        ("gemini-flash-latest", "high", {"includeThoughts": True, "thinkingLevel": "high"}),
        ("gemini-2.0-flash", "high", None),
        ("gemma-3-27b-it", "high", None),
    ],
)
def test_thinking_config_per_level(model: str, effort: str | None, expected: Any) -> None:
    assert thinking_config(model, effort) == expected


def test_request_body_golden() -> None:
    req = ChatRequest(
        model=MODEL,
        messages=[{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file.",
                    "parameters": {"type": "object", "properties": {"p": {"type": "string"}}},
                },
            },
            {"type": "function", "function": {"name": "now", "parameters": {"type": "object"}}},
        ],
        tool_choice="required",
        temperature=0.2,
        max_tokens=300,
        reasoning_effort="high",
    )
    assert body_of(GeminiTransport().build_request(route(), req)) == {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "systemInstruction": {"parts": [{"text": "be brief"}]},
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": "read",
                        "description": "Read a file.",
                        "parameters": {"type": "object", "properties": {"p": {"type": "string"}}},
                    },
                    {"name": "now"},
                ]
            }
        ],
        "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 300,
            "thinkingConfig": {"includeThoughts": True, "thinkingBudget": 16384},
        },
    }


def _history(call_id: str) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "content": "Checking.",
            "reasoning_details": [
                {"type": SIGNATURE_TYPE, "call_id": call_id, "signature": "sigA"},
                {"type": "thinking", "thinking": "other wire"},
            ],
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {"name": "get_weather", "arguments": '{"city":"Oslo"}'},
                },
                {"id": "other", "function": {"name": "now", "arguments": ""}},
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": '{"temp": 12}'},
        {"role": "tool", "tool_call_id": "other", "name": "ignored", "content": "noon"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "and this?"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
            ],
        },
    ]


def test_conversion_gemini_2() -> None:
    _, contents = convert_contents(_history("call_0_get_weather"), model=MODEL)
    assert contents == [
        {"role": "user", "parts": [{"text": "weather?"}]},
        {
            "role": "model",
            "parts": [
                {"text": "Checking."},
                {
                    "functionCall": {"name": "get_weather", "args": {"city": "Oslo"}},
                    "thoughtSignature": "sigA",
                },
                {"functionCall": {"name": "now", "args": {}}},
            ],
        },
        {
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "get_weather", "response": {"temp": 12}}},
                {"functionResponse": {"name": "now", "response": {"output": "noon"}}},
                {"text": "and this?"},
                {"inlineData": {"mimeType": "image/jpeg", "data": "QUJD"}},
            ],
        },
    ]


def test_conversion_gemini_3_sends_ids_and_signs_foreign_calls() -> None:
    _, contents = convert_contents(_history("fc-1"), model="gemini-3-pro-preview")
    model_parts = contents[1]["parts"]
    assert model_parts[1] == {
        "functionCall": {"name": "get_weather", "args": {"city": "Oslo"}, "id": "fc-1"},
        "thoughtSignature": "sigA",
    }
    assert model_parts[2]["thoughtSignature"] == FOREIGN_CALL_SIGNATURE
    assert contents[2]["parts"][0]["functionResponse"]["id"] == "fc-1"


def test_conversation_opens_with_a_user_turn() -> None:
    _, contents = convert_contents([{"role": "assistant", "content": "hi"}], model=MODEL)
    assert contents[0]["role"] == "user" and contents[1]["role"] == "model"


# -- reply -----------------------------------------------------------------------------------------


def test_stream_text_thoughts_and_usage() -> None:
    events = parse(
        GeminiTransport(),
        [
            chunk([{"text": "planning", "thought": True}]),
            chunk([{"text": "Hel"}]),
            data(
                {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "lo"}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 50,
                        "candidatesTokenCount": 5,
                        "thoughtsTokenCount": 20,
                        "cachedContentTokenCount": 30,
                    },
                }
            ),
        ],
    )
    response = assemble(events)
    assert (response.content, response.reasoning, response.finish_reason) == (
        "Hello",
        "planning",
        "stop",
    )
    assert response.usage is not None
    assert response.usage.completion_tokens == 25 and response.usage.reasoning_tokens == 20
    assert response.usage.cache_read_tokens == 30 and response.usage.prompt_tokens == 50


@pytest.mark.parametrize(
    ("reason", "finish"),
    [
        ("SAFETY", "content_filter"),
        ("RECITATION", "content_filter"),
        ("PROHIBITED_CONTENT", "content_filter"),
        ("BLOCKLIST", "content_filter"),
        ("SPII", "content_filter"),
        ("MAX_TOKENS", "length"),
        ("MALFORMED_FUNCTION_CALL", "error"),
        ("OTHER", "stop"),
        ("SOMETHING_NEW", "stop"),
    ],
)
def test_safety_block_mapping(reason: str, finish: str) -> None:
    events = parse(GeminiTransport(), [chunk([{"text": "x"}], finishReason=reason)])
    assert events[-1] == Finish(finish)  # type: ignore[arg-type]
    payload = {"candidates": [{"content": {"parts": []}, "finishReason": reason}]}
    assert GeminiTransport().parse_response(payload).finish_reason == finish


def test_prompt_blocked_before_generation() -> None:
    blocked = {
        "promptFeedback": {"blockReason": "SAFETY"},
        "usageMetadata": {"promptTokenCount": 9},
    }
    assert parse(GeminiTransport(), [data(blocked)])[-1] == Finish("content_filter")
    response = GeminiTransport().parse_response(blocked)
    assert response.finish_reason == "content_filter" and response.content is None


def test_stream_resent_arguments_emit_suffix_only() -> None:
    call = {"functionCall": {"name": "get_weather", "args": {"city": "Oslo"}}}
    events = parse(
        GeminiTransport(),
        [chunk([call]), chunk([call], finishReason="STOP")],
    )
    starts = [e for e in events if isinstance(e, ToolCallStart)]
    args = [e for e in events if isinstance(e, ToolCallArgsDelta)]
    assert len(starts) == 1 and "".join(a.fragment for a in args) == '{"city": "Oslo"}'
    response = assemble(events)
    assert response.finish_reason == "tool_calls"
    assert [json.loads(c.arguments) for c in response.tool_calls] == [{"city": "Oslo"}]


def test_parallel_calls_in_one_chunk_and_provider_ids() -> None:
    parts = [
        {"functionCall": {"name": "a", "args": {"x": 1}, "id": "id-a"}},
        {"functionCall": {"name": "a", "args": {"x": 1}, "id": "id-b"}},
    ]
    response = assemble(parse(GeminiTransport(), [chunk(parts, finishReason="STOP")]))
    assert [c.id for c in response.tool_calls] == ["id-a", "id-b"]


def test_signature_round_trip() -> None:
    signed = {
        "functionCall": {"name": "get_weather", "args": {"city": "Oslo"}},
        "thoughtSignature": "c2lnbmVk",
    }
    events = parse(
        GeminiTransport(),
        [
            chunk([{"text": "Sure.", "thoughtSignature": "dGV4dA=="}]),
            chunk([signed], finishReason="STOP"),
        ],
    )
    response = assemble(events)
    call = response.tool_calls[0]
    assert ReasoningDetail({"type": SIGNATURE_TYPE, "signature": "dGV4dA=="}) in events
    assistant = {
        "role": "assistant",
        "content": response.content,
        "reasoning_details": list(response.reasoning_details),
        "tool_calls": [
            {"id": call.id, "function": {"name": call.name, "arguments": call.arguments}}
        ],
    }
    _, contents = convert_contents([{"role": "user", "content": "q"}, assistant], model=MODEL)
    assert contents[1]["parts"] == [
        {"text": "Sure.", "thoughtSignature": "dGV4dA=="},
        {
            "functionCall": {"name": "get_weather", "args": {"city": "Oslo"}},
            "thoughtSignature": "c2lnbmVk",
        },
    ]


def test_error_chunk_and_eof() -> None:
    error = {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}
    with pytest.raises(ProviderStreamError) as caught:
        parse(GeminiTransport(), [data(error)])
    assert classify(caught.value).reason == "rate_limit"
    with pytest.raises(ProviderStreamError) as eof:
        parse(GeminiTransport(), [chunk([{"text": "cut"}])])
    assert classify(eof.value).reason == "incomplete_response"
    with pytest.raises(ProviderStreamError):
        GeminiTransport().parse_response(error)


@pytest.mark.parametrize("scenario", ["text", "tool_call"])
def test_captures_replay(scenario: str) -> None:
    capture = load_capture(f"gemini/{scenario}")
    req = chat_request_from(capture)
    capture_route = RuntimeRoute("gemini", req.model, "gemini", capture["meta"]["base_url"])

    async def main() -> list[StreamEvent]:
        async with httpx.AsyncClient(transport=ReplayTransport(capture)) as client:
            return [e async for e in iter_call(client, GeminiTransport(), capture_route, req)]

    response = assemble(asyncio.run(main()))
    if scenario == "text":
        assert response.content == "hello" and response.finish_reason == "stop"
    else:
        assert response.finish_reason == "tool_calls"
        assert [(c.name, json.loads(c.arguments)) for c in response.tool_calls] == [
            ("get_weather", {"city": "Oslo"})
        ]
    assert response.usage is not None and response.usage.prompt_tokens > 0
