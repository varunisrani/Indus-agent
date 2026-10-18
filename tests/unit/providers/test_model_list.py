"""P03-17: live ``/models`` listing with a clock-based cache; failures give an empty list."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from indus.foundation.scope import Scope
from indus.providers.model_list import (
    ModelInfo,
    cache_path,
    list_models,
    models_url,
    parse_models,
    request_headers,
)
from indus.providers.profile import ProviderProfile
from indus.providers.route import RuntimeRoute, Secret, resolve
from tests.fakes.clock import FakeClock
from tests.unit.providers.conftest import ScopeFactory, fake_key

LOCAL = "http://127.0.0.1:8000/v1"
MODELS = {
    "object": "list",
    "data": [
        {"id": "big-model", "max_model_len": 131072, "owned_by": "vllm"},
        {"id": "ctx-model", "context_length": 200000},
        {"id": "win-model", "context_window": "65536"},
        {"id": "plain"},
        {"id": "plain"},
        {"name": "no-id"},
        "junk",
    ],
}


class Server:
    def __init__(self, respond: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond or (lambda request: httpx.Response(200, json=MODELS))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _list(route: RuntimeRoute, server: Server, **kwargs: Any) -> list[ModelInfo]:
    async def run() -> list[ModelInfo]:
        async with server.client() as client:
            return await list_models(route, client=client, **kwargs)

    return asyncio.run(run())


def test_parse_models() -> None:
    models = parse_models(MODELS)
    assert [m.id for m in models] == ["big-model", "ctx-model", "win-model", "plain"]
    assert [m.context_length for m in models] == [131072, 200000, 65536, None]
    assert models[0].owned_by == "vllm" and models[1].owned_by is None
    assert parse_models([{"id": "a"}]) == [ModelInfo("a")]
    assert parse_models({"models": [{"id": "b"}]}) == [ModelInfo("b")]
    assert parse_models({"data": "nope"}) == [] and parse_models(None) == []


def test_lists_local_server_and_caches(make_scope: ScopeFactory, fake_clock: FakeClock) -> None:
    scope = make_scope()
    route = resolve(scope, provider="custom", base_url=LOCAL)
    server = Server()
    first = _list(route, server, scope=scope)
    assert [m.id for m in first][:2] == ["big-model", "ctx-model"]
    assert str(server.requests[0].url) == LOCAL + "/models"
    assert "authorization" not in server.requests[0].headers
    fake_clock.advance(19 * 60)
    assert _list(route, server, scope=scope) == first
    assert len(server.requests) == 1
    fake_clock.advance(60)
    assert _list(route, server, scope=scope) == first
    assert len(server.requests) == 2
    assert cache_path(scope).exists()
    assert oct(cache_path(scope).stat().st_mode & 0o777) == "0o600"


def test_refresh_skips_cache(make_scope: ScopeFactory) -> None:
    scope = make_scope()
    route = resolve(scope, provider="custom", base_url=LOCAL)
    server = Server()
    _list(route, server, scope=scope)
    _list(route, server, scope=scope, refresh=True)
    assert len(server.requests) == 2


def test_ttl_from_config(make_scope: ScopeFactory, fake_clock: FakeClock) -> None:
    scope = make_scope(config={"model_catalog": {"ttl_minutes": 1}})
    route = resolve(scope, provider="custom", base_url=LOCAL)
    server = Server()
    _list(route, server, scope=scope)
    fake_clock.advance(61)
    _list(route, server, scope=scope)
    assert len(server.requests) == 2


@pytest.mark.parametrize(
    "respond",
    [
        lambda request: httpx.Response(500, json={"error": "boom"}),
        lambda request: httpx.Response(401, json={"error": "no"}),
        lambda request: httpx.Response(200, text="<html>not json</html>"),
    ],
)
def test_failures_give_empty_list_and_are_not_cached(
    make_scope: ScopeFactory,
    respond: Callable[[httpx.Request], httpx.Response],
    caplog: pytest.LogCaptureFixture,
) -> None:
    scope = make_scope()
    route = resolve(scope, provider="custom", base_url=LOCAL)
    server = Server(respond)
    assert _list(route, server, scope=scope) == []
    assert _list(route, server, scope=scope) == []
    assert len(server.requests) == 2
    assert "could not list models" in caplog.text


def test_transport_error_gives_empty_list(make_scope: ScopeFactory) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    route = resolve(make_scope(), provider="custom", base_url=LOCAL)
    assert _list(route, Server(refuse)) == []


def test_listing_disabled_makes_no_request(make_scope: ScopeFactory) -> None:
    scope = make_scope(env={"ANTHROPIC_API_KEY": fake_key("an")})
    route = resolve(scope, provider="anthropic")
    server = Server()
    assert _list(route, server, scope=scope) == []
    assert server.requests == []


def test_bearer_only_with_key(make_scope: ScopeFactory) -> None:
    key = fake_key("or")
    route = resolve(make_scope(env={"OPENROUTER_API_KEY": key}), provider="openrouter")
    server = Server()
    _list(route, server)
    assert server.requests[0].headers["authorization"] == f"Bearer {key}"
    assert str(server.requests[0].url) == "https://openrouter.ai/api/v1/models"


def test_models_url_only_for_default_base() -> None:
    profile = ProviderProfile(
        name="acme",
        base_url="https://api.acme.example/v1",
        models_url="https://catalog.acme.example/models",
        env_vars=("ACME_KEY",),
    )

    def route(base: str) -> RuntimeRoute:
        return RuntimeRoute("acme", "m", "chat_completions", base, profile=profile)

    assert models_url(route("https://api.acme.example/v1")) == "https://catalog.acme.example/models"
    assert (
        models_url(route("https://api.acme.example/v1/")) == "https://catalog.acme.example/models"
    )
    assert models_url(route("https://proxy.example/v1")) == "https://proxy.example/v1/models"


def test_anthropic_wire_listing_headers() -> None:
    key = fake_key("mm")
    plain = RuntimeRoute("gw", "m", "anthropic_messages", "https://gw.example/anthropic")
    assert models_url(plain) == "https://gw.example/anthropic/v1/models"
    with_v1 = RuntimeRoute("gw", "m", "anthropic_messages", "https://gw.example/anthropic/v1")
    assert models_url(with_v1) == "https://gw.example/anthropic/v1/models"
    keyed = RuntimeRoute("gw", "m", "anthropic_messages", plain.base_url, api_key=Secret(key))
    headers = request_headers(keyed)
    assert headers["x-api-key"] == key and headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in headers
    bearer_profile = ProviderProfile(
        name="gw", base_url=plain.base_url, api_mode="anthropic_messages", bearer_auth=True
    )
    bearer = RuntimeRoute(
        "gw", "m", "anthropic_messages", plain.base_url, api_key=Secret(key), profile=bearer_profile
    )
    headers = request_headers(bearer)
    assert headers["Authorization"] == f"Bearer {key}" and "x-api-key" not in headers


def test_corrupt_cache_is_ignored(make_scope: ScopeFactory) -> None:
    scope: Scope = make_scope()
    path = cache_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    route = resolve(scope, provider="custom", base_url=LOCAL)
    server = Server()
    assert len(_list(route, server, scope=scope)) == 4
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and LOCAL in data["lists"]
