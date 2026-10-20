"""P18-05: one build served at ``/`` and under ``/indus/`` (R09.23)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from indus.web.prefix import normalise_prefix, rewrite_css, rewrite_index_html
from tests.fakes.idp.fake_oidc import CLIENT_ID, ISSUER, FakeIdp
from tests.unit.web.conftest import CSS, INDEX_HTML, Builder

PREFIX = {"x-forwarded-prefix": "/indus"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/indus", "/indus"),
        ("indus/", "/indus"),
        ("/a/b/", "/a/b"),
        ("/", ""),
        ("", ""),
        (None, ""),
        ("/a/../b", ""),
        ("//evil", ""),
        ("/a b", ""),
        ('/a"b', ""),
        ("/<script>", ""),
        ("/a%2e%2e", ""),
        ("/a\nb", ""),
        ("/" + "x" * 300, ""),
    ],
)
def test_prefix_normalisation(raw: str | None, expected: str) -> None:
    assert normalise_prefix(raw) == expected


def test_rewrites_touch_only_bundle_urls() -> None:
    html = rewrite_index_html(INDEX_HTML + '<a href="/sessions">x</a>', "/indus")
    assert 'href="/indus/assets/index-abc123.css"' in html
    assert 'src="/indus/assets/index-abc123.js"' in html
    assert 'href="/indus/fonts/Sans.woff2"' in html
    assert 'href="/indus/favicon.ico"' in html
    assert 'href="/sessions"' in html
    css = rewrite_css(CSS, "/indus")
    assert "url(/indus/fonts/Sans.woff2)" in css and "url('/indus/assets/bg.png')" in css
    assert rewrite_index_html(INDEX_HTML, "") == INDEX_HTML and rewrite_css(CSS, "") == CSS


def test_same_build_at_root_and_under_a_prefix(build: Builder) -> None:
    built = build()
    root = built.client.get("/sessions").text
    assert 'src="/assets/index-abc123.js"' in root
    assert 'window.__INDUS_BASE_PATH__="";' in root
    prefixed = built.client.get("/sessions", headers=PREFIX).text
    assert 'src="/indus/assets/index-abc123.js"' in prefixed
    assert 'href="/indus/assets/index-abc123.css"' in prefixed
    assert 'window.__INDUS_BASE_PATH__="/indus";' in prefixed
    css = built.client.get("/assets/index-abc123.css", headers=PREFIX)
    assert "url(/indus/fonts/Sans.woff2)" in css.text
    assert "X-Forwarded-Prefix" in css.headers["vary"]
    assert "url(/fonts/Sans.woff2)" in built.client.get("/assets/index-abc123.css").text


def test_prefix_from_an_untrusted_peer_is_ignored(build: Builder) -> None:
    outside = build(peer="198.51.100.7", host="127.0.0.1")
    page = outside.client.get("/sessions", headers=PREFIX).text
    assert 'window.__INDUS_BASE_PATH__="";' in page


def test_callback_ignores_the_prefix_when_a_public_url_is_set(build: Builder) -> None:
    idp = FakeIdp()
    oidc = {"oidc": {"issuer": ISSUER, "client_id": CLIENT_ID}}
    declared = build(
        config={"dashboard": {**oidc, "public_url": "https://dash.example.test/indus"}},
        http=idp.client(),
    )
    start = declared.client.get(
        "/auth/login?provider=oidc", headers={**PREFIX, "host": "dash.example.test"}
    )
    redirect = parse_qs(urlsplit(start.headers["location"]).query)["redirect_uri"][0]
    assert redirect == "https://dash.example.test/indus/auth/callback"
    undeclared = build(host="0.0.0.0", config={"dashboard": oidc}, http=idp.client())
    start = undeclared.client.get("/auth/login?provider=oidc", headers=PREFIX)
    redirect = parse_qs(urlsplit(start.headers["location"]).query)["redirect_uri"][0]
    assert redirect == "http://127.0.0.1:9119/indus/auth/callback"
    assert start.headers["set-cookie"].count("Path=/indus") >= 1
