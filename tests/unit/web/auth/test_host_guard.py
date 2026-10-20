"""P18-02: the Host guard against DNS rebinding (exact hosts only, no wildcard or suffix)."""

from __future__ import annotations

import pytest

from indus.web.auth.host_guard import HostRules, host_of_authority
from tests.unit.web.conftest import Builder, basic_config


@pytest.mark.parametrize(
    ("value", "host"),
    [
        ("127.0.0.1:9119", "127.0.0.1"),
        ("LOCALHOST", "localhost"),
        ("[::1]:9119", "::1"),
        ("[::1]", "::1"),
        ("dash.example.test", "dash.example.test"),
        ("::1", ""),
        ("[::1", ""),
        ("[127.0.0.1]", ""),
        ("[::1]x", ""),
        ("host:port", ""),
        ("host:123456", ""),
        ("http://host", ""),
        ("user@host", ""),
        ("host/path", ""),
        ("host?x", ""),
        ('ho"st', ""),
        ("ho st", ""),
        ("", ""),
    ],
)
def test_host_of_authority(value: str, host: str) -> None:
    assert host_of_authority(value) == host


LOOPBACK = HostRules("127.0.0.1")


@pytest.mark.parametrize(
    ("rules", "header", "accepted"),
    [
        (LOOPBACK, "127.0.0.1:9119", True),
        (LOOPBACK, "localhost:9119", True),
        (LOOPBACK, "[::1]:9119", True),
        (LOOPBACK, "evil.com", False),
        (LOOPBACK, "evil.com:9119", False),
        (LOOPBACK, "127.0.0.1.evil.com", False),
        (LOOPBACK, "", False),
        (HostRules("192.168.1.5"), "192.168.1.5:9119", True),
        (HostRules("192.168.1.5"), "localhost", False),
        (HostRules("192.168.1.5"), "192.168.1.50", False),
        (HostRules("0.0.0.0"), "anything.example:80", True),
        (HostRules("::"), "anything.example", True),
        (HostRules("0.0.0.0"), "bad host", False),
        (HostRules("127.0.0.1", frozenset({"dash.example.com"})), "dash.example.com", True),
        (HostRules("127.0.0.1", frozenset({"dash.example.com"})), "dash.example.com:443", True),
        (
            HostRules("127.0.0.1", frozenset({"dash.example.com"})),
            "dash.example.com.evil.test",
            False,
        ),
        (HostRules("127.0.0.1", frozenset({"dash.example.com"})), "x.dash.example.com", False),
    ],
)
def test_accepted_hosts(rules: HostRules, header: str, accepted: bool) -> None:
    assert rules.accepts(header) is accepted


def test_origin_rules() -> None:
    assert LOOPBACK.accepts_origin("http://127.0.0.1:9119")
    assert LOOPBACK.accepts_origin("http://localhost:5173")
    assert not LOOPBACK.accepts_origin("http://evil.example")
    assert not LOOPBACK.accepts_origin("http://user@127.0.0.1")
    # a non-web origin (an app shell, a file page) only on a loopback bind
    assert LOOPBACK.accepts_origin("null")
    assert not HostRules("0.0.0.0").accepts_origin("null")
    assert HostRules("0.0.0.0").accepts_origin("https://any.example")
    assert LOOPBACK.check_upgrade({"host": "127.0.0.1:9119"}) is None
    assert LOOPBACK.check_upgrade({"host": "evil.com"}) == "host not accepted"
    reason = LOOPBACK.check_upgrade({"host": "127.0.0.1", "origin": "http://evil"})
    assert reason is not None and reason.startswith("origin not accepted")


def test_middleware_answers_400_for_a_rebound_name(build: Builder) -> None:
    built = build()
    for host in ("evil.com", "127.0.0.1.evil.com:9119", "localhost.evil.com"):
        response = built.client.get("/api/health", headers={"host": host})
        assert response.status_code == 400, host
        assert response.headers["content-type"] == "application/json"
    assert built.client.get("/api/health", headers={"host": "localhost:9119"}).status_code == 200


def test_public_host_is_accepted_exactly(build: Builder) -> None:
    dashboard = {**basic_config()["dashboard"], "public_url": "https://dash.example.com/indus"}
    built = build(config={"dashboard": dashboard})
    assert built.client.get("/api/health", headers={"host": "dash.example.com"}).status_code == 200
    bad = built.client.get("/api/health", headers={"host": "dash.example.com.evil.test"})
    assert bad.status_code == 400
