"""A fake signal-cli daemon in HTTP mode, served to the adapter through ``httpx.MockTransport``.

Routes as signal-cli 0.13 serves them: ``GET /api/v1/check``, ``GET /api/v1/events`` (a
server-sent event stream of ``receive`` events, one envelope each) and ``POST /api/v1/rpc``
(JSON-RPC 2.0 with the methods in ``RPC_SURFACE``). Backed by one `FakeChatPlatform`: inbound
traffic injected with the platform's builders becomes envelopes (group chats are
``group.<groupId>``; Signal has no threads and no bot flag, so those builders produce nothing),
sends land as platform messages whose id is the send timestamp, and armed failures come back the
way signal-cli reports them (per-recipient results, JSON-RPC errors, transport errors).

Tests can also push raw envelopes (``push_envelope``), drop or refuse the event stream, mark the
daemon unhealthy, script RPC errors and read every request in ``requests``.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from indus.gateway.adapters.base import utf16_units
from tests.fakes.platform.core import (
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakePlatformError,
)

MAX_LEN = 8000
ACCOUNT = "+15550000999"
GROUP_PREFIX = "group."
RPC_SURFACE: tuple[str, ...] = (
    "send",
    "sendTyping",
    "sendReaction",
    "getAttachment",
    "listContacts",
)

SAMPLE_BYTES: Mapping[str, bytes] = {
    "voice": b"OggS\x00\x02" + b"\x00" * 26,
    "audio": b"ID3\x04\x00" + b"\x00" * 27,
    "photo": b"\xff\xd8\xff\xe0" + b"\x00" * 28,
    "video": b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20,
    "document": b"plain text notes\n",
    "sticker": b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16,
}


def uuid_for(number: str) -> str:
    """A stable fake account UUID for a phone number."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"signal:{number}"))


@dataclass
class RpcCall:
    method: str
    params: dict[str, Any]
    at: float


@dataclass
class _Stream:
    opened_at: float
    ended: bool = False


@dataclass
class SignalDaemon:
    platform: FakeChatPlatform = field(
        default_factory=lambda: FakeChatPlatform(
            "signal", max_len=MAX_LEN, len_fn=utf16_units, bot_user_id=ACCOUNT
        )
    )
    account: str = ACCOUNT
    healthy: bool = True
    # Event streams: ``refuse_streams`` answers 503, ``fail_streams`` raises a connect error.
    refuse_streams: bool = False
    fail_streams: bool = False
    # Every stream opens (200) and ends at once without sending anything.
    end_streams_at_once: bool = False
    contacts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    attachments: dict[str, bytes] = field(default_factory=dict[str, bytes])
    rpc: list[RpcCall] = field(default_factory=list[RpcCall])
    stream_opens: list[float] = field(default_factory=list[float])
    checks: list[float] = field(default_factory=list[float])
    # Scripted JSON-RPC errors per method, used before the platform is asked.
    scripted: dict[str, deque[dict[str, Any]]] = field(
        default_factory=dict[str, deque[dict[str, Any]]]
    )

    def __post_init__(self) -> None:
        self.transport = httpx.MockTransport(self.handle)
        self._raw: deque[dict[str, Any]] = deque()
        self._stream: _Stream | None = None
        self._drop = False

    @property
    def bot_uuid(self) -> str:
        return uuid_for(self.account)

    # -- test controls -------------------------------------------------------------------------

    def push_envelope(self, envelope: Mapping[str, Any]) -> None:
        """Queue one raw envelope (wrapped as signal-cli wraps it) for the event stream."""
        self._raw.append({"envelope": dict(envelope), "account": self.account})

    def drop_stream(self) -> None:
        """End the open event stream (the adapter sees the connection close)."""
        self._drop = True

    def script_error(self, method: str, error: Mapping[str, Any], times: int = 1) -> None:
        queue = self.scripted.setdefault(method, deque())
        queue.extend(dict(error) for _ in range(times))

    def calls(self, method: str) -> list[RpcCall]:
        return [c for c in self.rpc if c.method == method]

    # -- HTTP --------------------------------------------------------------------------------------

    def _now(self) -> float:
        return self.platform.clock.monotonic()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/check" and request.method == "GET":
            return await self._check(request)
        if path == "/api/v1/events" and request.method == "GET":
            return await self._events(request)
        if path == "/api/v1/rpc" and request.method == "POST":
            return await self._rpc(request)
        return httpx.Response(404, json={"error": "not found"})

    async def _check(self, request: httpx.Request) -> httpx.Response:
        self.checks.append(self._now())
        try:
            await self.platform.connect()
        except FakePlatformError as exc:
            raise httpx.ConnectTimeout(exc.text, request=request) from None
        if not self.healthy:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"versions": {"signal-cli": "0.13.22"}})

    async def _events(self, request: httpx.Request) -> httpx.Response:
        self.stream_opens.append(self._now())
        if self.fail_streams:
            raise httpx.ConnectError("connection refused", request=request)
        if self.refuse_streams or not self.healthy:
            return httpx.Response(503, text="unavailable")
        if request.url.params.get("account") != self.account:
            return httpx.Response(400, text="unknown account")
        await self.platform.connect()
        stream = _Stream(self._now())
        self._stream = stream
        self._drop = False
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_EventStream(self, stream),
        )

    async def next_events(self) -> list[bytes]:
        chunks: list[bytes] = []
        while self._raw:
            chunks.append(self._frame(self._raw.popleft()))
        for item in self.platform.drain_inbound():
            envelope = self.envelope_for(item)
            if envelope is not None:
                chunks.append(self._frame({"envelope": envelope, "account": self.account}))
        return chunks

    @staticmethod
    def _frame(payload: Mapping[str, Any]) -> bytes:
        return f"event:receive\ndata:{json.dumps(payload)}\n\n".encode()

    async def stream_closed(self, stream: _Stream) -> None:
        stream.ended = True
        if self._stream is stream:
            self._stream = None
            await self.platform.disconnect()

    # -- inbound shapes ----------------------------------------------------------------------------

    def envelope_for(self, item: FakeInbound) -> dict[str, Any] | None:
        if item.is_bot or item.thread_id or item.event == "button":
            return None  # Signal has no bot flag, no threads and no buttons
        stamp = int(item.message_id) if item.message_id.isdigit() else 0
        data: dict[str, Any] = {"timestamp": stamp}
        text = item.text
        if item.mentions_bot:
            data["mentions"] = [
                {
                    "start": 0,
                    "length": 1,
                    "number": self.account,
                    "uuid": self.bot_uuid,
                    "name": self.platform.bot_name,
                }
            ]
            text = "\ufffc " + text
        if text:
            data["message"] = text
        if item.chat_kind != "dm":
            data["groupInfo"] = {
                "groupId": item.chat_id.removeprefix(GROUP_PREFIX),
                "groupName": item.chat_name,
                "type": "DELIVER",
            }
        if item.reply_to:
            original = self.platform.message(item.reply_to)
            author = self.account if original is not None and original.from_bot else item.user_id
            data["quote"] = {
                "id": int(item.reply_to),
                "author": author,
                "authorNumber": author,
                "text": original.text if original is not None else "",
            }
        if item.media:
            data["attachments"] = [self._attachment(item, m) for m in item.media]
        envelope: dict[str, Any] = {
            "source": item.user_id,
            "sourceNumber": item.user_id,
            "sourceUuid": uuid_for(item.user_id),
            "sourceName": item.user_name or item.user_id,
            "sourceDevice": 1,
            "timestamp": stamp,
        }
        if item.event == "edited":
            envelope["editMessage"] = {"targetSentTimestamp": stamp, "dataMessage": data}
        else:
            envelope["dataMessage"] = data
        return envelope

    def _attachment(self, item: FakeInbound, media: FakeMedia) -> dict[str, Any]:
        att_id = f"att{item.update_id}-{media.ref}"
        body = SAMPLE_BYTES.get(media.kind, b"data")
        self.attachments[att_id] = body
        return {
            "contentType": media.mime or "application/octet-stream",
            "filename": media.ref,
            "id": att_id,
            "size": len(body),
            "voiceNote": media.kind == "voice",
        }

    # -- JSON-RPC ----------------------------------------------------------------------------------

    async def _rpc(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = str(body.get("method"))
        params: dict[str, Any] = dict(body.get("params") or {})
        rpc_id = body.get("id")
        self.rpc.append(RpcCall(method, params, self._now()))
        scripted = self.scripted.get(method)
        if scripted:
            return _error(rpc_id, scripted.popleft())
        try:
            result = await self._dispatch(method, params, request)
        except _RpcFailure as failure:
            return _error(rpc_id, failure.error)
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "id": rpc_id})

    def _chat_of(self, params: Mapping[str, Any]) -> str:
        group = params.get("groupId")
        if group:
            return GROUP_PREFIX + str(group)
        recipient = str((params.get("recipient") or [""])[0])
        for contact in self.contacts:
            if recipient in (contact.get("uuid"), contact.get("number")):
                return str(contact.get("number") or recipient)
        return recipient

    async def _dispatch(self, method: str, params: dict[str, Any], request: httpx.Request) -> Any:
        chat = self._chat_of(params)
        try:
            if method == "send":
                return await self._send(chat, params)
            if method == "sendTyping":
                if params.get("stop"):
                    await self.platform.typing_stop(chat)
                else:
                    await self.platform.typing(chat)
                return {}
            if method == "sendReaction":
                await self.platform.react(
                    chat, str(params.get("targetTimestamp")), str(params.get("emoji") or "")
                )
                return {"timestamp": int(self._now() * 1000)}
            if method == "getAttachment":
                data = self.attachments.get(str(params.get("id")))
                if data is None:
                    raise _RpcFailure({"code": -1, "message": "Attachment not found"})
                return {"data": base64.b64encode(data).decode()}
            if method == "listContacts":
                return list(self.contacts)
        except FakePlatformError as exc:
            return self._platform_failure(exc, chat, request)
        raise _RpcFailure({"code": -32601, "message": f"Method not implemented: {method}"})

    async def _send(self, chat: str, params: Mapping[str, Any]) -> dict[str, Any]:
        paths = [str(p) for p in params.get("attachments") or ()]
        extra: dict[str, Any] = {"attachments": paths}
        if params.get("quoteTimestamp"):
            extra["quote"] = (params.get("quoteTimestamp"), params.get("quoteAuthor"))
        sent = await self.platform.post(
            chat,
            str(params.get("message") or ""),
            reply_to=str(params["quoteTimestamp"]) if params.get("quoteTimestamp") else None,
            extra=extra,
        )
        return {
            "timestamp": int(sent.message_id),
            "results": [{"recipientAddress": {"number": chat}, "type": "SUCCESS"}],
        }

    def _platform_failure(
        self, exc: FakePlatformError, chat: str, request: httpx.Request
    ) -> dict[str, Any]:
        """An armed failure the way signal-cli shows it."""
        kind = exc.kind
        group = chat.startswith(GROUP_PREFIX)
        if kind is FailureKind.CONNECT_TIMEOUT:
            raise httpx.ConnectTimeout(exc.text, request=request)
        if kind is FailureKind.READ_TIMEOUT:
            raise httpx.ReadTimeout(exc.text, request=request)
        if kind is FailureKind.RATE_LIMIT:
            wait = exc.retry_after if exc.retry_after is not None else 5.0
            raise _RpcFailure(
                {
                    "code": -5,
                    "message": "Failed to send message due to rate limiting",
                    "data": {
                        "response": {
                            "results": [
                                {"type": "RATE_LIMIT_FAILURE", "retryAfterSeconds": int(wait)}
                            ]
                        }
                    },
                }
            )
        if kind is FailureKind.CHAT_NOT_FOUND:
            if group:
                raise _RpcFailure({"code": -1, "message": "Invalid group id"})
            return {"timestamp": 1, "results": [{"type": "UNREGISTERED_FAILURE"}]}
        if kind is FailureKind.FORBIDDEN:
            if group:
                raise _RpcFailure({"code": -1, "message": "User is not a member in group"})
            return {"timestamp": 1, "results": [{"type": "IDENTITY_FAILURE"}]}
        raise _RpcFailure({"code": -1, "message": exc.text})


class _RpcFailure(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(str(error.get("message")))
        self.error = error


def _error(rpc_id: object, error: Mapping[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "error": dict(error), "id": rpc_id})


class _EventStream(httpx.AsyncByteStream):
    """The open ``/api/v1/events`` response: frames as inbound traffic arrives."""

    def __init__(self, daemon: SignalDaemon, stream: _Stream) -> None:
        self._daemon = daemon
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        clock = self._daemon.platform.clock
        daemon = self._daemon
        while not daemon._drop and not daemon.end_streams_at_once:  # pyright: ignore[reportPrivateUsage]
            for chunk in await self._daemon.next_events():
                yield chunk
            await clock.sleep(0.25)
        await self._daemon.stream_closed(self._stream)

    async def aclose(self) -> None:
        await self._daemon.stream_closed(self._stream)


__all__ = [
    "ACCOUNT",
    "GROUP_PREFIX",
    "MAX_LEN",
    "RPC_SURFACE",
    "SAMPLE_BYTES",
    "RpcCall",
    "SignalDaemon",
    "uuid_for",
]
