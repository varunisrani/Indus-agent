"""The daemon's loopback WebSocket endpoint (P17-13).

``WsEndpoint`` serves one Starlette WebSocket route (``/ws``) on 127.0.0.1 and a port the OS picks
(``start`` returns the ``ws_url``; ``status_row`` is what the runtime status records). Clients reach
it with a single-use ticket from the owner-only control socket (P17-11).

Upgrade: ``service.ws_auth.authenticate_upgrade`` decides, and nothing else. A refusal is accepted
(echoing ``indus-gateway-v1`` only when offered, so a browser can read the code) and closed at once
with the helper's code: 4401 for a missing, used, expired or wrong ticket, 4403 for a peer that is
not loopback or an Origin not on the allow-list. A ticket in the URL is never used. On success the
server echoes only ``indus-gateway-v1`` and sends ``gateway.ready`` first: replay epoch, heartbeat,
profile id, instance id, capabilities and protocol version.

Per connection (``WsConnection``): each text message is one JSON-RPC frame. ``decode_request`` →
the shared ``Dispatcher`` over the daemon's method table (``gateway.ws.methods``); requests run as
their own tasks so a long submit never holds up an interrupt; ``gateway.ping`` is answered by the
read loop itself. Every outgoing frame goes through the connection's ``Outbox`` (P17-14) and one
writer task, so a slow client only ever stalls itself:

- one frame may take at most ``write_timeout`` (10 s) on the socket, and a frame may wait at most
  ``send_deadline`` (30 s) before it is sent; past either the socket is closed 1011;
- at most ``dispatch_queue`` (256) requests may be waiting or running; the next one closes the
  socket 1013 ("try again later"), as does an outbox past its byte cap;
- an inbound message larger than ``max_message_bytes`` closes 1009; a binary message 1003;
- no inbound frame for ``idle_timeout`` (120 s; clients ping every 15 s) closes 1001, so a peer
  that vanished without a close does not keep its viewers attached.

Every request runs under the endpoint's scope, which must belong to the profile its ticket book
serves (``profile_guard.bind_daemon_scope``; the endpoint refuses to exist otherwise).

A response frame from the client whose id is a server-request id (``srq-…``) with an error records
that viewer's refusal (``-32601`` / ``4404``). Closing the socket only detaches its viewers; turns
keep running. Close reasons are fixed short texts. Logs never carry params, tickets, or a URL's
query or user-info.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast
from urllib.parse import urlsplit, urlunsplit

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket

from indus.foundation.scope import Scope
from indus.gateway.http.serving import HttpServer, bind_listener
from indus.gateway.ws import methods as ws_methods
from indus.gateway.ws.extras import WsExtras
from indus.protocol.bridge import READY_METHOD, BridgeReady
from indus.protocol.envelope import Request
from indus.protocol.errors import INTERNAL_ERROR
from indus.protocol.methods import PROTOCOL_VERSION, ProtocolError
from indus.service.daemon_host import DaemonSessionHost
from indus.service.remote.profile_guard import bind_daemon_scope
from indus.service.request_redelivery import REQUEST_ID_PREFIX
from indus.service.rpc import ContractViolation, Dispatcher, decode_request, error_frame
from indus.service.rpc.dispatch import check_payload, strict_from
from indus.service.subscriptions import (
    Outbox,
    OutboxLimits,
    SessionWatch,
    Subscriptions,
    notification,
)
from indus.service.ws_auth import (
    SUBPROTOCOL,
    WS_INTERACTIVE,
    TicketBook,
    UpgradeRefused,
    authenticate_upgrade,
    is_loopback_host,
)

__all__ = [
    "CLOSE_GOING_AWAY",
    "CLOSE_SEND_DEADLINE",
    "CLOSE_TOO_BIG",
    "CLOSE_TRY_AGAIN",
    "CLOSE_UNSUPPORTED",
    "WS_PATH",
    "WsConnection",
    "WsEndpoint",
    "WsLimits",
    "WsUnavailable",
    "loggable_url",
]

log = logging.getLogger(__name__)

WS_PATH: Final = "/ws"
CLOSE_NORMAL: Final = 1000
CLOSE_GOING_AWAY: Final = 1001
CLOSE_UNSUPPORTED: Final = 1003
CLOSE_TOO_BIG: Final = 1009
CLOSE_SEND_DEADLINE: Final = 1011
CLOSE_TRY_AGAIN: Final = 1013
MAX_CLOSE_REASON: Final = 123
"""Bytes a WebSocket close reason may hold."""
_CLOSE_WAIT_S: Final = 2.0
_MIB = 1024 * 1024


class WsUnavailable(RuntimeError):
    """No WebSocket implementation is installed for the server (``wsproto`` or ``websockets``)."""


@dataclass(frozen=True, slots=True)
class WsLimits:
    """Per-connection limits (see the module notes)."""

    write_timeout: float = 10.0
    send_deadline: float = 30.0
    dispatch_queue: int = 256
    max_message_bytes: int = 1 * _MIB
    inbound_bytes: int = 16 * _MIB
    max_connections: int = 64
    idle_timeout: float = 120.0
    """Seconds without any inbound frame before the socket is closed (clients ping every 15 s);
    a peer that vanished without a close never holds its viewers for ever."""
    drain_s: float = 2.0
    outbox: OutboxLimits = field(default_factory=OutboxLimits)


def loggable_url(url: str) -> str:
    """``url`` without user-info, query or fragment (safe for a log line)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "<unparsable url>"
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _clamp(reason: str) -> str:
    data = reason.encode("utf-8")[:MAX_CLOSE_REASON]
    return data.decode("utf-8", errors="ignore")


def _implementation() -> str:
    for name in ("wsproto", "websockets"):
        if importlib.util.find_spec(name) is not None:
            return name
    raise WsUnavailable(
        "the WebSocket endpoint needs the wsproto (or websockets) package; it is not installed"
    )


def _peer(scope: Mapping[str, Any]) -> str:
    client = scope.get("client")
    if isinstance(client, list | tuple) and len(cast(tuple[object, ...], client)) >= 2:
        host, port = cast(tuple[object, ...], client)[:2]
        return f"{host}:{port}"
    return "unknown"


# -- the socket seam ------------------------------------------------------------------------------


class _Socket:
    """What a connection needs from its socket (Starlette's ``WebSocket`` underneath)."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket

    async def receive(self) -> Mapping[str, Any]:
        return await self.websocket.receive()

    async def send_text(self, text: str) -> None:
        await self.websocket.send_text(text)

    async def close(self, code: int, reason: str) -> None:
        await self.websocket.close(code=code, reason=_clamp(reason))


# -- one connection -------------------------------------------------------------------------------


class _Stop(Exception):
    """End the read loop and close with ``code``."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class WsConnection:
    """One accepted client (see the module notes); the dispatcher's ``RpcContext``."""

    def __init__(self, endpoint: WsEndpoint, socket: _Socket, *, peer: str) -> None:
        self.endpoint = endpoint
        self.host = endpoint.host
        self.socket = socket
        self.peer = peer
        self.connection_id = f"ws-{secrets.token_hex(6)}"
        self.limits = endpoint.limits
        self.outbox = Outbox(self.limits.outbox)
        self.subs = Subscriptions(
            endpoint.host,
            self.connection_id,
            self.outbox,
            watch=endpoint.watch,
            strict=endpoint.strict,
        )
        self.dispatcher = Dispatcher(self, endpoint.table)
        self.violations: list[ContractViolation] = []
        self._inflight: dict[asyncio.Task[None], int] = {}
        self._inbound_bytes = 0
        self._closing = asyncio.Event()
        self._close_code: int | None = None
        self._close_reason = ""
        self.requests = 0

    # -- RpcContext -------------------------------------------------------------------------------

    @property
    def scope(self) -> Scope:
        return self.endpoint.scope

    @property
    def strict(self) -> bool:
        return self.endpoint.strict

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    # -- lifetime ---------------------------------------------------------------------------------

    async def run(self) -> None:
        """Serve until the client goes, a limit is hit or the endpoint stops."""
        writer = asyncio.ensure_future(self._write_loop())
        try:
            self._send_ready()
            await self._read_loop()
        except _Stop as stop:
            self._mark_close(stop.code, stop.reason)
        finally:
            self._closing.set()
            await self._teardown(writer)

    def _send_ready(self) -> None:
        host = self.host
        book = self.endpoint.book
        ready = BridgeReady(
            theme=self.endpoint.theme,
            heartbeat=True,
            protocol_version=PROTOCOL_VERSION,
            replay_epoch=host.replay_epoch,
            profile_id=book.profile_id,
            instance_id=book.instance_id,
            capabilities=list(host.capabilities()),
        )
        params = ready.to_wire()
        check_payload(BridgeReady, params, strict=self.strict)
        self.outbox.put(notification(READY_METHOD, params))

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        """Send one unnumbered notification (``wake.detected``, P24-20) on the loop."""
        self._put(notification(method, params))

    def close_soon(self, code: int, reason: str) -> None:
        """Ask the connection to close (from any task on the loop)."""
        self._mark_close(code, reason)
        self._closing.set()

    def _mark_close(self, code: int, reason: str) -> None:
        if self._close_code is None:
            self._close_code = code
            self._close_reason = reason

    async def _next_message(self) -> Mapping[str, Any] | None:
        """The next inbound message; None when closing. ``_Stop`` after ``idle_timeout``."""
        receive = asyncio.ensure_future(self.socket.receive())
        closing = asyncio.ensure_future(self._closing.wait())
        try:
            await asyncio.wait(
                {receive, closing},
                timeout=self.limits.idle_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            closing.cancel()
        if receive.done() and not receive.cancelled() and receive.exception() is None:
            return receive.result()
        receive.cancel()
        with contextlib.suppress(BaseException):
            await receive
        if not self._closing.is_set():
            raise _Stop(CLOSE_GOING_AWAY, "idle")
        return None

    async def _read_loop(self) -> None:
        while not self._closing.is_set():
            message = await self._next_message()
            if message is None or message.get("type") == "websocket.disconnect":
                self._mark_close(CLOSE_NORMAL, "client closed")
                return
            text = message.get("text")
            if not isinstance(text, str):
                raise _Stop(CLOSE_UNSUPPORTED, "only text frames are accepted")
            if self._too_big(text):
                raise _Stop(CLOSE_TOO_BIG, "message too big")
            await self._take(text)

    def _too_big(self, text: str) -> bool:
        cap = self.limits.max_message_bytes
        if len(text) > cap:
            return True
        return len(text) * 4 > cap and len(text.encode("utf-8", errors="replace")) > cap

    async def _take(self, text: str) -> None:
        if REQUEST_ID_PREFIX in text and self._refusal(text):
            return
        frame = decode_request(text)
        if frame is None:
            return
        if not isinstance(frame, Request):
            self._put(frame)  # an error answer for a frame that is no request
            return
        self.requests += 1
        if frame.method == "gateway.ping":
            self._put(await self._handle(frame))
            return
        if len(self._inflight) >= self.limits.dispatch_queue:
            raise _Stop(CLOSE_TRY_AGAIN, "too many requests waiting")
        size = len(text)
        if self._inbound_bytes + size > self.limits.inbound_bytes:
            raise _Stop(CLOSE_TRY_AGAIN, "too many request bytes waiting")
        self._inbound_bytes += size
        task = asyncio.ensure_future(self._answer(frame))
        self._inflight[task] = size
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._inbound_bytes -= self._inflight.pop(task, 0)

    def _refusal(self, text: str) -> bool:
        """A client's error answer to a server request: that viewer refuses it."""
        try:
            value: object = json.loads(text)
        except ValueError:
            return False
        if not isinstance(value, dict) or "method" in value:
            return False
        frame = cast(dict[str, object], value)
        request_id, error = frame.get("id"), frame.get("error")
        if not isinstance(request_id, str) or not request_id.startswith(REQUEST_ID_PREFIX):
            return False
        if isinstance(error, dict):
            code = cast(dict[str, object], error).get("code")
            if isinstance(code, int) and not isinstance(code, bool):
                self.subs.refuse(request_id, code)
        return True

    async def _handle(self, request: Request) -> dict[str, Any]:
        try:
            return await self.dispatcher.handle(request)
        except ContractViolation as exc:
            self.violations.append(exc)
            log.error("contract violation in %s: %s", request.method, exc)
            return error_frame(request.id, ProtocolError(INTERNAL_ERROR, str(exc)))

    async def _answer(self, request: Request) -> None:
        self._put(await self._handle(request))

    def _put(self, frame: Mapping[str, Any]) -> None:
        if not self.outbox.put(frame) and self.outbox.overloaded:
            self.close_soon(CLOSE_TRY_AGAIN, "too much to send")

    async def _write_loop(self) -> None:
        outbox = self.outbox
        while True:
            if outbox.oldest_wait() > self.limits.send_deadline:
                self.close_soon(CLOSE_SEND_DEADLINE, "send deadline")
                return
            text = await outbox.get()
            if text is None:
                if outbox.overloaded:
                    self.close_soon(CLOSE_TRY_AGAIN, "too much to send")
                return
            try:
                async with asyncio.timeout(self.limits.write_timeout):
                    await self.socket.send_text(text)
            except TimeoutError:
                log.warning("WebSocket client %s stopped reading; closing it", self.peer)
                self.close_soon(CLOSE_SEND_DEADLINE, "send deadline")
                return
            except Exception as exc:
                log.info("WebSocket send to %s failed: %s", self.peer, type(exc).__name__)
                self.close_soon(CLOSE_NORMAL, "client gone")
                return

    async def _teardown(self, writer: asyncio.Future[None]) -> None:
        tasks = set(self._inflight)
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=self.limits.drain_s)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=self.limits.drain_s)
        detached = await self.subs.close()
        self.endpoint.extras.connection_closed(self)
        writer.cancel()
        with contextlib.suppress(BaseException):
            await writer
        code = self._close_code if self._close_code is not None else CLOSE_NORMAL
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_CLOSE_WAIT_S):
                await self.socket.close(code, self._close_reason or "closed")
        self.violations.extend(self.subs.violations)
        self.endpoint.violations.extend(self.violations)
        log.info(
            "WebSocket client %s closed (%d %s): %d requests, %d sessions detached",
            self.peer,
            code,
            self._close_reason or "closed",
            self.requests,
            len(detached),
        )


# -- the endpoint ---------------------------------------------------------------------------------


class WsEndpoint:
    """The daemon's WebSocket endpoint over ``host`` (see the module notes)."""

    def __init__(
        self,
        host: DaemonSessionHost,
        book: TicketBook,
        *,
        scope: Scope,
        origins: Callable[[], Iterable[str]] | Iterable[str] = (),
        limits: WsLimits | None = None,
        theme: str = "auto",
        strict: bool | None = None,
        served: bool = False,
    ) -> None:
        self.host = host
        self.book = book
        # Every RPC runs under this one scope (the dispatcher's ``use_scope``): it must be the
        # profile the ticket book serves, with that profile's secrets (P17-24, R05.14). A served
        # profile's daemon in a multiplex host passes ``served`` (its own routed scope, D-16.542).
        self.scope = bind_daemon_scope(scope, book.profile_id, served=served)
        self.limits = limits or WsLimits()
        self.theme = theme
        self.strict = strict_from(scope) if strict is None else strict
        self._origins = origins
        self.table = ws_methods.table()
        # P24: projects, pet, wake word, capabilities, bots, group rooms (gateway.ws.extras)
        self.extras = WsExtras(self.scope, instance_id=book.instance_id)
        self.extras.register(self.table)
        self.watch = SessionWatch(host)
        self.connections: dict[str, WsConnection] = {}
        self.violations: list[ContractViolation] = []
        """Contract problems seen on closed connections (strict mode keeps them here)."""
        self._tasks: set[asyncio.Task[None]] = set()
        self._server: HttpServer | None = None
        self._url: str | None = None
        self._stopping = False
        self.app = Starlette(routes=[WebSocketRoute(WS_PATH, self._serve)])

    # -- reading ----------------------------------------------------------------------------------

    def origins(self) -> list[str]:
        source = self._origins
        values = source() if callable(source) else source
        return [str(item) for item in values]

    @property
    def ws_url(self) -> str | None:
        """``ws://127.0.0.1:<port>/ws`` while serving, else None."""
        return self._url

    def status_row(self) -> dict[str, Any]:
        """The runtime-status row (``StatusWriter.set_surface("ws", …)``)."""
        if self._server is None or self._url is None:
            return {"state": "stopped"}
        bound, port = self._server.address
        return {
            "state": "listening",
            "url": self._url,
            "host": bound,
            "port": port,
            "connections": len(self.connections),
        }

    # -- serving ----------------------------------------------------------------------------------

    async def start(self, *, bind: str = "127.0.0.1", port: int = 0) -> str:
        """Listen on ``bind:port`` (loopback only; port 0 picks one) and return the ``ws_url``."""
        if not is_loopback_host(bind):
            raise ValueError("the gateway WebSocket listens on a loopback address only")
        if self._server is not None and self._url is not None:
            return self._url
        implementation = _implementation()
        sock = await bind_listener(bind, port, self.scope.clock)
        server = HttpServer(
            self.app,
            sock,
            name="gateway WebSocket",
            ws={
                "ws": implementation,
                "ws_max_size": self.limits.max_message_bytes,
                "ws_per_message_deflate": False,
            },
        )
        await server.start()
        host, bound_port = server.address
        shown = f"[{host}]" if ":" in host else host
        self._server = server
        self._stopping = False
        self._url = f"ws://{shown}:{bound_port}{WS_PATH}"
        self.extras.bind_loop(asyncio.get_running_loop())
        self.watch.start()
        log.info("gateway WebSocket listening on %s", loggable_url(self._url))
        return self._url

    async def stop(self) -> None:
        """Close every connection (1001) and stop listening."""
        self._stopping = True
        for connection in list(self.connections.values()):
            connection.close_soon(CLOSE_GOING_AWAY, "daemon stopping")
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=self.limits.drain_s + _CLOSE_WAIT_S)
        server, self._server = self._server, None
        self._url = None
        if server is not None:
            await server.stop()
        self.watch.stop()
        self.extras.close()

    async def _serve(self, websocket: WebSocket) -> None:
        scope = websocket.scope
        peer = _peer(scope)
        verdict = authenticate_upgrade(
            scope, WS_INTERACTIVE, book=self.book, origins=self.origins()
        )
        offered = [str(p) for p in cast(list[object], scope.get("subprotocols") or [])]
        if isinstance(verdict, UpgradeRefused):
            await self._refuse(websocket, offered, verdict.close_code, verdict.reason)
            return
        if self._stopping:
            await self._refuse(websocket, offered, CLOSE_GOING_AWAY, "daemon stopping")
            return
        if len(self.connections) >= self.limits.max_connections:
            await self._refuse(websocket, offered, CLOSE_TRY_AGAIN, "too many connections")
            return
        await websocket.accept(subprotocol=verdict.subprotocol)
        connection = WsConnection(self, _Socket(websocket), peer=peer)
        self.connections[connection.connection_id] = connection
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        log.info("WebSocket client %s attached (path %s)", peer, scope.get("path", WS_PATH))
        try:
            await connection.run()
        finally:
            self.connections.pop(connection.connection_id, None)
            if task is not None:
                self._tasks.discard(task)

    async def _refuse(
        self, websocket: WebSocket, offered: list[str], code: int, reason: str
    ) -> None:
        await websocket.accept(subprotocol=SUBPROTOCOL if SUBPROTOCOL in offered else None)
        with contextlib.suppress(Exception):
            await websocket.close(code=code, reason=_clamp(reason))
