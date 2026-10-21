"""P24-37: dashboard plugins — discovery, manifest rules, assets, the plugin-route gate order and
in-process API routes, over the real app (``create_app``) with sample plugins written by us."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from indus.foundation.scope import Scope
from indus.web.app import AppOptions, create_app
from indus.web.auth.token import SESSION_HEADER
from indus.web.plugins import (
    EXTRAS_KEY,
    DashboardPlugins,
    ManifestRefused,
    normalise,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "web" / "plugins"
BASE_URL = "http://127.0.0.1:9119"


# A plugin's built bundle: ``dist/`` is build output (git-ignored), so the sample's is written here.
_BUILT: dict[str, dict[str, str]] = {
    "hello": {
        "dist/index.js": 'window.__INDUS_PLUGINS__.register("hello", function Hello() '
        "{ return null; });\n",
        "dist/style.css": ".hello { color: inherit; }\n",
    },
}


def _install(home: Path, *names: str) -> None:
    for name in names:
        target = home / "plugins" / name
        shutil.copytree(FIXTURES / name, target)
        for rel, text in _BUILT.get(name, {}).items():
            path = target / "dashboard" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


def _enable(home: Path, enabled: list[str], disabled: list[str] | None = None) -> None:
    lines = ["plugins:", f"  enabled: {json.dumps(enabled)}"]
    if disabled is not None:
        lines.append(f"  disabled: {json.dumps(disabled)}")
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class Rig:
    def __init__(self, scope: Scope, mode: str = "serve") -> None:
        self.app = create_app(scope, mode, options=AppOptions(config={}))  # type: ignore[arg-type]
        self.state: Any = self.app.state.indus
        self.anon = TestClient(self.app, base_url=BASE_URL, follow_redirects=False)
        self.client = TestClient(self.app, base_url=BASE_URL, follow_redirects=False)
        self.client.headers[SESSION_HEADER] = self.state.auth.token.value

    @property
    def catalogue(self) -> DashboardPlugins:
        found = self.state.extras[EXTRAS_KEY]
        assert isinstance(found, DashboardPlugins)
        return found

    def close(self) -> None:
        self.anon.close()
        self.client.close()


@pytest.fixture
def rig_for(scope: Scope) -> Iterator[Any]:
    made: list[Rig] = []

    def build(mode: str = "serve") -> Rig:
        rig = Rig(scope, mode)
        made.append(rig)
        return rig

    yield build
    for rig in made:
        rig.close()


def test_listing_shows_enabled_plugins_without_internal_fields(scope: Scope, rig_for: Any) -> None:
    _install(scope.home, "hello", "notes", "takeover", "sneaky")
    _enable(scope.home, ["hello", "notes", "takeover", "sneaky"])
    rig = rig_for()
    reply = rig.client.get("/api/dashboard/plugins")
    assert reply.status_code == 200
    plugins = {p["name"]: p for p in reply.json()["plugins"]}
    assert set(plugins) == {"hello", "notes", "sneaky"}  # takeover refused (/plugins override)
    hello = plugins["hello"]
    assert hello == {
        "name": "hello",
        "label": "Hello",
        "description": "A sample tab.",
        "icon": "Sparkles",
        "version": "1.2.0",
        "tab": {"path": "/hello", "position": "after:skills"},
        "slots": ["sidebar", "sessions:top"],
        "entry": "dist/index.js",
        "css": "dist/style.css",
        "has_api": True,
        "source": "user",
        "integrity": "sha384-" + "A" * 66,
    }
    assert plugins["notes"]["icon"] == "Puzzle"  # unknown icon → puzzle
    assert plugins["notes"]["tab"]["override"] == "/logs"
    assert plugins["sneaky"]["has_api"] is False  # an api path outside the folder
    assert "integrity" not in plugins["sneaky"]  # not an SRI value
    assert all("folder" not in p and "api_file" not in p for p in plugins.values())


def test_not_enabled_and_disabled_plugins_are_not_listed(scope: Scope, rig_for: Any) -> None:
    _install(scope.home, "hello", "notes")
    _enable(scope.home, ["hello", "notes"], disabled=["notes"])
    rig = rig_for()
    names = [p["name"] for p in rig.client.get("/api/dashboard/plugins").json()["plugins"]]
    assert names == ["hello"]
    _enable(scope.home, [])
    rig.catalogue.rescan()
    assert rig.client.get("/api/dashboard/plugins").json()["plugins"] == []


def test_user_copy_wins_and_project_needs_the_opt_in(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = tmp_path / "bundled"
    shutil.copytree(FIXTURES / "hello", bundled / "hello")
    data = json.loads((bundled / "hello" / "dashboard" / "manifest.json").read_text())
    data["label"] = "Bundled hello"
    (bundled / "hello" / "dashboard" / "manifest.json").write_text(json.dumps(data))
    shutil.copytree(FIXTURES / "notes", bundled / "notes")
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "sneaky", project / ".indus" / "plugins" / "sneaky")
    _install(scope.home, "hello")
    _enable(scope.home, ["hello", "sneaky"])
    catalogue = DashboardPlugins(scope, bundled_dir=bundled, project_root=lambda: project)
    listed = {p.name: p for p in catalogue.listed()}
    assert listed["hello"].label == "Hello" and listed["hello"].source == "user"
    assert listed["notes"].source == "bundled"  # bundled: on without being listed
    assert "sneaky" not in listed
    opted = DashboardPlugins(
        scope.with_env(scope.env.with_overlay({"INDUS_ENABLE_PROJECT_PLUGINS": "yes"})),
        bundled_dir=bundled,
        project_root=lambda: project,
    )
    assert opted.get("sneaky") is not None and opted.get("sneaky").source == "project"  # type: ignore[union-attr]
    off = DashboardPlugins(
        scope.with_env(scope.env.with_overlay({"INDUS_ENABLE_PROJECT_PLUGINS": "0"})),
        bundled_dir=bundled,
        project_root=lambda: project,
    )
    assert off.get("sneaky") is None


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard-plugins/hello/../../config.yaml",
        "/dashboard-plugins/hello/%2e%2e/%2e%2e/config.yaml",
        "/dashboard-plugins/hello/dist/..%2f..%2fapi.py",
        "/dashboard-plugins/hello/..%5c..%5cconfig.yaml",
        "/dashboard-plugins/hello/dist/missing.js",
        "/dashboard-plugins/nobody/dist/index.js",
    ],
)
def test_asset_traversal_and_unknown_files_are_404(scope: Scope, rig_for: Any, path: str) -> None:
    _install(scope.home, "hello")
    _enable(scope.home, ["hello"])
    (scope.home / "config.yaml").touch()
    rig = rig_for()
    assert rig.client.get(path).status_code == 404


def test_assets_are_served_with_their_type(scope: Scope, rig_for: Any) -> None:
    _install(scope.home, "hello")
    _enable(scope.home, ["hello"])
    rig = rig_for()
    script = rig.anon.get("/dashboard-plugins/hello/dist/index.js")  # loopback: no header needed
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert script.headers["x-content-type-options"] == "nosniff"
    assert 'register("hello"' in script.text
    css = rig.anon.get("/dashboard-plugins/hello/dist/style.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")


def test_disabled_plugin_assets_are_404(scope: Scope, rig_for: Any) -> None:
    _install(scope.home, "hello")
    _enable(scope.home, ["hello"], disabled=["hello"])
    rig = rig_for()
    assert rig.client.get("/dashboard-plugins/hello/dist/index.js").status_code == 404


def test_plugin_api_mounted_in_process_behind_the_gates(scope: Scope, rig_for: Any) -> None:
    _install(scope.home, "hello")
    _enable(scope.home, ["hello"])
    rig = rig_for()
    assert rig.client.get("/api/ext/hello/ping").json() == {"pong": "hello"}
    assert rig.anon.get("/api/ext/hello/ping").status_code == 401


def test_disabled_plugin_is_401_without_auth_and_404_with_it(scope: Scope, rig_for: Any) -> None:
    _install(scope.home, "hello")
    _enable(scope.home, ["hello"])
    rig = rig_for()
    assert rig.client.get("/api/ext/hello/ping").status_code == 200
    _enable(scope.home, ["hello"], disabled=["hello"])
    rig.catalogue.rescan()
    assert rig.anon.get("/api/ext/hello/ping").status_code == 401
    assert rig.client.get("/api/ext/hello/ping").status_code == 404
    # never installed: the same answers, so a 404 never tells an anonymous caller what exists
    assert rig.anon.get("/api/ext/ghost/ping").status_code == 401
    assert rig.client.get("/api/ext/ghost/ping").status_code == 404


def test_rescan_finds_and_mounts_a_new_plugin(scope: Scope, rig_for: Any) -> None:
    rig = rig_for("dashboard")
    assert rig.client.get("/api/dashboard/plugins").json()["plugins"] == []
    _install(scope.home, "hello")
    _enable(scope.home, ["hello"])
    assert rig.client.get("/api/ext/hello/ping").status_code == 404
    reply = rig.client.post("/api/dashboard/plugins/rescan")
    assert [p["name"] for p in reply.json()["plugins"]] == ["hello"]
    assert rig.client.get("/api/ext/hello/ping").json() == {"pong": "hello"}
    # the SPA's catch-alls still come after the new routes
    assert rig.client.get("/api/nothing-here").status_code == 404


def test_project_plugin_api_is_never_imported(scope: Scope, tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(FIXTURES / "hello", project / ".indus" / "plugins" / "hello")
    _enable(scope.home, ["hello"])
    imported: list[str] = []
    catalogue = DashboardPlugins(
        scope.with_env(scope.env.with_overlay({"INDUS_ENABLE_PROJECT_PLUGINS": "1"})),
        bundled_dir=tmp_path / "none",
        project_root=lambda: project,
        importer=lambda name, _path: imported.append(name),
    )
    app = create_app(scope, "serve", options=AppOptions(config={}))
    assert catalogue.get("hello") is not None
    assert catalogue.mount(app) == []
    assert imported == []
    assert catalogue.api_allowed("hello") is False


def test_bad_manifest_is_skipped_others_load(scope: Scope, rig_for: Any) -> None:
    _install(scope.home, "hello")
    broken = scope.home / "plugins" / "broken" / "dashboard"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{ not json", encoding="utf-8")
    _enable(scope.home, ["hello", "broken"])
    rig = rig_for()
    names = [p["name"] for p in rig.client.get("/api/dashboard/plugins").json()["plugins"]]
    assert names == ["hello"]


@pytest.mark.parametrize(
    ("manifest", "problem"),
    [
        ({"name": "Bad Name"}, "plugin name"),
        ({"name": "x", "tab": {"path": "/a/b"}}, "one segment"),
        ({"name": "x", "tab": {"path": "/x", "override": "/plugins"}}, "cannot be overridden"),
        ({"name": "x", "tab": {"path": "/x", "override": "/nowhere"}}, "not a built-in"),
        ({"name": "x", "tab": {"path": "/logs"}}, "built-in page"),
        ({"name": "x", "entry": "/etc/passwd"}, "entry"),
        ({"name": "x", "entry": "../x.js"}, "entry"),
        ({"name": "x", "css": "..\\x.css"}, "css"),
    ],
)
def test_manifest_rules(tmp_path: Path, manifest: dict[str, Any], problem: str) -> None:
    folder = tmp_path / "x" / "dashboard"
    folder.mkdir(parents=True)
    with pytest.raises(ManifestRefused, match=problem):
        normalise(manifest, folder, "user")


def test_manifest_defaults(tmp_path: Path) -> None:
    folder = tmp_path / "demo" / "dashboard"
    folder.mkdir(parents=True)
    plugin = normalise({"tab": {"position": "before:bogus/seg", "hidden": True}}, folder, "user")
    assert plugin.name == "demo"
    assert plugin.tab == {"path": "/demo", "position": "end", "hidden": True}
    assert plugin.entry == "dist/index.js" and plugin.icon == "Puzzle"
    assert normalise({"tab": {"position": "before:cron"}}, folder, "user").tab["position"] == (
        "before:cron"
    )


def test_plugin_socket_goes_through_the_shared_helper(scope: Scope) -> None:
    import asyncio

    from indus.foundation.clock import SystemClock
    from indus.gateway.http.serving import HttpServer, bind_listener
    from indus.service.ws_auth import dashboard_offer
    from tests.fakes.pty.ws_wire import WireClient, WireClosed

    _install(scope.home, "hello", "notes")
    _enable(scope.home, ["hello", "notes"])

    async def run() -> None:
        sock = await bind_listener("127.0.0.1", 0, SystemClock())
        port = int(sock.getsockname()[1])
        app = create_app(scope, "serve", options=AppOptions(port=port, config={}))
        token = app.state.indus.auth.token.value
        http = HttpServer(app, sock, name="plugin-ws-test", ws={"ws": "wsproto"})
        base = f"ws://127.0.0.1:{port}"
        async with app.router.lifespan_context(app):
            await http.start()
            try:
                good = await WireClient.connect(
                    f"{base}/api/ext/hello/live", dashboard_offer("token", token)
                )
                assert await good.recv() == "hello token"
                bare = await WireClient.connect(
                    f"{base}/api/ext/hello/live", ["indus-dashboard-v1"]
                )
                assert (await bare.closed_with()).code == 4401
                # a plugin without a backend: the plugin-route gate refuses before the upgrade
                with pytest.raises(WireClosed):
                    await WireClient.connect(
                        f"{base}/api/ext/notes/live", dashboard_offer("token", token)
                    )
            finally:
                await http.stop()

    asyncio.run(run())
