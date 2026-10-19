"""Browser sign-in with an authorization code, PKCE (RFC 7636, ``S256``) and a loopback redirect.

The order matters (R04.18): the callback server binds **first** (port 0 lets the OS choose, or a
provider's documented fixed port), so the redirect URI names a port this process already owns.
The authorize URL carries the S256 challenge and a random ``state``; the server answers until a
callback with that ``state`` brings a code or an error, or until the deadline (never under 5 s),
and it is always shut down. Callbacks on other paths get 404; callbacks with a wrong or missing
``state`` get 400, are counted, and are never exchanged.

On a remote session the person is shown the ``ssh -L`` line that forwards the callback port; a
browser is opened only when ``browser.should_open`` says one is likely. A provider whose fixed
port is busy falls back to its device flow when it documents one.

Exchanges:

- ``key_mint`` — OpenRouter's documented PKCE key mint: ``POST`` JSON ``{code, code_verifier,
  code_challenge_method}`` → ``{"key": …}``, an ordinary API key;
- ``token`` — the RFC 6749 §4.1.3 authorization-code grant with ``code_verifier`` → a
  ``TokenSet``.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import logging
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from indus.foundation.clock import Clock
from indus.foundation.errors import AuthError
from indus.providers.auth import browser
from indus.providers.auth.device_code import (
    DeviceFlowError,
    DeviceFlowSpec,
    OpenUrl,
    Say,
    TokenSet,
    run_device_login,
)

log = logging.getLogger(__name__)

LOOPBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
MIN_WAIT_SECONDS = 5.0
DEFAULT_WAIT_SECONDS = 300.0
WAIT_POLL_SECONDS = 0.1
VERIFIER_BYTES = 48  # 64 base64url characters, inside RFC 7636's 43..128

Exchange = Literal["key_mint", "token"]


class PkceError(AuthError):
    default_code = "auth.flow_failed"


class PkceDenied(PkceError):
    default_code = "auth.denied"


class CallbackTimeout(PkceError):
    default_code = "auth.callback_timeout"


class CallbackPortBusy(PkceError):
    default_code = "auth.port_busy"


class PkceExchangeFailed(PkceError):
    default_code = "auth.flow_failed"


# -- PKCE values -------------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_verifier() -> str:
    """A fresh code verifier: 64 characters from the unreserved set."""
    return _b64url(secrets.token_bytes(VERIFIER_BYTES))


def challenge_for(verifier: str) -> str:
    """``S256``: BASE64URL(SHA256(ASCII(verifier))) without padding."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def make_state() -> str:
    return secrets.token_urlsafe(24)


# -- the flow description ----------------------------------------------------------------------


@dataclass(frozen=True)
class PkceFlowSpec:
    """A provider's documented authorization-code + PKCE endpoints."""

    provider: str
    authorize_url: str
    exchange_url: str
    exchange: Exchange = "token"
    client_id: str = ""
    scope: str = ""
    fixed_port: int | None = None
    extra_params: Mapping[str, str] = field(default_factory=dict[str, str])
    fallback_device: DeviceFlowSpec | None = None
    doc_url: str = ""


@dataclass(frozen=True)
class PkceOutcome:
    """What a sign-in produced: an API key (key mint) or OAuth tokens."""

    api_key: str | None = None
    tokens: TokenSet | None = None
    via_device: bool = False

    def __repr__(self) -> str:
        kind = "api_key" if self.api_key else "tokens"
        return f"PkceOutcome(kind={kind!r}, via_device={self.via_device})"


@dataclass
class CallbackResult:
    code: str | None = None
    error: str | None = None
    error_description: str | None = None
    rejected: int = 0
    iss: str | None = None  # RFC 9207 issuer, when the server sends one


# -- the loopback server -----------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self) -> None:  # noqa: N802 (http.server's spelling)
        parts = urlsplit(self.path)
        owner = self.server.owner
        if parts.path != owner.path:
            self._reply(404, "Not found.")
            return
        params = {k: v[0] for k, v in parse_qs(parts.query).items() if v}
        if params.get("state") != owner.state:
            if params.get("error") and "code" not in params:
                # Some servers drop ``state`` on a refusal; a refusal carries nothing to steal.
                owner.finish(error=params["error"], description=params.get("error_description"))
                self._reply(200, "Sign-in was declined. You can close this tab.")
                return
            owner.reject()
            self._reply(400, "This sign-in link does not belong to the waiting session.")
            return
        if params.get("error"):
            owner.finish(error=params["error"], description=params.get("error_description"))
            self._reply(200, "Sign-in was declined. You can close this tab.")
            return
        code = params.get("code")
        if not code:
            owner.reject()
            self._reply(400, "The callback carried no authorization code.")
            return
        owner.finish(code=code, iss=params.get("iss"))
        self._reply(200, "Indus Agent received the sign-in. You can close this tab.")

    def _reply(self, status: int, text: str) -> None:
        body = f"<!doctype html><title>Indus Agent</title><p>{text}</p>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


class _CallbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], owner: LoopbackServer) -> None:
        self.owner = owner
        super().__init__(address, _Handler)


class LoopbackServer:
    """A one-shot HTTP listener on 127.0.0.1 for the authorization redirect."""

    def __init__(
        self, *, port: int = 0, path: str = CALLBACK_PATH, state: str | None = None
    ) -> None:
        self.path = path
        self.state = state or make_state()
        self.result = CallbackResult()
        self._done = threading.Event()
        self._lock = threading.Lock()
        try:
            self._server = _CallbackServer((LOOPBACK_HOST, port), self)
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES) or port:
                raise CallbackPortBusy(
                    f"port {port} on {LOOPBACK_HOST} is in use, so the sign-in callback cannot "
                    "be received",
                    detail=str(exc),
                ) from exc
            raise
        self.port: int = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="indus-auth-callback",
            daemon=True,
        )
        self._thread.start()
        self._closed = False

    @property
    def redirect_uri(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}{self.path}"

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def finish(
        self,
        *,
        code: str | None = None,
        error: str | None = None,
        description: str | None = None,
        iss: str | None = None,
    ) -> None:
        with self._lock:
            if self._done.is_set():
                return
            self.result.code = code
            self.result.iss = iss
            self.result.error = error
            self.result.error_description = description
            self._done.set()

    def reject(self) -> None:
        with self._lock:
            self.result.rejected += 1

    async def wait(self, seconds: float, clock: Clock) -> CallbackResult:
        """Until a callback with the right ``state`` arrives or ``seconds`` (at least 5) pass."""
        deadline = clock.monotonic() + max(MIN_WAIT_SECONDS, seconds)
        while not self._done.is_set():
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                refused = self.result.rejected
                extra = (
                    f" ({refused} callback(s) with the wrong sign-in state were refused)"
                    if refused
                    else ""
                )
                raise CallbackTimeout(
                    "no sign-in arrived from the browser before the time ran out" + extra
                )
            await clock.sleep(min(WAIT_POLL_SECONDS, remaining))
        return self.result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    def __enter__(self) -> LoopbackServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# -- exchange ----------------------------------------------------------------------------------


async def _exchange(
    client: httpx.AsyncClient,
    spec: PkceFlowSpec,
    code: str,
    verifier: str,
    redirect_uri: str,
    clock: Clock,
) -> PkceOutcome:
    try:
        if spec.exchange == "key_mint":
            response = await client.post(
                spec.exchange_url,
                json={"code": code, "code_verifier": verifier, "code_challenge_method": "S256"},
            )
        else:
            form = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }
            if spec.client_id:
                form["client_id"] = spec.client_id
            response = await client.post(spec.exchange_url, data=form)
    except httpx.HTTPError as exc:
        raise PkceExchangeFailed(
            f"could not reach {spec.provider} to finish the sign-in: {exc}"
        ) from exc
    status = response.status_code
    if status in (401, 403):
        raise PkceExchangeFailed(
            f"{spec.provider} refused the authorization code (it may be used or older than ten "
            "minutes); sign in again"
        )
    try:
        payload: object = response.json()
    except (ValueError, UnicodeDecodeError):
        payload = None
    if status >= 400 or not isinstance(payload, dict):
        raise PkceExchangeFailed(f"{spec.provider} did not finish the sign-in (HTTP {status})")
    data = cast(dict[str, Any], payload)
    if spec.exchange == "key_mint":
        key = data.get("key")
        if not isinstance(key, str) or not key.strip():
            raise PkceExchangeFailed(f"{spec.provider}'s answer carried no key")
        return PkceOutcome(api_key=key.strip())
    try:
        return PkceOutcome(tokens=TokenSet.from_response(data, clock.now()))
    except DeviceFlowError as exc:
        raise PkceExchangeFailed(str(exc)) from exc


def authorize_url(spec: PkceFlowSpec, *, redirect_uri: str, challenge: str, state: str) -> str:
    if spec.exchange == "key_mint":
        params: dict[str, str] = {"callback_url": redirect_uri}
    else:
        params = {"response_type": "code", "redirect_uri": redirect_uri}
        if spec.client_id:
            params["client_id"] = spec.client_id
        if spec.scope:
            params["scope"] = spec.scope
    params.update({"code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    params.update(spec.extra_params)
    joiner = "&" if "?" in spec.authorize_url else "?"
    return f"{spec.authorize_url}{joiner}{urlencode(params)}"


# -- the whole login ---------------------------------------------------------------------------


async def run_pkce_login(
    spec: PkceFlowSpec,
    *,
    clock: Clock,
    say: Say,
    env: Mapping[str, str],
    client: httpx.AsyncClient,
    open_url: OpenUrl | None = browser.open_url,
    no_browser: bool = False,
    timeout: float = DEFAULT_WAIT_SECONDS,
) -> PkceOutcome:
    """Run the browser sign-in; ``open_url`` None never opens anything (tests, ``--no-browser``)."""
    try:
        server = LoopbackServer(port=spec.fixed_port or 0)
    except CallbackPortBusy:
        if spec.fallback_device is None:
            raise
        say(f"Port {spec.fixed_port} is busy; signing in with a device code instead.")
        opener = (
            open_url
            if open_url is not None and browser.should_open(env, no_browser=no_browser)
            else None
        )
        tokens = await run_device_login(
            spec.fallback_device, clock=clock, say=say, open_url=opener, client=client
        )
        return PkceOutcome(tokens=tokens, via_device=True)
    with server:
        verifier = make_verifier()
        url = authorize_url(
            spec,
            redirect_uri=server.redirect_uri,
            challenge=challenge_for(verifier),
            state=server.state,
        )
        say(f"Open this address to sign in to {spec.provider}:")
        say(f"  {url}")
        if browser.is_remote_session(env):
            say("This looks like a remote session. Your browser must reach this machine's port")
            say(f"{server.port}; run this on your own computer first, then open the address:")
            say(f"  {browser.tunnel_hint(server.port, env)}")
        if open_url is not None and browser.should_open(env, no_browser=no_browser):
            say(
                "(A browser window was opened for you.)"
                if open_url(url)
                else "(No browser could be opened; use the address above.)"
            )
        say("Waiting for the browser…")
        result = await server.wait(timeout, clock)
        redirect_uri = server.redirect_uri
    if result.error:
        detail = f": {result.error_description}" if result.error_description else ""
        raise PkceDenied(f"the sign-in was not completed ({result.error}){detail}")
    assert result.code is not None
    return await _exchange(client, spec, result.code, verifier, redirect_uri, clock)
