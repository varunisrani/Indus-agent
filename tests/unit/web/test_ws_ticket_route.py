"""P18-06: ``POST /api/auth/ws-ticket`` (gated mode, cookie-authenticated, single use)."""

from __future__ import annotations

from indus.service.ws_auth import WEB_EVENTS, WEB_PTY, looks_like_ticket, stable_profile_id
from tests.unit.web.conftest import Builder, Built, basic_config


def _signed_in(build: Builder) -> Built:
    built = build(host="0.0.0.0", config=basic_config())
    response = built.client.post(
        "/auth/password-login", json={"username": "admin", "password": "correct horse"}
    )
    assert response.status_code == 200
    return built


def test_a_signed_in_browser_gets_a_ticket(build: Builder) -> None:
    built = _signed_in(build)
    response = built.client.post("/api/auth/ws-ticket", json={"purpose": WEB_PTY})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert looks_like_ticket(body["ticket"])
    assert body["purpose"] == WEB_PTY and body["expires_in"] == 30.0
    grant = built.state.tickets.redeem(
        body["ticket"], purpose=WEB_PTY, profile_id=stable_profile_id(built.state.home)
    )
    assert grant.purpose == WEB_PTY
    log = built.state.auth.audit.path.read_text(encoding="utf-8")
    assert "ws_ticket_minted" in log and body["ticket"] not in log


def test_tickets_need_a_session(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    response = built.client.post("/api/auth/ws-ticket", json={"purpose": WEB_PTY})
    assert response.status_code == 401
    assert "ticket" not in response.json()


def test_a_token_mode_client_gets_a_ticket(build: Builder) -> None:
    """D-16.691: the desktop's main process holds the token and mints one ticket per dial."""
    built = build()
    response = built.client.post(
        "/api/auth/ws-ticket", json={"purpose": WEB_PTY}, headers=built.auth()
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert looks_like_ticket(body["ticket"]) and body["purpose"] == WEB_PTY
    log = built.state.auth.audit.path.read_text(encoding="utf-8")
    assert "ws_ticket_minted" in log and body["ticket"] not in log


def test_token_mode_tickets_still_need_the_token(build: Builder) -> None:
    built = build()
    response = built.client.post("/api/auth/ws-ticket", json={"purpose": WEB_PTY})
    assert response.status_code == 401
    assert "ticket" not in response.json()


def test_bad_purposes_and_profiles_are_refused(build: Builder) -> None:
    built = _signed_in(build)
    assert (
        built.client.post("/api/auth/ws-ticket", json={"purpose": "ws-interactive"}).status_code
        == 400
    )
    assert built.client.post("/api/auth/ws-ticket", json={"purpose": "web-pub"}).status_code == 400
    ghost = built.client.post(
        "/api/auth/ws-ticket", json={"purpose": WEB_EVENTS, "profile": "ghost"}
    )
    assert ghost.status_code == 404
    bad = built.client.post("/api/auth/ws-ticket", json={"purpose": WEB_EVENTS, "profile": "../x"})
    assert bad.status_code == 404


def test_a_ticket_is_bound_to_the_named_profile(build: Builder) -> None:
    built = _signed_in(build)
    other = built.state.root / "profiles" / "other"
    other.mkdir(parents=True)
    (other / "profile.yaml").touch()
    body = built.client.post(
        "/api/auth/ws-ticket", json={"purpose": WEB_PTY, "profile": "other"}
    ).json()
    grant = built.state.tickets.redeem(body["ticket"], purpose=WEB_PTY)
    assert grant.profile_id == stable_profile_id(other)
    assert grant.profile_id != built.state.stable_id


def test_a_full_book_answers_429(build: Builder) -> None:
    built = _signed_in(build)
    built.state.tickets.limit = 1
    assert built.client.post("/api/auth/ws-ticket", json={"purpose": WEB_PTY}).status_code == 200
    assert built.client.post("/api/auth/ws-ticket", json={"purpose": WEB_PTY}).status_code == 429
