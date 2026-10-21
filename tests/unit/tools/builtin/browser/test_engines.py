"""Alternative engines (P24-39): Lightpanda with its Chrome fallback, and Camofox over respx."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from indus.foundation.scope import Scope
from indus.tools.builtin.browser.engines import (
    READY_TIMEOUT_S,
    Camofox,
    EngineMissing,
    LightPage,
    Lightpanda,
    locate_tool,
    parse_snapshot,
)
from tests.fakes.browser import FakeSite, el
from tests.fakes.clock import checkpoint
from tests.fakes.secret_store import MemorySecretStore
from tests.unit.tools.builtin.browser._rig import Rig, make_rig, make_scope_for

PROVIDER_KEY = "OPENAI_API_" + "KEY"
FAKE_VALUE = "sk-" + "z" * 24
SITES = {
    "https://example.com/": FakeSite(title="Example", elements=[el("button", "Go")]),
}
CAMOFOX = "http://127.0.0.1:9377"


class FakeServer:
    def __init__(self, exit_code: int | None = None) -> None:
        self.pid = 4321
        self.exit_code = exit_code
        self.stopped = 0

    def poll(self) -> int | None:
        return self.exit_code

    async def stop(self) -> None:
        self.stopped += 1


class FakeLauncher:
    def __init__(self, exit_code: int | None = None) -> None:
        self.started: list[tuple[list[str], dict[str, str], Path]] = []
        self.servers: list[FakeServer] = []
        self.exit_code = exit_code

    async def start(self, argv: list[str], env: Mapping[str, str], log_path: Path) -> FakeServer:
        self.started.append((list(argv), dict(env), log_path))
        server = FakeServer(self.exit_code)
        self.servers.append(server)
        return server


def light_rig(
    *, ready: bool = True, exit_code: int | None = None, binary: bool = True
) -> tuple[Rig, FakeLauncher]:
    launcher = FakeLauncher(exit_code)

    async def probe(url: str) -> bool:
        return ready

    def locate(scope: Scope, name: str) -> Path | None:
        return Path("/opt/lightpanda/lightpanda") if binary else None

    engine = Lightpanda(launcher=launcher, probe=probe, locate=locate, platform="linux")
    return make_rig(dict(SITES), lightpanda=engine), launcher


def light_scope(tmp_path: Path, clock: Any) -> Scope:
    return make_scope_for(tmp_path, clock, {"browser": {"engine": "lightpanda"}}).with_secrets(
        MemorySecretStore({PROVIDER_KEY: FAKE_VALUE})
    )


def test_lightpanda_serves_through_cdp_with_a_clean_env(tmp_path: Path) -> None:
    rig, launcher = light_rig()
    scope = light_scope(tmp_path, rig.clock)

    async def go() -> None:
        out = json.loads(
            await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
        )
        assert out["success"] is True and out["engine"] == "lightpanda"
        await rig.manager.end("default")

    asyncio.run(go())
    [(argv, env, _log)] = launcher.started
    assert argv[1:4] == ["serve", "--host", "127.0.0.1"] and argv[4] == "--port"
    assert rig.driver.attaches == [f"http://127.0.0.1:{argv[5]}"]
    assert PROVIDER_KEY not in env and FAKE_VALUE not in env.values()
    assert launcher.servers[0].stopped == 1  # stopped with the session
    assert rig.driver.launches == []


def test_screenshot_on_lightpanda_goes_to_chrome(tmp_path: Path) -> None:
    rig, _ = light_rig()
    scope = light_scope(tmp_path, rig.clock)
    shot = tmp_path / "shot.png"

    async def go() -> None:
        await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
        session = rig.manager.current("default")
        assert session is not None
        await rig.manager.run(session, lambda p: p.screenshot(shot))
        page = session.page
        assert isinstance(page, LightPage) and page.served_by == "chrome-fallback"
        assert "no renderer" in page.take_notes()[0]
        await rig.manager.end("default")

    asyncio.run(go())
    assert shot.read_bytes().startswith(b"\x89PNG")
    [chrome] = [h for h in rig.driver.handles if h.how == "launch"]
    assert [c[0] for c in chrome.page.calls] == ["goto", "screenshot"]
    assert chrome.page.calls[0][1][0] == "https://example.com/"
    assert chrome.closed and chrome.gate is not None  # guarded, then closed
    light = next(h for h in rig.driver.handles if h.how == "attach")
    assert all(c[0] != "screenshot" for c in light.page.calls)
    assert not rig.driver.launches[0].profile_dir.exists()  # the temporary folder is gone


def test_failed_eligible_command_falls_back(tmp_path: Path) -> None:
    rig, _ = light_rig()
    scope = light_scope(tmp_path, rig.clock)
    rig.driver.eval_answers["document.title"] = "From Chrome"

    async def go() -> None:
        await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
        session = rig.manager.current("default")
        assert session is not None
        light = next(h for h in rig.driver.handles if h.how == "attach")

        async def unsupported(expression: str) -> Any:
            raise NotImplementedError("Runtime.evaluate is not supported")

        vars(light.page)["evaluate"] = unsupported
        value = await rig.manager.run(session, lambda p: p.evaluate("document.title"))
        assert value == "From Chrome"
        page = session.page
        assert isinstance(page, LightPage)
        assert "NotImplementedError" in page.take_notes()[0]
        await rig.manager.end("default")

    asyncio.run(go())
    assert len(rig.driver.launches) == 1


def test_ineligible_command_does_not_fall_back(tmp_path: Path) -> None:
    rig, _ = light_rig()
    scope = light_scope(tmp_path, rig.clock)

    async def go() -> None:
        await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
        session = rig.manager.current("default")
        assert session is not None
        light = next(h for h in rig.driver.handles if h.how == "attach")

        async def broken(dy: int) -> None:
            raise RuntimeError("scroll failed")

        vars(light.page)["scroll"] = broken
        with pytest.raises(RuntimeError, match="scroll failed"):
            await rig.manager.run(session, lambda p: p.scroll(500))
        await rig.manager.end("default")

    asyncio.run(go())
    assert rig.driver.launches == []


def test_nearly_empty_snapshot_falls_back(tmp_path: Path) -> None:
    sites = {"https://example.com/": FakeSite(title="Empty")}
    rig, _ = light_rig()
    rig.driver.sites = dict(sites)

    async def chrome_outline(full: bool, attr: str) -> list[dict[str, Any]]:
        return [{"depth": 0, "role": "button", "name": "Rendered", "ref": "e1"}]

    def on_launch(spec: Any) -> None:
        pass

    rig.driver.on_launch = on_launch
    scope = light_scope(tmp_path, rig.clock)

    async def go() -> None:
        await rig.manager.session("t", scope)
        session = rig.manager.current("t")
        assert session is not None
        await rig.manager.run(session, lambda p: p.goto("https://example.com/", 10))
        original_launch = rig.driver.launch

        async def launch(spec: Any) -> Any:
            handle = await original_launch(spec)
            vars(handle.page)["outline"] = chrome_outline
            return handle

        vars(rig.driver)["launch"] = launch
        rows = await rig.manager.run(session, lambda p: p.outline(False, "data-x"))
        assert rows[0]["name"] == "Rendered"
        page = session.page
        assert isinstance(page, LightPage) and "empty" in page.take_notes()[0]
        await rig.manager.end("t")

    asyncio.run(go())


def test_page_lightpanda_cannot_load_is_served_by_chrome(tmp_path: Path) -> None:
    rig, _ = light_rig()
    scope = light_scope(tmp_path, rig.clock)

    async def go() -> None:
        session = await rig.manager.session("t", scope)
        light = next(h for h in rig.driver.handles if h.how == "attach")

        async def cannot_load(url: str, timeout_s: float) -> str:
            raise RuntimeError("frame detached")

        vars(light.page)["goto"] = cannot_load
        landed = await rig.manager.run(session, lambda p: p.goto("https://example.com/", 10))
        assert landed == "https://example.com/"
        assert session.page.url() == "https://example.com/"
        assert await rig.manager.run(session, lambda p: p.title()) == "Example"
        await rig.manager.end("t")

    asyncio.run(go())
    assert len(rig.driver.launches) == 2  # open, then title, each in a fresh Chrome


def test_missing_binary_gives_an_install_hint(tmp_path: Path) -> None:
    rig, launcher = light_rig(binary=False)
    scope = light_scope(tmp_path, rig.clock)

    async def go() -> None:
        out = json.loads(
            await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
        )
        assert out["success"] is False
        assert "indus pm install lightpanda" in out["error"]

    asyncio.run(go())
    assert launcher.started == []


def test_windows_has_no_build(tmp_path: Path) -> None:
    rig = make_rig()
    engine = Lightpanda(platform="win32")
    scope = light_scope(tmp_path, rig.clock)

    async def go() -> None:
        with pytest.raises(EngineMissing, match="no Windows build"):
            await engine.open(rig.manager, scope, "t")

    asyncio.run(go())


def test_server_that_never_answers_is_stopped(tmp_path: Path) -> None:
    rig, launcher = light_rig(ready=False)
    scope = light_scope(tmp_path, rig.clock)

    async def go() -> None:
        task = asyncio.ensure_future(rig.manager.session("t", scope))
        for _ in range(500):
            await checkpoint()
            if task.done():
                break
            rig.clock.advance(0.5)
        with pytest.raises(EngineMissing, match=f"within {READY_TIMEOUT_S:g} s"):
            await task

    asyncio.run(go())
    assert launcher.servers[0].stopped >= 1
    assert rig.driver.attaches == []


def test_server_that_exits_reports_its_last_line(tmp_path: Path) -> None:
    rig, launcher = light_rig(ready=False, exit_code=3)
    scope = light_scope(tmp_path, rig.clock)
    original = launcher.start

    async def start(argv: list[str], env: Mapping[str, str], log_path: Path) -> FakeServer:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("starting\nerror: port in use\n")
        return await original(argv, env, log_path)

    vars(launcher)["start"] = start

    async def go() -> None:
        with pytest.raises(EngineMissing, match="code 3 before it answered: error: port in use"):
            await rig.manager.session("t", scope)

    asyncio.run(go())
    assert not launcher.started[0][2].exists()  # the log goes with the server


def test_locate_tool_asks_pm_then_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_rig()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "lightpanda"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    scope = make_scope_for(tmp_path, rig.clock, env={"PATH": str(bin_dir)})
    assert locate_tool(scope, "lightpanda") == tool
    assert locate_tool(scope, "no-such-tool") is None
    import indus.pm.api as pm

    monkeypatch.setattr(pm, "tool_path", lambda s, n: tmp_path / "store" / n, raising=False)
    assert locate_tool(scope, "lightpanda") == tmp_path / "store" / "lightpanda"


# -- Camofox -----------------------------------------------------------------------------------

SNAPSHOT = "\n".join(
    [
        '- heading "Shop" [level=1]',
        '- link "Home" [e1]',
        '  - img "Logo"',
        "    - /url: https://example.com/logo.png",
        '- textbox "Search" [e2]',
        '- button "Buy" [e3]',
    ]
)


def camofox_rig() -> Rig:
    return make_rig(dict(SITES), camofox=Camofox())


def camofox_scope(tmp_path: Path, clock: Any) -> Scope:
    return make_scope_for(tmp_path, clock, {"browser": {"engine": "camofox"}}).with_secrets(
        MemorySecretStore({"CAMOFOX_API_KEY": "cf" + "-test-" + "e5" * 8})
    )


def mock_camofox(router: respx.MockRouter) -> dict[str, Any]:
    return {
        "health": router.get(f"{CAMOFOX}/health").mock(return_value=httpx.Response(200, json={})),
        "tabs": router.post(f"{CAMOFOX}/tabs").mock(
            return_value=httpx.Response(200, json={"tabId": "t1"})
        ),
        "snapshot": router.get(f"{CAMOFOX}/tabs/t1/snapshot").mock(
            return_value=httpx.Response(200, json={"snapshot": SNAPSHOT, "refsCount": 3})
        ),
        "click": router.post(f"{CAMOFOX}/tabs/t1/click").mock(
            return_value=httpx.Response(200, json={"url": "https://example.com/cart"})
        ),
        "end": router.delete(url__regex=rf"{CAMOFOX}/sessions/indus-[0-9a-f]+").mock(
            return_value=httpx.Response(200, json={})
        ),
    }


def test_camofox_navigate_snapshot_click(tmp_path: Path) -> None:
    rig = camofox_rig()
    scope = camofox_scope(tmp_path, rig.clock)

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            routes = mock_camofox(router)
            out = json.loads(
                await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
            )
            assert out["success"] is True and out["engine"] == "camofox"
            assert "@e3" in out["snapshot"] or "e3" in out["snapshot"]
            opened = json.loads(routes["tabs"].calls.last.request.content)
            assert opened["url"] == "https://example.com/"
            assert routes["tabs"].calls.last.request.headers["Authorization"].startswith("Bearer ")
            clicked = json.loads(await rig.tools.pages.click({"ref": "@e3"}, scope=scope))
            assert clicked["success"] is True
            assert json.loads(routes["click"].calls.last.request.content)["ref"] == "e3"
            await rig.manager.end("default")
            assert routes["end"].call_count == 1

    asyncio.run(go())
    records = tmp_path / "home" / "cache" / "browser" / "camofox" / "sessions.json"
    assert json.loads(records.read_text()) == {}  # its own state folder, emptied at close


def test_camofox_recreates_a_collected_tab(tmp_path: Path) -> None:
    rig = camofox_rig()
    scope = camofox_scope(tmp_path, rig.clock)

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            routes = mock_camofox(router)
            router.post(f"{CAMOFOX}/tabs/t1/navigate").mock(return_value=httpx.Response(404))
            await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
            await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
            assert routes["tabs"].call_count == 2  # the collected tab was replaced
            await rig.manager.end("default")

    asyncio.run(go())


def test_camofox_down_gives_a_hint(tmp_path: Path) -> None:
    rig = camofox_rig()
    scope = camofox_scope(tmp_path, rig.clock)

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{CAMOFOX}/health").mock(side_effect=httpx.ConnectError("refused"))
            out = json.loads(
                await rig.tools.pages.navigate({"url": "https://example.com/"}, scope=scope)
            )
            assert out["success"] is False and "Start it" in out["error"]

    asyncio.run(go())


def test_camofox_closes_sessions_left_by_a_dead_process(tmp_path: Path) -> None:
    rig = camofox_rig()
    scope = camofox_scope(tmp_path, rig.clock)
    folder = tmp_path / "home" / "cache" / "browser" / "camofox"
    folder.mkdir(parents=True)
    (folder / "sessions.json").write_text(json.dumps({"indus-0000000000": {"pid": 999_999_9}}))

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            old = router.delete(f"{CAMOFOX}/sessions/indus-0000000000").mock(
                return_value=httpx.Response(200, json={})
            )
            mock_camofox(router)
            await rig.manager.session("t", scope)
            assert old.call_count == 1
            await rig.manager.end("t")

    asyncio.run(go())


def test_camofox_navigation_is_checked_by_the_gate(tmp_path: Path) -> None:
    rig = camofox_rig()
    scope = camofox_scope(tmp_path, rig.clock)

    async def go() -> None:
        with respx.mock(assert_all_called=False) as router:
            routes = mock_camofox(router)
            session = await rig.manager.session("t", scope)
            with pytest.raises(RuntimeError, match="Blocked"):
                await rig.manager.run(session, lambda p: p.goto("http://10.0.0.1/", 10))
            assert routes["tabs"].call_count == 0
            await rig.manager.end("t")

    asyncio.run(go())


def test_parse_snapshot_rows() -> None:
    rows = parse_snapshot(SNAPSHOT)
    assert rows[0] == {"depth": 0, "role": "heading", "name": "Shop", "level": 1}
    assert rows[1] == {"depth": 0, "role": "link", "name": "Home", "ref": "e1"}
    assert [r.get("ref") for r in rows] == [None, "e1", None, "e2", "e3"]
