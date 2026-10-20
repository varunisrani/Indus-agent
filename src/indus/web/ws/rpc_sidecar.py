"""``/api/ws``: the Chat tab's JSON-RPC sidecar, relayed to the profile's daemon (P18-09).

The SPA uses it for connection state and credential warnings next to the terminal. After the
shared WebSocket helper (``guarded_ws``, purpose ``web-rpc``; the internal credential is allowed)
and the profile scope (P18-10), the sidecar dials the profile's
daemon the way every attached client does (P17): a fresh single-use ticket from the owner-only
control socket per dial, offered as a subprotocol to the daemon's loopback WebSocket; when no
daemon answers, the recovery path starts one (``service.remote.channel.home_dialer``). From then on
text frames are relayed unchanged in both directions — the daemon's ``gateway.ready`` first — so
the browser speaks the same declared contract as any other client.

Authority: who is calling comes from the WebSocket credential the helper checked, never from a
frame. Privileged methods (``PRIVILEGED_METHODS``) are answered with a JSON-RPC error, without
reaching the daemon, when the credential is the server-internal one (a spawned child, not a
person).

Limits: binary frames close 1003, frames over 1 MiB close 1009. A profile that does not exist or
a daemon that cannot be reached closes 1011 with a short reason; when the daemon connection drops
the browser socket is closed 1011, and when the browser goes the daemon connection is closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Final

from starlette.websockets import WebSocket, WebSocketDisconnect

from indus.foundation.scope import Scope
from indus.protocol.errors import INVALID_REQUEST
from indus.service.remote.channel import Dial, DialRefused, Transport
from indus.web.pty.registry import close_quietly
from indus.web.scope import ProfileScopes, ProfileUnknown
from indus.web.ws import WsGrant, guarded_ws
from indus.web.ws.chat_gate import INTERNAL, PURPOSE_RPC, scope_of

__all__ = [
    "CLOSE_UNAVAILABLE",
    "MAX_FRAME_BYTES",
    "PRIVILEGED_METHODS",
    "RpcSidecar",
    "daemon_dial",
]

log = logging.getLogger(__name__)

CLOSE_UNAVAILABLE: Final = 1011
CLOSE_UNSUPPORTED: Final = 1003
CLOSE_TOO_BIG: Final = 1009
MAX_FRAME_BYTES: Final = 1024 * 1024
PRIVILEGED_METHODS: Final = frozenset({"maintenance.run"})
NOT_ALLOWED: Final = "This connection may not call this method."


def daemon_dial(scope: Scope) -> Dial:
    """Dial the daemon of ``scope``'s profile, starting it when none answers."""
    from indus.service.gateway_ensure import RecoverBudget
    from indus.service.remote.channel import home_dialer

    return home_dialer(scope.home, env=dict(scope.env), budget=RecoverBudget())


type DialFor = Callable[[Scope], Dial]


class RpcSidecar:
    """The ``/api/ws`` endpoint (see the module notes)."""

    purpose: Final = PURPOSE_RPC

    def __init__(
        self,
        *,
        scopes: ProfileScopes,
        dial_for: DialFor = daemon_dial,
        privileged: frozenset[str] = PRIVILEGED_METHODS,
        max_frame: int = MAX_FRAME_BYTES,
    ) -> None:
        self.scopes = scopes
        self._dial_for = dial_for
        self._privileged = privileged
        self._max_frame = max_frame

    @property
    def endpoint(self) -> Callable[[WebSocket], Awaitable[None]]:
        async def rpc_socket(websocket: WebSocket, grant: WsGrant) -> None:
            await self.handle(websocket, grant)

        return guarded_ws(self.purpose, allow_internal=True)(rpc_socket)

    def refusal(self, identity: WsGrant, text: str) -> str | None:
        """The error frame answering ``text`` here ("" to drop it silently), or None to relay
        it."""
        if identity.credential != INTERNAL or not self._privileged:
            return None
        try:
            frame: Any = json.loads(text)
        except ValueError:
            return None
        if not isinstance(frame, dict) or frame.get("method") not in self._privileged:  # pyright: ignore[reportUnknownMemberType]
            return None
        request_id: object = frame.get("id")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if request_id is None:
            return ""  # a notification: dropped, nothing to answer
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": INVALID_REQUEST.code, "message": NOT_ALLOWED},
            }
        )

    async def handle(self, websocket: WebSocket, identity: WsGrant) -> None:
        try:
            scope = scope_of(self.scopes, identity)
        except ProfileUnknown:
            await close_quietly(websocket, CLOSE_UNAVAILABLE, "no such profile")
            return
        try:
            dialed = await self._dial_for(scope)()
        except DialRefused as refused:
            log.info("rpc sidecar: the gateway refused the attach (%s)", refused.reason)
            await close_quietly(websocket, CLOSE_UNAVAILABLE, "gateway unavailable")
            return
        except Exception as exc:
            log.warning("rpc sidecar: the gateway could not be reached (%s)", type(exc).__name__)
            await close_quietly(websocket, CLOSE_UNAVAILABLE, "gateway unavailable")
            return
        await self._relay(websocket, identity, dialed.transport)

    async def _relay(self, websocket: WebSocket, identity: WsGrant, upstream: Transport) -> None:
        upstream_lost = False

        async def from_browser() -> None:
            while True:
                try:
                    message = await websocket.receive()
                except (RuntimeError, WebSocketDisconnect):
                    return
                if message.get("type") == "websocket.disconnect":
                    return
                text = message.get("text")
                if not isinstance(text, str):
                    await close_quietly(websocket, CLOSE_UNSUPPORTED, "text frames only")
                    return
                if len(text.encode("utf-8", "replace")) > self._max_frame:
                    await close_quietly(websocket, CLOSE_TOO_BIG, "frame too large")
                    return
                answer = self.refusal(identity, text)
                if answer is not None:
                    if answer:
                        await websocket.send_text(answer)
                    continue
                await upstream.send(text)

        async def from_daemon() -> None:
            nonlocal upstream_lost
            try:
                while True:
                    text = await upstream.recv()
                    await websocket.send_text(text)
            except Exception:
                upstream_lost = True

        loop = asyncio.get_running_loop()
        tasks = [
            loop.create_task(from_browser(), name="indus-rpc-up"),
            loop.create_task(from_daemon(), name="indus-rpc-down"),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            with contextlib.suppress(Exception):
                await upstream.close(1000, "client left")
            if upstream_lost:
                await close_quietly(websocket, CLOSE_UNAVAILABLE, "gateway connection lost")
