"""Google credentials for the Google Chat adapter (P16-13): our token source, Google's tokens."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from indus.gateway.adapters.google_chat.auth import (
    CHAT_ISSUER,
    CHAT_JWKS_URL,
    CHAT_SCOPES,
    GOOGLE_JWKS_URL,
    CredentialError,
    GoogleTokenVerifier,
    JwkCache,
    ServiceAccount,
    ServiceAccountTokens,
    TokenError,
    TokenRejected,
    b64url_decode,
    b64url_encode,
    http_key_fetcher,
)
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FakeChatPlatform
from tests.fakes.platform.google_chat_wire import (
    ENDPOINT_URL,
    GOOGLE_KID,
    PROJECT_NUMBER,
    SA_EMAIL,
    TOKEN_URI,
    GoogleChatWire,
    jwk_of,
    rsa_key,
    service_account_json,
    sign_jwt,
    unsigned_jwt,
)


def run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


def _wire(flavour: Any = "project_number", audience: str = PROJECT_NUMBER) -> GoogleChatWire:
    platform = FakeChatPlatform("google_chat", clock=_clock())
    return GoogleChatWire(platform, audience=audience, flavour=flavour)


def _verifier(wire: GoogleChatWire, kind: Any = "project_number", **kw: Any) -> GoogleTokenVerifier:
    return GoogleTokenVerifier(
        audience=kw.pop("audience", wire.audience),
        kind=kind,
        keys=JwkCache(wire.fetch_keys, wire.platform.clock),
        clock=wire.platform.clock,
        **kw,
    )


def _reason(verifier: GoogleTokenVerifier, header: str | None) -> str:
    async def main() -> str:
        try:
            await verifier.verify(header)
        except TokenRejected as exc:
            return exc.reason
        return "accepted"

    return run(main())


# -- the bearer token on callbacks ----------------------------------------------------------------


def test_good_tokens_pass_in_both_flavours() -> None:
    wire = _wire()
    claims = run(_verifier(wire).verify(f"Bearer {wire.id_token()}"))
    assert claims["iss"] == CHAT_ISSUER and claims["aud"] == PROJECT_NUMBER
    url_wire = _wire("url", ENDPOINT_URL)
    verifier = _verifier(url_wire, "url")
    assert verifier.jwks_url == GOOGLE_JWKS_URL
    assert _reason(verifier, f"Bearer {url_wire.id_token()}") == "accepted"
    assert _verifier(wire).jwks_url == CHAT_JWKS_URL


def test_the_audience_kind_follows_the_audience() -> None:
    assert GoogleTokenVerifier.kind_for("123456789012") == "project_number"
    assert GoogleTokenVerifier.kind_for("https://bot.example.test/x") == "url"
    assert GoogleTokenVerifier.kind_for("123", "url") == "url"
    assert GoogleTokenVerifier.kind_for("https://x", "project-number") == "project_number"


def test_forged_and_unsigned_tokens_are_refused() -> None:
    wire = _wire()
    verifier = _verifier(wire)
    now = int(wire.platform.clock.now().timestamp())
    claims = {"iss": CHAT_ISSUER, "aud": PROJECT_NUMBER, "iat": now, "exp": now + 600}
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cases = {
        "forged signature": (sign_jwt(other, {"alg": "RS256", "kid": GOOGLE_KID}, claims)),
        "alg none": unsigned_jwt({"alg": "none", "kid": GOOGLE_KID}, claims, b"x"),
        "alg none, no signature": unsigned_jwt({"alg": "none", "kid": GOOGLE_KID}, claims),
        "alg none, any case": unsigned_jwt({"alg": "None", "kid": GOOGLE_KID}, claims, b"x"),
        "HMAC": unsigned_jwt({"alg": "HS256", "kid": GOOGLE_KID}, claims, b"x" * 32),
        "no alg": unsigned_jwt({"kid": GOOGLE_KID}, claims, b"x" * 256),
        "unknown kid": wire.id_token(header={"kid": "nope"}),
        "no kid": sign_jwt(wire.google_key, {"alg": "RS256"}, claims),
        "tampered claims": _tamper(wire.id_token()),
    }
    expected = {
        "forged signature": "bad_signature",
        "alg none": "bad_algorithm",
        "alg none, no signature": "malformed_token",
        "alg none, any case": "bad_algorithm",
        "HMAC": "bad_algorithm",
        "no alg": "bad_algorithm",
        "unknown kid": "unknown_kid",
        "no kid": "missing_kid",
        "tampered claims": "bad_signature",
    }
    for name, token in cases.items():
        assert _reason(verifier, f"Bearer {token}") == expected[name], name


def _tamper(token: str) -> str:
    head, body, sig = token.split(".")
    claims = json.loads(b64url_decode(body))
    claims["aud"] = "someone-else"
    return ".".join([head, b64url_encode(json.dumps(claims).encode()), sig])


def test_claims_are_checked() -> None:
    wire = _wire()
    verifier = _verifier(wire)
    now = int(wire.platform.clock.now().timestamp())
    assert _reason(verifier, f"Bearer {wire.id_token(iss='accounts.google.com')}") == (
        "wrong_issuer"
    )
    assert _reason(verifier, f"Bearer {wire.id_token(aud='999')}") == "wrong_audience"
    listed = wire.id_token(aud=["other", PROJECT_NUMBER])
    assert _reason(verifier, f"Bearer {listed}") == "accepted"
    assert _reason(verifier, f"Bearer {wire.id_token(exp=now - 61)}") == "expired"
    assert _reason(verifier, f"Bearer {wire.id_token(exp=now - 30)}") == "accepted"  # skew
    assert _reason(verifier, f"Bearer {wire.id_token(exp='soon')}") == "expired"
    assert _reason(verifier, f"Bearer {wire.id_token(iat=now + 120)}") == "issued_in_future"
    assert _reason(verifier, f"Bearer {wire.id_token(nbf=now + 120)}") == "not_yet_valid"


def test_the_url_flavour_needs_the_verified_chat_identity() -> None:
    wire = _wire("url", ENDPOINT_URL)
    verifier = _verifier(wire, "url")
    assert _reason(verifier, f"Bearer {wire.id_token(email='mallory@example.org')}") == (
        "unexpected_identity"
    )
    assert _reason(verifier, f"Bearer {wire.id_token(email_verified=False)}") == (
        "unexpected_identity"
    )
    assert _reason(verifier, f"Bearer {wire.id_token(email=None)}") == "unexpected_identity"
    # A project-number token is not a valid ID token for the URL audience.
    assert _reason(verifier, f"Bearer {wire.id_token(iss=CHAT_ISSUER)}") == "wrong_issuer"
    custom = _verifier(wire, "url", emails=("Relay@Example.org",))
    token = wire.id_token(email="relay@example.org")
    assert _reason(custom, f"Bearer {token}") == "accepted"


def test_malformed_headers_are_refused() -> None:
    wire = _wire()
    verifier = _verifier(wire)
    for header, reason in [
        (None, "missing_bearer"),
        ("", "missing_bearer"),
        (f"Basic {wire.id_token()}", "missing_bearer"),
        ("Bearer ", "missing_bearer"),
        ("Bearer a.b", "malformed_token"),
        ("Bearer a.b.c.d", "malformed_token"),
        ("Bearer @@.@@.@@", "malformed_token"),
        ("Bearer " + "a" * 20_000, "oversized_token"),
        ("Bearer " + b64url_encode(b"[1]") + ".e30.AA", "malformed_token"),
    ]:
        assert _reason(verifier, header) == reason, header
    assert _reason(verifier, f"bearer {wire.id_token()}") == "accepted"


def test_keys_are_cached_for_300_seconds_and_unknown_kids_refetch_once_a_minute() -> None:
    wire = _wire()
    clock = wire.platform.clock
    verifier = _verifier(wire)
    keys = verifier.keys
    assert _reason(verifier, f"Bearer {wire.id_token()}") == "accepted"
    assert _reason(verifier, f"Bearer {wire.id_token()}") == "accepted"
    assert keys.fetches == 1
    clock.advance(301)
    assert _reason(verifier, f"Bearer {wire.id_token()}") == "accepted"
    assert keys.fetches == 2
    stranger = f"Bearer {wire.id_token(header={'kid': 'rotated'})}"
    assert _reason(verifier, stranger) == "unknown_kid"
    assert keys.fetches == 3  # one forced refetch
    assert _reason(verifier, stranger) == "unknown_kid"
    assert keys.fetches == 3  # not again inside the minute
    clock.advance(61)
    assert _reason(verifier, stranger) == "unknown_kid"
    assert keys.fetches == 4


def test_a_failing_key_fetch_fails_closed() -> None:
    clock = _clock()
    wire = _wire()

    async def broken(url: str) -> Mapping[str, Any]:
        raise httpx.ConnectError("down")

    verifier = GoogleTokenVerifier(
        audience=PROJECT_NUMBER, kind="project_number", keys=JwkCache(broken, clock), clock=clock
    )
    assert _reason(verifier, f"Bearer {wire.id_token()}") == "unknown_kid"


def test_weak_or_odd_keys_are_never_used() -> None:
    clock = _clock()
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)

    async def fetch(url: str) -> Mapping[str, Any]:
        good = jwk_of(rsa_key("google"), "good")
        return {
            "keys": [
                jwk_of(weak, "weak"),
                {**good, "kid": "hmac", "kty": "oct"},
                {**good, "kid": "enc", "use": "enc"},
                {**good, "kid": "ps", "alg": "PS256"},
                {**good, "kid": "broken", "n": "@@"},
                "junk",
                good,
            ]
        }

    cache = JwkCache(fetch, clock)
    assert run(cache.key(CHAT_JWKS_URL, "good")) is not None
    for kid in ("weak", "hmac", "enc", "ps", "broken"):
        assert run(cache.key(CHAT_JWKS_URL, kid)) is None, kid


def test_the_http_fetcher_only_talks_to_googleapis() -> None:
    wire = _wire()

    async def main() -> None:
        async with httpx.AsyncClient(transport=wire.transport()) as http:
            fetch = http_key_fetcher(http)
            document = await fetch(GOOGLE_JWKS_URL)
            assert document["keys"][0]["kid"] == GOOGLE_KID
            assert (await fetch(CHAT_JWKS_URL))["keys"]
            for url in ("http://www.googleapis.com/oauth2/v3/certs", "https://evil.example/x"):
                with pytest.raises(ValueError):
                    await fetch(url)
        assert set(wire.hosts) == {"www.googleapis.com"}

    run(main())


# -- our service account --------------------------------------------------------------------------


def test_the_key_json_is_checked_and_never_echoed() -> None:
    good = service_account_json()
    account = ServiceAccount.parse(good)
    assert account.client_email == SA_EMAIL and account.token_uri == TOKEN_URI
    assert "PRIVATE" not in repr(account) and repr(account) == f"<ServiceAccount {SA_EMAIL}>"
    data = json.loads(good)
    bad = {
        "": "not set",
        "/home/me/key.json": "not a path",
        "{nope": "not valid JSON",
        "[1]": "not valid JSON",
        json.dumps({**data, "type": "authorized_user"}): "service-account",
        json.dumps({**data, "client_email": ""}): "client_email",
        json.dumps({**data, "private_key": "x"}): "private_key",
        json.dumps({**data, "token_uri": "https://evil.example/token"}): "token_uri",
        json.dumps({**data, "token_uri": "http://oauth2.googleapis.com/token"}): "token_uri",
    }
    for raw, words in bad.items():
        with pytest.raises(CredentialError) as caught:
            ServiceAccount.parse(raw)
        text = str(caught.value)
        assert "PRIVATE KEY" not in text and data["private_key"][40:80] not in text
        if raw != "[1]":
            assert words.split()[-1] in text, (raw[:20], text)


def _tokens(wire: GoogleChatWire, http: httpx.AsyncClient) -> ServiceAccountTokens:
    account = ServiceAccount.parse(wire.account_json)
    return ServiceAccountTokens(account, http, clock=wire.platform.clock)


def test_the_assertion_is_signed_and_the_token_cached_until_a_minute_before_expiry() -> None:
    wire = _wire()
    clock = wire.platform.clock

    async def main() -> None:
        async with httpx.AsyncClient(transport=wire.transport()) as http:
            tokens = _tokens(wire, http)
            assert await tokens.token() == wire.access_token
            assert await tokens.token() == wire.access_token
            assert wire.tokens_issued == 1
            claims = wire.assertion_claims[0]
            assert claims["iss"] == SA_EMAIL and claims["aud"] == TOKEN_URI
            assert claims["scope"].split() == list(CHAT_SCOPES)
            assert claims["exp"] - claims["iat"] == 3600
            clock.advance(3600 - 61)
            await tokens.token()
            assert wire.tokens_issued == 1
            clock.advance(2)
            await tokens.token()
            assert wire.tokens_issued == 2
            tokens.invalidate()
            await tokens.token()
            assert wire.tokens_issued == 3
            assert "PRIVATE" not in repr(tokens) and wire.access_token not in repr(tokens)

    run(main())


def test_concurrent_callers_share_one_token_fetch() -> None:
    wire = _wire()

    async def main() -> None:
        async with httpx.AsyncClient(transport=wire.transport()) as http:
            tokens = _tokens(wire, http)
            got = await asyncio.gather(*(tokens.token() for _ in range(5)))
            assert set(got) == {wire.access_token} and wire.tokens_issued == 1

    run(main())


def test_token_endpoint_failures_are_classified() -> None:
    def answer(status: int, body: Any = None) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=body if body is not None else {})

        return httpx.MockTransport(handle)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    wire = _wire()
    cases: list[tuple[httpx.MockTransport, bool, str]] = [
        (answer(400, {"error": "invalid_grant"}), False, "invalid_grant"),
        (answer(401, {"error": "unauthorized_client"}), False, "unauthorized_client"),
        (answer(503), True, "503"),
        (answer(429), True, "429"),
        (answer(200, {"token_type": "Bearer"}), True, "without a token"),
        (httpx.MockTransport(boom), True, "ConnectError"),
    ]

    async def main() -> None:
        for transport, retryable, words in cases:
            async with httpx.AsyncClient(transport=transport) as http:
                tokens = _tokens(wire, http)
                with pytest.raises(TokenError) as caught:
                    await tokens.token()
                assert caught.value.retryable is retryable
                assert words in str(caught.value)
                assert "PRIVATE" not in str(caught.value)

    run(main())


def _armoured(body: str) -> str:
    """PEM armour built at run time, so no key-shaped literal sits in the tree."""
    label = " ".join(("PRIVATE", "KEY"))
    dashes = "-" * 5
    return f"{dashes}BEGIN {label}{dashes}\n{body}\n{dashes}END {label}{dashes}\n"


def _throwaway_ec_key() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


@pytest.mark.parametrize("make_key", [lambda: _armoured("AAAA"), _throwaway_ec_key])
def test_an_unreadable_or_non_rsa_key_is_a_credential_error(make_key: Any) -> None:
    data = json.loads(service_account_json())
    data["private_key"] = make_key()
    account = ServiceAccount.parse(json.dumps(data))

    async def main() -> None:
        async with httpx.AsyncClient() as http:
            tokens = ServiceAccountTokens(account, http, clock=_clock())
            with pytest.raises(CredentialError):
                await tokens.token()

    run(main())


def test_base64url_is_strict() -> None:
    assert b64url_decode(b64url_encode(b"\xff\xfe")) == b"\xff\xfe"
    for bad in ("", "a", "ab+c", "ab/c", "ab=c", "a b"):
        with pytest.raises(ValueError):
            b64url_decode(bad)
