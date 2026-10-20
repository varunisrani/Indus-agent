"""The one way a dashboard WebSocket is accepted (P18-06, R09.2).

Every WebSocket route (PTY, events, RPC sidecar, publisher, console, plugin sockets) is an
endpoint wrapped by ``guarded_ws(purpose)``; the wrapper calls ``ws_accept`` first and runs the
route's handler only with a ``WsGrant``. ``ws_accept`` gathers the inputs — gate mode, the loopback
token, the ticket book, the internal credential, the Host/Origin verdict of the listener's Host
rules, whether the bind is loopback (then the peer must be too), the profile the request names —
and lets ``service.ws_auth.authenticate_dashboard_upgrade`` decide. A refused upgrade is accepted
and closed at once with the decision's code and clamped reason (closing before the accept would
turn the code into a bare HTTP 403 a browser cannot read); the handler never runs.

The credential is offered in the subprotocol list (``service.ws_auth.dashboard_offer``), never in
the URL; the server echoes only ``indus-dashboard-v1`` — on a refusal too, when the client offered
it, so a browser sees the close code instead of a failed handshake.

``guarded_ws`` marks the endpoint (``WS_PURPOSE_ATTR``) so a test can enumerate the app's WebSocket
routes and prove each one goes through here.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, cast
from urllib.parse import parse_qsl

from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket

from indus.service.ws_auth import (
    CLOSE_BAD_TICKET,
    DASHBOARD_SUBPROTOCOL,
    DashboardAccepted,
    UpgradeRefused,
    authenticate_dashboard_upgrade,
)
from indus.web.auth.audit import AuditEvent
from indus.web.auth.host_guard import scope_headers
from indus.web.auth.proxy import client_ip
from indus.web.state import DashboardState, ProfileRef, dashboard_state

__all__ = ["WS_PURPOSE_ATTR", "WsGrant", "guarded_ws", "websocket_routes", "ws_accept"]

log = logging.getLogger(__name__)

WS_PURPOSE_ATTR: Final = "__indus_ws_purpose__"

type WsHandler = Callable[[WebSocket, WsGrant], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class WsGrant:
    """An accepted upgrade: purpose, credential kind and the profile it serves — the ticket's
    profile, else the one ``?profile=`` names, else the backend's own. ``profile`` is None only
    when the request named a profile that does not exist: the route says so in its own terms
    (the PTY route in red text and 1011, P18-09)."""

    purpose: str
    credential: str
    profile: ProfileRef | None


def _query(scope: dict[str, Any], key: str) -> str | None:
    raw: bytes = scope.get("query_string", b"") or b""
    for name, value in parse_qsl(raw.decode("latin-1"), keep_blank_values=True):
        if name == key:
            return value
    return None


def _echo(scope: dict[str, Any]) -> str | None:
    offered: object = scope.get("subprotocols") or ()
    if isinstance(offered, list | tuple) and DASHBOARD_SUBPROTOCOL in cast(
        tuple[object, ...], offered
    ):
        return DASHBOARD_SUBPROTOCOL
    return None


async def _refuse(websocket: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await websocket.accept(subprotocol=_echo(cast(dict[str, Any], websocket.scope)))
        await websocket.close(code=code, reason=reason)


async def ws_accept(
    websocket: WebSocket,
    purpose: str,
    *,
    allow_internal: bool = False,
) -> WsGrant | None:
    """Decide the upgrade and accept it, or close it with the refusal's code (None)."""
    state: DashboardState = dashboard_state(websocket.app)
    scope = cast(dict[str, Any], websocket.scope)
    requested = _query(scope, "profile")
    profile = state.resolve_profile(requested)
    rules = state.auth.rules
    verdict = authenticate_dashboard_upgrade(
        scope,
        purpose,
        enabled=state.feature_enabled(purpose),
        gated=state.gated,
        book=state.tickets,
        session_token=state.auth.token.matches,
        host_origin_problem=rules.check_upgrade(scope_headers(scope)),
        require_loopback_peer=rules.loopback_bind and not state.gated,
        internal=state.internal,
        allow_internal=allow_internal,
        profile_id=profile.stable_id if requested and profile is not None else None,
    )
    if isinstance(verdict, UpgradeRefused):
        if verdict.close_code == CLOSE_BAD_TICKET:
            state.auth.audit.record(
                AuditEvent.WS_TICKET_REFUSED,
                purpose=purpose,
                reason=verdict.reason,
                ip=client_ip(scope),
            )
        await _refuse(websocket, verdict.close_code, verdict.reason)
        return None
    accepted: DashboardAccepted = verdict
    if requested and profile is None:
        pass  # named but unknown: the route refuses it after the accept
    elif profile is None or (accepted.profile_id and accepted.profile_id != profile.stable_id):
        profile = _profile_by_stable_id(state, accepted.profile_id)
    await websocket.accept(subprotocol=DASHBOARD_SUBPROTOCOL)
    return WsGrant(purpose, accepted.credential, profile)


def _profile_by_stable_id(state: DashboardState, stable_id: str | None) -> ProfileRef | None:
    if not stable_id:
        return state.resolve_profile("")
    if stable_id == state.stable_id:
        return state.resolve_profile("")
    from indus.foundation.profiles import list_profiles

    for name in list_profiles(state.root):
        found = state.resolve_profile(name)
        if found is not None and found.stable_id == stable_id:
            return found
    return None


def guarded_ws(
    purpose: str, *, allow_internal: bool = False
) -> Callable[[WsHandler], Callable[[WebSocket], Awaitable[None]]]:
    """Wrap a ``handler(websocket, grant)`` into a route endpoint that accepts only through
    ``ws_accept``."""

    def wrap(handler: WsHandler) -> Callable[[WebSocket], Awaitable[None]]:
        async def endpoint(websocket: WebSocket) -> None:
            grant = await ws_accept(websocket, purpose, allow_internal=allow_internal)
            if grant is None:
                return
            await handler(websocket, grant)

        endpoint.__name__ = getattr(handler, "__name__", "ws_endpoint")
        endpoint.__doc__ = handler.__doc__
        setattr(endpoint, WS_PURPOSE_ATTR, purpose)
        return endpoint

    return wrap


def websocket_routes(app: Any) -> list[tuple[str, Callable[..., Any]]]:
    """Every WebSocket route of ``app`` as ``(path, endpoint)``, also inside mounts and included
    routers (FastAPI keeps an included router as one wrapper route)."""
    found: list[tuple[str, Callable[..., Any]]] = []

    def walk(routes: Any, prefix: str) -> None:
        for route in routes or ():
            if isinstance(route, WebSocketRoute):
                found.append((prefix + route.path, route.endpoint))
                continue
            original = getattr(route, "original_router", None)
            if original is not None:
                context = getattr(route, "include_context", None)
                walk(getattr(original, "routes", ()), prefix + str(getattr(context, "prefix", "")))
                continue
            nested = getattr(route, "routes", None)
            if nested is not None:
                walk(nested, prefix + str(getattr(route, "path", "")))

    walk(getattr(app, "routes", ()), "")
    return found
