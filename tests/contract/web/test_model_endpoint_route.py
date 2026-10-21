"""P20-32/33: ``POST /api/model/test-endpoint`` — the desktop's custom-endpoint Test asks the
endpoint's own ``/models`` route from the backend (no real network: a mock transport)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, cast

import httpx
import pytest

from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStore
from tests.contract.web.rest_app import RestApp, build_rest_app

WRITE = {"profile": "work"}


@pytest.fixture
def rest(scope: Scope) -> Iterator[RestApp]:
    config_loader.reset()
    built = build_rest_app(scope)
    yield built
    built.close()
    config_loader.reset()


def _client(
    seen: list[httpx.Request], answer: Callable[[httpx.Request], httpx.Response]
) -> Callable[[], httpx.Client]:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    return lambda: httpx.Client(transport=httpx.MockTransport(handle))


def _post(rest: RestApp, body: dict[str, Any]) -> Any:
    return rest.client.post("/api/model/test-endpoint", params=WRITE, json=body)


def test_lists_the_endpoints_models_with_the_given_key(rest: RestApp) -> None:
    seen: list[httpx.Request] = []
    key = "-".join(["local", "key", "123"])
    rest.state.extras["providers.endpoint_client"] = _client(
        seen,
        lambda _r: httpx.Response(200, json={"data": [{"id": "llama-3"}, {"id": "qwen"}]}),
    )
    answer = _post(rest, {"base_url": "http://127.0.0.1:8000/v1/", "api_key": key})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["ok"] is True and body["models"] == ["llama-3", "qwen"]
    assert body["status"] == 200
    assert str(seen[0].url) == "http://127.0.0.1:8000/v1/models"
    assert seen[0].headers["authorization"] == f"Bearer {key}"
    assert key not in answer.text


def test_uses_a_stored_key_by_name(rest: RestApp, scope: Scope) -> None:
    seen: list[httpx.Request] = []
    stored = "-".join(["stored", "value", "9"])
    cast(SecretStore, scope.secrets).set("MY_ENDPOINT_API_KEY", stored)
    rest.state.extras["providers.endpoint_client"] = _client(
        seen, lambda _r: httpx.Response(200, json=[{"id": "m"}])
    )
    body = {"base_url": "https://llm.example.test/v1", "key_env": "MY_ENDPOINT_API_KEY"}
    answer = _post(rest, body)
    assert answer.json()["ok"] is True
    assert seen[0].headers["authorization"] == f"Bearer {stored}"


def test_refusals_and_failures_are_answers_not_errors(rest: RestApp) -> None:
    seen: list[httpx.Request] = []
    rest.state.extras["providers.endpoint_client"] = _client(
        seen, lambda _r: httpx.Response(401, json={"error": "no"})
    )
    refused = _post(rest, {"base_url": "https://llm.example.test/v1"}).json()
    assert refused == {
        "ok": False,
        "status": 401,
        "models": [],
        "message": "The endpoint needs a key.",
    }

    def offline(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    rest.state.extras["providers.endpoint_client"] = _client(seen, offline)
    down = _post(rest, {"base_url": "http://127.0.0.1:9/v1"}).json()
    assert down["ok"] is False and down["status"] is None

    rest.state.extras["providers.endpoint_client"] = _client(
        seen, lambda _r: httpx.Response(200, text="<html>")
    )
    assert _post(rest, {"base_url": "http://127.0.0.1:9/v1"}).json()["ok"] is False


def test_bad_urls_are_refused(rest: RestApp) -> None:
    for url in ("ftp://x.test/v1", "not a url", "http://user:pw@x.test/v1", "file:///etc"):
        answer = _post(rest, {"base_url": url})
        assert answer.status_code == 400, url
        assert answer.json()["detail"]["code"] == "invalid_url"
