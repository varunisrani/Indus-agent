"""P15-30: the API server's OpenAI-compatible routes against the fake model (no socket)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from functools import cache
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest

from indus.foundation.scope import Scope
from indus.gateway.http.api_server import KEEPALIVE_S, MAX_REQUEST_BYTES, SESSION_HEADER
from indus.gateway.http.openai_wire import MAX_CONTENT_ITEMS, MAX_IMAGE_DATA_URL, MAX_TEXT_PART
from indus.tools.builtin import load_builtins
from indus.tools.registry import ToolRegistry
from tests.contract.gateway._http_kit import ApiKit, breathe, build_api, sse_events, until, within
from tests.fakes.clock import FakeClock
from tests.fakes.model import Finish, Stall, Text, Turn, text_turn, tool_batch

SCHEMA = (
    Path(__file__).parents[2] / "fixtures" / "gateway" / "openai_schema" / "openai_trimmed.json"
)


@cache
def _defs() -> dict[str, Any]:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]


def conforms(name: str, instance: object) -> None:
    schema = {"$ref": f"#/$defs/{name}", "$defs": _defs()}
    jsonschema.Draft202012Validator(schema).validate(instance)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def chat(text: Any = "hello", **extra: Any) -> dict[str, Any]:
    return {"model": "indus-agent", "messages": [{"role": "user", "content": text}], **extra}


async def _kit(scope: Scope, clock: FakeClock, tmp_path: Path, *turns: Turn, **kw: Any) -> ApiKit:
    return await build_api(scope, clock, tmp_path, *turns, **kw)


# -- auth, limits, headers ----------------------------------------------------------------------

ROUTES = [
    ("GET", "/health"),
    ("GET", "/v1/health"),
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/responses"),
    ("GET", "/v1/responses/resp_x"),
    ("POST", "/v1/runs"),
    ("GET", "/v1/runs/run_x"),
    ("GET", "/api/sessions"),
    ("GET", "/api/jobs"),
    ("POST", "/api/cron/fire"),
    ("GET", "/no/such/route"),
]


def test_every_route_needs_a_key_on_loopback_too(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def main() -> None:
        kit = await _kit(scope, fake_clock, tmp_path)
        try:
            for key in (None, "wrong-" + "k" * 30, ""):
                async with kit.client(key=key) as client:
                    for method, path in ROUTES:
                        answer = await client.request(method, path, json={})
                        assert answer.status_code == 401, (method, path, key)
                        assert answer.json()["error"]["code"] == "invalid_api_key"
                        assert answer.headers["x-content-type-options"] == "nosniff"
            async with kit.client() as client:
                assert (await client.get("/health")).status_code == 200
                assert (await client.get("/v1/health")).json()["status"] == "ok"
                assert (await client.get("/no/such/route")).status_code == 404
            async with kit.client(headers={"Authorization": "Basic " + kit.key}, key=None) as c:
                assert (await c.get("/health")).status_code == 401
        finally:
            await kit.close()

    with caplog.at_level(logging.DEBUG):
        run(main())
    assert "wrong-" not in caplog.text


def test_a_revoked_key_stops_working_at_once(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await _kit(scope, fake_clock, tmp_path)
        try:
            info, second = kit.ctx.keys.create("other")
            async with kit.client(key=second) as client:
                assert (await client.get("/health")).status_code == 200
                assert kit.ctx.keys.revoke(info.key_id)
                assert (await client.get("/health")).status_code == 401
        finally:
            await kit.close()

    run(main())


def test_a_body_over_10_mib_is_refused_with_413(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await _kit(scope, fake_clock, tmp_path)
        try:
            big = b"x" * (MAX_REQUEST_BYTES + 1)
            async with kit.client() as client:
                answer = await client.post("/v1/chat/completions", content=big)
                assert answer.status_code == 413
                assert answer.json()["error"]["code"] == "body_too_large"

                async def chunks() -> AsyncIterator[bytes]:
                    for start in range(0, len(big), 1 << 20):
                        yield big[start : start + (1 << 20)]

                answer = await client.post("/v1/chat/completions", content=chunks())
                assert answer.status_code == 413
                assert kit.server.model_requests == []
        finally:
            await kit.close()

    run(main())


def test_content_limits(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        kit = await _kit(scope, fake_clock, tmp_path, text_turn("ok"))
        try:
            async with kit.client() as client:
                many = [{"type": "text", "text": "a"}] * (MAX_CONTENT_ITEMS + 1)
                answer = await client.post("/v1/chat/completions", json=chat(many))
                assert answer.status_code == 400
                assert answer.json()["error"]["code"] == "too_many_content_parts"
                head = "data:image/png;base64,"
                image = head + "A" * (MAX_IMAGE_DATA_URL + 1 - len(head))
                parts = [{"type": "image_url", "image_url": {"url": image}}]
                answer = await client.post("/v1/chat/completions", json=chat(parts))
                assert answer.status_code == 413
                assert answer.json()["error"]["code"] == "image_too_large"
                for bad in ([{"type": "file", "file": {}}], [{"type": "audio"}]):
                    answer = await client.post("/v1/chat/completions", json=chat(bad))
                    assert answer.status_code == 400
                answer = await client.post("/v1/chat/completions", content=b"{not json")
                assert answer.status_code == 400
                assert (
                    await client.post("/v1/chat/completions", json={"messages": []})
                ).status_code == 400
                long_part = "y" * (MAX_TEXT_PART + 500)
                answer = await client.post("/v1/chat/completions", json=chat(long_part))
                assert answer.status_code == 200
            assert kit.model_texts()[-1].count("y") == MAX_TEXT_PART
        finally:
            await kit.close()

    run(main())


def test_cors_is_off_by_default_and_follows_the_allow_list(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await _kit(scope, fake_clock, tmp_path)
        try:
            async with kit.client(headers={"Origin": "https://evil.example"}) as client:
                assert (await client.get("/health")).status_code == 403
            kit.knobs["cors_origins"] = ("https://app.example",)
            async with kit.client(headers={"Origin": "https://app.example"}) as client:
                answer = await client.get("/health")
                assert answer.status_code == 200
                assert answer.headers["access-control-allow-origin"] == "https://app.example"
            async with kit.client(key=None, headers={"Origin": "https://app.example"}) as client:
                answer = await client.options("/v1/chat/completions")
                assert answer.status_code == 204
                assert "Authorization" in answer.headers["access-control-allow-headers"]
                assert (await client.get("/health")).status_code == 401
        finally:
            await kit.close()

    run(main())


# -- the wire shapes ----------------------------------------------------------------------------


def test_models_and_a_chat_completion_match_the_openai_schema(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await _kit(scope, fake_clock, tmp_path, text_turn("Hel", "lo there"))
        try:
            async with kit.client() as client:
                models = (await client.get("/v1/models")).json()
                conforms("ListModelsResponse", models)
                assert models["data"][0]["id"] == "indus-agent"
                answer = await within(client.post("/v1/chat/completions", json=chat("hi")))
            assert answer.status_code == 200
            body = answer.json()
            conforms("CreateChatCompletionResponse", body)
            assert body["choices"][0]["message"]["content"] == "Hello there"
            assert body["choices"][0]["finish_reason"] == "stop"
            assert answer.headers[SESSION_HEADER]
            assert kit.model_texts() == ["hi"]
        finally:
            await kit.close()

    run(main())


def test_streamed_chat_frames_match_the_chunk_schema(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await _kit(scope, fake_clock, tmp_path, text_turn("one ", "two ", "three"))
        try:
            async with kit.client() as client:
                answer = await within(
                    client.post("/v1/chat/completions", json=chat("count", stream="true"))
                )
            assert answer.status_code == 200
            assert answer.headers["content-type"].startswith("text/event-stream")
            frames = sse_events(answer.text)
            assert frames[-1]["data"] == "[DONE]"
            chunks = [f["data"] for f in frames[:-1] if "data" in f]
            for chunk in chunks:
                conforms("CreateChatCompletionStreamResponse", chunk)
            text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
            assert text == "one two three"
            assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
            assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
            assert len({c["id"] for c in chunks}) == 1
        finally:
            await kit.close()

    run(main())


def test_an_idle_stream_gets_a_keepalive_comment_every_10_s(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        stalled = Turn((Text(("first",)), Stall(25.0), Text((" last",)), Finish("stop")))
        kit = await _kit(scope, fake_clock, tmp_path, stalled)
        try:
            async with kit.client() as client:
                request = asyncio.ensure_future(
                    client.post("/v1/chat/completions", json=chat("wait", stream=True))
                )
                await until(lambda: kit.server.model_requests)
                for _ in range(26):
                    await breathe(0.01)
                    fake_clock.advance(1.0)
                answer = await within(request)
            frames = sse_events(answer.text)
            keepalives = [f for f in frames if f.get("comment") == "keepalive"]
            assert len(keepalives) >= 2  # 25 s of silence: at 10 s and 20 s
            text = "".join(
                f["data"]["choices"][0]["delta"].get("content") or ""
                for f in frames
                if isinstance(f.get("data"), dict)
            )
            assert text == "first last"
        finally:
            await kit.close()

    assert KEEPALIVE_S == 10.0
    run(main())


def test_responses_json_stream_and_lookup_match_the_schema(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await _kit(
            scope, fake_clock, tmp_path, text_turn("first answer"), text_turn("second", " answer")
        )
        try:
            async with kit.client() as client:
                answer = await within(client.post("/v1/responses", json={"input": "hi"}))
                assert answer.status_code == 200
                first = answer.json()
                conforms("Response", first)
                assert first["status"] == "completed"
                assert first["output"][0]["content"][0]["text"] == "first answer"
                stored = (await client.get(f"/v1/responses/{first['id']}")).json()
                assert stored == first
                body = {"input": "again", "previous_response_id": first["id"], "stream": True}
                streamed = await within(client.post("/v1/responses", json=body))
            frames = [f for f in sse_events(streamed.text) if "data" in f]
            names = {
                "response.created": "ResponseCreatedEvent",
                "response.in_progress": "ResponseInProgressEvent",
                "response.output_item.added": "ResponseOutputItemAddedEvent",
                "response.content_part.added": "ResponseContentPartAddedEvent",
                "response.output_text.delta": "ResponseTextDeltaEvent",
                "response.output_text.done": "ResponseTextDoneEvent",
                "response.content_part.done": "ResponseContentPartDoneEvent",
                "response.output_item.done": "ResponseOutputItemDoneEvent",
                "response.completed": "ResponseCompletedEvent",
            }
            for frame in frames:
                assert frame["event"] == frame["data"]["type"]
                conforms(names[frame["event"]], frame["data"])
            assert [f["data"]["sequence_number"] for f in frames] == list(range(len(frames)))
            assert frames[-1]["event"] == "response.completed"
            assert frames[-1]["data"]["response"]["output"][0]["content"][0]["text"] == (
                "second answer"
            )
            # previous_response_id resumed the first session: the model saw the first exchange.
            messages = kit.model_messages(-1)
            assert any(m.get("content") == "first answer" for m in messages)
            assert streamed.headers[SESSION_HEADER] == answer.headers[SESSION_HEADER]
        finally:
            await kit.close()

    run(main())


# -- sessions -----------------------------------------------------------------------------------


def test_the_session_header_resumes_and_no_header_starts_fresh(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await _kit(
            scope, fake_clock, tmp_path, text_turn("A1"), text_turn("A2"), text_turn("A3")
        )
        try:
            async with kit.client() as client:
                first = await within(client.post("/v1/chat/completions", json=chat("Q1")))
                sid = first.headers[SESSION_HEADER]
                second = await within(
                    client.post(
                        "/v1/chat/completions", json=chat("Q2"), headers={SESSION_HEADER: sid}
                    )
                )
                assert second.headers[SESSION_HEADER] == sid
                contents = [m.get("content") for m in kit.model_messages(-1)]
                assert "Q1" in contents and "A1" in contents and "Q2" in contents
                third = await within(client.post("/v1/chat/completions", json=chat("Q3")))
                assert third.headers[SESSION_HEADER] != sid
                assert "Q1" not in [m.get("content") for m in kit.model_messages(-1)]
                unknown = await client.post(
                    "/v1/chat/completions", json=chat("x"), headers={SESSION_HEADER: "s_nope"}
                )
                assert unknown.status_code == 404
                for bad in ("a" * 257, "bad id", "../etc"):
                    answer = await client.post(
                        "/v1/chat/completions", json=chat("x"), headers={SESSION_HEADER: bad}
                    )
                    assert answer.status_code == 400, bad
        finally:
            await kit.close()

    run(main())


def test_a_new_session_folds_the_request_history_into_its_first_message(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await _kit(scope, fake_clock, tmp_path, text_turn("ok"))
        try:
            pixel = base64.b64encode(b"\x89PNG fake").decode()
            body = {
                "messages": [
                    {"role": "system", "content": "Answer in French."},
                    {"role": "user", "content": "earlier question"},
                    {"role": "assistant", "content": "earlier answer"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "and now?"},
                            {"type": "image_url", "image_url": {"url": "https://x.example/a.png"}},
                            {"type": "input_image", "image_url": f"data:image/png;base64,{pixel}"},
                        ],
                    },
                ]
            }
            async with kit.client() as client:
                assert (await within(client.post("/v1/chat/completions", json=body))).is_success
            sent = kit.model_texts()[-1]
            assert "Instructions from the API client:\nAnswer in French." in sent
            assert "user: earlier question" in sent and "assistant: earlier answer" in sent
            assert "[image: https://x.example/a.png]" in sent
            saved = list((scope.home / "cache" / "api_server" / "images").iterdir())
            assert len(saved) == 1 and saved[0].read_bytes() == b"\x89PNG fake"
            assert f"[image: {saved[0]}]" in sent
        finally:
            await kit.close()

    run(main())


# -- R02.27: unattended API turns ---------------------------------------------------------------


def test_a_flagged_command_is_denied_at_once(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await _kit(
            scope, fake_clock, tmp_path, tool_batch(("flagged", {})), text_turn("could not")
        )
        requests: list[object] = []
        try:
            before = fake_clock.monotonic()
            async with kit.client() as client:
                answer = await within(client.post("/v1/chat/completions", json=chat("push")))
            assert answer.status_code == 200
            assert fake_clock.monotonic() == before  # nothing waited on an approval
            tool_rows = [m for m in kit.model_messages(-1) if m.get("role") == "tool"]
            assert tool_rows and "blocked (unattended_deny)" in str(tool_rows[0]["content"])
            sid = answer.headers[SESSION_HEADER]
            events = kit.service.events(sid)
            requests = [e for e in events if getattr(e, "type", "") == "server_request.cancelled"]
            assert requests == []
        finally:
            await kit.close()

    run(main())


def test_clarify_and_tts_are_not_in_an_api_sessions_tools(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        registry = ToolRegistry()
        load_builtins(registry)
        kit = await _kit(scope, fake_clock, tmp_path, registry=registry)
        try:
            sid = await kit.desk.open(None)
            names = set(kit.desk.tool_names(sid))
            assert names, "the API toolset resolved to nothing"
            assert "clarify" not in names and "text_to_speech" not in names
            assert "read_file" in names
        finally:
            await kit.close()

    run(main())


def test_more_turns_than_the_cap_get_429(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        stalled = Turn((Text(("slow",)), Stall(1000.0), Finish("stop")))
        kit = await _kit(scope, fake_clock, tmp_path, stalled, settings={"max_concurrent_runs": 1})
        try:
            async with kit.client() as client:
                first = asyncio.ensure_future(client.post("/v1/chat/completions", json=chat("one")))
                await until(lambda: kit.server.model_requests)
                second = await client.post("/v1/chat/completions", json=chat("two"))
                assert second.status_code == 429
                assert second.json()["error"]["type"] == "rate_limit_error"
                fake_clock.advance(1000.0)
                assert (await within(first)).status_code == 200
            assert kit.ctx.gate.active == 0
        finally:
            await kit.close()

    run(main())


def test_a_client_that_goes_away_interrupts_its_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        stalled = Turn((Text(("slow",)), Stall(1000.0), Finish("stop")))
        kit = await _kit(scope, fake_clock, tmp_path, stalled)
        try:
            async with kit.client() as client:
                request = asyncio.ensure_future(
                    client.post("/v1/chat/completions", json=chat("one"))
                )
                await until(lambda: kit.server.model_requests)
                request.cancel()
                with pytest.raises((asyncio.CancelledError, httpx.HTTPError)):
                    await request
            await until(lambda: kit.ctx.gate.active == 0 and kit.desk.active == 0)
        finally:
            await kit.close()

    run(main())


def test_an_idle_api_session_is_closed_and_can_still_be_resumed(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await _kit(
            scope,
            fake_clock,
            tmp_path,
            text_turn("A1"),
            text_turn("A2"),
            settings={"idle_close_seconds": 60.0},
        )
        try:
            async with kit.client() as client:
                first = await within(client.post("/v1/chat/completions", json=chat("Q1")))
                sid = first.headers[SESSION_HEADER]
                assert sid in kit.service.attached()
                await fake_clock.advance_and_settle(59.0)
                assert sid in kit.service.attached()
                await fake_clock.advance_and_settle(2.0)
                await until(lambda: sid not in kit.service.attached())
                again = await within(
                    client.post(
                        "/v1/chat/completions", json=chat("Q2"), headers={SESSION_HEADER: sid}
                    )
                )
                assert (
                    again.status_code == 200
                    and again.json()["choices"][0]["message"]["content"] == "A2"
                )
        finally:
            await kit.close()

    run(main())
