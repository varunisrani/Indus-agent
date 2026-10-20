"""MCP OAuth sign-ins started from the dashboard (P18-14), and their browser callback.

``indus mcp login`` waits for the IdP's redirect on a loopback port of the machine it runs on; a
browser using the dashboard from another machine cannot reach that port. So a sign-in started from
the dashboard asks the IdP to redirect to the dashboard itself: ``CALLBACK_PATH`` under the declared
public URL, else under the address the request came in on (with the trusted path prefix).

``OAuthFlows`` (one per app, in ``DashboardState.extras``) holds the sign-ins in progress: at most
``MAX_FLOWS`` at once and one per server and profile; a flow is forgotten ``FLOW_TTL_S`` after it
started. Each flow runs on its own thread and event loop: P13's OAuth provider (``mcp.oauth``) does
discovery, registration, PKCE and the code exchange; our redirect handler records the
authorization URL and its ``state``; our callback handler waits for the browser's callback, which
``deliver`` hands over after matching ``state`` in constant time. The stored sign-in is set aside
for the flow and put back when the flow does not end with new tokens (a failed, refused or
cancelled sign-in keeps the previous one), as ``oauth.login`` does.

The callback page is fixed text: nothing from the request is echoed. The callback path is on the
shared public list (the browser coming back from the IdP carries no dashboard credential); the
``state`` value is what ties it to a flow a signed-in person started.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from urllib.parse import parse_qs, urlsplit

import anyio.to_thread
from mcp.shared.auth import AuthorizationCodeResult

from indus.foundation.clock import SystemClock
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.mcp import oauth
from indus.mcp.config import McpServer, remote_endpoint
from indus.mcp.oauth import IndusOAuthProvider, McpOAuthFailed

__all__ = [
    "CALLBACK_PATH",
    "FLOW_TTL_S",
    "MAX_FLOWS",
    "Connector",
    "FlowBusy",
    "OAuthFlow",
    "OAuthFlows",
    "callback_page",
    "run_flow",
]

log = logging.getLogger(__name__)

CALLBACK_PATH: Final = "/api/mcp/oauth/callback"
FLOW_TTL_S: Final = 15 * 60.0
MAX_FLOWS: Final = 8
URL_WAIT_S: Final = 30.0

type FlowStatus = Literal[
    "starting", "authorization_required", "authorizing", "done", "error", "cancelled"
]
_SETTLED: Final = frozenset({"done", "error", "cancelled"})

Connector = Callable[[str, McpServer, Scope, IndusOAuthProvider], Awaitable[list[str]]]
"""Connects once with the given auth handler and returns the server's tool names."""


class FlowBusy(Exception):
    """No new flow now: too many in progress, or one already runs for this server."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Callback:
    code: str | None
    state: str | None
    iss: str | None
    error: str | None


@dataclass
class OAuthFlow:
    flow_id: str
    server: str
    home: str
    started: float
    status: FlowStatus = "starting"
    authorization_url: str | None = None
    expected_state: str | None = None
    error: str | None = None
    tools: list[str] = field(default_factory=list[str])
    url_ready: threading.Event = field(default_factory=threading.Event, repr=False)
    _arrived: threading.Event = field(default_factory=threading.Event, repr=False)
    _callback: _Callback | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def settled(self) -> bool:
        return self.status in _SETTLED

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {
                "flow_id": self.flow_id,
                "server": self.server,
                "status": self.status,
                "error": self.error,
            }
            if self.status == "authorization_required":
                out["authorization_url"] = self.authorization_url
            if self.status == "done":
                out["tools"] = list(self.tools)
            return out

    def authorize_at(self, url: str, state: str) -> None:
        with self._lock:
            self.authorization_url = url
            self.expected_state = state or None
            if not self.settled:
                self.status = "authorization_required"
        self.url_ready.set()

    def matches(self, state: str) -> bool:
        expected = self.expected_state
        if expected is None or self.status != "authorization_required":
            return False
        return hmac.compare_digest(expected.encode("utf-8"), state.encode("utf-8"))

    def deliver(self, *, code: str | None, state: str, iss: str | None, error: str | None) -> bool:
        """Hand the browser's callback to the waiting flow; False when it is not waiting."""
        with self._lock:
            if self.status != "authorization_required" or self._callback is not None:
                return False
            self._callback = _Callback(code, state, iss, error)
            self.status = "authorizing"
        self._arrived.set()
        return True

    def wait_callback(self, timeout: float) -> _Callback:
        if not self._arrived.wait(timeout):
            raise McpOAuthFailed(f"MCP server {self.server!r}: no sign-in arrived in time")
        with self._lock:
            found = self._callback
        if found is None:  # cancelled
            raise McpOAuthFailed(f"MCP server {self.server!r}: the sign-in was cancelled")
        return found

    def finish(self, tools: list[str]) -> None:
        with self._lock:
            if not self.settled:
                self.status = "done"
                self.tools = list(tools)
        self.url_ready.set()

    def fail(self, message: str, *, cancelled: bool = False) -> None:
        with self._lock:
            if not self.settled:
                self.status = "cancelled" if cancelled else "error"
                self.error = message
        self.url_ready.set()
        self._arrived.set()


class OAuthFlows:
    """The sign-ins in progress of one dashboard (see the module notes)."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._flows: dict[str, OAuthFlow] = {}

    def _gc_locked(self) -> None:
        cutoff = self._monotonic() - FLOW_TTL_S
        for flow_id in [k for k, f in self._flows.items() if f.started < cutoff]:
            stale = self._flows.pop(flow_id)
            stale.fail("the sign-in expired", cancelled=True)

    def start(self, server: str, home: str) -> OAuthFlow:
        with self._lock:
            self._gc_locked()
            live = [f for f in self._flows.values() if not f.settled]
            if any(f.server == server and f.home == home for f in live):
                raise FlowBusy("flow_in_progress", f"A sign-in to {server!r} is already running.")
            if len(live) >= MAX_FLOWS:
                raise FlowBusy("too_many_flows", "Too many sign-ins are in progress.")
            flow = OAuthFlow(secrets.token_urlsafe(24), server, home, self._monotonic())
            self._flows[flow.flow_id] = flow
            return flow

    def get(self, flow_id: str) -> OAuthFlow | None:
        with self._lock:
            self._gc_locked()
            return self._flows.get(flow_id)

    def by_state(self, state: str) -> OAuthFlow | None:
        """The flow waiting for this ``state`` (constant-time compare against each)."""
        if not state:
            return None
        with self._lock:
            self._gc_locked()
            candidates = list(self._flows.values())
        found: OAuthFlow | None = None
        for flow in candidates:
            if flow.matches(state):
                found = flow
        return found


async def _connect_and_list(
    name: str, server: McpServer, scope: Scope, auth: IndusOAuthProvider
) -> list[str]:
    from indus.mcp.connect import Connection, ConnectOptions

    async with Connection(
        name, server, scope, clock=auth.clock, options=ConnectOptions(auth=auth)
    ) as live:
        return [tool.name for tool in await live.list_tools()]


def run_flow(
    flow: OAuthFlow,
    name: str,
    server: McpServer,
    scope: Scope,
    redirect_uri: str,
    *,
    connect: Connector = _connect_and_list,
) -> None:
    """Run one sign-in to its end on this thread (start it on a thread of its own)."""
    clock = SystemClock()
    settings = oauth._settings(server)  # pyright: ignore[reportPrivateUsage]

    async def main() -> list[str]:
        writer = oauth.secrets_for(scope)
        store = oauth._store_for(name, server, scope, writer, clock, redirect_uri)  # pyright: ignore[reportPrivateUsage]
        saved = store.snapshot()
        store.clear_tokens()
        held = store.load_client()

        async def announce(url: str) -> None:
            state = parse_qs(urlsplit(url).query).get("state", [""])[0]
            flow.authorize_at(url, state)

        async def wait_for_code() -> AuthorizationCodeResult:
            got = await anyio.to_thread.run_sync(flow.wait_callback, settings.timeout)
            if got.error:
                raise McpOAuthFailed(f"MCP server {name!r}: the sign-in was refused ({got.error})")
            return AuthorizationCodeResult(
                code=got.code or "", state=flow.expected_state or "", iss=got.iss
            )

        provider = IndusOAuthProvider(
            server_name=name,
            server_url=remote_endpoint(name, server, scope).url,
            client_metadata=oauth._client_metadata(  # pyright: ignore[reportPrivateUsage]
                settings, redirect_uri, bool(held is not None and held.client_secret)
            ),
            store=store,
            clock=clock,
            interactive=True,
            redirect_handler=announce,
            callback_handler=wait_for_code,
            client_metadata_url=oauth._extra(settings, "client_metadata_url"),  # pyright: ignore[reportPrivateUsage]
        )
        bounded = server.model_copy(update={"connect_timeout": oauth.login_timeout(server)})
        try:
            tools = await connect(name, bounded, scope, provider)
        except BaseException:
            oauth._put_back(store, saved)  # pyright: ignore[reportPrivateUsage]
            raise
        if not store.has_tokens():
            oauth._put_back(store, saved)  # pyright: ignore[reportPrivateUsage]
            raise McpOAuthFailed(f"MCP server {name!r}: the sign-in did not store a token")
        return tools

    try:
        flow.finish(asyncio.run(main()))
    except Exception as exc:
        reason = getattr(exc, "message", None) or str(exc) or type(exc).__name__
        log.info("MCP sign-in to %r ended without tokens: %s", name, type(exc).__name__)
        flow.fail(redact_text(str(reason)))


def start_thread(flow: OAuthFlow, *args: Any, **kwargs: Any) -> threading.Thread:
    thread = threading.Thread(
        target=run_flow,
        args=(flow, *args),
        kwargs=kwargs,
        name=f"indus-mcp-oauth-{flow.server}",
        daemon=True,
    )
    thread.start()
    return thread


_PAGES: Final[dict[str, tuple[int, str, str]]] = {
    "received": (200, "Sign-in received", "You can close this tab and go back to the dashboard."),
    "refused": (400, "Sign-in refused", "Go back to the dashboard for the details."),
    "unknown": (404, "Sign-in expired", "This sign-in is no longer waiting. Start it again."),
    "repeated": (409, "Sign-in already received", "Go back to the dashboard."),
}


def callback_page(kind: str) -> tuple[int, str]:
    """``(status, html)`` of the fixed callback page ``kind``."""
    status, title, line = _PAGES[kind]
    html = (
        "<!doctype html><html><head><meta charset=utf-8>"
        f"<title>{title}</title></head><body><h1>{title}</h1><p>{line}</p></body></html>"
    )
    return status, html
