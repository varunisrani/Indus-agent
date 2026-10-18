"""Streaming call driver (P03-14) against the fake model on a fake clock."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from indus.foundation.errors import ProviderHTTPError, ProviderStreamError, StaleBreakerTripped
from indus.providers.classify import classify
from indus.providers.streaming import (
    CallResult,
    RouteChanged,
    StaleStreak,
    StreamFlags,
    stream_call,
)
from indus.providers.timeouts import TimeoutPolicy
from indus.providers.transports import get_transport
from indus.providers.types import ChatRequest, TextDelta
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, Script, fake_route, load_script, parse_script

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
REQ = ChatRequest(model="fake-model-1", messages=[{"role": "user", "content": "hi"}])


def script(*turns: list[dict[str, Any]]) -> Script:
    return parse_script({"turns": [{"steps": steps} for steps in turns]})


class Harness:
    def __init__(
        self,
        source: Script,
        *,
        api_mode: str = "chat_completions",
        policy: TimeoutPolicy | None = None,
        transport: Any = None,
    ) -> None:
        self.clock = FakeClock(START)
        self.server = FakeModelServer(source, clock=self.clock)
        self.route = fake_route(self.server, api_mode)
        self.transport = transport or get_transport(api_mode)
        self.policy = policy or TimeoutPolicy()
        self.events: list[Any] = []
        self.epoch = 0
        self.flags = StreamFlags()
        self.streak = StaleStreak()

    async def call(self, client: httpx.AsyncClient, req: ChatRequest = REQ) -> Any:
        return await stream_call(
            client,
            self.transport,
            self.route,
            req,
            policy=self.policy,
            clock=self.clock,
            on_event=self.events.append,
            route_epoch=lambda: self.epoch,
            flags=self.flags,
            streak=self.streak,
        )

    async def drive[T](self, work: Awaitable[T], *, step: float = 1.0) -> T:
        task = asyncio.ensure_future(work)
        for _ in range(20_000):
            await self.clock.run_until_idle()
            if task.done():
                return task.result()
            self.clock.advance(step)
        raise AssertionError("call never finished")

    def run(self, n_calls: int = 1) -> list[Any]:
        async def go() -> list[Any]:
            out: list[Any] = []
            async with self.server.client() as client:
                for _ in range(n_calls):
                    try:
                        out.append(await self.drive(self.call(client)))
                    except Exception as exc:
                        out.append(exc)
            return out

        return asyncio.run(go())

    @property
    def texts(self) -> str:
        return "".join(e.text for e in self.events if isinstance(e, TextDelta))


def test_plain_stream_assembles_the_answer() -> None:
    h = Harness(load_script("tool_then_text"))
    (result,) = h.run()
    assert isinstance(result, CallResult)
    response = result.response
    assert response.content == "Let me look at the notes."
    assert response.reasoning == "Need the file first."
    assert [(c.id, c.name) for c in response.tool_calls] == [("call_notes_1", "read_file")]
    assert response.tool_calls[0].arguments == '{"path":"notes.txt","limit":20}'
    assert response.finish_reason == "tool_calls"
    assert result.requests == 1 and not result.stub and result.streamed
    assert h.texts == "Let me look at the notes."


def test_stall_before_first_delta_reconnects() -> None:
    h = Harness(load_script("stall_then_text"))
    (result,) = h.run()
    assert isinstance(result, CallResult)
    assert result.response.content == "back online"
    assert len(h.server.model_requests) == 2 and result.attempts == 2
    first, second = h.server.model_requests
    assert first.at == 0.0
    # Stale at 180 s of fake time, then the first 1 s reconnect pause.
    assert second.at is not None and 181.0 <= second.at < 182.0
    assert h.streak.count == 0  # the answer reset the streak
    assert h.server.streams[0].closed_at is not None


def test_reconnect_pauses_and_budget() -> None:
    cut = [{"reasoning": "hm"}, {"disconnect": None}]
    h = Harness(script(cut, cut, cut, [{"text": "never"}]))
    (error,) = h.run()
    assert isinstance(error, httpx.RemoteProtocolError)
    times = [r.at for r in h.server.model_requests]
    assert len(times) == 3  # one attempt plus two reconnects
    assert times[1] == 1.0 and times[2] == 3.0  # 1 s, then 2 s, measured on the fake clock
    assert h.server.turns_left == 1


def test_stall_after_visible_text_returns_length_stub() -> None:
    h = Harness(
        script(
            [
                {"text": ["Here is ", "the plan"]},
                {"tool_call": {"name": "write_file", "arguments": {"path": "a"}, "fragments": 2}},
                {"stall": 500},
            ],
            [{"text": "must not be requested"}],
        )
    )
    (result,) = h.run()
    assert isinstance(result, CallResult)
    assert result.stub and not result.stub_terminal
    assert result.response.finish_reason == "length"
    assert result.response.content == "Here is the plan"
    assert result.response.tool_calls == ()
    assert result.dropped_tools == ("write_file",) and result.dropped_tool_count == 1
    assert result.error is not None and result.error.reason == "timeout"
    assert len(h.server.model_requests) == 1  # no retry once text was shown


def test_stub_names_at_most_three_dropped_tools() -> None:
    calls = [{"tool_call": {"name": f"tool_{i}"}} for i in range(5)]
    h = Harness(script([{"text": "doing it"}, *calls, {"disconnect": None}]))
    (result,) = h.run()
    assert result.stub
    assert result.dropped_tools == ("tool_0", "tool_1", "tool_2")
    assert result.dropped_tool_count == 5


def test_overflow_mid_stream_returns_empty_terminal_stub() -> None:
    overflow = {
        "error": {
            "message": "This model's maximum context length is 8192 tokens.",
            "code": "context_length_exceeded",
        }
    }
    h = Harness(script([{"text": "partial answer"}, {"mid_stream_error": overflow}]))
    (result,) = h.run()
    assert result.stub and result.stub_terminal
    assert result.response.content is None and result.response.finish_reason == "length"
    assert result.error.reason == "context_overflow"


def test_whitespace_only_text_is_not_visible() -> None:
    h = Harness(
        script([{"text": "  \n"}, {"disconnect": None}], [{"text": "ok"}, {"finish": "stop"}])
    )
    (result,) = h.run()
    assert not result.stub and result.response.content == "ok"
    assert result.requests == 2


def test_route_epoch_change_returns_route_changed() -> None:
    h = Harness(script([{"reasoning": "thinking"}, {"disconnect": None}], [{"text": "x"}]))

    def bump_on_first_event(event: Any) -> None:
        h.events.append(event)
        h.epoch += 1  # /model switched while the request was in flight

    async def go() -> Any:
        async with h.server.client() as client:
            return await h.drive(
                stream_call(
                    client,
                    h.transport,
                    h.route,
                    REQ,
                    policy=h.policy,
                    clock=h.clock,
                    on_event=bump_on_first_event,
                    route_epoch=lambda: h.epoch,
                )
            )

    result = asyncio.run(go())
    assert isinstance(result, RouteChanged)
    assert result.error is not None and result.error.reason == "connection"
    assert len(h.server.model_requests) == 1 and h.server.turns_left == 1


def test_breaker_trips_after_five_stalls() -> None:
    stall = [{"stall": 200}]
    h = Harness(script(*[stall] * 6))
    first, second, third = h.run(3)
    # Call 1: three silent attempts, then the last stale error.
    assert isinstance(first, ProviderStreamError)
    error_obj = first.body["error"]
    assert isinstance(error_obj, dict) and error_obj["code"] == "stale_stream"
    # Call 2: the fourth attempt is silent, a reconnect, the fifth trips the breaker.
    assert isinstance(second, StaleBreakerTripped)
    assert len(h.server.model_requests) == 5
    # Call 3: refused before any request.
    assert isinstance(third, StaleBreakerTripped)
    assert len(h.server.model_requests) == 5
    assert classify(third).reason == "timeout" and not classify(third).retryable


def test_breaker_disabled_with_zero() -> None:
    stall = [{"stall": 200}]
    h = Harness(script(*[stall] * 6), policy=TimeoutPolicy(breaker_threshold=0))
    results = h.run(2)
    assert all(isinstance(r, ProviderStreamError) for r in results)
    assert len(h.server.model_requests) == 6 and h.streak.count == 6


def test_keep_alive_only_server_flips_to_non_stream() -> None:
    h = Harness(
        script(
            [{"keep_alive": 3}, {"disconnect": None}],
            [
                {"text": "plain answer"},
                {"usage": {"prompt": 5, "completion": 2}},
                {"finish": "stop"},
            ],
            [{"text": "again"}, {"finish": "stop"}],
        )
    )
    first, second = h.run(2)
    assert isinstance(first, CallResult) and not first.streamed
    assert first.response.content == "plain answer"
    assert h.flags.streaming is False
    bodies = [r.body for r in h.server.model_requests]
    assert [b["stream"] for b in bodies] == [True, False, False]
    assert "stream_options" not in bodies[1]
    assert h.texts == "plain answeragain"  # non-stream answers still reach on_event
    assert second.response.content == "again" and second.requests == 1


def test_stream_unsupported_flips_to_non_stream() -> None:
    refusal = '{"error": {"message": "Streaming is not supported for this model."}}'
    h = Harness(
        script([{"fail": {"status": 400, "body": refusal}}], [{"text": "ok"}, {"finish": "stop"}])
    )
    (result,) = h.run()
    assert result.response.content == "ok" and not result.streamed
    assert h.flags.streaming is False


def test_stream_options_rejection_retried_once_without_it() -> None:
    rejection = json.dumps(
        {"error": {"message": "Unknown parameter: 'stream_options'.", "code": "unknown_parameter"}}
    )
    h = Harness(
        script(
            [{"fail": {"status": 400, "body": rejection}}],
            [{"text": "fine"}, {"finish": "stop"}],
        ),
        policy=TimeoutPolicy(stream_retries=0),  # the compatibility retry costs nothing
        # A fresh transport: it remembers the rejection per route.
        transport=type(get_transport("chat_completions"))(),
    )
    (result,) = h.run()
    assert isinstance(result, CallResult) and result.response.content == "fine"
    first, second = (r.body for r in h.server.model_requests)
    assert "stream_options" in first and "stream_options" not in second
    assert second["stream"] is True


def test_http_429_raises_with_retry_after_for_the_retry_loop() -> None:
    h = Harness(load_script("rate_limited"))
    (error,) = h.run()
    assert isinstance(error, ProviderHTTPError)
    assert error.status == 429 and error.retry_after == 2.0
    verdict = classify(error)
    assert verdict.reason == "rate_limit" and verdict.retry_after == 2.0
    assert len(h.server.model_requests) == 1  # status errors are not reconnected here


def test_read_timeout_raised_to_stale_value() -> None:
    h = Harness(load_script("hello"))
    h.run()
    timeout = h.server.model_requests[0].timeout
    assert timeout is not None and timeout["read"] == 180.0


class LateTransport:
    """Wraps a transport; on the first attempt it keeps yielding after being cancelled."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.api_mode = inner.api_mode
        self.streams = 0

    def build_request(self, route: Any, req: Any) -> httpx.Request:
        return self.inner.build_request(route, req)

    def parse_response(self, data: Any) -> Any:
        return self.inner.parse_response(data)

    async def parse_stream(self, events: AsyncIterator[Any]) -> AsyncIterator[Any]:
        self.streams += 1
        if self.streams > 1:
            async for event in self.inner.parse_stream(events):
                yield event
            return
        try:
            async for event in self.inner.parse_stream(events):
                yield event
        except asyncio.CancelledError:
            yield TextDelta("late one")
            yield TextDelta("late two")


def test_superseded_attempt_events_are_discarded() -> None:
    h = Harness(
        load_script("stall_then_text"), transport=LateTransport(get_transport("chat_completions"))
    )
    (result,) = h.run()
    assert isinstance(result, CallResult)
    assert result.discarded_events == 2
    assert "late" not in h.texts and h.texts == "back online"
    assert result.response.content == "back online"


def test_cancellation_closes_stream_and_raises() -> None:
    h = Harness(script([{"text": "streaming"}, {"stall": 1000}]))

    async def go() -> None:
        async with h.server.client() as client:
            task = asyncio.ensure_future(h.call(client))
            await h.clock.run_until_idle()
            assert h.texts == "streaming"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert h.server.streams[0].closed_at is not None
            assert h.clock.pending == 0  # watchdog and stall sleepers are gone

    asyncio.run(go())


@pytest.mark.slow
def test_cancellation_close_latency_under_200ms() -> None:
    latencies: list[float] = []
    for _ in range(20):
        h = Harness(script([{"text": "streaming"}, {"stall": 1000}]))

        async def go(h: Harness = h) -> float:
            async with h.server.client() as client:
                task = asyncio.ensure_future(h.call(client))
                await h.clock.run_until_idle()
                start = time.perf_counter()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                closed = h.server.streams[0].closed_at
                assert closed is not None
                return closed - start

        latencies.append(asyncio.run(go()))
    print(f"\n[P03-14] cancel -> stream closed: max {max(latencies) * 1000:.3f} ms over 20 runs")
    assert max(latencies) < 0.2


def test_non_stream_stale_timeout_aborts() -> None:
    h = Harness(script([{"stall": 100}, {"text": "late"}]))
    h.flags.streaming = False

    (error,) = h.run()
    assert isinstance(error, httpx.ReadTimeout)
    assert classify(error).reason == "timeout"
    timeout = h.server.model_requests[0].timeout
    assert timeout is not None and timeout["read"] == 120.0


def test_bug_in_consumer_propagates() -> None:
    h = Harness(load_script("hello"))

    def broken(event: Any) -> None:
        raise ValueError("consumer bug")

    async def go() -> None:
        async with h.server.client() as client:
            await stream_call(
                client,
                h.transport,
                h.route,
                REQ,
                policy=h.policy,
                clock=h.clock,
                on_event=broken,
                route_epoch=lambda: 0,
            )

    with pytest.raises(ValueError, match="consumer bug"):
        asyncio.run(go())


def test_async_on_event_is_awaited() -> None:
    h = Harness(load_script("hello"))
    seen: list[str] = []

    async def consumer(event: Any) -> None:
        if isinstance(event, TextDelta):
            seen.append(event.text)

    async def go() -> Any:
        async with h.server.client() as client:
            return await stream_call(
                client,
                h.transport,
                h.route,
                REQ,
                policy=h.policy,
                clock=h.clock,
                on_event=consumer,
                route_epoch=lambda: 0,
            )

    result = asyncio.run(go())
    assert "".join(seen) == "hello" and result.response.content == "hello"


@pytest.mark.parametrize("api_mode", ["chat_completions", "anthropic_messages"])
def test_both_wire_formats_through_the_driver(api_mode: str) -> None:
    h = Harness(load_script("stall_then_text"), api_mode=api_mode)
    (result,) = h.run()
    assert result.response.content == "back online" and result.requests == 2
