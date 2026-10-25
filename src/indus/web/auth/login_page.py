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
from indus.web.brand import BRAND_NAME, MARK_PATH, MARK_VIEWBOX

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

# One fixed dark look in both themes (W52-login-1; Indus AGI colours since W63): a brand-black base
# with a faint ember checkerboard and a glow from the top, a square card with a bevel, the mark and
# the INDUS / AGI wordmark above it, ember buttons with black text (white on ember fails AA).
# System font stacks only: no font files (R09.22), so the look comes from weights, capitals and
# letter-spacing. The entrance animation is CSS only and is switched off under reduced motion.
LOGIN_STYLE: Final = (
    ":root{color-scheme:dark;--base:#0a0a0a;--amber:#f24001;--card:#141110;--line:#3a1a0d;"
    "--field:#0e0c0b;--field-line:#5a2a14;--text:#ffffff;--soft:#afaca8;--label:#96928d;"
    "--form-title:#bbb8b5;--foot:#716c66;--danger:#ff6b6b}"
    "*{box-sizing:border-box}"
    "html{min-height:100%;background:var(--base)}"
    "body{margin:0;min-height:100vh;color:var(--text);"
    "font:15px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;"
    "background-color:var(--base);"
    "background-image:"
    "radial-gradient(ellipse 70% 55% at 50% 0,rgba(242,64,1,.10),transparent 100%),"
    "repeating-conic-gradient(rgba(242,64,1,.04) 0 25%,transparent 0 50%);"
    "background-size:100% 100%,6px 6px;background-attachment:fixed}"
    "main{width:26rem;max-width:calc(100% - 2rem);margin:0 auto;padding:6vh 0 3rem;"
    "animation:rise .6s ease-out both}"
    "@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}"
    "@media (prefers-reduced-motion:reduce){main{animation:none}}"
    ".mark{display:flex;align-items:center;justify-content:center;gap:12px;margin:4px 0 36px}"
    ".mark svg{width:40px;height:40px;fill:var(--amber)}"
    ".mark .word{color:var(--text);font-size:19px;font-weight:700;letter-spacing:.06em;"
    "line-height:.95;text-transform:uppercase}"
    ".card{background:var(--card);border:1px solid var(--line);padding:36px 32px 32px;"
    "box-shadow:inset 1px 1px 0 rgba(255,255,255,.04),inset -1px -1px 0 rgba(0,0,0,.35),"
    "0 24px 60px rgba(0,0,0,.45)}"
    "h1{margin:0;font-size:30px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;"
    "line-height:1.2}"
    ".lead{margin:6px 0 28px;color:var(--soft);font-size:15px}"
    ".err{margin:0 0 16px;color:var(--danger);font-size:13px}"
    ".none{margin:0;color:var(--soft)}"
    ".ways{display:flex;flex-direction:column;gap:12px}"
    "form{display:flex;flex-direction:column;gap:12px;margin:0}"
    ".form-title{margin:0;color:var(--form-title);font-size:11.5px;font-weight:600;"
    "letter-spacing:.18em;text-transform:uppercase}"
    "label{display:block;margin:0 0 6px;color:var(--label);font-size:11.5px;letter-spacing:.12em;"
    "text-transform:uppercase}"
    "input{display:block;width:100%;height:41px;padding:0 12px;font:inherit;font-size:15px;"
    "color:var(--text);background:var(--field);border:1px solid var(--field-line);border-radius:0;"
    "outline:none}"
    "input:focus{border-color:var(--amber);box-shadow:0 0 0 1px var(--amber)}"
    ".go{display:block;width:100%;padding:15px 16px;font:inherit;font-size:12.5px;font-weight:700;"
    "line-height:19px;letter-spacing:.2em;text-transform:uppercase;text-align:center;"
    "text-decoration:none;color:var(--base);background:var(--amber);border:0;border-radius:0;"
    "cursor:pointer;"
    "box-shadow:inset 1px 1px 0 rgba(255,255,255,.35),inset -1px -1px 0 rgba(0,0,0,.25)}"
    ".go:hover{filter:brightness(1.08)}"
    ".go:active{color:var(--amber);background:var(--base);box-shadow:inset 0 0 0 1px var(--amber)}"
    ".go:focus-visible{outline:2px solid var(--amber);outline-offset:3px}"
    "footer{display:flex;align-items:center;justify-content:center;gap:10px;margin-top:28px;"
    "color:var(--foot);font-size:12px;letter-spacing:.1em;text-transform:uppercase}"
    "footer::before,footer::after{content:'';width:24px;height:1px;background:rgba(242,64,1,.40)}"
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
        f'<form method="post" action="{_attr(prefix + "/auth/password-login")}">',
        f'<p class="form-title">{html.escape(provider.display_name)}</p>',
        f'<input type="hidden" name="provider" value="{_attr(provider.name)}">',
    ]
    if next_path:
        fields.append(f'<input type="hidden" name="next" value="{_attr(next_path)}">')
    ident = _attr(f"user-{provider.name}")
    secret = _attr(f"pass-{provider.name}")
    fields.extend(
        [
            f'<div><label for="{ident}">Username</label>',
            f'<input id="{ident}" name="username" autocomplete="username" '
            'autocapitalize="none" autocorrect="off" spellcheck="false" required></div>',
            f'<div><label for="{secret}">Password</label>',
            f'<input id="{secret}" name="password" type="password" '
            'autocomplete="current-password" required></div>',
            '<button class="go" type="submit">Sign in</button>',
            "</form>",
        ]
    )
    return "".join(fields)


def _redirect_link(provider: Provider, prefix: str, next_path: str) -> str:
    href = f"{prefix}/auth/login?provider={quote(provider.name, safe='')}"
    if next_path:
        href += f"&next={quote(next_path, safe='')}"
    return (
        f'<a class="go" href="{_attr(href)}">Sign in with {html.escape(provider.display_name)}</a>'
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
        f"<title>Sign in · {BRAND_NAME}</title>",
        f"<style>{LOGIN_STYLE}</style>",
        "</head><body><main>",
        f'<p class="mark"><svg viewBox="{MARK_VIEWBOX}" aria-hidden="true"><path d="{MARK_PATH}"/>'
        f'</svg><span class="word">Indus<br>AGI</span></p>',
        '<div class="card">',
        "<h1>Sign in</h1>",
        f'<p class="lead">Pick a way to sign in to this {BRAND_NAME} dashboard.</p>',
    ]
    message = _message(error)
    if message:
        parts.append(f'<p class="err" role="alert">{html.escape(message)}</p>')
    if not providers:
        parts.append('<p class="none">No sign-in method is set up for this dashboard.</p>')
    else:
        parts.append('<div class="ways">')
        for provider in providers:
            if provider.kind == "password":
                parts.append(_password_form(provider, prefix, next_path))
            else:
                parts.append(_redirect_link(provider, prefix, next_path))
        parts.append("</div>")
    parts.append("</div><footer>Remote access · Sign-in required</footer></main></body></html>")
    return "".join(parts)
