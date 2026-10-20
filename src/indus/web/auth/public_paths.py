"""The one list of ``/api`` paths reachable without a credential (P18-02, R09.4).

Both gates — the loopback session-token gate and the gated-mode login gate — import
``PUBLIC_API_PATHS`` from here and match it exactly, so the two lists can never drift apart (a
test asserts both gates hold this very object). Every entry must be safe to show anyone who can
reach the port: liveness, coarse status, the config defaults and schema, model metadata, the theme
list, the dashboard plugin manifest list, and the cron fire webhook (which carries its own
short-lived signed token, checked by its handler, P18-14). Widening this list needs a D-id.

``LOGIN_PUBLIC_PREFIXES`` are what gated mode additionally lets through before login: the login
page and the auth round trip, the providers list the login page reads, and the static bundle files
the login page needs. They are not API data, so they are not on the shared list.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "LOGIN_PUBLIC_PREFIXES",
    "PUBLIC_API_PATHS",
    "QUERY_TOKEN_PATHS",
    "is_login_public",
    "is_public_api_path",
]

PUBLIC_API_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/api/config/defaults",
        "/api/config/schema",
        "/api/cron/fire",
        "/api/dashboard/plugins",
        "/api/dashboard/themes",
        "/api/health",
        "/api/model/info",
        "/api/status",
    }
)
"""Exact paths, no prefix matching: ``/api/status/x`` is not public."""

LOGIN_PUBLIC_PREFIXES: Final[tuple[str, ...]] = (
    "/login",
    "/auth/login",
    "/auth/callback",
    "/auth/password-login",
    "/auth/logout",
    "/api/auth/providers",
    "/assets/",
    "/fonts/",
    "/favicon.ico",
    "/favicon.svg",
)
"""Gated mode only: exact path, or a prefix when the entry ends with ``/``."""

QUERY_TOKEN_PATHS: Final[frozenset[str]] = frozenset({"/api/files/download"})
"""The only REST route that may carry the loopback token as ``?token=`` (a download opened in a
new tab cannot set a header; P18-16)."""


def is_public_api_path(path: str) -> bool:
    return path in PUBLIC_API_PATHS


def is_login_public(path: str) -> bool:
    """Reachable before login in gated mode: the shared list or a login-bootstrap path."""
    if path in PUBLIC_API_PATHS:
        return True
    for entry in LOGIN_PUBLIC_PREFIXES:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False
