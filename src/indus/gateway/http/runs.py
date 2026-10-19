"""Runs: an attended agent turn a client starts, watches and steers over HTTP (P15-31).

``POST /v1/runs`` answers 202 with a run id at once; the turn runs in the daemon as an
``interactive`` turn (its client can answer approvals). ``GET /v1/runs/{id}/events`` streams the
run's events as SSE (``id: <seq>``; ``Last-Event-ID`` resumes after a reconnect; a replay older
than the 1,000-event backlog starts with ``replay.truncated``; ``: keepalive`` after 10 s of
silence). ``GET /v1/runs/{id}`` is the pollable status. Controls: ``/approval`` and ``/clarify``
answer the open server request through the session's broker, ``/steer`` adds guidance to the
running turn, ``/stop`` interrupts it, ``/resolve-unknown`` settles a run whose outcome could not
be read, only with its exact generation.

A run belongs to the API key that started it; any other key gets 404. Runs count against the
API server's turn cap (429 beyond). The event buffer is dropped ``STREAM_TTL_S`` after the run
started unless a client is listening; a finished run's status is kept ``STATUS_TTL_S`` after it
ended. Times are on the injected clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from indus.gateway.http import openai_wire as wire
from indus.gateway.http.serving import BadRequest, read_json
from indus.gateway.http.turns import Turn, TurnOutcome
from indus.protocol.events import (
    MessageDelta,
    ReasoningDelta,
    ServerRequestCancelled,
    ToolCompleted,
    ToolStarted,
)
from indus.protocol.methods import ProtocolError
from indus.protocol.server_requests import ApprovalChoice, ApprovalRequest, ClarifyRequest
from indus.safety.redaction import redact_output

if TYPE_CHECKING:
    from indus.gateway.http.api_server import ApiContext

__all__ = [
    "BACKLOG_LIMIT",
    "STATUS_TTL_S",
    "STREAM_TTL_S",
    "TERMINAL",
    "Run",
    "RunStream",
    "RunTable",
    "router",
]

log = logging.getLogger(__name__)

STREAM_TTL_S = 300.0
STATUS_TTL_S = 3_600.0
SWEEP_S = 60.0
BACKLOG_LIMIT = 1_000
SUBSCRIBER_QUEUE = 256
PREVIEW_MAX = 500
TERMINAL = frozenset({"completed", "failed", "cancelled", "resolved"})
_OVERFLOW = object()
_CHOICE_WORDS = {"approve": "once", "approved": "once", "allow": "once", "yes": None}


# -- the event stream ---------------------------------------------------------------------------


class RunStream:
    """Numbered events of one run, a bounded backlog for replay, and live subscribers."""

    def __init__(self) -> None:
        self.backlog: deque[tuple[int, dict[str, Any] | None]] = deque(maxlen=BACKLOG_LIMIT)
        self.subscribers: set[asyncio.Queue[tuple[int, Any]]] = set()
        self.next_seq = 0
        self.closed = False

    def put(self, event: dict[str, Any] | None) -> None:
        if self.closed:
            return
        seq = self.next_seq
        self.next_seq += 1
        self.backlog.append((seq, event))
        if event is None:
            self.closed = True
        for queue in list(self.subscribers):
            try:
                queue.put_nowait((seq, event))
            except asyncio.QueueFull:
                self.subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait((seq, _OVERFLOW))

    def attach(
        self, last_seq: int
    ) -> tuple[asyncio.Queue[tuple[int, Any]], list[tuple[int, dict[str, Any] | None]]]:
        replay = [(seq, event) for seq, event in self.backlog if seq > last_seq]
        queue: asyncio.Queue[tuple[int, Any]] = asyncio.Queue(SUBSCRIBER_QUEUE + len(replay))
        self.subscribers.add(queue)
        return queue, replay

    def detach(self, queue: asyncio.Queue[tuple[int, Any]]) -> None:
        self.subscribers.discard(queue)


# -- runs ---------------------------------------------------------------------------------------


@dataclass
class Run:
    run_id: str
    owner: str
    session_id: str
    started: float  # monotonic
    created_at: int  # unix seconds
    status: str = "queued"
    updated_at: int = 0
    generation: int = 0
    last_event: str | None = None
    output: str | None = None
    error: str | None = None
    pending: ApprovalRequest | ClarifyRequest | None = None
    turn: Turn | None = None
    task: asyncio.Task[None] | None = None
    stream: RunStream | None = field(default_factory=RunStream)
    ended: float | None = None  # monotonic

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


class RunTable:
    """The runs of one API server (see the module notes)."""

    def __init__(self, ctx: ApiContext) -> None:
        self.ctx = ctx
        self.runs: dict[str, Run] = {}
        self._sweeper: asyncio.Task[None] | None = None

    # -- bookkeeping ----------------------------------------------------------------------------

    def _now(self) -> float:
        return self.ctx.clock.monotonic()

    def emit(self, run: Run, name: str, **fields: Any) -> None:
        run.last_event = name
        run.updated_at = self.ctx.now_s()
        if run.stream is not None:
            run.stream.put(
                {"event": name, "run_id": run.run_id, "timestamp": run.updated_at, **fields}
            )

    def set_status(self, run: Run, status: str) -> None:
        run.status = status
        run.updated_at = self.ctx.now_s()
        if status in TERMINAL and run.ended is None:
            run.ended = self._now()

    def sweep(self) -> None:
        now = self._now()
        for run_id, run in list(self.runs.items()):
            stream = run.stream
            if stream is not None and now - run.started > STREAM_TTL_S and not stream.subscribers:
                run.stream = None
            if run.terminal and run.ended is not None and now - run.ended > STATUS_TTL_S:
                del self.runs[run_id]

    def get(self, run_id: str, owner: str | None) -> Run:
        self.sweep()
        run = self.runs.get(run_id)
        if run is None or owner is None or run.owner != owner:
            raise BadRequest("no run has this id", status=404, code="run_not_found")
        return run

    def status(self, run: Run) -> dict[str, Any]:
        out: dict[str, Any] = {
            "object": "indus.run",
            "run_id": run.run_id,
            "status": run.status,
            "session_id": run.session_id,
            "generation": run.generation,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "last_event": run.last_event,
        }
        if run.output is not None:
            out["output"] = run.output
        if run.error is not None:
            out["error"] = run.error
        pending = run.pending
        if isinstance(pending, ApprovalRequest):
            out["approval"] = _approval_fields(pending)
        elif isinstance(pending, ClarifyRequest):
            out["clarify"] = _clarify_fields(pending)
        return out

    # -- starting -------------------------------------------------------------------------------

    async def create(self, owner: str, body: object, header_session: str | None) -> Run:
        from indus.gateway.http.api_server import compose_text, open_turn

        req = wire.parse_responses_request(body)
        fields = cast(dict[str, Any], body)
        raw_sid = fields.get("session_id")
        resume = raw_sid.strip() if isinstance(raw_sid, str) and raw_sid.strip() else None
        resume = resume or header_session
        if not self.ctx.gate.enter():
            raise BadRequest(
                "too many API turns are running; try again shortly",
                status=429,
                code="too_many_runs",
            )
        try:
            text = compose_text(
                self.ctx,
                req.user,
                system=req.instructions,
                history=req.history,
                resumed=resume is not None,
            )
            turn = await open_turn(self.ctx, resume, text, run_context="interactive")
        except BaseException:
            self.ctx.gate.leave()
            raise
        run = Run(
            run_id="run_" + secrets.token_hex(16),
            owner=owner,
            session_id=turn.session_id,
            started=self._now(),
            created_at=self.ctx.now_s(),
            generation=turn.generation,
            turn=turn,
        )
        self.runs[run.run_id] = run
        self.set_status(run, "running")
        self.emit(run, "run.started", session_id=run.session_id, generation=run.generation)
        run.task = asyncio.ensure_future(self._drive(run, turn))
        self._ensure_sweeper()
        return run

    def _ensure_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.ensure_future(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while self.runs:
            await self.ctx.clock.sleep(SWEEP_S)
            self.sweep()

    async def _drive(self, run: Run, turn: Turn) -> None:
        try:
            async for item in turn.events():
                self._on_item(run, item)
            outcome = await turn.outcome()
            self._finish(run, outcome)
        except asyncio.CancelledError:
            if not run.terminal:
                self.set_status(run, "cancelled")
                self.emit(run, "run.cancelled")
            raise
        except Exception as exc:
            log.warning("run %s failed", run.run_id, exc_info=True)
            run.error = redact_output(str(exc), force=True)
            self.set_status(run, "failed")
            self.emit(run, "run.failed", error=run.error)
        finally:
            run.turn = None
            run.pending = None
            self.ctx.gate.leave()
            if run.stream is not None:
                run.stream.put(None)

    def _on_item(self, run: Run, item: object) -> None:
        if isinstance(item, MessageDelta):
            if item.text:
                self.emit(run, "message.delta", delta=item.text)
        elif isinstance(item, ReasoningDelta):
            if item.text:
                self.emit(run, "reasoning.delta", text=item.text)
        elif isinstance(item, ToolStarted):
            self.emit(run, "tool.started", tool=item.name, tool_call_id=item.call_id)
        elif isinstance(item, ToolCompleted):
            preview = redact_output(item.result, force=True)
            if len(preview) > PREVIEW_MAX:
                preview = preview[: PREVIEW_MAX - 3] + "..."
            self.emit(
                run,
                "tool.completed",
                tool=item.name,
                tool_call_id=item.call_id,
                ok=item.ok,
                duration_ms=item.duration_ms,
                preview=preview,
            )
        elif isinstance(item, ApprovalRequest):
            run.pending = item
            self.set_status(run, "waiting_for_approval")
            self.emit(run, "approval.request", **_approval_fields(item))
        elif isinstance(item, ClarifyRequest):
            run.pending = item
            self.set_status(run, "waiting_for_input")
            self.emit(run, "clarify.request", **_clarify_fields(item))
        elif isinstance(item, ServerRequestCancelled):
            pending = run.pending
            if pending is not None and pending.request_id == item.request_id:
                run.pending = None
                if not run.terminal and run.status != "stopping":
                    self.set_status(run, "running")
                self.emit(
                    run, "request.cancelled", request_id=item.request_id, reason=item.reason.value
                )

    def _finish(self, run: Run, outcome: TurnOutcome) -> None:
        if run.status == "stopping" or outcome.interrupted:
            status, name = "cancelled", "run.cancelled"
        elif outcome.unknown:
            status, name = "unknown", "run.unknown"
        elif outcome.completed:
            status, name = "completed", "run.completed"
        else:
            status, name = "failed", "run.failed"
        run.output = outcome.text
        if status in {"failed", "unknown"}:
            run.error = redact_output(
                outcome.failure or "the turn ended without an answer", force=True
            )
        self.set_status(run, status)
        fields: dict[str, Any] = {
            "output": run.output,
            "usage": {
                "input_tokens": outcome.prompt_tokens,
                "output_tokens": outcome.completion_tokens,
            },
        }
        if run.error is not None:
            fields["error"] = run.error
        self.emit(run, name, **fields)

    # -- controls -------------------------------------------------------------------------------

    def answer(self, run: Run, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        pending = run.pending
        wanted = ApprovalRequest if kind == "approval" else ClarifyRequest
        if run.terminal or not isinstance(pending, wanted):
            raise BadRequest(
                f"the run has no {kind} request waiting", status=409, code=f"{kind}_not_pending"
            )
        request_id = body.get("request_id")
        if request_id is not None and request_id != pending.request_id:
            raise BadRequest(
                f"the open {kind} request has another id", status=409, code=f"{kind}_not_pending"
            )
        if kind == "approval":
            raw = str(body.get("choice") or "").strip().lower()
            choice = _CHOICE_WORDS.get(raw, raw)
            offered = {c.value for c in cast(ApprovalRequest, pending).choices}
            if choice not in offered:
                raise BadRequest(
                    "choice must be one of: " + ", ".join(sorted(offered)),
                    code="invalid_approval_choice",
                )
            reason = body.get("reason")
            answer: dict[str, Any] = {"kind": "approval", "choice": choice}
            if isinstance(reason, str) and reason.strip() and choice == ApprovalChoice.DENY.value:
                answer["reason"] = reason.strip()
            shown: dict[str, Any] = {"choice": choice}
        else:
            answers = body.get("answers")
            if not isinstance(answers, dict):
                raise BadRequest(
                    "'answers' must map question ids to lists of answers",
                    code="invalid_clarify_answers",
                )
            answer = {"kind": "clarify", "answers": answers}
            shown = {}
        try:
            self.ctx.desk.service.answer_server_request(run.session_id, pending.request_id, answer)
        except ProtocolError as exc:
            status = 409 if exc.error.status in {404, 409} else 400
            raise BadRequest(
                redact_output(exc.message, force=True), status=status, code=exc.error.name.lower()
            ) from None
        run.pending = None
        if not run.terminal and run.status != "stopping":
            self.set_status(run, "running")
        self.emit(run, f"{kind}.responded", request_id=pending.request_id, **shown)
        return {
            "object": f"indus.run.{kind}_response",
            "run_id": run.run_id,
            "request_id": pending.request_id,
            **shown,
        }

    def steer(self, run: Run, text: str) -> dict[str, Any]:
        if run.terminal or run.status == "stopping" or run.turn is None:
            raise BadRequest("the run is not running", status=409, code="run_not_running")
        try:
            result = self.ctx.desk.service.steer(run.session_id, text)
        except ProtocolError as exc:
            raise BadRequest(
                redact_output(exc.message, force=True), status=409, code=exc.error.name.lower()
            ) from None
        if not result.queued:
            raise BadRequest(
                "the run did not take the guidance", status=409, code="steer_not_accepted"
            )
        self.emit(run, "run.steered", accepted=True)
        return {"object": "indus.run.steer", "run_id": run.run_id, "accepted": True}

    def stop(self, run: Run) -> dict[str, Any]:
        if run.terminal or run.status == "unknown":
            return self.status(run)
        turn = run.turn
        self.set_status(run, "stopping")
        self.emit(run, "run.stopping")
        if turn is not None:
            turn.interrupt(hard=True)
        return {"run_id": run.run_id, "status": "stopping"}

    def resolve_unknown(self, run: Run, generation: object) -> dict[str, Any]:
        if run.status != "unknown":
            raise BadRequest(
                "only a run in status 'unknown' can be resolved", status=409, code="run_not_unknown"
            )
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or (generation != run.generation)
        ):
            raise BadRequest(
                "generation does not match the run's turn", status=409, code="generation_mismatch"
            )
        self.set_status(run, "resolved")
        self.emit(run, "run.resolved", generation=generation)
        return self.status(run)

    async def aclose(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
        tasks = [run.task for run in self.runs.values() if run.task is not None]
        for run in self.runs.values():
            if run.turn is not None:
                run.turn.interrupt(hard=True)
        for task in tasks:
            task.cancel()
        for task in [*tasks, self._sweeper]:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


def _approval_fields(request: ApprovalRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "command": request.command,
        "description": request.description,
        "choices": [c.value for c in request.choices],
    }


def _clarify_fields(request: ClarifyRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "questions": [q.model_dump(mode="json") for q in request.questions],
    }


# -- routes -------------------------------------------------------------------------------------


def _owner(request: Request) -> str | None:
    state = request.scope.get("state") or {}
    key = state.get("api_key_id")
    return key if isinstance(key, str) else None


def _last_seq(request: Request) -> int:
    raw = request.headers.get("last-event-id") or request.query_params.get("last_seq")
    if raw is None:
        return -1
    try:
        return max(-1, int(raw.strip()))
    except ValueError:
        return -1


async def _body(request: Request) -> dict[str, Any]:
    from indus.gateway.http.api_server import MAX_REQUEST_BYTES

    data = await read_json(request, MAX_REQUEST_BYTES)
    if not isinstance(data, dict):
        raise BadRequest("the request body must be a JSON object")
    return cast(dict[str, Any], data)


def router(ctx: ApiContext) -> APIRouter:
    from indus.gateway.http.api_server import KEEPALIVE_S, next_or_tick, session_header

    api = APIRouter()

    def table() -> RunTable:
        return cast(RunTable, ctx.runs)

    async def create(request: Request) -> Response:
        owner = _owner(request)
        assert owner is not None  # the bearer gate let it in
        body = await _body(request)
        run = await table().create(owner, body, session_header(request))
        return JSONResponse(
            {"run_id": run.run_id, "status": "started", "session_id": run.session_id},
            status_code=202,
            headers={"X-Indus-Session-Id": run.session_id},
        )

    async def get(request: Request) -> Response:
        run = table().get(request.path_params["run_id"], _owner(request))
        return JSONResponse(table().status(run))

    async def events(request: Request) -> Response:
        runs = table()
        run = runs.get(request.path_params["run_id"], _owner(request))
        stream = run.stream
        if stream is None:
            raise BadRequest(
                "the run's events are no longer kept", status=410, code="run_events_expired"
            )
        last = _last_seq(request)
        queue, replay = stream.attach(last)
        return _EventStream(
            _event_stream(ctx, run, stream, queue, replay, last, KEEPALIVE_S, next_or_tick),
            lambda: stream.detach(queue),
        )

    async def approval(request: Request) -> Response:
        run = table().get(request.path_params["run_id"], _owner(request))
        return JSONResponse(table().answer(run, "approval", await _body(request)))

    async def clarify(request: Request) -> Response:
        run = table().get(request.path_params["run_id"], _owner(request))
        return JSONResponse(table().answer(run, "clarify", await _body(request)))

    async def steer(request: Request) -> Response:
        run = table().get(request.path_params["run_id"], _owner(request))
        body = await _body(request)
        raw = body.get("input") or body.get("text") or body.get("message")
        text = wire.normalise_content(raw, "input").text.strip()
        if not text:
            raise BadRequest("steer needs non-empty 'input' text", code="invalid_steer_input")
        return JSONResponse(table().steer(run, text))

    async def stop(request: Request) -> Response:
        run = table().get(request.path_params["run_id"], _owner(request))
        return JSONResponse(table().stop(run))

    async def resolve_unknown(request: Request) -> Response:
        run = table().get(request.path_params["run_id"], _owner(request))
        body = await _body(request)
        return JSONResponse(table().resolve_unknown(run, body.get("generation")))

    api.add_api_route("/v1/runs", create, methods=["POST"])
    api.add_api_route("/v1/runs/{run_id}", get, methods=["GET"])
    api.add_api_route("/v1/runs/{run_id}/events", events, methods=["GET"])
    api.add_api_route("/v1/runs/{run_id}/approval", approval, methods=["POST"])
    api.add_api_route("/v1/runs/{run_id}/clarify", clarify, methods=["POST"])
    api.add_api_route("/v1/runs/{run_id}/steer", steer, methods=["POST"])
    api.add_api_route("/v1/runs/{run_id}/stop", stop, methods=["POST"])
    api.add_api_route("/v1/runs/{run_id}/resolve-unknown", resolve_unknown, methods=["POST"])
    return api


class _EventStream(StreamingResponse):
    """The run's SSE answer; its subscriber is detached even if the body never ran."""

    def __init__(self, body: AsyncIterator[bytes], detach: Callable[[], None]) -> None:
        super().__init__(
            body,
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )
        self.detach = detach

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.detach()


async def _event_stream(
    ctx: ApiContext,
    run: Run,
    stream: RunStream,
    queue: asyncio.Queue[tuple[int, Any]],
    replay: list[tuple[int, dict[str, Any] | None]],
    last: int,
    keepalive: float,
    next_or_tick: Any,
) -> AsyncIterator[bytes]:
    pending: asyncio.Future[Any] | None = None
    try:
        yield wire.sse_comment("open")
        if replay and replay[0][0] > last + 1:
            yield wire.sse_frame(
                {
                    "event": "replay.truncated",
                    "run_id": run.run_id,
                    "oldest_retained_seq": replay[0][0],
                }
            )
        for seq, event in replay:
            if event is None:
                yield wire.sse_comment("stream closed")
                return
            yield wire.sse_frame({**event, "seq": seq}, event_id=seq)
        if stream.closed and queue.empty():
            yield wire.sse_comment("stream closed")
            return
        while True:
            if pending is None:
                pending = asyncio.ensure_future(queue.get())
            ready, item = await next_or_tick(pending, ctx.clock, keepalive)
            if not ready:
                yield wire.sse_comment("keepalive")
                continue
            pending = None
            seq, event = item
            if event is _OVERFLOW:
                yield wire.sse_comment("too slow; reconnect with Last-Event-ID")
                return
            if event is None:
                yield wire.sse_comment("stream closed")
                return
            yield wire.sse_frame({**event, "seq": seq}, event_id=seq)
    finally:
        if pending is not None:
            pending.cancel()
        stream.detach(queue)
