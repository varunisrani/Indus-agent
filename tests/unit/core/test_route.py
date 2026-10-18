"""Route binding (P04-23): one state value, switched whole or not at all."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from indus.core.prompt.builder import BuildContext, PromptParts
from indus.core.prompt.lifecycle import PromptLifecycle
from indus.core.route import (
    MIN_CONTEXT,
    CachePolicy,
    RouteBinding,
    RouteState,
    prompt_identity,
)
from indus.foundation.errors import ContextTooSmall, RouteRefused
from indus.foundation.scope import Scope
from indus.providers.model_list import ModelInfo
from indus.providers.route import RuntimeRoute, Secret
from tests.fakes.clock import checkpoint
from tests.fakes.model.server import FAKE_KEY

BASE = "https://fake.invalid/v1"
OTHER_BASE = "https://other.invalid/v1"

WINDOWS = {"model-a": 200_000, "model-b": 128_000, "model-c": 1_000_000, "tiny": 32_768}


class DictConfig:
    """A config view over a plain nested mapping (dotted paths)."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = data

    def get(self, path: str, /) -> object:
        node: object = self._data
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]  # pyright: ignore[reportUnknownVariableType]
        return node  # pyright: ignore[reportUnknownVariableType]


def make_route(
    model: str = "model-a",
    provider: str = "alpha",
    *,
    api_mode: str = "chat_completions",
    base_url: str = BASE,
    key: str = FAKE_KEY,
) -> RuntimeRoute:
    return RuntimeRoute(
        provider=provider,
        model=model,
        api_mode=api_mode,
        base_url=base_url,
        api_key=Secret(key),
        source="test",
    )


async def lister(route: RuntimeRoute) -> list[ModelInfo]:
    return [ModelInfo(name, tokens) for name, tokens in WINDOWS.items()]


class Clients:
    """A client factory that records what it made; every client answers 404."""

    def __init__(self) -> None:
        self.made: list[httpx.AsyncClient] = []
        self.fail = False

    def __call__(self, route: RuntimeRoute) -> httpx.AsyncClient:
        if self.fail:
            raise RuntimeError("no client for you")
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404)))
        self.made.append(client)
        return client

    async def close(self) -> None:
        for client in self.made:
            await client.aclose()


class Recorder:
    """A dependent that records each call and can be told to fail going forward."""

    def __init__(self, name: str, log: list[tuple[str, str, str]]) -> None:
        self.name = name
        self.log = log
        self.fail_forward = False
        self.fail_back = False

    async def __call__(self, before: RouteState, after: RouteState) -> None:
        self.log.append((self.name, before.model, after.model))
        backwards = after.model == "model-a" and before.model != "model-a"
        if self.fail_back and backwards:
            raise RuntimeError(f"{self.name} cannot go back")
        if self.fail_forward and not backwards:
            raise RuntimeError(f"{self.name} refuses")


def with_config(scope: Scope, data: Mapping[str, Any] | None = None) -> Scope:
    return scope.with_config(DictConfig(data or {}))


async def bound(
    scope: Scope, clients: Clients, *dependents: Recorder, **fields: Any
) -> RouteBinding:
    return await RouteBinding.bind(
        scope,
        make_route(**fields),
        client_factory=clients,
        list_models=lister,
        dependents=dependents,
    )


def snapshot(state: RouteState) -> tuple[object, ...]:
    return (
        state.provider,
        state.model,
        state.base_url,
        state.api_mode,
        state.credential,
        state.client,
        state.reasoning_effort,
        dict(state.extra_body),
        state.context_length,
        state.cache,
        state.identity,
    )


# -- binding and switching ------------------------------------------------------------------------


def test_bind_derives_every_field(scope: Scope) -> None:
    config = {
        "providers": {
            "alpha": {
                "reasoning_effort": "high",
                "extra_body": {"tier": "fast"},
                "models": {"model-a": {"extra_body": {"top_k": 4}}},
            }
        }
    }

    async def go() -> None:
        clients = Clients()
        binding = await bound(with_config(scope, config), clients)
        state = binding.state
        assert (state.provider, state.model, state.base_url, state.api_mode) == (
            "alpha",
            "model-a",
            BASE,
            "chat_completions",
        )
        assert state.credential == Secret(FAKE_KEY)
        assert state.client is clients.made[0]
        assert (state.context_length, state.window.source) == (200_000, "endpoint")
        assert state.reasoning_effort == "high"
        assert dict(state.extra_body) == {"tier": "fast", "top_k": 4}
        assert state.cache == CachePolicy(False, "wire")
        assert state.identity == ("model-a", "alpha")
        assert binding.generation == 0 and binding.previous is None
        with pytest.raises(TypeError):
            state.extra_body["tier"] = "slow"  # type: ignore[index]
        await clients.close()

    asyncio.run(go())


def test_apply_swaps_every_field(scope: Scope) -> None:
    async def go() -> None:
        clients = Clients()
        binding = await bound(with_config(scope), clients)
        before = binding.state
        after = await binding.apply(
            make_route("model-c", "beta", api_mode="anthropic_messages", base_url=OTHER_BASE)
        )
        assert binding.state is after and binding.previous is before
        assert binding.generation == 1
        assert snapshot(after) == (
            "beta",
            "model-c",
            OTHER_BASE,
            "anthropic_messages",
            Secret(FAKE_KEY),
            clients.made[1],
            None,
            {},
            1_000_000,
            CachePolicy(False, "wire"),  # not a Claude model (P08-07 rule)
            ("model-c", "beta"),
        )
        await clients.close()

    asyncio.run(go())


def test_default_lister_asks_the_endpoint_with_the_new_client(scope: Scope) -> None:
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "model-a", "context_length": 150_000}]})

    async def go() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        binding = await RouteBinding.bind(
            with_config(scope), make_route(), client_factory=lambda _: client
        )
        assert (binding.state.context_length, binding.state.window.source) == (150_000, "endpoint")
        assert seen == [f"{BASE}/models"]
        await client.aclose()

    asyncio.run(go())


def test_config_override_for_another_model_is_not_carried(scope: Scope) -> None:
    config = {"model": {"default": "model-a", "context_length": 500_000}}

    async def go() -> None:
        clients = Clients()
        binding = await bound(with_config(scope, config), clients)
        assert (binding.state.context_length, binding.state.window.source) == (500_000, "config")
        after = await binding.apply(make_route("model-b"))
        assert (after.context_length, after.window.source) == (128_000, "endpoint")
        await clients.close()

    asyncio.run(go())


# -- refusals -------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["endpoint", "config"])
def test_small_window_refused_before_any_dependent(scope: Scope, how: str) -> None:
    config = {"model": {"default": "model-b", "context_length": 40_000}} if how == "config" else {}
    target = "model-b" if how == "config" else "tiny"
    calls: list[tuple[str, str, str]] = []

    async def go() -> None:
        clients = Clients()
        binding = await bound(with_config(scope, config), clients, Recorder("one", calls))
        kept = snapshot(binding.state)
        with pytest.raises(ContextTooSmall) as caught:
            await binding.apply(make_route(target))
        assert caught.value.tokens < MIN_CONTEXT
        assert snapshot(binding.state) == kept and binding.generation == 0
        assert calls == []
        await clients.close()

    asyncio.run(go())


@pytest.mark.parametrize(("model", "base_url"), [("", BASE), ("model-b", ""), ("  ", " ")])
def test_incomplete_route_refused(scope: Scope, model: str, base_url: str) -> None:
    async def go() -> None:
        clients = Clients()
        binding = await bound(with_config(scope), clients)
        kept = snapshot(binding.state)
        with pytest.raises(RouteRefused, match="the route has no"):
            await binding.apply(make_route(model, "beta", base_url=base_url))
        assert snapshot(binding.state) == kept
        assert len(clients.made) == 1  # no client was even asked for
        await clients.close()

    asyncio.run(go())
    with pytest.raises(RouteRefused):
        asyncio.run(RouteBinding.bind(scope, make_route(""), list_models=lister))


@pytest.mark.parametrize("failing", ["client", "first", "second", "third"])
def test_failing_step_restores_every_field(scope: Scope, failing: str) -> None:
    calls: list[tuple[str, str, str]] = []

    async def go() -> None:
        clients = Clients()
        steps = [Recorder(name, calls) for name in ("first", "second", "third")]
        binding = await bound(with_config(scope), clients, *steps)
        before = binding.state
        kept = snapshot(before)
        if failing == "client":
            clients.fail = True
        else:
            next(s for s in steps if s.name == failing).fail_forward = True
        with pytest.raises(RuntimeError):
            await binding.apply(make_route("model-b", "beta", base_url=OTHER_BASE))
        assert binding.state is before and snapshot(binding.state) == kept
        assert binding.generation == 0 and binding.previous is None
        await clients.close()

    asyncio.run(go())
    forward = {"client": 0, "first": 1, "second": 2, "third": 3}[failing]
    names = ["first", "second", "third"][:forward]
    went = [(n, "model-a", "model-b") for n in names]
    undone = [(n, "model-b", "model-a") for n in reversed(names[:-1])]
    assert calls == went + undone


def test_walk_back_failure_is_logged_and_the_first_error_kept(
    scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[tuple[str, str, str]] = []

    async def go() -> None:
        clients = Clients()
        first, second = Recorder("first", calls), Recorder("second", calls)
        first.fail_back = True
        second.fail_forward = True
        binding = await bound(with_config(scope), clients, first, second)
        with pytest.raises(RuntimeError, match="second refuses"):
            await binding.apply(make_route("model-b"))
        assert binding.state.model == "model-a"
        await clients.close()

    with caplog.at_level(logging.WARNING, logger="indus.core.route"):
        asyncio.run(go())
    assert "first cannot go back" in caplog.text
    assert calls == [
        ("first", "model-a", "model-b"),
        ("second", "model-a", "model-b"),
        ("first", "model-b", "model-a"),
    ]


def test_cancelled_switch_walks_back(scope: Scope) -> None:
    calls: list[tuple[str, str, str]] = []

    async def go() -> None:
        clients = Clients()
        entered = asyncio.Event()

        async def hang(before: RouteState, after: RouteState) -> None:
            if after.model == "model-b":
                entered.set()
                await asyncio.Event().wait()

        binding = await bound(with_config(scope), clients, Recorder("first", calls))
        binding.add_dependent(hang)
        task = asyncio.create_task(binding.apply(make_route("model-b")))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert binding.state.model == "model-a" and binding.generation == 0
        await clients.close()

    asyncio.run(go())
    assert calls == [("first", "model-a", "model-b"), ("first", "model-b", "model-a")]


# -- rollback -------------------------------------------------------------------------------------


def test_rollback_returns_to_the_previous_state(scope: Scope) -> None:
    calls: list[tuple[str, str, str]] = []

    async def go() -> None:
        clients = Clients()
        binding = await bound(with_config(scope), clients, Recorder("one", calls))
        assert await binding.rollback() is None  # nothing applied yet
        before = binding.state
        await binding.apply(make_route("model-b", "beta"))
        restored = await binding.rollback()
        assert restored is before and binding.state is before
        assert binding.generation == 2 and binding.previous is None
        assert await binding.rollback() is None
        await clients.close()

    asyncio.run(go())
    assert calls == [("one", "model-a", "model-b"), ("one", "model-b", "model-a")]


def test_failed_rollback_keeps_the_current_state(scope: Scope) -> None:
    calls: list[tuple[str, str, str]] = []

    async def go() -> None:
        clients = Clients()
        step = Recorder("one", calls)
        binding = await bound(with_config(scope), clients, step)
        current = await binding.apply(make_route("model-b"))
        step.fail_back = True
        with pytest.raises(RuntimeError):
            await binding.rollback()
        assert binding.state is current and binding.previous is not None
        await clients.close()

    asyncio.run(go())


def test_rollback_to_explicit_snapshot(scope: Scope) -> None:
    async def go() -> None:
        clients = Clients()
        binding = await bound(with_config(scope), clients)
        primary = binding.state
        await binding.apply(make_route("model-b", "beta"))
        await binding.apply(make_route("model-c", "gamma"))
        assert binding.previous is not None and binding.previous.model == "model-b"
        assert await binding.rollback(to=primary) is primary
        assert binding.state.identity == ("model-a", "alpha")
        await clients.close()

    asyncio.run(go())


def test_rollback_replaces_a_closed_client(scope: Scope) -> None:
    async def go() -> None:
        clients = Clients()
        binding = await bound(with_config(scope), clients)
        first = binding.state.client
        await binding.apply(make_route("model-b"))
        await first.aclose()
        restored = await binding.rollback()
        assert restored is not None and restored.client is not first
        assert not restored.client.is_closed and restored.client is clients.made[-1]
        assert snapshot(restored)[:5] == (
            "alpha",
            "model-a",
            BASE,
            "chat_completions",
            Secret(FAKE_KEY),
        )
        await clients.close()

    asyncio.run(go())


# -- derived values -------------------------------------------------------------------------------


def test_extra_body_never_leaks_across_providers(scope: Scope) -> None:
    config = {
        "providers": {
            "alpha": {"extra_body": {"tier": "fast", "nested": {"k": 1}}},
            "beta": {"models": {"model-b": {"extra_body": {"seed": 7}}}},
        }
    }

    async def go() -> None:
        clients = Clients()
        binding = await bound(with_config(scope, config), clients)
        assert dict(binding.state.extra_body) == {"tier": "fast", "nested": {"k": 1}}
        assert (
            binding.state.extra_body["nested"]
            is not config["providers"]["alpha"]["extra_body"]["nested"]
        )
        after = await binding.apply(make_route("model-b", "beta"))
        assert dict(after.extra_body) == {"seed": 7}
        other = await binding.apply(make_route("model-c", "gamma"))
        assert dict(other.extra_body) == {}
        await clients.close()

    asyncio.run(go())


def test_reasoning_follows_config_then_session(scope: Scope) -> None:
    config = {
        "providers": {
            "alpha": {"models": {"model-a": {"reasoning_effort": "low"}}},
            "beta": {"reasoning_effort": False},
            "delta": {"reasoning_effort": "none"},
            "omega": {"reasoning_effort": 3},
        }
    }

    async def go() -> None:
        clients = Clients()
        binding = await RouteBinding.bind(
            with_config(scope, config),
            make_route(),
            requested_effort="High",
            client_factory=clients,
            list_models=lister,
        )
        assert binding.state.reasoning_effort == "low"
        assert binding.state.requested_effort == "high"
        assert (await binding.apply(make_route("model-b", "gamma"))).reasoning_effort == "high"
        assert (await binding.apply(make_route("model-b", "beta"))).reasoning_effort is None
        assert (await binding.apply(make_route("model-b", "delta"))).reasoning_effort is None
        assert (await binding.apply(make_route("model-b", "omega"))).reasoning_effort == "high"
        cleared = await binding.apply(make_route("model-b", "gamma"), requested_effort=None)
        assert cleared.reasoning_effort is None and cleared.requested_effort is None
        changed = await binding.apply(make_route("model-c", "gamma"), requested_effort="medium")
        assert changed.reasoning_effort == "medium"
        await clients.close()

    asyncio.run(go())


def test_cache_policy_follows_the_wire(scope: Scope) -> None:
    config = {
        "providers": {"beta": {"prompt_caching": False}},
        "prompt_caching": {"cache_ttl": "auto"},
    }

    async def go() -> None:
        clients = Clients()
        binding = await bound(
            with_config(scope, config), clients, model="claude-a", api_mode="anthropic_messages"
        )
        assert binding.state.cache == CachePolicy(True, "wire", "auto")
        off = await binding.apply(make_route("claude-b", "beta", api_mode="anthropic_messages"))
        assert off.cache == CachePolicy(False, "config", "auto")
        plain = await binding.apply(make_route("model-b", "gamma"))
        assert plain.cache == CachePolicy(False, "wire", "auto")
        await clients.close()

    asyncio.run(go())


# -- the prompt identity dependent ----------------------------------------------------------------

PROMPT = (
    "Identity text.\n\nConversation date: Saturday\nModel: model-a\nProvider: alpha\nPlatform: cli"
)


class FixedBuilder:
    async def build(self, ctx: BuildContext) -> PromptParts:
        return PromptParts(stable=PROMPT, context="", volatile="", text=PROMPT)


def test_identity_dependent_rewrites_prompt_lines(scope: Scope) -> None:
    calls: list[tuple[str, str, str]] = []

    async def go() -> None:
        clients = Clients()
        life = PromptLifecycle(FixedBuilder())
        await life.restore_or_build(BuildContext(scope=scope, session_id="s1"))
        late = Recorder("late", calls)
        binding = await bound(with_config(scope), clients)
        binding.add_dependent(prompt_identity(life))
        binding.add_dependent(late)

        await binding.apply(make_route("model-b", "beta"))
        assert life.prompt == PROMPT.replace("model-a", "model-b").replace("alpha", "beta")

        late.fail_forward = True
        with pytest.raises(RuntimeError):
            await binding.apply(make_route("model-c", "gamma"))
        assert life.prompt == PROMPT.replace("model-a", "model-b").replace("alpha", "beta")

        late.fail_forward = False
        await binding.rollback()
        assert life.prompt == PROMPT
        # Same identity (a key refresh on the same model) leaves the prompt alone.
        await binding.apply(make_route(key=FAKE_KEY + "x"))
        assert life.prompt == PROMPT
        await clients.close()

    asyncio.run(go())


def test_concurrent_switches_are_serialized(scope: Scope) -> None:
    order: list[str] = []

    async def go() -> None:
        clients = Clients()
        gate = asyncio.Event()

        async def slow_lister(route: RuntimeRoute) -> list[ModelInfo]:
            order.append(f"start {route.model}")
            if route.model == "model-b":
                await gate.wait()
            order.append(f"end {route.model}")
            return await lister(route)

        binding = await RouteBinding.bind(
            with_config(scope), make_route(), client_factory=clients, list_models=slow_lister
        )
        order.clear()
        first = asyncio.create_task(binding.apply(make_route("model-b")))
        second = asyncio.create_task(binding.apply(make_route("model-c")))
        await checkpoint()
        gate.set()
        b_state, c_state = await first, await second
        assert binding.state is c_state and binding.previous is b_state
        assert binding.generation == 2
        await clients.close()

    asyncio.run(go())
    assert order == ["start model-b", "end model-b", "start model-c", "end model-c"]
