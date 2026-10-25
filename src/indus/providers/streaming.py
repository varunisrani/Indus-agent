"""Run one model call as a stream and decide what to do when the stream goes wrong.

``stream_call`` sends the request, feeds the parsed events to ``on_event`` and returns the
assembled response. Around that it handles:

- **writer tokens** — each attempt owns a token; events of an attempt we gave up on are
  discarded and counted, never delivered;
- **stale detection** — a watchdog on the injected clock aborts an attempt that delivers no
  content (keep-alive frames do not count) within the stale timeout of the ``TimeoutPolicy``;
- **reconnects** — an attempt that failed before anything visible was shown is retried up to
  ``stream_retries`` more times (default 2) after 1 s, 2 s, 4 s pauses; a 400 rejecting
  ``stream_options`` gets one extra retry without it that costs nothing from that budget;
- **route changes** — if the route epoch moved while the call ran, nothing is replayed and
  ``RouteChanged`` is returned so the turn loop rebuilds the request (R01.5);
- **the breaker** — consecutive silent attempts are counted in a ``StaleStreak`` the caller keeps
  per route; at the threshold calls fail at once with ``StaleBreakerTripped``;
- **partial delivery** — once text was shown, a failure becomes a ``length`` stub holding that
  text, with mid-stream tool calls dropped and named, never run; a context overflow becomes an
  empty terminal stub instead (R01.6);
- **non-streaming fallback** — a provider that rejects streaming, or sends only keep-alive frames,
  flips ``StreamFlags.streaming`` off and the call is redone once without streaming;
- **cancellation** — the stream is closed and ``CancelledError`` propagates; nothing partial is
  returned as a reply (R01.4).

Errors before anything visible propagate to the caller (``retry_call``, P03-16).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from indus.foundation.errors import (
    ProviderError,
    ProviderHTTPError,
    ProviderStreamError,
    StaleBreakerTripped,
)
from indus.providers.classify import ClassifiedError, route_key
from indus.providers.classify import classify as default_classify
from indus.providers.retry import sleep_sliced
from indus.providers.transports import frames_of, http_error_from_response
from indus.providers.types import (
    ChatRequest,
    Finish,
    KeepAlive,
    NormalizedResponse,
    ReasoningDelta,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    UsageReport,
    assemble,
)

if TYPE_CHECKING:
    from indus.foundation.clock import Clock
    from indus.providers.timeouts import CallTimeouts, TimeoutPolicy

log = logging.getLogger(__name__)

RECONNECT_PAUSES = (1.0, 2.0, 4.0)
PAUSE_SLICE = 0.1
MAX_NAMED_DROPPED = 3
# Verdicts a fresh connection can fix; HTTP status errors go to retry_call with their waits.
RECONNECT_REASONS = frozenset({"timeout", "connection", "incomplete_response"})
_CAUGHT = (httpx.HTTPError, ProviderError, OSError)


@dataclass
class StreamFlags:
    """Per-route session switches the driver may turn off (kept by the caller)."""

    streaming: bool = True
    stream_options: bool = True


@dataclass
class StaleStreak:
    """Consecutive attempts that went silent; reset by any answer or a route change."""

    count: int = 0

    def bump(self) -> int:
        self.count += 1
        return self.count

    def reset(self) -> None:
        self.count = 0


@dataclass(frozen=True)
class CallResult:
    response: NormalizedResponse
    stub: bool = False  # a length stub built from text shown before the stream failed
    stub_terminal: bool = False  # the failure was a context overflow: end the turn
    dropped_tools: tuple[str, ...] = ()  # up to 3 names of mid-stream tool calls not run
    dropped_tool_count: int = 0
    streamed: bool = True
    attempts: int = 1
    requests: int = 1
    discarded_events: int = 0
    error: ClassifiedError | None = None  # why a stub was needed


@dataclass(frozen=True)
class RouteChanged:
    """The route moved while the call ran; the caller rebuilds the request for the new route."""

    error: ClassifiedError | None
    attempts: int
    requests: int


CallOutcome = CallResult | RouteChanged


@dataclass
class _Writer:
    """The single-writer token: only the current attempt's events are delivered."""

    token: int = 0
    discarded: int = 0

    def claim(self) -> int:
        self.token += 1
        return self.token

    def release(self, token: int) -> None:
        if token == self.token:
            self.token += 1

    def owns(self, token: int) -> bool:
        return token == self.token


@dataclass
class _Attempt:
    token: int
    events: list[Any] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    tools: dict[int, str] = field(default_factory=dict)
    visible: bool = False
    content_events: int = 0
    keepalives: int = 0
    finished: bool = False
    stale: bool = False
    error: BaseException | None = None


class _Call:
    def __init__(
        self,
        client: httpx.AsyncClient,
        transport: Any,
        route: Any,
        clock: Clock,
        on_event: Callable[[Any], object],
    ) -> None:
        self.client = client
        self.transport = transport
        self.route = route
        self.clock = clock
        self.on_event = on_event
        self.writer = _Writer()
        self.attempts = 0
        self.requests = 0

    async def deliver(self, event: Any) -> None:
        result = self.on_event(event)
        if inspect.isawaitable(result):
            await result

    # -- one streamed attempt --------------------------------------------------------------------

    async def attempt(self, request: httpx.Request, timeouts: CallTimeouts) -> _Attempt:
        self.attempts += 1
        self.requests += 1
        att = _Attempt(self.writer.claim())
        last_content = [self.clock.monotonic()]
        reader = asyncio.ensure_future(self._read(att, request, last_content))
        watchdog = (
            asyncio.ensure_future(self._watch(att, reader, last_content, timeouts.stale))
            if timeouts.stale is not None
            else None
        )
        try:
            await asyncio.wait({reader})
        except asyncio.CancelledError:
            self.writer.release(att.token)
            reader.cancel()
            if watchdog is not None:
                watchdog.cancel()
            await _settle(reader, watchdog)
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
        self.writer.release(att.token)
        if watchdog is not None:
            await _settle(watchdog)
        if reader.cancelled():
            # Only the watchdog cancels the reader without cancelling us; it set ``stale``.
            att.error = att.error or _stale_error(self.route)
        else:
            reader.result()  # re-raises anything that is not a provider failure (a bug)
        if att.error is None and not att.stale and not att.finished:
            att.error = _stream_error(
                "the stream ended before the model finished", "incomplete_stream"
            )
        return att

    async def _watch(
        self, att: _Attempt, reader: asyncio.Future[None], last: list[float], stale: float
    ) -> None:
        while not reader.done():
            due = last[0] + stale
            now = self.clock.monotonic()
            if now >= due:
                att.stale = True
                self.writer.release(att.token)
                log.warning(
                    "no content from %s for %.0fs (limit %.0fs); aborting this attempt",
                    _host(self.route),
                    now - last[0],
                    stale,
                )
                reader.cancel()
                return
            await self.clock.sleep(due - now)

    async def _read(self, att: _Attempt, request: httpx.Request, last: list[float]) -> None:
        response: httpx.Response | None = None
        try:
            response = await self.client.send(request, stream=True)
            if response.status_code >= 400:
                await response.aread()
                raise _http_error(response, self.clock, self.route)
            async for event in self.transport.parse_stream(frames_of(self.transport, response)):
                if not self.writer.owns(att.token):
                    self.writer.discarded += 1
                    continue
                if isinstance(event, KeepAlive):
                    att.keepalives += 1
                    continue
                last[0] = self.clock.monotonic()
                att.content_events += 1
                att.events.append(event)
                if isinstance(event, TextDelta):
                    att.text.append(event.text)
                    att.visible = att.visible or bool(event.text.strip())
                elif isinstance(event, ToolCallStart):
                    if event.name or event.index not in att.tools:
                        att.tools[event.index] = event.name
                elif isinstance(event, Finish):
                    att.finished = True
                await self.deliver(event)
        except asyncio.CancelledError:
            if not att.stale:
                raise
            att.error = att.error or _stale_error(self.route)
        except _CAUGHT as exc:
            att.error = exc
        finally:
            if response is not None:
                await response.aclose()
        if att.stale and att.error is None:
            att.error = _stale_error(self.route)

    # -- the non-streaming call ------------------------------------------------------------------

    async def non_stream(
        self, request: httpx.Request, timeouts: CallTimeouts
    ) -> NormalizedResponse:
        self.attempts += 1
        self.requests += 1
        token = self.writer.claim()

        async def send() -> httpx.Response:
            response = await self.client.send(request)
            if response.status_code >= 400:
                raise _http_error(response, self.clock, self.route)
            return response

        task = asyncio.ensure_future(send())
        timer = (
            asyncio.ensure_future(self.clock.sleep(timeouts.stale))
            if timeouts.stale is not None
            else None
        )
        try:
            waiting = {task} if timer is None else {task, timer}
            await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            await _settle(task, timer)
            raise
        if not task.done():
            task.cancel()
            await _settle(task, timer)
            raise httpx.ReadTimeout(
                f"no answer from {_host(self.route)} within {timeouts.stale:.0f}s", request=request
            )
        if timer is not None:
            timer.cancel()
            await _settle(timer)
        response = task.result()
        normalized = self.transport.parse_response(response.json())
        if self.writer.owns(token):
            for event in _events_of(normalized):
                await self.deliver(event)
        self.writer.release(token)
        return normalized


async def stream_call(
    client: httpx.AsyncClient,
    transport: Any,
    route: Any,
    req: ChatRequest,
    *,
    policy: TimeoutPolicy,
    clock: Clock,
    on_event: Callable[[Any], object | Awaitable[object]],
    route_epoch: Callable[[], object],
    flags: StreamFlags | None = None,
    streak: StaleStreak | None = None,
    classify: Callable[[BaseException], ClassifiedError] | None = None,
    est_prompt_tokens: int | None = None,
    reasoning: bool | None = None,
) -> CallOutcome:
    flags = flags if flags is not None else StreamFlags()
    streak = streak if streak is not None else StaleStreak()

    def judge(err: BaseException) -> ClassifiedError:
        if classify is not None:
            return classify(err)
        return default_classify(err, route=route, clock=clock)

    epoch = route_epoch()
    # A provider that only answers streamed requests never gets the one-JSON-reply path.
    stream_only = bool(getattr(getattr(route, "profile", None), "stream_only", False))
    if stream_only:
        flags.streaming = True
    thinking = bool(req.reasoning_effort) if reasoning is None else reasoning
    call = _Call(client, transport, route, clock, on_event)
    first = transport.build_request(route, replace(req, stream=flags.streaming))
    tokens = est_prompt_tokens if est_prompt_tokens is not None else len(first.content) // 4
    stream_t = policy.for_call(route, tokens, streaming=True, reasoning=thinking)
    threshold = stream_t.breaker_threshold
    if threshold and streak.count >= threshold:
        raise StaleBreakerTripped(_breaker_message(streak.count))

    async def without_streaming() -> CallResult:
        flags.streaming = False
        call_t = policy.for_call(route, tokens, streaming=False, reasoning=thinking)
        request = transport.build_request(route, replace(req, stream=False))
        normalized = await call.non_stream(_with_read_timeout(client, request, call_t), call_t)
        streak.reset()
        return _result(call, normalized, streamed=False)

    if not flags.streaming:
        return await without_streaming()

    reconnects = 0
    compat_used = False
    request = first
    while True:
        if not flags.stream_options:
            request = _without_stream_options(request)
        att = await call.attempt(_with_read_timeout(client, request, stream_t), stream_t)
        if att.error is None:
            streak.reset()
            return _result(call, assemble(att.events), streamed=True)
        err = att.error
        if att.visible:
            # Text reached the caller: never retry (it would repeat), hand back what was shown.
            streak.reset()
            return _stub(call, att, judge(err))
        if att.stale:
            count = streak.bump()
            if threshold and count >= threshold:
                raise StaleBreakerTripped(_breaker_message(count)) from err
        verdict = judge(err)
        if route_epoch() != epoch:
            log.warning("route changed during the call; not replaying it on the new route")
            return RouteChanged(verdict, call.attempts, call.requests)
        if not compat_used and _compat_retry(transport, route, flags, err):
            # The request shape changed for this route; one extra try outside the budget.
            compat_used = True
            request = transport.build_request(route, replace(req, stream=True))
            continue
        keepalive_only = att.keepalives > 0 and att.content_events == 0
        if (verdict.reason == "stream_unsupported" or keepalive_only) and not stream_only:
            log.warning(
                "%s did not stream (%s); switching this route to non-streaming",
                _host(route),
                "keep-alive frames only" if keepalive_only else verdict.message,
            )
            return await without_streaming()
        if (
            att.stale or verdict.reason in RECONNECT_REASONS
        ) and reconnects < stream_t.stream_retries:
            pause = RECONNECT_PAUSES[min(reconnects, len(RECONNECT_PAUSES) - 1)]
            reconnects += 1
            log.info(
                "stream attempt %d failed (%s); reconnecting in %.0fs",
                call.attempts,
                "stale" if att.stale else verdict.reason,
                pause,
            )
            await sleep_sliced(clock, pause, slice_seconds=PAUSE_SLICE)
            request = transport.build_request(route, replace(req, stream=True))
            continue
        raise err


# -- helpers --------------------------------------------------------------------------------------


def _result(call: _Call, response: NormalizedResponse, *, streamed: bool) -> CallResult:
    return CallResult(
        response=response,
        streamed=streamed,
        attempts=call.attempts,
        requests=call.requests,
        discarded_events=call.writer.discarded,
    )


def _stub(call: _Call, att: _Attempt, verdict: ClassifiedError) -> CallResult:
    names = [att.tools[i] for i in sorted(att.tools)]
    terminal = verdict.reason == "context_overflow"
    content = None if terminal else "".join(att.text)
    if names:
        log.warning(
            "stream failed mid tool call (%s); the call(s) were dropped and not run",
            ", ".join(names[:MAX_NAMED_DROPPED]),
        )
    response = NormalizedResponse(
        content=content,
        tool_calls=(),
        finish_reason="length",
        reasoning=None,
        reasoning_details=(),
        usage=None,
        provider_data={},
    )
    return CallResult(
        response=response,
        stub=True,
        stub_terminal=terminal,
        dropped_tools=tuple(names[:MAX_NAMED_DROPPED]),
        dropped_tool_count=len(names),
        streamed=True,
        attempts=call.attempts,
        requests=call.requests,
        discarded_events=call.writer.discarded,
        error=verdict,
    )


def _events_of(response: NormalizedResponse) -> list[Any]:
    """The events a non-streamed answer stands for, so consumers see one shape."""
    events: list[Any] = []
    if response.reasoning:
        events.append(ReasoningDelta(response.reasoning))
    if response.content:
        events.append(TextDelta(response.content))
    for index, call in enumerate(response.tool_calls):
        events.append(ToolCallStart(index, call.id, call.name))
        events.append(ToolCallArgsDelta(index, call.arguments))
    if response.usage is not None:
        events.append(UsageReport(response.usage))
    events.append(Finish(response.finish_reason))
    return events


def _http_error(response: httpx.Response, clock: Clock, route: Any) -> ProviderHTTPError:
    """``ProviderHTTPError`` for an error answer whose body has been read (the shared builder in
    ``providers.transports``; the route's key is masked out of the body and message)."""
    key = route_key(route)
    return http_error_from_response(
        response.status_code,
        response.headers,
        response.content,
        url=str(response.request.url),
        secrets=[key] if key else (),
        now=clock.now(),
    )


def _compat_retry(transport: Any, route: Any, flags: StreamFlags, err: BaseException) -> bool:
    """Whether a rejected request should be sent once more in a changed shape.

    A transport with an ``on_request_rejected`` hook (P03-06) decides and remembers the change
    itself; otherwise a 400/422 naming ``stream_options`` turns that option off in ``flags``.
    """
    if not isinstance(err, ProviderHTTPError):
        return False
    hook = getattr(transport, "on_request_rejected", None)
    if callable(hook):
        return bool(hook(route, err))
    if not flags.stream_options or err.status not in (400, 422):
        return False
    text = err.body if isinstance(err.body, str) else json.dumps(err.body, default=str)
    if "stream_options" not in f"{err.message} {text}":
        return False
    log.info("endpoint rejected stream_options; retrying once without it")
    flags.stream_options = False
    return True


def _without_stream_options(request: httpx.Request) -> httpx.Request:
    try:
        body = json.loads(request.content)
    except ValueError:
        return request
    if not isinstance(body, dict) or "stream_options" not in body:
        return request
    body.pop("stream_options")
    headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
    rebuilt = httpx.Request(request.method, request.url, headers=headers, content=json.dumps(body))
    rebuilt.extensions = dict(request.extensions)
    return rebuilt


def _with_read_timeout(
    client: httpx.AsyncClient, request: httpx.Request, timeouts: CallTimeouts
) -> httpx.Request:
    """Apply the client's timeouts with the read timeout raised to this call's value."""
    if "timeout" in request.extensions:
        return request
    base = client.timeout
    read = base.read
    if read is not None:
        read = max(read, timeouts.read)
    request.extensions = {
        **request.extensions,
        "timeout": httpx.Timeout(
            connect=base.connect, read=read, write=base.write, pool=base.pool
        ).as_dict(),
    }
    return request


def _stream_error(message: str, code: str) -> ProviderStreamError:
    return ProviderStreamError(message, body={"error": {"message": message, "code": code}})


def _stale_error(route: Any) -> ProviderStreamError:
    return _stream_error(f"no content from {_host(route)} within the stale timeout", "stale_stream")


def _breaker_message(count: int) -> str:
    return (
        f"the provider stayed silent on {count} attempts in a row; "
        "switch the model or provider, or start a new session, then try again"
    )


def _host(route: Any) -> str:
    try:
        return urlsplit(str(getattr(route, "base_url", ""))).hostname or "the provider"
    except ValueError:
        return "the provider"


async def _settle(*futures: asyncio.Future[Any] | None) -> None:
    """Wait for cancelled helpers to finish unwinding; their errors are already recorded."""
    pending = [f for f in futures if f is not None]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


__all__ = [
    "CallOutcome",
    "CallResult",
    "RouteChanged",
    "StaleStreak",
    "StreamFlags",
    "stream_call",
]
