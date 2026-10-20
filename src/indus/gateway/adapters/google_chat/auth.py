"""Google credentials for the Google Chat adapter (P16-13): our side and Google's side.

Our side, ``ServiceAccountTokens``: the service account's key JSON (from the secret store) signs a
one-hour JWT assertion (RS256, RFC 7523) that Google's OAuth endpoint trades for an access token.
The token is cached until a minute before it expires and refreshed by one caller at a time. The key
and the token never appear in an error, a log line or a repr; the assertion only ever goes to
Google's own https token endpoint (a key file naming any other ``token_uri`` is refused).

Google's side, ``GoogleTokenVerifier``: every HTTP callback carries ``Authorization: Bearer <JWT>``
signed by Google. Two flavours, chosen by the audience the Chat app is configured with:

- ``project_number``: issuer ``chat@system.gserviceaccount.com``, keys from that account's JWK set;
- ``url``: an OIDC ID token, issuer ``https://accounts.google.com``, keys from Google's OAuth JWK
  set, and a verified ``email`` claim naming the Chat service account (or the configured one).

A token passes only with ``alg`` RS256 (``none``, HMAC and every other algorithm are refused before
any key is looked at), a ``kid`` naming a published RSA key, a valid PKCS#1 v1.5 SHA-256 signature,
the expected issuer and audience, and ``exp`` / ``iat`` / ``nbf`` inside a 60 s skew. Keys come
from an injectable fetcher (``KeyFetcher``: URL → JWK set) and are cached for 300 s; an unknown
``kid`` forces one refetch, at most once a minute per URL.

RSA comes from ``cryptography`` (the ``google-chat`` extra; also in the base install), imported only
when a key is used; verification goes through the shared ``adapters/_jws.py`` (also Teams').
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx

from indus.foundation.clock import Clock
from indus.gateway.adapters._jws import b64url_decode, b64url_encode, rs256_verify, rsa_public_key

__all__ = [
    "CHAT_ISSUER",
    "CHAT_JWKS_URL",
    "CHAT_SCOPES",
    "GOOGLE_ISSUERS",
    "GOOGLE_JWKS_URL",
    "GOOGLE_TOKEN_URIS",
    "INSTALL_HINT",
    "AudienceKind",
    "CredentialError",
    "GoogleTokenVerifier",
    "JwkCache",
    "KeyFetcher",
    "ServiceAccount",
    "ServiceAccountTokens",
    "TokenError",
    "TokenRejected",
    "b64url_decode",
    "b64url_encode",
    "http_key_fetcher",
]

log = logging.getLogger(__name__)

INSTALL_HINT = "pip install 'indus-agent[google-chat]'"
CHAT_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/chat.bot",
    "https://www.googleapis.com/auth/pubsub",
)
CHAT_ISSUER = "chat@system.gserviceaccount.com"
GOOGLE_ISSUERS: frozenset[str] = frozenset({"https://accounts.google.com", "accounts.google.com"})
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
CHAT_JWKS_URL = f"https://www.googleapis.com/service_accounts/v1/jwk/{CHAT_ISSUER}"
# Where a service-account assertion may be sent: Google's own token endpoints, nothing else.
GOOGLE_TOKEN_URIS: frozenset[str] = frozenset(
    {
        "https://oauth2.googleapis.com/token",
        "https://accounts.google.com/o/oauth2/token",
        "https://www.googleapis.com/oauth2/v4/token",
    }
)
ASSERTION_LIFETIME_S = 3600
REFRESH_EARLY_S = 60.0
KEY_TTL_S = 300.0
FORCED_REFRESH_GAP_S = 60.0
CLOCK_SKEW_S = 60
MIN_RSA_BITS = 2048
MAX_TOKEN_CHARS = 16 * 1024

type AudienceKind = Literal["project_number", "url"]
type KeyFetcher = Callable[[str], Awaitable[Mapping[str, Any]]]


class CredentialError(ValueError):
    """The service-account key cannot be used (a configuration problem; never echoes the key)."""


class TokenError(Exception):
    """Google's token endpoint refused or could not be reached. ``retryable`` false = config."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class TokenRejected(Exception):
    """An inbound bearer token failed verification; ``reason`` is a short code for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Crypto:
    hashes: Any
    serialization: Any
    padding: Any
    rsa: Any


@functools.cache
def _crypto() -> _Crypto:
    """The ``cryptography`` pieces we use, imported on first need."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ModuleNotFoundError:  # pragma: no cover - the base install brings it
        raise CredentialError(
            f"Google Chat needs the cryptography package: {INSTALL_HINT}"
        ) from None
    return _Crypto(hashes, serialization, padding, rsa)


# -- our service account ---------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class ServiceAccount:
    """The fields of a service-account key file we use."""

    client_email: str
    private_key: str = field(repr=False)
    private_key_id: str = ""
    token_uri: str = "https://oauth2.googleapis.com/token"
    project_id: str = ""

    def __repr__(self) -> str:
        return f"<ServiceAccount {self.client_email}>"

    @classmethod
    def parse(cls, raw: str) -> ServiceAccount:
        """The key file's JSON text → `ServiceAccount`, or `CredentialError` (no key echoed)."""
        text = (raw or "").strip()
        if not text:
            raise CredentialError("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON is not set")
        if not text.startswith("{"):
            raise CredentialError(
                "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON must hold the key file's JSON itself (store it "
                "in the secret store), not a path"
            )
        try:
            data = json.loads(text)
        except ValueError:
            raise CredentialError("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON is not valid JSON") from None
        if not isinstance(data, dict):
            raise CredentialError("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON is not a JSON object")
        fields = cast(dict[str, Any], data)
        if fields.get("type") != "service_account":
            raise CredentialError("the key JSON is not a service-account key (type)")
        email = str(fields.get("client_email") or "").strip()
        key = str(fields.get("private_key") or "")
        if not email or "@" not in email:
            raise CredentialError("the key JSON has no client_email")
        if "PRIVATE KEY" not in key:
            raise CredentialError("the key JSON has no private_key")
        token_uri = str(fields.get("token_uri") or "https://oauth2.googleapis.com/token").strip()
        if token_uri not in GOOGLE_TOKEN_URIS:
            raise CredentialError(
                "the key JSON's token_uri is not a Google token endpoint; refusing to send the "
                "signed assertion there"
            )
        return cls(
            client_email=email,
            private_key=key,
            private_key_id=str(fields.get("private_key_id") or ""),
            token_uri=token_uri,
            project_id=str(fields.get("project_id") or ""),
        )


class ServiceAccountTokens:
    """Access tokens for one service account (see the module notes)."""

    def __init__(
        self,
        account: ServiceAccount,
        http: httpx.AsyncClient,
        *,
        clock: Clock,
        scopes: Iterable[str] = CHAT_SCOPES,
    ) -> None:
        self.account = account
        self._http = http
        self.clock = clock
        self.scopes = tuple(scopes)
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._signer: Any = None
        self.fetches = 0

    def __repr__(self) -> str:
        return f"<ServiceAccountTokens {self.account.client_email}>"

    def _load_signer(self) -> Any:
        if self._signer is None:
            crypto = _crypto()
            try:
                key = crypto.serialization.load_pem_private_key(
                    self.account.private_key.encode("utf-8"), password=None
                )
            except (ValueError, TypeError):
                raise CredentialError("the service account's private_key cannot be read") from None
            if not isinstance(key, crypto.rsa.RSAPrivateKey):
                raise CredentialError("the service account's private_key is not an RSA key")
            self._signer = key
        return self._signer

    def assertion(self) -> str:
        """A freshly signed JWT assertion for the token endpoint."""
        key = self._load_signer()
        crypto = _crypto()
        now = int(self.clock.now().timestamp())
        header: dict[str, str] = {"alg": "RS256", "typ": "JWT"}
        if self.account.private_key_id:
            header["kid"] = self.account.private_key_id
        claims = {
            "iss": self.account.client_email,
            "scope": " ".join(self.scopes),
            "aud": self.account.token_uri,
            "iat": now,
            "exp": now + ASSERTION_LIFETIME_S,
        }
        signing_input = ".".join(
            b64url_encode(json.dumps(part, separators=(",", ":")).encode())
            for part in (header, claims)
        )
        signature = key.sign(
            signing_input.encode("ascii"), crypto.padding.PKCS1v15(), crypto.hashes.SHA256()
        )
        return f"{signing_input}.{b64url_encode(signature)}"

    def _fresh(self) -> bool:
        return bool(self._token) and self.clock.monotonic() < self._expires_at - REFRESH_EARLY_S

    def invalidate(self) -> None:
        """Forget the cached token (Google answered 401 with it)."""
        self._token = ""
        self._expires_at = 0.0

    async def token(self) -> str:
        if self._fresh():
            return self._token
        async with self._lock:
            if self._fresh():
                return self._token
            await self._fetch()
            return self._token

    async def _fetch(self) -> None:
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": self.assertion(),
        }
        self.fetches += 1
        try:
            answer = await self._http.post(self.account.token_uri, data=form)
        except httpx.HTTPError as exc:
            raise TokenError(
                f"Google's token endpoint could not be reached: {type(exc).__name__}",
                retryable=True,
            ) from None
        if answer.status_code >= 500 or answer.status_code == 429:
            raise TokenError(
                f"Google's token endpoint answered HTTP {answer.status_code}", retryable=True
            )
        try:
            data = answer.json()
        except (ValueError, UnicodeDecodeError):
            data = None
        body = cast(dict[str, Any], data) if isinstance(data, dict) else {}
        if answer.status_code >= 400:
            code = str(body.get("error") or f"HTTP {answer.status_code}")[:80]
            raise TokenError(
                f"Google refused the service-account credentials ({code})", retryable=False
            )
        token = str(body.get("access_token") or "")
        if not token:
            raise TokenError("Google's token endpoint answered without a token", retryable=True)
        try:
            lifetime = float(body.get("expires_in") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600.0
        self._token = token
        self._expires_at = self.clock.monotonic() + max(lifetime, REFRESH_EARLY_S + 1)


# -- Google's signing keys --------------------------------------------------------------------


def http_key_fetcher(http: httpx.AsyncClient) -> KeyFetcher:
    """The production fetcher: GET the JWK set with the adapter's client (https Google hosts
    only)."""

    async def fetch(url: str) -> Mapping[str, Any]:
        parts = urlsplit(url)
        if parts.scheme != "https" or not (parts.hostname or "").endswith(".googleapis.com"):
            raise ValueError("JWK sets are only fetched from https googleapis.com hosts")
        answer = await http.get(url)
        answer.raise_for_status()
        data = answer.json()
        if not isinstance(data, dict):
            raise ValueError("the JWK set is not a JSON object")
        return cast(dict[str, Any], data)

    return fetch


def _int_of(text: object) -> int:
    if not isinstance(text, str):
        raise ValueError("JWK field is not a string")
    return int.from_bytes(b64url_decode(text), "big")


def _public_keys(document: Mapping[str, Any]) -> dict[str, Any]:
    """``kid`` → RSA public key for every usable signing key in a JWK set."""
    _crypto()  # a missing library is a configuration error, reported with the install hint
    found: dict[str, Any] = {}
    keys = document.get("keys")
    if not isinstance(keys, list):
        return found
    for raw in cast(list[object], keys):
        if not isinstance(raw, dict):
            continue
        jwk = cast(dict[str, Any], raw)
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not kid or jwk.get("kty") != "RSA":
            continue
        if jwk.get("alg") not in (None, "RS256") or jwk.get("use") not in (None, "sig"):
            continue
        try:
            n, e = _int_of(jwk.get("n")), _int_of(jwk.get("e"))
            if n.bit_length() < MIN_RSA_BITS:
                continue
            found[kid] = rsa_public_key(n, e)
        except ValueError:
            continue
    return found


@dataclass
class _KeySet:
    keys: dict[str, Any]
    fetched_at: float


class JwkCache:
    """Published keys per JWK-set URL (TTL 300 s; one forced refetch per minute for a new kid)."""

    def __init__(
        self,
        fetch: KeyFetcher,
        clock: Clock,
        *,
        ttl_s: float = KEY_TTL_S,
        forced_gap_s: float = FORCED_REFRESH_GAP_S,
    ) -> None:
        self._fetch = fetch
        self.clock = clock
        self.ttl_s = ttl_s
        self.forced_gap_s = forced_gap_s
        self._sets: dict[str, _KeySet] = {}
        self._forced_at: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.fetches = 0

    async def _load(self, url: str) -> _KeySet | None:
        self.fetches += 1
        try:
            document = await self._fetch(url)
        except Exception as exc:
            log.warning(
                "google_chat: fetching Google's signing keys failed: %s", type(exc).__name__
            )
            return self._sets.get(url)
        loaded = _KeySet(_public_keys(document), self.clock.monotonic())
        self._sets[url] = loaded
        return loaded

    async def key(self, url: str, kid: str) -> Any | None:
        async with self._lock:
            now = self.clock.monotonic()
            current = self._sets.get(url)
            if current is None or now - current.fetched_at >= self.ttl_s:
                current = await self._load(url)
            if current is not None and kid in current.keys:
                return current.keys[kid]
            last = self._forced_at.get(url)
            if last is not None and now - last < self.forced_gap_s:
                return None
            self._forced_at[url] = now
            current = await self._load(url)
            return current.keys.get(kid) if current is not None else None


class GoogleTokenVerifier:
    """Checks the bearer token on a Google Chat callback (see the module notes)."""

    def __init__(
        self,
        *,
        audience: str,
        kind: AudienceKind,
        keys: JwkCache,
        clock: Clock,
        emails: Iterable[str] = (CHAT_ISSUER,),
        skew_s: int = CLOCK_SKEW_S,
    ) -> None:
        if not audience.strip():
            raise CredentialError("the Google Chat callback audience is not set")
        self.audience = audience.strip()
        self.kind: AudienceKind = kind
        self.keys = keys
        self.clock = clock
        self.emails = frozenset(e.strip().lower() for e in emails if e.strip())
        self.skew_s = skew_s
        self.jwks_url = CHAT_JWKS_URL if kind == "project_number" else GOOGLE_JWKS_URL
        self.issuers = frozenset({CHAT_ISSUER}) if kind == "project_number" else GOOGLE_ISSUERS

    @staticmethod
    def kind_for(audience: str, configured: str = "") -> AudienceKind:
        """``project_number`` for an all-digit audience, ``url`` otherwise (unless configured)."""
        wanted = configured.strip().lower().replace("-", "_")
        if wanted in ("project_number", "url"):
            return cast(AudienceKind, wanted)
        return "project_number" if audience.strip().isdigit() else "url"

    async def verify(self, authorization: str | None) -> Mapping[str, Any]:
        """The token's claims, or `TokenRejected`."""
        value = (authorization or "").strip()
        scheme, _, token = value.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise TokenRejected("missing_bearer")
        if len(token) > MAX_TOKEN_CHARS:
            raise TokenRejected("oversized_token")
        parts = token.split(".")
        if len(parts) != 3:
            raise TokenRejected("malformed_token")
        try:
            header = json.loads(b64url_decode(parts[0]))
            claims = json.loads(b64url_decode(parts[1]))
            signature = b64url_decode(parts[2])
        except (ValueError, UnicodeDecodeError):
            raise TokenRejected("malformed_token") from None
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise TokenRejected("malformed_token")
        head = cast(dict[str, Any], header)
        body = cast(dict[str, Any], claims)
        # The algorithm is pinned before anything else: never "none", never HMAC.
        if head.get("alg") != "RS256":
            raise TokenRejected("bad_algorithm")
        kid = head.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenRejected("missing_kid")
        key = await self.keys.key(self.jwks_url, kid)
        if key is None:
            raise TokenRejected("unknown_kid")
        if not rs256_verify(key, f"{parts[0]}.{parts[1]}".encode("ascii"), signature):
            raise TokenRejected("bad_signature")
        self._check_claims(body)
        return body

    def _check_claims(self, claims: Mapping[str, Any]) -> None:
        if claims.get("iss") not in self.issuers:
            raise TokenRejected("wrong_issuer")
        audience = claims.get("aud")
        audiences = (
            [audience]
            if isinstance(audience, str)
            else [a for a in cast(list[object], audience) if isinstance(a, str)]
            if isinstance(audience, list)
            else []
        )
        if self.audience not in audiences:
            raise TokenRejected("wrong_audience")
        now = self.clock.now().timestamp()
        exp, iat, nbf = claims.get("exp"), claims.get("iat"), claims.get("nbf")
        if not _number(exp) or float(cast(float, exp)) + self.skew_s <= now:
            raise TokenRejected("expired")
        if _number(iat) and float(cast(float, iat)) - self.skew_s > now:
            raise TokenRejected("issued_in_future")
        if _number(nbf) and float(cast(float, nbf)) - self.skew_s > now:
            raise TokenRejected("not_yet_valid")
        if self.kind == "url":
            email = str(claims.get("email") or "").strip().lower()
            if email not in self.emails or claims.get("email_verified") is not True:
                raise TokenRejected("unexpected_identity")


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
