"""P24 integration: what the daemon's HTTP surfaces mount — the peer-gateway bot routes (P24-07)
on the API app, and the browser-control broker (P24-47) only with ``browser.control_enabled``
and the daemon's ticket book."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from indus.foundation.config import loader, writer
from indus.foundation.scope import Scope
from indus.gateway.browser_broker.broker import BrowserBroker
from indus.gateway.http import HttpHost, _browser_broker  # pyright: ignore[reportPrivateUsage]
from indus.gateway.http.api_server import ApiContext, ApiSettings, build_api_app
from indus.service.ws_auth import TicketBook


class _Keys:
    def verify(self, token: str) -> str | None:
        return "k1" if token == "key-" + "for-tests" else None


def _services(_spec: Any) -> Any:
    raise AssertionError("not used")


def test_the_api_app_serves_the_bot_routes(scope: Scope) -> None:
    ctx = ApiContext(
        scope=scope,
        desk=None,  # type: ignore[arg-type]
        keys=_Keys(),  # type: ignore[arg-type]
        settings=ApiSettings,
        clock=scope.clock,
    )
    with TestClient(build_api_app(ctx), client=("127.0.0.1", 1)) as client:
        anonymous = client.post("/v1/bots/chat")
        assert anonymous.status_code == 401  # mounted (not 404) and behind the API key
        keyed = client.post("/v1/bots/chat", headers={"Authorization": "Bearer key-for-tests"})
        assert keyed.status_code == 503  # no session store in this context


def test_the_broker_is_built_only_when_control_is_on(scope: Scope) -> None:
    book = TicketBook.for_home(scope.home)
    assert _browser_broker(HttpHost(scope, services=_services, tickets=book)) is None
    writer.save(scope, {"browser": {"control_enabled": True}})
    on = scope.with_config(loader.load(scope))
    broker = _browser_broker(HttpHost(on, services=_services, tickets=book))
    assert isinstance(broker, BrowserBroker) and broker.book is book
    assert broker.artifacts is not None
    assert broker.artifacts.root == on.home / "cache" / "browser-control"
    assert _browser_broker(HttpHost(on, services=_services)) is None  # no ticket book
