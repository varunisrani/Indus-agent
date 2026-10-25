"""W61-M1: the dashboard's ChatGPT sign-in — start/poll/cancel through ``/api/providers/login``,
status and sign-out through ``/api/providers/login/accounts/{provider}`` (the "Contract for UI" in
docs/notes/P08/chatgpt-sign-in.md). A fake sign-in service stands in for OpenAI; no answer may carry
a token, the device id or the verifier."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest

from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope
from indus.providers.auth.device_code import TokenSet
from indus.providers.auth.pool_model import PoolEntry, edit_pool, secret_name
from indus.providers.auth.store import AuthStore
from indus.web.routes import provider_accounts
from tests.contract.web.rest_app import RestApp, build_rest_app, pause, route_table, unscoped_writes
from tests.fakes import chatgpt_auth as fake
from tests.fakes.clock import FakeClock

WRITE = {"profile": "work"}
ACCOUNTS = "/api/providers/login/accounts/openai-codex"
SPEC = replace(fake.DEVICE_SPEC, min_interval=1)


@pytest.fixture
def rest(scope: Scope) -> Iterator[RestApp]:
    config_loader.reset()
    built = build_rest_app(scope)
    yield built
    built.close()
    config_loader.reset()


def _code(answer: Any) -> str:
    return answer.json()["detail"]["code"]


def _poll(rest: RestApp, session_id: str, clock: FakeClock) -> dict[str, Any]:
    """Poll the session the way the page does, moving the scope's fake clock on so the sign-in
    thread's interval waits pass."""
    deadline = time.monotonic() + 20
    while True:
        got = rest.client.get(f"/api/providers/login/{session_id}", params=WRITE).json()
        if got["status"] != "pending" or time.monotonic() > deadline:
            return got
        clock.advance(0.5)
        pause(0.05)


def _secrets_in(text: str) -> list[str]:
    values = (
        fake.fake_jwt(1),
        fake.fake_jwt(101),
        fake.fake_refresh(1),
        "dev-auth-1",
        "verifier-1",
    )
    return [v for v in values if v in text]


def _usercode(interval: int = 1) -> httpx.Response:
    return httpx.Response(
        200, json={"device_auth_id": "dev-auth-1", "user_code": "ABCD-1234", "interval": interval}
    )


def test_routes_are_scoped_and_writes_need_a_profile(rest: RestApp) -> None:
    assert route_table(rest.app, ("/api/providers/login/accounts",)) == set(
        provider_accounts.ROUTE_LIST
    )
    assert unscoped_writes(rest.app, ("/api/providers/login/accounts",)) == []


def test_device_sign_in_status_and_sign_out(rest: RestApp, scope: Scope) -> None:
    server = fake.FakeSignIn(usercode=[_usercode()])
    rest.state.extras["providers.device_flows"] = {"openai-codex": SPEC}
    rest.state.extras["providers.http_client"] = server.factory()

    before = rest.client.get(ACCOUNTS, params=WRITE)
    assert before.status_code == 200
    assert before.json() == {
        "provider": "openai-codex",
        "label": "OpenAI (ChatGPT sign-in)",
        "signed_in": False,
        "signed_in_at": None,
        "flows": {"browser": True, "device": True},
        "accounts": [],
    }

    started = rest.client.post(
        "/api/providers/login/start", params=WRITE, json={"provider": "openai-codex"}
    )
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["status"] == "pending" and body["user_code"] == "ABCD-1234"
    assert body["verification_url"] == SPEC.verify_page
    assert body["interval"] == 1 and body["expires_in"] == SPEC.deadline_seconds
    assert _secrets_in(started.text) == []

    done = _poll(rest, body["session_id"], cast(FakeClock, scope.clock))
    assert done["status"] == "approved", done
    assert _secrets_in(json.dumps(done)) == []

    status = rest.client.get(ACCOUNTS, params=WRITE)
    got = status.json()
    assert got["signed_in"] and got["signed_in_at"]
    [account] = got["accounts"]
    assert account["source"] == "device_code" and account["plan"] == "plus"
    assert account["status"] == "ok" and not account["expired"] and account["expires_at"]
    assert _secrets_in(status.text) == []

    options = rest.client.get("/api/model/options", params=WRITE).json()
    row = next(p for p in options["providers"] if p["name"] == "openai-codex")
    assert row["configured"] and row["device_login"] and row["sign_in"]

    out = rest.client.delete(ACCOUNTS, params=WRITE)
    assert out.status_code == 200 and out.json() == {
        "provider": "openai-codex",
        "removed": 1,
        "signed_in": False,
    }
    assert rest.client.get(ACCOUNTS, params=WRITE).json()["accounts"] == []
    again = rest.client.delete(ACCOUNTS, params=WRITE).json()
    assert again["removed"] == 0


def test_sign_out_deletes_the_stored_tokens(rest: RestApp, scope: Scope) -> None:
    now = scope.clock.now()
    tokens = TokenSet(
        fake.fake_jwt(1), now, refresh_token=fake.fake_refresh(1), expires_at=now - timedelta(1)
    )
    entry = PoolEntry(id="c0de0004", label="old", auth_type="oauth", source="pkce")
    assert scope.secrets is not None
    scope.secrets.set(secret_name("openai-codex", entry.id), tokens.to_secret())  # pyright: ignore[reportAttributeAccessIssue]
    edit_pool(AuthStore(scope.home, env=scope.env), "openai-codex", lambda p, _d: p.add(entry))
    status = rest.client.get(ACCOUNTS, params=WRITE).json()
    assert status["signed_in"] and status["accounts"][0]["expired"] is True
    assert status["accounts"][0]["plan"] == "plus"
    assert _secrets_in(json.dumps(status)) == []
    assert rest.client.delete(ACCOUNTS, params=WRITE).json()["removed"] == 1
    assert scope.secrets.get(secret_name("openai-codex", entry.id)) is None


def test_poll_cancel_and_failures_say_nothing_secret(rest: RestApp) -> None:
    server = fake.FakeSignIn(
        usercode=[_usercode()], polls=[httpx.Response(403, json={"error": "pending"})]
    )
    rest.state.extras["providers.device_flows"] = {"openai-codex": SPEC}
    rest.state.extras["providers.http_client"] = server.factory()
    started = rest.client.post(
        "/api/providers/login/start", params=WRITE, json={"provider": "openai-codex"}
    ).json()
    cancelled = rest.client.delete(f"/api/providers/login/{started['session_id']}", params=WRITE)
    assert cancelled.json()["status"] == "cancelled"

    refused = fake.FakeSignIn(usercode=[httpx.Response(500, json={"device_auth_id": "dev-auth-1"})])
    rest.state.extras["providers.http_client"] = refused.factory()
    answer = rest.client.post(
        "/api/providers/login/start", params=WRITE, json={"provider": "openai-codex"}
    )
    assert answer.status_code == 502 and _code(answer) == "sign_in_unavailable"
    assert _secrets_in(answer.text) == []


def test_unknown_provider_and_api_keys_for_a_sign_in_provider(rest: RestApp) -> None:
    missing = rest.client.get("/api/providers/login/accounts/nope", params=WRITE)
    assert missing.status_code == 404 and _code(missing) == "unknown_provider"
    key = rest.client.post(
        "/api/credentials/pool",
        params=WRITE,
        json={"provider": "openai-codex", "api_key": "k" * 32},
    )
    assert key.status_code == 400 and _code(key) == "sign_in_only"


def test_routes_need_the_dashboard_session(rest: RestApp) -> None:
    anonymous = rest.anonymous()
    assert anonymous.get(ACCOUNTS, params=WRITE).status_code == 401
    assert anonymous.delete(ACCOUNTS, params=WRITE).status_code == 401
