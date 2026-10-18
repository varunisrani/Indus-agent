"""P03-17: context-window resolution order, confirmed-only cache, 64K minimum (A35)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest

from indus.foundation.errors import ContextTooSmall
from indus.foundation.scope import Scope
from indus.providers import context_length as cl
from indus.providers.context_length import (
    FALLBACK_CONTEXT,
    ContextWindow,
    cache_path,
    cached_length,
    family_length,
    find_window,
    parse_overflow_limit,
    remember,
    resolve,
)
from indus.providers.model_list import ModelInfo, list_models
from indus.providers.profile import ProviderProfile
from indus.providers.route import RuntimeRoute
from indus.providers.route import resolve as resolve_route
from tests.unit.providers.conftest import ScopeFactory, fake_key

LOCAL = "http://127.0.0.1:8000/v1"


class Lister:
    """A ``list_models`` stand-in that records calls."""

    def __init__(self, models: list[ModelInfo]) -> None:
        self.models = models
        self.calls = 0

    async def __call__(self, route: RuntimeRoute) -> list[ModelInfo]:
        self.calls += 1
        return self.models


def _window(route: RuntimeRoute, scope: Scope, lister: Any = None) -> ContextWindow:
    return asyncio.run(resolve(route, scope=scope, list_models=lister))


def _local_route(scope: Scope, model: str) -> RuntimeRoute:
    return resolve_route(scope, provider="custom", base_url=LOCAL, model=model)


def test_config_beats_everything(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={
            "model": {
                "provider": "custom",
                "base_url": LOCAL,
                "default": "big-model",
                "context_length": 100_000,
            }
        }
    )
    route = resolve_route(scope)
    remember(scope, LOCAL, "big-model", 300_000, source="endpoint")
    lister = Lister([ModelInfo("big-model", 131_072)])
    assert _window(route, scope, lister) == ContextWindow(100_000, "config")
    assert lister.calls == 0


def test_config_override_for_another_model_is_ignored(make_scope: ScopeFactory) -> None:
    scope = make_scope(config={"model": {"default": "other", "context_length": 100_000}})
    route = _local_route(scope, "claude-like-model")
    assert _window(route, scope) == ContextWindow(cl.FAMILY_TABLE["claude"], "family")


def test_per_model_provider_setting(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={"providers": {"openrouter": {"models": {"vendor/m1": {"context_length": 98_304}}}}},
        env={"OPENROUTER_API_KEY": fake_key("or")},
    )
    route = resolve_route(scope, provider="openrouter", model="vendor/m1")
    assert _window(route, scope) == ContextWindow(98_304, "config")


def test_profile_hook_and_hint(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={
            "providers": {
                "gpu": {
                    "base_url": LOCAL,
                    "context_length": 128_000,
                    "models": {"wide": {"context_length": 262_144}},
                }
            }
        }
    )
    lister = Lister([])
    wide = resolve_route(scope, provider="gpu", model="wide")
    # per-model config is read directly first; the provider-level value comes via the profile
    assert _window(wide, scope, lister) == ContextWindow(262_144, "config")
    other = resolve_route(scope, provider="gpu", model="other")
    assert _window(other, scope, lister) == ContextWindow(128_000, "profile")
    assert lister.calls == 0


def test_plugin_profile_hook(make_scope: ScopeFactory) -> None:
    scope = make_scope()
    profile = ProviderProfile(
        name="acme",
        base_url="https://api.acme.example/v1",
        env_vars=("ACME_KEY",),
        context_length=lambda model: 300_000 if model == "acme-xl" else None,
        context_length_hint=96_000,
    )
    route = RuntimeRoute("acme", "acme-xl", "chat_completions", profile.base_url, profile=profile)
    assert _window(route, scope) == ContextWindow(300_000, "profile")
    other = RuntimeRoute("acme", "acme-s", "chat_completions", profile.base_url, profile=profile)
    assert _window(other, scope) == ContextWindow(96_000, "profile")


def test_local_models_small_window_refused(make_scope: ScopeFactory) -> None:
    scope = make_scope()
    route = _local_route(scope, "qwen-small")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == LOCAL + "/models"
        return httpx.Response(200, json={"data": [{"id": "qwen-small", "max_model_len": 32768}]})

    async def run() -> ContextWindow:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:

            async def lister(r: RuntimeRoute) -> list[ModelInfo]:
                return await list_models(r, client=client, scope=scope)

            return await resolve(route, scope=scope, list_models=lister)

    with pytest.raises(ContextTooSmall) as caught:
        asyncio.run(run())
    assert caught.value.tokens == 32768 and caught.value.source == "endpoint"
    assert "64,000" in caught.value.message


def test_endpoint_value_cached_then_reused(make_scope: ScopeFactory) -> None:
    scope = make_scope()
    route = _local_route(scope, "big-model")
    lister = Lister([ModelInfo("other", 8_000), ModelInfo("Big-Model", 131_072)])
    assert _window(route, scope, lister) == ContextWindow(131_072, "endpoint")
    assert _window(route, scope, lister) == ContextWindow(131_072, "cache")
    assert lister.calls == 1
    assert cached_length(scope, LOCAL + "/", "big-model") == 131_072


def test_vendor_routes_skip_live_listing(make_scope: ScopeFactory) -> None:
    scope = make_scope(env={"DEEPSEEK_API_KEY": fake_key("ds")})
    route = resolve_route(scope, provider="deepseek", model="deepseek-chat")
    lister = Lister([ModelInfo("deepseek-chat", 999_999)])
    assert _window(route, scope, lister) == ContextWindow(128_000, "family")
    assert lister.calls == 0


def test_family_and_fallback_are_not_cached(make_scope: ScopeFactory) -> None:
    scope = make_scope()
    _window(_local_route(scope, "gpt-4.1-mini"), scope, Lister([]))
    _window(_local_route(scope, "mystery-model"), scope, Lister([]))
    assert not cache_path(scope).exists()
    assert remember(scope, LOCAL, "m", 100_000, source="family") is False
    assert remember(scope, LOCAL, "m", 10, source="endpoint") is False
    assert remember(scope, LOCAL, " ", 100_000, source="endpoint") is False
    assert not cache_path(scope).exists()


def test_cache_entries_validated_on_read(make_scope: ScopeFactory) -> None:
    scope = make_scope()
    path = cache_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = {
        f"{LOCAL}|guessed": {"tokens": 500_000, "source": "family"},
        f"{LOCAL}|zero": {"tokens": 0, "source": "endpoint"},
        f"{LOCAL}|huge": {"tokens": 99_000_000, "source": "endpoint"},
        f"{LOCAL}|text": {"tokens": "lots", "source": "endpoint"},
        f"{LOCAL}|good": {"tokens": 150_000, "source": "provider-error"},
    }
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
    for model in ("guessed", "zero", "huge", "text"):
        assert cached_length(scope, LOCAL, model) is None
    assert cached_length(scope, LOCAL, "good") == 150_000
    path.write_text("[broken", encoding="utf-8")
    assert cached_length(scope, LOCAL, "good") is None


@pytest.mark.parametrize(
    ("model", "tokens"),
    [
        ("anthropic/claude-sonnet-4.5", 200_000),
        ("gpt-4o-mini", 128_000),
        ("openai/gpt-4.1", 1_047_576),
        ("qwen3-coder-plus", 262_144),
        ("qwen3-32b", 131_072),
        ("GLM-4.6", 200_000),
        ("unknown-thing", None),
    ],
)
def test_family_table_longest_key(model: str, tokens: int | None) -> None:
    assert family_length(model) == tokens


def test_unknown_model_fallback_warns_once(
    make_scope: ScopeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    scope = make_scope()
    route = _local_route(scope, "mystery-model")
    with caplog.at_level(logging.WARNING, logger="indus.providers.context_length"):
        first = _window(route, scope, Lister([]))
        second = _window(route, scope, Lister([]))
    assert first == second == ContextWindow(FALLBACK_CONTEXT, "fallback")
    warnings = [r for r in caplog.records if "mystery-model" in r.getMessage()]
    assert len(warnings) == 1


def test_blank_model_falls_back_without_warning(
    make_scope: ScopeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    scope = make_scope()
    route = _local_route(scope, "")
    lister = Lister([ModelInfo("", 50_000)])
    with caplog.at_level(logging.WARNING):
        assert _window(route, scope, lister) == ContextWindow(FALLBACK_CONTEXT, "fallback")
    assert lister.calls == 0 and not caplog.records


def test_config_below_minimum_refused(make_scope: ScopeFactory) -> None:
    scope = make_scope(config={"model": {"context_length": 32_000}})
    route = _local_route(scope, "any")
    with pytest.raises(ContextTooSmall) as caught:
        _window(route, scope)
    assert caught.value.tokens == 32_000 and caught.value.source == "config"
    window = asyncio.run(find_window(route, scope=scope))
    assert window == ContextWindow(32_000, "config")


@pytest.mark.parametrize(
    ("message", "limit"),
    [
        (
            "This model's maximum context length is 128000 tokens. However, your messages "
            "resulted in 130512 tokens. Please reduce the length of the messages.",
            128_000,
        ),
        ("prompt is too long: 215034 tokens > 200000 maximum", 200_000),
        (
            "The input token count (1200000) exceeds the maximum number of tokens allowed "
            "(1048576).",
            1_048_576,
        ),
        (
            "Prompt contains 140000 tokens and 0 draft tokens, too large for model with 131072 "
            "maximum context length",
            131_072,
        ),
        (
            "request (40000 tokens) exceeds the available context size (32768 tokens), "
            "try increasing it",
            32_768,
        ),
        (
            "Trying to keep the first 5000 tokens when context the overflows. However, the model "
            "is loaded with context length of only 4096 tokens",
            4_096,
        ),
        ("Input prompt (40000 tokens) is too long and exceeds limit of 32768", 32_768),
        ("context_length_exceeded: max_model_len=16384, prompt is 20000 tokens", 16_384),
        ("input is longer than num_ctx: 8192", 8_192),
        ("Your input exceeds the context window of 200,000 tokens", 200_000),
        ("the prompt has 300000 tokens; this model supports up to 262144 tokens", 262_144),
        ("This model's maximum context length is 65536 tokens", 65_536),
    ],
)
def test_overflow_parser(message: str, limit: int) -> None:
    assert parse_overflow_limit(message) == limit


@pytest.mark.parametrize(
    "message",
    [
        "max_tokens: 100000 > 64000, which is the maximum allowed number of output tokens",
        "max_completion_tokens is too large: 65536. This model supports at most 32768 "
        "completion tokens.",
        "rate limit exceeded, try again in 20s",
        "Invalid API key",
        "maximum context length is 12 tokens",
        "",
    ],
)
def test_overflow_parser_ignores_output_caps(message: str) -> None:
    assert parse_overflow_limit(message) is None
