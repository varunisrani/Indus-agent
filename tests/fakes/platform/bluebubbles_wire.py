"""A fake BlueBubbles server over the shared `FakeChatPlatform` (P16-24).

REST API v1 as the adapter uses it (every call must carry the server password as the
``password`` query parameter, else 401): ping, server info, webhooks (list, register, delete),
chat query / new / info / typing / read, message text / attachment (multipart) / react, attachment
download. Webhooks are delivered the way the real server does it: a POST to the exact URL that was
registered (path and query string, token included); with no registration nothing is delivered.

Chats: a DM's GUID is ``iMessage;-;<address>`` and its platform chat id is the address; a group's
GUID is its platform chat id (``iMessage;+;chat…``). Threads and bot accounts do not exist on
iMessage, so those inbound shapes render to nothing.
"""

from __future__ import annotations

import itertools
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from tests.fakes.platform.core import FakeChatPlatform, FakeInbound, FakeMedia, FakePlatformError
from tests.fakes.platform.http_wire import (
    ERROR_STATUS,
    ApiCall,
    Callback,
    HttpCallbackWire,
    api,
    json_body,
)

# Built at runtime: never a credential-shaped literal in the source.
PASSWORD = "".join(["bb", "-pass-", "e5f6" * 4])
SERVER_URL = "http://192.168.1.10:1234"
MAX_LEN = 4000

_VOICE_UTI = "com.apple.coreaudio-format"


def guid_of(chat_id: str, kind: str) -> str:
    """The chat GUID the server reports for a platform chat."""
    if ";" in chat_id:
        return chat_id
    return f"iMessage;-;{chat_id}" if kind == "dm" else f"iMessage;+;{chat_id}"


def chat_of(guid: str) -> str:
    """The platform chat id behind a chat GUID."""
    return guid.split(";-;", 1)[1] if ";-;" in guid else guid


def _ok(data: Any = None, message: str = "Success") -> httpx.Response:
    return httpx.Response(200, json={"status": 200, "message": message, "data": data})


class BlueBubblesWire(HttpCallbackWire):
    base_url = SERVER_URL
    # The server sits on the LAN.
    client_address = ("192.168.1.10", 51000)

    def __init__(
        self,
        platform: FakeChatPlatform,
        *,
        password: str = PASSWORD,
        private_api: bool = True,
        helper_connected: bool = True,
    ) -> None:
        super().__init__(platform)
        self.password = password
        self.private_api = private_api
        self.helper_connected = helper_connected
        self.webhooks: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}
        self.reads: list[str] = []
        self.reactions: list[dict[str, Any]] = []
        self.chat_queries = 0
        self._webhook_ids = itertools.count(1)

    # -- helpers --------------------------------------------------------------------------------

    def _denied(self, call: ApiCall) -> httpx.Response | None:
        if call.params.get("password") != self.password:
            return httpx.Response(
                401, json={"status": 401, "message": "Unauthorized", "error": {"type": "auth"}}
            )
        return None

    def error_response(self, exc: FakePlatformError) -> httpx.Response:
        status = ERROR_STATUS.get(exc.kind, 500)
        headers: dict[str, str] = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after))
        body = {"status": status, "message": "Error", "error": {"type": "x", "message": exc.text}}
        return httpx.Response(status, json=body, headers=headers)

    def _chats(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for chat in self.platform.chats.values():
            guid = guid_of(chat.chat_id, chat.kind)
            ident = chat.chat_id if chat.kind == "dm" else guid.rsplit(";", 1)[-1]
            rows.append({"guid": guid, "chatIdentifier": ident, "displayName": chat.name})
        return rows

    # -- server ---------------------------------------------------------------------------------

    @api("GET", "/api/v1/ping")
    async def ping(self, call: ApiCall) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        await self.platform.connect()
        return _ok("pong", "Ping received!")

    @api("GET", "/api/v1/server/info")
    async def info(self, call: ApiCall) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        data = {
            "server_version": "1.9.9",
            "private_api": self.private_api,
            "helper_connected": self.helper_connected,
        }
        return _ok(data)

    @api("GET", "/api/v1/webhook")
    async def list_webhooks(self, call: ApiCall) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        return _ok([dict(w) for w in self.webhooks])

    @api("POST", "/api/v1/webhook")
    async def add_webhook(self, call: ApiCall) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        body = call.json()
        row = {"id": next(self._webhook_ids), "url": body["url"], "events": body["events"]}
        self.webhooks.append(row)
        return _ok(row, "Successfully created webhook!")

    @api("DELETE", "/api/v1/webhook/{ident}")
    async def delete_webhook(self, call: ApiCall, ident: str) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        self.webhooks = [w for w in self.webhooks if str(w["id"]) != ident]
        if not self.webhooks:
            await self.platform.disconnect()
        return _ok(None, "Successfully deleted webhook!")

    # -- chats ----------------------------------------------------------------------------------

    @api("POST", "/api/v1/chat/query")
    async def query(self, call: ApiCall) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        self.chat_queries += 1
        return _ok(self._chats())

    @api("POST", "/api/v1/chat/new")
    async def new_chat(self, call: ApiCall) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        if not self.private_api:
            return httpx.Response(400, json={"status": 400, "message": "Private API required"})
        body = call.json()
        address = body["addresses"][0]
        if address not in self.platform.chats:
            self.platform.add_chat(address, "dm", address)
        sent = await self.platform.post(address, body["message"])
        data = {
            "guid": guid_of(address, "dm"),
            "chatIdentifier": address,
            "messages": [{"guid": sent.message_id}],
        }
        return _ok(data)

    @api("GET", "/api/v1/chat/{guid}")
    async def chat(self, call: ApiCall, guid: str) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        found = await self.platform.chat(chat_of(unquote(guid)))
        data = {"guid": unquote(guid), "displayName": found.name, "chatIdentifier": found.chat_id}
        return _ok(data)

    @api("POST", "/api/v1/chat/{guid}/typing")
    async def typing(self, call: ApiCall, guid: str) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        await self.platform.typing(chat_of(unquote(guid)))
        return _ok()

    @api("DELETE", "/api/v1/chat/{guid}/typing")
    async def typing_stop(self, call: ApiCall, guid: str) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        await self.platform.typing_stop(chat_of(unquote(guid)))
        return _ok()

    @api("POST", "/api/v1/chat/{guid}/read")
    async def read(self, call: ApiCall, guid: str) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        self.reads.append(unquote(guid))
        return _ok()

    # -- messages -------------------------------------------------------------------------------

    @api("POST", "/api/v1/message/text")
    async def text(self, call: ApiCall) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        body = call.json()
        sent = await self.platform.post(
            chat_of(body["chatGuid"]),
            body["message"],
            reply_to=body.get("selectedMessageGuid"),
            extra={"method": body.get("method"), "tempGuid": body.get("tempGuid")},
        )
        return _ok({"guid": sent.message_id, "text": body["message"]}, "Message sent!")

    @api("POST", "/api/v1/message/attachment")
    async def attachment(self, call: ApiCall) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        fields, files = self.multipart(call)
        name, data = files["attachment"]
        kind = "voice" if fields.get("isAudioMessage") == "true" else "document"
        self.files[f"sent-{name}"] = data
        sent = await self.platform.upload(chat_of(fields["chatGuid"]), FakeMedia(kind, name))
        return _ok({"guid": sent.message_id}, "Attachment sent!")

    @api("POST", "/api/v1/message/react")
    async def react(self, call: ApiCall) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        body = call.json()
        self.reactions.append(body)
        await self.platform.react(
            chat_of(body["chatGuid"]), body["selectedMessageGuid"], body["reaction"]
        )
        return _ok({"guid": f"react-{len(self.reactions)}"})

    @api("GET", "/api/v1/attachment/{guid}/download")
    async def download(self, call: ApiCall, guid: str) -> httpx.Response:
        if (denied := self._denied(call)) is not None:
            return denied
        data = self.files.get(unquote(guid))
        if data is None:
            return httpx.Response(404, json={"status": 404, "message": "Attachment not found"})
        return httpx.Response(200, content=data, headers={"Content-Type": "image/jpeg"})

    @staticmethod
    def multipart(call: ApiCall) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        """Form fields and files of a multipart request."""
        head = f"Content-Type: {call.headers['content-type']}\r\n\r\n".encode()
        message = BytesParser(policy=HTTP).parsebytes(head + call.body)
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes]] = {}
        for part in message.iter_parts():  # pyright: ignore[reportAttributeAccessIssue]
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            raw = part.get_payload(decode=True)
            payload = raw if isinstance(raw, bytes) else b""
            if filename:
                files[str(name)] = (filename, payload)
            else:
                fields[str(name)] = payload.decode()
        return fields, files

    # -- webhooks -------------------------------------------------------------------------------

    def message_data(self, item: FakeInbound) -> dict[str, Any]:
        """The ``data`` of a message webhook for ``item`` (BlueBubbles ≥ 1.9 shape)."""
        guid = guid_of(item.chat_id, item.chat_kind)
        attachments: list[dict[str, Any]] = []
        for media in item.media:
            att = f"att-{item.message_id}-{media.ref}"
            self.files[att] = b"\xff\xd8\xff" + media.ref.encode()
            row: dict[str, Any] = {
                "guid": att,
                "mimeType": media.mime,
                "transferName": media.ref,
                "totalBytes": 3 + len(media.ref),
            }
            if media.kind == "voice":
                row["uti"] = _VOICE_UTI
            attachments.append(row)
        stamp = int(item.at.timestamp() * 1000)
        return {
            "guid": item.message_id,
            "text": item.text or ("￼" if attachments else ""),
            "isFromMe": False,
            "handle": {"address": item.user_id},
            "chats": [
                {
                    "guid": guid,
                    "chatIdentifier": item.chat_id if item.chat_kind == "dm" else guid[-7:],
                    "displayName": item.chat_name if item.chat_kind != "dm" else "",
                    "style": 45 if item.chat_kind == "dm" else 43,
                }
            ],
            "attachments": attachments,
            "isAudioMessage": any(m.kind == "voice" for m in item.media),
            "dateCreated": stamp,
            "threadOriginatorGuid": item.reply_to,
            "associatedMessageType": None,
        }

    def callback(self, payload: dict[str, Any]) -> Callback | None:
        """A POST of ``payload`` to the registered URL (None when nothing is registered)."""
        rows = [w for w in self.webhooks if "new-message" in w["events"]]
        if not rows:
            return None
        url = urlsplit(rows[-1]["url"])
        headers = {"Content-Type": "application/json"}
        return Callback(url.path, json_body(payload), headers, query=url.query)

    def render(self, item: FakeInbound) -> Callback | None:
        if item.event == "button" or item.thread_id or item.is_bot:
            return None
        data = self.message_data(item)
        if item.event == "edited":
            data["dateEdited"] = int(self.platform.clock.now().timestamp() * 1000) + item.update_id
            return self.callback({"type": "updated-message", "data": data})
        return self.callback({"type": "new-message", "data": data})


__all__ = ["MAX_LEN", "PASSWORD", "SERVER_URL", "BlueBubblesWire", "chat_of", "guid_of"]
