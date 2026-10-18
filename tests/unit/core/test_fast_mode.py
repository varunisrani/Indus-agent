"""Fast mode (P08-15): the window, the route gate, the request body, the served tier, ``/fast``."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from indus.cli.slash.context import RouteInfo
from indus.cli.slash.dispatch import dispatch
from indus.core import fast_mode
from indus.core.agent import Agent
from indus.core.fast_mode import FastMode, FastTier, ModeTable, parse_mode, served, tier_for
from indus.core.usage import CanonicalUsage
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.providers import bundled
from indus.providers.pricing import estimate_cost
from indus.providers.profile import ProviderProfile
from indus.providers.route import RuntimeRoute
from indus.providers.streaming import CallResult
from indus.providers.types import NormalizedResponse
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import DictConfig, fake_options, run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import (
    FakeModelServer,
    Finish,
    Text,
    ToolCall,
    Turn,
    fake_route,
    script_of,
    text_turn,
)
from tests.fakes.model import Usage as UsageStep
from tests.fakes.tools import FakeTools
from tests.unit.cli.slash._fakes import FakePort, make_ctx, route_of

FAST_BODY = {"service_tier": "priority"}


def fast_profile(base_url: str, **changes: Any) -> ProviderProfile:
    fields: dict[str, Any] = {
        "name": "quickvendor",
        "base_url": base_url,
        "env_vars": ("QUICK_KEY",),
        "fast_body": FAST_BODY,
        "fast_models": ("fake-model-1",),
    }
    fields.update(changes)
    return ProviderProfile(**fields)


def fast_route(
    server: FakeModelServer, api_mode: str = "chat_completions", **changes: Any
) -> RuntimeRoute:
    route = fake_route(server, api_mode)
    return replace(route, profile=fast_profile(route.base_url, **changes))


def with_mode(scope: Scope, mode: str, window: float | None = None) -> Scope:
    agent: dict[str, Any] = {"fast_mode": mode}
    if window is not None:
        agent["fast_window_s"] = window
    return scope.with_config(DictConfig({"agent": agent}))


def tool_turn(seconds: float) -> Turn:
    return Turn((ToolCall("slow", {"seconds": seconds}, "call_slow"), Finish("tool_calls")))


def run_turns(
    scope: Scope, clock: FakeClock, server: FakeModelServer, route: RuntimeRoute, *texts: str
) -> Agent:
    tools = FakeTools(clock)
    registry = tools.register(ToolRegistry(clock=clock))

    async def main() -> Agent:
        agent = await Agent.start(scope, route, ["fake"], fake_options(server, registry=registry))
        for text in texts:
            await agent.run_turn(text)
        return agent

    agent = asyncio.run(run_driven(main(), clock))
    asyncio.run(agent.close())
    return agent


def tiers(server: FakeModelServer) -> list[object]:
    return [r.body.get("service_tier") for r in server.model_requests]


# -- words and the window -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("word", "mode"),
    [
        ("normal", "normal"),
        ("OFF", "normal"),
        ("fast", "fast"),
        ("on", "fast"),
        ("priority", "fast"),
        (" auto ", "auto"),
        ("turbo", None),
        (None, None),
        (True, "fast"),
    ],
)
def test_mode_words(word: object, mode: str | None) -> None:
    assert parse_mode(word) == mode


WINDOWS = [(30, 30.0), ("45", 45.0), (0, 60.0), (-5, 60.0), ("soon", 60.0), (None, 60.0),
           (True, 60.0)]  # fmt: skip


@pytest.mark.parametrize(("raw", "seconds"), WINDOWS)
def test_window_setting(raw: object, seconds: float) -> None:
    assert fast_mode.window_seconds(raw) == seconds


def test_window_opens_per_turn_and_expires(scope: Scope, fake_clock: FakeClock) -> None:
    fast = FastMode(with_mode(scope, "auto"), lambda: "s1", table=ModeTable())
    assert not fast.active()  # no turn yet
    fast.begin_turn()
    fake_clock.advance(59.9)
    assert fast.active() and fast.remaining() == pytest.approx(0.1)
    fake_clock.advance(0.1)
    assert not fast.active() and fast.remaining() == 0.0  # the window is half-open
    fast.begin_turn()
    assert fast.active()


def test_fast_and_normal_need_no_window(scope: Scope, fake_clock: FakeClock) -> None:
    table = ModeTable()
    fast = FastMode(with_mode(scope, "normal"), lambda: "s1", table=table)
    fast.begin_turn()
    assert not fast.active()
    fast.set_mode("fast")
    fake_clock.advance(10_000)
    assert fast.active() and fast.mode == "fast"
    assert table.get((str(scope.home), "s1")) == "fast"


# -- the route gate -------------------------------------------------------------------------------


def test_only_declaring_profiles_on_their_own_host(fake_clock: FakeClock) -> None:
    server = FakeModelServer(script_of(), clock=fake_clock)
    good = fast_route(server)
    assert tier_for(good) == FastTier(FAST_BODY, {})
    plain = fake_route(server)
    assert tier_for(plain) is None
    assert "does not offer" in (fast_mode.unsupported_reason(plain) or "")
    proxy = replace(good, base_url="https://proxy.example/v1")
    assert tier_for(proxy) is None
    assert "only sent to" in (fast_mode.unsupported_reason(proxy) or "")
    other_model = replace(good, model="small-model")
    assert "no priority tier" in (fast_mode.unsupported_reason(other_model) or "")
    custom = replace(good, profile=fast_profile(good.base_url, family="custom"))
    assert tier_for(custom) is None


def test_bundled_declarations() -> None:
    anthropic = next(p for p in bundled.PROFILES if p.name == "anthropic")
    route = RuntimeRoute("anthropic", "claude-opus-5-5", "anthropic_messages",
                         "https://api.anthropic.com", profile=anthropic)  # fmt: skip
    tier = tier_for(route)
    assert tier is not None and tier.body == {"speed": "fast"}
    assert tier.headers == {"anthropic-beta": "fast-mode-2026-02-01"}
    assert tier_for(replace(route, model="claude-sonnet-5-5")) is None
    for model in ("claude-opus-5", "claude-opus-4-8"):  # the vendor's list, exact ids
        assert tier_for(replace(route, model=model)) is not None
    assert tier_for(replace(route, model="claude-opus-4-6")) is None
    openai = next(p for p in bundled.PROFILES if p.name == "openai")
    route = RuntimeRoute("openai", "gpt-5.2", "responses", "https://api.openai.com/v1",
                         profile=openai)  # fmt: skip
    assert tier_for(route) == FastTier({"service_tier": "priority"}, {})
    assert tier_for(replace(route, model="GPT-4.1-mini")) is not None  # any case
    for model in ("gpt-5-nano", "gpt-5.4-nano", "o3-mini", "gpt-5.1-codex-max"):
        assert tier_for(replace(route, model=model)) is None  # not in the vendor's table


# -- requests through a real agent ----------------------------------------------------------------


def test_tier_only_inside_the_window(scope: Scope, fake_clock: FakeClock) -> None:
    server = FakeModelServer(
        script_of(tool_turn(70.0), text_turn("done"), text_turn("second")), clock=fake_clock
    )
    run_turns(with_mode(scope, "auto"), fake_clock, server, fast_route(server), "go", "again")
    # turn 1: first request inside the window, the one after a 70 s tool outside it;
    # turn 2 opens a fresh window
    assert tiers(server) == ["priority", None, "priority"]


def test_fast_mode_on_every_request(scope: Scope, fake_clock: FakeClock) -> None:
    server = FakeModelServer(script_of(tool_turn(500.0), text_turn("done")), clock=fake_clock)
    run_turns(with_mode(scope, "fast"), fake_clock, server, fast_route(server), "go")
    assert tiers(server) == ["priority", "priority"]


def test_normal_mode_sends_nothing(scope: Scope, fake_clock: FakeClock) -> None:
    server = FakeModelServer(script_of(text_turn("hi")), clock=fake_clock)
    run_turns(scope, fake_clock, server, fast_route(server), "go")
    assert tiers(server) == [None]


def test_unsupported_route_is_left_alone_with_one_notice(
    scope: Scope, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeModelServer(script_of(tool_turn(1.0), text_turn("done")), clock=fake_clock)
    with caplog.at_level(logging.INFO, logger="indus.core.fast_mode"):
        run_turns(with_mode(scope, "fast"), fake_clock, server, fake_route(server), "go")
    assert tiers(server) == [None, None]
    notes = [r for r in caplog.records if "fast mode:" in r.getMessage()]
    assert len(notes) == 1 and "does not offer a priority tier" in notes[0].getMessage()


def test_session_choice_beats_the_config_and_explicit_fields_win(
    scope: Scope, fake_clock: FakeClock
) -> None:
    server = FakeModelServer(script_of(text_turn("a"), text_turn("b")), clock=fake_clock)
    configured = scope.with_config(
        DictConfig({"providers": {"fake": {"extra_body": {"service_tier": "flex"}}}})
    )
    fast_mode.FAST_MODES.set(fast_mode.session_key(configured, "s1"), "fast")
    try:
        run_turns(configured, fake_clock, server, fast_route(server), "one", "two")
    finally:
        fast_mode.FAST_MODES.clear(fast_mode.session_key(configured, "s1"))
    assert tiers(server) == ["flex", "flex"]  # a field the request already sets is kept


def test_headers_join_an_existing_beta_list(scope: Scope, fake_clock: FakeClock) -> None:
    server = FakeModelServer(script_of(text_turn("hi")), clock=fake_clock)
    route = fast_route(
        server,
        "anthropic_messages",
        fast_body={"speed": "fast"},
        fast_headers={"anthropic-beta": "fast-mode-2026-02-01"},
    )
    route = replace(route, headers={**route.headers, "anthropic-beta": "other-beta"})
    agent = run_turns(with_mode(scope, "fast"), fake_clock, server, route, "go")
    (request,) = server.model_requests
    assert request.body["speed"] == "fast"
    assert request.headers["anthropic-beta"] == "other-beta,fast-mode-2026-02-01"
    assert agent.binding.state.route.headers["anthropic-beta"] == "other-beta"  # route untouched


# -- usage records the tier -----------------------------------------------------------------------


def _answer(data: dict[str, Any]) -> CallResult:
    return CallResult(NormalizedResponse("x", (), "stop", None, (), None, data))


def test_served_tier_kept_when_reported_else_the_request_is_recorded() -> None:
    priority = FastTier({"service_tier": "priority"}, {})
    reported = served(_answer({"service_tier": "default"}), priority)
    assert isinstance(reported, CallResult)
    assert reported.response.provider_data == {"service_tier": "default"}
    unknown = served(_answer({"id": "r1"}), priority)
    assert isinstance(unknown, CallResult)
    assert unknown.response.provider_data == {
        "id": "r1",
        "service_tier": "priority",
        "fast_requested": True,
    }
    speed = served(_answer({}), FastTier({"speed": "fast"}, {}))
    assert isinstance(speed, CallResult)
    assert speed.response.provider_data["usage"] == {"speed": "fast"}
    assert served(_answer({}), None).response.provider_data == {}  # type: ignore[union-attr]


def test_a_fast_request_is_never_priced_at_standard_rates() -> None:
    tokens = CanonicalUsage.whole_prompt(1_000_000, 0, 0, 0, 0, None)
    standard = estimate_cost(tokens, model="claude-opus-5-5", base_url="https://api.anthropic.com")
    tagged = served(_answer({}), FastTier({"speed": "fast"}, {}))
    assert isinstance(tagged, CallResult)
    fast = estimate_cost(
        replace(tokens, raw=tagged.response.provider_data["usage"]),
        model="claude-opus-5-5",
        base_url="https://api.anthropic.com",
    )
    assert standard.amount == Decimal(4) and fast.amount == Decimal(8)
    priority = served(_answer({}), FastTier({"service_tier": "priority"}, {}))
    assert isinstance(priority, CallResult)
    unpriced = estimate_cost(
        replace(tokens, raw={"service_tier": priority.response.provider_data["service_tier"]}),
        model="gpt-5.2",
        base_url="https://api.openai.com/v1",
    )
    assert unpriced.status == "unknown"


def test_agent_ledger_sees_the_requested_tier(scope: Scope, fake_clock: FakeClock) -> None:
    server = FakeModelServer(
        script_of(Turn((Text(("hi",)), UsageStep(100, 5), Finish("stop")))), clock=fake_clock
    )
    seen: list[object] = []

    async def main() -> Agent:
        agent = await Agent.start(
            with_mode(scope, "fast"), fast_route(server), [], fake_options(server)
        )
        agent.usage.add_listener(lambda usage, task: seen.append(usage.raw if usage else None))
        await agent.run_turn("go")
        return agent

    agent = asyncio.run(run_driven(main(), fake_clock))
    asyncio.run(agent.close())
    assert tiers(server) == ["priority"]
    assert seen == [{"service_tier": "priority"}]


# -- /fast ----------------------------------------------------------------------------------------


def _port_with(route: RuntimeRoute) -> FakePort:
    port = FakePort()
    port.route_now = RouteInfo(route, 128_000, None, None)
    return port


def test_fast_command_sets_the_session_mode(scope: Scope) -> None:
    supported = replace(route_of(), profile=fast_profile(route_of().base_url, fast_models=()))
    ctx = make_ctx(scope, port=_port_with(supported))
    key = fast_mode.session_key(scope, ctx.session_id)
    try:
        dispatch("/fast", ctx)
        assert ctx.lines == ["Fast mode: normal (default for this profile: normal)."]
        dispatch("/fast auto", ctx)
        assert fast_mode.FAST_MODES.get(key) == "auto"
        assert ctx.lines[-3:] == [
            "Fast mode auto for this session.",
            "Each message asks for the priority tier for its first 60 s.",
            "The priority tier is billed at the provider's higher rates.",
        ]
        ctx.lines.clear()
        dispatch("/fast status", ctx)
        assert ctx.lines[0] == "Fast mode: auto (default for this profile: normal)."
        dispatch("/fast turbo", ctx)
        assert ctx.lines[-1] == "Usage: /fast [normal|fast|auto|status] [--global]"
        assert fast_mode.FAST_MODES.get(key) == "auto"
    finally:
        fast_mode.FAST_MODES.clear(key)


def test_fast_command_notes_an_unsupported_route(scope: Scope) -> None:
    ctx = make_ctx(scope, port=_port_with(route_of()))
    key = fast_mode.session_key(scope, ctx.session_id)
    try:
        dispatch("/fast on", ctx)
        assert fast_mode.FAST_MODES.get(key) == "fast"
        assert ctx.lines[-1] == (
            "Note: fake does not offer a priority tier, so requests go out unchanged on this route."
        )
        dispatch("/fast", ctx)
        assert (
            ctx.lines[-1] == "This route has no priority tier: fake does not offer a priority tier."
        )
    finally:
        fast_mode.FAST_MODES.clear(key)


def test_fast_global_saves_the_default(scope: Scope) -> None:
    ctx = make_ctx(scope, port=_port_with(route_of()))
    key = fast_mode.session_key(scope, ctx.session_id)
    try:
        dispatch("/fast fast --global", ctx)
        assert ctx.lines[0] == "Fast mode fast for this session and saved as the default."
        loader.reset()
        assert loader.load(scope).get("agent.fast_mode") == "fast"
    finally:
        fast_mode.FAST_MODES.clear(key)
