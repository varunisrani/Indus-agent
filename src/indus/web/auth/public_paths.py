"""The one list of ``/api`` paths reachable without a credential (P18-02, R09.4).

Both gates — the loopback session-token gate and the gated-mode login gate — import
``PUBLIC_API_PATHS`` from here and match it exactly, so the two lists can never drift apart (a
test asserts both gates hold this very object). Every entry must be safe to show anyone who can
reach the port: liveness, coarse status, the config defaults and schema, model metadata, the theme
list, the dashboard plugin manifest list, the cron fire webhook (which carries its own
short-lived signed token, checked by its handler, P18-14) and the MCP OAuth callback (the browser
coming back from an IdP; ``state`` ties it to a sign-in a signed-in person started, P18-14).
Widening this list needs a D-id.

``LOGIN_PUBLIC_PREFIXES`` are what gated mode additionally lets through before login: the login
page and the auth round trip, the providers list the login page reads, and the static bundle files
the login page needs. They are not API data, so they are not on the shared list.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "LOGIN_PUBLIC_PREFIXES",
    "PUBLIC_API_PATHS",
    "TICKET_PATHS",
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
        "/api/mcp/oauth/callback",
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

TICKET_PATHS: Final[frozenset[str]] = frozenset({"/api/files/download", "/api/backups/download"})
"""REST routes that authenticate by their own single-use, path-bound ticket in the query (a
download opened in a new tab cannot set a header; ``auth.download_ticket``, P18-16: files and
backup archives). The loopback
gate hands them to their handler, which refuses anything without a good ticket; the gated mode
still wants the session cookie as well. No route takes the session token in the URL: ``?token=``
is refused on every ``/api`` path."""


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
