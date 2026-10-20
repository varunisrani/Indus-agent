"""P18-05: public URL rules, trusted proxies and forwarded headers."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from indus.web.auth.proxy import (
    PUBLIC_URL_ENV,
    ForwardedHeaders,
    ProxyConfigError,
    PublicOrigin,
    TrustedProxies,
    callback_url,
    normalise_public_url,
    parse_trusted_proxies,
    peer_is_trusted,
    resolve_public_url,
)
from tests.unit.web.conftest import Builder, basic_config


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://dash.example.test/indus/", "https://dash.example.test/indus"),
        ("http://127.0.0.1:9119", "http://127.0.0.1:9119"),
        ("  https://dash.example.test  ", "https://dash.example.test"),
        ("dash.example.test", ""),
        ("https://", ""),
        ('https://dash"x.test', ""),
        ("https://<dash>.test", ""),
        ("https://dash.example.test/a b", ""),
        ("https://dash.example.test/\x07", ""),
        ("https://user:pw@dash.example.test", ""),
        ("https://dash.example.test/?x=1", ""),
        ("ftp://dash.example.test", ""),
        ("https://dash.example.test:99999", ""),
        (None, ""),
    ],
)
def test_public_url_validation(raw: object, expected: str) -> None:
    assert normalise_public_url(raw) == expected


def test_env_beats_config_and_blank_env_is_unset(caplog: pytest.LogCaptureFixture) -> None:
    found = resolve_public_url({PUBLIC_URL_ENV: "https://env.example.test"}, "https://cfg.test")
    assert found == PublicOrigin("https://env.example.test", "env.example.test")
    assert resolve_public_url({PUBLIC_URL_ENV: "  "}, "https://cfg.test") == PublicOrigin(
        "https://cfg.test", "cfg.test"
    )
    with caplog.at_level("WARNING"):
        bad = resolve_public_url({PUBLIC_URL_ENV: "not a url"}, "also <bad>")
    assert bad is None
    assert "ignored" in caplog.text and "not a url" not in caplog.text


def test_callback_url_uses_the_public_url_verbatim() -> None:
    public = PublicOrigin.parse("https://dash.example.test/indus")
    assert callback_url(public, scheme="http", host_header="x", prefix="/other") == (
        "https://dash.example.test/indus/auth/callback"
    )
    assert callback_url(None, scheme="https", host_header="h.test", prefix="/indus") == (
        "https://h.test/indus/auth/callback"
    )


@pytest.mark.parametrize("entry", ["*", "0.0.0.0/0", "::/0", "proxy.local", "10.0.0.5/24", ""])
def test_open_or_invalid_proxy_entries_are_refused(entry: str) -> None:
    with pytest.raises(ProxyConfigError):
        parse_trusted_proxies([entry])


def test_bounded_proxies_are_accepted() -> None:
    proxies = parse_trusted_proxies(["172.20.0.5", "10.8.0.0/16", "fd00::/64"])
    assert proxies.trusts("172.20.0.5") and proxies.trusts("10.8.3.4") and proxies.trusts("fd00::7")
    assert not proxies.trusts("172.20.0.6") and not proxies.trusts("8.8.8.8")
    assert (
        proxies.trusts("127.0.0.1") and proxies.trusts("::1") and proxies.trusts("::ffff:127.0.0.1")
    )
    assert not proxies.trusts("testclient") and not proxies.trusts("")
    assert parse_trusted_proxies(None) == TrustedProxies()


def test_zero_slash_zero_in_config_refuses_to_start(build: Builder) -> None:
    with pytest.raises(ProxyConfigError):
        build(config={"dashboard": {"trusted_proxies": ["0.0.0.0/0"]}})


def _echo_app(proxies: TrustedProxies) -> Any:
    async def echo(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "client": request.client.host if request.client else "",
                "scheme": request.url.scheme,
                "trusted": peer_is_trusted(request.scope),
            }
        )

    return ForwardedHeaders(Starlette(routes=[Route("/", echo)]), proxies=proxies)


FORWARDED = {"x-forwarded-for": "203.0.113.9, 172.20.0.5", "x-forwarded-proto": "https"}


def test_forwarded_headers_from_a_listed_proxy_are_applied() -> None:
    app = _echo_app(parse_trusted_proxies(["172.20.0.5"]))
    client = TestClient(app, client=("172.20.0.5", 4000))
    assert client.get("/", headers=FORWARDED).json() == {
        "client": "203.0.113.9",
        "scheme": "https",
        "trusted": True,
    }


def test_forwarded_headers_from_other_peers_are_ignored() -> None:
    app = _echo_app(parse_trusted_proxies(["172.20.0.5"]))
    client = TestClient(app, client=("198.51.100.7", 4000))
    assert client.get("/", headers=FORWARDED).json() == {
        "client": "198.51.100.7",
        "scheme": "http",
        "trusted": False,
    }


def test_a_spoofed_chain_stops_at_the_first_untrusted_hop() -> None:
    app = _echo_app(TrustedProxies())
    client = TestClient(app, client=("127.0.0.1", 4000))
    spoofed = {"x-forwarded-for": "1.2.3.4, 203.0.113.9", "x-forwarded-proto": "gopher"}
    body = client.get("/", headers=spoofed).json()
    assert body["client"] == "203.0.113.9" and body["scheme"] == "http"
    garbage = client.get("/", headers={"x-forwarded-for": "not-an-ip"}).json()
    assert garbage["client"] == "127.0.0.1"


def test_rate_limits_follow_the_forwarded_client(build: Builder) -> None:
    built = build(
        host="0.0.0.0",
        config={"dashboard": {**basic_config()["dashboard"], "trusted_proxies": ["172.20.0.5"]}},
        peer="172.20.0.5",
    )
    for _ in range(10):
        built.client.post(
            "/auth/password-login",
            json={"username": "admin", "password": "x"},
            headers={"x-forwarded-for": "203.0.113.1"},
        )
    other = built.client.post(
        "/auth/password-login",
        json={"username": "admin", "password": "correct horse"},
        headers={"x-forwarded-for": "203.0.113.2"},
    )
    assert other.status_code == 200
