"""Recorded captures, the replay transport and the capture recorder (P03-11)."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

from indus.foundation.redact import redact_text
from indus.providers.route import RuntimeRoute
from indus.providers.transports import get_transport
from tests.fakes.model.replay import (
    ReplayMismatch,
    ReplayTransport,
    all_captures,
    capture_chunks,
    chat_request_from,
    load_capture,
)
from tests.unit.providers._support import run

ROOT = Path(__file__).resolve().parents[3]


def _recorder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "record_capture", ROOT / "scripts" / "dev" / "record_capture.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REC = _recorder()


def route_for(capture: dict[str, Any]) -> RuntimeRoute:
    meta = capture["meta"]
    return RuntimeRoute(
        provider=meta["provider"],
        model=meta["model"],
        api_mode=meta["api_mode"],
        base_url=meta["base_url"],
        api_key=None,
    )


def test_captures_exist_and_validate() -> None:
    paths = all_captures()
    names = {f"{p.parent.name}/{p.stem}" for p in paths}
    modes = ("chat_completions", "anthropic_messages", "responses", "bedrock", "gemini")
    assert names == {f"{mode}/{scenario}" for mode in modes for scenario in ("text", "tool_call")}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        capture = json.loads(text)
        meta = capture["meta"]
        for key in ("recorded_at", "provider", "model", "doc_url", "doc_date", "indus_version"):
            assert meta[key], (path, key)
        assert meta["api_mode"] == path.parent.name and meta["scenario"] == path.stem
        assert set(capture["request"]["headers"]) <= set(REC.REQUEST_HEADER_ALLOW)
        assert set(capture["response"]["headers"]) <= set(REC.RESPONSE_HEADER_ALLOW)
        assert redact_text(text) == text  # nothing credential-shaped anywhere
        assert all(chunk for chunk in capture_chunks(capture))


@pytest.mark.parametrize("name", ["chat_completions/text", "anthropic_messages/tool_call"])
def test_replay_serves_identical_bytes_in_same_chunks(name: str) -> None:
    capture = load_capture(name)
    transport = get_transport(capture["meta"]["api_mode"])
    request = transport.build_request(route_for(capture), chat_request_from(capture))
    replay = ReplayTransport(capture)

    async def main() -> list[bytes]:
        async with httpx.AsyncClient(transport=replay) as client:
            response = await client.send(request, stream=True)
            try:
                return [chunk async for chunk in response.aiter_raw()]
            finally:
                await response.aclose()

    assert run(main()) == capture_chunks(capture)
    assert len(replay.requests) == 1 and replay.streams[0].closed


def test_changed_request_body_fails_with_a_diff() -> None:
    capture = load_capture("chat_completions/text")
    req = chat_request_from(capture)
    changed = type(req)(**{**req.__dict__, "max_tokens": 999})
    request = get_transport("chat_completions").build_request(route_for(capture), changed)

    async def main() -> None:
        async with httpx.AsyncClient(transport=ReplayTransport(capture)) as client:
            await client.send(request)

    with pytest.raises(ReplayMismatch) as caught:
        run(main())
    message = str(caught.value)
    assert "request body differs" in message and '"max_tokens": 999' in message


def test_volatile_fields_are_ignored() -> None:
    capture = load_capture("anthropic_messages/text")
    req = chat_request_from(capture)
    changed = type(req)(**{**req.__dict__, "extra_body": {"metadata": {"user_id": "u-1"}}})
    request = get_transport("anthropic_messages").build_request(route_for(capture), changed)

    async def main() -> int:
        async with httpx.AsyncClient(transport=ReplayTransport(capture)) as client:
            return (await client.send(request)).status_code

    assert run(main()) == 200


# -- recorder ----------------------------------------------------------------------------------

KEY = "test-key-" + "rec0rd3r" + "-0123456789"


def _record_against(body: bytes, headers: dict[str, str]) -> dict[str, Any]:
    async def stream() -> AsyncIterator[bytes]:
        yield body[:10]
        yield body[10:]

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return httpx.Response(200, headers=headers, content=stream())

    request = httpx.Request(
        "POST",
        "https://api.fake.invalid/v1/chat/completions?trace=1",
        headers={
            "authorization": f"Bearer {KEY}",
            "content-type": "application/json",
            "cookie": "session=abc",
            "openai-organization": "org-123",
        },
        content=b'{"model": "m", "messages": []}',
    )

    async def main() -> tuple[int, dict[str, str], list[bytes]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            return await REC.exchange(client, request)

    status, got_headers, chunks = run(main())
    return REC.build_capture(
        meta={"provider": "fake"},
        request=request,
        status=status,
        headers=got_headers,
        chunks=chunks,
    )


def test_recorder_drops_key_bearing_headers() -> None:
    capture = _record_against(
        b'data: {"choices": []}\n\ndata: [DONE]\n\n',
        {
            "content-type": "text/event-stream",
            "x-echo-key": KEY,
            "x-request-id": "req_1",
            "set-cookie": "a=b",
        },
    )
    text = REC.render_capture(capture, [KEY])
    assert KEY not in text
    assert capture["request"]["url"] == "https://api.fake.invalid/v1/chat/completions"
    assert capture["request"]["headers"] == {"content-type": "application/json"}
    assert capture["response"]["headers"] == {"content-type": "text/event-stream"}


def test_recorder_refuses_when_the_body_echoes_a_secret() -> None:
    capture = _record_against(
        f'data: {{"error": "bad key {KEY}"}}\n\n'.encode(), {"content-type": "text/event-stream"}
    )
    with pytest.raises(REC.CaptureRefused) as caught:
        REC.render_capture(capture, [KEY])
    assert KEY not in str(caught.value)


def test_secrets_for_collects_route_store_and_env_values() -> None:
    store = SimpleNamespace(known_values=lambda: ["stored-value-1234"])
    scope = SimpleNamespace(
        secrets=store, env={"SOME_API_KEY": "env-value-5678", "PATH": "/usr/bin"}
    )
    found = REC.secrets_for(scope, KEY)
    assert {KEY, "stored-value-1234", "env-value-5678"} <= set(found)
    assert "/usr/bin" not in found
