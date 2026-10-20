"""The Chat tab's WebSocket routes as one unit the app factory mounts (P18-07..10 in P18-01).

``build_chat(state, base, seams)`` makes, per backend: one ``ProfileScopes`` over the backend's own
scope (also installed on the app for the REST routes), one ``PtyRegistry``, one ``ChannelFiles``,
one ``EventsFanout`` and the four endpoints over them:

- ``/api/pty`` (``PtyRoute``), ``/api/pub`` and ``/api/events`` (``EventsRoutes``), ``/api/ws``
  (``RpcSidecar``) — each behind ``guarded_ws`` with its own purpose (``chat_gate``).

The PTY route's ``SidecarLink`` points the TUI child at this server over loopback (the listener's
own address when it is bound to one host) with the internal credential from the dashboard state.

``start()`` runs the registry's reaper in the app's lifespan; ``stop()`` cancels it, ends every PTY
and removes the channel files. ``ChatSeams`` lets tests swap the pieces that touch the machine
(the child resolver, the PTY spawn, the support probe, the lease lookup, the daemon dial).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import APIRouter

from indus.foundation.scope import Scope
from indus.web.auth.host_guard import ALL_INTERFACES, LOOPBACK_HOSTS
from indus.web.pty import bridge as pty_bridge
from indus.web.pty.channels import ChannelFiles
from indus.web.pty.registry import PtyRegistry
from indus.web.scope import ProfileScopes
from indus.web.state import DashboardState
from indus.web.ws.chat_gate import SidecarLink
from indus.web.ws.events_fanout import EventsFanout, EventsRoutes
from indus.web.ws.pty_route import (
    ChildResolver,
    LeaseHolder,
    PtyRoute,
    SpawnFn,
    lease_holder_pid,
)
from indus.web.ws.rpc_sidecar import DialFor, RpcSidecar, daemon_dial

__all__ = ["ChatRoutes", "ChatSeams", "build_chat", "loopback_base_url"]


@dataclass
class ChatSeams:
    """What a test may replace (every default is the real piece)."""

    child: ChildResolver | None = None
    spawn: SpawnFn = pty_bridge.spawn
    unavailable: Callable[[], str | None] = pty_bridge.unavailable
    lease_holder: LeaseHolder = lease_holder_pid
    dial_for: DialFor = daemon_dial
    registry: PtyRegistry | None = None
    channels: ChannelFiles | None = None


def loopback_base_url(state: DashboardState) -> str | None:
    """``ws://<host>:<port>`` the server's own child can dial: loopback for a loopback or
    all-interfaces bind, else the one host it is bound to. None before the port is known."""
    if state.port <= 0:
        return None
    host = state.bound_host.strip().strip("[]").lower()
    if host in ALL_INTERFACES or host in LOOPBACK_HOSTS:
        host = "::1" if host in ("::", "::1") else "127.0.0.1"
    shown = f"[{host}]" if ":" in host else host
    return f"ws://{shown}:{state.port}"


@dataclass
class ChatRoutes:
    """The chat routes of one backend and the pieces they share."""

    scopes: ProfileScopes
    registry: PtyRegistry
    channels: ChannelFiles
    fanout: EventsFanout
    pty: PtyRoute
    events: EventsRoutes
    rpc: RpcSidecar
    _reaper: asyncio.Task[None] | None = field(default=None, repr=False)

    def router(self) -> APIRouter:
        router = APIRouter()
        router.add_api_websocket_route("/api/pty", self.pty.endpoint)
        router.add_api_websocket_route("/api/pub", self.events.publish_endpoint)
        router.add_api_websocket_route("/api/events", self.events.events_endpoint)
        router.add_api_websocket_route("/api/ws", self.rpc.endpoint)
        return router

    async def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.get_running_loop().create_task(
                self.registry.run_reaper(), name="indus-pty-reaper"
            )

    async def stop(self) -> None:
        reaper, self._reaper = self._reaper, None
        if reaper is not None:
            reaper.cancel()
            with contextlib.suppress(BaseException):
                await reaper
        try:
            await self.registry.close_all()
        finally:
            self.channels.close()


def build_chat(state: DashboardState, base: Scope, seams: ChatSeams | None = None) -> ChatRoutes:
    """The chat routes for the backend whose own scope is ``base`` (see the module notes)."""
    used = seams or ChatSeams()
    scopes = ProfileScopes(base)
    registry = used.registry if used.registry is not None else PtyRegistry(clock=base.clock)
    channels = used.channels if used.channels is not None else ChannelFiles()
    fanout = EventsFanout()
    link = SidecarLink(lambda: loopback_base_url(state), lambda: state.internal.value)
    pty = PtyRoute(
        scopes=scopes,
        registry=registry,
        channels=channels,
        link=link,
        child=used.child,
        spawn=used.spawn,
        unavailable=used.unavailable,
        lease_holder=used.lease_holder,
    )
    return ChatRoutes(
        scopes=scopes,
        registry=registry,
        channels=channels,
        fanout=fanout,
        pty=pty,
        events=EventsRoutes(fanout=fanout),
        rpc=RpcSidecar(scopes=scopes, dial_for=used.dial_for),
    )
