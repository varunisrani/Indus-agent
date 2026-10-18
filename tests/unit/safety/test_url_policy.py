"""URL policy (SSRF gate), website blocklist and the pinned-address transport (P05-22)."""

from __future__ import annotations

import asyncio
import ipaddress
import random
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.scope import Scope, make_scope
from indus.safety import url_policy
from indus.safety.safe_transport import ConnectBlocked, SafeTransport, safe_client
from indus.safety.url_policy import (
    MAX_DIAL_ADDRESSES,
    Allowed,
    Blocked,
    blocklist_hit,
    check_url,
    classify_address,
    declared_fake_ranges,
    normalize_url,
)
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock
from tests.fakes.resolver import FakeResolver

PUBLIC = "93.184.216.34"


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    url_policy.clear_blocklist_cache()
    yield
    url_policy.clear_blocklist_cache()


def scope_for(
    tmp_path: Path,
    config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    clock: FakeClock | None = None,
) -> Scope:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    scope = make_scope(
        home, env=env or {}, clock=clock or FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    )
    return scope.with_config(DictConfig(config or {}))


def ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    return ipaddress.ip_address(text)


# -- address classes ---------------------------------------------------------------------------

_BLOCKED_NETS = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "100.64.0.0/10",
    "198.18.0.0/15",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "0.0.0.0/8",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
    "fec0::/10",
]
_PUBLIC_SAMPLES = ["8.8.8.8", "1.1.1.1", "100.63.255.255", "100.128.0.1", "2606:4700::1111"]


def _sample(net: str, rng: random.Random) -> str:
    network = ipaddress.ip_network(net)
    offset = rng.randrange(1, network.num_addresses - 1) if network.num_addresses > 2 else 0
    return str(network.network_address + offset)


def test_address_classes() -> None:
    rng = random.Random(2205)
    for net in _BLOCKED_NETS:
        for _ in range(20):
            address = _sample(net, rng)
            assert classify_address(ip(address)) == "private", address
            assert classify_address(ip(address), allow_private=True) is None, address
    for address in ["::1", "::", "0.0.0.0", "255.255.255.255"]:
        assert classify_address(ip(address)) == "private", address
    for address in _PUBLIC_SAMPLES:
        assert classify_address(ip(address)) is None, address


def test_ipv6_wrappers_are_classified_by_the_ipv4_they_carry() -> None:
    for wrapped in ["::ffff:10.1.2.3", "::ffff:0:10.1.2.3", "64:ff9b::10.1.2.3", "::10.1.2.3"]:
        assert classify_address(ip(wrapped)) == "private", wrapped
    for wrapped in ["::ffff:169.254.169.254", "::ffff:0:169.254.169.254", "64:ff9b::a9fe:a9fe"]:
        assert classify_address(ip(wrapped), allow_private=True) == "metadata", wrapped
    assert classify_address(ip("::ffff:8.8.8.8")) is None


def test_metadata_floor_survives_allow_private() -> None:
    rng = random.Random(7)
    floor = ["169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254"]
    floor += [_sample("169.254.0.0/16", rng) for _ in range(20)]
    for address in floor:
        assert classify_address(ip(address), allow_private=True) == "metadata", address
        assert classify_address(ip(address)) == "metadata", address


def test_fake_ip_declarations(tmp_path: Path) -> None:
    declared = ["198.18.0.0/15", "not a net", "10.0.0.0/8", "0.0.0.0/0", "::/0", "fc00::/7"]
    scope = scope_for(tmp_path, {"security": {"fake_ip_ranges": declared}})
    ranges = declared_fake_ranges(scope)
    assert [str(r) for r in ranges] == ["198.18.0.0/15"]
    assert classify_address(ip("198.18.3.4"), fake_ranges=ranges) is None
    assert classify_address(ip("::ffff:0:198.18.3.4"), fake_ranges=ranges) is None
    assert classify_address(ip("192.168.1.1"), fake_ranges=ranges) == "private"
    assert classify_address(ip("169.254.169.254"), fake_ranges=ranges) == "metadata"
    resolver = FakeResolver({"example.com": ["198.18.0.9"]})
    assert check_url("https://example.com/", scope, resolver=resolver).allowed
    plain = scope_for(tmp_path / "other")
    assert check_url("https://example.com/", plain, resolver=resolver).allowed is False


# -- whole URLs --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("", "invalid"),
        ("   ", "invalid"),
        ("http://", "invalid"),
        ("example.com/path", "scheme"),
        ("ftp://example.com/file", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("http://example.com:99999/", "invalid"),
    ],
)
def test_unusable_urls_are_refused(tmp_path: Path, url: str, kind: str) -> None:
    verdict = check_url(url, scope_for(tmp_path), resolver=FakeResolver({"example.com": [PUBLIC]}))
    assert isinstance(verdict, Blocked)
    assert verdict.kind == kind


def test_public_host_is_allowed_with_its_addresses(tmp_path: Path) -> None:
    resolver = FakeResolver({"example.com": [PUBLIC, "2606:2800:220:1::1"]})
    verdict = check_url("https://Example.COM./page", scope_for(tmp_path), resolver=resolver)
    assert isinstance(verdict, Allowed)
    assert verdict.host == "example.com"
    assert verdict.addresses == (PUBLIC, "2606:2800:220:1::1")


@pytest.mark.parametrize(
    ("url", "answers", "kind"),
    [
        ("http://localhost:8080/", ["127.0.0.1"], "private"),
        ("http://intranet.example/", ["10.0.0.7"], "private"),
        ("http://[::1]:8080/", None, "private"),
        ("http://127.1/", ["127.0.0.1"], "private"),  # shorthand: the resolver answers
        ("http://169.254.169.254/latest/meta-data/", None, "metadata"),
        ("http://[::ffff:169.254.169.254]/", None, "metadata"),
        ("http://[fe80::1%25eth0]/", None, "private"),
        ("http://metadata.google.internal/", None, "metadata"),
        ("http://rebind.example/", ["8.8.8.8", "192.168.0.10"], "private"),
    ],
)
def test_internal_targets_are_refused(
    tmp_path: Path, url: str, answers: list[str] | None, kind: str
) -> None:
    host = httpx.URL(url).host
    resolver = FakeResolver({host: answers} if answers else {})
    verdict = check_url(url, scope_for(tmp_path), resolver=resolver)
    assert isinstance(verdict, Blocked), url
    assert verdict.kind == kind


def test_literal_decimal_forms_never_reach_the_resolver(tmp_path: Path) -> None:
    resolver = FakeResolver()
    assert check_url("http://10.0.0.1/", scope_for(tmp_path), resolver=resolver).allowed is False
    assert resolver.lookups == []


def test_allow_private_opens_lan_but_not_metadata(tmp_path: Path) -> None:
    scope = scope_for(tmp_path, {"security": {"allow_private_urls": True}})
    resolver = FakeResolver({"nas.lan": ["192.168.1.20"], "evil.example": ["169.254.169.254"]})
    assert check_url("http://nas.lan/", scope, resolver=resolver).allowed
    assert check_url("http://127.0.0.1:11434/", scope, resolver=resolver).allowed
    for url in ["http://evil.example/", "http://169.254.169.254/", "http://metadata.goog/"]:
        verdict = check_url(url, scope, resolver=resolver)
        assert isinstance(verdict, Blocked) and verdict.kind == "metadata", url


def test_allow_private_reads_text_switches(tmp_path: Path) -> None:
    resolver = FakeResolver({"nas.lan": ["192.168.1.20"]})
    on = scope_for(tmp_path, {"security": {"allow_private_urls": "yes"}})
    off = scope_for(tmp_path / "b", {"security": {"allow_private_urls": "false"}})
    assert check_url("http://nas.lan/", on, resolver=resolver).allowed
    assert check_url("http://nas.lan/", off, resolver=resolver).allowed is False


def test_dns_failure_and_proxy(tmp_path: Path) -> None:
    resolver = FakeResolver()
    plain = scope_for(tmp_path)
    verdict = check_url("https://nowhere.example/", plain, resolver=resolver)
    assert isinstance(verdict, Blocked) and verdict.kind == "dns"
    proxied = scope_for(tmp_path / "p", env={"HTTPS_PROXY": "http://proxy.local:3128"})
    deferred = check_url("https://nowhere.example/", proxied, resolver=resolver)
    assert isinstance(deferred, Allowed) and deferred.via_proxy and deferred.addresses == ()
    for url in [
        "https://metadata.google.internal/",
        "https://10.0.0.1/",
        "http://169.254.169.254/",
    ]:
        assert check_url(url, proxied, resolver=resolver).allowed is False, url
    # the proxy for plain http comes from HTTP_PROXY / ALL_PROXY, not HTTPS_PROXY
    assert check_url("http://nowhere.example/", proxied, resolver=resolver).allowed is False
    every = scope_for(tmp_path / "a", env={"ALL_PROXY": "socks5://127.0.0.1:1080"})
    assert check_url("http://nowhere.example/", every, resolver=resolver).allowed


def test_unreadable_answer_fails_closed(tmp_path: Path) -> None:
    resolver = FakeResolver({"odd.example": [PUBLIC, "not-an-address"]})
    verdict = check_url("https://odd.example/", scope_for(tmp_path), resolver=resolver)
    assert isinstance(verdict, Blocked) and verdict.kind == "invalid"


def test_resolver_crash_fails_closed(tmp_path: Path) -> None:
    def boom(host: str, port: int | None) -> list[str]:
        raise RuntimeError("resolver bug")

    verdict = check_url("https://example.com/", scope_for(tmp_path), resolver=boom)
    assert isinstance(verdict, Blocked) and verdict.kind == "invalid"


def test_every_answer_checked_but_eight_kept(tmp_path: Path) -> None:
    many = [f"8.8.{i}.{i}" for i in range(12)]
    resolver = FakeResolver({"big.example": many})
    verdict = check_url("https://big.example/", scope_for(tmp_path), resolver=resolver)
    assert isinstance(verdict, Allowed)
    assert verdict.addresses == tuple(many[:MAX_DIAL_ADDRESSES])
    resolver.set("big.example", [*many, "10.9.9.9"])
    assert (
        check_url("https://big.example/", scope_for(tmp_path), resolver=resolver).allowed is False
    )


def _token(*parts: str) -> str:
    return "".join(parts)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/cb?access_token=abc123",
        "https://example.com/cb?API-KEY=xyz",
        "https://example.com/dl?X-Amz-Signature=deadbeef",
        "https://example.com/x?q=" + _token("sk", "-", "a" * 30),
        "https://example.com/" + _token("gh", "p_", "B" * 36) + "/file",
        "https://example.com/page#access_token=" + "c" * 20,
        "https://user:hunter2hunter2@example.com/",
        "https://example.com/x?token=%61%62%63",
    ],
)
def test_urls_carrying_credentials_are_refused(tmp_path: Path, url: str) -> None:
    resolver = FakeResolver({"example.com": [PUBLIC]})
    verdict = check_url(url, scope_for(tmp_path), resolver=resolver)
    assert isinstance(verdict, Blocked) and verdict.kind == "secret", url
    assert resolver.lookups == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/search?q=token+rings&key=value&code=42&sig=a",
        "https://example.com/x?token=",
        "https://example.com/docs/api_key-setup",
    ],
)
def test_ordinary_query_words_pass(tmp_path: Path, url: str) -> None:
    resolver = FakeResolver({"example.com": [PUBLIC]})
    assert check_url(url, scope_for(tmp_path), resolver=resolver).allowed, url


# -- normalising -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://wetter.example/Zürich", "https://wetter.example/Z%C3%BCrich"),
        ("https://wetter.example/Z%C3%BCrich", "https://wetter.example/Z%C3%BCrich"),
        ("https://bücher.example/a b", "https://xn--bcher-kva.example/a%20b"),
        ("  https://example.com/x  ", "https://example.com/x"),
        ("https://  example.com/x", "https://example.com/x"),
        ("https://Example.com:8443/p?q=ä#frag", "https://Example.com:8443/p?q=%C3%A4#frag"),
        ("https://u@bücher.example:81/", "https://u@xn--bcher-kva.example:81/"),
        ("https://[::1]:8080/x", "https://[::1]:8080/x"),
        ("ftp://bücher.example/x", "ftp://bücher.example/x"),
        (
            "https://example.com/r?next=https:// other.example",
            "https://example.com/r?next=https://%20other.example",
        ),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


# -- website blocklist -------------------------------------------------------------------------


def _blocklist(tmp_path: Path, clock: FakeClock | None = None, **section: Any) -> Scope:
    body = {"enabled": True, **section}
    return scope_for(tmp_path, {"security": {"website_blocklist": body}}, clock=clock)


def test_blocklist_rules(tmp_path: Path) -> None:
    scope = _blocklist(
        tmp_path,
        domains=[
            "bad.example",
            "https://www.tracker.example/path",
            "*.ads.example",
            "  # a comment",
            "",
            "Mixed.Case.Example.",
            "ports.example:8080",
        ],
    )
    blocked = [
        "https://bad.example/",
        "https://sub.bad.example/x",
        "http://tracker.example/",
        "http://www.tracker.example/",
        "https://one.ads.example/",
        "https://two.levels.ads.example/",
        "https://mixed.case.example/",
        "bad.example/no-scheme",
        "https://ports.example/",
    ]
    for url in blocked:
        hit = blocklist_hit(url, scope)
        assert hit is not None, url
    for url in ["https://notbad.example/", "https://bad.example.org/", "https://ads.example/"]:
        assert blocklist_hit(url, scope) is None, url


def test_blocklist_reaches_check_url(tmp_path: Path) -> None:
    scope = _blocklist(tmp_path, domains=["bad.example"])
    resolver = FakeResolver({"bad.example": [PUBLIC]})
    verdict = check_url("https://bad.example/", scope, resolver=resolver)
    assert isinstance(verdict, Blocked) and verdict.kind == "blocklist"
    assert "bad.example" in verdict.reason and "config" in verdict.reason


def test_blocklist_shared_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "lists").mkdir(parents=True)
    (home / "lists" / "team.txt").write_text("﻿# team list\nshared.example\n\n*.cdn.example\n")
    absolute = tmp_path / "abs.txt"
    absolute.write_text("abs.example\n")
    scope = _blocklist(
        tmp_path, shared_files=["lists/team.txt", str(absolute), "missing.txt", "", 7]
    )
    hit = blocklist_hit("https://shared.example/", scope)
    assert hit is not None and hit.source.endswith("team.txt")
    assert blocklist_hit("https://a.cdn.example/", scope) is not None
    assert blocklist_hit("https://abs.example/", scope) is not None
    assert blocklist_hit("https://fine.example/", scope) is None


def test_blocklist_off_and_malformed_fail_open(tmp_path: Path) -> None:
    off = scope_for(tmp_path, {"security": {"website_blocklist": {"domains": ["bad.example"]}}})
    assert blocklist_hit("https://bad.example/", off) is None
    for broken in [
        "yes",
        {"enabled": "maybe", "domains": ["bad.example"]},
        {"enabled": True, "domains": "bad.example"},
    ]:
        url_policy.clear_blocklist_cache()
        scope = scope_for(tmp_path, {"security": {"website_blocklist": broken}})
        assert blocklist_hit("https://bad.example/", scope) is None
    assert blocklist_hit("https://bad.example/", None) is None


def test_blocklist_is_cached_for_thirty_seconds(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    rules = home / "rules.txt"
    rules.write_text("first.example\n")
    scope = _blocklist(tmp_path, clock=clock, shared_files=["rules.txt"])
    assert blocklist_hit("https://first.example/", scope) is not None
    rules.write_text("second.example\n")
    clock.advance(29)
    assert blocklist_hit("https://second.example/", scope) is None
    clock.advance(2)
    assert blocklist_hit("https://second.example/", scope) is not None
    assert blocklist_hit("https://first.example/", scope) is None


def test_blocklist_cache_is_per_profile(tmp_path: Path) -> None:
    one = _blocklist(tmp_path / "one", domains=["bad.example"])
    two = scope_for(tmp_path / "two")
    assert blocklist_hit("https://bad.example/", one) is not None
    assert blocklist_hit("https://bad.example/", two) is None


# -- the transport -----------------------------------------------------------------------------


class Recorder:
    """Inner transport factory: records what the safe transport hands down."""

    def __init__(self, respond: Any = None) -> None:
        self.requests: list[httpx.Request] = []
        self.proxies: list[str | None] = []
        self._respond = respond or (lambda request: httpx.Response(200, text="ok"))

    def __call__(self, proxy: str | None) -> httpx.AsyncBaseTransport:
        self.proxies.append(proxy)

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._respond(request)

        return httpx.MockTransport(handler)


def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    async def go() -> httpx.Response:
        async with client:
            return await client.get(url)

    return asyncio.run(go())


def test_request_is_pinned_and_keeps_host_and_sni(tmp_path: Path) -> None:
    recorder = Recorder()
    resolver = FakeResolver({"example.com": [PUBLIC]})
    client = safe_client(scope_for(tmp_path), resolver=resolver, inner_factory=recorder)
    response = _get(client, "https://example.com/page?q=1")
    assert response.text == "ok"
    (sent,) = recorder.requests
    assert sent.url.host == PUBLIC
    assert sent.url.path == "/page" and sent.url.query == b"q=1"
    assert sent.headers["host"] == "example.com"
    assert sent.extensions["sni_hostname"] == "example.com"
    assert str(response.request.url) == "https://example.com/page?q=1"


def test_rebinding_is_caught_at_connect(tmp_path: Path) -> None:
    resolver = FakeResolver({"rebind.example": [[PUBLIC], ["10.0.0.5"]]})
    scope = scope_for(tmp_path)
    assert check_url("http://rebind.example/", scope, resolver=resolver).allowed
    recorder = Recorder()
    client = safe_client(scope, resolver=resolver, inner_factory=recorder)
    with pytest.raises(ConnectBlocked) as caught:
        _get(client, "http://rebind.example/")
    assert caught.value.verdict.kind == "private"
    assert recorder.requests == []


def test_redirect_to_metadata_is_blocked(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})

    recorder = Recorder(respond)
    resolver = FakeResolver({"example.com": [PUBLIC]})
    client = safe_client(scope_for(tmp_path), resolver=resolver, inner_factory=recorder)
    with pytest.raises(ConnectBlocked) as caught:
        _get(client, "http://example.com/go")
    assert caught.value.verdict.kind == "metadata"
    assert len(recorder.requests) == 1


def test_redirect_to_a_named_internal_host_is_blocked(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return httpx.Response(301, headers={"location": "https://admin.internal/"})
        return httpx.Response(200, text="secret page")

    resolver = FakeResolver({"example.com": [PUBLIC], "admin.internal": ["192.168.0.1"]})
    client = safe_client(scope_for(tmp_path), resolver=resolver, inner_factory=Recorder(respond))
    with pytest.raises(ConnectBlocked):
        _get(client, "https://example.com/")


def test_next_address_tried_after_a_connect_error(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "8.8.8.1":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, text="second")

    recorder = Recorder(respond)
    resolver = FakeResolver({"multi.example": ["8.8.8.1", "8.8.8.2"]})
    client = safe_client(scope_for(tmp_path), resolver=resolver, inner_factory=recorder)
    assert _get(client, "http://multi.example/").text == "second"
    assert [r.url.host for r in recorder.requests] == ["8.8.8.1", "8.8.8.2"]


def test_proxy_gets_the_request_unchanged(tmp_path: Path) -> None:
    recorder = Recorder()
    scope = scope_for(tmp_path, env={"HTTPS_PROXY": "http://proxy.local:3128"})
    client = safe_client(scope, resolver=FakeResolver(), inner_factory=recorder)
    _get(client, "https://unresolvable.example/x")
    assert recorder.proxies == ["http://proxy.local:3128"]
    assert recorder.requests[0].url.host == "unresolvable.example"


def test_one_inner_transport_per_host_and_close(tmp_path: Path) -> None:
    recorder = Recorder()
    resolver = FakeResolver({"a.example": [PUBLIC], "b.example": [PUBLIC]})
    transport = SafeTransport(scope_for(tmp_path), resolver=resolver, inner_factory=recorder)

    async def go() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://a.example/1")
            await client.get("https://a.example/2")
            await client.get("https://b.example/")

    asyncio.run(go())
    assert recorder.proxies == [None, None]
