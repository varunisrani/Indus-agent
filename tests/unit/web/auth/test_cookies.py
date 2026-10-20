"""P18-03: cookie names, attributes per scheme and prefix, readers and logout clearing."""

from __future__ import annotations

from http.cookies import SimpleCookie

import pytest
from starlette.requests import HTTPConnection
from starlette.responses import Response

from indus.web.auth import cookies
from tests.unit.web.conftest import Builder, basic_config


def _parsed(response: Response) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for name, value in response.raw_headers:
        if name != b"set-cookie":
            continue
        text = value.decode("latin-1")
        jar: SimpleCookie = SimpleCookie()
        jar.load(text)
        for key, morsel in jar.items():
            attrs = {k: str(v) for k, v in morsel.items() if v}
            attrs["value"] = morsel.value
            attrs["raw"] = text
            found[f"{key}|{attrs.get('path', '')}"] = attrs
    return found


def _one(response: Response, name: str) -> dict[str, str]:
    matches = [v for k, v in _parsed(response).items() if k.split("|")[0] == name]
    assert len(matches) == 1, (name, _parsed(response).keys())
    return matches[0]


def _session(response: Response, *, https: bool, prefix: str, refresh: str = "") -> None:
    cookies.set_session(
        response,
        session_token="tok",
        max_age=43_200,
        refresh_token=refresh,
        provider="basic",
        https=https,
        prefix=prefix,
    )


def test_plain_http_uses_bare_names_and_lax() -> None:
    response = Response()
    _session(response, https=False, prefix="")
    at = _one(response, "indus_session_at")
    assert at["max-age"] == "43200" and at["path"] == "/"
    assert at["samesite"].lower() == "lax" and "httponly" in at["raw"].lower()
    assert "secure" not in at["raw"].lower()
    assert not [k for k in _parsed(response) if k.startswith("indus_session_rt")]
    assert _one(response, "indus_session_provider")["value"] == "basic"


def test_https_root_uses_host_prefix_and_secure() -> None:
    response = Response()
    _session(response, https=True, prefix="", refresh="rt-value")
    at = _one(response, "__Host-indus_session_at")
    assert "secure" in at["raw"].lower() and at["path"] == "/"
    rt = _one(response, "__Host-indus_session_rt")
    assert rt["max-age"] == str(30 * 24 * 3600)


def test_https_under_a_prefix_uses_secure_prefix_and_path() -> None:
    response = Response()
    _session(response, https=True, prefix="/indus")
    at = _one(response, "__Secure-indus_session_at")
    assert at["path"] == "/indus" and "secure" in at["raw"].lower()


@pytest.mark.parametrize(("https", "samesite"), [(True, "none"), (False, "lax")])
def test_pkce_cookie_attributes(https: bool, samesite: str) -> None:
    response = Response()
    cookies.set_pkce(response, {"state": "s", "verifier": "v"}, https=https, prefix="")
    name = "__Host-indus_session_pkce" if https else "indus_session_pkce"
    pkce = _one(response, name)
    assert pkce["max-age"] == "600"
    assert pkce["samesite"].lower() == samesite
    assert cookies.decode_payload(pkce["value"]) == {"state": "s", "verifier": "v"}


def test_short_lifetimes_are_floored_and_sso_marker_is_60s() -> None:
    response = Response()
    cookies.set_session(
        response,
        session_token="t",
        max_age=5,
        refresh_token="",
        provider="",
        https=False,
        prefix="",
    )
    assert _one(response, "indus_session_at")["max-age"] == "60"
    marker = Response()
    cookies.set_sso_attempt(marker, https=False, prefix="")
    assert _one(marker, "indus_sso_attempt")["max-age"] == "60"


def test_readers_accept_every_variant() -> None:
    for name in ("__Host-indus_session_at", "__Secure-indus_session_at", "indus_session_at"):
        scope = {"type": "http", "headers": [(b"cookie", f"{name}=abc".encode())]}
        assert cookies.read(HTTPConnection(scope), cookies.AT_COOKIE) == "abc"
    empty = {"type": "http", "headers": []}
    assert cookies.read(HTTPConnection(empty), cookies.AT_COOKIE) is None


def test_payload_round_trip_and_garbage() -> None:
    raw = cookies.encode_payload({"next": "/a;b=c", "state": "x"})
    assert "=" not in raw and ";" not in raw
    assert cookies.decode_payload(raw) == {"next": "/a;b=c", "state": "x"}
    for bad in (None, "", "%%%", "bm90IGpzb24", "W10"):
        assert cookies.decode_payload(bad) == {}


def test_clear_all_expires_every_variant() -> None:
    response = Response()
    cookies.clear_all(response, prefix="/indus")
    names = {key.split("|")[0] for key in _parsed(response)}
    for bare in cookies.ALL_COOKIES:
        assert {bare, f"__Host-{bare}", f"__Secure-{bare}"} <= names
    assert all(v["max-age"] == "0" for v in _parsed(response).values())


def test_logout_clears_every_cookie(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    built.client.post(
        "/auth/password-login", json={"username": "admin", "password": "correct horse"}
    )
    assert built.client.get("/api/auth/me").status_code == 200
    response = built.client.post("/auth/logout")
    assert response.status_code == 302 and response.headers["location"] == "/login"
    cleared = {line.split("=", 1)[0] for line in response.headers.get_list("set-cookie")}
    for bare in cookies.ALL_COOKIES:
        assert bare in cleared and f"__Host-{bare}" in cleared
    assert built.client.get("/api/auth/me").status_code == 401


def test_logout_clears_the_cookies_even_when_revoking_breaks(
    build: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    built.client.post(
        "/auth/password-login", json={"username": "admin", "password": "correct horse"}
    )
    provider = built.state.auth.registry.interactive()[0]

    def broken(_refresh: str) -> None:
        raise RuntimeError("identity provider answered nonsense")

    monkeypatch.setattr(provider, "revoke", broken)
    built.client.cookies.set(cookies.RT_COOKIE, "r" + "t" * 20)
    response = built.client.post("/auth/logout")
    assert response.status_code == 302
    cleared = {line.split("=", 1)[0] for line in response.headers.get_list("set-cookie")}
    assert cookies.AT_COOKIE in cleared and cookies.RT_COOKIE in cleared
