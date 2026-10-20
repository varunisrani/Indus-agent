"""The gated-mode auth cookies (P18-03).

- ``indus_session_at``: our signed session token; Max-Age = its remaining life (at least 60 s);
- ``indus_session_rt``: the provider's refresh token, 30 days, written only when there is one;
- ``indus_session_provider``: the provider's name (a routing hint, not a secret), 30 days;
- ``indus_session_pkce``: PKCE verifier, state, nonce, provider and landing path, 10 minutes;
  SameSite=None+Secure on HTTPS (it must survive the IdP's cross-site redirect chain), Lax on HTTP;
- ``indus_sso_attempt``: ``1``, the one-shot automatic sign-in guard, 60 s.

All are HttpOnly with ``Path`` = the proxy prefix (or ``/``), and ``Secure`` whenever the request
arrived over HTTPS (``scope["scheme"]`` after trusted forwarding, ``auth.proxy``). Over HTTPS the
name carries a cookie prefix: ``__Host-`` at the root path, ``__Secure-`` under a proxy prefix
(``__Host-`` requires ``Path=/``). Readers try every variant; logout clears every variant.

The PKCE payload is base64url(JSON) without padding, which needs no cookie quoting.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any, Final, Literal, cast

from starlette.requests import HTTPConnection
from starlette.responses import Response

__all__ = [
    "AT_COOKIE",
    "PKCE_COOKIE",
    "PKCE_MAX_AGE",
    "PROVIDER_COOKIE",
    "RT_COOKIE",
    "RT_MAX_AGE",
    "SSO_ATTEMPT_COOKIE",
    "SSO_ATTEMPT_MAX_AGE",
    "clear_all",
    "clear_pkce",
    "clear_session",
    "clear_sso_attempt",
    "cookie_name",
    "decode_payload",
    "encode_payload",
    "is_https",
    "read",
    "set_pkce",
    "set_session",
    "set_sso_attempt",
]

AT_COOKIE: Final = "indus_session_at"
RT_COOKIE: Final = "indus_session_rt"
PROVIDER_COOKIE: Final = "indus_session_provider"
PKCE_COOKIE: Final = "indus_session_pkce"
SSO_ATTEMPT_COOKIE: Final = "indus_sso_attempt"
ALL_COOKIES: Final = (AT_COOKIE, RT_COOKIE, PROVIDER_COOKIE, PKCE_COOKIE, SSO_ATTEMPT_COOKIE)

RT_MAX_AGE: Final = 30 * 24 * 3600
PKCE_MAX_AGE: Final = 10 * 60
SSO_ATTEMPT_MAX_AGE: Final = 60
MIN_AT_MAX_AGE: Final = 60
_VARIANTS: Final = ("__Host-", "__Secure-", "")
_B64URL: Final = re.compile(r"^[A-Za-z0-9_-]{1,8192}$")

type SameSite = Literal["lax", "strict", "none"]


def is_https(connection: HTTPConnection) -> bool:
    return connection.url.scheme in ("https", "wss")


def cookie_name(bare: str, *, https: bool, prefix: str) -> str:
    if not https:
        return bare
    return f"__Secure-{bare}" if prefix else f"__Host-{bare}"


def _path(prefix: str) -> str:
    return prefix or "/"


def _set(
    response: Response,
    bare: str,
    value: str,
    *,
    max_age: int,
    https: bool,
    prefix: str,
    samesite: SameSite = "lax",
) -> None:
    response.set_cookie(
        cookie_name(bare, https=https, prefix=prefix),
        value,
        max_age=max_age,
        path=_path(prefix),
        httponly=True,
        secure=https,
        samesite=samesite,
    )


def set_session(
    response: Response,
    *,
    session_token: str,
    max_age: int,
    refresh_token: str,
    provider: str,
    https: bool,
    prefix: str,
) -> None:
    """Write the session cookies (the refresh-token cookie only when there is one)."""
    _set(
        response,
        AT_COOKIE,
        session_token,
        max_age=max(MIN_AT_MAX_AGE, int(max_age)),
        https=https,
        prefix=prefix,
    )
    if refresh_token:
        _set(response, RT_COOKIE, refresh_token, max_age=RT_MAX_AGE, https=https, prefix=prefix)
    if provider:
        _set(response, PROVIDER_COOKIE, provider, max_age=RT_MAX_AGE, https=https, prefix=prefix)


def set_pkce(response: Response, payload: dict[str, str], *, https: bool, prefix: str) -> None:
    _set(
        response,
        PKCE_COOKIE,
        encode_payload(payload),
        max_age=PKCE_MAX_AGE,
        https=https,
        prefix=prefix,
        samesite="none" if https else "lax",
    )


def set_sso_attempt(response: Response, *, https: bool, prefix: str) -> None:
    _set(response, SSO_ATTEMPT_COOKIE, "1", max_age=SSO_ATTEMPT_MAX_AGE, https=https, prefix=prefix)


def _clear(response: Response, bare: str, *, prefix: str, samesite: SameSite) -> None:
    """Expire every name variant: the prefixed ones need Secure (and ``__Host-`` Path=/)."""
    response.set_cookie(
        f"__Host-{bare}", "", max_age=0, path="/", httponly=True, secure=True, samesite=samesite
    )
    response.set_cookie(
        f"__Secure-{bare}",
        "",
        max_age=0,
        path=_path(prefix),
        httponly=True,
        secure=True,
        samesite=samesite,
    )
    response.set_cookie(bare, "", max_age=0, path=_path(prefix), httponly=True, samesite="lax")


def clear_all(response: Response, *, prefix: str) -> None:
    """Expire every auth cookie in every variant (logout, expired session)."""
    for bare in ALL_COOKIES:
        _clear(response, bare, prefix=prefix, samesite="none" if bare == PKCE_COOKIE else "lax")


def clear_session(response: Response, *, prefix: str) -> None:
    for bare in (AT_COOKIE, RT_COOKIE, PROVIDER_COOKIE):
        _clear(response, bare, prefix=prefix, samesite="lax")


def clear_pkce(response: Response, *, prefix: str) -> None:
    _clear(response, PKCE_COOKIE, prefix=prefix, samesite="none")


def clear_sso_attempt(response: Response, *, prefix: str) -> None:
    _clear(response, SSO_ATTEMPT_COOKIE, prefix=prefix, samesite="lax")


def read(connection: HTTPConnection, bare: str) -> str | None:
    """The first non-empty value among the name variants, most strict first."""
    for variant in _VARIANTS:
        value = connection.cookies.get(f"{variant}{bare}")
        if value:
            return value
    return None


def encode_payload(parts: dict[str, str]) -> str:
    raw = json.dumps(parts, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_payload(raw: str | None) -> dict[str, str]:
    """The PKCE payload, or ``{}`` for anything that is not one."""
    if not raw or not _B64URL.match(raw):
        return {}
    try:
        decoded: Any = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    items = cast(dict[object, object], decoded).items()
    return {str(key): str(value) for key, value in items if isinstance(value, str)}
