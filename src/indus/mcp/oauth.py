# pyright: strict
"""OAuth for remote MCP servers (``auth: oauth``, P13-09).

The SDK's ``OAuthClientProvider`` does the protocol: metadata discovery, client identification
(a Client ID Metadata Document when one is configured and the server supports it, else dynamic
registration, or a pre-registered client), PKCE, the code exchange, refresh and scope step-up.
This module supplies what sits around it:

- ``OAuthStore``: the SDK's token storage, kept in the profile's secret store (OS keychain, else
  the owner-only file; never config, never logs). Access token, refresh token and client secret
  are separate entries so each is redacted by value; the token record keeps an absolute expiry
  so a restarted process does not send a dead token as valid.
- ``IndusOAuthProvider``: the SDK provider plus (1) a refresh fence per server: a request that
  would refresh re-reads the store under the fence first (another session may have refreshed),
  and no second refresh is sent within ``FENCE_SECONDS``; (2) background mode: when the server
  answers 401/403 and only a new authorization could help, the flow stops with
  ``McpAuthRequired`` before any registration or browser (R07.24); (3) the per-provider lock is
  not held while the MCP request itself is in flight.
- ``login`` (browser, loopback callback) and ``device_login`` (RFC 8628) for ``indus mcp login``;
  ``logout`` removes what is stored for a server.

Only explicit logins are interactive. Handlers built for sessions (``auth_for``) are always
background ones, whatever the terminal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_field_from_www_auth,
)
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from pydantic import ValidationError

from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.foundation.secrets import open_secret_store
from indus.mcp.config import McpError, McpServer, OAuthSettings, remote_endpoint
from indus.providers.auth import browser
from indus.providers.auth.device_code import (
    DeviceFlowError,
    DeviceFlowSpec,
    TokenSet,
    run_device_login,
)
from indus.providers.auth.pkce import LOOPBACK_HOST, CallbackTimeout, LoopbackServer

__all__ = [
    "CLIENT_NAME",
    "FENCE_SECONDS",
    "LOGIN_HEADROOM_S",
    "IndusOAuthProvider",
    "LoginOutcome",
    "LoginUi",
    "McpAuthRequired",
    "McpOAuthFailed",
    "OAuthStore",
    "SecretWriter",
    "auth_for",
    "credential_stamp",
    "device_login",
    "forward_hint",
    "login",
    "login_timeout",
    "logout",
    "secret_prefix",
    "secrets_for",
]

log = logging.getLogger(__name__)

CLIENT_NAME = "Indus Agent"
FENCE_SECONDS = 60.0
LOGIN_HEADROOM_S = 15.0
CALLBACK_PATH = "/callback"
_SAFE = re.compile(r"[^A-Za-z0-9]+")


# -- errors -------------------------------------------------------------------------------------


class McpAuthRequired(McpError, PermissionError):
    """The server needs a (new) authorization that only an interactive login can give.

    A ``PermissionError`` so the connect layer treats it as permanent and parks the server
    instead of retrying a token the server rejected (R07.24).
    """

    default_code = "mcp.auth_required"

    def __init__(self, message: str, *, server: str = "", hint: str | None = None) -> None:
        McpError.__init__(self, message, server=server, hint=hint)


class McpOAuthFailed(McpError):
    """An interactive login could not finish (refused, timed out, server without OAuth)."""

    default_code = "mcp.oauth_failed"


def _login_hint(name: str) -> str:
    return f"run `indus mcp login {name}` in a terminal, then /reload-mcp"


# -- secret storage -----------------------------------------------------------------------------


class SecretWriter(Protocol):
    """What the OAuth store needs from a secret store (``SecretStore`` fits)."""

    def get(self, name: str, /) -> str | None: ...

    def set(self, name: str, value: str, /) -> None: ...

    def delete(self, name: str, /) -> bool: ...


def secrets_for(scope: Scope) -> SecretWriter:
    """The profile's writable secret store: the scope's own when it can write, else opened as
    ``security.secret_store`` says."""
    held = scope.secrets
    writable = callable(getattr(held, "set", None)) and callable(getattr(held, "delete", None))
    if held is not None and writable:
        return cast(SecretWriter, held)
    mode = "auto"
    if scope.config is not None:
        try:
            mode = str(scope.config.get("security.secret_store") or "auto")
        except Exception:
            mode = "auto"
    return open_secret_store(scope.home, scope.profile_id, mode=mode, clock=scope.clock)


def secret_prefix(server: str) -> str:
    """``MCP_OAUTH_<SAFE>_<sha8>``: secret names allow only letters, digits and ``_``, so the
    server name is folded and a short hash keeps two similar names apart."""
    safe = _SAFE.sub("_", server).strip("_").upper()[:40] or "SERVER"
    digest = hashlib.sha256(server.encode("utf-8")).hexdigest()[:8].upper()
    return f"MCP_OAUTH_{safe}_{digest}"


@dataclass
class _Shared:
    """In-process state of one server's OAuth across every provider built for it: the refresh
    fence, a generation bumped on every token write, and when the last refresh went out."""

    fence: anyio.Semaphore = field(default_factory=lambda: anyio.Semaphore(1, max_value=1))
    generation: int = 0
    last_refresh_at: float | None = None


_shared_lock = threading.Lock()
_shared: dict[tuple[str, str], _Shared] = {}


def _shared_for(home: Path, server: str) -> _Shared:
    key = (str(home), server)
    with _shared_lock:
        found = _shared.get(key)
        if found is None:
            found = _shared[key] = _Shared()
        return found


def reset_shared_state() -> None:
    """Forget fences and generations (tests only)."""
    with _shared_lock:
        _shared.clear()


def _loads(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


_PARTS = ("ACCESS", "REFRESH", "TOKEN", "CLIENT", "CLIENT_SECRET", "META")


def _dumps(data: Mapping[str, Any]) -> str:
    return json.dumps(dict(data), sort_keys=True, separators=(",", ":"))


class OAuthStore:
    """One server's OAuth state in the profile's secret store (the SDK's ``TokenStorage``)."""

    def __init__(
        self,
        secrets: SecretWriter,
        server: str,
        *,
        clock: Clock,
        home: Path,
        preregistered: OAuthClientInformationFull | None = None,
    ) -> None:
        self.server = server
        self._secrets = secrets
        self._clock = clock
        self._prefix = secret_prefix(server)
        self._prereg = preregistered
        self.shared = _shared_for(home, server)
        self.loaded_generation = -1

    def __repr__(self) -> str:
        return f"OAuthStore(server={self.server!r})"

    def _name(self, part: str) -> str:
        return f"{self._prefix}_{part}"

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._name(p) for p in _PARTS)

    def _get(self, part: str) -> str | None:
        try:
            return self._secrets.get(self._name(part))
        except Exception as exc:
            raise McpOAuthFailed(
                f"MCP server {self.server!r}: the secret store could not be read "
                f"({type(exc).__name__})",
                server=self.server,
            ) from exc

    def _put(self, part: str, value: str | None) -> None:
        name = self._name(part)
        try:
            if value is None:
                self._secrets.delete(name)
            else:
                self._secrets.set(name, value)
        except Exception as exc:
            raise McpOAuthFailed(
                f"MCP server {self.server!r}: the secret store could not be written "
                f"({type(exc).__name__})",
                server=self.server,
            ) from exc

    # -- tokens ---------------------------------------------------------------------------------

    def _client_id(self) -> str | None:
        if self._prereg is not None:
            return self._prereg.client_id
        record = _loads(self._get("CLIENT"))
        value = record.get("client_id") if record else None
        return str(value) if value else None

    def has_tokens(self) -> bool:
        return bool(self._get("ACCESS"))

    def load_tokens(self) -> OAuthToken | None:
        """The stored tokens with ``expires_in`` counted from now; tokens minted for another
        client than the configured pre-registered one are dropped."""
        self.loaded_generation = self.shared.generation
        access = self._get("ACCESS")
        record = _loads(self._get("TOKEN"))
        if not access or record is None:
            return None
        minted_for = record.get("client_id")
        if self._prereg is not None and minted_for and minted_for != self._prereg.client_id:
            log.warning(
                "MCP server %r: the configured OAuth client changed; the old sign-in was dropped",
                self.server,
            )
            self.clear_tokens()
            return None
        expires_in: int | None = None
        expires_at = record.get("expires_at")
        if isinstance(expires_at, int | float) and not isinstance(expires_at, bool):
            expires_in = max(0, int(expires_at - self._clock.now().timestamp()))
        data: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "scope": record.get("scope"),
            "refresh_token": self._get("REFRESH"),
        }
        try:
            return OAuthToken.model_validate(data)
        except ValidationError:
            log.warning("MCP server %r: the stored OAuth tokens are unreadable", self.server)
            return None

    def save_tokens(self, tokens: OAuthToken) -> None:
        record: dict[str, Any] = {"saved_at": self._clock.now().timestamp()}
        if tokens.expires_in is not None:
            record["expires_at"] = self._clock.now().timestamp() + int(tokens.expires_in)
        if tokens.scope:
            record["scope"] = tokens.scope
        client_id = self._client_id()
        if client_id:
            record["client_id"] = client_id
        self._put("ACCESS", tokens.access_token)
        self._put("REFRESH", tokens.refresh_token or None)
        self._put("TOKEN", _dumps(record))
        self.shared.generation += 1
        self.loaded_generation = self.shared.generation

    def clear_tokens(self) -> None:
        for part in ("ACCESS", "REFRESH", "TOKEN"):
            self._put(part, None)
        self.shared.generation += 1

    def snapshot(self) -> dict[str, str | None]:
        """Everything stored for this server, to put back with ``restore`` (a failed login)."""
        return {part: self._get(part) for part in _PARTS}

    def restore(self, saved: Mapping[str, str | None]) -> None:
        for part in _PARTS:
            self._put(part, saved.get(part))
        self.shared.generation += 1

    def stamp(self) -> str | None:
        """Changes whenever a sign-in or refresh stores tokens (P13-07's credential stamp)."""
        record = self._get("TOKEN")
        if not record:
            return None
        return hashlib.sha256(record.encode("utf-8")).hexdigest()[:16]

    # -- client and metadata --------------------------------------------------------------------

    def load_client(self) -> OAuthClientInformationFull | None:
        if self._prereg is not None:
            return self._prereg
        record = _loads(self._get("CLIENT"))
        if record is None:
            return None
        secret = self._get("CLIENT_SECRET")
        if secret:
            record["client_secret"] = secret
        try:
            return OAuthClientInformationFull.model_validate(record)
        except ValidationError:
            log.warning("MCP server %r: the stored OAuth client is unreadable", self.server)
            return None

    def save_client(self, info: OAuthClientInformationFull) -> None:
        if self._prereg is not None:
            return  # a pre-registered client lives in config; nothing to remember
        data = info.model_dump(mode="json", exclude_none=True)
        secret = data.pop("client_secret", None)
        self._put("CLIENT", _dumps(data))
        self._put("CLIENT_SECRET", str(secret) if secret else None)

    def load_metadata(self) -> OAuthMetadata | None:
        record = _loads(self._get("META"))
        if record is None:
            return None
        try:
            return OAuthMetadata.model_validate(record)
        except ValidationError:
            return None

    def save_metadata(self, metadata: OAuthMetadata) -> None:
        self._put("META", _dumps(metadata.model_dump(mode="json", exclude_none=True)))

    def clear(self) -> int:
        """Remove everything stored for this server; returns how many entries existed."""
        removed = 0
        for name in self.names:
            try:
                if self._secrets.delete(name):
                    removed += 1
            except Exception as exc:
                log.debug("could not remove %s: %s", name, type(exc).__name__)
        self.shared.generation += 1
        return removed

    def cached_redirect_port(self) -> int | None:
        """The loopback port of the stored registration (a dynamic client is bound to it)."""
        if self._prereg is not None:
            return None
        record = _loads(self._get("CLIENT"))
        uris = record.get("redirect_uris") if record else None
        for raw in cast(list[object], uris) if isinstance(uris, list) else []:
            try:
                parts = urlsplit(str(raw))
                port = parts.port
            except ValueError:
                continue
            if parts.scheme == "http" and port and parts.hostname in (LOOPBACK_HOST, "localhost"):
                return port
        return None

    # -- the SDK's TokenStorage -----------------------------------------------------------------

    async def get_tokens(self) -> OAuthToken | None:
        return self.load_tokens()

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.save_tokens(tokens)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.load_client()

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.save_client(client_info)


# -- the provider -------------------------------------------------------------------------------


def _is_refresh(request: httpx2.Request) -> bool:
    if request.method != "POST":
        return False
    try:
        body = request.content.decode("utf-8", "replace")
    except Exception:
        return False
    return "grant_type=refresh_token" in body


def _needs_authorization(response: httpx2.Response) -> bool:
    if response.status_code == 401:
        return True
    return (
        response.status_code == 403
        and extract_field_from_www_auth(response, "error") == "insufficient_scope"
    )


class IndusOAuthProvider(OAuthClientProvider):
    """The SDK provider with our refresh fence, background mode and stored expiry/metadata."""

    def __init__(
        self,
        *,
        server_name: str,
        server_url: str,
        client_metadata: OAuthClientMetadata,
        store: OAuthStore,
        clock: Clock,
        interactive: bool = False,
        redirect_handler: Callable[[str], Awaitable[None]] | None = None,
        callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]] | None = None,
        client_metadata_url: str | None = None,
    ) -> None:
        super().__init__(
            server_url,
            client_metadata,
            store,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            client_metadata_url=client_metadata_url,
        )
        # A semaphore is not owned by a task, so the flow may release it around the MCP request
        # and httpx may close the flow from another task.
        self.context.lock = cast(Any, anyio.Semaphore(1, max_value=1))
        self.server_name = server_name
        self.store = store
        self.clock = clock
        self.interactive = interactive
        self.refreshes_sent = 0

    async def _initialize(self) -> None:
        await super()._initialize()
        context = self.context
        tokens = context.current_tokens
        if tokens is not None and tokens.expires_in is not None:
            if tokens.expires_in <= 0:
                context.token_expiry_time = time.time() - 1
            else:
                context.update_token_expiry(tokens)
        if context.oauth_metadata is None:
            context.oauth_metadata = self.store.load_metadata()

    def _refresh_due(self) -> bool:
        context = self.context
        return not context.is_token_valid() and context.can_refresh_token()

    def _persist_metadata(self) -> None:
        metadata = self.context.oauth_metadata
        if metadata is None:
            return
        try:
            stored = self.store.load_metadata()
            if stored is None or stored.token_endpoint != metadata.token_endpoint:
                self.store.save_metadata(metadata)
        except McpOAuthFailed as exc:
            log.debug("MCP server %r: metadata not saved: %s", self.server_name, exc)

    def _refresh_recent(self) -> bool:
        last = self.store.shared.last_refresh_at
        return last is not None and self.clock.monotonic() - last < FENCE_SECONDS

    async def _enter_fence(self) -> tuple[bool, bool]:
        """Take the server's fence when this request may refresh (or must read the store); the
        store is read again under it. Returns (fence held, refresh held back): within
        ``FENCE_SECONDS`` of the last refresh the token is used without its refresh token for
        this one request."""
        shared = self.store.shared
        context = self.context
        usable = context.is_token_valid() or context.can_refresh_token()
        if self._initialized and (self.store.loaded_generation != shared.generation or not usable):
            # Another session wrote tokens, or ours are spent (a login in another process may
            # have stored new ones): read the store again.
            self._initialized = False
        if self._initialized and not self._refresh_due():
            return False, False
        await shared.fence.acquire()
        if not self._initialized or self.store.loaded_generation != shared.generation:
            await self._initialize()
        if self._refresh_due() and self._refresh_recent():
            tokens = self.context.current_tokens
            assert tokens is not None
            log.info(
                "MCP server %r: token refresh skipped (at most one per %.0f s)",
                self.server_name,
                FENCE_SECONDS,
            )
            self.context.current_tokens = tokens.model_copy(update={"refresh_token": None})
            return True, True
        return True, False

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        """The SDK's flow, bridged: the fence is held from before a refresh until the request
        goes out; the provider lock is let go while the request itself is in flight; in
        background mode a 401/403 that needs a new authorization ends the flow (after at most one
        refresh-and-resend) with ``McpAuthRequired``."""
        shared = self.store.shared
        lock = cast(anyio.Semaphore, self.context.lock)
        for attempt in (1, 2):
            refused: httpx2.Response | None = None
            fenced, held_back = await self._enter_fence()
            released = False
            sent_request = False
            inner = super().async_auth_flow(request)
            try:
                outgoing = await inner.__anext__()
                while True:
                    if outgoing is request:
                        sent_request = True
                        if fenced:
                            shared.fence.release()
                            fenced = False
                        lock.release()
                        released = True
                    elif not sent_request and _is_refresh(outgoing):
                        shared.last_refresh_at = self.clock.monotonic()
                        self.refreshes_sent += 1
                    incoming = yield outgoing
                    if released:
                        await lock.acquire()
                        released = False
                        if not self.interactive and _needs_authorization(incoming):
                            refused = incoming
                            break
                    outgoing = await inner.asend(incoming)
            except StopAsyncIteration:
                self._persist_metadata()
                return
            finally:
                if released:
                    with anyio.CancelScope(shield=True):
                        await lock.acquire()
                await inner.aclose()
                if fenced:
                    shared.fence.release()
                if held_back:
                    self._initialized = False  # the next request reads the refresh token again
            if attempt == 1 and self.context.can_refresh_token() and not self._refresh_recent():
                self.context.token_expiry_time = time.time() - 1  # refresh, then send again
                continue
            raise McpAuthRequired(
                f"MCP server {self.server_name!r} needs a new sign-in (HTTP {refused.status_code})",
                server=self.server_name,
                hint=_login_hint(self.server_name),
            )


# -- building handlers --------------------------------------------------------------------------


def _settings(server: McpServer) -> OAuthSettings:
    return server.oauth or OAuthSettings()


def _extra(settings: OAuthSettings, key: str) -> str | None:
    value = (settings.model_extra or {}).get(key)
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _redirect_uri(settings: OAuthSettings, port: int) -> str:
    host = (settings.redirect_host or LOOPBACK_HOST).strip() or LOOPBACK_HOST
    return f"http://{host}:{port}{CALLBACK_PATH}"


def _client_secret(name: str, settings: OAuthSettings, secrets: SecretWriter) -> str | None:
    ref = settings.client_secret_ref
    if not ref:
        return None
    value = secrets.get(ref)
    if value is None:
        raise McpOAuthFailed(
            f"MCP server {name!r}: oauth.client_secret_ref names {ref}, which is not stored",
            server=name,
            hint=f"store it with `indus auth add` or `indus config set` as {ref}",
        )
    return value


def _client_metadata(
    settings: OAuthSettings, redirect_uri: str, secret: bool
) -> OAuthClientMetadata:
    data: dict[str, Any] = {
        "client_name": settings.client_name or CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post" if secret else "none",
        "application_type": "native",
    }
    scope = _extra(settings, "scope")
    if scope:
        data["scope"] = scope
    return OAuthClientMetadata.model_validate(data)


def _preregistered(
    name: str, settings: OAuthSettings, redirect_uri: str, secrets: SecretWriter
) -> OAuthClientInformationFull | None:
    if not settings.client_id:
        return None
    secret = _client_secret(name, settings, secrets)
    data: dict[str, Any] = {
        "client_id": settings.client_id,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post" if secret else "none",
    }
    if secret:
        data["client_secret"] = secret
    if settings.client_name:
        data["client_name"] = settings.client_name
    return OAuthClientInformationFull.model_validate(data)


def _store_for(
    name: str,
    server: McpServer,
    scope: Scope,
    secrets: SecretWriter,
    clock: Clock,
    redirect_uri: str,
) -> OAuthStore:
    prereg = _preregistered(name, _settings(server), redirect_uri, secrets)
    return OAuthStore(secrets, name, clock=clock, home=scope.home, preregistered=prereg)


def _server_url(name: str, server: McpServer, scope: Scope) -> str:
    return remote_endpoint(name, server, scope).url


def auth_for(
    name: str,
    server: McpServer,
    scope: Scope,
    *,
    secrets: SecretWriter | None = None,
    clock: Clock | None = None,
) -> IndusOAuthProvider | None:
    """A background OAuth handler for a session's connect (``ConnectOptions.auth``), or None
    when the server does not use OAuth. It refreshes tokens but never starts a login."""
    if server.auth != "oauth" or not server.is_remote:
        return None
    writer = secrets if secrets is not None else secrets_for(scope)
    when = clock or scope.clock
    settings = _settings(server)
    probe = OAuthStore(writer, name, clock=when, home=scope.home)
    port = settings.redirect_port or probe.cached_redirect_port() or 0
    redirect_uri = (
        _redirect_uri(settings, port) if port else f"http://{LOOPBACK_HOST}{CALLBACK_PATH}"
    )
    store = _store_for(name, server, scope, writer, when, redirect_uri)
    secret = store.load_client()

    async def refuse(_url: str) -> None:
        raise McpAuthRequired(
            f"MCP server {name!r} needs a sign-in", server=name, hint=_login_hint(name)
        )

    return IndusOAuthProvider(
        server_name=name,
        server_url=_server_url(name, server, scope),
        client_metadata=_client_metadata(
            settings, redirect_uri, bool(secret is not None and secret.client_secret)
        ),
        store=store,
        clock=when,
        interactive=False,
        redirect_handler=refuse,
        client_metadata_url=_extra(settings, "client_metadata_url"),
    )


# -- interactive login --------------------------------------------------------------------------


class LoginUi(Protocol):
    def say(self, text: str) -> None: ...

    def open_url(self, url: str) -> bool: ...


def login_timeout(server: McpServer) -> float:
    """How long an interactive login's connect may take: the callback wait plus headroom for the
    exchange, or the server's connect timeout when that is longer."""
    return max(_settings(server).timeout + LOGIN_HEADROOM_S, server.connect_timeout)


def forward_hint(port: int, env: Mapping[str, str]) -> str | None:
    """The ``ssh -L`` line for a remote session (the browser is on another machine), else None."""
    if not browser.is_remote_session(env):
        return None
    return browser.tunnel_hint(port, env)


@dataclass(frozen=True)
class LoginOutcome:
    tools: tuple[str, ...]
    authorized: bool


Connector = Callable[[str, McpServer, Scope, IndusOAuthProvider], Awaitable[list[str]]]
"""Connects once with the given auth handler and returns the server's tool names."""


async def _connect_and_list(
    name: str, server: McpServer, scope: Scope, auth: IndusOAuthProvider
) -> list[str]:
    from indus.mcp.connect import Connection, ConnectOptions

    async with Connection(
        name, server, scope, clock=auth.clock, options=ConnectOptions(auth=auth)
    ) as live:
        return [tool.name for tool in await live.list_tools()]


async def login(
    name: str,
    server: McpServer,
    scope: Scope,
    *,
    ui: LoginUi,
    secrets: SecretWriter | None = None,
    clock: Clock | None = None,
    connect: Connector = _connect_and_list,
) -> LoginOutcome:
    """Authorize in a browser and connect once: the loopback callback is bound first, the
    authorization URL printed (and opened when a browser is likely), the ``ssh -L`` line shown
    on remote sessions; the wait is ``oauth.timeout``. The stored tokens are set aside for the
    login (so the SDK cannot skip it with them) and put back when it does not end with new ones:
    a failed, refused, timed-out or interrupted login leaves the previous sign-in in place."""
    if server.auth != "oauth" or not server.is_remote:
        raise McpOAuthFailed(f"MCP server {name!r} is not configured with auth: oauth", server=name)
    _require_interactive(name)
    writer = secrets if secrets is not None else secrets_for(scope)
    when = clock or scope.clock
    settings = _settings(server)
    stale = OAuthStore(writer, name, clock=when, home=scope.home)
    port = settings.redirect_port or stale.cached_redirect_port() or 0
    try:
        listener = LoopbackServer(port=port, path=CALLBACK_PATH)
    except Exception as exc:
        raise McpOAuthFailed(
            f"MCP server {name!r}: the sign-in callback port {port} is in use",
            server=name,
            hint="close the other sign-in or set a free oauth.redirect_port",
        ) from exc
    try:
        redirect_uri = _redirect_uri(settings, listener.port)
        store = _store_for(name, server, scope, writer, when, redirect_uri)
        saved = store.snapshot()
        store.clear_tokens()
        secret = store.load_client()

        async def announce(url: str) -> None:
            state = parse_qs(urlsplit(url).query).get("state", [""])[0]
            listener.state = state
            ui.say(f"Sign in to the MCP server {name!r}: open this address in a browser:")
            ui.say(f"  {url}")
            hint = forward_hint(listener.port, scope.env)
            if hint is not None:
                ui.say("This is a remote session, so the browser's redirect cannot reach this")
                ui.say("machine on its own. On your own computer, first run:")
                ui.say(f"  {hint}")
            if browser.should_open(scope.env) and ui.open_url(url):
                ui.say("(A browser window was opened.)")
            ui.say(f"Waiting up to {settings.timeout:g} s for the sign-in…")

        async def wait_for_code() -> AuthorizationCodeResult:
            try:
                result = await listener.wait(settings.timeout, when)
            except CallbackTimeout as exc:
                raise McpOAuthFailed(
                    f"MCP server {name!r}: no sign-in arrived within {settings.timeout:g} s",
                    server=name,
                    hint="raise mcp_servers.<name>.oauth.timeout if you need longer",
                ) from exc
            if result.error:
                detail = f": {result.error_description}" if result.error_description else ""
                raise McpOAuthFailed(
                    f"MCP server {name!r}: the sign-in was refused ({result.error}){detail}",
                    server=name,
                )
            return AuthorizationCodeResult(
                code=result.code or "", state=listener.state, iss=result.iss
            )

        provider = IndusOAuthProvider(
            server_name=name,
            server_url=_server_url(name, server, scope),
            client_metadata=_client_metadata(
                settings, redirect_uri, bool(secret is not None and secret.client_secret)
            ),
            store=store,
            clock=when,
            interactive=True,
            redirect_handler=announce,
            callback_handler=wait_for_code,
            client_metadata_url=_extra(settings, "client_metadata_url"),
        )
        bounded = server.model_copy(update={"connect_timeout": login_timeout(server)})
        try:
            tools = await connect(name, bounded, scope, provider)
        except BaseException:
            _put_back(store, saved)
            raise
    finally:
        listener.close()
    authorized = store.has_tokens()
    if not authorized:
        _put_back(store, saved)
    return LoginOutcome(tools=tuple(tools), authorized=authorized)


def _require_interactive(name: str) -> None:
    """Only a person's own login may open a browser or show a code (R07.24): background work
    (discovery, reload, cron) never reaches here; ``indus mcp login`` runs inside
    ``trust.allow_interactive_auth()``."""
    from indus.mcp import trust  # trust imports this module

    if not trust.interactive_auth_allowed():
        raise McpOAuthFailed(
            f"MCP server {name!r}: a sign-in can only be started by the person",
            server=name,
            hint=_login_hint(name),
        )


def _put_back(store: OAuthStore, saved: Mapping[str, str | None]) -> None:
    """Restore the sign-in a login set aside; a store that cannot be written is logged only."""
    try:
        store.restore(saved)
    except McpOAuthFailed as exc:
        log.warning(
            "MCP server %r: the previous sign-in could not be restored: %s", store.server, exc
        )


# -- device code --------------------------------------------------------------------------------

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


async def _get_json(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    try:
        response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        log.debug("metadata request to %s failed: %s", url, exc)
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


async def _discover(
    name: str, url: str, client: httpx.AsyncClient
) -> tuple[OAuthMetadata, dict[str, Any]]:
    auth_server: str | None = None
    for candidate in build_protected_resource_metadata_discovery_urls(None, url):
        data = await _get_json(client, candidate)
        if data is None:
            continue
        try:
            resource = ProtectedResourceMetadata.model_validate(data)
        except ValidationError:
            continue
        if resource.authorization_servers:
            auth_server = str(resource.authorization_servers[0])
        break
    for candidate in build_oauth_authorization_server_metadata_discovery_urls(auth_server, url):
        data = await _get_json(client, candidate)
        if data is None:
            continue
        try:
            return OAuthMetadata.model_validate(data), data
        except ValidationError:
            continue
    raise McpOAuthFailed(
        f"MCP server {name!r}: no OAuth authorization server metadata was found", server=name
    )


async def _register_device_client(
    name: str,
    settings: OAuthSettings,
    metadata: OAuthMetadata,
    client: httpx.AsyncClient,
) -> OAuthClientInformationFull:
    endpoint = metadata.registration_endpoint
    if endpoint is None:
        raise McpOAuthFailed(
            f"MCP server {name!r}: the server offers no client registration",
            server=name,
            hint=f"configure mcp_servers.{name}.oauth.client_id with a client from the provider",
        )
    body: dict[str, Any] = {
        "client_name": settings.client_name or CLIENT_NAME,
        "grant_types": [DEVICE_GRANT, "refresh_token"],
        "response_types": [],
        "token_endpoint_auth_method": "none",
        "application_type": "native",
    }
    scope = _extra(settings, "scope")
    if scope:
        body["scope"] = scope
    try:
        response = await client.post(str(endpoint), json=body)
    except httpx.HTTPError as exc:
        raise McpOAuthFailed(
            f"MCP server {name!r}: client registration failed ({exc})", server=name
        ) from exc
    if response.status_code not in (200, 201):
        raise McpOAuthFailed(
            f"MCP server {name!r}: client registration refused (HTTP {response.status_code})",
            server=name,
        )
    try:
        return OAuthClientInformationFull.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        raise McpOAuthFailed(
            f"MCP server {name!r}: the client registration answer is not usable", server=name
        ) from exc


def _token_from(tokens: TokenSet, now: datetime) -> OAuthToken:
    expires_in: int | None = None
    if tokens.expires_at is not None:
        expires_in = max(0, int((tokens.expires_at - now).total_seconds()))
    return OAuthToken.model_validate(
        {
            "access_token": tokens.access_token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "scope": tokens.scope,
            "refresh_token": tokens.refresh_token,
        }
    )


async def device_login(
    name: str,
    server: McpServer,
    scope: Scope,
    *,
    ui: LoginUi,
    client: httpx.AsyncClient,
    secrets: SecretWriter | None = None,
    clock: Clock | None = None,
) -> None:
    """Sign in with a device code (RFC 8628) for a session without a local browser: the code
    and address are shown, the token endpoint is polled at the server's interval (``slow_down``
    adds 5 s), and tokens, client and metadata are stored for the SDK's later refreshes."""
    if server.auth != "oauth" or not server.is_remote:
        raise McpOAuthFailed(f"MCP server {name!r} is not configured with auth: oauth", server=name)
    _require_interactive(name)
    writer = secrets if secrets is not None else secrets_for(scope)
    when = clock or scope.clock
    settings = _settings(server)
    url = _server_url(name, server, scope)
    metadata, raw = await _discover(name, url, client)
    device_endpoint = raw.get("device_authorization_endpoint")
    if not isinstance(device_endpoint, str) or not device_endpoint:
        raise McpOAuthFailed(
            f"MCP server {name!r}: its authorization server offers no device sign-in",
            server=name,
            hint=f"use `indus mcp login {name}` without --device",
        )
    store = _store_for(name, server, scope, writer, when, f"http://{LOOPBACK_HOST}{CALLBACK_PATH}")
    info = store.load_client()
    if info is None or (
        settings.client_id is None and DEVICE_GRANT not in (info.grant_types or [])
    ):
        info = await _register_device_client(name, settings, metadata, client)
    spec = DeviceFlowSpec(
        provider=name,
        device_authorization_url=device_endpoint,
        token_url=str(metadata.token_endpoint),
        client_id=info.client_id or "",
        scope=_extra(settings, "scope") or "",
    )
    opener = ui.open_url if browser.should_open(scope.env) else None
    try:
        tokens = await run_device_login(
            spec, clock=when, say=ui.say, open_url=opener, client=client
        )
    except DeviceFlowError as exc:
        raise McpOAuthFailed(f"MCP server {name!r}: {exc.message}", server=name) from exc
    store.save_client(info)
    store.save_metadata(metadata)
    store.save_tokens(_token_from(tokens, when.now()))


def credential_stamp(
    scope: Scope, *, secrets: SecretWriter | None = None
) -> Callable[[str], object]:
    """``trust.CredentialStamp`` for a host: server name → a value that changes whenever a
    sign-in (in this process or another one) or a refresh stored tokens; None when none are."""
    held: list[SecretWriter] = [secrets] if secrets is not None else []

    def stamp(server: str) -> object:
        if not held:
            held.append(secrets_for(scope))
        return OAuthStore(held[0], server, clock=scope.clock, home=scope.home).stamp()

    return stamp


def logout(name: str, scope: Scope, *, secrets: SecretWriter | None = None) -> int:
    """Forget the stored sign-in, client registration and metadata of one server."""
    writer = secrets if secrets is not None else secrets_for(scope)
    return OAuthStore(writer, name, clock=scope.clock, home=scope.home).clear()
