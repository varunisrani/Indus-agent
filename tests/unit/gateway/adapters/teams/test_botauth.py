"""The Bot Connector token check (P16-11): only RS256 tokens signed by a published Bot Framework
key, from the Bot Framework, for our app id, inside their validity window."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any

import pytest

from indus.gateway.adapters.teams.botauth import (
    CLOCK_SKEW_S,
    KEYS_TTL_S,
    BotKeys,
    BotTokenVerifier,
    RsaKey,
    TokenRejected,
    b64url_decode,
    parse_jwk,
    rsa_sha256_verify,
)
from tests.fakes.clock import FakeClock
from tests.fakes.platform.teams_wire import (
    APP_ID,
    KEYS_URL,
    METADATA_URL,
    BotSigner,
    b64url,
    claims_for,
)


def run[T](coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


class KeyService:
    """Serves the metadata and the key set; counts fetches; can fail or lie."""

    def __init__(self, *keys: dict[str, Any]) -> None:
        self.keys = list(keys)
        self.fetched: list[str] = []
        self.fail = False
        self.jwks_uri = KEYS_URL

    async def __call__(self, url: str) -> Any:
        self.fetched.append(url)
        if self.fail:
            raise OSError("key service down")
        if url == METADATA_URL:
            return {"issuer": "https://api.botframework.com", "jwks_uri": self.jwks_uri}
        if url == self.jwks_uri:
            return {"keys": self.keys}
        raise AssertionError(f"unexpected fetch of {url}")


class Rig:
    def __init__(self) -> None:
        self.clock = FakeClock(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
        self.signer = BotSigner()
        self.service = KeyService(self.signer.jwk())
        self.keys = BotKeys(self.service, self.clock)
        self.verifier = BotTokenVerifier(self.keys, self.clock, app_id=APP_ID)

    def now(self) -> float:
        return self.clock.now().timestamp()

    def token(self, **claims: Any) -> str:
        return self.signer.token(claims_for(self.now(), **claims))

    def check(self, token: str) -> Any:
        return run(self.verifier.verify(f"Bearer {token}"))

    def refused(self, token: str, reason: str | None = None) -> str:
        with pytest.raises(TokenRejected) as caught:
            self.check(token)
        if reason is not None:
            assert caught.value.reason == reason
        return caught.value.reason


def _forge(header: dict[str, Any], claims: dict[str, Any], signature: bytes = b"") -> str:
    head = b64url(json.dumps(header).encode())
    body = b64url(json.dumps(claims).encode())
    return f"{head}.{body}.{b64url(signature)}"


def test_a_good_token_verifies_and_reports_its_key_endorsements() -> None:
    rig = Rig()
    verified = rig.check(rig.token())
    assert verified.claims["aud"] == APP_ID
    assert verified.endorsements == frozenset({"msteams"})
    assert verified.service_url == "https://smba.trafficmanager.net/teams/"
    assert rig.service.fetched == [METADATA_URL, KEYS_URL]


@pytest.mark.parametrize("alg", ["none", "None", "HS256", "PS256", "RS512", "ES256", ""])
def test_only_rs256_is_accepted(alg: str) -> None:
    rig = Rig()
    claims = claims_for(rig.now())
    forged = _forge({"alg": alg, "kid": rig.signer.kid}, claims, b"\x01" * 256)
    rig.refused(forged, "algorithm_not_allowed")


def test_unsigned_and_hmac_confused_tokens_are_refused() -> None:
    rig = Rig()
    claims = claims_for(rig.now())
    # "alg: none" with an empty signature, and a dangling two-part token.
    rig.refused(_forge({"alg": "none", "kid": rig.signer.kid}, claims))
    two_parts = ".".join(_forge({"alg": "RS256", "kid": rig.signer.kid}, claims).split(".")[:2])
    rig.refused(two_parts, "not_a_jws")
    rig.refused(_forge({"alg": "RS256", "kid": rig.signer.kid}, claims), "not_a_jws")
    rig.refused(
        _forge({"alg": "RS256", "kid": rig.signer.kid}, claims, b"\x00" * 256), "bad_signature"
    )
    # The classic key-confusion forgery: HMAC with the public key as the secret, labelled RS256.
    public = json.dumps(rig.signer.jwk()).encode()
    head = b64url(json.dumps({"alg": "RS256", "kid": rig.signer.kid}).encode())
    body = b64url(json.dumps(claims).encode())
    mac = hmac.new(public, f"{head}.{body}".encode(), hashlib.sha256).digest()
    rig.refused(f"{head}.{body}.{b64url(mac)}", "bad_signature")


def test_a_tampered_payload_fails_the_signature() -> None:
    rig = Rig()
    head, _, signature = rig.token().split(".")
    other = b64url(json.dumps(claims_for(rig.now(), aud=APP_ID, extra="x")).encode())
    rig.refused(f"{head}.{other}.{signature}", "bad_signature")


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"iss": "https://sts.windows.net/evil/"}, "wrong_issuer"),
        ({"iss": None}, "wrong_issuer"),
        ({"aud": "some-other-app"}, "wrong_audience"),
        ({"aud": ["some-other-app", "another"]}, "wrong_audience"),
        ({"aud": None}, "wrong_audience"),
        ({"exp": None}, "no_expiry"),
        ({"exp": "soon"}, "no_expiry"),
        ({"exp": True}, "no_expiry"),
    ],
)
def test_claims_are_checked(claims: dict[str, Any], reason: str) -> None:
    rig = Rig()
    rig.refused(rig.token(**claims), reason)


def test_an_audience_list_naming_our_app_is_accepted() -> None:
    rig = Rig()
    assert rig.check(rig.token(aud=["other", APP_ID])).claims["aud"] == ["other", APP_ID]


def test_expiry_and_not_before_allow_five_minutes_of_skew() -> None:
    rig = Rig()
    now = rig.now()
    rig.check(rig.token(exp=int(now - CLOCK_SKEW_S + 5)))
    rig.refused(rig.token(exp=int(now - CLOCK_SKEW_S - 5)), "expired")
    rig.check(rig.token(nbf=int(now + CLOCK_SKEW_S - 5)))
    rig.refused(rig.token(nbf=int(now + CLOCK_SKEW_S + 5)), "not_yet_valid")
    rig.refused(rig.token(nbf="later"), "not_yet_valid")
    # A token that was fine when issued expires on the clock.
    token = rig.token()
    rig.clock.advance(3600 + CLOCK_SKEW_S + 1)
    rig.refused(token, "expired")


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "Bearer ",
        "Bearer a.b",
        "Bearer a.b.c.d",
        "Bearer ..",
        "Bearer " + "a" * 9000,
        "Bearer é.é.é",
    ],
)
def test_malformed_authorization_headers_are_refused(header: str) -> None:
    rig = Rig()
    with pytest.raises(TokenRejected):
        run(rig.verifier.verify(header))
    with pytest.raises(TokenRejected):
        run(rig.verifier.verify(None))


def test_header_rules_crit_kid_typ_and_urls() -> None:
    rig = Rig()
    claims = claims_for(rig.now())
    rig.refused(rig.signer.token(claims, crit=["exp"]), "critical_header")
    rig.refused(rig.signer.token(claims, typ="at+jwt"), "not_a_jwt")
    rig.refused(rig.signer.token(claims, kid=""), "no_key_id")
    rig.refused(rig.signer.token(claims, kid=7), "no_key_id")
    # Key URLs in the header are never fetched; the published key still decides.
    good = rig.signer.token(claims, jku="https://evil.example/keys", x5u="https://evil.example/c")
    rig.check(good)
    assert all(url in (METADATA_URL, KEYS_URL) for url in rig.service.fetched)
    other = BotSigner(rig.signer.kid, slot=1)
    forged = other.token(claims, jwk=other.jwk(), jku="https://evil.example/keys")
    rig.refused(forged, "bad_signature")


def test_an_unknown_kid_refreshes_once_then_waits_before_asking_again() -> None:
    rig = Rig()
    rig.check(rig.token())
    assert rig.service.fetched.count(KEYS_URL) == 1
    newer = BotSigner("bf-key-2", slot=1)
    claims = claims_for(rig.now())
    # Rotation published already: the unknown kid triggers one refresh and then verifies.
    rig.clock.advance(400)
    rig.service.keys.append(newer.jwk())
    rig.check(newer.token(claims_for(rig.now())))
    assert rig.service.fetched.count(KEYS_URL) == 2
    # A kid that does not exist: one refresh at most every five minutes.
    stranger = BotSigner("nobody", slot=1)
    rig.refused(stranger.token(claims), "unknown_key")
    rig.refused(stranger.token(claims), "unknown_key")
    assert rig.service.fetched.count(KEYS_URL) == 2
    rig.clock.advance(301)
    rig.refused(stranger.token(claims_for(rig.now())), "unknown_key")
    assert rig.service.fetched.count(KEYS_URL) == 3


def test_keys_are_kept_a_day_and_a_failed_refresh_keeps_them() -> None:
    rig = Rig()
    rig.check(rig.token())
    rig.clock.advance(KEYS_TTL_S - 10)
    rig.check(rig.token())
    assert rig.service.fetched.count(KEYS_URL) == 1
    rig.clock.advance(20)
    rig.service.fail = True
    rig.check(rig.token())  # the refresh failed; the held key still verifies
    assert rig.service.fetched.count(METADATA_URL) == 2
    rig.service.fail = False
    rig.clock.advance(301)
    rig.check(rig.token())
    assert rig.service.fetched.count(KEYS_URL) == 2


def test_the_first_fetch_failing_refuses_the_request() -> None:
    rig = Rig()
    rig.service.fail = True
    rig.refused(rig.token(), "unknown_key")


@pytest.mark.parametrize(
    "jwks_uri",
    [
        "http://login.botframework.com/v1/.well-known/keys",
        "https://evil.example/keys",
        "https://user@login.botframework.com/v1/.well-known/keys",
    ],
)
def test_a_key_set_off_the_metadata_host_is_not_used(jwks_uri: str) -> None:
    rig = Rig()
    rig.service.jwks_uri = jwks_uri
    rig.refused(rig.token(), "unknown_key")
    assert jwks_uri not in rig.service.fetched


def test_small_keys_and_non_signing_entries_are_ignored() -> None:
    small = BotSigner("small", bits=1024)
    good = BotSigner()
    assert parse_jwk(small.jwk()) is None
    assert parse_jwk({**good.jwk(), "use": "enc"}) is None
    assert parse_jwk({**good.jwk(), "kty": "EC"}) is None
    assert parse_jwk({**good.jwk(), "alg": "RS512"}) is None
    assert parse_jwk({**good.jwk(), "e": b64url((4).to_bytes(1, "big"))}) is None
    assert parse_jwk({**good.jwk(), "n": "!!"}) is None
    assert parse_jwk("not a key") is None
    key = parse_jwk({k: v for k, v in good.jwk().items() if k != "endorsements"})
    assert key is not None and key.endorsements is None
    # A token signed by the small key is refused although the key is published.
    rig = Rig()
    rig.service.keys.append(small.jwk())
    rig.refused(small.token(claims_for(rig.now())), "unknown_key")


def test_rs256_verification_agrees_with_the_reference_library() -> None:
    signer = BotSigner()
    key = parse_jwk(signer.jwk())
    assert key is not None
    for message in (b"", b"a", b"hello.world", bytes(range(256)) * 9):
        signature = signer.sign_raw(message)
        assert rsa_sha256_verify(key, message, signature)
        assert not rsa_sha256_verify(key, message + b"!", signature)
        flipped = bytes([signature[0] ^ 1]) + signature[1:]
        assert not rsa_sha256_verify(key, message, flipped)
    sig = signer.sign_raw(b"x")
    assert not rsa_sha256_verify(key, b"x", sig[1:])  # wrong length
    assert not rsa_sha256_verify(key, b"x", b"\x00" + sig)
    assert not rsa_sha256_verify(key, b"x", key.n.to_bytes(len(sig), "big"))
    assert not rsa_sha256_verify(RsaKey("tiny", 3233, 17), b"x", b"\x01\x02")


def test_base64url_is_strict() -> None:
    assert b64url_decode("aGk") == b"hi"
    for bad in ("aGk=", "a+b/", "a", "é"):
        with pytest.raises(ValueError):
            b64url_decode(bad)
    assert base64.urlsafe_b64encode(b"hi") == b"aGk="
