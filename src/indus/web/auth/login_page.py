"""The server-rendered sign-in page for gated mode (P18-28).

``render_login_page`` turns the interactive providers into one plain HTML page:

- a ``password`` provider → a form that POSTs ``provider``, ``username``, ``password`` and ``next``
  to ``<prefix>/auth/password-login`` (the route answers with a 303: to the landing page, or back
  here with an error code);
- a ``redirect`` provider (OIDC) → a link to ``<prefix>/auth/login?provider=<name>&next=<path>``,
  which starts the PKCE round trip. It is a link, not a form, because ``form-action`` would also
  have to allow the identity provider the start redirects to.

The page has no script and nothing injected: no session token, no runtime globals, no bundle
assets. ``login_headers()`` gives the headers it must be sent with — a CSP that allows nothing but
the one inline stylesheet (by its SHA-256 hash) and forms posting back to this origin, no framing,
no caching, and a referrer only for this origin (``same-origin``: with ``no-referrer`` a browser
sends ``Origin: null`` on the form's POST, which the gate's cross-site check must refuse).
Messages come from ``LOGIN_ERRORS`` by code; nothing from the URL is shown except the (escaped,
already validated) landing path inside hidden fields and links.
"""

from __future__ import annotations

import base64
import hashlib
import html
from collections.abc import Sequence
from typing import Final
from urllib.parse import quote

from indus.web.auth.providers import Provider

__all__ = [
    "LOGIN_CSP",
    "LOGIN_ERRORS",
    "LOGIN_STYLE",
    "login_headers",
    "render_login_page",
]

LOGIN_ERRORS: Final[dict[str, str]] = {
    "invalid": "That username and password did not match. Try again.",
    "rate_limited": "Too many sign-in attempts from here. Wait a minute, then try again.",
    "unavailable": "The sign-in service did not answer. Try again shortly.",
}
"""Error codes the login routes send back in ``?error=``; any other value shows nothing."""

ERROR_ALIASES: Final[dict[str, str]] = {"1": "invalid"}
"""``?error=1`` is what the password route sends for wrong credentials (kept from P18-03)."""

# Our light/dark pair, the @indus/ui semantic token values (bg, bg-raised, fg, fg-muted, border,
# accent, accent-fg, danger, focus-ring). System font stacks only: no font files (R09.22).
LOGIN_STYLE: Final = (
    ":root{color-scheme:light dark;--bg:#f8f6f5;--card:#ffffff;--fg:#312d29;--muted:#5d5750;"
    "--line:#918980;--accent:#007c9a;--accent-fg:#ffffff;--danger:#c23539;--focus:#009abd}"
    "@media (prefers-color-scheme: dark){:root{--bg:#1d1a17;--card:#312d29;--fg:#f8f6f5;"
    "--muted:#c6c0b9;--line:#766f67;--accent:#3db6da;--accent-fg:#1d1a17;--danger:#ff7470;"
    "--focus:#76cfed}}"
    "*{box-sizing:border-box}"
    "html,body{margin:0;min-height:100%;background:var(--bg);color:var(--fg);"
    "font:15px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif}"
    "main{max-width:24rem;margin:12vh auto 2rem;padding:0 1rem}"
    "h1{font-size:1.35rem;margin:0 0 .25rem}"
    ".lead{color:var(--muted);margin:0 0 1.25rem}"
    ".card{background:var(--card);border:1px solid var(--line);border-radius:.6rem;"
    "padding:1.1rem;margin:0 0 .9rem}"
    "label{display:block;font-size:.85rem;color:var(--muted);margin:.6rem 0 .2rem}"
    "input{display:block;width:100%;padding:.5rem .6rem;font:inherit;color:var(--fg);"
    "background:var(--bg);border:1px solid var(--line);border-radius:.4rem}"
    ".go{display:block;width:100%;margin-top:1rem;padding:.55rem .8rem;font:inherit;"
    "font-weight:600;text-align:center;text-decoration:none;color:var(--accent-fg);"
    "background:var(--accent);border:0;border-radius:.4rem;cursor:pointer}"
    "input:focus-visible,.go:focus-visible{outline:2px solid var(--focus);outline-offset:2px}"
    ".err{color:var(--danger);border-color:var(--danger)}"
    ".none{color:var(--muted)}"
)

_STYLE_HASH: Final = base64.b64encode(hashlib.sha256(LOGIN_STYLE.encode("utf-8")).digest()).decode(
    "ascii"
)

LOGIN_CSP: Final = (
    "default-src 'none'; "
    f"style-src 'sha256-{_STYLE_HASH}'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'"
)


def login_headers() -> dict[str, str]:
    """The headers the login page is always sent with."""
    return {
        "cache-control": "no-store",
        "content-security-policy": LOGIN_CSP,
        "cross-origin-opener-policy": "same-origin",
        "referrer-policy": "same-origin",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
    }


def _attr(value: str) -> str:
    return html.escape(value, quote=True)


def _message(error: str) -> str:
    code = ERROR_ALIASES.get(error, error)
    return LOGIN_ERRORS.get(code, "")


def _password_form(provider: Provider, prefix: str, next_path: str) -> str:
    fields = [
        f'<form class="card" method="post" action="{_attr(prefix + "/auth/password-login")}">',
        f'<input type="hidden" name="provider" value="{_attr(provider.name)}">',
    ]
    if next_path:
        fields.append(f'<input type="hidden" name="next" value="{_attr(next_path)}">')
    ident = _attr(f"user-{provider.name}")
    secret = _attr(f"pass-{provider.name}")
    fields.extend(
        [
            f'<label for="{ident}">Username</label>',
            f'<input id="{ident}" name="username" autocomplete="username" '
            'autocapitalize="none" autocorrect="off" spellcheck="false" required>',
            f'<label for="{secret}">Password</label>',
            f'<input id="{secret}" name="password" type="password" '
            'autocomplete="current-password" required>',
            f'<button class="go" type="submit">Sign in with {html.escape(provider.display_name)}'
            "</button>",
            "</form>",
        ]
    )
    return "".join(fields)


def _redirect_link(provider: Provider, prefix: str, next_path: str) -> str:
    href = f"{prefix}/auth/login?provider={quote(provider.name, safe='')}"
    if next_path:
        href += f"&next={quote(next_path, safe='')}"
    return (
        f'<p class="card"><a class="go" href="{_attr(href)}">'
        f"Continue with {html.escape(provider.display_name)}</a></p>"
    )


def render_login_page(
    *,
    providers: Sequence[Provider],
    prefix: str = "",
    next_path: str = "",
    error: str = "",
) -> str:
    """The sign-in page (see the module notes).

    ``next_path`` must already be a safe same-origin path (``auth.gate.safe_next``); ``prefix`` is
    the trusted reverse-proxy prefix (``""`` at the root).
    """
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta name="referrer" content="same-origin">',
        "<title>Sign in · Indus Agent</title>",
        f"<style>{LOGIN_STYLE}</style>",
        "</head><body><main>",
        "<h1>Sign in to Indus Agent</h1>",
        '<p class="lead">This dashboard is reachable from beyond this computer, so it asks who '
        "you are first.</p>",
    ]
    message = _message(error)
    if message:
        parts.append(f'<p class="card err" role="alert">{html.escape(message)}</p>')
    if not providers:
        parts.append('<p class="card none">No sign-in method is set up for this dashboard.</p>')
    for provider in providers:
        if provider.kind == "password":
            parts.append(_password_form(provider, prefix, next_path))
        else:
            parts.append(_redirect_link(provider, prefix, next_path))
    parts.append("</main></body></html>")
    return "".join(parts)
