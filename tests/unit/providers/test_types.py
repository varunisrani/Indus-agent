"""Normalized types, ``assemble`` and the transport registry (P03-06)."""

from __future__ import annotations

import httpx
import pytest

from indus.foundation.errors import ProviderHTTPError
from indus.providers.transports import (
    Transport,
    UnsupportedApiMode,
    get_transport,
    http_error_from_response,
    registered_api_modes,
)
from indus.providers.transports.anthropic import AnthropicTransport
from indus.providers.transports.chat_completions import ChatCompletionsTransport
from indus.providers.types import (
    CONTENT_EVENTS,
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
    arguments_object,
    assemble,
    clamp_effort,
    map_finish_reason,
)

TABLE = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length", "x": "bogus"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("end_turn", "stop"),
        ("tool_use", "tool_calls"),
        ("max_tokens", "length"),
        ("something_new", "stop"),
        (None, "stop"),
        ("x", "stop"),  # a table entry outside the closed set is not trusted
    ],
)
def test_map_finish_reason_table(raw: str | None, expected: str) -> None:
    assert map_finish_reason(raw, TABLE) == expected


def test_assemble_keeps_arguments_bytes() -> None:
    raw = '{"b": 1,  "a":"x"}'
    events = [
        ToolCallStart(0, "call_1", "lookup"),
        ToolCallArgsDelta(0, raw[:7]),
        ToolCallArgsDelta(0, raw[7:]),
        Finish("tool_calls"),
    ]
    response = assemble(events)
    assert response.tool_calls == (ToolCall("call_1", "lookup", raw),)
    assert response.tool_calls[0].arguments == raw  # no re-serialisation (R01.1 groundwork)


def test_assemble_late_id_and_order() -> None:
    events = [
        ToolCallStart(1, "call_b", "second"),
        ToolCallStart(0, None, "first"),
        ToolCallArgsDelta(0, "{}"),
        ToolCallStart(0, "call_a", "first"),
        ToolCallArgsDelta(1, '{"q": 2}'),
    ]
    response = assemble(events)
    assert [(c.id, c.name, c.arguments) for c in response.tool_calls] == [
        ("call_a", "first", "{}"),
        ("call_b", "second", '{"q": 2}'),
    ]
    assert response.finish_reason == "tool_calls"


def test_assemble_reasoning_details_order_and_usage_last_wins() -> None:
    d1 = {"type": "thinking", "thinking": "a", "signature": "s1"}
    d2 = {"type": "reasoning.encrypted", "data": "zz"}
    d3 = {"type": "thinking", "thinking": "b", "signature": "s2"}
    events = [
        ReasoningDelta("think "),
        ReasoningDetail(d1),
        UsageReport(Usage(prompt_tokens=5)),
        TextDelta("hel"),
        KeepAlive(),
        ReasoningDetail(d2),
        TextDelta("lo"),
        ReasoningDetail(d3),
        UsageReport(Usage(prompt_tokens=5, completion_tokens=2)),
        Finish("stop"),
    ]
    response = assemble(events)
    assert response.reasoning_details == (d1, d2, d3)
    assert response.content == "hello"
    assert response.reasoning == "think "
    assert response.usage == Usage(prompt_tokens=5, completion_tokens=2)
    assert response.finish_reason == "stop"


def test_assemble_empty_arguments_become_empty_object() -> None:
    response = assemble([ToolCallStart(0, "c", "noop"), Finish("tool_calls")])
    assert response.tool_calls[0].arguments == "{}"
    assert arguments_object(response.tool_calls[0].arguments) == {}
    assert arguments_object("[1]") is None
    assert arguments_object("{bad") is None


def test_keepalive_is_not_a_content_event() -> None:
    assert KeepAlive not in CONTENT_EVENTS
    assert assemble([KeepAlive(), KeepAlive("ping")]).content is None


@pytest.mark.parametrize(
    ("requested", "supported", "expected"),
    [
        ("xhigh", ("low", "medium", "high"), "high"),
        ("high", ("low", "medium", "high"), "high"),
        ("medium", ("low", "high"), "low"),
        ("minimal", ("low", "medium", "high"), "low"),
        ("ultra", ("minimal", "low", "medium", "high", "xhigh", "max"), "max"),
        ("none", ("low", "high"), None),
        ("Disabled", ("low",), None),
        (None, ("low",), None),
        ("X-High", ("low", "medium", "high", "xhigh"), "xhigh"),
        ("weird", ("low", "medium", "high"), "medium"),
        ("high", (), None),
    ],
)
def test_clamp_effort(
    requested: str | None, supported: tuple[str, ...], expected: str | None
) -> None:
    assert clamp_effort(requested, supported) == expected


def test_registry_returns_p03_transports() -> None:
    chat = get_transport("chat_completions")
    anthropic = get_transport("anthropic_messages")
    assert isinstance(chat, ChatCompletionsTransport) and chat.api_mode == "chat_completions"
    assert isinstance(anthropic, AnthropicTransport) and anthropic.api_mode == "anthropic_messages"
    assert get_transport("chat_completions") is chat  # one shared instance per mode
    assert isinstance(chat, Transport) and isinstance(anthropic, Transport)
    assert {"chat_completions", "anthropic_messages"} <= registered_api_modes()


@pytest.mark.parametrize("mode", ["responses", "bedrock", "gemini", "nonsense"])
def test_registry_refuses_later_modes(mode: str) -> None:
    with pytest.raises(UnsupportedApiMode) as caught:
        get_transport(mode)
    assert mode in str(caught.value)


def test_http_error_from_response_reads_retry_after_and_message() -> None:
    body = b'{"error": {"message": "slow down", "type": "rate_limit_error"}}'
    err = http_error_from_response(
        429, {"Retry-After": "7"}, body, url="https://api.fake.invalid/v1/chat/completions"
    )
    assert isinstance(err, ProviderHTTPError)
    assert (err.status, err.retry_after, err.code) == (429, 7.0, "provider.http")
    assert "slow down" in str(err) and "api.fake.invalid" in str(err)
    assert isinstance(err.body, dict)


def test_http_error_never_carries_the_key() -> None:
    key = "test-key-" + "9" * 24
    body = f'{{"error": {{"message": "bad key {key}"}}}}'.encode()
    err = http_error_from_response(401, {}, body, secrets=[key])
    assert key not in str(err) and key not in repr(err.body)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({"retry-after-ms": "1500"}, 1.5), ({"Retry-After": "soon"}, None), ({}, None)],
)
def test_retry_after_forms(headers: dict[str, str], expected: float | None) -> None:
    assert http_error_from_response(503, headers, b"").retry_after == expected


def test_plain_text_error_body() -> None:
    err = http_error_from_response(502, {}, b"<html>bad gateway</html>")
    assert "bad gateway" in str(err)
    assert isinstance(httpx.URL("https://x.invalid"), httpx.URL)
