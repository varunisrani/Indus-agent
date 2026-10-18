"""P03-08: client factory, timeouts, identity headers and stream abort (R01.4, R01.12)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from indus.foundation.scope import Scope
from indus.providers import http as phttp
from indus.providers.http import (
    ClientPool,
    StreamHandle,
    Timeouts,
    base_headers,
    force_ipv4_enabled,
    make_transport,
    open_stream,
    request_timeout,
    timeouts_for,
)
from indus.providers.profile import USER_AGENT, foreign_product_tokens
from indus.providers.route import RuntimeRoute, resolve
from indus.providers.settings import config_of
from tests.unit.providers.conftest import ScopeFactory, fake_key

OR_KEY = fake_key("or")


def _route(scope: Scope, **kwargs: Any) -> RuntimeRoute:
    return resolve(scope, **kwargs)


def _or_scope(make_scope: ScopeFactory, **config: Any) -> Scope:
    return make_scope(config=config or None, env={"OPENROUTER_API_KEY": OR_KEY})


def test_default_timeouts(make_scope: ScopeFactory) -> None:
    scope = _or_scope(make_scope)
    timeouts = timeouts_for(_route(scope, provider="openrouter"), scope)
    assert timeouts == Timeouts(connect=30.0, read=120.0, write=1800.0, pool=30.0)
    as_httpx = timeouts.to_httpx()
    assert (as_httpx.connect, as_httpx.read, as_httpx.write, as_httpx.pool) == (30, 120, 1800, 30)


@pytest.mark.parametrize(
    ("providers", "env", "expected"),
    [
        (
            {
                "openrouter": {
                    "request_timeout_seconds": 90,
                    "models": {"m1": {"timeout_seconds": 45}},
                }
            },
            {"INDUS_API_TIMEOUT": "600"},
            (45.0, "model"),
        ),
        (
            {
                "openrouter": {
                    "request_timeout_seconds": 90,
                    "models": {"m2": {"timeout_seconds": 45}},
                }
            },
            {"INDUS_API_TIMEOUT": "600"},
            (90.0, "provider"),
        ),
        ({"openrouter": {"request_timeout_seconds": "90"}}, {}, (90.0, "provider")),
        ({}, {"INDUS_API_TIMEOUT": "600"}, (600.0, "env")),
        (
            {"openrouter": {"request_timeout_seconds": -1}},
            {"INDUS_API_TIMEOUT": "x"},
            (1800.0, "default"),
        ),
        ({"openrouter": {"request_timeout_seconds": True}}, {}, (1800.0, "default")),
        ({}, {}, (1800.0, "default")),
    ],
)
def test_request_timeout_precedence(
    make_scope: ScopeFactory,
    providers: dict[str, Any],
    env: dict[str, str],
    expected: tuple[float, str],
) -> None:
    scope = make_scope(config={"providers": providers}, env={"OPENROUTER_API_KEY": OR_KEY, **env})
    got = request_timeout("openrouter", "m1", config=config_of(scope), env=scope.env)
    assert got == expected


def test_configured_request_timeout_is_also_read(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={"providers": {"openrouter": {"request_timeout_seconds": 90}}},
        env={"OPENROUTER_API_KEY": OR_KEY},
    )
    timeouts = timeouts_for(_route(scope, provider="openrouter"), scope, stale_timeout=300)
    assert timeouts == Timeouts(connect=30.0, read=90.0, write=90.0, pool=30.0)


def test_read_timeout_raises(make_scope: ScopeFactory) -> None:
    scope = _or_scope(make_scope)
    cloud = _route(scope, provider="openrouter")
    assert timeouts_for(cloud, scope, stale_timeout=300).read == 300
    assert timeouts_for(cloud, scope, stale_timeout=60).read == 120
    local = _route(scope, provider="custom", base_url="http://127.0.0.1:8080/v1")
    assert timeouts_for(local, scope).read == 1800
    env_scope = make_scope(env={"INDUS_API_TIMEOUT": "2400"})
    local = _route(env_scope, provider="custom", base_url="http://localhost:8080/v1")
    assert timeouts_for(local, env_scope) == Timeouts(read=2400.0, write=2400.0)


def test_short_request_timeout_caps_connect(make_scope: ScopeFactory) -> None:
    scope = make_scope(env={"OPENROUTER_API_KEY": OR_KEY, "INDUS_API_TIMEOUT": "10"})
    timeouts = timeouts_for(_route(scope, provider="openrouter"), scope)
    assert (timeouts.connect, timeouts.pool, timeouts.write) == (10.0, 10.0, 10.0)


def test_identity_headers_only_ours(make_scope: ScopeFactory) -> None:
    scope = _or_scope(make_scope)
    route = _route(scope, provider="openrouter")
    assert base_headers(route) == {"User-Agent": USER_AGENT}

    async def run() -> httpx.Request:
        pool = ClientPool()
        with respx.mock(assert_all_called=True) as mock:
            called = mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            client = pool.client_for(route, timeouts=Timeouts())
            response = await client.post(route.base_url + "/chat/completions", json={})
            assert response.status_code == 200
            await pool.close_all()
            return called.calls.last.request

    request = asyncio.run(run())
    assert request.headers["user-agent"] == USER_AGENT
    for name, value in request.headers.items():
        assert not name.lower().startswith("x-"), name
        assert foreign_product_tokens(value) == [], (name, value)
    assert "authorization" not in request.headers  # the transport adds auth, not the client


def test_force_ipv4_passes_local_address(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    real = httpx.AsyncHTTPTransport

    class Recording(real):  # type: ignore[valid-type,misc]
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(phttp.httpx, "AsyncHTTPTransport", Recording)
    make_transport(force_ipv4=True)
    make_transport(force_ipv4=False)
    assert seen[0]["local_address"] == "0.0.0.0" and seen[0]["http2"] is False
    assert "local_address" not in seen[1]


def test_force_ipv4_setting(make_scope: ScopeFactory) -> None:
    assert force_ipv4_enabled(make_scope(config={"network": {"force_ipv4": True}}))
    assert not force_ipv4_enabled(make_scope(config={}))


def test_clients_reused_per_key_and_closed(make_scope: ScopeFactory) -> None:
    scope = _or_scope(make_scope)
    route = _route(scope, provider="openrouter")
    other_key = resolve(
        make_scope(env={"OPENROUTER_API_KEY": fake_key("other")}), provider="openrouter"
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200))

    async def run() -> None:
        pool = ClientPool()
        first = pool.client_for(route, timeouts=Timeouts(), transport=transport)
        assert pool.client_for(route, timeouts=Timeouts(), transport=transport) is first
        assert pool.client_for(other_key, timeouts=Timeouts(), transport=transport) is not first
        assert pool.client_for(route, timeouts=Timeouts(read=300), transport=transport) is not first
        assert len(pool) == 3
        assert await pool.close_all() == 3
        assert first.is_closed and len(pool) == 0
        fresh = pool.client_for(route, timeouts=Timeouts(), transport=transport)
        assert fresh is not first and not fresh.is_closed
        await pool.close_all()

    asyncio.run(run())


def test_module_level_pool(make_scope: ScopeFactory) -> None:
    route = _route(_or_scope(make_scope), provider="openrouter")
    transport = httpx.MockTransport(lambda request: httpx.Response(204))

    async def run() -> int:
        client = phttp.client_for(route, timeouts=Timeouts(), transport=transport)
        assert phttp.client_for(route, timeouts=Timeouts(), transport=transport) is client
        response = await client.get(route.base_url + "/models")
        assert response.status_code == 204
        return await phttp.close_all()

    assert asyncio.run(run()) >= 1


def test_redirects_not_followed(make_scope: ScopeFactory) -> None:
    route = _route(_or_scope(make_scope), provider="openrouter")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://elsewhere.example/steal"})

    async def run() -> int:
        pool = ClientPool()
        client = pool.client_for(route, timeouts=Timeouts(), transport=httpx.MockTransport(handler))
        response = await client.post(route.base_url + "/chat/completions", json={})
        await pool.close_all()
        return response.status_code

    assert asyncio.run(run()) == 307
    assert seen == ["https://openrouter.ai/api/v1/chat/completions"]


class EndlessStream(httpx.AsyncByteStream):
    """Sends one chunk, then waits forever; records when it is closed."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self.first_sent = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"data: {}\n\n"
        self.first_sent.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed.set()


def _endless_client(stream: EndlessStream) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    )


def test_stream_handle_reads_and_closes_once() -> None:
    async def run() -> None:
        stream = EndlessStream()
        async with _endless_client(stream) as client:
            handle = await open_stream(client, client.build_request("POST", "https://x.invalid/v1"))
            assert isinstance(handle, StreamHandle) and handle.status_code == 200
            chunks = handle.aiter_bytes()
            assert await anext(chunks) == b"data: {}\n\n"
            await handle.aclose()
            await handle.aclose()
            assert handle.closed and stream.closed.is_set()

    asyncio.run(run())


def test_aclose_swallows_errors() -> None:
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"x"

        async def aclose(self) -> None:
            raise RuntimeError("socket already gone")

    async def run() -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Broken()))
        async with httpx.AsyncClient(transport=transport) as client:
            handle = await open_stream(client, client.build_request("GET", "https://x.invalid/"))
            await handle.aclose()
            assert handle.closed

    asyncio.run(run())


@pytest.mark.slow
def test_cancel_closes_stream_within_200ms() -> None:
    async def run() -> tuple[float, bool, bool]:
        stream = EndlessStream()
        async with _endless_client(stream) as client:

            async def consume() -> None:
                request = client.build_request("POST", "https://x.invalid/v1/chat/completions")
                async with await open_stream(client, request) as handle:
                    async for _ in handle.aiter_bytes():
                        pass

            task = asyncio.create_task(consume())
            await stream.first_sent.wait()
            started = time.perf_counter()
            task.cancel()
            await asyncio.wait([task], timeout=2.0)
            elapsed = time.perf_counter() - started
            return elapsed, task.cancelled(), stream.closed.is_set()

    timings = [asyncio.run(run()) for _ in range(5)]
    print("\n[P03-08] cancel→closed ms:", [round(t * 1000, 3) for t, _, _ in timings])
    for elapsed, cancelled, closed in timings:
        assert cancelled and closed
        assert elapsed < 0.2
