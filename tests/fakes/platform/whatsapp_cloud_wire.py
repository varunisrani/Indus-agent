"""A fake WhatsApp Cloud API (Graph REST + signed webhooks) over `FakeChatPlatform` (P16-04).

REST (bearer token; anything else answers Graph error 190 with HTTP 401):

- ``GET /<ver>/<phone-number-id>`` → the number's id and display number (the platform connects);
  ``GET /<ver>/<media-id>`` → ``{url, mime_type, file_size, id}`` of an inbound or uploaded file;
- ``POST /<ver>/<phone-number-id>/messages``: ``text``, ``interactive`` (reply buttons or a list:
  each option becomes a platform `Button`), ``image``/``audio``/``video``/``document`` by uploaded
  id, and the read receipt with a typing indicator (a platform typing op on that message's chat);
  a chat in ``window_closed`` refuses everything but templates with Graph error 131047;
- ``POST /<ver>/<phone-number-id>/media`` (multipart ``file``) → ``{id}``;
- ``GET /media-download/<media-id>`` → the bytes (the URL ``GET /<ver>/<media-id>`` hands out).

Closing the adapter's client disconnects the platform (the lifecycle the contract counts).
Armed platform failures answer in Graph's error shape (``error_response``).

Webhooks: ``render`` turns a platform-neutral `FakeInbound` into Meta's
``whatsapp_business_account`` payload, signed with ``X-Hub-Signature-256``. Cloud API chats are
1:1, so group, thread and bot-authored items have no form, and Meta sends no edits: those render
as None. ``envelope`` / ``signed`` / ``handshake`` build hand-made callbacks for unit tests.
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from email.message import Message
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any

import httpx

from indus.gateway.adapters._http import hmac_sha256_hex
from indus.gateway.adapters.whatsapp_cloud.adapter import DEFAULT_API_VERSION, DEFAULT_PATH, MAX_LEN
from tests.fakes.platform.core import (
    Button,
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakePlatformError,
)
from tests.fakes.platform.http_wire import ApiCall, Callback, HttpCallbackWire, api, json_body

__all__ = [
    "APP_SECRET",
    "BUSINESS_NUMBER",
    "MAX_LEN",
    "PHONE_NUMBER_ID",
    "TOKEN",
    "VERIFY_TOKEN",
    "VERSION",
    "WABA_ID",
    "WhatsAppCloudWire",
]

VERSION = DEFAULT_API_VERSION
PHONE_NUMBER_ID = "106540352242922"
WABA_ID = "102290129340398"
BUSINESS_NUMBER = "15550009999"
# Built at runtime: never a credential-shaped literal in the source.
TOKEN = "".join(["wac", "-token-", "a1b2" * 5])
APP_SECRET = "".join(["wac", "-secret-", "c3d4" * 4])
VERIFY_TOKEN = "".join(["wac", "-verify-", "e5f6" * 3])

# Graph error code and title per armed failure kind (HTTP 429 for throughput, else 400).
GRAPH_ERRORS: Mapping[FailureKind, tuple[int, str]] = {
    FailureKind.RATE_LIMIT: (130429, "Rate limit hit"),
    FailureKind.CHAT_NOT_FOUND: (131026, "Message undeliverable"),
    FailureKind.THREAD_NOT_FOUND: (131009, "Parameter value is not valid"),
    FailureKind.FORBIDDEN: (131047, "Re-engagement message"),
    FailureKind.PARSE_ERROR: (100, "Invalid parameter"),
    FailureKind.TOO_LONG: (100, "Invalid parameter"),
}

_MEDIA_TYPES = {"photo": "image", "video": "video", "document": "document", "sticker": "sticker"}
_UPLOAD_KINDS = {"image": "photo", "video": "video", "audio": "voice", "document": "document"}


def graph_error(code: int, message: str, details: str = "", status: int = 400) -> httpx.Response:
    error: dict[str, Any] = {
        "message": f"(#{code}) {message}",
        "type": "OAuthException",
        "code": code,
        "fbtrace_id": "AfakeTrace",
    }
    if details:
        error["error_data"] = {"messaging_product": "whatsapp", "details": details}
    return httpx.Response(status, json={"error": error})


class _ClosingTransport(httpx.MockTransport):
    """A mock transport whose close is the platform's disconnect."""

    def __init__(
        self,
        handler: Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]],
        on_close: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(handler)
        self._on_close = on_close

    async def aclose(self) -> None:
        await self._on_close()


class WhatsAppCloudWire(HttpCallbackWire):
    """Meta's side of a WhatsApp Cloud number (see the module notes)."""

    def __init__(
        self,
        platform: FakeChatPlatform,
        *,
        token: str = TOKEN,
        app_secret: str = APP_SECRET,
        phone_number_id: str = PHONE_NUMBER_ID,
        waba_id: str = WABA_ID,
        display_number: str = BUSINESS_NUMBER,
    ) -> None:
        super().__init__(platform)
        self.token = token
        self.app_secret = app_secret
        self.phone_number_id = phone_number_id
        self.waba_id = waba_id
        self.display_number = display_number
        # media id → (bytes, mime, file name)
        self.media: dict[str, tuple[bytes, str, str]] = {}
        self.window_closed: set[str] = set()
        self.closed = 0
        self._uploads = itertools.count(1)

    def transport(self) -> httpx.MockTransport:
        return _ClosingTransport(self._handle, self._closed)

    async def _closed(self) -> None:
        self.closed += 1
        await self.platform.disconnect()

    def _refused(self, call: ApiCall) -> httpx.Response | None:
        if call.headers.get("authorization") != f"Bearer {self.token}":
            return graph_error(190, "Error validating access token", status=401)
        return None

    # -- REST ----------------------------------------------------------------------------------

    @api("GET", "/{version}/{node}")
    async def node(self, call: ApiCall, version: str, node: str) -> httpx.Response:
        if (refused := self._refused(call)) is not None:
            return refused
        if node == self.phone_number_id:
            await self.platform.connect()
            return httpx.Response(
                200,
                json={
                    "id": node,
                    "display_phone_number": f"+1 555-000-{self.display_number[-4:]}",
                    "verified_name": "Indus Test",
                },
            )
        found = self.media.get(node)
        if found is None:
            return graph_error(100, "Unsupported get request", "media not found")
        data, mime, _ = found
        return httpx.Response(
            200,
            json={
                "id": node,
                "url": f"{self.base_url}/media-download/{node}",
                "mime_type": mime,
                "file_size": len(data),
                "sha256": "0" * 64,
                "messaging_product": "whatsapp",
            },
        )

    @api("GET", "/media-download/{media}")
    async def download(self, call: ApiCall, media: str) -> httpx.Response:
        if (refused := self._refused(call)) is not None:
            return refused
        found = self.media.get(media)
        if found is None:
            return httpx.Response(404)
        data, mime, _ = found
        return httpx.Response(200, content=data, headers={"content-type": mime})

    @api("POST", "/{version}/{phone}/media")
    async def upload(self, call: ApiCall, version: str, phone: str) -> httpx.Response:
        if (refused := self._refused(call)) is not None:
            return refused
        fields = _multipart(call)
        found = fields.get("file")
        if found is None or fields.get("messaging_product", (b"", "", ""))[0] != b"whatsapp":
            return graph_error(100, "Invalid parameter", "file and messaging_product required")
        media_id = f"up{next(self._uploads)}"
        self.media[media_id] = found
        return httpx.Response(200, json={"id": media_id})

    @api("POST", "/{version}/{phone}/messages")
    async def messages(self, call: ApiCall, version: str, phone: str) -> httpx.Response:
        if (refused := self._refused(call)) is not None:
            return refused
        body: dict[str, Any] = call.json()
        if body.get("messaging_product") != "whatsapp":
            return graph_error(100, "Invalid parameter", "messaging_product must be whatsapp")
        if body.get("status") == "read":
            message = self.platform.message(str(body.get("message_id")))
            if message is None:
                return graph_error(131009, "Parameter value is not valid", "unknown message")
            if body.get("typing_indicator"):
                await self.platform.typing(message.chat_id)
            return httpx.Response(200, json={"success": True})
        to, kind = str(body["to"]), str(body["type"])
        if to in self.window_closed and kind != "template":
            return graph_error(
                131047,
                "Re-engagement message",
                "Message failed to send because more than 24 hours have passed since the "
                "customer last replied to this number.",
            )
        reply_to = (body.get("context") or {}).get("message_id")
        block: dict[str, Any] = body[kind]
        if kind == "text":
            sent = await self.platform.post(to, block["body"], reply_to=reply_to)
        elif kind == "interactive":
            sent = await self.platform.post(
                to, block["body"]["text"], reply_to=reply_to, buttons=_buttons(block)
            )
        elif kind in _UPLOAD_KINDS:
            found = self.media.get(str(block.get("id")))
            if found is None:
                return graph_error(131053, "Media upload error", "unknown media id")
            _, mime, name = found
            media = FakeMedia(
                _UPLOAD_KINDS[kind],  # pyright: ignore[reportArgumentType]
                name,
                mime,
                str(block.get("caption") or ""),
            )
            sent = await self.platform.upload(to, media, reply_to=reply_to)
        else:
            return graph_error(100, "Invalid parameter", f"type {kind} is not supported here")
        return httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "contacts": [{"input": to, "wa_id": to}],
                "messages": [{"id": sent.message_id}],
            },
        )

    def error_response(self, exc: FakePlatformError) -> httpx.Response:
        code, title = GRAPH_ERRORS.get(exc.kind, (131000, "Something went wrong"))
        status = 429 if exc.kind is FailureKind.RATE_LIMIT else 400
        response = graph_error(code, title, exc.text, status)
        if exc.kind is FailureKind.RATE_LIMIT and exc.retry_after is not None:
            response.headers["Retry-After"] = str(int(exc.retry_after))
        return response

    # -- webhooks ------------------------------------------------------------------------------

    def message_for(self, item: FakeInbound) -> dict[str, Any] | None:
        """Meta's message object for ``item``, or None when the Cloud API has no such event."""
        if item.event == "edited" or item.chat_kind != "dm" or item.is_bot:
            return None
        wamid = item.message_id or f"wamid.u{item.update_id}"
        message: dict[str, Any] = {
            "from": item.user_id,
            "id": wamid,
            "timestamp": str(int(item.at.timestamp())),
        }
        if item.event == "button":
            message["type"] = "interactive"
            message["interactive"] = {
                "type": "button_reply",
                "button_reply": {"id": item.button_data, "title": "Tap"},
            }
            if item.message_id:
                message["id"] = f"wamid.tap{item.update_id}"
                message["context"] = {"from": self.display_number, "id": item.message_id}
            return message
        if item.reply_to:
            quoted = self.platform.message(item.reply_to)
            author = self.display_number if quoted is not None and quoted.from_bot else item.user_id
            message["context"] = {"from": author, "id": item.reply_to}
        if item.media:
            media = item.media[0]
            data = f"bytes of {media.ref}".encode()
            self.media.setdefault(media.ref, (data, media.mime, media.ref))
            if media.kind in ("voice", "audio"):
                message["type"] = "audio"
                message["audio"] = {
                    "id": media.ref,
                    "mime_type": media.mime,
                    "voice": media.kind == "voice",
                }
                return message
            kind = _MEDIA_TYPES[media.kind]
            block: dict[str, Any] = {"id": media.ref, "mime_type": media.mime}
            if media.caption and kind != "sticker":
                block["caption"] = media.caption
            if kind == "document":
                block["filename"] = media.ref
            message["type"] = kind
            message[kind] = block
            return message
        message["type"] = "text"
        message["text"] = {"body": item.text}
        return message

    def envelope(
        self,
        messages: Iterable[Mapping[str, Any]] = (),
        *,
        statuses: Iterable[Mapping[str, Any]] = (),
        contacts: Iterable[Mapping[str, Any]] = (),
        phone_number_id: str | None = None,
        waba_id: str | None = None,
        field: str = "messages",
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "metadata": {
                "display_phone_number": self.display_number,
                "phone_number_id": phone_number_id or self.phone_number_id,
            },
        }
        found_contacts = list(contacts)
        found_messages = list(messages)
        found_statuses = list(statuses)
        if found_contacts:
            value["contacts"] = found_contacts
        if found_messages:
            value["messages"] = found_messages
        if found_statuses:
            value["statuses"] = found_statuses
        return {
            "object": "whatsapp_business_account",
            "entry": [
                {"id": waba_id or self.waba_id, "changes": [{"field": field, "value": value}]}
            ],
        }

    def signed(self, payload: Mapping[str, Any] | bytes, *, secret: str | None = None) -> Callback:
        body = payload if isinstance(payload, bytes) else json_body(payload)
        signature = "sha256=" + hmac_sha256_hex(secret or self.app_secret, body)
        headers = {"Content-Type": "application/json", "X-Hub-Signature-256": signature}
        return Callback(DEFAULT_PATH, body, headers)

    def handshake(self, mode: str, token: str, challenge: str) -> Callback:
        query = httpx.QueryParams(
            {"hub.mode": mode, "hub.verify_token": token, "hub.challenge": challenge}
        )
        return Callback(DEFAULT_PATH, b"", {}, method="GET", query=str(query))

    def render(self, item: FakeInbound) -> Callback | None:
        message = self.message_for(item)
        if message is None:
            return None
        contact = {"profile": {"name": item.user_name}, "wa_id": item.user_id}
        return self.signed(self.envelope([message], contacts=[contact]))


def _buttons(block: Mapping[str, Any]) -> tuple[Button, ...]:
    action: Mapping[str, Any] = block.get("action") or {}
    if block.get("type") == "button":
        return tuple(
            Button(b["reply"]["title"], b["reply"]["id"]) for b in action.get("buttons") or ()
        )
    rows = [row for section in action.get("sections") or () for row in section.get("rows") or ()]
    return tuple(Button(row["title"], row["id"]) for row in rows)


def _multipart(call: ApiCall) -> dict[str, tuple[bytes, str, str]]:
    """``name → (bytes, content type, file name)`` of a multipart/form-data body."""
    head = f"Content-Type: {call.headers.get('content-type', '')}\r\n\r\n".encode()
    parsed = BytesParser(policy=HTTP).parsebytes(head + call.body)
    fields: dict[str, tuple[bytes, str, str]] = {}
    for part in parsed.iter_parts():
        assert isinstance(part, Message)
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if isinstance(name, str) and isinstance(payload, bytes):
            fields[name] = (payload, part.get_content_type(), part.get_filename() or "")
    return fields
