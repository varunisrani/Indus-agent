"""Hybrid routing of private URLs (P24-41): private addresses go to a local sidecar when on."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import httpx
import respx

from indus.foundation.scope import Scope
from indus.safety.url_policy import check_url
from indus.tools.builtin.browser import routing
from tests.fakes.browser import FakeHandle, FakeSite, el
from tests.fakes.secret_store import MemorySecretStore
from tests.unit.tools.builtin.browser._rig import make_rig, make_scope_for

BB = "https://api.browserbase.com"
SECRETS = {"BROWSERBASE_API_KEY": "bb" + "-test-" + "d4" * 10, "BROWSERBASE_PROJECT_ID": "p-1"}
SITES = {
    "https://example.com/": FakeSite(title="Public"),
    "https://example.com/go": FakeSite(redirect_to="http://10.0.0.1/"),
    "http://10.0.0.1/": FakeSite(title="Router admin", elements=[el("button", "Reboot")]),
    "http://192.168.1.10/": FakeSite(title="NAS", elements=[el("link", "Files")]),
    "http://169.254.169.254/": FakeSite(title="metadata"),
}


def scope_for(
    tmp_path: Path, clock: Any, *, routing_on: bool, provider: str = "browserbase"
) -> Scope:
    config = {"browser": {"provider": provider, "route_private_local": routing_on}}
    return make_scope_for(tmp_path, clock, config).with_secrets(MemorySecretStore(dict(SECRETS)))


def mock_cloud(router: respx.MockRouter) -> dict[str, Any]:
    create = router.post(f"{BB}/v1/sessions").mock(
        return_value=httpx.Response(200, json={"id": "bb-1", "connectUrl": "wss://bb/x"})
    )
    close = router.post(f"{BB}/v1/sessions/bb-1").mock(return_value=httpx.Response(200))
    return {"create": create, "close": close}


def visited(rig: Any, how: str) -> list[str]:
    return [
        call[1][0]
        for handle in rig.driver.handles
        if handle.how == how
        for call in handle.page.calls
        if call[0] == "goto"
    ]


def test_private_address_goes_local_when_routing_is_on(tmp_path: Path) -> None:
    rig = make_rig(SITES)
    scope = scope_for(tmp_path, rig.clock, routing_on=True)

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            mock_cloud(router)
            out = json.loads(
                await rig.tools.pages.navigate({"url": "http://192.168.1.10/"}, scope=scope)
            )
            assert out["success"] is True, out
            assert out["engine"] == "local-sidecar"
            assert out["title"] == "NAS"
            # Later page tools follow the route of the last navigation.
            snap = json.loads(await rig.tools.pages.snapshot({}, scope=scope))
            assert "Files" in snap["snapshot"]
            public = json.loads(
                await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
            )
            assert public["engine"] == "cloud:browserbase"
            assert rig.manager.routes["default"] == routing.MAIN
            await rig.manager.end("default")
            assert rig.manager.sessions == {}

    asyncio.run(go())
    assert visited(rig, "attach") == ["https://example.com/"]  # the cloud never saw it
    assert visited(rig, "launch") == ["http://192.168.1.10/"]


def test_routing_off_blocks_private_address(tmp_path: Path) -> None:
    rig = make_rig(SITES)
    scope = scope_for(tmp_path, rig.clock, routing_on=False)

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            routes = mock_cloud(router)
            out = json.loads(
                await rig.tools.pages.navigate({"url": "http://192.168.1.10/"}, scope=scope)
            )
            assert out["success"] is False and out["blocked"] == "private"
            assert routes["create"].call_count == 0

    asyncio.run(go())
    assert rig.driver.handles == []


def test_no_provider_means_no_routing(tmp_path: Path) -> None:
    rig = make_rig(SITES)
    scope = make_scope_for(tmp_path, rig.clock, {"browser": {"route_private_local": True}})

    async def go() -> None:
        out = json.loads(
            await rig.tools.pages.navigate({"url": "http://192.168.1.10/"}, scope=scope)
        )
        assert out["success"] is False and out["blocked"] == "private"

    asyncio.run(go())


def test_public_url_redirecting_private_is_retried_locally(tmp_path: Path) -> None:
    rig = make_rig(SITES)
    scope = scope_for(tmp_path, rig.clock, routing_on=True)

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            mock_cloud(router)
            out = json.loads(
                await rig.tools.pages.navigate({"url": "https://example.com/go"}, scope=scope)
            )
            assert out["success"] is True, out
            assert out["engine"] == "local-sidecar"
            assert out["title"] == "Router admin"
            cloud_page = next(h for h in rig.driver.handles if h.how == "attach").page
            assert cloud_page.current == "about:blank"  # stopped on the cloud route
            await rig.manager.end("default")

    asyncio.run(go())


def test_public_url_redirecting_private_stays_blocked_when_routing_is_off(tmp_path: Path) -> None:
    rig = make_rig(SITES)
    scope = scope_for(tmp_path, rig.clock, routing_on=False)

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            mock_cloud(router)
            out = json.loads(
                await rig.tools.pages.navigate({"url": "https://example.com/go"}, scope=scope)
            )
            assert out["success"] is False
            assert "Blocked" in out["error"]
            await rig.manager.end("default")

    asyncio.run(go())
    assert [h.how for h in rig.driver.handles] == ["attach"]  # no sidecar


def test_guard_hit_on_the_cloud_route_retries_locally(tmp_path: Path) -> None:
    """The cloud tab's guard refused a private hop and the navigation failed: retried locally."""
    rig = make_rig(SITES)
    scope = scope_for(tmp_path, rig.clock, routing_on=True)

    async def failing_goto(page: Any) -> None:
        raise RuntimeError("net::ERR_FAILED")

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            mock_cloud(router)
            cloud = await rig.manager.session("default", scope)
            original = cloud.handle.page.goto

            async def goto(url: str, timeout_s: float) -> str:
                gate = cast(FakeHandle, cloud.handle).gate
                assert gate is not None
                await gate("http://10.0.0.1/")  # the redirect hop the guard refuses
                raise RuntimeError("net::ERR_FAILED")

            vars(cloud.handle.page)["goto"] = goto
            out = json.loads(
                await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
            )
            assert out["success"] is True and out["engine"] == "local-sidecar"
            assert cloud.private_hits == ["http://10.0.0.1/"]
            vars(cloud.handle.page)["goto"] = original
            await rig.manager.end("default")

    asyncio.run(go())


def test_metadata_is_refused_even_with_routing(tmp_path: Path) -> None:
    rig = make_rig(SITES)
    scope = scope_for(tmp_path, rig.clock, routing_on=True)

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            mock_cloud(router)
            out = json.loads(
                await rig.tools.pages.navigate({"url": "http://169.254.169.254/"}, scope=scope)
            )
            assert out["success"] is False and out["blocked"] == "metadata"

    asyncio.run(go())
    assert rig.driver.handles == []


def test_relaxed_scope_allows_private_but_not_metadata(tmp_path: Path) -> None:
    rig = make_rig()
    scope = make_scope_for(tmp_path, rig.clock)
    relaxed = routing.relaxed_scope(scope)
    assert not check_url("http://10.1.2.3/", scope).allowed
    assert check_url("http://10.1.2.3/", relaxed).allowed
    assert not check_url("http://169.254.169.254/", relaxed).allowed


def test_sidecar_gate_is_relaxed_only_for_the_sidecar(tmp_path: Path) -> None:
    rig = make_rig(SITES)
    scope = scope_for(tmp_path, rig.clock, routing_on=True)

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            mock_cloud(router)
            sidecar = await rig.manager.session("t", scope, route=routing.SIDECAR)
            main = await rig.manager.session("t", scope, route=routing.MAIN)
            assert sidecar.relaxed and not main.relaxed
            side_gate = cast(FakeHandle, sidecar.handle).gate
            main_gate = cast(FakeHandle, main.handle).gate
            assert side_gate is not None and main_gate is not None
            assert await side_gate("http://10.0.0.1/") is None
            assert await main_gate("http://10.0.0.1/") is not None
            assert await side_gate("http://169.254.169.254/") is not None
            await rig.manager.end("t")

    asyncio.run(go())
