"""Login, logout and identity routes (P18-03, P18-04).

- ``GET /login`` (public): the plain sign-in page — the providers, a username/password form;
- ``GET /api/auth/providers`` (public): the interactive providers and the gate flag;
- ``GET /auth/login?provider=&next=`` (public): a redirect provider's start, 302 to the IdP with
  the PKCE cookie set;
- ``GET /auth/callback`` (public): completes a redirect login and sets the session cookies;
- ``POST /auth/password-login`` (public): JSON ``{provider?, username, password, next?}`` or a
  form post;
- ``POST /auth/logout`` (public): revokes where possible, clears every auth cookie, 302 to the
  login page;
- ``GET /api/auth/me`` (authenticated): who am I (loopback: a fixed local identity).

In loopback mode there is nothing to sign in to: ``/login`` redirects to the app and the ``/auth``
routes answer 404. Password logins are limited to ``LOGIN_LIMIT`` attempts per ``LOGIN_WINDOW_S``
per client address (429), and an unknown user and a wrong password get the same 401 body. A
redirect login keeps its verifier, state, nonce, provider and landing path only in the PKCE cookie;
the callback trusts nothing else (the IdP echoes back only the code and the state). Every outcome is
written to the audit log without tokens, codes or passwords.

The login page itself is ``auth.login_page`` (no script, a strict CSP). A browser form post that
fails comes back to it with an error code (``1`` for wrong credentials, ``rate_limited``,
``unavailable``); JSON clients get the 401 / 429 / 503 bodies.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Any, Final
from urllib.parse import parse_qsl, quote, unquote

import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from indus.web.auth import cookies
from indus.web.auth.audit import AuditEvent
from indus.web.auth.gate import request_prefix, safe_next, session_of
from indus.web.auth.login_page import login_headers, render_login_page
from indus.web.auth.providers import (
    BadCredentials,
    LoginRejected,
    ProviderUnavailable,
    Session,
)
from indus.web.auth.proxy import callback_url, client_ip
from indus.web.state import DashboardState, dashboard_state

__all__ = ["LOGIN_LIMIT", "LOGIN_WINDOW_S", "LoginLimiter", "router"]

log = logging.getLogger(__name__)

router = APIRouter()

LOGIN_LIMIT: Final = 10
LOGIN_WINDOW_S: Final = 60.0
MAX_BUCKETS: Final = 4096
MAX_FORM_BYTES: Final = 16 * 1024
_NO_STORE: Final = {"cache-control": "no-store"}
_GENERIC_401: Final = {"error": "invalid_credentials", "detail": "Wrong username or password."}


class LoginLimiter:
    """A sliding window of attempts per client address (bounded number of addresses)."""

    def __init__(
        self,
        *,
        limit: int = LOGIN_LIMIT,
        window: float = LOGIN_WINDOW_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self._now = monotonic
        self._lock = threading.Lock()
        self._buckets: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, address: str) -> bool:
        """Record one attempt; False when ``address`` used up its window."""
        key = address or "?"
        now = self._now()
        cutoff = now - self.window
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                while len(self._buckets) >= MAX_BUCKETS:
                    self._buckets.popitem(last=False)
                bucket = deque()
                self._buckets[key] = bucket
            else:
                self._buckets.move_to_end(key)
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True


def _state(request: Request) -> DashboardState:
    return dashboard_state(request.app)


def _limiter(state: DashboardState) -> LoginLimiter:
    found = state.extras.get("login_limiter")
    if not isinstance(found, LoginLimiter):
        found = LoginLimiter()
        state.extras["login_limiter"] = found
    return found


def _not_gated() -> JSONResponse:
    return JSONResponse(
        {"error": "not_gated", "detail": "This dashboard runs on loopback; there is no sign-in."},
        status_code=404,
        headers=_NO_STORE,
    )


def _audit(request: Request, event: AuditEvent, **fields: object) -> None:
    _state(request).auth.audit.record(event, ip=client_ip(request.scope), **fields)


def _write_session(response: Response, request: Request, session: Session) -> None:
    state = _state(request)
    cookies.set_session(
        response,
        session_token=session.token,
        max_age=session.expires_at - state.auth.signer.now(),
        refresh_token=session.refresh_token,
        provider=session.provider,
        https=cookies.is_https(request),
        prefix=request_prefix(request.scope),
    )


def _landing(request: Request, raw: str) -> str:
    """Where to go after login: a safe same-origin path (with the proxy prefix), else the app."""
    prefix = request_prefix(request.scope)
    target = safe_next(unquote(raw)) if raw else ""
    if target and prefix and not (target == prefix or target.startswith(prefix + "/")):
        target = prefix + target
    return target or f"{prefix}/"


@router.get("/login", include_in_schema=False)
async def login_page(request: Request) -> Response:
    state = _state(request)
    prefix = request_prefix(request.scope)
    if state.mode == "serve":
        return JSONResponse(
            {"error": "not_found", "detail": "The headless backend has no sign-in page."},
            status_code=404,
            headers=_NO_STORE,
        )
    if not state.gated:
        return RedirectResponse(f"{prefix}/", status_code=302)
    next_path = safe_next(unquote(request.query_params.get("next", "")))
    page = render_login_page(
        providers=state.auth.registry.interactive(),
        prefix=prefix,
        next_path=next_path,
        error=request.query_params.get("error", ""),
    )
    return HTMLResponse(page, headers=login_headers())


@router.get("/api/auth/providers")
async def providers_list(request: Request) -> JSONResponse:
    state = _state(request)
    providers = state.auth.registry.interactive()
    return JSONResponse(
        {
            "auth_required": state.gated,
            "providers": [provider.describe() for provider in providers],
        },
        headers=_NO_STORE,
    )


@router.get("/auth/login", include_in_schema=False)
async def start_login(request: Request, provider: str = "", next: str = "") -> Response:
    state = _state(request)
    if not state.gated:
        return _not_gated()
    found = state.auth.registry.get(provider)
    if found is None or not found.interactive:
        return JSONResponse(
            {"error": "unknown_provider", "detail": "No such sign-in method."},
            status_code=404,
            headers=_NO_STORE,
        )
    prefix = request_prefix(request.scope)
    landing = safe_next(unquote(next)) if next else ""
    if found.kind == "password":
        target = f"{prefix}/login"
        if landing:
            target += f"?next={quote(landing, safe='')}"
        return RedirectResponse(target, status_code=302)
    redirect_uri = callback_url(
        state.public,
        scheme=request.url.scheme,
        host_header=request.headers.get("host", ""),
        prefix=prefix,
    )
    try:
        started = await anyio.to_thread.run_sync(found.start_login, redirect_uri)
    except ProviderUnavailable as exc:
        _audit(request, AuditEvent.LOGIN_FAILURE, provider=found.name, reason="unavailable")
        return JSONResponse(
            {"error": "provider_unavailable", "detail": str(exc)},
            status_code=503,
            headers=_NO_STORE,
        )
    response = RedirectResponse(started.url, status_code=302)
    kept = {**started.keep, "provider": found.name}
    if landing:
        kept["next"] = landing
    cookies.set_pkce(response, kept, https=cookies.is_https(request), prefix=prefix)
    _audit(request, AuditEvent.LOGIN_START, provider=found.name)
    return response


@router.get("/auth/callback", include_in_schema=False)
async def finish_login(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> Response:
    app_state = _state(request)
    if not app_state.gated:
        return _not_gated()
    kept = cookies.decode_payload(cookies.read(request, cookies.PKCE_COOKIE))
    prefix = request_prefix(request.scope)
    provider = app_state.auth.registry.get(kept.get("provider", ""))

    def refused(reason: str, status: int = 400) -> JSONResponse:
        _audit(
            request,
            AuditEvent.LOGIN_FAILURE,
            provider=provider.name if provider is not None else "",
            reason=reason,
        )
        response = JSONResponse(
            {"error": "login_failed", "detail": f"Sign-in could not be completed ({reason})."},
            status_code=status,
            headers=_NO_STORE,
        )
        cookies.clear_pkce(response, prefix=prefix)
        return response

    if not kept or provider is None or provider.kind != "redirect":
        return refused("no_login_in_progress")
    if error:
        return refused("provider_error")
    expected = kept.get("state", "")
    if not state or not expected or not _same(state, expected):
        return refused("state_mismatch")
    redirect_uri = callback_url(
        app_state.public,
        scheme=request.url.scheme,
        host_header=request.headers.get("host", ""),
        prefix=prefix,
    )
    try:
        session = await anyio.to_thread.run_sync(provider.complete_login, code, kept, redirect_uri)
    except LoginRejected:
        return refused("rejected")
    except ProviderUnavailable:
        return refused("provider_unavailable", status=503)
    _audit(request, AuditEvent.LOGIN_SUCCESS, provider=provider.name, user_id=session.user_id)
    response = RedirectResponse(_landing(request, kept.get("next", "")), status_code=302)
    _write_session(response, request, session)
    cookies.clear_pkce(response, prefix=prefix)
    cookies.clear_sso_attempt(response, prefix=prefix)
    return response


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


async def _login_body(request: Request) -> tuple[dict[str, str], bool] | None:
    """The fields of a JSON or urlencoded login body, and whether it was a form post."""
    raw = await request.body()
    if len(raw) > MAX_FORM_BYTES:
        return None
    kind = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if kind == "application/x-www-form-urlencoded":
        fields = dict(parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True))
        return fields, True
    try:
        data: Any = json.loads(raw or b"{}")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    fields = {str(key): value for key, value in data.items() if isinstance(value, str)}
    return fields, False


def _back_to_login(prefix: str, code: str, raw_next: str) -> RedirectResponse:
    """A failed browser form post: back to the login page with a fixed error code."""
    target = f"{prefix}/login?error={code}"
    landing = safe_next(raw_next)
    if landing:
        target += f"&next={quote(landing, safe='')}"
    return RedirectResponse(target, status_code=303, headers=_NO_STORE)


@router.post("/auth/password-login", include_in_schema=False)
async def password_login(request: Request) -> Response:
    state = _state(request)
    if not state.gated:
        return _not_gated()
    prefix = request_prefix(request.scope)
    parsed = await _login_body(request)
    if parsed is None:
        return JSONResponse(
            {"error": "bad_request", "detail": "Send a username and a password."},
            status_code=400,
            headers=_NO_STORE,
        )
    fields, is_form = parsed
    name = fields.get("provider") or "basic"
    if not _limiter(state).allow(client_ip(request.scope)):
        _audit(request, AuditEvent.LOGIN_FAILURE, provider=name, reason="rate_limited")
        if is_form:
            return _back_to_login(prefix, "rate_limited", fields.get("next", ""))
        return JSONResponse(
            {"error": "rate_limited", "detail": "Too many sign-in attempts; wait a minute."},
            status_code=429,
            headers={**_NO_STORE, "retry-after": str(int(LOGIN_WINDOW_S))},
        )
    provider = state.auth.registry.get(name)
    if provider is None or provider.kind != "password":
        _audit(request, AuditEvent.LOGIN_FAILURE, provider=name, reason="unknown_provider")
        return JSONResponse(
            {"error": "unknown_provider", "detail": "No such sign-in method."},
            status_code=404,
            headers=_NO_STORE,
        )
    username = fields.get("username", "")[:256]
    password = fields.get("password", "")[:4096]
    try:
        session = await anyio.to_thread.run_sync(provider.password_login, username, password)
    except BadCredentials:
        _audit(request, AuditEvent.LOGIN_FAILURE, provider=name, reason="invalid_credentials")
        if is_form:
            return _back_to_login(prefix, "1", fields.get("next", ""))
        return JSONResponse(_GENERIC_401, status_code=401, headers=_NO_STORE)
    except ProviderUnavailable:
        _audit(request, AuditEvent.LOGIN_FAILURE, provider=name, reason="provider_unavailable")
        if is_form:
            return _back_to_login(prefix, "unavailable", fields.get("next", ""))
        return JSONResponse(
            {"error": "provider_unavailable", "detail": "Try again shortly."},
            status_code=503,
            headers=_NO_STORE,
        )
    _audit(request, AuditEvent.LOGIN_SUCCESS, provider=name, user_id=session.user_id)
    landing = _landing(request, fields.get("next", ""))
    response: Response
    if is_form:
        response = RedirectResponse(landing, status_code=303, headers=_NO_STORE)
    else:
        response = JSONResponse({"ok": True, "next": landing}, headers=_NO_STORE)
    _write_session(response, request, session)
    return response


@router.post("/auth/logout", include_in_schema=False)
async def logout(request: Request) -> Response:
    state = _state(request)
    if not state.gated:
        return _not_gated()
    prefix = request_prefix(request.scope)
    refresh = cookies.read(request, cookies.RT_COOKIE)
    at = cookies.read(request, cookies.AT_COOKIE)
    session = state.auth.signer.verify(at, allow_expired=True) if at else None
    if refresh:
        for provider in state.auth.registry.interactive():
            if session is None or provider.name == session.provider:
                try:
                    await anyio.to_thread.run_sync(provider.revoke, refresh)
                except Exception as exc:  # best effort: logout always clears the cookies
                    log.warning(
                        "logout: revoking at %s failed (%s)", provider.name, type(exc).__name__
                    )
    _audit(
        request,
        AuditEvent.LOGOUT,
        provider=session.provider if session is not None else "",
        user_id=session.user_id if session is not None else "",
    )
    response = RedirectResponse(f"{prefix}/login", status_code=302, headers=_NO_STORE)
    cookies.clear_all(response, prefix=prefix)
    return response


@router.get("/api/auth/me")
async def who_am_i(request: Request) -> JSONResponse:
    state = _state(request)
    if not state.gated:
        return JSONResponse(
            {
                "user_id": "local",
                "email": "",
                "display_name": "This computer",
                "org_id": "",
                "provider": "loopback",
                "expires_at": 0,
            },
            headers=_NO_STORE,
        )
    session = session_of(request)
    if session is None:
        return JSONResponse(
            {
                "error": "unauthenticated",
                "detail": "Sign in to use the dashboard.",
                "login_url": f"{request_prefix(request.scope)}/login",
            },
            status_code=401,
            headers=_NO_STORE,
        )
    return JSONResponse(session.public(), headers=_NO_STORE)
