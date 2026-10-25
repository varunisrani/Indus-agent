"""P18-14: the Plugins and MCP REST routes, the dashboard MCP sign-in and its public callback."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from mcp.shared.auth import OAuthToken

from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope
from indus.mcp.config import McpServer
from indus.mcp.oauth import IndusOAuthProvider, OAuthStore, secrets_for
from indus.plugins.install import InstallBlocked, InstallError, InstallResult
from indus.safety.content_scan import ScanReport
from indus.web.routes import mcp, mcp_oauth, plugins
from indus.web.routes.mcp_oauth import OAuthFlow
from tests.contract.web.rest_app import RestApp, build_rest_app, route_table, unscoped_writes

WRITE = {"profile": "work"}
FAKE_SERVER = Path(__file__).resolve().parents[2] / "fakes" / "mcp" / "stdio_server.py"
REMOTE = "https://mcp.example.test/mcp"
SHA = "0123456789abcdef" * 2 + "01234567"


class FakeInstaller:
    """The ``plugins.installer`` seam: records installs; ``outcome`` is returned or raised."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, bool]] = []
        self.outcome: Any = None
        self.installed: dict[str, dict[str, Any]] = {}

    def __call__(self, _scope: Scope) -> FakeInstaller:
        return self

    def install(
        self, identifier: str, *, ref: str | None = None, force: bool = False, confirm: Any = None
    ) -> InstallResult:
        self.calls.append((identifier, ref, force))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def records(self) -> dict[str, dict[str, Any]]:
        return self.installed


class OAuthPlay:
    """The ``mcp.oauth_connect`` seam: plays the IdP and the server for a dashboard sign-in."""

    def __init__(self) -> None:
        self.codes: list[str] = []

    async def __call__(
        self, name: str, server: McpServer, scope: Scope, auth: IndusOAuthProvider
    ) -> list[str]:
        query = urlencode({"client_id": "dashboard", "state": "st-" + "x" * 20})
        redirect = auth.context.redirect_handler
        callback = auth.context.callback_handler
        assert redirect is not None and callback is not None
        await redirect(f"https://auth.example.test/authorize?{query}")
        result = await callback()
        self.codes.append(result.code)
        access = "at-" + "q" * 24
        await auth.store.set_tokens(OAuthToken(access_token=access, token_type="Bearer"))
        return ["search", "fetch"]


class Flows:
    """The ``mcp.oauth_start`` seam: runs each flow on a thread the test can join."""

    def __init__(self, play: OAuthPlay) -> None:
        self.play = play
        self.by_id: dict[str, threading.Thread] = {}

    def __call__(self, flow: OAuthFlow, *args: Any) -> None:
        self.by_id[flow.flow_id] = mcp_oauth.start_thread(flow, *args, connect=self.play)


@pytest.fixture
def installer() -> FakeInstaller:
    return FakeInstaller()


@pytest.fixture
def play() -> OAuthPlay:
    return OAuthPlay()


@pytest.fixture
def threads(play: OAuthPlay) -> Flows:
    return Flows(play)


@pytest.fixture
def rest(scope: Scope, installer: FakeInstaller, threads: Flows) -> Iterator[RestApp]:
    config_loader.reset()
    built = build_rest_app(
        scope, extras={"plugins.installer": installer, "mcp.oauth_start": threads}
    )
    built.client.params = WRITE  # the test root holds two profiles: writes must name one
    yield built
    built.close()
    config_loader.reset()


def _code(answer: Any) -> str:
    return answer.json()["detail"]["code"]


def _config(scope: Scope) -> str:
    return (scope.home / "config.yaml").read_text(encoding="utf-8")


# -- plugins ---------------------------------------------------------------------------------------


def _plugin(scope: Scope, name: str, **manifest: Any) -> Path:
    folder = scope.home / "plugins" / name
    folder.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "version": "1.2.0", "description": f"The {name} plugin.", **manifest}
    lines = [f"{k}: {json.dumps(v)}" for k, v in data.items()]
    (folder / "plugin.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (folder / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    return folder


def test_plugins_list_enable_disable(rest: RestApp, scope: Scope, installer: FakeInstaller) -> None:
    _plugin(scope, "weather")
    installer.installed = {
        "weather": {"revision": SHA, "pinned": True, "source": "https://h/x.git"}
    }
    rows = {r["name"]: r for r in rest.client.get("/api/plugins").json()["plugins"]}
    row = rows["weather"]
    assert row["enabled"] is False and row["version"] == "1.2.0" and row["sha"] == SHA
    assert row["pinned"] is True and row["description"] == "The weather plugin."

    on = rest.client.post("/api/plugins/weather/enable")
    assert on.status_code == 200 and on.json()["enabled"] is True
    assert "weather" in _config(scope)
    assert {r["name"]: r for r in rest.client.get("/api/plugins").json()["plugins"]}["weather"][
        "enabled"
    ]
    off = rest.client.post("/api/plugins/weather/disable")
    assert off.json() == {"ok": True, "name": "weather", "enabled": False}
    rows = {r["name"]: r for r in rest.client.get("/api/plugins").json()["plugins"]}
    assert rows["weather"]["state"] == "disabled"
    missing = rest.client.post("/api/plugins/nope/enable")
    assert missing.status_code == 404 and _code(missing) == "plugin_not_found"


def test_enabling_a_plugin_that_needs_packages_needs_a_terminal(
    rest: RestApp, scope: Scope
) -> None:
    _plugin(scope, "heavy", python_dependencies=["requests==2.32.3"])
    answer = rest.client.post("/api/plugins/heavy/enable")
    assert answer.status_code == 409 and _code(answer) == "dependencies_required"
    assert not (scope.home / "config.yaml").exists() or "heavy" not in _config(scope)


@pytest.mark.parametrize("sha", ["abc123", "g" * 40, SHA[:39], ""])
def test_plugin_install_refuses_a_short_or_bad_sha(
    rest: RestApp, installer: FakeInstaller, sha: str
) -> None:
    answer = rest.client.post("/api/plugins/install", json={"url": "acme/tool", "sha": sha})
    assert answer.status_code == 422
    assert installer.calls == []


@pytest.mark.parametrize("url", ["/srv/plugins/x", "./x", "~/x", "file:///srv/x", "x y"])
def test_plugin_install_refuses_local_sources(
    rest: RestApp, installer: FakeInstaller, url: str
) -> None:
    answer = rest.client.post("/api/plugins/install", json={"url": url, "sha": SHA})
    assert answer.status_code == 400 and _code(answer) == "invalid_source"
    assert installer.calls == []


def test_plugin_install_blocked_by_the_scan_is_409(rest: RestApp, installer: FakeInstaller) -> None:
    report = ScanReport(target="evil", kind="plugin", verdict="dangerous")
    installer.outcome = InstallBlocked("install of evil blocked: dangerous content", report)
    answer = rest.client.post(
        "/api/plugins/install", json={"url": "https://git.example.test/evil.git", "sha": SHA}
    )
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "scan_blocked"
    assert answer.json()["detail"]["verdict"] == "dangerous"
    assert installer.calls == [("https://git.example.test/evil.git", SHA, False)]


def test_plugin_install_success_does_not_enable(
    rest: RestApp, installer: FakeInstaller, tmp_path: Path
) -> None:
    report = ScanReport(target="tool", kind="plugin", verdict="safe")
    record = {"revision": SHA, "pinned": True}
    installer.outcome = InstallResult("tool", tmp_path / "tool", record, report, None)
    answer = rest.client.post("/api/plugins/install", json={"url": "acme/tool", "sha": SHA.upper()})
    assert answer.status_code == 200
    assert answer.json() == {
        "ok": True,
        "name": "tool",
        "sha": SHA,
        "verdict": "safe",
        "enabled": False,
    }
    assert installer.calls == [("acme/tool", SHA, False)]
    installer.outcome = InstallError("plugin tool is already installed")
    again = rest.client.post("/api/plugins/install", json={"url": "acme/tool", "sha": SHA})
    assert again.status_code == 400 and _code(again) == "install_failed"


# -- MCP servers -----------------------------------------------------------------------------------


def _servers(rest: RestApp) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in rest.client.get("/api/mcp/servers").json()["servers"]}


def _add_fake(rest: RestApp, name: str = "fake", **extra: Any) -> Any:
    body = {"name": name, "command": sys.executable, "args": [str(FAKE_SERVER)], **extra}
    return rest.client.post("/api/mcp/servers", json=body)


def test_mcp_add_list_toggle_delete(rest: RestApp, scope: Scope) -> None:
    plain = "hello-" + "world" * 3
    added = _add_fake(rest, env={"GREETING": plain, "TOKEN": "${MY_TOKEN}"})
    assert added.status_code == 201, added.text
    assert _add_fake(rest).status_code == 409
    row = _servers(rest)["fake"]
    assert row["transport"] == "stdio" and row["enabled"] is True and row["auth"] == "none"
    assert row["env"]["TOKEN"] == "${MY_TOKEN}"
    assert row["env"]["GREETING"] != plain and plain not in rest.client.get("/api/mcp/servers").text
    assert plain in _config(scope)  # stored as written; only the answer is redacted

    off = rest.client.put("/api/mcp/servers/fake/enabled", json={"enabled": False})
    assert off.json() == {"ok": True, "name": "fake", "enabled": False}
    assert _servers(rest)["fake"]["enabled"] is False
    assert rest.client.delete("/api/mcp/servers/fake").json()["ok"] is True
    assert "fake" not in _servers(rest)
    assert rest.client.delete("/api/mcp/servers/fake").status_code == 404


def test_mcp_catalog_lists_the_presets_and_installs_through_the_add_route(rest: RestApp) -> None:
    """W55-F2-3: the catalogue is ``indus mcp add --preset``'s list; its entry adds as it reads."""
    from indus.mcp import presets

    rows = rest.client.get("/api/mcp/catalog").json()["entries"]
    assert [r["name"] for r in rows] == sorted(presets.PRESETS)
    assert all(r["description"] and r["transport"] == "stdio" for r in rows)
    assert not any(r["installed"] for r in rows)
    git = next(r for r in rows if r["name"] == "git")
    assert {"command": git["command"], "args": git["args"]} == presets.entry("git")
    body = {"name": "git", "command": git["command"], "args": git["args"]}
    assert rest.client.post("/api/mcp/servers", json=body).status_code == 201
    rows = {r["name"]: r for r in rest.client.get("/api/mcp/catalog").json()["entries"]}
    assert rows["git"]["installed"] is True and rows["memory"]["installed"] is False
    assert _servers(rest)["git"]["args"] == git["args"]


def test_mcp_add_suggests_the_jobs_waiting_for_the_server(rest: RestApp, scope: Scope) -> None:
    """Wave 45 (D-16.981): a skill blueprint waiting for this server becomes a suggestion."""
    from indus.cron.suggestions import SuggestionStore

    folder = scope.home / "skills" / "watch"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: watch\ndescription: d\n"
        'blueprint: {key: files, title: Watch files, schedule: "every 240m", '
        'prompt: "Check the files.", integrations: ["mcp:fake"], slots: []}\n'
        "---\n\n# watch\n",
        encoding="utf-8",
    )
    assert _add_fake(rest, name="other").status_code == 201
    assert SuggestionStore(scope).pending() == []
    assert _add_fake(rest).status_code == 201
    pending = SuggestionStore(scope).pending()
    assert [(s.dedup_key, s.source) for s in pending] == [("blueprint:watch:files", "integration")]


def test_mcp_add_refuses_bad_shapes_and_literal_secrets(rest: RestApp) -> None:
    secret = "kv-" + "Zq7r" * 8
    both = {"name": "x", "command": "a", "url": REMOTE}
    assert rest.client.post("/api/mcp/servers", json=both).status_code == 422
    assert rest.client.post("/api/mcp/servers", json={"name": "x"}).status_code == 422
    leaked = _add_fake(rest, "leaky", env={"API_KEY": secret})
    assert leaked.status_code == 400 and _code(leaked) == "secret_value"
    assert secret not in leaked.text
    bad_name = _add_fake(rest, "../etc")
    assert bad_name.status_code == 400 and _code(bad_name) == "invalid_name"


def test_mcp_lists_a_broken_entry_with_its_problem(rest: RestApp, scope: Scope) -> None:
    (scope.home / "config.yaml").write_text(
        "mcp_servers:\n  broken:\n    args: [x]\n", encoding="utf-8"
    )
    row = _servers(rest)["broken"]
    assert row["enabled"] is False and row["problem"]


def test_mcp_test_against_the_fake_stdio_server_lists_tools(rest: RestApp) -> None:
    assert _add_fake(rest).status_code == 201
    answer = rest.client.post("/api/mcp/servers/fake/test")
    assert answer.status_code == 200
    body = answer.json()
    assert body["ok"] is True, body
    names = {tool["name"] for tool in body["tools"]}
    assert {"echo", "add"} <= names
    assert rest.client.post("/api/mcp/servers/nope/test").status_code == 404


def test_mcp_test_reports_a_failure_without_a_500(rest: RestApp) -> None:
    added = rest.client.post(
        "/api/mcp/servers",
        json={
            "name": "crashy",
            "command": sys.executable,
            "args": [str(FAKE_SERVER), "--crash-on-start"],
        },
    )
    assert added.status_code == 201
    body = rest.client.post("/api/mcp/servers/crashy/test").json()
    assert body["ok"] is False and body["error"] and body["tools"] == []


def test_mcp_test_of_an_oauth_server_without_sign_in(rest: RestApp) -> None:
    added = rest.client.post(
        "/api/mcp/servers", json={"name": "remote", "url": REMOTE, "auth": "oauth"}
    )
    assert added.status_code == 201 and added.json()["auth"] == "oauth"
    body = rest.client.post("/api/mcp/servers/remote/test").json()
    assert body == {"ok": False, "error": "This server needs a sign-in first.", "tools": []}


# -- MCP sign-in from the dashboard ----------------------------------------------------------------


def _settled(rest: RestApp, threads: Flows, flow_id: str) -> dict[str, Any]:
    """The flow's snapshot once its thread has ended."""
    threads.by_id[flow_id].join(10)
    return rest.client.get(f"/api/mcp/oauth/flows/{flow_id}").json()


def _start_sign_in(rest: RestApp) -> dict[str, Any]:
    assert rest.client.post(
        "/api/mcp/servers", json={"name": "remote", "url": REMOTE, "auth": "oauth"}
    ).status_code in (201, 409)
    started = rest.client.post("/api/mcp/servers/remote/auth")
    assert started.status_code == 200, started.text
    return started.json()


def _state_of(snapshot: Mapping[str, Any]) -> str:
    return parse_qs(urlsplit(snapshot["authorization_url"]).query)["state"][0]


def test_oauth_callback_is_public_and_matches_state(
    rest: RestApp, scope: Scope, play: OAuthPlay, threads: Flows
) -> None:
    started = _start_sign_in(rest)
    assert started["status"] == "authorization_required"
    state = _state_of(started)

    anon = rest.anonymous()
    stranger = anon.get("/api/mcp/oauth/callback", params={"state": "nope<b>", "code": "c1"})
    assert stranger.status_code == 404 and "nope" not in stranger.text
    assert stranger.headers["content-type"].startswith("text/html")

    back = anon.get("/api/mcp/oauth/callback", params={"state": state, "code": "code-1"})
    assert back.status_code == 200 and "code-1" not in back.text
    done = _settled(rest, threads, started["flow_id"])
    assert done["status"] == "done" and done["tools"] == ["search", "fetch"]
    assert play.codes == ["code-1"]
    store = OAuthStore(secrets_for(scope), "remote", clock=scope.clock, home=scope.home)
    assert store.has_tokens()

    replay = anon.get("/api/mcp/oauth/callback", params={"state": state, "code": "code-2"})
    assert replay.status_code == 404
    assert play.codes == ["code-1"]


def test_oauth_refusal_and_cancel_end_the_flow(rest: RestApp, scope: Scope, threads: Flows) -> None:
    started = _start_sign_in(rest)
    refused = rest.anonymous().get(
        "/api/mcp/oauth/callback", params={"state": _state_of(started), "error": "access_denied"}
    )
    assert refused.status_code == 400
    ended = _settled(rest, threads, started["flow_id"])
    assert ended["status"] == "error" and "refused" in ended["error"]
    store = OAuthStore(secrets_for(scope), "remote", clock=scope.clock, home=scope.home)
    assert not store.has_tokens()

    second = _start_sign_in(rest)
    busy = rest.client.post("/api/mcp/servers/remote/auth")
    assert busy.status_code == 409 and _code(busy) == "flow_in_progress"
    cancelled = rest.client.delete(f"/api/mcp/oauth/flows/{second['flow_id']}")
    assert cancelled.json() == {"ok": True, "status": "cancelled"}
    assert _settled(rest, threads, second["flow_id"])["status"] == "cancelled"
    assert rest.client.get("/api/mcp/oauth/flows/unknown").status_code == 404


def test_oauth_needs_a_remote_server(rest: RestApp) -> None:
    assert _add_fake(rest).status_code == 201
    answer = rest.client.post("/api/mcp/servers/fake/auth")
    assert answer.status_code == 400 and _code(answer) == "oauth_not_applicable"


# -- scope rules and the route list ----------------------------------------------------------------


def test_every_write_depends_on_the_write_scope(rest: RestApp) -> None:
    assert unscoped_writes(rest.app, ("/api/plugins", "/api/mcp")) == []


def test_route_lists_match_the_app(rest: RestApp) -> None:
    assert route_table(rest.app, ("/api/plugins",)) == set(plugins.ROUTE_LIST)
    assert route_table(rest.app, ("/api/mcp",)) == set(mcp.ROUTE_LIST)
