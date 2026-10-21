"""A fake relay connector over the shared `FakeChatPlatform` (P24-09; contract in
``docs/user/relay-contract.md``).

``RelayWire.open`` is the transport's socket opener: opening connects the platform (an armed
connect failure or ``refuse`` refuses the socket), closing disconnects it. The fake checks the
signed hello with the product's own ``verify_hello`` (secret, ±300 s window on the platform's fake
clock, nonce memory) and answers ``refused`` + close, or one ``descriptor`` per fronted platform,
``ready``, then the replay of every buffered event (each with its ``buffer_id``) in order.

Operations act on the platform: ``send`` → ``post``, ``edit``, ``typing``, ``prompt`` → a post
with buttons whose data is the option's ``data``, ``send_media`` → ``upload`` (the reference must
come from ``media_transport``), ``react``, ``thread_create`` / ``thread_rename`` (recorded),
``follow_up`` (recorded and posted), ``chat_info``. Armed platform failures answer the matching
result code; a read timeout leaves the operation unanswered. ``mute`` drops every operation.

Inbound: ``push_inbound()`` renders the platform's queued items as ``event`` frames; while no
authenticated socket is open, or after the gateway went dormant (``idle`` → ``idle_ack``), they are
buffered instead. ``acks`` lists acknowledged buffer ids; ``drop()`` breaks the open socket;
``revoke()`` changes the secret so the next hello is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

from indus.gateway.relay.descriptor import Descriptor
from indus.gateway.relay.transport import NonceMemory, verify_hello
from tests.fakes.platform.core import (
    Button,
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakePlatformError,
)

URL = "https://relay.invalid"
GATEWAY_ID = "gw-test"
ALL_OPS = (
    "send",
    "edit",
    "typing",
    "follow_up",
    "send_media",
    "prompt",
    "react",
    "thread_create",
    "thread_rename",
    "chat_info",
    "delete",
)
BASE_OPS = ("send", "edit", "typing", "follow_up")

_CODES: dict[FailureKind, str] = {
    FailureKind.CONNECT_TIMEOUT: "unavailable",
    FailureKind.RATE_LIMIT: "rate_limited",
    FailureKind.CHAT_NOT_FOUND: "chat_not_found",
    FailureKind.THREAD_NOT_FOUND: "thread_not_found",
    FailureKind.FORBIDDEN: "forbidden",
    FailureKind.PARSE_ERROR: "bad_format",
    FailureKind.TOO_LONG: "too_long",
}


def shared_secret() -> str:
    """The shared secret the fake expects (built at runtime: never a literal in the repo)."""
    return "-".join(("relay", "fake", "shared", "value"))


def descriptor_for(
    platform: FakeChatPlatform,
    *,
    ops: Iterable[str] = ALL_OPS,
    len_unit: str = "chars",
    max_len: int | None = None,
    edits: bool = True,
) -> Descriptor:
    return Descriptor(
        platform=platform.name,
        label=f"{platform.name} (relay)",
        max_len=platform.max_len if max_len is None else max_len,
        len_unit="utf16" if len_unit == "utf16" else "chars",
        supports_edit=edits,
        supports_threads=True,
        markdown_dialect="plain",
        ops=frozenset(ops),
    )


class _Closed(Exception):
    """The fake socket was dropped (httpx-ws raises its own disconnect error)."""


class FakeRelaySocket:
    def __init__(self, wire: RelayWire) -> None:
        self.wire = wire
        self.frames: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.open = True
        self.authed = False

    async def receive_text(self) -> str:
        frame = await self.frames.get()
        if frame is None:
            self.open = False
            raise _Closed("socket dropped")
        return frame

    async def send_text(self, data: str) -> None:
        if not self.open:
            raise _Closed("socket closed")
        frame = json.loads(data)
        self.sent.append(frame)
        await self.wire.handle(self, frame)

    def push(self, frame: dict[str, Any] | str) -> None:
        self.frames.put_nowait(frame if isinstance(frame, str) else json.dumps(frame))

    def close(self) -> None:
        self.frames.put_nowait(None)


class RelayWire:
    def __init__(
        self,
        platform: FakeChatPlatform,
        *,
        descriptors: Iterable[Descriptor] | None = None,
        secret: str | None = None,
        gateway_id: str = GATEWAY_ID,
        raw_descriptors: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        self.platform = platform
        self.secret = secret or shared_secret()
        self.gateway_id = gateway_id
        self.descriptors = (
            list(descriptors) if descriptors is not None else [descriptor_for(platform)]
        )
        self.raw_descriptors = list(raw_descriptors or ())
        self.nonces = NonceMemory()
        self.socket: FakeRelaySocket | None = None
        self.opens = 0
        self.refuse = False
        self.mute = False
        self.dormant = False
        self.hellos: list[dict[str, Any]] = []
        self.refusals: list[str] = []
        self.actions: list[dict[str, Any]] = []
        self.interrupts: list[dict[str, Any]] = []
        self.acks: list[str] = []
        self.buffer: list[tuple[str, str, dict[str, Any]]] = []
        self.media: dict[str, bytes] = {}
        self.uploads: list[dict[str, str]] = []
        self.threads: list[dict[str, Any]] = []
        self.follow_ups: list[dict[str, Any]] = []
        self._buffer_ids = itertools.count(1)
        self._media_ids = itertools.count(1)
        self._thread_ids = itertools.count(900)

    # -- the opener -----------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def open(self, url: str) -> AsyncIterator[FakeRelaySocket]:
        if self.refuse:
            raise ConnectionRefusedError("connection refused")
        try:
            await self.platform.connect()
        except FakePlatformError as exc:
            raise ConnectionRefusedError(str(exc)) from None
        self.opens += 1
        socket = FakeRelaySocket(self)
        self.socket = socket
        try:
            yield socket
        finally:
            socket.open = False
            if self.socket is socket:
                self.socket = None
            await self.platform.disconnect()

    @property
    def live(self) -> bool:
        socket = self.socket
        return socket is not None and socket.open and socket.authed and not self.dormant

    def drop(self) -> None:
        if self.socket is not None:
            self.socket.close()

    def revoke(self) -> None:
        self.secret = "-".join(("rotated", "fake", "value"))

    # -- frames from the gateway ----------------------------------------------------------------

    async def handle(self, socket: FakeRelaySocket, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "hello":
            self._hello(socket, frame)
        elif kind == "op":
            if socket.authed:
                await self._op(socket, frame)
        elif kind == "ack":
            buffer_id = str(frame.get("buffer_id"))
            self.acks.append(buffer_id)
            self.buffer = [b for b in self.buffer if b[0] != buffer_id]
        elif kind == "idle":
            self.dormant = True
            socket.push({"type": "idle_ack"})
        elif kind == "interrupt":
            self.interrupts.append(frame)

    def _hello(self, socket: FakeRelaySocket, frame: dict[str, Any]) -> None:
        self.hellos.append(frame)
        why = verify_hello(frame, self.secret, now=self.platform.clock.now(), seen=self.nonces)
        if why is None and frame.get("gateway_id") != self.gateway_id:
            why = "unknown gateway"
        if why is not None:
            self.refusals.append(why)
            socket.push({"type": "refused", "reason": "auth"})
            socket.close()
            return
        socket.authed = True
        self.dormant = False
        for descriptor in self.descriptors:
            socket.push({"type": "descriptor", "descriptor": descriptor.to_wire()})
        for raw in self.raw_descriptors:
            socket.push({"type": "descriptor", "descriptor": raw})
        socket.push({"type": "ready"})
        for buffer_id, platform, event in list(self.buffer):
            socket.push(
                {"type": "event", "platform": platform, "event": event, "buffer_id": buffer_id}
            )

    async def _op(self, socket: FakeRelaySocket, frame: dict[str, Any]) -> None:
        action = dict(frame.get("action") or {})
        action["_platform"] = frame.get("platform")
        self.actions.append(action)
        if self.mute:
            return
        try:
            result = await self._perform(action)
        except FakePlatformError as exc:
            if exc.kind is FailureKind.READ_TIMEOUT:
                return  # never answered
            result = {"ok": False, "code": _CODES.get(exc.kind, "error"), "error": exc.text}
            if exc.retry_after is not None:
                result["retry_after"] = exc.retry_after
        socket.push({"type": "result", "id": frame.get("id"), "result": result})

    def _supported(self, op: str) -> bool:
        return any(op in d.ops for d in self.descriptors)

    async def _perform(self, action: dict[str, Any]) -> dict[str, Any]:
        op = str(action.get("op"))
        if not self._supported(op):
            return {"ok": False, "code": "unsupported", "error": f"{op} is not supported"}
        chat_id = str(action.get("chat_id") or "")
        meta = action.get("meta") or {}
        p = self.platform
        if op == "send":
            sent = await p.post(
                chat_id,
                str(action.get("text") or ""),
                reply_to=action.get("reply_to"),
                thread_id=meta.get("thread_id"),
                extra={"meta": meta},
            )
            return {"ok": True, "message_id": sent.message_id}
        if op == "edit":
            await p.edit(chat_id, str(action.get("message_id")), str(action.get("text") or ""))
            return {"ok": True, "message_id": action.get("message_id")}
        if op == "delete":
            await p.delete(chat_id, str(action.get("message_id")))
            return {"ok": True}
        if op == "typing":
            await p.typing(chat_id, thread_id=meta.get("thread_id"))
            return {"ok": True}
        if op == "prompt":
            buttons = tuple(Button(o["label"], o["data"]) for o in action.get("options") or ())
            sent = await p.post(chat_id, str(action.get("text") or ""), buttons=buttons)
            return {"ok": True, "message_id": sent.message_id}
        if op == "send_media":
            ref = str(action.get("ref") or "")
            if ref not in self.media:
                return {"ok": False, "code": "error", "error": "unknown media reference"}
            kind = str(action.get("media_kind") or "document")
            fake_kind = {"image": "photo", "file": "document"}.get(kind, kind)
            media = FakeMedia(
                fake_kind,  # pyright: ignore[reportArgumentType]
                str(action.get("filename") or ref),
                caption=str(action.get("caption") or ""),
            )
            sent = await p.upload(chat_id, media, reply_to=action.get("reply_to"))
            return {"ok": True, "message_id": sent.message_id}
        if op == "react":
            emoji = "" if action.get("remove") else str(action.get("emoji") or "")
            await p.react(chat_id, str(action.get("message_id")), emoji)
            return {"ok": True}
        if op == "thread_create":
            thread_id = str(next(self._thread_ids))
            self.threads.append({"op": op, "chat_id": chat_id, "thread_id": thread_id, **action})
            return {"ok": True, "thread_id": thread_id}
        if op == "thread_rename":
            self.threads.append({"op": op, **action})
            return {"ok": True}
        if op == "follow_up":
            self.follow_ups.append(action)
            sent = await p.post(chat_id, str(action.get("text") or ""))
            return {"ok": True, "message_id": sent.message_id}
        if op == "chat_info":
            chat = await p.chat(chat_id)
            return {"ok": True, "name": chat.name, "chat_type": chat.kind}
        return {"ok": False, "code": "unsupported", "error": f"unknown op {op}"}

    # -- frames to the gateway ------------------------------------------------------------------

    def render(self, item: FakeInbound) -> dict[str, Any]:
        event: dict[str, Any] = {
            "chat_id": item.chat_id,
            "chat_type": "thread" if item.thread_id else item.chat_kind,
            "chat_name": item.chat_name or None,
            "user_id": item.user_id,
            "user_name": item.user_name or None,
            "thread_id": item.thread_id,
            "is_bot": item.is_bot,
            "message_id": item.message_id or None,
            "text": item.text,
            "reply_to": item.reply_to,
            "mentioned": item.mentions_bot,
            "scope_id": item.scope_id,
            "guild_id": item.guild_id,
            "ts": item.at.timestamp(),
        }
        if item.event == "edited":
            event["edited"] = True
        if item.event == "button":
            event["button"] = item.button_data
        if item.media:
            event["media"] = [
                {"ref": f"relay-media:in{n}", "kind": m.kind, "mime": m.mime, "name": m.ref}
                for n, m in enumerate(item.media)
            ]
        return event

    def deliver(self, event: dict[str, Any], platform: str | None = None) -> None:
        """One event for the gateway: live when it can take it, else buffered."""
        name = platform or self.platform.name
        if self.live and self.socket is not None:
            self.socket.push({"type": "event", "platform": name, "event": event})
            return
        self.buffer.append((f"b{next(self._buffer_ids)}", name, event))

    def push_inbound(self) -> int:
        items = self.platform.drain_inbound()
        for item in items:
            self.deliver(self.render(item))
        return len(items)

    def interrupt(self, chat_id: str, session_key: str | None = None) -> None:
        assert self.socket is not None, "no socket is open"
        frame: dict[str, Any] = {
            "type": "interrupt",
            "platform": self.platform.name,
            "chat_id": chat_id,
        }
        if session_key:
            frame["session_key"] = session_key
        self.socket.push(frame)

    # -- the media endpoint ---------------------------------------------------------------------

    def media_transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            headers = request.headers
            hello = {
                "type": "hello",
                "v": 1,
                "gateway_id": headers.get("x-indus-relay-gateway"),
                "ts": int(headers.get("x-indus-relay-ts") or 0),
                "nonce": headers.get("x-indus-relay-nonce"),
                "sig": headers.get("x-indus-relay-sig"),
            }
            if verify_hello(hello, self.secret, now=self.platform.clock.now(), seen=self.nonces):
                return httpx.Response(401, json={"error": "unauthorized"})
            if request.method == "POST":
                media_id = f"m{next(self._media_ids)}"
                ref = f"relay-media:{media_id}"
                self.media[ref] = request.content
                self.uploads.append(
                    {
                        "ref": ref,
                        "kind": headers.get("x-indus-relay-kind", ""),
                        "filename": headers.get("x-indus-relay-filename", ""),
                    }
                )
                return httpx.Response(200, json={"ref": ref})
            media_id = request.url.path.rsplit("/", 1)[-1]
            data = self.media.get(f"relay-media:{media_id}")
            if data is None:
                return httpx.Response(404)
            return httpx.Response(200, content=data)

        return httpx.MockTransport(handle)


__all__ = [
    "ALL_OPS",
    "BASE_OPS",
    "GATEWAY_ID",
    "URL",
    "FakeRelaySocket",
    "RelayWire",
    "descriptor_for",
    "shared_secret",
]
