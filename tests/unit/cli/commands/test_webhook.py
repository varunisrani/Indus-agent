"""P15-32: ``indus webhook`` subscribe / list / remove / test (non-TTY)."""

from __future__ import annotations

import json
import stat

import httpx
import pytest
import respx

from indus.cli.commands._support import secret_store
from indus.foundation.scope import Scope
from indus.gateway.http.webhook import verify_signature
from indus.state.webhook_subscriptions import SubscriptionStore, secret_name_for
from tests.unit.cli.commands.cmdrun import Ran, run_cli, write_settings


def stored(scope: Scope, name: str) -> str | None:
    return secret_store(scope).get(name)


def _run(capsys: pytest.CaptureFixture[str], scope: Scope, *args: str) -> Ran:
    return run_cli(capsys, scope, "webhook", *args)


def test_subscribe_stores_the_secret_once_and_never_in_the_file(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    ran = _run(
        capsys,
        scope,
        "subscribe",
        "GitHub PRs",
        "--events",
        "pull_request, push",
        "--prompt",
        "PR {pull_request.title}",
        "--deliver",
        "telegram:42",
        "--auth",
        "github",
    )
    assert ran.code == 0, ran.err
    assert "Created webhook route github-prs" in ran.out
    assert "http://127.0.0.1:8644/webhooks/github-prs" in ran.out
    assert "receiver is off" in ran.err
    secret = stored(scope, secret_name_for("github-prs"))
    assert secret and secret in ran.out
    store = SubscriptionStore(scope)
    route = store.load()["github-prs"]
    assert route["secret"] == "INDUS_WEBHOOK_GITHUB_PRS" and route["events"] == [
        "pull_request",
        "push",
    ]
    assert secret not in store.path.read_text()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    again = _run(capsys, scope, "subscribe", "github-prs", "--prompt", "new")
    assert again.code == 0 and "Updated" in again.out and secret not in again.out
    assert stored(scope, secret_name_for("github-prs")) == secret


def test_subscribe_refusals(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    assert _run(capsys, scope, "subscribe", "bad!name").code == 1
    assert _run(capsys, scope, "subscribe", "x", "--deliver-only").code == 1
    assert _run(capsys, scope, "subscribe", "x", "--secret-name", "INDUS_NOT_STORED").code == 1
    assert _run(capsys, scope, "subscribe").code == 2


def test_list_shows_subscribed_and_config_routes_without_values(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    assert "No webhook routes" in _run(capsys, scope).out
    write_settings(
        scope,
        "gateway:\n  webhook:\n    enabled: true\n    port: 9000\n"
        "    routes: {alerts: {secret: INDUS_ALERTS, auth: hmac_ts, deliver: slack:C1}}\n",
    )
    _run(capsys, scope, "subscribe", "gl", "--auth", "gitlab")
    listed = _run(capsys, scope, "list")
    assert (
        listed.code == 0 and "alerts  [config]" in listed.out and "gl  [subscribed]" in listed.out
    )
    assert "http://127.0.0.1:9000/webhooks/gl" in listed.out and "receiver is off" not in listed.err
    data = json.loads(_run(capsys, scope, "ls", "--json").out)
    assert {r["name"] for r in data["routes"]} == {"alerts", "gl"}
    secret = stored(scope, secret_name_for("gl"))
    assert secret and secret not in listed.out


def test_remove_drops_the_route_and_its_generated_secret(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    _run(capsys, scope, "subscribe", "tmp")
    assert _run(capsys, scope, "rm", "tmp").code == 0
    assert SubscriptionStore(scope).load() == {}
    assert stored(scope, secret_name_for("tmp")) is None
    assert _run(capsys, scope, "remove", "tmp").code == 1


@pytest.mark.parametrize("auth", ["github", "gitlab", "hmac", "hmac_ts", "auto"])
def test_test_signs_and_posts_to_the_local_receiver(
    capsys: pytest.CaptureFixture[str], scope: Scope, auth: str
) -> None:
    _run(capsys, scope, "subscribe", "hook", "--auth", auth)
    secret = stored(scope, secret_name_for("hook"))
    assert secret
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        headers = {k.lower(): v for k, v in request.headers.items()}
        ok = verify_signature(auth, secret, headers, request.content, scope.clock.now().timestamp())
        return httpx.Response(202 if ok else 401, json={"status": "accepted" if ok else "bad"})

    with respx.mock(assert_all_called=True) as mock:
        mock.post("http://127.0.0.1:8644/webhooks/hook").mock(side_effect=answer)
        ran = _run(capsys, scope, "test", "hook", "--payload", '{"x": 1}')
    assert ran.code == 0, ran.out + ran.err
    assert "Answer 202" in ran.out and json.loads(seen[0].content) == {"x": 1}
    assert secret not in ran.out


def test_test_reports_refusals_and_an_unreachable_receiver(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    assert _run(capsys, scope, "test", "ghost").code == 1
    _run(capsys, scope, "subscribe", "hook")
    assert _run(capsys, scope, "test", "hook", "--payload", "{nope").code == 1
    with respx.mock() as mock:
        mock.post("http://127.0.0.1:8644/webhooks/hook").mock(
            side_effect=httpx.ConnectError("refused")
        )
        ran = _run(capsys, scope, "test", "hook")
    assert ran.code == 1 and "could not reach" in ran.err
    with respx.mock() as mock:
        mock.post("http://127.0.0.1:8644/webhooks/hook").mock(
            return_value=httpx.Response(401, json={"error": "invalid signature"})
        )
        assert _run(capsys, scope, "test", "hook").code == 1
