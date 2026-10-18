"""Bedrock Converse transport (P08-10): SigV4, event-stream framing, conversion, stream events."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import AsyncIterator, Awaitable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.errors import NoUsableProvider, ProviderStreamError
from indus.foundation.scope import make_scope, use_scope
from indus.providers import aws_sigv4, context_length
from indus.providers.aws_sigv4 import AwsCredentials, region_from_host, resolve_credentials
from indus.providers.classify import classify
from indus.providers.eventstream import EventMessage, EventStreamDecoder, encode_message
from indus.providers.route import RuntimeRoute, Secret
from indus.providers.streaming import CallResult, StaleStreak, StreamFlags, stream_call
from indus.providers.timeouts import TimeoutPolicy
from indus.providers.transports import get_transport, iter_call
from indus.providers.transports.bedrock import (
    BLANK_TEXT,
    BedrockCredentialsMissing,
    BedrockTransport,
    convert_messages,
)
from indus.providers.types import ChatRequest, Finish, ReasoningDetail, StreamEvent, assemble
from tests.fakes.clock import FakeClock
from tests.fakes.model import Fail, Script, Text, Turn
from tests.fakes.model import Finish as FinishStep
from tests.fakes.model.replay import (
    ReplayTransport,
    capture_chunks,
    chat_request_from,
    load_capture,
)
from tests.fakes.model.server import FakeModelServer, fake_route
from tests.unit.providers._support import body_of

# The AWS SigV4 test-suite credentials, assembled at runtime (no key-shaped literal in the file).
AK = "AKID" + "EXAMPLE"
SK = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEX" + "AMPLEKEY"
CREDS = AwsCredentials(AK, SK)
SUITE_TIME = datetime(2015, 8, 30, 12, 36, tzinfo=UTC)
MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"
BASE = "https://bedrock-runtime.us-west-2.amazonaws.com"
START = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def run[T](coro: Awaitable[T]) -> T:
    async def main() -> T:
        return await coro

    return asyncio.run(main())


def route(*, key: str | None = None, key_env: str = "", model: str = MODEL) -> RuntimeRoute:
    return RuntimeRoute(
        provider="bedrock",
        model=model,
        api_mode="bedrock",
        base_url=BASE,
        api_key=Secret(key) if key else None,
        key_env=key_env,
    )


def signing_transport(**kw: Any) -> BedrockTransport:
    return BedrockTransport(credentials=lambda: CREDS, now=lambda: SUITE_TIME, **kw)


async def _items(chunks: Iterable[bytes]) -> AsyncIterator[EventMessage]:
    decoder = EventStreamDecoder()
    for chunk in chunks:
        for message in decoder.feed(chunk):
            yield message


def parse(chunks: Iterable[bytes]) -> list[StreamEvent]:
    async def main() -> list[StreamEvent]:
        return [e async for e in BedrockTransport().parse_stream(_items(chunks))]

    return asyncio.run(main())


def event(name: str, payload: dict[str, Any]) -> bytes:
    headers = {":event-type": name, ":content-type": "application/json", ":message-type": "event"}
    return encode_message(headers, json.dumps(payload).encode())


# -- SigV4 (AWS published test vectors) ------------------------------------------------------------


def test_signing_key_derivation_example() -> None:
    key = aws_sigv4.signing_key(SK, "20120215", "us-east-1", "iam")
    assert key.hex() == "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"


def test_suite_get_vanilla() -> None:
    request = httpx.Request("GET", "https://example.amazonaws.com/")
    aws_sigv4.sign(request, CREDS, region="us-east-1", service="service", now=SUITE_TIME)
    assert request.headers["x-amz-date"] == "20150830T123600Z"
    assert request.headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_suite_post_x_www_form_urlencoded() -> None:
    request = httpx.Request(
        "POST",
        "https://example.amazonaws.com/",
        headers={"content-type": "application/x-www-form-urlencoded"},
        content=b"Param1=value1",
    )
    aws_sigv4.sign(request, CREDS, region="us-east-1", service="service", now=SUITE_TIME)
    assert request.headers["authorization"].endswith(
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=ff11897932ad3f4e8b18135d722051e5ac45fc38421b1da7b9d196a0fe09473a"
    )


def test_iam_list_users_example() -> None:
    request = httpx.Request(
        "GET",
        "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        headers={"content-type": "application/x-www-form-urlencoded; charset=utf-8"},
    )
    aws_sigv4.sign(request, CREDS, region="us-east-1", service="iam", now=SUITE_TIME)
    assert request.headers["authorization"].endswith(
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )


def test_session_token_is_signed_and_model_path_encoded_twice() -> None:
    creds = AwsCredentials(AK, SK, "session" + "-token-value")
    transport = BedrockTransport(credentials=lambda: creds, now=lambda: START)
    request = transport.build_request(route(), ChatRequest(model=MODEL, messages=[]))
    assert request.url.raw_path.decode() == (
        "/model/anthropic.claude-sonnet-4-5-20250929-v1%3A0/converse-stream"
    )
    assert request.headers["x-amz-security-token"] == "session-token-value"
    auth = request.headers["authorization"]
    assert "/20261018/us-west-2/bedrock/aws4_request" in auth
    assert "x-amz-security-token" in auth.split("SignedHeaders=")[1]
    canonical, _ = aws_sigv4.canonical_parts(request, "0" * 64)
    assert "/model/anthropic.claude-sonnet-4-5-20250929-v1%253A0/converse-stream" in canonical
    assert "user-agent" not in canonical


# -- credentials and region ------------------------------------------------------------------------


def test_missing_credentials_is_a_named_error(tmp_path: Path) -> None:
    env = {"AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "missing")}
    transport = BedrockTransport(env=env)
    with pytest.raises(BedrockCredentialsMissing) as caught:
        transport.build_request(route(), ChatRequest(model=MODEL, messages=[]))
    assert isinstance(caught.value, NoUsableProvider)
    assert "AWS_ACCESS_KEY_ID" in caught.value.env_names
    assert "AWS_BEARER_TOKEN_BEDROCK" in caught.value.env_names


def test_credentials_chain(tmp_path: Path) -> None:
    shared = tmp_path / "credentials"
    shared.write_text(
        "[default]\naws_access_key_id = AKIDDEFAULT\naws_secret_access_key = s1\n\n"
        "[work]\naws_access_key_id = AKIDWORK\naws_secret_access_key = s2\n"
        "aws_session_token = t2\n",
        encoding="utf-8",
    )
    env = {"AWS_SHARED_CREDENTIALS_FILE": str(shared)}
    found = resolve_credentials(env)
    assert found is not None and found.access_key == "AKIDDEFAULT"
    work = resolve_credentials({**env, "AWS_PROFILE": "work"})
    assert work is not None and (work.access_key, work.session_token) == ("AKIDWORK", "t2")
    assert resolve_credentials({**env, "AWS_PROFILE": "nope"}) is None
    from_env = resolve_credentials(
        {**env, "AWS_ACCESS_KEY_ID": "AKIDENV", "AWS_SECRET_ACCESS_KEY": "s3"}
    )
    assert from_env is not None and from_env.source == "environment"
    assert "s3" not in repr(from_env)


def test_credentials_come_from_the_scope(tmp_path: Path) -> None:
    env = {
        "AWS_ACCESS_KEY_ID": "AKIDSCOPE",
        "AWS_SECRET_ACCESS_KEY": "scope" + "-secret",
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "none"),
    }
    with use_scope(make_scope(tmp_path, env=env)):
        request = BedrockTransport().build_request(route(), ChatRequest(model=MODEL, messages=[]))
    assert "Credential=AKIDSCOPE/" in request.headers["authorization"]


def test_bearer_token_paths() -> None:
    token = "bedrock" + "-api-key-0000"
    keyed = signing_transport().build_request(
        route(key=token), ChatRequest(model=MODEL, messages=[])
    )
    assert keyed.headers["authorization"] == f"Bearer {token}"
    from_env = BedrockTransport(env={"AWS_BEARER_TOKEN_BEDROCK": token})
    request = from_env.build_request(route(), ChatRequest(model=MODEL, messages=[]))
    assert request.headers["authorization"] == f"Bearer {token}"
    # a key read from the generic OpenAI variable is never sent to AWS
    foreign = signing_transport().build_request(
        route(key=token, key_env="OPENAI_API_KEY"), ChatRequest(model=MODEL, messages=[])
    )
    assert foreign.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")


@pytest.mark.parametrize(
    ("host", "env", "expected"),
    [
        ("bedrock-runtime.eu-central-1.amazonaws.com", {}, "eu-central-1"),
        ("bedrock-runtime-fips.us-east-1.amazonaws.com", {}, "us-east-1"),
        ("proxy.example", {"AWS_REGION": "ap-south-1"}, "ap-south-1"),
        ("proxy.example", {"AWS_DEFAULT_REGION": "eu-west-3"}, "eu-west-3"),
        ("proxy.example", {}, "us-east-1"),
    ],
)
def test_region(host: str, env: dict[str, str], expected: str) -> None:
    assert aws_sigv4.resolve_region(host, env) == expected
    assert region_from_host("bedrock-runtime.us-west-2.amazonaws.com.evil.example") is None


# -- event stream framing --------------------------------------------------------------------------


def test_decoder_handles_every_split_of_the_capture() -> None:
    data = b"".join(capture_chunks(load_capture("bedrock/tool_call")))
    whole = EventStreamDecoder().feed(data)
    assert [m.header(":event-type") for m in whole][:2] == ["messageStart", "contentBlockDelta"]
    for size in (1, 2, 3, 7, 16, 64):
        decoder = EventStreamDecoder()
        got: list[EventMessage] = []
        for start in range(0, len(data), size):
            got.extend(decoder.feed(data[start : start + size]))
        assert got == whole and decoder.pending == 0


def test_damaged_frames_are_refused() -> None:
    frame = bytearray(event("messageStop", {"stopReason": "end_turn"}))
    bad_message = bytes(frame[:-1]) + bytes([frame[-1] ^ 0xFF])
    with pytest.raises(ProviderStreamError, match="message checksum"):
        EventStreamDecoder().feed(bad_message)
    bad_prelude = bytes([frame[0] ^ 0x01]) + bytes(frame[1:])
    with pytest.raises(ProviderStreamError, match="prelude checksum"):
        EventStreamDecoder().feed(bad_prelude)
    huge = struct.pack(">II", 32 * 1024 * 1024, 0)
    import zlib

    with pytest.raises(ProviderStreamError, match="out of range"):
        EventStreamDecoder().feed(huge + struct.pack(">I", zlib.crc32(huge)))


def test_truncated_stream_is_incomplete() -> None:
    frame = event("messageStop", {"stopReason": "end_turn"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=frame[:-3])

    async def main() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async for _ in iter_call(
                client, signing_transport(), route(), ChatRequest(model=MODEL, messages=[])
            ):
                pass

    with pytest.raises(ProviderStreamError) as caught:
        asyncio.run(main())
    assert classify(caught.value).reason == "incomplete_response"


def test_exception_frame_is_classified() -> None:
    headers = {
        ":exception-type": "throttlingException",
        ":content-type": "application/json",
        ":message-type": "exception",
    }
    frame = encode_message(headers, b'{"message":"Too many requests, please wait."}')
    with pytest.raises(ProviderStreamError) as caught:
        parse([event("messageStart", {"role": "assistant"}), frame])
    assert "throttlingException" in str(caught.value)
    assert classify(caught.value).reason == "rate_limit"


# -- conversion ------------------------------------------------------------------------------------


def test_conversion_golden() -> None:
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "assistant", "content": "hello earlier"},
        {"role": "user", "content": "  "},
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_details": [{"type": "thinking", "thinking": "plan", "signature": "sig1"}],
            "tool_calls": [
                {"id": "call:1", "function": {"name": "read", "arguments": '{"p": "a"}'}},
                {"id": "call_2", "function": {"name": "list", "arguments": "not json"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call:1", "content": "text"},
        {"role": "tool", "tool_call_id": "call_2", "content": "", "is_error": True},
    ]
    system, turns = convert_messages(messages, model=MODEL)
    assert system == [{"text": "be brief"}]
    assert turns == [
        {"role": "user", "content": [{"text": BLANK_TEXT}]},
        {"role": "assistant", "content": [{"text": "hello earlier"}]},
        {"role": "user", "content": [{"text": BLANK_TEXT}, {"text": "read it"}]},
        {
            "role": "assistant",
            "content": [
                {"reasoningContent": {"reasoningText": {"text": "plan", "signature": "sig1"}}},
                {"toolUse": {"toolUseId": "call_1", "name": "read", "input": {"p": "a"}}},
                {"toolUse": {"toolUseId": "call_2", "name": "list", "input": {"_raw": "not json"}}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"toolResult": {"toolUseId": "call_1", "content": [{"text": "text"}]}},
                {
                    "toolResult": {
                        "toolUseId": "call_2",
                        "content": [{"text": BLANK_TEXT}],
                        "status": "error",
                    }
                },
            ],
        },
    ]


def test_thinking_not_replayed_to_other_models() -> None:
    _, turns = convert_messages(
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "a",
                "reasoning_details": [{"type": "thinking", "signature": "s"}],
            },
        ],
        model="amazon.nova-pro-v1:0",
    )
    assert turns[1] == {"role": "assistant", "content": [{"text": "a"}]}


def test_request_body_tools_and_reasoning() -> None:
    req = ChatRequest(
        model=MODEL,
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {"type": "function", "function": {"name": "t", "description": "d", "parameters": {}}}
        ],
        tool_choice="required",
        max_tokens=1000,
        reasoning_effort="high",
    )
    body = body_of(signing_transport().build_request(route(), req))
    assert body["toolConfig"] == {
        "tools": [{"toolSpec": {"name": "t", "description": "d", "inputSchema": {"json": {}}}}],
        "toolChoice": {"any": {}},
    }
    assert body["additionalModelRequestFields"] == {
        "thinking": {"type": "enabled", "budget_tokens": 16000}
    }
    assert body["inferenceConfig"] == {"maxTokens": 20096, "temperature": 1}
    nova = body_of(
        signing_transport().build_request(
            route(model="amazon.nova-pro-v1:0"),
            ChatRequest(model="amazon.nova-pro-v1:0", messages=[], reasoning_effort="high"),
        )
    )
    assert "additionalModelRequestFields" not in nova and "inferenceConfig" not in nova


def test_non_streaming_url() -> None:
    request = signing_transport().build_request(
        route(), ChatRequest(model=MODEL, messages=[], stream=False)
    )
    assert request.url.raw_path.decode().endswith("/converse")
    assert request.headers["accept"] == "application/json"


# -- stream ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "finish"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("tool_use", "tool_calls"),
        ("max_tokens", "length"),
        ("guardrail_intervened", "content_filter"),
        ("content_filtered", "content_filter"),
        ("something_new", "stop"),
    ],
)
def test_stop_reasons(raw: str, finish: str) -> None:
    events = parse([event("messageStop", {"stopReason": raw})])
    assert events[-1] == Finish(finish)  # type: ignore[arg-type]
    payload = {"output": {"message": {"content": []}}, "stopReason": raw}
    assert BedrockTransport().parse_response(payload).finish_reason == finish


def test_stream_without_message_stop_is_incomplete() -> None:
    with pytest.raises(ProviderStreamError) as caught:
        parse([event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "x"}})])
    assert classify(caught.value).reason == "incomplete_response"


def test_reasoning_stream_becomes_a_signed_detail() -> None:
    chunks = [
        event(
            "contentBlockDelta",
            {"contentBlockIndex": 0, "delta": {"reasoningContent": {"text": "hm"}}},
        ),
        event(
            "contentBlockDelta",
            {"contentBlockIndex": 0, "delta": {"reasoningContent": {"signature": "abc"}}},
        ),
        event("contentBlockStop", {"contentBlockIndex": 0}),
        event("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"text": "answer"}}),
        event("messageStop", {"stopReason": "end_turn"}),
        event(
            "metadata",
            {"usage": {"inputTokens": 10, "outputTokens": 3, "cacheReadInputTokens": 90}},
        ),
    ]
    events = parse(chunks)
    assert ReasoningDetail({"type": "thinking", "thinking": "hm", "signature": "abc"}) in events
    response = assemble(events)
    assert (response.content, response.reasoning) == ("answer", "hm")
    assert response.usage is not None
    assert (response.usage.prompt_tokens, response.usage.cache_read_tokens) == (100, 90)


def test_stream_tool_use_round_trip() -> None:
    capture = load_capture("bedrock/tool_call")
    req = chat_request_from(capture)

    async def main() -> list[StreamEvent]:
        transport = signing_transport()
        async with httpx.AsyncClient(transport=ReplayTransport(capture)) as client:
            capture_route = RuntimeRoute(
                "bedrock", req.model, "bedrock", capture["meta"]["base_url"]
            )
            return [e async for e in iter_call(client, transport, capture_route, req)]

    response = assemble(run(main()))
    assert response.content == "I'll look up the weather in Oslo."
    (call,) = response.tool_calls
    assert (call.name, json.loads(call.arguments)) == ("get_weather", {"city": "Oslo"})
    assert response.finish_reason == "tool_calls"
    follow_up = [
        *req.messages,
        {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [
                {"id": call.id, "function": {"name": call.name, "arguments": call.arguments}}
            ],
        },
        {"role": "tool", "tool_call_id": call.id, "content": "sunny, 12 C"},
    ]
    _, turns = convert_messages(follow_up, model=req.model)
    assert turns[1]["content"][1] == {
        "toolUse": {"toolUseId": call.id, "name": "get_weather", "input": {"city": "Oslo"}}
    }
    assert turns[2]["content"] == [
        {"toolResult": {"toolUseId": call.id, "content": [{"text": "sunny, 12 C"}]}}
    ]


def test_json_reply() -> None:
    payload = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": "t", "signature": "s"}}},
                    {"text": "hi"},
                    {"toolUse": {"toolUseId": "tu1", "name": "x", "input": {"a": 1}}},
                ],
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 3, "outputTokens": 4, "cacheWriteInputTokens": 7},
        "metrics": {"latencyMs": 250},
    }
    response = BedrockTransport().parse_response(payload)
    assert response.content == "hi" and response.finish_reason == "tool_calls"
    assert response.tool_calls[0].arguments == '{"a": 1}'
    assert response.reasoning_details == ({"type": "thinking", "thinking": "t", "signature": "s"},)
    assert response.usage is not None and response.usage.prompt_tokens == 10
    assert response.provider_data == {"latency_ms": 250}


def test_stream_denied_switches_to_non_streaming() -> None:
    denial = json.dumps(
        {
            "message": "User: arn:aws:iam::123456789012:user/dev is not authorized to perform: "
            "bedrock:InvokeModelWithResponseStream on resource: arn:aws:bedrock:us-east-1::"
            "foundation-model/x"
        }
    )
    clock = FakeClock(START)
    server = FakeModelServer(
        Script((Turn((Fail(403, denial),)), Turn((Text(("fine",)), FinishStep("stop"))))),
        clock=clock,
    )
    fake = fake_route(server, "bedrock")
    flags = StreamFlags()

    async def main() -> CallResult:
        async with server.client() as client:
            task = asyncio.ensure_future(
                stream_call(
                    client,
                    get_transport("bedrock"),
                    fake,
                    ChatRequest(model=fake.model, messages=[{"role": "user", "content": "x"}]),
                    policy=TimeoutPolicy(),
                    clock=clock,
                    on_event=lambda e: None,
                    route_epoch=lambda: 0,
                    flags=flags,
                    streak=StaleStreak(),
                )
            )
            for _ in range(1000):
                await clock.run_until_idle()
                if task.done():
                    break
                clock.advance(1.0)
            result = task.result()
            assert isinstance(result, CallResult)
            return result

    result = asyncio.run(main())
    assert result.response.content == "fine" and not result.streamed
    assert flags.streaming is False
    paths = [r.path for r in server.model_requests]
    assert paths[0].endswith("/converse-stream") and paths[1].endswith("/converse")


# -- context windows -------------------------------------------------------------------------------


def test_context_table() -> None:
    from indus.providers.transports.bedrock import context_length as table

    assert table("us.anthropic.claude-haiku-4-5-20251001-v1:0") == 200_000
    assert table("amazon.nova-pro-v1:0") == 300_000
    assert table("amazon.nova-micro-v1:0") == 128_000
    assert table("unknown.vendor-model") is None


def test_window_resolution_uses_the_table_for_bedrock_routes(tmp_path: Path) -> None:
    scope = make_scope(tmp_path)
    nova = RuntimeRoute("bedrock", "amazon.nova-pro-v1:0", "bedrock", BASE)
    window = run(context_length.find_window(nova, scope=scope))
    assert (window.tokens, window.source) == (300_000, "family")
