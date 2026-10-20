"""The loopback session token (P18-02).

In loopback mode the dashboard mints a 32-byte URL-safe random token when it starts, or takes the
one the desktop shell minted and handed over in ``INDUS_DASHBOARD_SESSION_TOKEN`` (D-16.89). The
token is injected into ``index.html`` (never in gated mode) and every ``/api`` request outside the
public list carries it in ``X-Indus-Session-Token`` or ``Authorization: Bearer``. Comparison is
constant time over SHA-256 digests, so neither content nor length leaks. A handed-over token must
look like one we would mint (at least 32 URL-safe characters); anything else is refused at start.

``SessionToken`` never shows its value in ``repr``/``str``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping
from typing import Final

__all__ = [
    "SESSION_HEADER",
    "SESSION_TOKEN_ENV",
    "SessionToken",
    "SessionTokenError",
    "bearer_of",
]

SESSION_TOKEN_ENV: Final = "INDUS_DASHBOARD_SESSION_TOKEN"
SESSION_HEADER: Final = "X-Indus-Session-Token"
TOKEN_BYTES: Final = 32
MIN_HANDED_CHARS: Final = 32
_SHAPE: Final = re.compile(r"^[A-Za-z0-9_-]{32,512}$")


class SessionTokenError(ValueError):
    """The handed-over token is not usable (too short or outside the URL-safe alphabet)."""


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).digest()


def bearer_of(authorization: str | None) -> str:
    """The token of an ``Authorization: Bearer <t>`` header (scheme case-insensitive), or ``""``."""
    if not authorization:
        return ""
    scheme, _, rest = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return rest.strip()


class SessionToken:
    """One process's loopback token (see the module notes)."""

    __slots__ = ("_digest", "_value", "handed_over")

    def __init__(self, value: str, *, handed_over: bool = False) -> None:
        if not _SHAPE.match(value):
            raise SessionTokenError(
                f"the dashboard session token must be at least {MIN_HANDED_CHARS} URL-safe "
                "characters"
            )
        self._value = value
        self._digest = _digest(value)
        self.handed_over = handed_over

    @classmethod
    def mint(cls) -> SessionToken:
        return cls(secrets.token_urlsafe(TOKEN_BYTES))

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SessionToken:
        """The handed-over token when the env carries one (``SessionTokenError`` when it is not
        usable), else a fresh one."""
        handed = env.get(SESSION_TOKEN_ENV, "")
        if handed.strip():
            return cls(handed.strip(), handed_over=True)
        return cls.mint()

    @property
    def value(self) -> str:
        """The secret itself: only for the index injection and a spawned child's env."""
        return self._value

    def matches(self, candidate: object) -> bool:
        if not isinstance(candidate, str) or not candidate:
            return False
        return hmac.compare_digest(self._digest, _digest(candidate))

    def carried_by(self, headers: Mapping[str, str]) -> bool:
        """The header or a Bearer credential equals this token."""
        return self.matches(headers.get(SESSION_HEADER.lower(), "")) or self.matches(
            bearer_of(headers.get("authorization"))
        )

    def __repr__(self) -> str:
        return "SessionToken(<hidden>)"

    __str__ = __repr__
