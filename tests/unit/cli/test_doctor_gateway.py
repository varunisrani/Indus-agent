"""P16-36: the `indus doctor` messaging gateway section."""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.cli import doctor
from indus.cli.doctor import Finding
from indus.cli.doctor import gateway_checks as gc
from indus.foundation.config import loader as config_loader
from indus.foundation.scope import EnvView, Scope
from indus.gateway import registry as registry_mod
from indus.state.gateway_runtime import process_start_time, status_path

REAL_TCP = gc.tcp_reachable
TOKEN = "tg-test-" + "q" * 12
SECRET = "line-" + "s" * 10


def profile(scope: Scope, env: dict[str, str], config: str = "") -> Scope:
    home = scope.home
    (home / "config.yaml").write_text("_config_version: 1\n" + config)
    config_loader.invalidate()
    return scope.with_env(EnvView({"INDUS_HOME": str(home), **env}))


def rows(scope: Scope) -> dict[str, Finding]:
    return {f.id: f for f in gc.gateway_findings(scope)}


def texts(scope: Scope) -> list[str]:
    return [f"{f.severity} {f.message} | {f.fix or ''}" for f in gc.gateway_findings(scope)]


@pytest.fixture(autouse=True)
def quiet_probes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No probe leaves the process: every reachability probe answers "refused"."""
    asked: list[str] = []

    def http(url: str) -> str | None:
        asked.append(url)
        return "ConnectError"

    def tcp(host: str, port: int) -> str | None:
        asked.append(f"tcp://{host}:{port}")
        return "ConnectionRefusedError"

    monkeypatch.setattr(gc, "http_reachable", http)
    monkeypatch.setattr(gc, "tcp_reachable", tcp)
    return asked


# -- platforms and credentials ---------------------------------------------------------------------


def test_nothing_set_up_is_one_quiet_line(scope: Scope) -> None:
    found = gc.gateway_findings(profile(scope, {}))
    assert [(f.id, f.severity) for f in found] == [("gateway.platforms", "info")]
    assert "indus gateway setup" in found[0].message


def test_a_platform_that_is_on_shows_names_never_values(scope: Scope) -> None:
    ready = profile(scope, {"TELEGRAM_BOT_TOKEN": TOKEN})
    found = rows(ready)
    assert found["gateway.daemon"].message == "the gateway has not run for this profile yet"
    on = found["gateway.telegram"]
    assert (on.severity, on.message) == ("ok", "Telegram: on; set: TELEGRAM_BOT_TOKEN")
    access = found["gateway.telegram.access"]
    assert access.severity == "info"
    assert access.message.startswith("Telegram: a stranger's direct message gets ")
    assert all(TOKEN not in line for line in texts(ready))


def test_switched_on_without_credentials_is_an_error(scope: Scope) -> None:
    ready = profile(scope, {}, "platforms:\n  discord:\n    enabled: true\n")
    found = rows(ready)
    row = found["gateway.discord"]
    assert row.severity == "error"
    assert row.message == (
        "Discord is switched on in config but missing credentials: DISCORD_BOT_TOKEN"
    )
    assert row.fix == "run `indus gateway setup discord`"


def test_partly_set_up_is_a_warning(scope: Scope) -> None:
    ready = profile(scope, {"TWILIO_ACCOUNT_SID": "AC-test", "TELEGRAM_BOT_TOKEN": TOKEN})
    row = rows(ready)["gateway.sms"]
    assert row.severity == "warn"
    assert row.message == (
        "SMS (Twilio) is partly set up: TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER not set"
    )


def test_a_missing_library_names_the_install_hint(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry_mod, "modules_installed", lambda *names: lambda: False)
    ready = profile(scope, {"DISCORD_BOT_TOKEN": "dc-test-" + "z" * 8})
    row = rows(ready)["gateway.discord.library"]
    assert (row.severity, row.message) == ("warn", "Discord: its library is not installed")
    assert row.fix == "pip install 'indus-agent[discord]'"


# -- the daemon ------------------------------------------------------------------------------------


def _status(scope: Scope, **data: object) -> None:
    status_path(scope.home).write_text(json.dumps(data))


def test_a_running_gateway_reports_each_platforms_live_state(scope: Scope) -> None:
    ready = profile(scope, {"TELEGRAM_BOT_TOKEN": TOKEN})
    pid = os.getpid()
    _status(
        ready,
        state="running",
        pid=pid,
        start_time=process_start_time(pid),
        updated_at=datetime.now(UTC).isoformat(),
        platforms={"telegram": {"state": "fatal", "error_code": "telegram_auth"}},
    )
    found = rows(ready)
    assert found["gateway.daemon"] == Finding(
        "gateway.daemon", "ok", f"gateway running (pid {pid})"
    )
    row = found["gateway.telegram"]
    assert row.severity == "warn"
    assert row.message == (
        "Telegram: on; set: TELEGRAM_BOT_TOKEN; fatal in the running gateway (telegram_auth)"
    )


def test_a_stopped_or_stale_gateway(scope: Scope) -> None:
    ready = profile(scope, {"TELEGRAM_BOT_TOKEN": TOKEN})
    _status(ready, state="stopped", exit_reason="planned_stop:operator")
    assert rows(ready)["gateway.daemon"].message == "gateway stopped: planned_stop:operator"
    _status(ready, state="running", pid=2**22 + 7, updated_at=datetime.now(UTC).isoformat())
    stale = rows(ready)["gateway.daemon"]
    assert stale.severity == "warn" and "stale (its process is gone)" in stale.message


# -- ports -----------------------------------------------------------------------------------------

LINE = {"LINE_CHANNEL_ACCESS_TOKEN": "line-test-token", "LINE_CHANNEL_SECRET": SECRET}
GRAPH = {"MSGRAPH_WEBHOOK_CLIENT_STATE": "client-state-" + "k" * 8}


def test_two_listeners_on_one_port_are_reported(scope: Scope) -> None:
    ready = profile(scope, {**LINE, **GRAPH})
    row = rows(ready)["gateway.port.8646"]
    assert row.severity == "error"
    assert (
        row.message == "port 8646 is wanted by line and msgraph: only the first to start can listen"
    )
    assert row.fix == "move one with platforms.msgraph.webhook_port"
    assert all(SECRET not in line for line in texts(ready))


def test_webhook_port_moves_a_listener(scope: Scope) -> None:
    ready = profile(scope, {**LINE, **GRAPH}, "platforms:\n  msgraph:\n    webhook_port: 8650\n")
    found = rows(ready)
    assert not any(key.startswith("gateway.port.") for key in found)
    assert found["gateway.ports"].message == "listener ports: line 8646, msgraph 8650"


def test_http_surfaces_join_the_port_check(scope: Scope) -> None:
    env = {
        "TWILIO_ACCOUNT_SID": "AC-test",
        "TWILIO_AUTH_TOKEN": "tw-test",
        "TWILIO_PHONE_NUMBER": "+15550100",
    }
    config = "gateway:\n  api_server:\n    enabled: true\n    port: 8080\n"
    row = rows(profile(scope, env, config))["gateway.port.8080"]
    assert row.message.startswith("port 8080 is wanted by sms and api_server")
    assert row.fix == "move one with gateway.api_server.port"


def test_mode_dependent_listeners(scope: Scope) -> None:
    feishu = {"FEISHU_APP_ID": "cli-test", "FEISHU_APP_SECRET": "fs-test"}
    ready = profile(scope, {**feishu, "TELEGRAM_BOT_TOKEN": TOKEN})
    assert "gateway.ports" not in rows(ready)  # long connection, long polling: no listener
    hooked = profile(
        scope,
        {**feishu, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_WEBHOOK_URL": "https://bot.example/t"},
        "platforms:\n  feishu:\n    connection_mode: webhook\n",
    )
    assert rows(hooked)["gateway.ports"].message == "listener ports: telegram 8443, feishu 8765"


def test_listener_defaults_follow_the_adapters() -> None:
    from indus.gateway.adapters.bluebubbles.adapter import WEBHOOK_PORT as bluebubbles
    from indus.gateway.adapters.feishu.adapter import WEBHOOK_PORT as feishu
    from indus.gateway.adapters.google_chat.adapter import DEFAULT_PORT as google_chat
    from indus.gateway.adapters.line.adapter import DEFAULT_PORT as line
    from indus.gateway.adapters.msgraph.adapter import DEFAULT_PORT as msgraph
    from indus.gateway.adapters.photon.sidecar import DEFAULT_PORT as photon
    from indus.gateway.adapters.sms.adapter import DEFAULT_PORT as sms
    from indus.gateway.adapters.teams.adapter import DEFAULT_PORT as teams
    from indus.gateway.adapters.telegram.webhook import DEFAULT_PORT as telegram
    from indus.gateway.adapters.wecom.callback import DEFAULT_PORT as wecom_callback
    from indus.gateway.adapters.whatsapp_cloud.adapter import DEFAULT_PORT as whatsapp_cloud

    assert dict(gc.LISTENER_DEFAULTS) == {
        "sms": sms,
        "whatsapp_cloud": whatsapp_cloud,
        "wecom_callback": wecom_callback,
        "bluebubbles": bluebubbles,
        "line": line,
        "msgraph": msgraph,
        "google_chat": google_chat,
        "feishu": feishu,
        "photon": photon,
        "teams": teams,
        "telegram": telegram,
    }


# -- sidecars and local daemons --------------------------------------------------------------------


def test_local_daemons_are_probed_without_credentials(
    scope: Scope, quiet_probes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    password = "bb-" + "p" * 10
    env = {
        "SIGNAL_ACCOUNT": "+15550101",
        "SIGNAL_HTTP_URL": "http://127.0.0.1:8080/",
        "SIMPLEX_WS_URL": "ws://127.0.0.1:5225",
        "BLUEBUBBLES_SERVER_URL": "http://mac.local:1234",
        "BLUEBUBBLES_PASSWORD": password,
    }
    ready = profile(scope, env)
    found = rows(ready)
    assert quiet_probes == [
        "http://127.0.0.1:8080/api/v1/check",
        "tcp://127.0.0.1:5225",
        "http://mac.local:1234/api/v1/ping",
    ]
    signal = found["gateway.signal.reachable"]
    assert signal.severity == "warn"
    assert (
        signal.message
        == "Signal: signal-cli at http://127.0.0.1:8080 does not answer (ConnectError)"
    )
    assert found["gateway.simplex.reachable"].fix == "start `simplex-chat -p 5225`"
    assert all(password not in line for line in texts(ready))
    monkeypatch.setattr(gc, "http_reachable", lambda url: None)
    monkeypatch.setattr(gc, "tcp_reachable", lambda host, port: None)
    answered = rows(ready)
    assert answered["gateway.bluebubbles.reachable"] == Finding(
        "gateway.bluebubbles.reachable",
        "ok",
        "BlueBubbles: the BlueBubbles server at http://mac.local:1234 answers",
    )


def test_whatsapp_bridge_needs_node_a_script_and_a_provider(scope: Scope, tmp_path: Path) -> None:
    ready = profile(scope, {"PATH": str(tmp_path)}, "platforms:\n  whatsapp:\n    enabled: true\n")
    found = rows(ready)
    assert found["gateway.whatsapp.node"].severity == "error"
    assert found["gateway.whatsapp.provider"].fix == (
        "set platforms.whatsapp.provider (the bridge refuses to start without one)"
    )
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\n")
    node.chmod(0o755)
    script = tmp_path / "main.ts"
    script.write_text("// bridge\n")
    config = (
        "platforms:\n  whatsapp:\n    enabled: true\n"
        f"    bridge_script: {script}\n    provider: ./provider.mjs\n"
    )
    good = rows(profile(scope, {"PATH": str(tmp_path)}, config))
    assert good["gateway.whatsapp.bridge"] == Finding(
        "gateway.whatsapp.bridge", "ok", "WhatsApp: the bridge can start"
    )


def test_photon_sidecar_needs_node_a_script_a_provider_and_its_credentials(
    scope: Scope, tmp_path: Path
) -> None:
    on = "platforms:\n  photon:\n    enabled: true\n"
    found = rows(profile(scope, {"PATH": str(tmp_path)}, on))
    assert found["gateway.photon.node"].severity == "error"
    assert found["gateway.photon.provider"].fix == (
        "set platforms.photon.provider (the sidecar refuses to start without one)"
    )
    assert found["gateway.photon.credentials"] == Finding(
        "gateway.photon.credentials",
        "error",
        "Photon: PHOTON_PROJECT_ID, PHOTON_PROJECT_SECRET not set",
        fix="run `indus gateway setup photon`",
    )
    assert found["gateway.ports"].message == "listener ports: photon 8789"
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\n")
    node.chmod(0o755)
    script = tmp_path / "main.ts"
    script.write_text("// sidecar\n")
    config = (
        on + f"    sidecar_script: {script}\n    provider: ./provider.mjs\n    sidecar_port: 8646\n"
    )
    project = {"PHOTON_PROJECT_ID": "proj-test", "PHOTON_PROJECT_SECRET": "ph-" + "s" * 10}
    good = rows(profile(scope, {"PATH": str(tmp_path), **project, **LINE}, config))
    assert good["gateway.photon.sidecar"] == Finding(
        "gateway.photon.sidecar", "ok", "Photon: the sidecar can start"
    )
    assert good["gateway.port.8646"].fix == "move one with platforms.photon.sidecar_port"


def test_tcp_probe_against_a_loopback_socket() -> None:
    real = REAL_TCP
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert real("127.0.0.1", port) is None
    finally:
        server.close()
    assert real("127.0.0.1", port) is not None


# -- Discord voice ---------------------------------------------------------------------------------


def test_discord_voice_readiness(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    ready = profile(scope, {"DISCORD_BOT_TOKEN": "dc-test-" + "z" * 8})
    monkeypatch.setattr(gc, "_installed", lambda module: module != "davey")
    monkeypatch.setattr(gc.ctypes.util, "find_library", lambda name: None)
    monkeypatch.setattr(gc, "_ffmpeg", lambda scope: None)
    found = rows(ready)
    assert found["gateway.discord.voice"].message == ("Discord voice channels: davey not installed")
    assert found["gateway.discord.voice"].fix == "pip install 'indus-agent[discord-voice]'"
    assert found["gateway.discord.opus"].severity == "info"
    assert found["gateway.discord.ffmpeg"].severity == "info"
    monkeypatch.setattr(gc, "_installed", lambda module: True)
    monkeypatch.setattr(gc.ctypes.util, "find_library", lambda name: "libopus.so.0")
    monkeypatch.setattr(gc, "_ffmpeg", lambda scope: "/usr/bin/ffmpeg")
    assert rows(ready)["gateway.discord.voice"] == Finding(
        "gateway.discord.voice", "ok", "Discord voice channels: ready"
    )


# -- the section -----------------------------------------------------------------------------------


def test_the_section_runs_last_and_never_fails_the_run(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = doctor.all_checks()
    assert specs[-1].id == "gateway" and specs[-1].title == "Messaging gateway"

    def broken(scope: Scope) -> object:
        raise ValueError("bad settings")

    monkeypatch.setattr(gc._View, "load", classmethod(lambda cls, scope: broken(scope)))
    (row,) = gc.gateway_findings(profile(scope, {}))
    assert row == Finding(
        "gateway", "warn", "the gateway settings could not be read: ValueError: bad settings"
    )
