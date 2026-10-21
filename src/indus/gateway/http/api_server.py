"""The OpenAI-compatible API server inside the gateway daemon (P15-30).

``build_api_app(ctx)`` is the whole ASGI app: security headers, the CORS gate (off unless
``gateway.api_server.cors_origins`` lists origins), the bearer-key gate (every route, the
health checks included, loopback included: D-16.26), the length gate (10 MiB), then FastAPI with
the routes of this module, ``runs`` (P15-31) and ``admin_routes`` (P15-31).

Routes here: ``GET /health`` and ``/v1/health``, ``GET /v1/models``,
``POST /v1/chat/completions`` (JSON or SSE), ``POST /v1/responses`` (JSON or SSE) and
``GET /v1/responses/{id}``. A chat completion or a response is one ``unattended`` turn through
the surface's ``SessionDesk``: a flagged command is denied at once (R02.27), the session has the
``indus-api-server`` toolset (no clarify, no TTS). ``X-Indus-Session-Id`` resumes a stored session;
without it a new session is made and the request's system and earlier messages are folded into
its first message. Streams send ``: keepalive`` after 10 s of silence; a client that goes away
interrupts its turn. More turns than ``max_concurrent_runs`` (runs included) get 429.

With a browser-control broker (P24-47) the app also has ``POST /v1/browser-control/register``
and the controller WebSocket ``/v1/browser-control/ws`` (``gateway.browser_broker.routes``).

``ApiServer`` binds and serves the app (``serving.HttpServer``) and refuses to start while the
profile has no API key. ``front`` wraps the whole app before it is served: under multiplexing
(P24-04) the default profile's listener is a ``gateway.multiplex.ingress.SharedIngress`` around
it, outside this profile's bearer gate, so ``/p/<profile>/…`` reaches that profile's own app,
checked with that profile's own keys.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging
import re
import secrets
import urllib.parse
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from indus import __version__
from indus.cron.ports import CronBackend
from indus.foundation.clock import Clock
from indus.foundation.profiles import mkdir_in_profile
from indus.foundation.scope import Scope
from indus.gateway.config import gateway_section
from indus.gateway.http import openai_wire as wire
from indus.gateway.http.serving import (
    Asgi,
    BadRequest,
    BearerGate,
    BodyTooLarge,
    CorsGate,
    HttpServer,
    LengthGate,
    SecurityHeaders,
    bind_listener,
    read_json,
)
from indus.gateway.http.turns import CompletionLog, SessionDesk, Turn, TurnGate, TurnOutcome
from indus.protocol.events import MessageDelta, ReasoningDelta
from indus.protocol.methods import ProtocolError
from indus.safety.redaction import redact_output
from indus.state.api_keys import ApiKeyRing
from indus.state.registry import StoreHandle

__all__ = [
    "KEEPALIVE_S",
    "MAX_REQUEST_BYTES",
    "SESSION_HEADER",
    "ApiContext",
    "ApiServer",
    "ApiSettings",
    "ResponseStore",
    "build_api_app",
]

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 10 * 1024 * 1024
KEEPALIVE_S = 10.0
HEARTBEAT_S = 30.0
SESSION_HEADER = "X-Indus-Session-Id"
SESSION_HEADER_MAX = 256
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif",
              "image/webp": "webp", "image/bmp": "bmp"}  # fmt: skip
PLATFORM = "indus-agent"


@dataclass(frozen=True, slots=True)
class ApiSettings:
    host: str = "127.0.0.1"
    port: int = 8642
    cors_origins: tuple[str, ...] = ()
    max_concurrent_runs: int = 10
    model_name: str = "indus-agent"
    idle_close_seconds: float = 900.0

    @classmethod
    def from_scope(cls, scope: Scope) -> ApiSettings:
        section = gateway_section(scope).api_server
        return cls(
            host=section.host,
            port=section.port,
            cors_origins=tuple(section.cors_origins),
            max_concurrent_runs=section.max_concurrent_runs,
            model_name=section.model_name or "indus-agent",
            idle_close_seconds=section.idle_close_seconds,
        )


class ResponseStore:
    """Stored ``/v1/responses`` answers (``store: true``), newest ``limit`` kept, in memory."""

    def __init__(self, limit: int = 1_000) -> None:
        self.limit = limit
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def put(self, response: dict[str, Any], session_id: str) -> None:
        self._items[str(response["id"])] = {"response": response, "session_id": session_id}
        self._items.move_to_end(str(response["id"]))
        while len(self._items) > self.limit:
            self._items.popitem(last=False)

    def get(self, response_id: str) -> dict[str, Any] | None:
        return self._items.get(response_id)


@dataclass
class ApiContext:
    """What the API routes need; ``ApiServer`` (or a test) builds it."""

    scope: Scope
    desk: SessionDesk
    keys: ApiKeyRing
    settings: Callable[[], ApiSettings]
    clock: Clock
    store: StoreHandle | None = None
    completions: CompletionLog | None = None
    cron: Callable[[], CronBackend] | None = None
    gate: TurnGate = field(init=False)
    responses: ResponseStore = field(default_factory=ResponseStore)
    runs: Any = None  # runs.RunTable, set by build_api_app
    jobs: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])
    """Cron runs started with ``/api/jobs/{id}/run`` (cancelled and awaited at close)."""
    requests_served: int = 0
    browser_broker: Any = None
    """A ``gateway.browser_broker.BrowserBroker`` when ``browser.control_enabled`` is on: the
    register route and the controller WebSocket are mounted only then (P24-47)."""

    def __post_init__(self) -> None:
        self.gate = TurnGate(lambda: self.settings().max_concurrent_runs)

    @property
    def image_dir(self) -> Path:
        return self.scope.home / "cache" / "api_server" / "images"

    def now_s(self) -> int:
        return int(self.clock.now().timestamp())


# -- helpers ------------------------------------------------------------------------------------


def error_response(exc: BadRequest | wire.WireError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status)


def protocol_failure(exc: ProtocolError) -> BadRequest:
    status = exc.error.status
    return BadRequest(
        redact_output(exc.message, force=True),
        status=status if 400 <= status < 600 else 500,
        code=exc.error.name.lower(),
    )


def session_header(request: Request) -> str | None:
    raw = request.headers.get(SESSION_HEADER)
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if len(value) > SESSION_HEADER_MAX:
        raise BadRequest(
            f"{SESSION_HEADER} is longer than {SESSION_HEADER_MAX} characters",
            code="invalid_session_id",
        )
    if not _SESSION_ID_RE.match(value):
        raise BadRequest(
            f"{SESSION_HEADER} holds characters a session id never has", code="invalid_session_id"
        )
    return value


def busy() -> BadRequest:
    return BadRequest(
        "too many API turns are running; try again shortly", status=429, code="too_many_runs"
    )


def _save_data_image(ctx: ApiContext, image: wire.ImageRef) -> Path:
    head, _, data = image.url.partition(",")
    try:
        raw = (
            base64.b64decode(data, validate=False)
            if ";base64" in head.lower()
            else urllib.parse.unquote_to_bytes(data)
        )
    except (binascii.Error, ValueError):
        raise BadRequest(
            "an image data URL could not be decoded", code="invalid_image_url"
        ) from None
    ext = _IMAGE_EXT.get(image.media_type or "", "img")
    name = f"{hashlib.sha256(raw).hexdigest()[:32]}.{ext}"
    folder = mkdir_in_profile(ctx.scope.home, ctx.image_dir.relative_to(ctx.scope.home))
    path = folder / name
    if not path.exists():
        path.write_bytes(raw)
        path.chmod(0o600)
    return path


def _image_lines(ctx: ApiContext, content: wire.Content) -> list[str]:
    lines: list[str] = []
    for image in content.images:
        where = str(_save_data_image(ctx, image)) if image.is_data else image.url
        lines.append(f"[image: {where}]")
    return lines


def compose_text(
    ctx: ApiContext,
    user: wire.Content,
    *,
    system: str = "",
    history: tuple[wire.Message, ...] = (),
    resumed: bool = False,
) -> str:
    """The text one turn submits (see the module notes)."""
    message = "\n".join(p for p in (user.text.strip(), *_image_lines(ctx, user)) if p)
    if resumed or (not system.strip() and not history):
        return message
    blocks: list[str] = []
    if system.strip():
        blocks.append(f"Instructions from the API client:\n{system.strip()}")
    if history:
        lines = []
        for item in history:
            text = "\n".join(
                p for p in (item.content.text.strip(), *_image_lines(ctx, item.content)) if p
            )
            lines.append(f"{item.role}: {text}")
        blocks.append("Earlier in this conversation:\n" + "\n".join(lines))
    blocks.append(f"Message:\n{message}")
    return "\n\n".join(blocks)


async def open_turn(
    ctx: ApiContext, session_id: str | None, text: str, *, run_context: Any
) -> Turn:
    """Open (or create) the session and start the turn; the gate must already be entered."""
    try:
        sid = await ctx.desk.open(session_id)
        return await ctx.desk.start(sid, text, run_context=run_context)
    except ProtocolError as exc:
        raise protocol_failure(exc) from None


async def next_or_tick(
    pending: asyncio.Future[Any], clock: Clock, seconds: float
) -> tuple[bool, Any]:
    """Wait for ``pending`` at most ``seconds`` (on ``clock``): ``(True, result)`` or
    ``(False, None)`` when the time ran out first (``pending`` stays alive)."""
    if pending.done():
        return True, pending.result()
    tick = asyncio.ensure_future(clock.sleep(seconds))
    try:
        await asyncio.wait({pending, tick}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        tick.cancel()
    if pending.done():
        return True, pending.result()
    return False, None


def finish_reason(outcome: TurnOutcome) -> str:
    return "length" if outcome.cut_short else "stop"


def failure_text(outcome: TurnOutcome) -> str | None:
    if outcome.completed or outcome.interrupted:
        return None
    reason = outcome.failure or (
        "the turn's result could not be read"
        if outcome.unknown
        else f"the turn ended: {outcome.exit_reason.value if outcome.exit_reason else 'unknown'}"
    )
    return redact_output(reason, force=True)


def _session_headers(session_id: str) -> dict[str, str]:
    return {SESSION_HEADER: session_id}


SSE_HEADERS = {"cache-control": "no-cache", "x-accel-buffering": "no"}


class _Release:
    """Ends a streamed turn's hold on the gate once; a stream that did not finish interrupts its
    turn (the client went away)."""

    def __init__(self, ctx: ApiContext, turn: Turn) -> None:
        self.ctx = ctx
        self.turn = turn
        self.finished = False
        self._done = False

    def __call__(self) -> None:
        if self._done:
            return
        self._done = True
        if not self.finished:
            self.turn.interrupt(hard=True)
        self.ctx.gate.leave()


class TurnStream(StreamingResponse):
    """SSE answer whose release runs even when the body was never iterated (a client that left
    before the first frame)."""

    def __init__(self, body: AsyncIterator[bytes], release: Callable[[], None], session_id: str):
        super().__init__(
            body,
            media_type="text/event-stream",
            headers={**SSE_HEADERS, SESSION_HEADER: session_id},
        )
        self.release = release

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.release()


# -- the app ------------------------------------------------------------------------------------


def build_api_app(ctx: ApiContext) -> Asgi:
    from indus.gateway.http import admin_routes, runs

    app = FastAPI(
        title="Indus Agent API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(BadRequest)
    async def _bad(request: Request, exc: BadRequest) -> Response:  # pyright: ignore[reportUnusedFunction]
        return error_response(exc)

    @app.exception_handler(wire.WireError)
    async def _wire(request: Request, exc: wire.WireError) -> Response:  # pyright: ignore[reportUnusedFunction]
        return error_response(exc)

    @app.exception_handler(BodyTooLarge)
    async def _large(request: Request, exc: BodyTooLarge) -> Response:  # pyright: ignore[reportUnusedFunction]
        return error_response(BadRequest(str(exc), status=413, code="body_too_large"))

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "platform": PLATFORM, "version": __version__})

    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/v1/health", health, methods=["GET"])

    async def models(request: Request) -> Response:
        return JSONResponse(wire.model_list(ctx.settings().model_name, ctx.now_s()))

    app.add_api_route("/v1/models", models, methods=["GET"])

    async def chat_completions(request: Request) -> Response:
        return await _chat(ctx, request)

    app.add_api_route("/v1/chat/completions", chat_completions, methods=["POST"])

    async def responses(request: Request) -> Response:
        return await _responses(ctx, request)

    app.add_api_route("/v1/responses", responses, methods=["POST"])

    async def get_response(request: Request) -> Response:
        stored = ctx.responses.get(request.path_params["response_id"])
        if stored is None:
            raise BadRequest(
                "no stored response has this id", status=404, code="response_not_found"
            )
        return JSONResponse(stored["response"], headers=_session_headers(stored["session_id"]))

    app.add_api_route("/v1/responses/{response_id}", get_response, methods=["GET"])

    ctx.runs = runs.RunTable(ctx)
    app.include_router(runs.router(ctx))
    app.include_router(admin_routes.router(ctx))
    # P24-07: a peer gateway's bot chat and DMs (/v1/bots/chat, /v1/bots/dm)
    from indus.gateway.rooms.peers import bots_router

    app.include_router(bots_router(ctx))
    if ctx.browser_broker is not None:
        from indus.gateway.browser_broker.routes import mount_routes

        mount_routes(app, ctx.browser_broker, lambda: ctx.settings().cors_origins)

    def too_large(limit: int) -> dict[str, Any]:
        if limit < 0:
            return BadRequest("invalid Content-Length", code="invalid_content_length").body()
        return BadRequest(
            f"the request body is larger than {limit} bytes", status=413, code="body_too_large"
        ).body()

    inner: Asgi = LengthGate(app, MAX_REQUEST_BYTES, too_large)

    async def counted(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            ctx.requests_served += 1
        await inner(scope, receive, send)

    return SecurityHeaders(
        CorsGate(BearerGate(counted, ctx.keys.verify), lambda: ctx.settings().cors_origins)
    )


# -- chat completions ---------------------------------------------------------------------------


async def _chat(ctx: ApiContext, request: Request) -> Response:
    body = await read_json(request, MAX_REQUEST_BYTES)
    req = wire.parse_chat_request(body)
    resume = session_header(request)
    model = req.model or ctx.settings().model_name
    if not ctx.gate.enter():
        raise busy()
    try:
        text = compose_text(
            ctx, req.user, system=req.system, history=req.history, resumed=resume is not None
        )
        turn = await open_turn(ctx, resume, text, run_context="unattended")
    except BaseException:
        ctx.gate.leave()
        raise
    completion_id = "chatcmpl-" + secrets.token_hex(14)
    created = ctx.now_s()
    if req.stream:
        release = _Release(ctx, turn)
        body = _chat_stream(ctx, turn, release, completion_id, created, model)
        return TurnStream(body, release, turn.session_id)
    try:
        outcome = await turn.outcome()
    except BaseException:
        turn.interrupt(hard=True)
        raise
    finally:
        ctx.gate.leave()
    headers = _session_headers(outcome.session_id)
    problem = failure_text(outcome)
    if problem and not outcome.text.strip():
        body_out = wire.error_body(problem, kind="server_error", code="agent_failed")
        return JSONResponse(body_out, status_code=502, headers=headers)
    answer = wire.chat_completion(
        completion_id=completion_id,
        created=created,
        model=model,
        text=outcome.text,
        finish_reason=finish_reason(outcome),
        usage=wire.chat_usage(outcome.prompt_tokens, outcome.completion_tokens),
    )
    if problem:
        answer["indus"] = {"status": "failed", "error": problem}
    return JSONResponse(answer, headers=headers)


async def _chat_stream(
    ctx: ApiContext, turn: Turn, release: _Release, completion_id: str, created: int, model: str
) -> AsyncIterator[bytes]:
    def chunk(delta: dict[str, Any], **kw: Any) -> bytes:
        return wire.sse_frame(
            wire.chat_chunk(
                completion_id=completion_id, created=created, model=model, delta=delta, **kw
            )
        )

    sent_text = False
    pending: asyncio.Future[Any] | None = None
    try:
        yield chunk({"role": "assistant", "content": ""})
        while True:
            if pending is None:
                pending = asyncio.ensure_future(turn.next_item())
            ready, item = await next_or_tick(pending, ctx.clock, KEEPALIVE_S)
            if not ready:
                yield wire.sse_comment("keepalive")
                continue
            pending = None
            if item is None:
                break
            if isinstance(item, MessageDelta) and item.text:
                sent_text = True
                yield chunk({"content": item.text})
            elif isinstance(item, ReasoningDelta) and item.text:
                yield chunk({"reasoning_content": item.text})
        outcome = await turn.outcome()
        if not sent_text and outcome.text:
            yield chunk({"content": outcome.text})
        usage = wire.chat_usage(outcome.prompt_tokens, outcome.completion_tokens)
        last = wire.chat_chunk(
            completion_id=completion_id,
            created=created,
            model=model,
            delta={},
            finish_reason=finish_reason(outcome),
            usage=usage,
        )
        problem = failure_text(outcome)
        if problem:
            last["indus"] = {"status": "failed", "error": problem}
        release.finished = True
        yield wire.sse_frame(last)
        yield wire.SSE_DONE
    finally:
        if pending is not None:
            pending.cancel()
        release()


# -- responses ----------------------------------------------------------------------------------


async def _responses(ctx: ApiContext, request: Request) -> Response:
    body = await read_json(request, MAX_REQUEST_BYTES)
    req = wire.parse_responses_request(body)
    resume = session_header(request)
    if req.previous_response_id is not None:
        stored = ctx.responses.get(req.previous_response_id)
        if stored is None:
            raise BadRequest(
                "previous_response_id names no stored response",
                status=404,
                code="response_not_found",
                param="previous_response_id",
            )
        resume = resume or str(stored["session_id"])
    model = req.model or ctx.settings().model_name
    if not ctx.gate.enter():
        raise busy()
    try:
        text = compose_text(
            ctx, req.user, system=req.instructions, history=req.history, resumed=resume is not None
        )
        turn = await open_turn(ctx, resume, text, run_context="unattended")
    except BaseException:
        ctx.gate.leave()
        raise
    response_id = "resp_" + secrets.token_hex(24)
    item_id = "msg_" + secrets.token_hex(24)
    created = ctx.now_s()
    shape = _ResponseShape(ctx, response_id, item_id, created, model, req)
    if req.stream:
        release = _Release(ctx, turn)
        return TurnStream(_responses_stream(ctx, turn, shape, release), release, turn.session_id)
    try:
        outcome = await turn.outcome()
    except BaseException:
        turn.interrupt(hard=True)
        raise
    finally:
        ctx.gate.leave()
    final = shape.final(outcome, outcome.text)
    shape.keep(final, outcome.session_id)
    return JSONResponse(final, headers=_session_headers(outcome.session_id))


@dataclass
class _ResponseShape:
    ctx: ApiContext
    response_id: str
    item_id: str
    created: int
    model: str
    req: wire.ResponsesRequest

    def obj(self, status: str, **kw: Any) -> dict[str, Any]:
        return wire.response_object(
            response_id=self.response_id,
            created_at=self.created,
            model=self.model,
            status=status,
            instructions=self.req.instructions or None,
            previous_response_id=self.req.previous_response_id,
            **kw,
        )

    def final(self, outcome: TurnOutcome, text: str) -> dict[str, Any]:
        usage = wire.responses_usage(outcome.prompt_tokens, outcome.completion_tokens)
        problem = failure_text(outcome)
        if problem and not text:
            error = {"code": "server_error", "message": problem}
            return self.obj("failed", output=[], usage=usage, error=error)
        output = [wire.message_item(self.item_id, text)]
        if outcome.completed:
            return self.obj("completed", output=output, usage=usage)
        answer = self.obj("incomplete", output=output, usage=usage)
        if outcome.cut_short:
            answer["incomplete_details"] = {"reason": "max_output_tokens"}
        return answer

    def keep(self, answer: dict[str, Any], session_id: str) -> None:
        if self.req.store:
            self.ctx.responses.put(answer, session_id)


async def _responses_stream(
    ctx: ApiContext, turn: Turn, shape: _ResponseShape, release: _Release
) -> AsyncIterator[bytes]:
    seq = 0

    def event(kind: str, **fields: Any) -> bytes:
        nonlocal seq
        data = {"type": kind, "sequence_number": seq, **fields}
        seq += 1
        return wire.sse_frame(data, event=kind)

    parts: list[str] = []
    opened = False
    pending: asyncio.Future[Any] | None = None

    def opening() -> list[bytes]:
        item = {
            "id": shape.item_id,
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        }
        part = {"type": "output_text", "text": "", "annotations": [], "logprobs": []}
        return [
            event("response.output_item.added", output_index=0, item=item),
            event(
                "response.content_part.added",
                item_id=shape.item_id,
                output_index=0,
                content_index=0,
                part=part,
            ),
        ]

    def delta(text: str) -> bytes:
        return event(
            "response.output_text.delta",
            item_id=shape.item_id,
            output_index=0,
            content_index=0,
            delta=text,
            logprobs=[],
        )

    try:
        yield event("response.created", response=shape.obj("in_progress"))
        yield event("response.in_progress", response=shape.obj("in_progress"))
        while True:
            if pending is None:
                pending = asyncio.ensure_future(turn.next_item())
            ready, item = await next_or_tick(pending, ctx.clock, KEEPALIVE_S)
            if not ready:
                yield wire.sse_comment("keepalive")
                continue
            pending = None
            if item is None:
                break
            if isinstance(item, MessageDelta) and item.text:
                if not opened:
                    opened = True
                    for frame in opening():
                        yield frame
                parts.append(item.text)
                yield delta(item.text)
        outcome = await turn.outcome()
        text = "".join(parts) or outcome.text
        if text and not opened:
            opened = True
            for frame in opening():
                yield frame
            parts.append(text)
            yield delta(text)
        final = shape.final(outcome, text)
        if opened:
            part = {"type": "output_text", "text": text, "annotations": [], "logprobs": []}
            yield event(
                "response.output_text.done",
                item_id=shape.item_id,
                output_index=0,
                content_index=0,
                text=text,
                logprobs=[],
            )
            yield event(
                "response.content_part.done",
                item_id=shape.item_id,
                output_index=0,
                content_index=0,
                part=part,
            )
            yield event(
                "response.output_item.done",
                output_index=0,
                item=wire.message_item(shape.item_id, text),
            )
        shape.keep(final, outcome.session_id)
        release.finished = True
        yield event(f"response.{final['status']}", response=final)
    finally:
        if pending is not None:
            pending.cancel()
        release()


# -- the server ---------------------------------------------------------------------------------


class NoApiKey(RuntimeError):
    pass


class ApiServer:
    """The API app on its own port inside the daemon (see the module notes)."""

    def __init__(
        self,
        ctx: ApiContext,
        *,
        on_status: Callable[[dict[str, Any]], object] | None = None,
        front: Callable[[Asgi], Asgi] | None = None,
    ) -> None:
        self.ctx = ctx
        self.on_status = on_status
        self.profile_app = build_api_app(ctx)
        self.app = front(self.profile_app) if front is not None else self.profile_app
        self.server: HttpServer | None = None
        self._beat: asyncio.Task[None] | None = None

    @property
    def address(self) -> tuple[str, int] | None:
        return self.server.address if self.server is not None else None

    async def start(self) -> None:
        if not self.ctx.keys.has_keys():
            raise NoApiKey(
                "the API server needs an API key: run `indus gateway api-key create` first"
            )
        settings = self.ctx.settings()
        sock = await bind_listener(settings.host, settings.port, self.ctx.clock)
        ws: dict[str, Any] | None = None
        if self.ctx.browser_broker is not None:
            from indus.gateway.browser_broker.routes import ws_settings

            ws = ws_settings()
        server = HttpServer(self.app, sock, name="API server", ws=ws)
        await server.start()
        self.server = server
        if self.on_status is not None:
            self._beat = asyncio.ensure_future(self._heartbeat())

    def status(self) -> dict[str, Any]:
        address = self.address
        return {
            "listening": f"{address[0]}:{address[1]}" if address else None,
            "active_turns": self.ctx.gate.active,
            "requests": self.ctx.requests_served,
        }

    async def _heartbeat(self) -> None:
        while True:
            callback = self.on_status
            if callback is not None:
                try:
                    callback(self.status())
                except Exception:
                    log.warning("the API server status callback failed", exc_info=True)
            await self.ctx.clock.sleep(HEARTBEAT_S)

    async def aclose(self) -> None:
        if self._beat is not None:
            self._beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._beat
            self._beat = None
        if self.ctx.browser_broker is not None:
            await self.ctx.browser_broker.aclose()
        jobs = list(self.ctx.jobs)
        for task in jobs:
            task.cancel()
        for task in jobs:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.ctx.runs.aclose()
        await self.ctx.desk.aclose()
        if self.server is not None:
            await self.server.stop()
            self.server = None
