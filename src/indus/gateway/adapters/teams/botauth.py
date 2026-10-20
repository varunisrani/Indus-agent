"""Checks the Bot Connector's bearer token on every inbound Teams request (P16-11).

The Bot Framework signs each activity it POSTs to the bot with an RS256 JSON Web Token. A request
is accepted only when its token:

1. is a compact JWS of three strict base64url segments, at most 8 KiB;
2. names ``alg: RS256`` (nothing else: never ``none``, never an HMAC or another RSA padding), has
   a ``kid`` and no ``crit`` header; URLs in the header (``jku``, ``x5u``, ``jwk``) are ignored,
   keys come only from the Bot Framework's published metadata;
3. verifies against that key: RSASSA-PKCS1-v1_5 with SHA-256, done by ``cryptography`` (through
   ``adapters/_jws.py``; no RSA arithmetic of our own); keys under 2,048 bits are not used;
4. was issued by ``https://api.botframework.com`` for our app id (``aud`` a string or a list),
   has not expired and is already valid, give or take five minutes of clock skew.

Keys come from the OpenID metadata document (``jwks_uri``, which must be HTTPS on the metadata's
own host) through an injectable ``fetch`` (tests serve them from a fake). They are kept 24 hours; a
token naming an unknown ``kid`` triggers at most one refresh every five minutes (key rotation),
and a failed refresh keeps the keys already held. Each key's ``endorsements`` (the channels it may
sign for) and the token's ``serviceUrl`` claim are returned for the caller to compare with the
activity once its body is parsed.

``TokenRejected.reason`` is a short code safe to log; the token itself is never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from indus.foundation.clock import Clock
from indus.gateway.adapters._jws import b64url_decode, rs256_verify, rsa_public_key

__all__ = [
    "BOT_FRAMEWORK_ISSUER",
    "CLOCK_SKEW_S",
    "KEYS_TTL_S",
    "OPENID_METADATA_URL",
    "BotKeys",
    "BotTokenVerifier",
    "FetchJson",
    "RsaKey",
    "TokenRejected",
    "VerifiedToken",
    "b64url_decode",
    "parse_jwk",
    "rsa_sha256_verify",
]

log = logging.getLogger(__name__)

BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"
OPENID_METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
KEYS_TTL_S = 24 * 3600.0
# Least time between two refreshes asked for by an unknown ``kid``.
REFRESH_GAP_S = 300.0
CLOCK_SKEW_S = 300.0
MAX_TOKEN_BYTES = 8192
MIN_KEY_BITS = 2048
MAX_KEY_BITS = 8192

FetchJson = Callable[[str], Awaitable[Any]]
"""``await fetch(url)`` → the decoded JSON document at ``url`` (raises on any failure)."""


class TokenRejected(Exception):
    """The request's token is not acceptable; ``reason`` is a short, loggable code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RsaKey:
    kid: str
    n: int
    e: int
    # Channel ids the key may sign for; None when the key lists none.
    endorsements: frozenset[str] | None = None
    # ``cryptography``'s public key for ``n`` / ``e`` (built once by ``parse_jwk``).
    public: Any = field(default=None, compare=False)

    def __repr__(self) -> str:
        return f"<RsaKey {self.kid} {self.n.bit_length()} bits>"


@dataclass(frozen=True)
class VerifiedToken:
    claims: Mapping[str, Any]
    endorsements: frozenset[str] | None

    @property
    def service_url(self) -> str | None:
        value = self.claims.get("serviceUrl")
        return value if isinstance(value, str) and value else None


# -- primitives ---------------------------------------------------------------------------------


def _b64url_int(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = b64url_decode(value)
    except ValueError:
        return None
    return int.from_bytes(raw, "big") if raw else None


def parse_jwk(entry: object) -> RsaKey | None:
    """An RSA signing key from one JWK set entry, or None when it is not one we may use."""
    if not isinstance(entry, Mapping):
        return None
    jwk: Mapping[str, Any] = entry  # pyright: ignore[reportUnknownVariableType]
    kid = jwk.get("kid")
    if jwk.get("kty") != "RSA" or not isinstance(kid, str) or not kid:
        return None
    if jwk.get("use") not in (None, "sig"):
        return None
    if jwk.get("alg") not in (None, "RS256"):
        return None
    n = _b64url_int(jwk.get("n"))
    e = _b64url_int(jwk.get("e"))
    if n is None or e is None:
        return None
    if not MIN_KEY_BITS <= n.bit_length() <= MAX_KEY_BITS or e < 3 or e % 2 == 0 or e >= n:
        return None
    raw_endorsements = jwk.get("endorsements")
    endorsements: frozenset[str] | None = None
    if isinstance(raw_endorsements, list):
        endorsements = frozenset(
            str(x)
            for x in raw_endorsements
            if isinstance(x, str) and x  # pyright: ignore[reportUnknownVariableType]
        )
    try:
        public = rsa_public_key(n, e)
    except ValueError:
        return None
    return RsaKey(kid, n, e, endorsements, public)


def rsa_sha256_verify(key: RsaKey, message: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 / SHA-256 verification of ``message`` by ``cryptography``.

    A signature must be exactly the modulus' length (checked first); anything that is not a valid
    signature — including a key the library refuses — is False.
    """
    if len(signature) != (key.n.bit_length() + 7) // 8:
        return False
    public = key.public
    if public is None:
        try:
            public = rsa_public_key(key.n, key.e)
        except ValueError:
            return False
    return rs256_verify(public, message, signature)


# -- keys ---------------------------------------------------------------------------------------


class BotKeys:
    """The Bot Framework's signing keys, fetched through ``fetch`` (see the module notes)."""

    def __init__(
        self,
        fetch: FetchJson,
        clock: Clock,
        *,
        metadata_url: str = OPENID_METADATA_URL,
        ttl_s: float = KEYS_TTL_S,
        refresh_gap_s: float = REFRESH_GAP_S,
    ) -> None:
        parsed = urllib.parse.urlsplit(metadata_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("the OpenID metadata URL must be an https URL")
        self.fetch = fetch
        self.clock = clock
        self.metadata_url = metadata_url
        self.ttl_s = ttl_s
        self.refresh_gap_s = refresh_gap_s
        self._keys: dict[str, RsaKey] = {}
        self._fetched_at: float | None = None
        self._tried_at: float | None = None
        self._lock: asyncio.Lock | None = None

    @property
    def kids(self) -> frozenset[str]:
        return frozenset(self._keys)

    def _stale(self) -> bool:
        return self._fetched_at is None or self.clock.monotonic() - self._fetched_at >= self.ttl_s

    def _may_retry(self) -> bool:
        return (
            self._tried_at is None or self.clock.monotonic() - self._tried_at >= self.refresh_gap_s
        )

    async def key(self, kid: str) -> RsaKey | None:
        if self._stale() and self._may_retry():
            await self.refresh()
        found = self._keys.get(kid)
        if found is None and self._may_retry():
            await self.refresh()
            found = self._keys.get(kid)
        return found

    async def refresh(self) -> bool:
        """Fetch the metadata and the key set again; on failure the held keys stay."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._tried_at = self.clock.monotonic()
            try:
                keys = await self._load()
            except Exception as exc:
                log.warning("teams: signing keys not refreshed: %s", _reason(exc))
                return False
            self._keys = keys
            self._fetched_at = self.clock.monotonic()
            return True

    async def _load(self) -> dict[str, RsaKey]:
        metadata = await self.fetch(self.metadata_url)
        if not isinstance(metadata, Mapping):
            raise ValueError("the OpenID metadata is not an object")
        doc: Mapping[str, Any] = metadata  # pyright: ignore[reportUnknownVariableType]
        jwks_uri = doc.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise ValueError("the OpenID metadata has no jwks_uri")
        home = urllib.parse.urlsplit(self.metadata_url)
        target = urllib.parse.urlsplit(jwks_uri)
        if target.scheme != "https" or target.hostname != home.hostname or target.username:
            raise ValueError("the key set is not on the metadata host over https")
        algs = doc.get("id_token_signing_alg_values_supported")
        if isinstance(algs, list) and "RS256" not in algs:
            raise ValueError("the metadata does not offer RS256")
        jwks = await self.fetch(jwks_uri)
        listed = jwks.get("keys") if isinstance(jwks, Mapping) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(listed, list):
            raise ValueError("the key set has no keys")
        keys: dict[str, RsaKey] = {}
        for entry in listed:  # pyright: ignore[reportUnknownVariableType]
            key = parse_jwk(entry)
            if key is not None:
                keys[key.kid] = key
        if not keys:
            raise ValueError("the key set has no usable RSA key")
        return keys


def _reason(exc: BaseException) -> str:
    text = str(exc) if isinstance(exc, ValueError) else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# -- tokens -------------------------------------------------------------------------------------


def _json_segment(segment: str, what: str) -> dict[str, Any]:
    try:
        data: Any = json.loads(b64url_decode(segment))
    except (ValueError, UnicodeDecodeError):
        raise TokenRejected(f"{what}_not_json") from None
    if not isinstance(data, dict):
        raise TokenRejected(f"{what}_not_object")
    return data  # pyright: ignore[reportUnknownVariableType]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class BotTokenVerifier:
    """Verifies ``Authorization: Bearer <JWT>`` for one app id (see the module notes)."""

    def __init__(
        self,
        keys: BotKeys,
        clock: Clock,
        *,
        app_id: str,
        issuer: str = BOT_FRAMEWORK_ISSUER,
        skew_s: float = CLOCK_SKEW_S,
    ) -> None:
        if not app_id:
            raise ValueError("the app id is required")
        self.keys = keys
        self.clock = clock
        self.app_id = app_id
        self.issuer = issuer
        self.skew_s = skew_s

    async def verify(self, authorization: str | None) -> VerifiedToken:
        scheme, _, token = (authorization or "").strip().partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise TokenRejected("no_bearer_token")
        if len(token) > MAX_TOKEN_BYTES or not token.isascii():
            raise TokenRejected("malformed_token")
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            raise TokenRejected("not_a_jws")
        header = _json_segment(parts[0], "header")
        if header.get("alg") != "RS256":
            raise TokenRejected("algorithm_not_allowed")
        if "crit" in header:
            raise TokenRejected("critical_header")
        typ = header.get("typ")
        if typ is not None and (not isinstance(typ, str) or typ.upper() != "JWT"):
            raise TokenRejected("not_a_jwt")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenRejected("no_key_id")
        try:
            signature = b64url_decode(parts[2])
        except ValueError:
            raise TokenRejected("signature_not_base64url") from None
        claims = _json_segment(parts[1], "payload")
        key = await self.keys.key(kid)
        if key is None:
            raise TokenRejected("unknown_key")
        signed = f"{parts[0]}.{parts[1]}".encode("ascii")
        if not rsa_sha256_verify(key, signed, signature):
            raise TokenRejected("bad_signature")
        self._check_claims(claims)
        return VerifiedToken(claims, key.endorsements)

    def _check_claims(self, claims: Mapping[str, Any]) -> None:
        if claims.get("iss") != self.issuer:
            raise TokenRejected("wrong_issuer")
        audience: Any = claims.get("aud")
        audiences: list[Any] = (
            list(audience)  # pyright: ignore[reportUnknownArgumentType]
            if isinstance(audience, list)
            else [audience]
        )
        if not any(isinstance(a, str) and a == self.app_id for a in audiences):
            raise TokenRejected("wrong_audience")
        now = self.clock.now().timestamp()
        expires = _number(claims.get("exp"))
        if expires is None:
            raise TokenRejected("no_expiry")
        if now > expires + self.skew_s:
            raise TokenRejected("expired")
        if "nbf" in claims:
            not_before = _number(claims.get("nbf"))
            if not_before is None or not_before > now + self.skew_s:
                raise TokenRejected("not_yet_valid")
