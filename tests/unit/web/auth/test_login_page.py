"""P18-28: the server-rendered sign-in page and its round trip."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from indus.web.auth import cookies
from indus.web.auth.login_page import (
    LOGIN_CSP,
    LOGIN_ERRORS,
    LOGIN_STYLE,
    login_headers,
    render_login_page,
)
from indus.web.auth.providers import Provider
from indus.web.routes.auth import LOGIN_LIMIT
from tests.fakes.idp.fake_oidc import CLIENT_ID, ISSUER, FakeIdp
from tests.unit.web.conftest import Builder, basic_config, cheap_hash


def _both(idp: FakeIdp) -> dict[str, Any]:
    dashboard = {
        "basic_auth": {"username": "admin", "password_hash": cheap_hash("correct horse")},
        "oidc": {"issuer": ISSUER, "client_id": CLIENT_ID},
    }
    return {"dashboard": dashboard}


def _forms(page: str) -> list[str]:
    return re.findall(r"<form\b[^>]*>.*?</form>", page, flags=re.S)


def _links(page: str) -> list[str]:
    return re.findall(r'<a class="go" href="([^"]+)"', page)


def test_both_providers_render_with_next_and_prefix(build: Builder) -> None:
    idp = FakeIdp()
    built = build(host="0.0.0.0", config=_both(idp), http=idp.client())
    page = built.client.get(
        "/login?next=%2Findus%2Fsessions", headers={"x-forwarded-prefix": "/indus"}
    )
    assert page.status_code == 200
    forms = _forms(page.text)
    assert len(forms) == 1
    form = forms[0]
    assert 'action="/indus/auth/password-login"' in form and 'method="post"' in form
    assert 'name="provider" value="basic"' in form
    assert 'name="next" value="/indus/sessions"' in form
    assert 'name="username"' in form and 'type="password"' in form
    (link,) = _links(page.text)
    parts = urlsplit(link.replace("&amp;", "&"))
    assert parts.path == "/indus/auth/login"
    assert parse_qs(parts.query) == {"provider": ["oidc"], "next": ["/indus/sessions"]}
    # Registry order: the password form comes first, then the redirect provider.
    assert page.text.index("<form") < page.text.index('<a class="go"')


def test_the_link_starts_the_redirect_login(build: Builder) -> None:
    idp = FakeIdp()
    built = build(host="0.0.0.0", config=_both(idp), http=idp.client())
    page = built.client.get("/login?next=%2Flogs")
    (link,) = _links(page.text)
    start = built.client.get(link.replace("&amp;", "&"))
    assert start.status_code == 302
    assert start.headers["location"].startswith(f"{ISSUER}/authorize?")
    assert cookies.PKCE_COOKIE in start.headers.get("set-cookie", "")


def test_page_headers_are_strict(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    page = built.client.get("/login")
    for name, value in login_headers().items():
        assert page.headers[name] == value
    csp = page.headers["content-security-policy"]
    assert csp == LOGIN_CSP
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp and "base-uri 'none'" in csp
    assert "unsafe-inline" not in csp and "script-src" not in csp
    digest = base64.b64encode(hashlib.sha256(LOGIN_STYLE.encode()).digest()).decode()
    assert f"style-src 'sha256-{digest}'" in csp
    assert f"<style>{LOGIN_STYLE}</style>" in page.text  # the hash covers the very text sent
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"


def test_the_form_post_keeps_its_origin(build: Builder) -> None:
    # Under `Referrer-Policy: no-referrer` a browser sends `Origin: null` on the form's POST and
    # the gate's cross-site check refuses it (seen in the P18-30 browser run): the page asks for
    # `same-origin`, so the POST names this origin and the referrer never leaves it.
    built = build(host="0.0.0.0", config=basic_config())
    page = built.client.get("/login")
    assert page.headers["referrer-policy"] == "same-origin"
    assert '<meta name="referrer" content="same-origin">' in page.text
    host = "testserver"
    signed = built.client.post(
        "/auth/password-login",
        data={"provider": "basic", "username": "admin", "password": "correct horse"},
        headers={"origin": f"http://{host}", "host": host},
    )
    assert signed.status_code == 303 and signed.headers["location"] == "/"
    blank = built.client.post(
        "/auth/password-login",
        data={"provider": "basic", "username": "admin", "password": "correct horse"},
        headers={"origin": "null"},
    )
    assert blank.status_code == 403


def test_page_has_no_script_and_no_secret(build: Builder) -> None:
    secret = "s" * 40
    built = build(
        host="0.0.0.0",
        config=basic_config(),
        env={"INDUS_DASHBOARD_SESSION_SECRET": secret},
    )
    page = built.client.get("/login").text
    assert "<script" not in page.lower()
    assert "__INDUS_" not in page
    assert built.token not in page
    assert secret not in page
    assert "/assets/" not in page  # no bundle file is needed before sign-in


def test_values_are_escaped() -> None:
    class Odd(Provider):
        name = 'x"><b>'
        display_name = "<img src=x>"
        kind = "redirect"

    class OddPassword(Provider):
        name = "pw"
        display_name = "<i>Local</i>"
        kind = "password"

    page = render_login_page(
        providers=[OddPassword(), Odd()], prefix="", next_path='/a"><script>x</script>'
    )
    assert "<script" not in page and "<img" not in page and "<i>" not in page
    assert "&lt;i&gt;Local&lt;/i&gt;" in page
    assert 'value="/a&quot;&gt;&lt;script&gt;' in page
    assert "provider=x%22%3E%3Cb%3E" in page


@pytest.mark.parametrize(
    ("code", "shown"),
    [
        ("1", LOGIN_ERRORS["invalid"]),
        ("invalid", LOGIN_ERRORS["invalid"]),
        ("rate_limited", LOGIN_ERRORS["rate_limited"]),
        ("unavailable", LOGIN_ERRORS["unavailable"]),
        ("<b>hacked</b>", ""),
        ("", ""),
    ],
)
def test_only_known_error_codes_show_a_message(build: Builder, code: str, shown: str) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    page = built.client.get("/login", params={"error": code}).text
    assert "hacked" not in page
    if shown:
        assert shown in page and 'role="alert"' in page
    else:
        assert 'role="alert"' not in page


def test_unsafe_next_is_dropped(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    for raw in ("//evil.example/x", "https://evil.example/", "/a\\b", "/login"):
        page = built.client.get("/login", params={"next": raw}).text
        assert 'name="next"' not in page


def test_no_provider_says_so() -> None:
    page = render_login_page(providers=[])
    assert "No sign-in method" in page and "<form" not in page


def test_loopback_and_serve_have_no_login_page(build: Builder) -> None:
    assert build().client.get("/login").headers["location"] == "/"
    headless = build(mode="serve")
    answer = headless.client.get("/login")
    assert answer.status_code == 404 and answer.headers["content-type"].startswith(
        "application/json"
    )


def test_a_rate_limited_form_post_comes_back_to_the_page(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    form = {"username": "admin", "password": "nope", "next": "/logs"}
    for _ in range(LOGIN_LIMIT):
        assert built.client.post("/auth/password-login", data=form).status_code == 303
    limited = built.client.post("/auth/password-login", data=form)
    assert limited.status_code == 303
    assert limited.headers["location"] == "/login?error=rate_limited&next=%2Flogs"
    shown = built.client.get(limited.headers["location"]).text
    assert LOGIN_ERRORS["rate_limited"] in shown
    # A JSON client still gets the 429 body.
    assert built.client.post("/auth/password-login", json=form).status_code == 429


def _held(built: Any, bare: str) -> bool:
    return any(cookie.name.endswith(bare) and cookie.value for cookie in built.client.cookies.jar)


def test_form_sign_in_then_logout_clears_every_cookie(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    signed = built.client.post(
        "/auth/password-login",
        data={"provider": "basic", "username": "admin", "password": "correct horse"},
    )
    assert signed.status_code == 303 and signed.headers["location"] == "/"
    assert _held(built, cookies.AT_COOKIE)
    assert built.client.get("/api/auth/me").json()["user_id"] == "admin"

    out = built.client.post("/auth/logout")
    assert out.status_code == 302 and out.headers["location"] == "/login"
    cleared = [line.lower() for line in out.headers.get_list("set-cookie")]
    for name in cookies.ALL_COOKIES:
        assert any(line.startswith(f"{name}=") and "max-age=0" in line for line in cleared), name
    assert not _held(built, cookies.AT_COOKIE)
    assert built.client.get("/api/auth/me").status_code == 401


def test_logout_from_another_origin_is_refused(build: Builder) -> None:
    built = build(host="0.0.0.0", config=basic_config())
    built.client.post(
        "/auth/password-login", data={"username": "admin", "password": "correct horse"}
    )
    refused = built.client.post("/auth/logout", headers={"origin": "https://evil.example"})
    assert refused.status_code == 403
    assert built.client.get("/api/auth/me").status_code == 200
