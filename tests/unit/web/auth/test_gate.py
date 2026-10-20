"""P18-02: gate engagement, loopback token, shared allowlist, middleware order (R09.3, R09.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from indus.web.auth import gate as gate_module
from indus.web.auth import public_paths
from indus.web.auth.gate import AUTH_BODY_LIMIT, gate_required, safe_next
from indus.web.auth.proxy import PublicOrigin
from indus.web.auth.public_paths import PUBLIC_API_PATHS, TICKET_PATHS, is_login_public
from indus.web.auth.token import SESSION_TOKEN_ENV, SessionToken, SessionTokenError, bearer_of
from tests.unit.web.conftest import Builder, basic_config


@pytest.mark.parametrize(
    ("bind", "public", "gated"),
    [
        ("127.0.0.1", None, False),
        ("localhost", None, False),
        ("::1", None, False),
        ("[::1]", None, False),
        ("0.0.0.0", None, True),
        ("::", None, True),
        ("192.168.1.20", None, True),
        ("10.0.0.5", None, True),
        ("100.64.0.7", None, True),
        ("169.254.10.10", None, True),
        ("fe80::1", None, True),
        ("dash.example.test", None, True),
        ("127.0.0.1", "http://127.0.0.1:9119", False),
        ("127.0.0.1", "http://localhost:9119/indus", False),
        ("127.0.0.1", "https://dash.example.test/indus", True),
        ("127.0.0.1", "https://192.168.1.20", True),
        ("127.0.0.1", "https://100.64.0.7", True),
    ],
)
def test_gate_table(bind: str, public: str | None, gated: bool) -> None:
    origin = PublicOrigin.parse(public) if public else None
    assert gate_required(bind, origin) is gated


def test_a_declared_public_url_engages_the_gate_on_a_loopback_bind(build: Builder) -> None:
    config = {
        **basic_config(),
        "dashboard": {**basic_config()["dashboard"], "public_url": "https://dash.example.test"},
    }
    built = build(config=config)
    assert built.state.gated
    with pytest.raises(Exception, match="login provider"):
        build(config={"dashboard": {"public_url": "https://dash.example.test"}})


def test_token_is_needed_on_api_paths_in_loopback_mode(build: Builder) -> None:
    built = build()
    assert built.client.get("/api/auth/me").status_code == 401
    assert built.client.get("/api/auth/me", headers=built.auth()).status_code == 200
    bearer = {"Authorization": f"Bearer {built.token}"}
    assert built.client.get("/api/auth/me", headers=bearer).status_code == 200
    wrong = {"X-Indus-Session-Token": built.token[:-1] + ("A" if built.token[-1] != "A" else "B")}
    assert built.client.get("/api/auth/me", headers=wrong).status_code == 401
    assert (
        built.client.get("/api/auth/me", headers={"X-Indus-Session-Token": "x"}).status_code == 401
    )
    # pages and assets never need it
    assert built.client.get("/sessions").status_code == 200


def test_public_paths_need_no_token(build: Builder) -> None:
    built = build()
    assert built.client.get("/api/health").status_code == 200
    # an exact list: a longer path is not public
    assert built.client.get("/api/health/extra").status_code == 401


def test_no_route_takes_the_token_in_the_query(build: Builder, tmp_path: Path) -> None:
    built = build()
    for path in ("/api/files/download", "/api/files", "/api/sessions", "/api/health"):
        refused = built.client.get(path, params={"token": built.token})
        assert refused.status_code == 401, path
        assert refused.json()["error"] == "credential_in_url"
        # a good header does not excuse a credential in the URL
        both = built.client.get(path, params={"token": built.token}, headers=built.auth())
        assert both.status_code == 401, path
    # the download route is reached without the token (a new tab cannot send it) but answers
    # nothing about the file system without its own ticket (P18-16)
    probe = built.client.get("/api/files/download", params={"path": str(tmp_path / "nothing")})
    assert probe.status_code == 401
    assert probe.json()["detail"]["code"] == "download_ticket_invalid"
    assert PUBLIC_API_PATHS.isdisjoint(TICKET_PATHS)


def test_session_token_is_rejected_in_gated_mode(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    assert built.client.get("/api/auth/me", headers=built.auth()).status_code == 401
    bearer = {"Authorization": f"Bearer {built.token}"}
    assert built.client.get("/api/auth/me", headers=bearer).status_code == 401
    login = built.client.post(
        "/auth/password-login", json={"username": "admin", "password": "correct horse"}
    )
    assert login.status_code == 200
    assert built.client.get("/api/auth/me").status_code == 200
    # a valid cookie does not make a presented token acceptable
    refused = built.client.get("/api/auth/me", headers=built.auth())
    assert refused.status_code == 401


def test_the_allowlist_is_one_object_in_both_gates(build: Builder) -> None:
    loopback = build()
    gated = build(host="0.0.0.0", config=basic_config())
    assert loopback.state.auth.public_paths is PUBLIC_API_PATHS
    assert gated.state.auth.public_paths is PUBLIC_API_PATHS
    assert gate_module.PUBLIC_API_PATHS is public_paths.PUBLIC_API_PATHS
    assert all(is_login_public(path) for path in PUBLIC_API_PATHS)
    assert {
        "/api/config/defaults",
        "/api/config/schema",
        "/api/cron/fire",
        "/api/dashboard/plugins",
        "/api/dashboard/themes",
        "/api/health",
        "/api/mcp/oauth/callback",
        "/api/model/info",
        "/api/status",
    } == PUBLIC_API_PATHS
    for built in (loopback, gated):
        assert built.client.get("/api/health").status_code == 200


def test_gated_html_redirects_and_api_gets_the_envelope(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    page = built.client.get("/sessions?tab=all")
    assert page.status_code == 302
    assert page.headers["location"] == "/login?next=%2Fsessions%3Ftab%3Dall"
    api = built.client.get("/api/sessions")
    assert api.status_code == 401
    body = api.json()
    assert body["error"] == "unauthenticated"
    assert body["login_url"].startswith("/login")
    assert built.client.get("/login").status_code == 200
    assert built.client.get("/assets/index-abc123.js").status_code == 200


def test_unknown_plugin_route_is_401_before_404(build: Builder) -> None:
    loopback = build()
    assert loopback.client.get("/api/ext/secret-plugin/data").status_code == 401
    found = loopback.client.get("/api/ext/secret-plugin/data", headers=loopback.auth())
    assert found.status_code == 404 and found.json()["detail"] == "No such plugin route."
    gated = build(host="0.0.0.0", config=basic_config())
    assert gated.client.get("/api/ext/secret-plugin/data").status_code == 401
    enabled = build(plugin_allowed=lambda name: name == "known")
    passed = enabled.client.get("/api/ext/known/data", headers=enabled.auth())
    assert passed.status_code == 404 and passed.json()["detail"] != "No such plugin route."


def test_bad_host_is_400_after_auth(build: Builder) -> None:
    built = build()
    response = built.client.get("/api/health", headers={"host": "evil.com"})
    assert response.status_code == 400
    assert response.json()["error"] == "bad_host"
    authed = built.client.get("/api/auth/me", headers={**built.auth(), "host": "evil.com:9119"})
    assert authed.status_code == 400
    # order: an unauthenticated request with a bad Host is answered by the login gate first
    assert built.client.get("/api/auth/me", headers={"host": "evil.com"}).status_code == 401


def test_cross_site_unsafe_requests_are_refused_in_gated_mode(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    response = built.client.post(
        "/auth/password-login",
        json={"username": "admin", "password": "correct horse"},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403
    ok = built.client.post(
        "/auth/password-login",
        json={"username": "admin", "password": "correct horse"},
        headers={"origin": "http://127.0.0.1:9119"},
    )
    assert ok.status_code == 200


def test_auth_bodies_are_capped(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    big = "x" * (AUTH_BODY_LIMIT + 1)
    response = built.client.post(
        "/auth/password-login", json={"username": "admin", "password": big}
    )
    assert response.status_code == 413

    def chunks() -> object:
        yield b'{"username": "admin", "password": "'
        for _ in range(80):
            yield b"y" * 1024
        yield b'"}'

    streamed = built.client.post(
        "/auth/password-login",
        content=chunks(),  # pyright: ignore[reportArgumentType]
        headers={"content-type": "application/json"},
    )
    assert streamed.status_code == 413


def test_handed_over_token(build: Builder) -> None:
    handed = "A" * 20 + "b" * 20
    built = build(env={SESSION_TOKEN_ENV: handed})
    assert built.token == handed and built.state.auth.token.handed_over
    assert (
        built.client.get("/api/auth/me", headers={"X-Indus-Session-Token": handed}).status_code
        == 200
    )
    with pytest.raises(SessionTokenError):
        SessionToken.from_env({SESSION_TOKEN_ENV: "too-short"})
    with pytest.raises(SessionTokenError):
        SessionToken.from_env({SESSION_TOKEN_ENV: "x" * 40 + "!"})
    minted = SessionToken.mint()
    assert len(minted.value) >= 43 and minted.value not in repr(minted)


def test_bearer_parsing() -> None:
    assert bearer_of("Bearer abc") == "abc"
    assert bearer_of("bearer  abc ") == "abc"
    assert bearer_of("Basic abc") == ""
    assert bearer_of(None) == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/sessions", "/sessions"),
        ("/sessions?x=1", "/sessions?x=1"),
        ("//evil.example", ""),
        ("https://evil.example", ""),
        ("/login", ""),
        ("/auth/logout", ""),
        ("/api/sessions", ""),
        ("/api", ""),
        ("/\\evil", ""),
        ("", ""),
    ],
)
def test_safe_next(raw: str, expected: str) -> None:
    assert safe_next(raw) == expected
