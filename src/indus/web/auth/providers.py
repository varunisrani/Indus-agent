"""Login providers for gated mode: the shared types, the session signer and the registry (P18-03).

Several providers may be active at once (username/password, OIDC, service bearer); the login page
lists the interactive ones in registration order. Whatever the provider, a browser session is our
own signed token (``SessionSigner``): ``v1.<payload>.<signature>``, the payload base64url JSON with
the provider name, the identity claims and the expiry, the signature HMAC-SHA256 over the payload
with a key from ``INDUS_DASHBOARD_SESSION_SECRET`` (at least 32 bytes) or random per process — then
every session ends when the process does. Provider tokens never sit in a cookie except an OIDC
refresh token (HttpOnly) for silent renewal.

Errors a provider raises, and what the routes answer:

- ``ProviderUnavailable`` — the IdP or a backing store cannot be reached: 503, cookies kept;
- ``LoginRejected`` — a bad authorization code, state or ID token: 400;
- ``BadCredentials`` — username/password refused: one generic 401;
- ``RefreshRejected`` — the refresh token is dead: the session ends (cookies cleared, 401).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Final, Literal, cast

__all__ = [
    "SESSION_SECRET_ENV",
    "SESSION_TTL_S",
    "BadCredentials",
    "LoginRedirect",
    "LoginRejected",
    "Provider",
    "ProviderRegistry",
    "ProviderUnavailable",
    "RefreshRejected",
    "Session",
    "SessionSigner",
    "TokenPrincipal",
]

log = logging.getLogger(__name__)

SESSION_SECRET_ENV: Final = "INDUS_DASHBOARD_SESSION_SECRET"
SESSION_TTL_S: Final = 43_200
MIN_SECRET_BYTES: Final = 32
_VERSION: Final = "v1"

type ProviderKind = Literal["password", "redirect", "token"]


class ProviderUnavailable(Exception):
    """The IdP or store is unreachable right now (503, never a forced logout)."""


class LoginRejected(Exception):
    """A redirect login could not be completed (bad code, state, nonce or ID token)."""


class BadCredentials(Exception):
    """Username or password refused (the route answers one generic 401)."""


class RefreshRejected(Exception):
    """The refresh token is no longer good; the person must sign in again."""


@dataclass(frozen=True, slots=True)
class Session:
    """A verified browser identity. ``token`` is our signed session token; ``refresh_token`` the
    provider's (empty when it has none). Neither appears in ``repr``."""

    user_id: str
    provider: str
    expires_at: int
    email: str = ""
    display_name: str = ""
    org_id: str = ""
    issuer: str = ""
    token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)

    def public(self) -> dict[str, object]:
        """The who-am-I view (no token)."""
        return {
            "user_id": self.user_id,
            "email": self.email,
            "display_name": self.display_name,
            "org_id": self.org_id,
            "provider": self.provider,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class TokenPrincipal:
    """A verified service caller (one bearer token, one request)."""

    principal: str
    provider: str
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LoginRedirect:
    """First leg of a redirect login: where to send the browser and what to keep in the PKCE
    cookie (verifier, state, nonce...)."""

    url: str
    keep: dict[str, str] = field(repr=False)


# -- the session signer ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SessionSigner:
    """Mints and checks our session tokens (see the module notes)."""

    def __init__(
        self,
        key: bytes | None = None,
        *,
        now: Callable[[], float] = time.time,
        ttl: int = SESSION_TTL_S,
    ) -> None:
        self.random_key = key is None
        self._key = key if key is not None else secrets.token_bytes(32)
        self._now = now
        self.ttl = ttl

    @classmethod
    def from_env(cls, env: Mapping[str, str], **kwargs: Any) -> SessionSigner:
        raw = env.get(SESSION_SECRET_ENV, "")
        if raw:
            key = raw.encode("utf-8")
            if len(key) >= MIN_SECRET_BYTES:
                return cls(key, **kwargs)
            log.warning(
                "%s is shorter than %d bytes; using a random key (sessions end on restart)",
                SESSION_SECRET_ENV,
                MIN_SECRET_BYTES,
            )
        return cls(None, **kwargs)

    def now(self) -> int:
        return int(self._now())

    @property
    def key_digest(self) -> str:
        """A short fingerprint of the key (never the key) for diagnostics."""
        return hashlib.sha256(b"indus-session-key\0" + self._key).hexdigest()[:12]

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest())

    def mint(self, session: Session, *, ttl: int | None = None) -> Session:
        """``session`` with a fresh token expiring ``ttl`` (default ``self.ttl``) from now."""
        issued = self.now()
        expires = issued + (self.ttl if ttl is None else int(ttl))
        claims = {
            "p": session.provider,
            "sub": session.user_id,
            "em": session.email,
            "nm": session.display_name,
            "org": session.org_id,
            "iss": session.issuer,
            "iat": issued,
            "exp": expires,
            "sid": secrets.token_hex(8),
        }
        payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        token = f"{_VERSION}.{payload}.{self._sign(payload)}"
        return replace(session, token=token, expires_at=expires)

    def verify(
        self, token: object, *, provider: str | None = None, allow_expired: bool = False
    ) -> Session | None:
        """The session a token carries, or None (bad shape or signature, other provider, expired
        unless ``allow_expired``)."""
        if not isinstance(token, str) or len(token) > 8192:
            return None
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != _VERSION:
            return None
        payload, signature = parts[1], parts[2]
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        try:
            raw: Any = json.loads(_unb64(payload))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        claims = cast(dict[str, object], raw)
        name = claims.get("p")
        subject = claims.get("sub")
        expires = claims.get("exp")
        if not isinstance(name, str) or not isinstance(subject, str) or not subject:
            return None
        if not isinstance(expires, int) or isinstance(expires, bool):
            return None
        if provider is not None and name != provider:
            return None
        if not allow_expired and expires <= self.now():
            return None
        return Session(
            user_id=subject,
            provider=name,
            expires_at=expires,
            email=_text(claims.get("em")),
            display_name=_text(claims.get("nm")),
            org_id=_text(claims.get("org")),
            issuer=_text(claims.get("iss")),
            token=token,
        )


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


# -- the provider base ----------------------------------------------------------------------------


class Provider:
    """What every provider offers; a provider overrides what its ``kind`` needs."""

    name: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    kind: ClassVar[ProviderKind] = "password"

    @property
    def interactive(self) -> bool:
        """Offered on the login page (password or redirect)."""
        return self.kind != "token"

    def verify_session(self, token: str) -> Session | None:
        raise NotImplementedError

    def password_login(self, username: str, password: str) -> Session:
        raise NotImplementedError

    def start_login(self, redirect_uri: str) -> LoginRedirect:
        raise NotImplementedError

    def complete_login(self, code: str, kept: Mapping[str, str], redirect_uri: str) -> Session:
        raise NotImplementedError

    def refresh(self, expired: Session | None, refresh_token: str) -> Session:
        """A renewed session; ``RefreshRejected`` when it cannot be renewed. ``expired`` is the
        signed session that ran out (None when the browser dropped it)."""
        raise RefreshRejected("this provider cannot renew a session")

    def revoke(self, refresh_token: str) -> None:
        """Best effort; never raises."""
        return None

    @property
    def token_routes(self) -> frozenset[str]:
        return frozenset()

    def verify_bearer(self, token: str, path: str) -> TokenPrincipal | None:
        return None

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "kind": self.kind,
            "supports_password": self.kind == "password",
        }


# -- the registry ---------------------------------------------------------------------------------


@dataclass
class ProviderRegistry:
    """The providers of one dashboard process, in order, plus why the others are off."""

    _providers: dict[str, Provider] = field(default_factory=dict[str, Provider])
    reasons: dict[str, str] = field(default_factory=dict[str, str])
    disabled_routes: set[str] = field(default_factory=set[str])
    """Bearer routes whose provider is configured but refused (they answer 404)."""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def register(self, provider: Provider) -> None:
        if not provider.name or not provider.display_name:
            raise TypeError("a provider needs a name and a display name")
        with self._lock:
            if provider.name in self._providers:
                raise ValueError(f"a provider named {provider.name!r} is already registered")
            self._providers[provider.name] = provider
            self.reasons.pop(provider.name, None)

    def unavailable(self, name: str, reason: str) -> None:
        """Record why ``name`` is not active (the refuse-to-bind message lists these)."""
        with self._lock:
            if name not in self._providers:
                self.reasons[name] = reason

    def get(self, name: str) -> Provider | None:
        with self._lock:
            return self._providers.get(name)

    def all(self) -> list[Provider]:
        with self._lock:
            return list(self._providers.values())

    def interactive(self) -> list[Provider]:
        return [provider for provider in self.all() if provider.interactive]

    def token_providers(self) -> list[Provider]:
        return [provider for provider in self.all() if provider.kind == "token"]

    def token_routes(self) -> frozenset[str]:
        routes: set[str] = set()
        for provider in self.token_providers():
            routes |= provider.token_routes
        return frozenset(routes)

    def ordered(self, hint: str | None) -> list[Provider]:
        """Interactive providers, the hinted one first."""
        found = self.interactive()
        if hint:
            found.sort(key=lambda provider: provider.name != hint)
        return found

    def refusal_lines(self) -> list[str]:
        """One line per provider that is not active, for the refuse-to-bind message."""
        return [f"{name}: {why}" for name, why in sorted(self.reasons.items())]


def first_session(
    registry: ProviderRegistry, token: str, hint: str | None
) -> tuple[Session | None, str | None]:
    """Run ``verify_session`` over the interactive providers (hinted first). Returns the session
    and None, or None and the name of a provider that was unreachable when none verified."""
    unreachable: str | None = None
    for provider in registry.ordered(hint):
        try:
            session = provider.verify_session(token)
        except ProviderUnavailable:
            unreachable = unreachable or provider.name
            continue
        if session is not None:
            return session, None
    return None, unreachable


def names(providers: Iterable[Provider]) -> list[str]:
    return [provider.name for provider in providers]
