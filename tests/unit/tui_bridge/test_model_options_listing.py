"""``model.options`` offers a custom endpoint's own model list (W46 RS4).

The picker used to say "this provider lists no models" for a custom endpoint whose ``/models``
lists several, while setup offered them. The current provider's row now carries what its
endpoint lists; a vendor provider (whose list setup curates) and a failing endpoint stay empty.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx

from indus.foundation.scope import Scope
from indus.protocol.bridge import ModelOptionsParams
from indus.providers import bundled
from indus.providers.route import RuntimeRoute
from indus.tui_bridge.methods_config import model_options
from tests.fakes.model import FakeModelServer, script_of, text_turn

SIX = [{"id": f"local-model-{n}", "object": "model"} for n in range(6)]


def _bridge(scope: Scope, route: RuntimeRoute, client: httpx.AsyncClient) -> Any:
    state = SimpleNamespace(route=route, client=client, provider=route.provider, model=route.model)
    service = SimpleNamespace(
        agent=lambda _sid: SimpleNamespace(binding=SimpleNamespace(state=state))
    )

    async def get_service() -> Any:
        return service

    return SimpleNamespace(scope=scope, service=get_service)


def _route(provider: str, base_url: str) -> RuntimeRoute:
    return RuntimeRoute(
        provider=provider,
        model="local-model-0",
        api_mode="chat_completions",
        base_url=base_url,
        profile=bundled.by_name()[provider],
    )


def _rows(scope: Scope, route: RuntimeRoute, client: httpx.AsyncClient) -> dict[str, list[str]]:
    async def main() -> dict[str, list[str]]:
        async with client:
            result = await model_options(
                _bridge(scope, route, client), ModelOptionsParams(session_id="s1")
            )
        return {row.slug: row.models for row in result.providers}

    return asyncio.run(main())


def test_the_current_custom_endpoint_lists_its_models(scope: Scope) -> None:
    server = FakeModelServer(script_of(text_turn("unused")), models=SIX)
    rows = _rows(scope, _route("custom", server.base_url), server.client())
    assert rows["custom"] == [m["id"] for m in SIX]
    assert all(models == [] for slug, models in rows.items() if slug != "custom")
    assert [r.method for r in server.requests] == ["GET"]


def test_a_failing_endpoint_leaves_the_list_empty_for_a_typed_id(scope: Scope) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "down"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    rows = _rows(scope, _route("custom", "http://127.0.0.1:9/v1"), client)
    assert rows["custom"] == []


def test_a_vendor_provider_is_not_asked(scope: Scope) -> None:
    asked: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, json={"data": SIX})

    vendor = next(p for p in bundled.PROFILES if not p.is_custom and not p.hidden)
    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    rows = _rows(scope, _route(vendor.name, vendor.base_url or "https://x.invalid"), client)
    assert asked == [] and rows[vendor.name] == []
