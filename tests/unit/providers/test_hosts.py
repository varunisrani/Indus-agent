"""P03-04: strict base-URL parsing, exact host matching and API mode from a URL (R01.11, A51)."""

from __future__ import annotations

import random

import pytest

from indus.foundation.errors import InvalidBaseURL
from indus.providers.hosts import (
    api_mode_from_url,
    host_matches,
    is_loopback,
    is_private_network,
    parse_base_url,
    try_parse,
)


@pytest.mark.parametrize(
    ("url", "scheme", "host", "port", "path"),
    [
        ("https://api.openai.com/v1", "https", "api.openai.com", None, "/v1"),
        ("https://API.OpenAI.com./v1/", "https", "api.openai.com", None, "/v1"),
        ("HTTPS://api.openai.com", "https", "api.openai.com", None, ""),
        ("http://127.0.0.1:1234/v1", "http", "127.0.0.1", 1234, "/v1"),
        ("http://[::1]:8080/v1", "http", "::1", 8080, "/v1"),
        ("https://bücher.example/v1", "https", "xn--bcher-kva.example", None, "/v1"),
        ("https://my.host/api/v1?api-version=2025-01-01", "https", "my.host", None, "/api/v1"),
    ],
)
def test_parse_normalises(url: str, scheme: str, host: str, port: int | None, path: str) -> None:
    parsed = parse_base_url(url)
    assert (parsed.scheme, parsed.host, parsed.port, parsed.path) == (scheme, host, port, path)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "api.openai.com/v1",
        "//api.openai.com/v1",
        "ftp://api.openai.com",
        "file:///etc/passwd",
        "https://user@api.openai.com/v1",
        "https://user:pw@api.openai.com/v1",
        "https://api.openai.com /v1",
        " https://api.openai.com/v1",
        "https://api.openai.com/v1\n",
        "https://api.openai.com\\@evil.example/v1",
        "https://evil.example/v1#@api.openai.com",
        "https://api.openai.com:99999/v1",
        "https://api.openai.com:port/v1",
        "https:///v1",
        "https://a..b/v1",
        "https://api%2eopenai.com/v1",
        "http:/api.openai.com",
    ],
)
def test_parse_rejects(url: str) -> None:
    with pytest.raises(InvalidBaseURL):
        parse_base_url(url)
    assert try_parse(url) is None


SPOOFS = [
    "https://api.openai.com.evil.example/v1",
    "https://evilapi.openai.com/v1",
    "https://evil.example/api.openai.com",
    "https://evil.example/api.openai.com/v1",
    "https://api.openai.com@evil.example/v1",
    "https://api.openai.com:443@evil.example/v1",
    "https://evil.example#@api.openai.com",
    "https://evil.example?host=api.openai.com",
    "https://evil.example\\@api.openai.com",
    "https://api-openai.com/v1",
    "https://api.openai.co/v1",
    "https://openai.com/v1",
    "https://xapi.openai.com/v1",
    "https://api.openai.com.example/v1",
    "https://аpi.openai.com/v1",  # Cyrillic "а"
    "https://api.openai.com.:443.evil.example/v1",
    "api.openai.com/v1",
    "https://evil.example/v1?redirect=https://api.openai.com",
    "https://sub.api.openai.com/v1",  # a subdomain is not the host unless explicitly allowed
]


@pytest.mark.parametrize("url", SPOOFS)
def test_spoof_table_never_matches(url: str) -> None:
    assert host_matches(url, "api.openai.com") is False


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "https://API.OPENAI.COM/v1",
        "https://api.openai.com./v1",
        "https://api.openai.com:443/v1",
    ],
)
def test_exact_host_matches(url: str) -> None:
    assert host_matches(url, "api.openai.com")
    assert host_matches(url, "API.OpenAI.com.")


def test_subdomains_only_when_allowed() -> None:
    assert not host_matches("https://eu.api.openai.com/v1", "api.openai.com")
    assert host_matches("https://eu.api.openai.com/v1", "api.openai.com", allow_subdomains=True)
    assert not host_matches(
        "https://evilapi.openai.com/v1", "api.openai.com", allow_subdomains=True
    )
    assert not host_matches("https://openai.com.evil.example", "openai.com", allow_subdomains=True)


def test_bad_inputs_never_match() -> None:
    assert not host_matches("not a url", "api.openai.com")
    assert not host_matches("https://api.openai.com/v1", "")
    assert not host_matches("https://api.openai.com/v1", "bad..domain")


_FRAGMENTS = [
    "api.openai.com",
    "evil.example",
    "openai.com",
    "api",
    "com",
    ".",
    "@",
    "#",
    "?",
    "/",
    ":",
    "443",
    "\\",
    "%2e",
    "xn--",
    "API.OPENAI.COM",
    "-",
    "..",
    "a.api.openai.com",
    "аpi",
]


def test_property_match_implies_host_or_subdomain() -> None:
    """Seeded stand-in for a hypothesis property: a match always means the parsed host is the
    domain or a dot-boundary subdomain of it."""
    rng = random.Random(1303)
    domains = ["api.openai.com", "openai.com", "openrouter.ai"]
    checked = 0
    for _ in range(2000):
        url = rng.choice(["https://", "http://", ""]) + "".join(
            rng.choice(_FRAGMENTS) for _ in range(rng.randint(1, 5))
        )
        domain = rng.choice(domains)
        for allow in (False, True):
            if host_matches(url, domain, allow_subdomains=allow):
                host = parse_base_url(url).host
                assert host == domain or (allow and host.endswith("." + domain))
                checked += 1
    assert checked > 0


@pytest.mark.parametrize(
    ("url", "loopback", "private"),
    [
        ("http://127.0.0.1:1234/v1", True, True),
        ("http://127.5.6.7/v1", True, True),
        ("http://localhost:11434/v1", True, True),
        ("http://LOCALHOST./v1", True, True),
        ("http://[::1]:8080/v1", True, True),
        ("http://[::ffff:127.0.0.1]/v1", True, True),
        ("http://192.168.1.20:8000/v1", False, True),
        ("http://10.0.0.5/v1", False, True),
        ("http://172.16.4.4/v1", False, True),
        ("http://[fd00::1]/v1", False, True),
        ("http://169.254.1.1/v1", False, True),
        ("http://0.0.0.0:8000/v1", False, False),
        ("https://api.openai.com/v1", False, False),
        ("http://localhost.evil.example/v1", False, False),
        ("http://127.0.0.1.evil.example/v1", False, False),
        ("garbage", False, False),
    ],
)
def test_loopback_and_private(url: str, loopback: bool, private: bool) -> None:
    assert is_loopback(url) is loopback
    assert is_private_network(url) is private


@pytest.mark.parametrize(
    ("url", "mode"),
    [
        ("https://api.openai.com/v1", "responses"),
        ("https://eu.api.openai.com/v1", "responses"),
        ("https://api.openai.com.evil.example/v1", "chat_completions"),
        ("https://evil.example/api.openai.com/v1", "chat_completions"),
        ("https://api.anthropic.com", "anthropic_messages"),
        ("https://api.anthropic.com.evil.example", "chat_completions"),
        ("https://api.minimax.io/anthropic", "anthropic_messages"),
        ("https://api.minimax.io/anthropic/", "anthropic_messages"),
        ("https://proxy.example/anthropic/v1", "anthropic_messages"),
        ("https://proxy.example/Anthropic/V1", "anthropic_messages"),
        ("https://proxy.example/anthropic/v1/extra", "chat_completions"),
        ("https://proxy.example/anthropic-ish", "chat_completions"),
        ("https://openrouter.ai/api/v1", "chat_completions"),
        ("http://127.0.0.1:8080/v1", "chat_completions"),
    ],
)
def test_api_mode_from_url(url: str, mode: str) -> None:
    assert api_mode_from_url(url) == mode


def test_api_mode_from_bad_url_raises() -> None:
    with pytest.raises(InvalidBaseURL):
        api_mode_from_url("api.openai.com")
