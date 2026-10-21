"""Cloud browser providers and the user CDP endpoint (P24-38) over respx fakes."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from indus.foundation.scope import Scope
from indus.tools.builtin.browser.cloud import PROVIDERS, CloudBrowsers, CloudFailure, credential
from indus.tools.builtin.browser.session import Choice
from tests.fakes.browser import FakeSite
from tests.fakes.secret_store import MemorySecretStore
from tests.unit.tools.builtin.browser._rig import make_rig, make_scope_for

BU = "https://api.browser-use.com/api/v3"
BB = "https://api.browserbase.com"
FC = "https://api.firecrawl.dev"
# Fake keys built at runtime (never a credential-shaped literal in the source).
BU_KEY = "bu" + "-test-" + "a1" * 10
BB_KEY = "bb" + "-test-" + "b2" * 10
FC_KEY = "fc" + "-test-" + "c3" * 10
PROJECT = "project-" + "7" * 6
SECRETS = {
    "BROWSER_USE_API_KEY": BU_KEY,
    "BROWSERBASE_API_KEY": BB_KEY,
    "BROWSERBASE_PROJECT_ID": PROJECT,
    "FIRECRAWL_API_KEY": FC_KEY,
}
SITES = {"https://example.com/": FakeSite(title="Example")}


def cloud_scope(tmp_path: Path, clock: Any, provider: str, **browser: Any) -> Scope:
    config = {"browser": {"provider": provider, **browser}}
    return make_scope_for(tmp_path, clock, config).with_secrets(MemorySecretStore(dict(SECRETS)))


def mock_provider(router: respx.MockRouter, provider: str, *, status: int = 200) -> dict[str, Any]:
    """Routes for one provider; returns the route objects by name."""
    if provider == "browser-use":
        create = router.post(f"{BU}/browsers").mock(
            return_value=httpx.Response(status, json={"id": "bu-1", "cdpUrl": "wss://bu/cdp"})
        )
        close = router.patch(f"{BU}/browsers/bu-1").mock(return_value=httpx.Response(200))
    elif provider == "browserbase":
        create = router.post(f"{BB}/v1/sessions").mock(
            return_value=httpx.Response(status, json={"id": "bb-1", "connectUrl": "wss://bb/x"})
        )
        close = router.post(f"{BB}/v1/sessions/bb-1").mock(return_value=httpx.Response(200))
    else:
        create = router.post(f"{FC}/v2/browser").mock(
            return_value=httpx.Response(status, json={"id": "fc-1", "cdpUrl": "wss://fc/cdp"})
        )
        close = router.delete(f"{FC}/v2/browser/fc-1").mock(return_value=httpx.Response(204))
    return {"create": create, "close": close}


@pytest.mark.parametrize("provider", ["browser-use", "browserbase", "firecrawl"])
def test_each_provider_creates_and_releases_once(tmp_path: Path, provider: str) -> None:
    rig = make_rig(SITES)
    scope = cloud_scope(tmp_path, rig.clock, provider)

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            routes = mock_provider(router, provider)
            out = json.loads(
                await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
            )
            assert out["success"] is True
            assert out["engine"] == f"cloud:{provider}"
            assert "notice" not in out
            await rig.manager.end("default")
            assert routes["create"].call_count == 1
            assert routes["close"].call_count == 1

    asyncio.run(go())
    assert rig.driver.launches == []  # the cloud served it: nothing local was launched
    assert len(rig.driver.attaches) == 1


def test_request_shapes_and_headers(tmp_path: Path) -> None:
    rig = make_rig(SITES)
    scope = cloud_scope(tmp_path, rig.clock, "browserbase")

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            routes = mock_provider(router, "browserbase")
            await rig.manager.session("t", scope)
            await rig.manager.end("t")
            create = routes["create"].calls.last.request
            assert create.headers["X-BB-API-Key"] == BB_KEY
            assert json.loads(create.content) == {
                "projectId": PROJECT,
                "keepAlive": True,
                "proxies": True,
            }
            release = routes["close"].calls.last.request
            assert json.loads(release.content) == {
                "projectId": PROJECT,
                "status": "REQUEST_RELEASE",
            }
            # The key never travels in a URL.
            assert BB_KEY not in str(create.url) and BB_KEY not in str(release.url)

    asyncio.run(go())


def test_browser_use_accepts_connect_url(tmp_path: Path) -> None:
    rig = make_rig()
    scope = cloud_scope(tmp_path, rig.clock, "browser-use")

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{BU}/browsers").mock(
                return_value=httpx.Response(200, json={"id": "bu-9", "connectUrl": "wss://bu/c"})
            )
            router.patch(f"{BU}/browsers/bu-9").mock(return_value=httpx.Response(200))
            await rig.manager.session("t", scope)
            await rig.manager.end("t")

    asyncio.run(go())
    assert rig.driver.attaches == ["wss://bu/c"]


def test_firecrawl_sends_ttl_and_bearer(tmp_path: Path) -> None:
    rig = make_rig()
    scope = cloud_scope(tmp_path, rig.clock, "firecrawl")

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            routes = mock_provider(router, "firecrawl")
            await rig.manager.session("t", scope)
            await rig.manager.end("t")
            sent = routes["create"].calls.last.request
            assert json.loads(sent.content) == {"ttl": 300}
            assert sent.headers["Authorization"] == f"Bearer {FC_KEY}"

    asyncio.run(go())


def test_browserbase_402_drops_paid_features_in_order(tmp_path: Path) -> None:
    rig = make_rig()
    scope = cloud_scope(tmp_path, rig.clock, "browserbase")
    bodies: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "proxies" in body:
            return httpx.Response(402, json={"error": "upgrade"})
        return httpx.Response(200, json={"id": "bb-1", "connectUrl": "wss://bb/x"})

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{BB}/v1/sessions").mock(side_effect=answer)
            router.post(f"{BB}/v1/sessions/bb-1").mock(return_value=httpx.Response(200))
            made = await rig.manager.session("t", scope)
            assert made.engine == "cloud:browserbase"
            await rig.manager.end("t")

    asyncio.run(go())
    assert [sorted(b) for b in bodies] == [
        ["keepAlive", "projectId", "proxies"],
        ["projectId", "proxies"],
        ["projectId"],
    ]


def test_provider_5xx_falls_back_to_local_once_with_a_notice(tmp_path: Path) -> None:
    rig = make_rig(SITES)
    scope = cloud_scope(tmp_path, rig.clock, "browser-use")

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            routes = mock_provider(router, "browser-use", status=503)
            first = json.loads(
                await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
            )
            again = json.loads(
                await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
            )
            assert first["success"] and again["success"]
            assert first["engine"] == "local" and again["engine"] == "local"
            assert "Browser Use" in first["notice"] and "HTTP 503" in first["notice"]
            assert "notice" not in again  # once per session
            assert routes["create"].call_count == 1  # no second paid attempt
            assert routes["close"].call_count == 0  # nothing was created
            await rig.manager.end("default")

    asyncio.run(go())
    assert len(rig.driver.launches) == 1


def test_attach_failure_releases_the_cloud_session_then_falls_back(tmp_path: Path) -> None:
    rig = make_rig()
    rig.driver.attach_failures = 1
    scope = cloud_scope(tmp_path, rig.clock, "firecrawl")

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            routes = mock_provider(router, "firecrawl")
            made = await rig.manager.session("t", scope)
            assert made.fell_back and made.engine == "local"
            assert routes["close"].call_count == 1  # paired even though attach failed
            notice = made.take_notice() or ""
            assert "ConnectionError" in notice and "wss://" not in notice
            await rig.manager.end("t")

    asyncio.run(go())


def test_guard_failure_releases_the_cloud_session(tmp_path: Path) -> None:
    rig = make_rig()
    rig.driver.guard_failures = 1
    scope = cloud_scope(tmp_path, rig.clock, "browser-use")

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            routes = mock_provider(router, "browser-use")
            made = await rig.manager.session("t", scope)
            assert made.fell_back
            assert routes["close"].call_count == 1
            await rig.manager.end("t")

    asyncio.run(go())


@pytest.mark.parametrize("how", ["force_reap", "shutdown", "janitor"])
def test_every_way_out_releases_the_cloud_session(tmp_path: Path, how: str) -> None:
    rig = make_rig()
    scope = cloud_scope(tmp_path, rig.clock, "browserbase")

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            routes = mock_provider(router, "browserbase")
            await rig.manager.session("t", scope)
            if how == "force_reap":
                await rig.manager.force_reap("t")
            elif how == "shutdown":
                await rig.manager.shutdown()
            else:
                rig.clock.advance(10_000)
                assert await rig.manager.janitor_tick() == ["t"]
            assert routes["close"].call_count == 1
            # Nothing is released twice, whatever runs next.
            await rig.manager.shutdown()
            assert routes["close"].call_count == 1

    asyncio.run(go())


def test_release_failure_never_raises(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig()
    scope = cloud_scope(tmp_path, rig.clock, "browser-use")

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{BU}/browsers").mock(
                return_value=httpx.Response(200, json={"id": "bu-1", "cdpUrl": "wss://bu/cdp"})
            )
            router.patch(f"{BU}/browsers/bu-1").mock(side_effect=httpx.ConnectError("down"))
            await rig.manager.session("t", scope)
            await rig.manager.end("t")
        assert rig.manager.get("t") is None

    with caplog.at_level(logging.DEBUG):
        asyncio.run(go())
    assert BU_KEY not in caplog.text


def test_missing_key_names_the_secret_and_falls_back(tmp_path: Path) -> None:
    rig = make_rig()
    scope = make_scope_for(tmp_path, rig.clock, {"browser": {"provider": "browserbase"}})

    async def go() -> None:
        with respx.mock(assert_all_called=True):
            made = await rig.manager.session("t", scope)
            assert made.fell_back
            notice = made.take_notice() or ""
            assert "BROWSERBASE_API_KEY" in notice and "BROWSERBASE_PROJECT_ID" in notice
            await rig.manager.end("t")

    asyncio.run(go())


def test_error_text_is_redacted_and_short(tmp_path: Path) -> None:
    rig = make_rig()
    scope = cloud_scope(tmp_path, rig.clock, "firecrawl")
    echoed = "bad key " + FC_KEY + " " + "z" * 500

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{FC}/v2/browser").mock(return_value=httpx.Response(401, text=echoed))
            with pytest.raises(CloudFailure) as raised:
                await CloudBrowsers().open("firecrawl", scope, "t")
            text = str(raised.value)
            assert raised.value.status == 401
            assert FC_KEY not in text
            assert len(text) < 320

    asyncio.run(go())


def test_credentials_come_from_secrets_then_scope_env(tmp_path: Path) -> None:
    rig = make_rig()
    base = make_scope_for(tmp_path, rig.clock, env={"FIRECRAWL_API_KEY": "from-env"})
    assert credential(base, "FIRECRAWL_API_KEY") == "from-env"
    stored = base.with_secrets(MemorySecretStore({"FIRECRAWL_API_KEY": "from-store"}))
    assert credential(stored, "FIRECRAWL_API_KEY") == "from-store"
    assert PROVIDERS["firecrawl"].ready(stored)
    assert not PROVIDERS["browserbase"].ready(stored)


def test_user_cdp_endpoint_never_launches(tmp_path: Path) -> None:
    rig = make_rig()
    rig.driver.attach_failures = 1
    scope = make_scope_for(tmp_path, rig.clock, {"browser": {"cdp_url": "http://127.0.0.1:9222"}})

    async def go() -> None:
        assert rig.manager.choice_for(scope) == Choice("cdp", "http://127.0.0.1:9222", "config")
        out = json.loads(
            await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
        )
        assert out["success"] is False

    asyncio.run(go())
    assert rig.driver.launches == []
    assert rig.driver.attaches == ["http://127.0.0.1:9222"]


def test_cdp_endpoint_wins_over_a_provider(tmp_path: Path) -> None:
    rig = make_rig()
    scope = cloud_scope(tmp_path, rig.clock, "browserbase", cdp_url="http://127.0.0.1:9222")
    assert rig.manager.choice_for(scope).mode == "cdp"
