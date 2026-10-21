"""P24-04: secondary profiles' HTTP apps on the default listener, each checked with its own
profile's webhook secret or API key, run under its own scope; anything else is 404."""

from __future__ import annotations

import asyncio
import json
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.scope import Scope, scope_or_none
from indus.gateway.http.api_server import ApiServer
from indus.gateway.http.serving import Asgi
from indus.gateway.http.webhook import WebhookReceiver, WebhookSettings
from indus.gateway.multiplex.ingress import SharedIngress, profile_url
from indus.gateway.multiplex.scope import build_served_scope
from indus.state.webhook_subscriptions import sign_request
from tests.contract.gateway._http_kit import RecordingDeliver, build_api, within
from tests.fakes.clock import FakeClock
from tests.unit.gateway.multiplex.world import launch_scope, make_profile

SECRET_NAME = "INDUS_WEBHOOK_X"
BODY = json.dumps({"event_type": "push", "n": 1}).encode()


def secret_for(profile: str) -> str:
    return f"{profile}-shared-" + "s" * 20  # built at runtime: not a credential shape


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def launch(isolated_home: Path, fake_clock: FakeClock) -> Scope:
    return launch_scope(isolated_home, fake_clock)


@pytest.fixture
def scope_b(launch: Scope, isolated_home: Path, fake_clock: FakeClock) -> Scope:
    return build_served_scope(launch, make_profile(isolated_home, "b"), clock=fake_clock)


def receiver(scope: Scope, clock: FakeClock, deliver: RecordingDeliver) -> WebhookReceiver:
    """The profile's webhook receiver, its secret in that profile's own secret store."""
    assert scope.secrets is not None
    scope.secrets.set(SECRET_NAME, secret_for(scope.profile_id))  # type: ignore[attr-defined]
    route = {
        "secret": SECRET_NAME,
        "auth": "hmac",
        "deliver_only": True,
        "deliver": "local",
        "prompt": "{n}",
    }
    hooks = WebhookReceiver(
        scope,
        settings=lambda: WebhookSettings(routes={"x": route}),
        secrets=scope.secrets,
        desk=None,
        deliver=deliver,
        clock=clock,
    )
    hooks.load()
    return hooks


class Recorder:
    """An ASGI app that records what it was asked under which scope."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        bound = scope_or_none()
        self.seen.append(
            {
                "profile": bound.profile_id if bound is not None else None,
                "path": scope["path"],
                "root_path": scope.get("root_path"),
                "raw_path": scope.get("raw_path"),
                "query": scope.get("query_string"),
            }
        )
        body = b'{"ok": true}'
        headers = [(b"content-type", b"application/json")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def client(app: Asgi, port: int = 8644) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50002))  # pyright: ignore[reportArgumentType]
    return httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{port}")


def signed(profile: str, clock: FakeClock) -> dict[str, str]:
    now = int(clock.now().timestamp())
    return {
        "Content-Type": "application/json",
        **sign_request("hmac", secret_for(profile), BODY, now),
    }


# -- webhooks --------------------------------------------------------------------------------------


def test_b_secret_is_accepted_under_its_prefix_and_refused_unprefixed(
    launch: Scope, scope_b: Scope, fake_clock: FakeClock
) -> None:
    sent_a, sent_b = RecordingDeliver(), RecordingDeliver()
    hooks_a = receiver(launch, fake_clock, sent_a)
    hooks_b = receiver(scope_b, fake_clock, sent_b)
    ingress = SharedIngress(hooks_a.app)
    ingress.mount("b", "webhook", hooks_b.app, paths=("/webhooks/",), scope=scope_b)

    async def main() -> None:
        async with client(ingress) as http:
            good = await within(
                http.post("/p/b/webhooks/x", content=BODY, headers=signed("b", fake_clock))
            )
            assert good.status_code == 200 and good.json()["status"] == "delivered"
            unprefixed = await within(
                http.post("/webhooks/x", content=BODY, headers=signed("b", fake_clock))
            )
            assert unprefixed.status_code == 401  # the default profile checks with its own secret
            crossed = await within(
                http.post("/p/b/webhooks/x", content=BODY, headers=signed("default", fake_clock))
            )
            assert crossed.status_code == 401
            own = await within(
                http.post("/webhooks/x", content=BODY, headers=signed("default", fake_clock))
            )
            assert own.status_code == 200
        await hooks_a.aclose()
        await hooks_b.aclose()

    run(main())
    assert [s.text for s in sent_b.sent] == ["1"] and [s.text for s in sent_a.sent] == ["1"]


def test_a_profile_without_that_app_gets_404_not_the_default(
    launch: Scope, scope_b: Scope, isolated_home: Path, fake_clock: FakeClock
) -> None:
    default = Recorder()
    scope_c = build_served_scope(launch, make_profile(isolated_home, "c"), clock=fake_clock)
    ingress = SharedIngress(default)
    ingress.mount("b", "webhook", Recorder(), paths=("/webhooks/",), scope=scope_b)
    ingress.mount("c", "api", Recorder(), paths=("/v1/",), scope=scope_c)

    async def main() -> None:
        async with client(ingress) as http:
            for path in ("/p/c/webhooks/x", "/p/b/v1/models", "/p/b/", "/p/b"):
                answer = await within(http.post(path, content=BODY))
                assert answer.status_code == 404, path
                assert answer.json()["error"] == "not_found"

    run(main())
    assert default.seen == []


@pytest.mark.parametrize(
    "path",
    ["/p/zzz/webhooks/x", "/p/default/webhooks/x", "/p/B/webhooks/x", "/p", "/p/", "/p/b%2F"],
)
def test_unknown_and_default_prefixes_are_404(launch: Scope, scope_b: Scope, path: str) -> None:
    default = Recorder()
    ingress = SharedIngress(default)
    ingress.mount("b", "webhook", Recorder(), paths=("/webhooks/",), scope=scope_b)

    async def main() -> None:
        async with client(ingress) as http:
            assert (await within(http.post(path, content=BODY))).status_code == 404

    run(main())
    assert default.seen == []


def test_the_mounted_app_runs_under_its_profile_scope(scope_b: Scope) -> None:
    default, mounted = Recorder(), Recorder()
    ingress = SharedIngress(default)
    ingress.mount("b", "probe", mounted, paths=("/probe/", "/exact"), scope=scope_b)

    async def main() -> None:
        async with client(ingress) as http:
            assert (await within(http.get("/p/b/probe/x?y=1"))).status_code == 200
            assert (await within(http.get("/p/b/exact"))).status_code == 200
            assert (await within(http.get("/p/b/exact/more"))).status_code == 404
            assert (await within(http.get("/elsewhere"))).status_code == 200

    run(main())
    first, second = mounted.seen
    assert first == {
        "profile": "b",
        "path": "/probe/x",
        "root_path": "/p/b",
        "raw_path": b"/probe/x",
        "query": b"y=1",
    }
    assert second["path"] == "/exact"
    assert default.seen[0]["path"] == "/elsewhere" and default.seen[0]["profile"] is None


def test_unmount_and_mount_rules(scope_b: Scope) -> None:
    ingress = SharedIngress(Recorder())
    ingress.mount("b", "one", Recorder(), paths=("/a/",), scope=scope_b)
    ingress.mount("b", "two", Recorder(), paths=("b/",), scope=scope_b)
    assert ingress.mounted("b") == ["one", "two"]
    ingress.unmount("b", "one")
    assert ingress.mounted("b") == ["two"]
    ingress.unmount("b")
    assert ingress.mounted("b") == []
    for bad in ("default", "B", "../x", "root"):
        with pytest.raises(ValueError):
            ingress.mount(bad, "x", Recorder(), paths=("/a/",))
    with pytest.raises(ValueError):
        ingress.mount("b", "x", Recorder(), paths=())
    assert profile_url("http://127.0.0.1:8644/", "b", "/webhooks/x") == (
        "http://127.0.0.1:8644/p/b/webhooks/x"
    )


def test_a_websocket_to_a_missing_mount_is_closed(scope_b: Scope) -> None:
    ingress = SharedIngress(Recorder())
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(dict(message))

    run(ingress({"type": "websocket", "path": "/p/b/ws", "headers": []}, receive, send))
    assert sent == [{"type": "websocket.close", "code": 4404}]


# -- API keys --------------------------------------------------------------------------------------


def test_api_keys_are_per_profile_under_the_prefix(
    launch: Scope, scope_b: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit_a = await build_api(launch, fake_clock, tmp_path / "a")
        kit_b = await build_api(scope_b, fake_clock, tmp_path / "b")
        try:

            def front(app: Asgi) -> Asgi:
                ingress = SharedIngress(app)
                ingress.mount(
                    "b", "api_server", kit_b.app, paths=("/v1/", "/health"), scope=scope_b
                )
                return ingress

            server = ApiServer(kit_a.ctx, front=front)
            assert isinstance(server.app, SharedIngress)

            def auth(key: str) -> dict[str, str]:
                return {"Authorization": f"Bearer {key}"}

            async with client(server.app, 8642) as http:
                b_models = await within(http.get("/p/b/v1/models", headers=auth(kit_b.key)))
                assert b_models.status_code == 200
                assert (
                    await within(http.get("/v1/models", headers=auth(kit_b.key)))
                ).status_code == 401
                assert (
                    await within(http.get("/p/b/v1/models", headers=auth(kit_a.key)))
                ).status_code == 401
                assert (
                    await within(http.get("/v1/models", headers=auth(kit_a.key)))
                ).status_code == 200
                assert (await within(http.get("/p/b/health"))).status_code == 401
        finally:
            await kit_a.close()
            await kit_b.close()

    run(main())
