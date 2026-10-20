"""P18-03: the username/password provider, the session signer and the password-login route."""

from __future__ import annotations

import pytest

from indus.web.auth.basic import (
    PASSWORD_ENV,
    USERNAME_ENV,
    BasicProvider,
    hash_password,
    looks_like_hash,
    register_basic,
    verify_password,
)
from indus.web.auth.providers import (
    SESSION_SECRET_ENV,
    SESSION_TTL_S,
    BadCredentials,
    ProviderRegistry,
    Session,
    SessionSigner,
)
from indus.web.routes.auth import LoginLimiter
from tests.unit.web.conftest import Builder, basic_config, cheap_hash


class Clock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


def test_argon2id_hashes_round_trip() -> None:
    encoded = cheap_hash("correct horse")
    assert encoded.startswith("$argon2id$v=19$")
    assert looks_like_hash(encoded)
    assert verify_password("correct horse", encoded)
    assert not verify_password("correct hors", encoded)
    assert not verify_password("correct horse", "plain text")
    assert not verify_password("correct horse", "$argon2i$v=19$m=1024,t=1,p=1$c2FsdHNhbHQ$aGFzaA")
    assert cheap_hash("same") != cheap_hash("same")  # salted
    with pytest.raises(ValueError):
        hash_password("")


def test_sessions_expire_at_the_ttl() -> None:
    clock = Clock()
    signer = SessionSigner(b"k" * 32, now=clock)
    provider = BasicProvider(username="admin", signer=signer, password_hash=cheap_hash("pw"))
    session = provider.password_login("admin", "pw")
    assert session.expires_at == int(clock.value) + SESSION_TTL_S == int(clock.value) + 43_200
    clock.value += SESSION_TTL_S - 1
    assert provider.verify_session(session.token) is not None
    clock.value += 1
    assert provider.verify_session(session.token) is None
    assert signer.verify(session.token, allow_expired=True) is not None


def test_tampered_or_foreign_tokens_are_refused() -> None:
    signer = SessionSigner(b"k" * 32)
    other = SessionSigner(b"q" * 32)
    provider = BasicProvider(username="admin", signer=signer, password_hash=cheap_hash("pw"))
    token = provider.password_login("admin", "pw").token
    version, payload, signature = token.split(".")
    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    for bad in (
        f"{version}.{payload}.{flipped}",
        f"v2.{payload}.{signature}",
        token + ".x",
        "garbage",
        "",
        other.mint(Session(user_id="admin", provider="basic", expires_at=0)).token,
    ):
        assert provider.verify_session(bad) is None
    # a token of another provider name is not a basic session
    foreign = signer.mint(Session(user_id="admin", provider="oidc", expires_at=0)).token
    assert provider.verify_session(foreign) is None
    assert "token" not in repr(provider.password_login("admin", "pw")).lower().split("(")[1]


def test_changing_the_username_ends_sessions() -> None:
    signer = SessionSigner(b"k" * 32)
    first = BasicProvider(username="admin", signer=signer, password_hash=cheap_hash("pw"))
    token = first.password_login("admin", "pw").token
    renamed = BasicProvider(username="root2", signer=signer, password_hash=cheap_hash("pw"))
    assert renamed.verify_session(token) is None


def test_unknown_user_and_wrong_password_look_alike() -> None:
    calls: list[str] = []

    def dummy() -> str:
        calls.append("dummy")
        return cheap_hash("never")

    provider = BasicProvider(
        username="admin", signer=SessionSigner(), password_hash=cheap_hash("pw"), dummy_hash=dummy
    )
    with pytest.raises(BadCredentials) as wrong_user:
        provider.password_login("nobody", "pw")
    with pytest.raises(BadCredentials) as wrong_password:
        provider.password_login("admin", "nope")
    assert str(wrong_user.value) == str(wrong_password.value)
    assert calls == ["dummy"]  # the unknown user still paid for one verification


def test_env_plaintext_beats_the_config_hash() -> None:
    registry = ProviderRegistry()
    signer = SessionSigner()
    settings = {"username": "admin", "password_hash": cheap_hash("from-config")}
    provider = register_basic(registry, settings, {PASSWORD_ENV: "from-env"}, signer)
    assert provider is not None
    assert provider.password_login("admin", "from-env").user_id == "admin"
    with pytest.raises(BadCredentials):
        provider.password_login("admin", "from-config")
    renamed = register_basic(
        ProviderRegistry(), settings, {USERNAME_ENV: "ops", PASSWORD_ENV: "x"}, signer
    )
    assert renamed is not None and renamed.password_login("ops", "x").user_id == "ops"


@pytest.mark.parametrize(
    ("settings", "env", "reason"),
    [
        ({}, {}, "no username set"),
        ({"username": "admin"}, {}, "no password set"),
        ({"username": "admin", "password_hash": "hunter2"}, {}, "not an argon2id hash"),
    ],
)
def test_reasons_when_not_configured(
    settings: dict[str, str], env: dict[str, str], reason: str
) -> None:
    registry = ProviderRegistry()
    assert register_basic(registry, settings, env, SessionSigner()) is None
    assert reason in registry.reasons["basic"]
    assert not registry.interactive()


def test_session_secret_from_env() -> None:
    key = "s" * 40
    a = SessionSigner.from_env({SESSION_SECRET_ENV: key})
    b = SessionSigner.from_env({SESSION_SECRET_ENV: key})
    session = a.mint(Session(user_id="admin", provider="basic", expires_at=0))
    assert not a.random_key
    assert b.verify(session.token) is not None
    weak = SessionSigner.from_env({SESSION_SECRET_ENV: "short"})
    assert weak.random_key
    assert SessionSigner.from_env({}).verify(session.token) is None


def test_login_route_limits_attempts_per_address(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    clock = Clock(0.0)
    built.state.extras["login_limiter"] = LoginLimiter(monotonic=clock)
    for _ in range(10):
        response = built.client.post(
            "/auth/password-login", json={"username": "admin", "password": "wrong"}
        )
        assert response.status_code == 401
    eleventh = built.client.post(
        "/auth/password-login", json={"username": "admin", "password": "correct horse"}
    )
    assert eleventh.status_code == 429
    assert eleventh.json()["error"] == "rate_limited"
    clock.value += 60.0
    assert (
        built.client.post(
            "/auth/password-login", json={"username": "admin", "password": "correct horse"}
        ).status_code
        == 200
    )


def test_wrong_user_and_wrong_password_give_identical_bodies(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    unknown = built.client.post(
        "/auth/password-login", json={"username": "nobody", "password": "correct horse"}
    )
    wrong = built.client.post("/auth/password-login", json={"username": "admin", "password": "no"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.content == wrong.content
    assert "set-cookie" not in unknown.headers


def test_login_sets_cookies_and_identity(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    response = built.client.post(
        "/auth/password-login",
        json={"username": "admin", "password": "correct horse", "next": "/sessions"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "next": "/sessions"}
    me = built.client.get("/api/auth/me").json()
    assert me["user_id"] == "admin" and me["provider"] == "basic"
    assert "token" not in me
    providers = built.client.get("/api/auth/providers").json()
    assert providers["auth_required"] is True
    assert [p["name"] for p in providers["providers"]] == ["basic"]


def test_form_login_redirects(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    bad = built.client.post(
        "/auth/password-login",
        data={"username": "admin", "password": "nope", "next": "/logs"},
    )
    assert bad.status_code == 303
    assert bad.headers["location"] == "/login?error=1&next=%2Flogs"
    good = built.client.post(
        "/auth/password-login",
        data={"username": "admin", "password": "correct horse", "next": "//evil.example"},
    )
    assert good.status_code == 303 and good.headers["location"] == "/"
    assert built.client.get("/api/auth/me").status_code == 200


def test_login_page_lists_providers_with_a_csp(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    page = built.client.get("/login?next=%2Fsessions")
    assert page.status_code == 200
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert 'name="username"' in page.text and 'value="/sessions"' in page.text
    assert "<script" not in page.text
    loopback = build()
    assert loopback.client.get("/login").status_code == 302
    assert loopback.client.post("/auth/password-login", json={}).status_code == 404
