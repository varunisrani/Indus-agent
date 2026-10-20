"""Self-hosted OpenID Connect provider (P18-04, D-16.44).

Configuration (``dashboard.oidc``; ``INDUS_DASHBOARD_OIDC_ISSUER``,
``INDUS_DASHBOARD_OIDC_CLIENT_ID`` and ``INDUS_DASHBOARD_OIDC_SCOPES`` win when non-empty):
``issuer``, a public ``client_id`` (no
client secret: authorization code + PKCE S256), ``scopes`` (default ``openid profile email``) and
``id_token_leeway`` (seconds of clock skew on ``exp``/``nbf``/``iat``; default 60, 0 = strict, an
invalid value — not a number, negative, not finite — fails closed to 0).

Discovery: ``<issuer>/.well-known/openid-configuration``, fetched on first use and kept an hour.
The issuer and every endpoint must be HTTPS unless the host is loopback; the advertised issuer
must equal the configured one (a trailing slash is tolerated). Keys come from ``jwks_uri`` (kept an
hour; an unknown ``kid`` triggers one refetch at most every five minutes).

Login: ``start_login`` returns the authorization URL and what the PKCE cookie keeps (verifier,
state, nonce). ``complete_login`` swaps the code at the token endpoint and verifies the ID token:
RS256 or ES256 only, the key by ``kid`` from the JWKS, ``iss`` and ``aud`` pinned, the leeway on
time claims, the nonce from the cookie. PyJWT and ``cryptography`` do the signature maths. Claims:
``sub`` → user id, ``email``, display name from ``name`` → ``preferred_username`` → ``nickname`` →
``email``, org from ``org_id`` / ``organization`` else the joined ``groups``. The browser session is
our signed token (``SessionSigner``) living as long as the IdP's access token (60 s .. 12 h).

Renewal: the refresh-token grant, one call at a time per refresh token (``SingleFlight``): requests
arriving together with the same token wait for the first and share its result (kept 30 s; a
refusal kept 5 s), so a rotating refresh token is never replayed. Logout revokes the refresh token
at the IdP's RFC 7009 endpoint when it advertises one.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import math
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Final, Protocol, cast
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from indus.web.auth.providers import (
    LoginRedirect,
    LoginRejected,
    Provider,
    ProviderRegistry,
    ProviderUnavailable,
    RefreshRejected,
    Session,
    SessionSigner,
)

__all__ = [
    "CLIENT_ID_ENV",
    "DEFAULT_LEEWAY_S",
    "DEFAULT_SCOPES",
    "ISSUER_ENV",
    "SCOPES_ENV",
    "HttpClient",
    "OidcConfigError",
    "OidcProvider",
    "OidcSettings",
    "SingleFlight",
    "parse_leeway",
    "register_oidc",
    "url_allowed",
]

log = logging.getLogger(__name__)

ISSUER_ENV: Final = "INDUS_DASHBOARD_OIDC_ISSUER"
CLIENT_ID_ENV: Final = "INDUS_DASHBOARD_OIDC_CLIENT_ID"
SCOPES_ENV: Final = "INDUS_DASHBOARD_OIDC_SCOPES"
DEFAULT_SCOPES: Final = "openid profile email"
DEFAULT_LEEWAY_S: Final = 60.0
ALGORITHMS: Final = ("RS256", "ES256")
DISCOVERY_TTL_S: Final = 3600.0
JWKS_TTL_S: Final = 3600.0
JWKS_REFETCH_S: Final = 300.0
SUCCESS_TTL_S: Final = 30.0
FAILURE_TTL_S: Final = 5.0
MIN_SESSION_S: Final = 60
MAX_SESSION_S: Final = 43_200
DEFAULT_SESSION_S: Final = 3600
HTTP_TIMEOUT_S: Final = 10.0


class HttpClient(Protocol):
    """What the provider needs from an HTTP client (``httpx.Client`` or a test double)."""

    def get(self, url: str, *, headers: Any = None) -> Any: ...

    def post(self, url: str, *, data: Any = None, headers: Any = None) -> Any: ...


class OidcConfigError(ProviderUnavailable):
    """The issuer or the discovery document breaks a rule (scheme, issuer equality, endpoints)."""


def parse_leeway(value: object) -> float:
    """Seconds of leeway; missing → 60, anything invalid → 0 (strict)."""
    if value is None or value == "":
        return DEFAULT_LEEWAY_S
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number < 0:
        return 0.0
    return number


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def url_allowed(url: object) -> bool:
    """HTTPS, or HTTP on a loopback host; no userinfo, no fragment."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return False
    if not host or parts.username is not None or parts.fragment:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and _loopback(host)


def _same_issuer(left: str, right: str) -> bool:
    return left.rstrip("/") == right.rstrip("/")


@dataclass(frozen=True, slots=True)
class OidcSettings:
    issuer: str
    client_id: str
    scopes: str = DEFAULT_SCOPES
    leeway: float = DEFAULT_LEEWAY_S

    @classmethod
    def read(cls, settings: Mapping[str, Any], env: Mapping[str, str]) -> OidcSettings:
        issuer = env.get(ISSUER_ENV, "").strip() or str(settings.get("issuer") or "").strip()
        client_id = (
            env.get(CLIENT_ID_ENV, "").strip() or str(settings.get("client_id") or "").strip()
        )
        scopes = (
            env.get(SCOPES_ENV, "").strip()
            or str(settings.get("scopes") or DEFAULT_SCOPES).strip()
            or DEFAULT_SCOPES
        )
        return cls(issuer, client_id, scopes, parse_leeway(settings.get("id_token_leeway")))


# -- single flight --------------------------------------------------------------------------------


@dataclass
class _Flight:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


class SingleFlight:
    """One call per key at a time; late arrivals within a short window share the result."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._now = monotonic
        self._guard = threading.Lock()
        self._flights: dict[bytes, _Flight] = {}
        self._done: dict[bytes, tuple[float, Session | None]] = {}

    @staticmethod
    def key(secret: str) -> bytes:
        return hashlib.sha256(secret.encode("utf-8", errors="surrogatepass")).digest()

    def _prune(self, now: float) -> None:
        for key in [key for key, (until, _) in self._done.items() if until <= now]:
            del self._done[key]

    def run(self, secret: str, call: Callable[[], Session]) -> Session:
        """``call()`` once for ``secret``; ``RefreshRejected`` is shared like a result.
        ``ProviderUnavailable`` (and anything unexpected) is not kept."""
        key = self.key(secret)
        with self._guard:
            self._prune(self._now())
            flight = self._flights.setdefault(key, _Flight())
            flight.users += 1
        try:
            with flight.lock:
                with self._guard:
                    kept = self._done.get(key)
                if kept is not None and kept[0] > self._now():
                    if kept[1] is None:
                        raise RefreshRejected("the refresh token was refused")
                    return kept[1]
                try:
                    session: Session | None = call()
                except RefreshRejected:
                    session = None
                with self._guard:
                    ttl = SUCCESS_TTL_S if session is not None else FAILURE_TTL_S
                    self._done[key] = (self._now() + ttl, session)
                if session is None:
                    raise RefreshRejected("the refresh token was refused")
                return session
        finally:
            with self._guard:
                flight.users -= 1
                if flight.users == 0:
                    self._flights.pop(key, None)


# -- the provider ---------------------------------------------------------------------------------


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _claim_text(claims: Mapping[str, object], *names: str) -> str:
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _org(claims: Mapping[str, object]) -> str:
    direct = _claim_text(claims, "org_id", "organization")
    if direct:
        return direct
    groups = claims.get("groups")
    if isinstance(groups, list | tuple):
        return ",".join(str(group) for group in cast(list[object], groups) if str(group))
    return ""


class OidcProvider(Provider):
    name: ClassVar[str] = "oidc"
    display_name: ClassVar[str] = "Single sign-on (OpenID Connect)"
    kind = "redirect"

    def __init__(
        self,
        settings: OidcSettings,
        *,
        signer: SessionSigner,
        http: HttpClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not settings.issuer or not settings.client_id:
            raise OidcConfigError("an issuer and a client id are required")
        if not url_allowed(settings.issuer):
            raise OidcConfigError("the issuer must be https (http only on a loopback host)")
        self.settings = settings
        self._signer = signer
        self._http: HttpClient = http or httpx.Client(
            timeout=HTTP_TIMEOUT_S, follow_redirects=False
        )
        self._monotonic = monotonic
        self._now = now
        self._lock = threading.Lock()
        self._discovered: tuple[float, dict[str, str]] | None = None
        self._jwks: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._jwks_refetched_at = -math.inf
        self.flights = SingleFlight(monotonic=monotonic)

    # -- discovery and keys --

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = self._http.get(url, headers={"accept": "application/json"})
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"the identity provider did not answer ({type(exc).__name__})"
            ) from None
        if response.status_code != 200:
            raise ProviderUnavailable(f"the identity provider answered {response.status_code}")
        try:
            data: Any = response.json()
        except ValueError:
            raise ProviderUnavailable(
                "the identity provider sent something that is not JSON"
            ) from None
        if not isinstance(data, dict):
            raise ProviderUnavailable("the identity provider sent an unexpected document")
        return cast(dict[str, Any], data)

    def discovery(self) -> dict[str, str]:
        with self._lock:
            cached = self._discovered
        if cached is not None and cached[0] > self._monotonic():
            return cached[1]
        url = self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
        doc = self._get_json(url)
        issuer = doc.get("issuer")
        if not isinstance(issuer, str) or not _same_issuer(issuer, self.settings.issuer):
            raise OidcConfigError("the discovered issuer does not match the configured issuer")
        found: dict[str, str] = {"issuer": issuer}
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = doc.get(key)
            if not url_allowed(value):
                raise OidcConfigError(f"the discovery document's {key} is missing or not https")
            found[key] = cast(str, value)
        revocation = doc.get("revocation_endpoint")
        if revocation is not None:
            if not url_allowed(revocation):
                raise OidcConfigError("the discovery document's revocation_endpoint is not https")
            found["revocation_endpoint"] = cast(str, revocation)
        with self._lock:
            self._discovered = (self._monotonic() + DISCOVERY_TTL_S, found)
        return found

    def _keys(self, *, force: bool = False) -> dict[str, dict[str, Any]]:
        with self._lock:
            cached = self._jwks
        now = self._monotonic()
        if cached is not None and cached[0] > now and not force:
            return cached[1]
        doc = self._get_json(self.discovery()["jwks_uri"])
        raw_keys = doc.get("keys")
        keys: dict[str, dict[str, Any]] = {}
        if isinstance(raw_keys, list):
            for item in cast(list[object], raw_keys):
                if isinstance(item, dict):
                    entry = cast(dict[str, Any], item)
                    kid = entry.get("kid")
                    if isinstance(kid, str) and kid:
                        keys[kid] = entry
        with self._lock:
            self._jwks = (now + JWKS_TTL_S, keys)
            self._jwks_refetched_at = now
        return keys

    def _key_for(self, kid: str) -> dict[str, Any] | None:
        keys = self._keys()
        if kid in keys:
            return keys[kid]
        with self._lock:
            may_refetch = self._monotonic() - self._jwks_refetched_at >= JWKS_REFETCH_S
        if may_refetch:
            return self._keys(force=True).get(kid)
        return None

    def verify_id_token(self, id_token: str, *, nonce: str | None) -> dict[str, object]:
        """The verified claims of ``id_token``; ``LoginRejected`` for anything wrong with it."""
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.InvalidTokenError:
            raise LoginRejected("the ID token is not a JWT") from None
        algorithm = header.get("alg")
        kid = header.get("kid")
        if algorithm not in ALGORITHMS:
            raise LoginRejected("the ID token uses an algorithm that is not accepted")
        if not isinstance(kid, str) or not kid:
            raise LoginRejected("the ID token names no key")
        jwk = self._key_for(kid)
        if jwk is None:
            raise LoginRejected("the ID token's key is not in the provider's key set")
        try:
            key = jwt.PyJWK(jwk, algorithm=cast(str, algorithm)).key
            discovered = self.discovery()["issuer"]
            issuers = sorted({self.settings.issuer, discovered})
            claims: dict[str, object] = jwt.decode(
                id_token,
                key,
                algorithms=[cast(str, algorithm)],
                audience=self.settings.client_id,
                issuer=issuers,
                leeway=self.settings.leeway,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.InvalidTokenError as exc:
            raise LoginRejected(f"the ID token was refused ({type(exc).__name__})") from None
        except (ValueError, TypeError, jwt.PyJWKError):
            raise LoginRejected("the provider's key could not be used") from None
        if nonce is not None and not secrets.compare_digest(
            str(claims.get("nonce", "")).encode(), nonce.encode()
        ):
            raise LoginRejected("the ID token's nonce does not match this login")
        return claims

    # -- login --

    def start_login(self, redirect_uri: str) -> LoginRedirect:
        endpoints = self.discovery()
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.client_id,
                "redirect_uri": redirect_uri,
                "scope": self.settings.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": _s256(verifier),
                "code_challenge_method": "S256",
            }
        )
        endpoint = endpoints["authorization_endpoint"]
        joiner = "&" if "?" in endpoint else "?"
        return LoginRedirect(
            url=f"{endpoint}{joiner}{query}",
            keep={"verifier": verifier, "state": state, "nonce": nonce},
        )

    def _token_call(self, form: dict[str, str]) -> dict[str, Any]:
        endpoint = self.discovery()["token_endpoint"]
        try:
            response = self._http.post(endpoint, data=form, headers={"accept": "application/json"})
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"the token endpoint did not answer ({type(exc).__name__})"
            ) from None
        if response.status_code >= 500:
            raise ProviderUnavailable(f"the token endpoint answered {response.status_code}")
        try:
            data: Any = response.json()
        except ValueError:
            data = None
        if response.status_code != 200 or not isinstance(data, dict):
            raise LoginRejected(f"the token endpoint refused the request ({response.status_code})")
        return cast(dict[str, Any], data)

    def _session_from(
        self, claims: Mapping[str, object], tokens: Mapping[str, Any], *, refresh: str
    ) -> Session:
        email = _claim_text(claims, "email")
        session = Session(
            user_id=str(claims.get("sub")),
            provider=self.name,
            expires_at=0,
            email=email,
            display_name=_claim_text(claims, "name", "preferred_username", "nickname", "email"),
            org_id=_org(claims),
            issuer=str(claims.get("iss", "")),
            refresh_token=refresh,
        )
        lifetime = tokens.get("expires_in")
        ttl = DEFAULT_SESSION_S
        if isinstance(lifetime, int | float) and not isinstance(lifetime, bool):
            ttl = int(lifetime)
        ttl = max(MIN_SESSION_S, min(MAX_SESSION_S, ttl))
        return self._signer.mint(session, ttl=ttl)

    def complete_login(self, code: str, kept: Mapping[str, str], redirect_uri: str) -> Session:
        verifier = kept.get("verifier", "")
        if not code or not verifier:
            raise LoginRejected("the callback has no code or the login has no verifier")
        tokens = self._token_call(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self.settings.client_id,
                "code_verifier": verifier,
            }
        )
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise LoginRejected("the token endpoint sent no ID token")
        claims = self.verify_id_token(id_token, nonce=kept.get("nonce", ""))
        refresh = tokens.get("refresh_token")
        return self._session_from(
            claims, tokens, refresh=refresh if isinstance(refresh, str) else ""
        )

    def verify_session(self, token: str) -> Session | None:
        return self._signer.verify(token, provider=self.name)

    def refresh(self, expired: Session | None, refresh_token: str) -> Session:
        if not refresh_token:
            raise RefreshRejected("no refresh token")

        def call() -> Session:
            try:
                tokens = self._token_call(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self.settings.client_id,
                    }
                )
            except LoginRejected:
                raise RefreshRejected("the identity provider refused the refresh token") from None
            new_refresh = tokens.get("refresh_token")
            keep = new_refresh if isinstance(new_refresh, str) and new_refresh else refresh_token
            id_token = tokens.get("id_token")
            if isinstance(id_token, str):
                try:
                    claims = self.verify_id_token(id_token, nonce=None)
                except LoginRejected:
                    raise RefreshRejected("the renewed ID token was refused") from None
                if expired is not None and claims.get("sub") != expired.user_id:
                    raise RefreshRejected("the renewed ID token names another user")
                return self._session_from(claims, tokens, refresh=keep)
            if expired is None:
                raise RefreshRejected("the renewal carried no ID token and no session is left")
            renewed = replace(expired, refresh_token=keep)
            lifetime = tokens.get("expires_in")
            ttl = DEFAULT_SESSION_S
            if isinstance(lifetime, int | float) and not isinstance(lifetime, bool):
                ttl = int(lifetime)
            return self._signer.mint(renewed, ttl=max(MIN_SESSION_S, min(MAX_SESSION_S, ttl)))

        return self.flights.run(refresh_token, call)

    def revoke(self, refresh_token: str) -> None:
        if not refresh_token:
            return
        try:
            endpoint = self.discovery().get("revocation_endpoint")
            if not endpoint:
                return
            self._http.post(
                endpoint,
                data={
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": self.settings.client_id,
                },
            )
        except (httpx.HTTPError, ProviderUnavailable) as exc:
            log.warning("revoking the refresh token at the identity provider failed: %s", exc)


def register_oidc(
    registry: ProviderRegistry,
    settings: Mapping[str, Any],
    env: Mapping[str, str],
    signer: SessionSigner,
    *,
    http: HttpClient | None = None,
) -> OidcProvider | None:
    """Register the provider when an issuer and a client id are configured, else record why not."""
    read = OidcSettings.read(settings, env)
    if not read.issuer:
        registry.unavailable(OidcProvider.name, "no issuer set (dashboard.oidc.issuer)")
        return None
    if not read.client_id:
        registry.unavailable(OidcProvider.name, "no client id set (dashboard.oidc.client_id)")
        return None
    try:
        provider = OidcProvider(read, signer=signer, http=http)
    except OidcConfigError as exc:
        registry.unavailable(OidcProvider.name, str(exc))
        return None
    registry.register(provider)
    return provider
