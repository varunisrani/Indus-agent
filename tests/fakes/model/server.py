"""A scriptable fake model API behind ``httpx.MockTransport``.

``FakeModelServer(script, clock=...)`` answers:

- ``POST …/chat/completions`` in the Chat Completions wire format (SSE, or JSON when the request
  says ``"stream": false``);
- ``POST …/v1/messages`` in the Anthropic Messages wire format (same rule);
- ``POST …/responses`` in the OpenAI Responses wire format (same rule);
- ``POST …/model/<id>/converse-stream`` (binary event stream) and ``…/converse`` (JSON) in the
  Bedrock Converse wire format;
- ``POST …/models/<id>:streamGenerateContent`` (SSE) and ``…:generateContent`` (JSON) in the
  Gemini wire format;
- ``GET …/models`` with a model list.

Every POST consumes the next ``Turn`` of the script; a request past the end raises
``FakeScriptExhausted`` naming the script. ``Stall`` waits on the given clock (a ``FakeClock`` in
tests), so stale detection is testable without real time. Each request is logged in
``server.requests`` with its parsed JSON body. No socket is ever opened.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from indus.providers.route import RuntimeRoute, Secret
from tests.fakes.clock import checkpoint
from tests.fakes.model import (
    Disconnect,
    Fail,
    Script,
    ScriptError,
    Stall,
    Turn,
    wire_anthropic,
    wire_bedrock,
    wire_chat,
    wire_gemini,
    wire_responses,
)

if TYPE_CHECKING:
    import respx

    from indus.foundation.clock import Clock

FAKE_HOST = "fake.invalid"
CHAT_BASE_URL = f"https://{FAKE_HOST}/v1"
ANTHROPIC_BASE_URL = f"https://{FAKE_HOST}"
BEDROCK_BASE_URL = f"https://{FAKE_HOST}"
GEMINI_BASE_URL = f"https://{FAKE_HOST}/v1beta"
BASE_URLS = {
    "anthropic_messages": ANTHROPIC_BASE_URL,
    "bedrock": BEDROCK_BASE_URL,
    "gemini": GEMINI_BASE_URL,
}
WIRES: dict[str, Any] = {
    "chat": wire_chat,
    "anthropic": wire_anthropic,
    "responses": wire_responses,
    "bedrock": wire_bedrock,
    "gemini": wire_gemini,
}
CONTENT_TYPES = {"bedrock": "application/vnd.amazon.eventstream"}
DEFAULT_MODEL = "fake-model-1"


class FakeScriptExhausted(AssertionError):
    """The code under test sent more model requests than the script has turns."""


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: Any
    turn: int | None  # 1-based turn served, None for GET /models
    at: float | None = None  # the server clock's monotonic time when it arrived
    timeout: Mapping[str, Any] | None = None  # the httpx timeout extension the client sent

    @property
    def stream(self) -> bool:
        return isinstance(self.body, dict) and bool(self.body.get("stream"))


@dataclass
class StreamRecord:
    """One streamed body: when it opened and closed (perf counter seconds), how it ended."""

    turn: int
    wire: str
    opened_at: float
    closed_at: float | None = None
    frames_sent: int = 0
    completed: bool = False


@dataclass
class FakeModelServer:
    script: Script
    clock: Clock | None = None
    model: str = DEFAULT_MODEL
    models: Sequence[Mapping[str, Any]] | None = None
    name: str | None = None
    # Give way to the loop before every Nth frame is handed over (0 = never); trials use 1.
    yield_every: int = 0
    # Called with each frame just before it is handed to the client (latency trials stamp it).
    on_frame: Callable[[int, bytes], None] | None = None
    requests: list[RecordedRequest] = field(default_factory=list)
    streams: list[StreamRecord] = field(default_factory=list)
    _next_turn: int = 0

    def __post_init__(self) -> None:
        self.name = self.name or self.script.name or "<inline script>"
        needs_clock = False
        for t_no, turn in enumerate(self.script.turns, start=1):
            for s_no, step in enumerate(turn.steps, start=1):
                if isinstance(step, Fail) and s_no != 1:
                    raise ScriptError(
                        f"{self.name}: turn {t_no}, step {s_no}: 'fail' must be a turn's first step"
                    )
                needs_clock = needs_clock or isinstance(step, Stall)
        if needs_clock and self.clock is None:
            raise ScriptError(f"{self.name}: the script stalls, so pass clock=")
        self.transport = httpx.MockTransport(self._handle)

    # -- helpers for tests ---------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return CHAT_BASE_URL

    def base_url_for(self, api_mode: str) -> str:
        return BASE_URLS.get(api_mode, CHAT_BASE_URL)

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, **kwargs)

    @property
    def model_requests(self) -> list[RecordedRequest]:
        return [r for r in self.requests if r.method == "POST"]

    @property
    def turns_left(self) -> int:
        return len(self.script.turns) - self._next_turn

    # -- request handling ----------------------------------------------------------------------

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        raw = await request.aread()
        try:
            body: Any = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", "replace")
        path = request.url.path
        headers = {k.lower(): v for k, v in request.headers.items()}
        extra: dict[str, Any] = {
            "at": self.clock.monotonic() if self.clock is not None else None,
            "timeout": request.extensions.get("timeout"),
        }
        if request.method == "GET" and path.rstrip("/").endswith("/models"):
            self.requests.append(RecordedRequest("GET", path, headers, None, None, **extra))
            return httpx.Response(200, json=self._models_body())
        if request.method != "POST":
            self.requests.append(
                RecordedRequest(request.method, path, headers, body, None, **extra)
            )
            return httpx.Response(405, json={"error": {"message": "method not allowed"}})
        stream_by_path: bool | None = None
        if path.endswith("/chat/completions"):
            wire = "chat"
        elif path.endswith("/messages"):
            wire = "anthropic"
        elif path.endswith("/responses"):
            wire = "responses"
        elif path.endswith("/converse-stream") or path.endswith("/converse"):
            wire, stream_by_path = "bedrock", path.endswith("/converse-stream")
        elif ":streamGenerateContent" in path or ":generateContent" in path:
            wire, stream_by_path = "gemini", ":streamGenerateContent" in path
        else:
            self.requests.append(RecordedRequest("POST", path, headers, body, None, **extra))
            return httpx.Response(404, json={"error": {"message": f"no route {path}"}})
        if self._next_turn >= len(self.script.turns):
            self.requests.append(RecordedRequest("POST", path, headers, body, None, **extra))
            raise FakeScriptExhausted(
                f"fake model script {self.name!r} has {len(self.script.turns)} turn(s) but got "
                f"model request #{len(self.model_requests)} ({request.method} {path})"
            )
        self._next_turn += 1
        turn_no = self._next_turn
        turn = self.script.turns[turn_no - 1]
        self.requests.append(RecordedRequest("POST", path, headers, body, turn_no, **extra))
        first = turn.steps[0]
        if isinstance(first, Fail):
            return _fail_response(first)
        if stream_by_path is not None:
            stream = stream_by_path
        else:
            stream = isinstance(body, dict) and bool(body.get("stream"))
        model = str(body.get("model") or self.model) if isinstance(body, dict) else self.model
        module = WIRES[wire]
        if not stream:
            return await self._json_answer(turn, turn_no, model, module, request)
        actions = module.stream_actions(turn, turn_no=turn_no, model=model)
        record = StreamRecord(turn_no, wire, time.perf_counter())
        self.streams.append(record)
        content_type = CONTENT_TYPES.get(wire, "text/event-stream")
        return httpx.Response(
            200,
            headers={"content-type": content_type, "cache-control": "no-cache"},
            stream=_ScriptedStream(self._play(actions, record, request)),
        )

    async def _json_answer(
        self, turn: Turn, turn_no: int, model: str, module: Any, request: httpx.Request
    ) -> httpx.Response:
        for step in turn.steps:
            if isinstance(step, Stall):
                await self._sleep(step.seconds)
            elif isinstance(step, Disconnect):
                raise httpx.RemoteProtocolError(
                    "server disconnected without sending a response (fake)", request=request
                )
        return httpx.Response(200, json=module.json_body(turn, turn_no=turn_no, model=model))

    async def _play(
        self, actions: list[wire_chat.Action], record: StreamRecord, request: httpx.Request
    ) -> AsyncIterator[bytes]:
        try:
            for action in actions:
                if isinstance(action, Stall):
                    await self._sleep(action.seconds)
                    continue
                if isinstance(action, Disconnect):
                    raise httpx.RemoteProtocolError(
                        "peer closed connection without sending complete message body (fake)",
                        request=request,
                    )
                if self.on_frame is not None:
                    self.on_frame(record.turn, action)
                record.frames_sent += 1
                if self.yield_every and record.frames_sent % self.yield_every == 0:
                    await checkpoint()  # the frame "arrived"; the reader runs when the loop can
                yield action
            record.completed = True
        finally:
            record.closed_at = time.perf_counter()

    async def _sleep(self, seconds: float) -> None:
        assert self.clock is not None
        await self.clock.sleep(seconds)

    def _models_body(self) -> dict[str, Any]:
        entries = (
            [dict(m) for m in self.models]
            if self.models is not None
            else [{"id": self.model, "object": "model", "owned_by": "indus-fake"}]
        )
        return {"object": "list", "data": entries}


class _ScriptedStream(httpx.AsyncByteStream):
    """A body whose ``aclose`` really closes the generator, so closes are observable."""

    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._source:
            yield chunk

    async def aclose(self) -> None:
        aclose = getattr(self._source, "aclose", None)
        if aclose is not None:
            await aclose()


def _fail_response(step: Fail) -> httpx.Response:
    headers = dict(step.headers)
    if not any(k.lower() == "content-type" for k in headers):
        try:
            json.loads(step.body)
        except ValueError:
            headers["content-type"] = "text/plain; charset=utf-8"
        else:
            headers["content-type"] = "application/json"
    return httpx.Response(step.status, headers=headers, content=step.body.encode("utf-8"))


# -- routes and one-off mocks --------------------------------------------------------------------

FAKE_KEY = "test-key-" + "0" * 12


def fake_route(server: FakeModelServer, api_mode: str = "chat_completions") -> RuntimeRoute:
    """A ``RuntimeRoute`` aimed at the fake server with a fake key on the ``fake.invalid`` host."""
    return RuntimeRoute(
        provider="fake",
        model=server.model,
        api_mode=api_mode,
        base_url=server.base_url_for(api_mode),
        api_key=Secret(FAKE_KEY),
        source="test",
        headers={},
    )


def one_off_router(base_url: str = CHAT_BASE_URL) -> respx.MockRouter:
    """A respx router for single canned answers (``router.get("/models").respond(...)``).

    Use ``httpx.AsyncClient(transport=httpx.MockTransport(router.async_handler))`` so no real
    transport is patched globally.
    """
    import respx

    return respx.MockRouter(base_url=base_url, assert_all_called=False, assert_all_mocked=True)


__all__ = [
    "ANTHROPIC_BASE_URL",
    "BEDROCK_BASE_URL",
    "CHAT_BASE_URL",
    "GEMINI_BASE_URL",
    "FAKE_HOST",
    "FAKE_KEY",
    "FakeModelServer",
    "FakeScriptExhausted",
    "RecordedRequest",
    "StreamRecord",
    "fake_route",
    "one_off_router",
]
