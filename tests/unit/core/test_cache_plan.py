"""Prompt-cache markers (P08-07): policy, layouts, placement, TTL, and the wire bodies."""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import httpx
import pytest

from indus.core import cache_plan
from indus.core.agent import BoundCaller
from indus.core.route import CachePolicy, RouteState
from indus.core.wire import finish_request
from indus.providers.context_length import ContextWindow
from indus.providers.route import RuntimeRoute, Secret
from indus.providers.streaming import RouteChanged
from indus.providers.transports.anthropic import AnthropicTransport
from indus.providers.transports.chat_completions import ChatCompletionsTransport
from indus.providers.types import ChatRequest

EPH = {"type": "ephemeral"}
EPH_1H = {"type": "ephemeral", "ttl": "1h"}
PREFIX = "You are a careful assistant."
SYSTEM = PREFIX + "\n\nToday is a day.\nModel: claude-x"


def history() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "read a.txt"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "file text"},
        {"role": "user", "content": "thanks"},
    ]


def request(messages: list[dict[str, Any]] | None = None, **kw: Any) -> ChatRequest:
    return ChatRequest(model="claude-x", messages=messages or history(), **kw)


def anthropic_body(req: ChatRequest) -> dict[str, Any]:
    route = RuntimeRoute("anthropic", "claude-x", "anthropic_messages", "https://api.anthropic.com")
    return json.loads(AnthropicTransport().build_request(route, req).content)


def chat_body(req: ChatRequest) -> dict[str, Any]:
    route = RuntimeRoute(
        "openrouter", "anthropic/claude-x", "chat_completions", "https://o.invalid"
    )
    return json.loads(ChatCompletionsTransport().build_request(route, req).content)


# -- policy ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("api_mode", "model", "host", "entry", "expected"),
    [
        ("anthropic_messages", "claude-sonnet-4-5", "api.anthropic.com", {}, (True, "wire")),
        ("chat_completions", "anthropic/claude-sonnet-4.5", "openrouter.ai", {}, (True, "wire")),
        ("chat_completions", "anthropic/claude-sonnet-4.5", "other.example", {}, (False, "wire")),
        ("anthropic_messages", "MiniMax-M2", "api.minimax.io", {}, (False, "wire")),
        ("chat_completions", "gpt-5", "openrouter.ai", {}, (False, "wire")),
        ("chat_completions", "qwen3", "x.example", {"prompt_caching": True}, (True, "config")),
        (
            "anthropic_messages",
            "claude-x",
            "api.anthropic.com",
            {"prompt_caching": False},
            (False, "config"),
        ),
    ],
)
def test_policy_table(
    api_mode: str, model: str, host: str, entry: dict[str, Any], expected: tuple[bool, str]
) -> None:
    got = cache_plan.decide(api_mode=api_mode, model=model, host=host, profile=None, entry=entry)
    assert got == expected


def test_profile_can_declare_support() -> None:
    class Declares:
        prompt_cache = True

    got = cache_plan.decide(
        api_mode="chat_completions", model="m", host="h", profile=Declares(), entry={}
    )
    assert got == (True, "profile")


@pytest.mark.parametrize(
    ("setting", "source", "expected"),
    [
        (None, "cli", "5m"),
        ("5m", "cron", "5m"),
        ("1h", "cron", "1h"),
        ("auto", "cli", "1h"),
        ("auto", "tui", "1h"),
        ("auto", "telegram", "1h"),
        ("auto", "subagent", "5m"),
        ("auto", "cron", "5m"),
        ("auto", "oneshot", "5m"),
        ("auto", "webhook", "5m"),
        ("auto", "api_server", "5m"),
        ("auto", "batch", "5m"),
        ("AUTO", "", "1h"),
        ("2h", "cli", "5m"),
    ],
)
def test_auto_ttl_by_source(setting: str | None, source: str, expected: str) -> None:
    assert cache_plan.resolve_ttl(setting, source) == expected


def test_cache_hit_percent() -> None:
    assert cache_plan.cache_hit_percent(0, 0) is None
    assert cache_plan.cache_hit_percent(1000, 750) == 75.0
    assert cache_plan.cache_hit_percent(10, 50) == 100.0


# -- layouts (golden bodies) -----------------------------------------------------------------------


def test_golden_native_static_prefix() -> None:
    planned = finish_request(request(), markers=True, native=True, static_prefix=PREFIX)
    body = anthropic_body(planned)
    assert body["system"] == [
        {"type": "text", "text": PREFIX, "cache_control": EPH},
        {"type": "text", "text": "\n\nToday is a day.\nModel: claude-x", "cache_control": EPH},
    ]
    assert body["messages"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": "read a.txt"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "c1", "name": "read", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "c1",
                    "cache_control": EPH,
                    "content": "file text",
                },
                {"type": "text", "text": "thanks", "cache_control": EPH},
            ],
        },
    ]
    assert cache_plan.count_markers(planned.messages) == 4


def test_golden_native_system_and_three() -> None:
    planned = finish_request(request(), markers=True, ttl_setting="1h", native=True)
    body = anthropic_body(planned)
    assert body["system"] == [{"type": "text", "text": SYSTEM, "cache_control": EPH_1H}]
    assert body["messages"][3] == {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "c1", "name": "read", "input": {}, "cache_control": EPH_1H}
        ],
    }
    assert body["messages"][4]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "c1",
            "cache_control": EPH_1H,
            "content": "file text",
        },
        {"type": "text", "text": "thanks", "cache_control": EPH_1H},
    ]
    assert body["messages"][2] == {"role": "user", "content": "read a.txt"}
    assert cache_plan.count_markers(planned.messages) == 4


def test_golden_aggregator_body() -> None:
    planned = finish_request(request(), markers=True, native=False)
    body = chat_body(planned)
    msgs = body["messages"]
    assert msgs[0]["content"] == [{"type": "text", "text": SYSTEM, "cache_control": EPH}]
    # the empty assistant turn cannot carry a part marker: the budget moved to an older message
    assert "cache_control" not in msgs[4] and msgs[4]["content"] == ""
    assert msgs[5]["content"] == [{"type": "text", "text": "file text", "cache_control": EPH}]
    assert msgs[6]["content"] == [{"type": "text", "text": "thanks", "cache_control": EPH}]
    assert msgs[3]["content"] == [{"type": "text", "text": "read a.txt", "cache_control": EPH}]
    assert msgs[2]["content"] == "ok"
    assert sum(json.dumps(m).count("cache_control") for m in msgs) == 4


def test_aggregator_skips_empty_messages() -> None:
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]},
        {"role": "tool", "tool_call_id": "x", "content": ""},
        {"role": "user", "content": "b"},
    ]
    planned = cache_plan.plan_messages(msgs, ttl="5m", native=False)
    assert "cache_control" not in planned[2] and "cache_control" not in planned[3]
    assert planned[1]["content"][0]["cache_control"] == EPH
    assert planned[4]["content"][0]["cache_control"] == EPH


def test_tool_message_placement_per_wire() -> None:
    msgs = [{"role": "user", "content": "q"}, {"role": "tool", "tool_call_id": "t", "content": "r"}]
    native = cache_plan.plan_messages(msgs, ttl="5m", native=True)
    assert native[1] == {"role": "tool", "tool_call_id": "t", "content": "r", "cache_control": EPH}
    agg = cache_plan.plan_messages(msgs, ttl="5m", native=False)
    assert agg[1]["content"] == [{"type": "text", "text": "r", "cache_control": EPH}]
    assert "cache_control" not in agg[1]


def test_list_content_marks_last_part() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    msgs = [{"role": "user", "content": [{"type": "text", "text": "see"}, image]}]
    planned = cache_plan.plan_messages(msgs, ttl="5m", native=True)
    assert planned[0]["content"][0] == {"type": "text", "text": "see"}
    assert planned[0]["content"][1]["cache_control"] == EPH
    body = anthropic_body(ChatRequest(model="claude-x", messages=planned))
    assert body["messages"][0]["content"][1]["cache_control"] == EPH
    assert body["messages"][0]["content"][1]["type"] == "image"


def test_prefix_equal_to_whole_prompt_uses_system_and_three() -> None:
    msgs = [{"role": "system", "content": PREFIX}, {"role": "user", "content": "x"}]
    planned = cache_plan.plan_messages(msgs, ttl="5m", native=True, static_prefix=PREFIX)
    assert planned[0]["content"] == [{"type": "text", "text": PREFIX, "cache_control": EPH}]


# -- invariants ------------------------------------------------------------------------------------


def _random_messages(rng: random.Random) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if rng.random() < 0.8:
        out.append({"role": "system", "content": rng.choice([SYSTEM, PREFIX, "", "s"])})
    for _ in range(rng.randint(0, 12)):
        role = rng.choice(["user", "assistant", "tool"])
        content = rng.choice(
            [
                "",
                None,
                "text",
                [{"type": "text", "text": "a"}],
                [{"type": "text", "text": "a", "cache_control": EPH}],
                [],
            ]
        )
        message: dict[str, Any] = {"role": role, "content": content}
        if rng.random() < 0.2:
            message["cache_control"] = EPH
        out.append(message)
    return out


@pytest.mark.parametrize("native", [True, False])
def test_never_more_than_four_markers(native: bool) -> None:
    rng = random.Random(8007)
    for _ in range(600):
        msgs = _random_messages(rng)
        frozen = json.dumps(msgs, sort_keys=True)
        prefix = PREFIX if rng.random() < 0.5 else None
        planned = cache_plan.plan_messages(msgs, ttl="5m", native=native, static_prefix=prefix)
        assert cache_plan.count_markers(planned) <= cache_plan.MAX_MARKERS
        assert json.dumps(msgs, sort_keys=True) == frozen  # input untouched
        assert cache_plan.count_markers(cache_plan.strip_markers(planned)) == 0


def test_replan_is_idempotent() -> None:
    once = cache_plan.plan_messages(history(), ttl="5m", native=True, static_prefix=PREFIX)
    twice = cache_plan.plan_messages(once, ttl="5m", native=True, static_prefix=PREFIX)
    # a second plan over planned messages never adds markers (the split system stays split)
    assert cache_plan.count_markers(twice) <= 4


def test_unsupported_route_has_none() -> None:
    marked = cache_plan.plan_messages(history(), ttl="5m", native=True)
    tools = [{"type": "function", "function": {"name": "t"}, "cache_control": EPH}]
    req = ChatRequest(model="m", messages=marked, tools=tools)
    out = finish_request(req, markers=False)
    assert cache_plan.count_markers(out.messages) == 0
    assert "cache_control" not in json.dumps(chat_body(out))
    assert cache_plan.count_markers(req.messages) == 4  # the caller's request is unchanged


def test_transcript_rows_never_change() -> None:
    rows = history()
    frozen = json.dumps(rows)
    finish_request(request(rows), markers=True, native=True, static_prefix=PREFIX)
    assert json.dumps(rows) == frozen


# -- per destination (BoundCaller) -----------------------------------------------------------------


class _Binding:
    def __init__(self, state: RouteState) -> None:
        self.state = state
        self.generation = 0


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def call(self, request: ChatRequest, **_: Any) -> RouteChanged:
        self.requests.append(request)
        return RouteChanged(None, 0, 0)


def _state(api_mode: str, model: str, cache: CachePolicy) -> RouteState:
    route = RuntimeRoute("p", model, api_mode, "https://x.invalid", Secret("k" * 12))
    return RouteState(
        route=route,
        client=httpx.AsyncClient(),
        window=ContextWindow(200_000, "test"),
        reasoning_effort=None,
        requested_effort=None,
        extra_body={},
        cache=cache,
    )


def test_plan_rebuilt_for_the_route_that_is_bound_now() -> None:
    """A fallback route is a new bound state: the next request is planned for it, not reused."""
    primary = _state("anthropic_messages", "claude-x", CachePolicy(True, "wire", "auto"))
    fallback = _state("chat_completions", "claude-x", CachePolicy(False, "wire"))
    binding = _Binding(primary)
    recorder = _Recorder()
    caller = BoundCaller(binding, clock=None, source="cron", static_prefix=lambda: PREFIX)  # type: ignore[arg-type]
    caller.caller_for = lambda state, generation: recorder  # type: ignore[method-assign,assignment]

    async def go() -> None:
        await caller.call(request(), on_event=lambda e: None)
        binding.state = fallback
        await caller.call(request(), on_event=lambda e: None)
        await primary.client.aclose()
        await fallback.client.aclose()

    asyncio.run(go())
    first, second = recorder.requests
    assert cache_plan.count_markers(first.messages) == 4
    assert first.messages[0]["content"][0]["cache_control"] == EPH  # cron → auto = 5m
    assert cache_plan.count_markers(second.messages) == 0
