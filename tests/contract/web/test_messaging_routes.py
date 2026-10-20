"""P18-15: the Channels, Pairing, Webhooks and daemon REST routes (R06.15)."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config import loader as config_loader
from indus.foundation.envfile import read_env_file
from indus.foundation.envload import env_file_path
from indus.foundation.scope import EnvView, Scope
from indus.state.pairing import PairingStore
from indus.state.webhook_subscriptions import SubscriptionStore, secret_name_for
from indus.web.routes import daemon, messaging, webhooks
from tests.contract.web.fake_control import FakeControl, Refuse
from tests.contract.web.rest_app import (
    RestApp,
    build_rest_app,
    pause,
    route_table,
    unscoped_writes,
)

WRITE = {"profile": "work"}
TOKEN_NAME = "TELEGRAM_BOT_TOKEN"


def _token() -> str:
    """A fake bot credential, built at runtime."""
    return "77" + "0" * 7 + ":" + secrets.token_urlsafe(24)


class Ensure:
    """The ``daemon.ensure`` seam: records calls, answers ``ready``."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, home: Path, env: Any, *, mode: str) -> Any:
        self.calls.append((home, mode))

        class Verdict:
            state = "ready"
            detail = ""
            reason = ""

        return Verdict()


@pytest.fixture
def rest(scope: Scope) -> Iterator[RestApp]:
    config_loader.reset()
    built = build_rest_app(scope, extras={"daemon.ensure": Ensure()})
    yield built
    built.close()
    config_loader.reset()


def _code(answer: Any) -> str:
    return answer.json()["detail"]["code"]


def _platform(rest: RestApp, name: str = "telegram") -> dict[str, Any]:
    got = rest.client.get("/api/messaging/platforms", params=WRITE)
    assert got.status_code == 200
    return next(p for p in got.json()["platforms"] if p["id"] == name)


def _field(row: dict[str, Any], key: str) -> dict[str, Any]:
    return next(f for f in row["fields"] if f["key"] == key)


def _wait_action(rest: RestApp, name: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while True:
        got = rest.client.get(f"/api/actions/{name}/status", params=WRITE).json()
        if got["state"] != "running" or time.monotonic() > deadline:
            return got
        pause(0.05)


# -- route lists ----------------------------------------------------------------------------------


def test_route_lists_are_pinned_and_writes_are_scoped(rest: RestApp) -> None:
    assert route_table(rest.app, ("/api/messaging", "/api/pairing")) == set(messaging.ROUTE_LIST)
    assert route_table(rest.app, ("/api/webhooks",)) == set(webhooks.ROUTE_LIST)
    assert route_table(rest.app, ("/api/daemon",)) == set(daemon.ROUTE_LIST)
    prefixes = ("/api/messaging", "/api/pairing", "/api/webhooks", "/api/daemon")
    assert unscoped_writes(rest.app, prefixes) == ["POST /api/daemon/drain"]


def test_a_write_without_a_profile_is_refused_on_a_multi_profile_host(rest: RestApp) -> None:
    answer = rest.client.put("/api/messaging/platforms/telegram", json={"enabled": True})
    assert answer.status_code == 400


# -- channels -------------------------------------------------------------------------------------


def test_platforms_list_fields_and_never_a_credential(rest: RestApp, scope: Scope) -> None:
    token = _token()
    answer = rest.client.put(
        "/api/messaging/platforms/telegram", params=WRITE, json={"env": {TOKEN_NAME: token}}
    )
    assert answer.status_code == 200 and token not in answer.text
    assert scope.secrets is not None and scope.secrets.get(TOKEN_NAME) == token
    assert TOKEN_NAME not in read_env_file(env_file_path(scope.home))
    listing = rest.client.get("/api/messaging/platforms", params=WRITE)
    assert token not in listing.text
    row = _platform(rest)
    field = _field(row, TOKEN_NAME)
    assert field["kind"] == "credential" and field["is_set"] and field["value"] is None
    assert field["preview"] and field["preview"] != token
    assert row["enabled"] is True and row["state"] == "daemon_stopped"
    policy = _field(row, "INDUS_TELEGRAM_ALLOWED_USERS")
    assert policy["kind"] == "policy" and not policy["is_set"]


def test_blank_keeps_the_stored_value_and_settings_show_their_value(
    rest: RestApp, scope: Scope
) -> None:
    token = _token()
    rest.client.put(
        "/api/messaging/platforms/telegram", params=WRITE, json={"env": {TOKEN_NAME: token}}
    )
    answer = rest.client.put(
        "/api/messaging/platforms/telegram",
        params=WRITE,
        json={"env": {TOKEN_NAME: "  ", "INDUS_TELEGRAM_ALLOWED_USERS": "111,222"}},
    )
    assert answer.status_code == 200
    assert scope.secrets is not None and scope.secrets.get(TOKEN_NAME) == token
    assert read_env_file(env_file_path(scope.home))["INDUS_TELEGRAM_ALLOWED_USERS"] == "111,222"
    assert _field(_platform(rest), "INDUS_TELEGRAM_ALLOWED_USERS")["value"] == "111,222"


def test_a_masked_preview_sent_back_is_refused(rest: RestApp, scope: Scope) -> None:
    token = _token()
    rest.client.put(
        "/api/messaging/platforms/telegram", params=WRITE, json={"env": {TOKEN_NAME: token}}
    )
    preview = _field(_platform(rest), TOKEN_NAME)["preview"]
    answer = rest.client.put(
        "/api/messaging/platforms/telegram", params=WRITE, json={"env": {TOKEN_NAME: preview}}
    )
    assert answer.status_code == 400 and _code(answer) == "redacted_value"
    assert scope.secrets is not None and scope.secrets.get(TOKEN_NAME) == token


def test_one_bad_name_writes_nothing(rest: RestApp, scope: Scope) -> None:
    answer = rest.client.put(
        "/api/messaging/platforms/telegram",
        params=WRITE,
        json={"env": {TOKEN_NAME: _token(), "DISCORD_BOT_TOKEN": _token()}, "enabled": True},
    )
    assert answer.status_code == 400 and _code(answer) == "unknown_field"
    assert scope.secrets is not None and scope.secrets.get(TOKEN_NAME) is None
    assert not (scope.home / "config.yaml").exists() or "platforms" not in (
        scope.home / "config.yaml"
    ).read_text(encoding="utf-8")


def test_explicit_disable_beats_an_env_credential(scope: Scope) -> None:
    """R06.15: a token in the environment, ``enabled: false`` in config → disabled."""
    inherited = {**scope.env.to_dict(), TOKEN_NAME: _token()}
    env_scope = scope.with_env(EnvView(inherited))
    config_loader.reset()
    rest = build_rest_app(env_scope)
    try:
        assert _platform(rest)["enabled"] is True
        answer = rest.client.put(
            "/api/messaging/platforms/telegram", params=WRITE, json={"enabled": False}
        )
        assert answer.status_code == 200
        row = _platform(rest)
        assert row["enabled"] is False and row["state"] == "disabled"
        assert row["explicitly_disabled"] is True
        assert _field(row, TOKEN_NAME)["is_set"] is True
        tested = rest.client.post("/api/messaging/platforms/telegram/test", params=WRITE).json()
        assert tested["ok"] is False and tested["state"] == "disabled"
    finally:
        rest.close()
        config_loader.reset()


def test_clear_env_removes_a_credential_and_a_setting(rest: RestApp, scope: Scope) -> None:
    rest.client.put(
        "/api/messaging/platforms/telegram",
        params=WRITE,
        json={"env": {TOKEN_NAME: _token(), "INDUS_TELEGRAM_HOME_CHANNEL": "42"}},
    )
    answer = rest.client.put(
        "/api/messaging/platforms/telegram",
        params=WRITE,
        json={"clear_env": [TOKEN_NAME, "INDUS_TELEGRAM_HOME_CHANNEL"]},
    )
    assert answer.status_code == 200
    row = _platform(rest)
    assert not _field(row, TOKEN_NAME)["is_set"]
    assert not _field(row, "INDUS_TELEGRAM_HOME_CHANNEL")["is_set"]
    assert row["state"] == "not_configured"


def test_test_ladder_names_what_is_missing_and_unknown_platform_is_404(rest: RestApp) -> None:
    got = rest.client.post("/api/messaging/platforms/telegram/test", params=WRITE).json()
    assert got["ok"] is False and got["state"] == "not_configured"
    assert TOKEN_NAME in got["message"]
    missing = rest.client.post("/api/messaging/platforms/nope/test", params=WRITE)
    assert missing.status_code == 404


def test_live_state_comes_from_the_daemon(rest: RestApp, scope: Scope) -> None:
    rest.client.put(
        "/api/messaging/platforms/telegram", params=WRITE, json={"env": {TOKEN_NAME: _token()}}
    )
    fake = FakeControl(scope.home)
    try:
        assert _platform(rest)["state"] == "pending_restart"
        fake.verbs["status"] = lambda _p: {
            "state": "running",
            "platforms": {"telegram": {"state": "connected"}},
        }
        assert _platform(rest)["state"] == "connected"
        tested = rest.client.post("/api/messaging/platforms/telegram/test", params=WRITE).json()
        assert tested == {"ok": True, "state": "connected", "message": "Telegram is connected."}
    finally:
        fake.remove()


# -- pairing --------------------------------------------------------------------------------------


def test_pairing_list_approve_revoke_and_clear(rest: RestApp, scope: Scope) -> None:
    store = PairingStore(scope)
    first = store.request_code("telegram", "1001", "Ann")
    store.request_code("telegram", "1002", "Bob")
    assert first.code is not None
    listing = rest.client.get("/api/pairing", params=WRITE)
    assert listing.status_code == 200 and first.code not in listing.text
    pending = listing.json()["pending"]
    assert {p["user_id"] for p in pending} == {"1001", "1002"}

    wrong = rest.client.post(
        "/api/pairing/approve", params=WRITE, json={"platform": "telegram", "code": "ZZZZZZZZ"}
    )
    assert wrong.status_code == 404 and _code(wrong) == "no_such_code"
    good = rest.client.post(
        "/api/pairing/approve", params=WRITE, json={"platform": "telegram", "code": first.code}
    )
    assert good.status_code == 200 and good.json()["user"]["user_id"] == "1001"
    approved = rest.client.get("/api/pairing", params=WRITE).json()["approved"]
    assert [a["user_id"] for a in approved] == ["1001"]

    revoked = rest.client.post(
        "/api/pairing/revoke", params=WRITE, json={"platform": "telegram", "user_id": "1001"}
    )
    assert revoked.status_code == 200
    again = rest.client.post(
        "/api/pairing/revoke", params=WRITE, json={"platform": "telegram", "user_id": "1001"}
    )
    assert again.status_code == 404

    cleared = rest.client.post("/api/pairing/clear-pending", params=WRITE, json={})
    assert cleared.status_code == 200 and cleared.json()["cleared"] == 1
    assert rest.client.get("/api/pairing", params=WRITE).json()["pending"] == []


def test_approve_by_request_id_and_bad_platform(rest: RestApp, scope: Scope) -> None:
    request = PairingStore(scope).request_code("discord", "u-7", "Cy")
    assert request.request_id is not None
    good = rest.client.post(
        "/api/pairing/approve",
        params=WRITE,
        json={"platform": "discord", "request_id": request.request_id},
    )
    assert good.status_code == 200
    bad = rest.client.post(
        "/api/pairing/approve", params=WRITE, json={"platform": "no such!", "code": "X"}
    )
    assert bad.status_code == 400


# -- webhooks -------------------------------------------------------------------------------------


def test_webhook_secret_is_shown_once_then_never(rest: RestApp, scope: Scope) -> None:
    created = rest.client.post(
        "/api/webhooks",
        params=WRITE,
        json={"name": "Build Done", "events": ["push"], "deliver": "telegram:42"},
    )
    assert created.status_code == 201
    body = created.json()
    secret = body["secret"]
    assert body["name"] == "build-done" and len(secret) >= 32 and body["secret_set"] is True
    assert body["url"].endswith("/webhooks/build-done")
    assert scope.secrets is not None and scope.secrets.get(secret_name_for("build-done")) == secret
    stored = (scope.home / "gateway" / "webhook_subscriptions.json").read_text(encoding="utf-8")
    assert secret not in stored
    listing = rest.client.get("/api/webhooks", params=WRITE)
    assert listing.status_code == 200 and secret not in listing.text
    route = listing.json()["routes"][0]
    assert route["name"] == "build-done" and route["enabled"] and "secret" not in route
    duplicate = rest.client.post("/api/webhooks", params=WRITE, json={"name": "build-done"})
    assert duplicate.status_code == 409


def test_webhook_disable_reaches_the_route_file_and_delete_drops_the_secret(
    rest: RestApp, scope: Scope
) -> None:
    rest.client.post("/api/webhooks", params=WRITE, json={"name": "hook"})
    off = rest.client.put("/api/webhooks/hook/enabled", params=WRITE, json={"enabled": False})
    assert off.status_code == 200
    assert SubscriptionStore(scope).load()["hook"]["enabled"] is False
    assert rest.client.get("/api/webhooks", params=WRITE).json()["routes"][0]["enabled"] is False
    gone = rest.client.delete("/api/webhooks/hook", params=WRITE)
    assert gone.status_code == 200
    assert SubscriptionStore(scope).load() == {}
    assert scope.secrets is not None and scope.secrets.get(secret_name_for("hook")) is None
    assert rest.client.delete("/api/webhooks/hook", params=WRITE).status_code == 404


def test_disabled_route_is_refused_by_the_receiver_rules(rest: RestApp, scope: Scope) -> None:
    """The receiver validates each route with its own model; a disabled flag must survive it."""
    from indus.gateway.http.webhook_rules import RouteSpec

    rest.client.post("/api/webhooks", params=WRITE, json={"name": "ci", "auth": "github"})
    rest.client.put("/api/webhooks/ci/enabled", params=WRITE, json={"enabled": False})
    spec = RouteSpec.model_validate(SubscriptionStore(scope).load()["ci"])
    assert spec.enabled is False and spec.auth == "github"


def test_webhook_bad_name_config_route_and_receiver_toggle(rest: RestApp, scope: Scope) -> None:
    bad = rest.client.post("/api/webhooks", params=WRITE, json={"name": "-bad"})
    assert bad.status_code == 400 and _code(bad) == "bad_name"
    (scope.home / "config.yaml").write_text(
        "gateway:\n  webhook:\n    routes:\n      fixed:\n        secret: FIXED_SECRET\n",
        encoding="utf-8",
    )
    config_loader.reset()
    listing = rest.client.get("/api/webhooks", params=WRITE).json()
    assert [r["source"] for r in listing["routes"]] == ["config"]
    refused = rest.client.delete("/api/webhooks/fixed", params=WRITE)
    assert refused.status_code == 409 and _code(refused) == "config_route"
    toggled = rest.client.put("/api/webhooks/receiver", params=WRITE, json={"enabled": True})
    assert toggled.status_code == 200
    assert rest.client.get("/api/webhooks", params=WRITE).json()["receiver_enabled"] is True


# -- daemon ---------------------------------------------------------------------------------------


def test_daemon_state_from_the_control_socket(rest: RestApp, scope: Scope) -> None:
    assert rest.client.get("/api/daemon", params=WRITE).json()["running"] is False
    fake = FakeControl(scope.home)
    try:
        got = rest.client.get("/api/daemon", params=WRITE).json()
        assert got["running"] is True and got["state"] == "running" and got["profile"] == "work"
    finally:
        fake.remove()


def test_restart_goes_through_the_control_socket_then_ensure(rest: RestApp, scope: Scope) -> None:
    fake = FakeControl(scope.home)
    ensure: Ensure = rest.state.extras["daemon.ensure"]
    try:
        started = rest.client.post("/api/daemon/restart", params=WRITE)
        assert started.status_code == 202 and started.json()["action"] == "daemon-restart"
        status = _wait_action(rest, "daemon-restart")
        assert status["state"] == "done", status
        assert ("stop", {"reason": "dashboard", "restart": True}) in fake.calls
        assert ensure.calls == [(scope.home, "start")]
        text = "\n".join(status["lines"])
        assert "stop requested" in text and "the daemon stopped" in text and "ready" in text
    finally:
        fake.remove()


def test_stop_without_a_stop_verb_fails_with_the_cli_hint(rest: RestApp, scope: Scope) -> None:
    fake = FakeControl(scope.home, supports_stop=False)
    try:
        assert rest.client.post("/api/daemon/stop", params=WRITE).status_code == 202
        status = _wait_action(rest, "daemon-stop")
        assert status["state"] == "failed"
        assert any("indus gateway stop" in line for line in status["lines"])
        assert fake.alive
    finally:
        fake.remove()


def test_start_runs_ensure_and_a_second_start_while_running_is_409(
    rest: RestApp, scope: Scope
) -> None:
    gate = __import__("threading").Event()

    def slow(home: Path, env: Any, *, mode: str) -> Any:
        gate.wait(10)
        return Ensure()(home, env, mode=mode)

    rest.state.extras["daemon.ensure"] = slow
    first = rest.client.post("/api/daemon/start", params=WRITE)
    assert first.status_code == 202
    second = rest.client.post("/api/daemon/start", params=WRITE)
    assert second.status_code == 409 and _code(second) == "action_running"
    gate.set()
    assert _wait_action(rest, "daemon-start")["state"] == "done"


def test_drain_and_cancel_use_the_drain_verb(rest: RestApp, scope: Scope) -> None:
    stopped = rest.client.post("/api/daemon/drain", json={"action": "drain"})
    assert stopped.status_code == 409 and _code(stopped) == "daemon_not_running"
    fake = FakeControl(scope.home)
    try:
        on = rest.client.post("/api/daemon/drain", json={"action": "drain"})
        assert on.status_code == 200 and on.json()["draining"] is True
        off = rest.client.post("/api/daemon/drain", json={"action": "cancel"})
        assert off.status_code == 200 and off.json()["draining"] is False
        assert [p for v, p in fake.calls if v == "drain"] == [{"on": True}, {"on": False}]
        bad = rest.client.post("/api/daemon/drain", json={"action": "nap"})
        assert bad.status_code == 400
        fake.verbs["drain"] = lambda _p: (_ for _ in ()).throw(Refuse("failed"))
        refused = rest.client.post("/api/daemon/drain", json={"action": "drain"})
        assert refused.status_code == 502
    finally:
        fake.remove()


def test_drain_accepts_the_service_bearer(scope: Scope) -> None:
    secret = secrets.token_hex(32)
    env_scope = scope.with_env(scope.env.with_overlay({"INDUS_DASHBOARD_DRAIN_SECRET": secret}))
    rest = build_rest_app(env_scope)
    fake = FakeControl(scope.home)
    try:
        caller = rest.anonymous()
        refused = caller.post("/api/daemon/drain", json={"action": "drain"})
        assert refused.status_code == 401
        allowed = caller.post(
            "/api/daemon/drain",
            json={"action": "drain"},
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert allowed.status_code == 200 and allowed.json()["draining"] is True
        assert secret not in json.dumps(fake.calls)
    finally:
        fake.remove()
        rest.close()
