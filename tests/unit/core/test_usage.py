"""Usage normalisation, the session ledger and the recording caller (P08-08, B24)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from indus.core.usage import (
    CanonicalUsage,
    RateLimitBucket,
    RecordingCaller,
    UsageLedger,
    normalize,
    parse_rate_limits,
    parse_reset,
)
from indus.foundation.scope import Scope
from indus.providers.route import RuntimeRoute
from indus.providers.sse import SSEDecoder
from indus.providers.streaming import CallResult, RouteChanged
from indus.providers.transports import get_transport
from indus.providers.types import ChatRequest, NormalizedResponse, Usage, assemble
from indus.state import sessions
from indus.state import usage as usage_rows
from indus.state.registry import open_unshared
from indus.state.usage import UsageDelta
from tests.fakes.clock import FakeClock
from tests.fakes.model.replay import capture_chunks, load_capture

ANTHROPIC = RuntimeRoute(
    "anthropic", "claude-haiku-4-5", "anthropic_messages", "https://api.anthropic.com"
)
PROXY = RuntimeRoute("custom", "claude-haiku-4-5", "chat_completions", "https://proxy.example/v1")


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 18, 9, 0, tzinfo=UTC))


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def test_normalise_per_wire_format() -> None:
    rows: dict[str, tuple[str, dict[str, Any], tuple[int, int, int, int, int]]] = {
        "chat": (
            "chat_completions",
            {
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 600},
                "completion_tokens_details": {"reasoning_tokens": 20},
            },
            (400, 50, 600, 0, 20),
        ),
        "chat from a server using input/output names": (
            "chat_completions",
            {"input_tokens": 300, "output_tokens": 9, "cache_read_input_tokens": 100},
            (200, 9, 100, 0, 0),
        ),
        "chat with the DeepSeek hit field": (
            "chat_completions",
            {"prompt_tokens": 1000, "completion_tokens": 5, "prompt_cache_hit_tokens": 900},
            (100, 5, 900, 0, 0),
        ),
        "anthropic": (
            "anthropic_messages",
            {
                "input_tokens": 30,
                "output_tokens": 7,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 200,
            },
            (30, 7, 500, 200, 0),
        ),
        "responses": (
            "responses",
            {
                "input_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 300, "cache_write_tokens": 100},
                "output_tokens": 80,
                "output_tokens_details": {"reasoning_tokens": 60},
            },
            (600, 80, 300, 100, 60),
        ),
        "bedrock": (
            "bedrock_converse",
            {
                "inputTokens": 12,
                "outputTokens": 3,
                "cacheReadInputTokens": 400,
                "cacheWriteInputTokens": 50,
            },
            (12, 3, 400, 50, 0),
        ),
        "gemini": (
            "gemini",
            {
                "promptTokenCount": 2000,
                "cachedContentTokenCount": 1500,
                "candidatesTokenCount": 40,
                "thoughtsTokenCount": 60,
            },
            (500, 100, 1500, 0, 60),
        ),
    }
    for name, (mode, raw, expected) in rows.items():
        got = normalize(raw, mode)
        assert (
            got.input_tokens,
            got.output_tokens,
            got.cache_read_tokens,
            got.cache_write_tokens,
            got.reasoning_tokens,
        ) == expected, name
        assert (
            got.prompt_tokens == got.input_tokens + got.cache_read_tokens + got.cache_write_tokens
        )


def test_bad_counters_become_zero_and_cache_never_goes_negative() -> None:
    odd = normalize(
        {
            "prompt_tokens": 10,
            "completion_tokens": -3,
            "prompt_tokens_details": {"cached_tokens": 50},
        },
        "chat_completions",
    )
    assert (odd.input_tokens, odd.output_tokens, odd.cache_read_tokens) == (0, 0, 50)
    assert (
        normalize({"input_tokens": "12", "output_tokens": True}, "anthropic_messages")
        == CanonicalUsage()
    )


def _from_capture(name: str) -> tuple[CanonicalUsage | None, dict[str, Any]]:
    capture = load_capture(name)
    mode = capture["meta"]["api_mode"]
    transport = get_transport(mode)
    decoder = SSEDecoder()
    raw_usage: dict[str, Any] = {}
    items = []
    for chunk in capture_chunks(capture):
        items.extend(decoder.feed(chunk))
    items.extend(decoder.flush())
    for item in items:
        data = getattr(item, "data", "")
        try:
            frame = json.loads(data)
        except (TypeError, ValueError):
            continue
        for candidate in (frame.get("usage"), (frame.get("message") or {}).get("usage")):
            if isinstance(candidate, dict):
                raw_usage.update(candidate)

    async def events() -> list[Any]:
        async def gen():  # type: ignore[no-untyped-def]
            for item in items:
                yield item

        return [e async for e in transport.parse_stream(gen())]

    response = assemble(run(events()))
    return CanonicalUsage.from_usage(response.usage), raw_usage


@pytest.mark.parametrize(
    "name",
    ["chat_completions/text", "chat_completions/tool_call", "anthropic_messages/text"],
)
def test_capture_usage_matches_raw_normalisation(name: str) -> None:
    canonical, raw = _from_capture(name)
    assert canonical is not None
    mode = name.split("/")[0]
    direct = normalize(raw, mode)
    assert canonical.prompt_tokens == direct.prompt_tokens
    assert canonical.output_tokens == direct.output_tokens
    assert canonical.cache_read_tokens == direct.cache_read_tokens


def test_missing_usage_still_counts_the_call(caplog: pytest.LogCaptureFixture) -> None:
    ledger = UsageLedger(clock=_clock())
    with caplog.at_level(logging.INFO, logger="indus.core.usage"):
        record = run(ledger.record(None, route=ANTHROPIC, latency_s=1.5))
    snap = ledger.snapshot()
    assert snap.api_calls == 1 and snap.usage.total_tokens == 0
    assert record.cost.status == "unknown" and snap.cost_status == "unknown"
    assert record.delta.api_calls == 1
    assert "usage=missing" in caplog.text


def test_ledger_counters_history_and_info_line(caplog: pytest.LogCaptureFixture) -> None:
    ledger = UsageLedger(clock=_clock())
    usage = Usage(
        prompt_tokens=1000, completion_tokens=100, cache_read_tokens=800, cache_write_tokens=100
    )
    with caplog.at_level(logging.INFO, logger="indus.core.usage"):
        for n in range(12):
            run(ledger.record(usage, route=ANTHROPIC, latency_s=float(n), response_id=f"msg_{n}"))
    snap = ledger.snapshot()
    assert snap.api_calls == 12
    assert snap.usage.input_tokens == 12 * 100 and snap.usage.cache_read_tokens == 12 * 800
    assert snap.latencies_s == tuple(float(n) for n in range(2, 12))
    assert len(snap.output_sizes) == 10
    assert snap.cost_status == "estimated" and snap.cost_usd > 0
    assert snap.last_prompt_tokens == 1000
    line = caplog.records[-1].getMessage()
    for part in (
        "in=1000",
        "out=100",
        "total=1100",
        "latency=11.0s",
        "cache=800/1000 (80%)",
        "cache_write=100",
        "id=msg_11",
    ):
        assert part in line


def test_proxy_route_is_unknown_and_partial_label() -> None:
    ledger = UsageLedger(clock=_clock())
    usage = Usage(prompt_tokens=1_000_000)
    run(ledger.record(usage, route=ANTHROPIC, latency_s=0.1))
    run(ledger.record(usage, route=PROXY, latency_s=0.1))
    snap = ledger.snapshot()
    assert snap.cost_status == "unknown" and snap.unpriced_calls == 1
    assert snap.cost_label == "$1.00+ (partial)"
    assert snap.as_dict()["cost_usd"] == 1.0


def test_side_call_tagged_and_kept_out_of_main_counters() -> None:
    seen: list[tuple[UsageDelta, str]] = []
    ledger = UsageLedger(clock=_clock(), store=lambda d, t: seen.append((d, t)))
    run(
        ledger.record(
            Usage(prompt_tokens=10, completion_tokens=2),
            route=ANTHROPIC,
            latency_s=0.2,
            task="title",
        )
    )
    snap = ledger.snapshot()
    assert snap.api_calls == 0 and snap.side_calls == {"title": 1}
    delta, task = seen[0]
    assert task == "title" and delta.input_tokens == 10 and delta.model == "claude-haiku-4-5"
    assert delta.billing_provider == "anthropic" and delta.cost_status == "estimated"


def test_listeners_hear_every_answered_call_and_failures_are_contained() -> None:
    ledger = UsageLedger(clock=_clock())
    got: list[tuple[int | None, str]] = []
    ledger.add_listener(lambda u, t: got.append((None if u is None else u.prompt_tokens, t)))

    async def later(usage: CanonicalUsage | None, task: str) -> None:
        got.append((-1, task))

    def broken(usage: CanonicalUsage | None, task: str) -> None:
        raise RuntimeError("listener bug")

    ledger.add_listener(broken)
    ledger.add_listener(later)
    run(ledger.record(Usage(prompt_tokens=42), route=ANTHROPIC, latency_s=0.0))
    run(ledger.record(None, route=ANTHROPIC, latency_s=0.0, task="title"))
    # usage-less answers reach listeners too (the usage anchor counts them, P08-01)
    assert got == [(42, ""), (-1, ""), (None, "title"), (-1, "title")]


def test_store_failure_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    def failing(delta: UsageDelta, task: str) -> None:
        raise OSError("disk full")

    ledger = UsageLedger(clock=_clock(), session_id="s1", store=failing)
    run(ledger.record(Usage(prompt_tokens=1), route=ANTHROPIC, latency_s=0.0))
    assert "was not stored" in caplog.text


def test_deltas_land_in_the_store_with_task_tags(scope: Scope) -> None:
    store = open_unshared(scope)
    sessions.create(store, "20261018_000000_abcdef", source="cli")

    async def write(delta: UsageDelta, task: str) -> None:
        usage_rows.record(store, "20261018_000000_abcdef", delta, task=task)

    ledger = UsageLedger(clock=scope.clock, session_id="20261018_000000_abcdef", store=write)
    run(
        ledger.record(
            Usage(prompt_tokens=100, completion_tokens=10), route=ANTHROPIC, latency_s=0.0
        )
    )
    run(
        ledger.record(
            Usage(prompt_tokens=7, completion_tokens=3),
            route=ANTHROPIC,
            latency_s=0.0,
            task="title",
        )
    )
    totals = usage_rows.totals(store, "20261018_000000_abcdef")
    assert (totals.input_tokens, totals.output_tokens, totals.api_calls) == (100, 10, 1)
    assert totals.estimated_cost_usd > 0
    tasks = {
        row.task: row.input_tokens for row in usage_rows.by_model(store, "20261018_000000_abcdef")
    }
    store.close()
    assert tasks == {"": 100, "title": 7}


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("12", 12.0),
        ("0.5", 0.5),
        ("1s", 1.0),
        ("6m0s", 360.0),
        ("1h2m3.5s", 3723.5),
        ("20ms", 0.02),
        ("", None),
        ("soon", None),
        ("5x", None),
        ("-3", None),
    ],
)
def test_parse_reset(text: str, seconds: float | None) -> None:
    assert parse_reset(text) == seconds


def test_rate_limit_headers_parsed() -> None:
    headers = {
        "x-ratelimit-limit-requests": "500",
        "X-RateLimit-Remaining-Requests": "499",
        "x-ratelimit-reset-requests": "120ms",
        "x-ratelimit-limit-tokens": "30000",
        "x-ratelimit-remaining-tokens": "29000",
        "x-ratelimit-reset-tokens": "2s",
        "x-ratelimit-limit-requests-1h": "9000",
        "x-ratelimit-remaining-tokens-junk": "lots",
        "content-type": "application/json",
    }
    limits = parse_rate_limits(headers)
    assert limits["requests"] == RateLimitBucket(500, 499, 0.12)
    assert limits["tokens"] == RateLimitBucket(30000, 29000, 2.0)
    assert limits["requests-1h"] == RateLimitBucket(limit=9000)
    assert "tokens-junk" not in limits
    ledger = UsageLedger(clock=_clock())
    run(ledger.record(None, route=ANTHROPIC, latency_s=0.0, headers=headers))
    assert ledger.snapshot().rate_limits["tokens"].remaining == 29000


class _Scripted:
    model = "claude-haiku-4-5"

    def __init__(self, clock: FakeClock, outcome: Any, seconds: float) -> None:
        self.clock, self.outcome, self.seconds = clock, outcome, seconds

    async def call(self, request: ChatRequest, *, on_event: Any, on_wait: Any = None) -> Any:
        self.clock.advance(self.seconds)
        return self.outcome


def test_recording_caller_records_answers_only() -> None:
    clock = _clock()
    ledger = UsageLedger(clock=clock)
    answer = CallResult(
        NormalizedResponse(
            "hi",
            usage=Usage(prompt_tokens=20, completion_tokens=3),
            provider_data={"id": "resp_1", "service_tier": "default"},
        )
    )
    caller = RecordingCaller(_Scripted(clock, answer, 2.5), ledger, lambda: ANTHROPIC)
    request = ChatRequest(model="claude-haiku-4-5", messages=[])
    assert run(caller.call(request, on_event=lambda e: None)) is answer
    moved = RecordingCaller(
        _Scripted(clock, RouteChanged(None, 0, 0), 1.0), ledger, lambda: ANTHROPIC
    )
    run(moved.call(request, on_event=lambda e: None))
    snap = ledger.snapshot()
    assert snap.api_calls == 1 and snap.latencies_s == (2.5,)
    assert snap.cost_status == "estimated"
    assert caller.model == "claude-haiku-4-5"


def test_snapshot_dict_for_usage_file() -> None:
    ledger = UsageLedger(clock=_clock())
    run(
        ledger.record(
            Usage(prompt_tokens=10, completion_tokens=5),
            route=RuntimeRoute("lmstudio", "m", "chat_completions", "http://127.0.0.1:1234/v1"),
            latency_s=0.0,
        )
    )
    data = ledger.snapshot().as_dict()
    assert data["cost_status"] == "included" and data["cost_usd"] == 0.0
    assert data["total_tokens"] == 15 and data["api_calls"] == 1
    assert isinstance(ledger.snapshot().cost_usd, Decimal)
