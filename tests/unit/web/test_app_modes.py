"""P18-01: serve vs dashboard mode, the health answer, and the launch rules (75 / 78 / reuse)."""

from __future__ import annotations

import errno
import socket
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi import FastAPI

from indus.cli import app as app_mod
from indus.cli.args import CliState
from indus.foundation import config
from indus.foundation.scope import Scope
from indus.service.ws_auth import stable_profile_id
from indus.web import serve_runtime
from indus.web.app import AppOptions, DaemonLink, create_app
from indus.web.auth.basic import verify_password
from indus.web.auth.gate import GateRefused
from indus.web.command import EX_CONFIG, EX_TEMPFAIL, Launch, run_launch
from tests.unit.web.conftest import Builder, basic_config


@pytest.mark.parametrize(
    "path", ["/", "/sessions", "/index.html", "/assets/index-abc123.js", "/login", "/docs"]
)
def test_serve_mode_never_returns_html(build: Builder, path: str) -> None:
    built = build(mode="serve")
    for headers in ({}, built.auth()):
        response = built.client.get(path, headers=headers)
        assert response.status_code == 404, path
        assert response.headers["content-type"].startswith("application/json"), path
        assert "<html" not in response.text.lower()


def test_serve_mode_answers_the_api(build: Builder) -> None:
    built = build(mode="serve")
    health = built.client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert body["mode"] == "serve" and body["ok"] is True
    assert body["profile_id"] == stable_profile_id(built.state.home)
    assert built.client.get("/api/auth/me").status_code == 401
    assert built.client.get("/api/auth/me", headers=built.auth()).json()["provider"] == "loopback"
    assert built.client.get("/openapi.json").status_code == 404


def test_gated_serve_answers_json_not_redirects(build: Builder) -> None:
    built = build(mode="serve", host="0.0.0.0", config=basic_config())
    response = built.client.get("/sessions")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthenticated"


def test_gated_app_without_provider_is_refused(scope: Scope, bundle_dir: Path) -> None:
    with pytest.raises(GateRefused) as refused:
        create_app(scope, "dashboard", options=AppOptions(host="0.0.0.0", config={}))
    text = str(refused.value)
    assert "basic: no username set" in text and "oidc: no issuer set" in text


def test_daemon_link_ensures_in_the_background(build: Builder) -> None:
    seen: list[tuple[Path, str]] = []

    def fake_ensure(home: Path, env: dict[str, str], *, mode: str) -> str:
        seen.append((home, mode))
        return "ready"

    built = build(daemon=None)
    link = DaemonLink(built.state.home, {}, ensure=fake_ensure)
    built.state.daemon = link
    with built.client:
        link.wait(5)
    assert seen == [(built.state.home, "start")]
    assert link.verdict == "ready"


# -- launches -------------------------------------------------------------------------------------


class _Owner:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.pid = 4242
        self.url = serve_runtime.url_for(host, port)


def _launch(scope: Scope, **kwargs: Any) -> Launch:
    defaults: dict[str, Any] = {
        "scope": scope,
        "mode": "dashboard",
        "host": "127.0.0.1",
        "port": 9119,
        "open_browser": False,
        "build": False,
        "owner": lambda root: None,
    }
    defaults.update(kwargs)
    return Launch(**defaults)


def test_port_in_use_exits_75(scope: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    def taken(host: str, port: int) -> socket.socket:
        raise OSError(errno.EADDRINUSE, "Address already in use")

    assert run_launch(_launch(scope, bind=taken)) == EX_TEMPFAIL
    assert "in use" in capsys.readouterr().err


def test_a_live_owner_elsewhere_exits_78(scope: Scope, capsys: pytest.CaptureFixture[str]) -> None:
    owner = _Owner("127.0.0.1", 9200)
    code = run_launch(_launch(scope, owner=lambda root: owner, port=9119, port_given=True))
    assert code == EX_CONFIG
    err = capsys.readouterr().err
    assert "pid 4242" in err and "http://127.0.0.1:9200" in err


def test_a_named_profile_reuses_the_machine_backend(scope: Scope) -> None:
    opened: list[str] = []
    owner = _Owner("127.0.0.1", 9119)
    launch = _launch(
        scope, owner=lambda root: owner, port_given=False, open_browser=True, opener=opened.append
    )
    assert run_launch(launch) == 0
    assert opened == ["http://127.0.0.1:9119/?profile=work"]


def test_isolated_ignores_the_machine_backend(scope: Scope) -> None:
    served: list[FastAPI] = []

    def owner(root: Path) -> _Owner:
        raise AssertionError("an isolated launch must not look for the machine backend")

    launch = _launch(
        scope,
        isolated=True,
        port=0,
        owner=owner,
        serve=lambda app, sock: served.append(app),
    )
    assert run_launch(launch) == 0
    assert served and served[0].state.indus.profile_name == "work"
    assert not serve_runtime.dashboard_record_path(scope.root).exists()


def test_machine_launch_records_and_clears_its_owner_file(scope: Scope) -> None:
    seen: dict[str, Any] = {}

    def serve(app: FastAPI, sock: socket.socket) -> None:
        record = serve_runtime.read_record(serve_runtime.dashboard_record_path(scope.root))
        seen["record"] = record
        seen["state"] = app.state.indus

    assert run_launch(_launch(scope, port=0, serve=serve)) == 0
    record = seen["record"]
    assert record is not None and record.mode == "dashboard" and record.port > 0
    assert seen["state"].profile_name == "default"
    assert seen["state"].initial_profile == "work"
    assert not serve_runtime.dashboard_record_path(scope.root).exists()


def test_serve_launch_writes_the_profile_record(scope: Scope) -> None:
    seen: dict[str, Any] = {}

    def serve(app: FastAPI, sock: socket.socket) -> None:
        seen["record"] = serve_runtime.read_record(serve_runtime.serve_record_path(scope.home))
        seen["mode"] = app.state.indus.mode

    assert run_launch(_launch(scope, mode="serve", port=0, serve=serve)) == 0
    assert seen["mode"] == "serve"
    assert seen["record"].profile_id == stable_profile_id(scope.home)
    assert (serve_runtime.serve_record_path(scope.home)).exists() is False


def test_non_interactive_gated_start_without_provider_lists_reasons(
    scope: Scope, capsys: pytest.CaptureFixture[str]
) -> None:
    launch = _launch(scope, host="0.0.0.0", port=0, serve=lambda app, sock: None)
    assert run_launch(launch) == 1
    err = capsys.readouterr().err
    assert "basic: no username set" in err
    assert "oidc: no issuer set" in err


def test_interactive_gated_start_offers_a_password(scope: Scope) -> None:
    served: list[FastAPI] = []
    launch = _launch(
        scope,
        host="0.0.0.0",
        port=0,
        interactive=True,
        ask=lambda: ("admin", "a long passphrase"),
        serve=lambda app, sock: served.append(app),
        isolated=True,
    )
    assert run_launch(launch) == 0
    assert served and served[0].state.indus.gated
    saved = config.load(scope).get("dashboard.basic_auth")
    assert saved["username"] == "admin"
    assert verify_password("a long passphrase", saved["password_hash"])
    assert "a long passphrase" not in (scope.home / "config.yaml").read_text(encoding="utf-8")


def test_interactive_refusal_to_set_a_password_still_refuses(scope: Scope) -> None:
    launch = _launch(
        scope, host="0.0.0.0", port=0, interactive=True, ask=lambda: None, isolated=True
    )
    assert run_launch(launch) == 1


def test_the_commands_are_registered(scope: Scope) -> None:
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    result = CliRunner().invoke(app_mod.cli, ["dashboard", "--help"], obj=state)
    assert result.exit_code == 0 and "--isolated" in result.output
    assert "--insecure" not in result.output
    result = CliRunner().invoke(
        app_mod.cli, ["serve", "discover", "--json"], obj=state, standalone_mode=False
    )
    assert result.return_value == 1
    assert '"state": "absent"' in result.output


def test_the_machine_backend_runs_on_the_default_profile_s_own_env(scope: Scope) -> None:
    seen: dict[str, Any] = {}
    marker = "WORK_ONLY_" + "SETTING"
    (scope.home / ".env").write_text(f"{marker}=from-work\n", encoding="utf-8")
    launched = scope.with_env(scope.env.with_overlay({marker: "from-work"}))

    def serve(app: FastAPI, sock: socket.socket) -> None:
        seen["base"] = app.state.indus.extras["chat"].scopes.base

    assert run_launch(_launch(launched, port=0, serve=serve)) == 0
    base = seen["base"]
    assert base.home == scope.root and base.profile_id == "default"
    assert base.env.get("INDUS_HOME") == str(scope.root)
    assert marker not in base.env  # the launching profile's .env stays its own
