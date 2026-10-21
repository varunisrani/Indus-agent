"""The broker's two API-server routes (P24-47).

``POST /v1/browser-control/register`` sits behind the API key like every API route, and also
answers only a loopback peer (403 otherwise). Body: ``{controller_id, browser_profile, session,
protocol: 1}``; the server adds the transport family (``api``), the principal (the API key id)
and the profile. Answer: ``{ticket, expires_in, subprotocol, ticket_prefix, protocol, ws_path}``
with ``Cache-Control: no-store``. The ticket is in the body only.

``/v1/browser-control/ws`` is the controller's socket; ``BrowserBroker.admit`` decides the
upgrade. A refusal is accepted (echoing the base protocol only when offered) and closed at once
with its code. Then, per connection:

- the first frame must be ``browser.controller.hello`` with ``protocol`` 1 and a capability list
  within ``HELLO_TIMEOUT_S`` (10 s), else close 4400; the server answers
  ``browser.controller.ready`` with the negotiated capabilities;
- ``browser.controller.result {command_id, ok, result}`` settles a command;
  ``browser.controller.detach`` detaches (outstanding commands fail) and closes 1000;
- ``browser.controller.artifact.put {mime}`` followed by one binary frame stores an artefact for
  this identity and answers ``browser.controller.artifact.stored {artifact_id, size, sha256}``
  (or ``browser.controller.artifact.refused {reason}``);
  ``browser.controller.artifact.fetch {artifact_id}`` consumes one of this identity's artefacts:
  ``browser.controller.artifact.data {artifact_id, mime, size}`` then the bytes as one binary
  frame, or the refusal;
- a text frame over ``MAX_FRAME_BYTES`` (1 MiB) or a binary frame over the artefact cap closes
  1009; text that is not a JSON object, or a binary frame nobody announced, closes 1003;
- a dropped socket only marks the controller offline (``BrowserBroker.disconnect``).

Logs name the controller id, never a ticket, a URL query or frame contents.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, cast

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect

from indus.gateway.browser_broker.artifacts import MAX_BYTES, ArtifactRefused
from indus.gateway.browser_broker.broker import (
    FRAME_DETACH,
    FRAME_HELLO,
    FRAME_READY,
    FRAME_RESULT,
    PROTOCOL_VERSION,
    SUBPROTOCOL,
    BrowserBroker,
    ControllerUnavailable,
    Identity,
    RegistrationRefused,
    check_part,
    protocol_supported,
)
from indus.gateway.http.serving import BadRequest, BodyTooLarge, read_json
from indus.service.ws_auth import UpgradeRefused, clamp_close_reason, is_loopback_host

__all__ = [
    "HELLO_TIMEOUT_S",
    "MAX_FRAME_BYTES",
    "REGISTER_PATH",
    "TRANSPORT_FAMILY",
    "WS_PATH",
    "ControllerConnection",
    "mount_routes",
    "ws_settings",
]

log = logging.getLogger(__name__)

REGISTER_PATH: Final = "/v1/browser-control/register"
WS_PATH: Final = "/v1/browser-control/ws"
TRANSPORT_FAMILY: Final = "api"
HELLO_TIMEOUT_S: Final = 10.0
MAX_FRAME_BYTES: Final = 1024 * 1024
MAX_REGISTER_BYTES: Final = 16 * 1024
CLOSE_NORMAL: Final = 1000
CLOSE_UNSUPPORTED: Final = 1003
CLOSE_TOO_BIG: Final = 1009
CLOSE_TRY_AGAIN: Final = 1013
CLOSE_BAD_HELLO: Final = 4400

FRAME_ARTIFACT_PUT: Final = "browser.controller.artifact.put"
FRAME_ARTIFACT_STORED: Final = "browser.controller.artifact.stored"
FRAME_ARTIFACT_FETCH: Final = "browser.controller.artifact.fetch"
FRAME_ARTIFACT_DATA: Final = "browser.controller.artifact.data"
FRAME_ARTIFACT_REFUSED: Final = "browser.controller.artifact.refused"


def _peer(scope: Mapping[str, Any]) -> object:
    client: object = scope.get("client")
    if isinstance(client, list | tuple) and cast(Sequence[object], client):
        return cast(Sequence[object], client)[0]
    return None


class _Close(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class ControllerConnection:
    """One admitted controller socket (see the module notes)."""

    def __init__(self, websocket: WebSocket, broker: BrowserBroker, identity: Identity) -> None:
        self.websocket = websocket
        self.broker = broker
        self.identity = identity
        self.detached = False
        self._put_mime: str | None = None

    async def send(self, frame: dict[str, Any]) -> None:
        await self.websocket.send_text(json.dumps(frame, separators=(",", ":")))

    async def _receive(self) -> Mapping[str, Any]:
        message = await self.websocket.receive()
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(int(message.get("code") or CLOSE_NORMAL))
        return message

    async def _hello(self) -> object:
        receiving = asyncio.ensure_future(self._receive())
        timer = asyncio.ensure_future(self.broker.clock.sleep(HELLO_TIMEOUT_S))
        try:
            await asyncio.wait({receiving, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer
        if not receiving.done():
            receiving.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receiving
            raise _Close(CLOSE_BAD_HELLO, "no hello in time")
        frame = self._frame(receiving.result(), hello=True)
        params = frame.get("params")
        if frame.get("method") != FRAME_HELLO or not isinstance(params, Mapping):
            raise _Close(CLOSE_BAD_HELLO, "the first frame must be a hello")
        hello = cast(Mapping[str, Any], params)
        if not protocol_supported(hello.get("protocol")):
            raise _Close(CLOSE_BAD_HELLO, f"protocol {PROTOCOL_VERSION} only")
        return hello.get("capabilities")

    def _frame(self, message: Mapping[str, Any], *, hello: bool = False) -> dict[str, Any]:
        text = message.get("text")
        if not isinstance(text, str):
            raise _Close(CLOSE_BAD_HELLO if hello else CLOSE_UNSUPPORTED, "text frames only")
        if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
            raise _Close(CLOSE_TOO_BIG, "frame too large")
        try:
            data = json.loads(text)
        except ValueError:
            raise _Close(CLOSE_BAD_HELLO if hello else CLOSE_UNSUPPORTED, "not JSON") from None
        if not isinstance(data, dict):
            raise _Close(CLOSE_BAD_HELLO if hello else CLOSE_UNSUPPORTED, "not a JSON object")
        return cast(dict[str, Any], data)

    async def run(self) -> None:
        try:
            offered = await self._hello()
            try:
                negotiated = await self.broker.attach(self.identity, offered, self.send, owner=self)
            except ControllerUnavailable:
                raise _Close(CLOSE_TRY_AGAIN, "reconnect could not be completed") from None
            await self.send(
                {
                    "method": FRAME_READY,
                    "params": {"protocol": PROTOCOL_VERSION, "capabilities": sorted(negotiated)},
                }
            )
            log.info("browser controller %s attached", self.identity.controller_id)
            while not self.detached:
                await self._one(await self._receive())
        except _Close as close:
            with contextlib.suppress(Exception):
                await self.websocket.close(code=close.code, reason=clamp_close_reason(close.reason))
        except WebSocketDisconnect:
            pass
        finally:
            if not self.detached and self.broker.disconnect(self.identity, owner=self):
                log.info("browser controller %s went offline", self.identity.controller_id)

    async def _one(self, message: Mapping[str, Any]) -> None:
        if message.get("bytes") is not None:
            await self._artifact_bytes(cast(bytes, message["bytes"]))
            return
        frame = self._frame(message)
        if self._put_mime is not None:
            raise _Close(CLOSE_UNSUPPORTED, "an artefact's bytes were expected")
        method = frame.get("method")
        raw: object = frame.get("params")
        params = cast(Mapping[str, Any], raw) if isinstance(raw, Mapping) else {}
        if method == FRAME_RESULT:
            self.broker.complete(
                params.get("command_id"),
                self.identity,
                ok=params.get("ok") is True,
                result=params.get("result"),
            )
        elif method == FRAME_DETACH:
            self.detached = True
            await self.broker.detach(self.identity, owner=self)
            log.info("browser controller %s detached", self.identity.controller_id)
            raise _Close(CLOSE_NORMAL, "detached")
        elif method == FRAME_ARTIFACT_PUT:
            self._put_mime = str(params.get("mime") or "")
        elif method == FRAME_ARTIFACT_FETCH:
            await self._fetch(params.get("artifact_id"))
        else:
            raise _Close(CLOSE_UNSUPPORTED, "unknown frame")

    async def _refused(self, reason: str) -> None:
        await self.send({"method": FRAME_ARTIFACT_REFUSED, "params": {"reason": reason}})

    async def _artifact_bytes(self, data: bytes) -> None:
        mime, self._put_mime = self._put_mime, None
        if mime is None:
            raise _Close(CLOSE_UNSUPPORTED, "unannounced binary frame")
        if len(data) > MAX_BYTES:
            raise _Close(CLOSE_TOO_BIG, "artefact too large")
        store = self.broker.artifacts
        if store is None:
            await self._refused("unavailable")
            return
        try:
            receipt = store.put(self.identity.scope_key(), data, mime)
        except ArtifactRefused as refused:
            await self._refused(refused.reason)
            return
        await self.send(
            {
                "method": FRAME_ARTIFACT_STORED,
                "params": {"artifact_id": receipt.artifact_id, "size": receipt.size,
                           "sha256": receipt.sha256, "mime": receipt.mime},
            }
        )  # fmt: skip

    async def _fetch(self, artifact_id: object) -> None:
        store = self.broker.artifacts
        if store is None:
            await self._refused("unavailable")
            return
        try:
            data, receipt = store.consume(artifact_id, self.identity.scope_key())
        except ArtifactRefused as refused:
            await self._refused(refused.reason)
            return
        await self.send(
            {
                "method": FRAME_ARTIFACT_DATA,
                "params": {"artifact_id": receipt.artifact_id, "mime": receipt.mime,
                           "size": receipt.size},
            }
        )  # fmt: skip
        await self.websocket.send_bytes(data)


async def controller_socket(
    websocket: WebSocket, broker: BrowserBroker, origins: Sequence[str]
) -> None:
    """Admit or refuse one controller socket, then serve it."""
    decided = broker.admit(websocket.scope, origins=origins)
    if isinstance(decided, UpgradeRefused):
        offered = websocket.scope.get("subprotocols") or []
        await websocket.accept(subprotocol=SUBPROTOCOL if SUBPROTOCOL in offered else None)
        await websocket.close(code=decided.close_code, reason=clamp_close_reason(decided.reason))
        return
    await websocket.accept(subprotocol=SUBPROTOCOL)
    await ControllerConnection(websocket, broker, decided).run()


def ws_settings() -> dict[str, Any]:
    """uvicorn's WebSocket settings for the API server once the broker is mounted: room for one
    artefact frame, no compression. ``RuntimeError`` when no WebSocket package is installed."""
    for name in ("wsproto", "websockets"):
        if importlib.util.find_spec(name) is not None:
            return {
                "ws": name,
                "ws_max_size": MAX_BYTES + 64 * 1024,
                "ws_per_message_deflate": False,
            }
    raise RuntimeError("browser control needs the wsproto (or websockets) package")


def identity_from(body: object, *, principal: str, profile: str) -> Identity:
    """The identity a register body asks for; ``RegistrationRefused`` when malformed."""
    if not isinstance(body, Mapping):
        raise RegistrationRefused("the body must be a JSON object")
    data = cast(Mapping[str, Any], body)
    if not protocol_supported(data.get("protocol")):
        raise RegistrationRefused(f"protocol must be {PROTOCOL_VERSION}")
    return Identity(
        transport_family=TRANSPORT_FAMILY,
        principal=principal,
        browser_profile=check_part("browser_profile", data.get("browser_profile")),
        controller_id=check_part("controller_id", data.get("controller_id")),
        session=check_part("session", data.get("session")),
        profile=profile,
    )


def mount_routes(app: FastAPI, broker: BrowserBroker, origins: Callable[[], Sequence[str]]) -> None:
    """Add the register route and the controller socket to the API app."""

    def refuse(message: str, status: int, code: str) -> Response:
        failure = BadRequest(message, status=status, code=code)
        return JSONResponse(failure.body(), status_code=failure.status)

    async def register(request: Request) -> Response:
        if not is_loopback_host(_peer(request.scope)):
            return refuse("browser controllers register from this machine only", 403, "forbidden")
        principal = getattr(request.state, "api_key_id", None)
        if not isinstance(principal, str) or not principal:
            return refuse("an API key is required", 401, "invalid_api_key")
        try:
            body = await read_json(request, MAX_REGISTER_BYTES)
        except BodyTooLarge:
            return refuse("the register body is too large", 413, "body_too_large")
        except BadRequest as bad:
            return refuse(bad.message, 400, bad.code or "invalid_json")
        try:
            identity = identity_from(body, principal=principal, profile=broker.book.profile_id)
            registration = broker.register(identity)
        except RegistrationRefused as refused:
            return refuse(str(refused), 400, "registration_refused")
        return JSONResponse(
            {**registration.body(), "ws_path": WS_PATH}, headers={"Cache-Control": "no-store"}
        )

    async def socket(websocket: WebSocket) -> None:
        await controller_socket(websocket, broker, tuple(origins()))

    app.add_api_route(REGISTER_PATH, register, methods=["POST"])
    app.add_api_websocket_route(WS_PATH, socket)
