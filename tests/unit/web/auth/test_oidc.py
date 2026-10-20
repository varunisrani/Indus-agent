"""P18-04: the OpenID Connect provider against an in-process IdP (tests/fakes/idp)."""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from indus.web.auth.oidc import (
    OidcConfigError,
    OidcProvider,
    OidcSettings,
    parse_leeway,
    register_oidc,
    url_allowed,
)
from indus.web.auth.providers import (
    LoginRejected,
    ProviderRegistry,
    RefreshRejected,
    Session,
    SessionSigner,
)
from tests.fakes.idp.fake_oidc import CLIENT_ID, ISSUER, FakeIdp
from tests.unit.web.conftest import Builder


def _provider(idp: FakeIdp, *, leeway: float = 60.0, issuer: str = ISSUER) -> OidcProvider:
    return OidcProvider(
        OidcSettings(issuer, CLIENT_ID, leeway=leeway), signer=SessionSigner(), http=idp.client()
    )


def test_issuer_mismatch_is_refused() -> None:
    idp = FakeIdp(advertised_issuer="https://other.example.test/realms/indus")
    provider = _provider(idp)
    with pytest.raises(OidcConfigError, match="does not match"):
        provider.start_login("http://127.0.0.1:9119/auth/callback")


def test_a_trailing_slash_is_tolerated() -> None:
    idp = FakeIdp(advertised_issuer=ISSUER + "/")
    assert _provider(idp).start_login("http://127.0.0.1:9119/auth/callback").url


@pytest.mark.parametrize(
    ("issuer", "allowed"),
    [
        ("https://idp.example.test", True),
        ("http://idp.example.test", False),
        ("http://127.0.0.1:8080/realms/x", True),
        ("http://localhost:8080/realms/x", True),
        ("http://[::1]:8080", True),
        ("http://192.168.1.4:8080", False),
        ("ftp://idp.example.test", False),
        ("https://user@idp.example.test", False),
        ("", False),
    ],
)
def test_http_issuer_only_on_loopback(issuer: str, allowed: bool) -> None:
    assert url_allowed(issuer) is allowed
    registry = ProviderRegistry()
    found = register_oidc(registry, {"issuer": issuer, "client_id": CLIENT_ID}, {}, SessionSigner())
    assert (found is not None) is allowed


def test_plain_http_endpoints_are_refused() -> None:
    idp = FakeIdp()
    provider = _provider(idp)
    idp.issuer = "http://idp.example.test/realms/indus"  # endpoints now advertised over http
    idp.advertised_issuer = ISSUER
    with pytest.raises(OidcConfigError, match="not https"):
        provider.discovery()


@pytest.mark.parametrize(
    ("raw", "value"),
    [(None, 60.0), ("", 60.0), (30, 30.0), ("15", 15.0), (0, 0.0), (-5, 0.0), ("soon", 0.0)],
)
def test_leeway_parsing_fails_closed(raw: object, value: float) -> None:
    assert parse_leeway(raw) == value
    assert parse_leeway(float("inf")) == 0.0 and parse_leeway(True) == 0.0


def test_expiry_inside_the_leeway_is_accepted_outside_refused() -> None:
    idp = FakeIdp()
    provider = _provider(idp, leeway=60)
    assert provider.verify_id_token(idp.id_token(exp_in=-30), nonce=None)["sub"] == "user-1"
    with pytest.raises(LoginRejected):
        provider.verify_id_token(idp.id_token(exp_in=-120), nonce=None)
    strict = _provider(idp, leeway=0)
    with pytest.raises(LoginRejected):
        strict.verify_id_token(idp.id_token(exp_in=-30), nonce=None)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"aud": "someone-else"},
        {"iss": "https://other.example.test"},
        {"alg": "HS256"},
        {"kid": "unknown-kid"},
    ],
)
def test_bad_id_tokens_are_refused(kwargs: dict[str, Any]) -> None:
    idp = FakeIdp()
    with pytest.raises(LoginRejected):
        _provider(idp).verify_id_token(idp.id_token(**kwargs), nonce=None)


def test_es256_and_nonce() -> None:
    idp = FakeIdp()
    provider = _provider(idp)
    assert provider.verify_id_token(idp.id_token(alg="ES256", nonce="n1"), nonce="n1")
    with pytest.raises(LoginRejected, match="nonce"):
        provider.verify_id_token(idp.id_token(nonce="n1"), nonce="n2")


def test_claims_map_onto_the_session() -> None:
    idp = FakeIdp()
    provider = _provider(idp)
    redirect = "http://127.0.0.1:9119/auth/callback"
    started = provider.start_login(redirect)
    query = parse_qs(urlsplit(started.url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid profile email"]
    assert "verifier" in started.keep and started.keep["verifier"] not in started.url
    back = idp.client().get(started.url)
    code = parse_qs(urlsplit(back.headers["location"]).query)["code"][0]
    session = provider.complete_login(code, started.keep, redirect)
    assert session.user_id == "user-1" and session.email == "ada@example.test"
    assert session.display_name == "Ada" and session.org_id == "ops,dev"
    assert session.refresh_token
    verified = provider.verify_session(session.token)
    assert verified is not None
    assert (verified.user_id, verified.email, verified.token) == (
        session.user_id,
        session.email,
        session.token,
    )
    assert verified.refresh_token == ""  # the refresh token never rides in the session token


def test_display_name_falls_back() -> None:
    idp = FakeIdp(user={"sub": "u2", "preferred_username": "ada2", "org_id": "acme"})
    provider = _provider(idp)
    claims = provider.verify_id_token(idp.id_token(), nonce=None)
    session = provider._session_from(claims, {"expires_in": 120}, refresh="")  # pyright: ignore[reportPrivateUsage]
    assert session.display_name == "ada2" and session.org_id == "acme"
    only_email = FakeIdp(user={"sub": "u3", "email": "x@example.test"})
    claims = _provider(only_email).verify_id_token(only_email.id_token(), nonce=None)
    named = _provider(only_email)._session_from(claims, {}, refresh="")  # pyright: ignore[reportPrivateUsage]
    assert named.display_name == "x@example.test"


def test_a_wrong_verifier_or_reused_code_fails() -> None:
    idp = FakeIdp()
    provider = _provider(idp)
    redirect = "http://127.0.0.1:9119/auth/callback"
    started = provider.start_login(redirect)
    back = idp.client().get(started.url)
    code = parse_qs(urlsplit(back.headers["location"]).query)["code"][0]
    with pytest.raises(LoginRejected):
        provider.complete_login(code, {**started.keep, "verifier": "x" * 64}, redirect)
    with pytest.raises(LoginRejected):
        provider.complete_login(code, started.keep, redirect)


def test_two_concurrent_refreshes_make_one_grant_call() -> None:
    idp = FakeIdp(refresh_gate=threading.Event())
    provider = _provider(idp)
    refresh = idp._tokens(nonce=None)["refresh_token"]  # pyright: ignore[reportPrivateUsage]
    expired = Session(user_id="user-1", provider="oidc", expires_at=0)
    results: list[Session | BaseException] = []

    def run() -> None:
        try:
            results.append(provider.refresh(expired, refresh))
        except BaseException as exc:  # noqa: BLE001 - collected for the assert
            results.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    gate = idp.refresh_gate
    assert gate is not None
    # both callers are inside: one at the IdP, one waiting on the flight
    deadline = threading.Event()
    for _ in range(200):
        if idp.refresh_calls >= 1:
            break
        deadline.wait(0.01)
    gate.set()
    for thread in threads:
        thread.join(5)
    assert idp.refresh_calls == 1
    assert len(results) == 2 and all(isinstance(item, Session) for item in results)
    first, second = results
    assert isinstance(first, Session) and isinstance(second, Session)
    assert first.token == second.token


def test_a_refused_refresh_ends_the_session() -> None:
    idp = FakeIdp()
    provider = _provider(idp)
    with pytest.raises(RefreshRejected):
        provider.refresh(Session(user_id="user-1", provider="oidc", expires_at=0), "not-a-token")
    without_identity = FakeIdp(refresh_on_id_token=False)
    other = _provider(without_identity)
    refresh = without_identity._tokens(nonce=None)["refresh_token"]  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RefreshRejected):
        other.refresh(None, refresh)


# -- through the app ------------------------------------------------------------------------------


def _oidc_app(build: Builder, idp: FakeIdp, **config: Any) -> Any:
    dashboard = {"oidc": {"issuer": ISSUER, "client_id": CLIENT_ID}, **config}
    return build(host="0.0.0.0", config={"dashboard": dashboard}, http=idp.client())


def test_full_login_round_trip(build: Builder) -> None:
    idp = FakeIdp()
    built = _oidc_app(build, idp)
    start = built.client.get("/auth/login?provider=oidc&next=%2Fsessions")
    assert start.status_code == 302
    location = start.headers["location"]
    assert location.startswith(f"{ISSUER}/authorize?")
    redirect_uri = parse_qs(urlsplit(location).query)["redirect_uri"][0]
    assert redirect_uri == "http://127.0.0.1:9119/auth/callback"
    back = idp.client().get(location)
    callback = back.headers["location"]
    assert callback.startswith(redirect_uri)
    done = built.client.get(callback.removeprefix("http://127.0.0.1:9119"))
    assert done.status_code == 302 and done.headers["location"] == "/sessions"
    me = built.client.get("/api/auth/me").json()
    assert me["user_id"] == "user-1" and me["provider"] == "oidc"
    built.client.post("/auth/logout")
    assert idp.revoked, "logout revokes the refresh token at the IdP"
    assert built.client.get("/api/auth/me").status_code == 401


def test_callback_refuses_a_wrong_state(build: Builder) -> None:
    idp = FakeIdp()
    built = _oidc_app(build, idp)
    location = built.client.get("/auth/login?provider=oidc").headers["location"]
    back = idp.client().get(location).headers["location"]
    forged = back.split("&state=")[0] + "&state=forged"
    refused = built.client.get(forged.removeprefix("http://127.0.0.1:9119"))
    assert refused.status_code == 400
    assert "state_mismatch" in refused.json()["detail"]
    assert built.client.get("/api/auth/me").status_code == 401


def test_one_redirect_provider_signs_in_automatically_once(build: Builder) -> None:
    idp = FakeIdp()
    built = _oidc_app(build, idp)
    first = built.client.get("/logs")
    assert first.status_code == 302
    assert first.headers["location"] == "/auth/login?provider=oidc&next=%2Flogs"
    second = built.client.get("/logs")
    assert second.status_code == 302 and second.headers["location"].startswith("/login")


def test_an_expired_session_is_renewed_with_the_refresh_token(build: Builder) -> None:
    idp = FakeIdp(expires_in=60)
    built = _oidc_app(build, idp)
    location = built.client.get("/auth/login?provider=oidc").headers["location"]
    callback = idp.client().get(location).headers["location"]
    built.client.get(callback.removeprefix("http://127.0.0.1:9119"))
    signer = built.state.auth.signer
    start = signer.now()
    signer._now = lambda: start + 3600  # pyright: ignore[reportPrivateUsage]
    renewed = built.client.get("/api/auth/me")
    assert renewed.status_code == 200
    assert idp.refresh_calls == 1
    assert any("indus_session_at" in line for line in renewed.headers.get_list("set-cookie"))


def test_env_values_win_over_config_when_set() -> None:
    from indus.web.auth.oidc import CLIENT_ID_ENV, ISSUER_ENV, SCOPES_ENV, OidcSettings

    config = {"issuer": "https://a.example", "client_id": "a", "scopes": "openid"}
    assert OidcSettings.read(config, {}).scopes == "openid"
    env = {ISSUER_ENV: "https://b.example", CLIENT_ID_ENV: "b", SCOPES_ENV: "openid email"}
    read = OidcSettings.read(config, env)
    assert (read.issuer, read.client_id, read.scopes) == ("https://b.example", "b", "openid email")
    blank = OidcSettings.read(config, {SCOPES_ENV: "  "})
    assert blank.scopes == "openid"  # a blank variable counts as unset
