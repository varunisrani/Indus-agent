"""A fake LINE Messaging API and webhook sender over the shared fake platform (P16-23).

REST (``@api`` handlers of `HttpCallbackWire`), shaped like LINE's v2 API:

- ``GET /v2/bot/info`` checks the bearer token (401 otherwise) and connects the platform;
- ``POST /v2/bot/message/reply`` takes a reply token this fake issued with a webhook event, unused
  and younger than ``token_ttl`` seconds (else 400 "Invalid reply token"), and posts into that
  token's chat; ``POST /v2/bot/message/push`` posts into ``to``. Both refuse more than five
  message objects, a text over 5,000 UTF-16 units and a template outside LINE's limits, and answer
  ``{"sentMessages": [{"id", "quoteToken"}]}``. A call is all or nothing as on LINE: when the
  platform fails part way (an armed failure), the bubbles already posted by this call are taken
  back before the failure is answered;
- ``POST /v2/bot/chat/loading/start`` shows typing; ``GET /v2/bot/message/{id}/content`` serves
  ``contents``.

Text, template (as a message with buttons; the button data is the postback data), image, audio and
video objects land on the platform. Errors use LINE's ``{"message": …}`` body.

Webhook: ``render`` turns platform inbound items into signed ``POST /line/webhook`` calls (a fresh
reply token per event). LINE has no threads, bot authors, edits or captions: those items render
to nothing (the caption-less photo still arrives). ``attach_listener`` also counts the adapter's
listener going down as the platform disconnecting (LINE sees the webhook go away), and ``fetch``
downloads a media URL the adapter handed to LINE from that listener.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import urllib.parse
from typing import Any

import httpx

from indus.gateway.adapters._http import CallbackListener
from tests.fakes.platform.core import (
    Button,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakeMessage,
    FakePlatformError,
)
from tests.fakes.platform.http_wire import (
    ERROR_STATUS,
    ApiCall,
    Callback,
    HttpCallbackWire,
    api,
    json_body,
)

WEBHOOK_PATH = "/line/webhook"
API_HOST = "https://api.line.me"


def line_signature(secret: str, body: bytes) -> str:
    """``X-Line-Signature``: base64 of the HMAC-SHA256 of the raw body under the secret."""
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class LineWire(HttpCallbackWire):
    base_url = API_HOST

    def __init__(self, platform: FakeChatPlatform, *, token: str, secret: str) -> None:
        super().__init__(platform)
        self.token = token
        self.secret = secret
        self.token_ttl = 60.0
        # reply token → (chat id, issued at (monotonic), used)
        self.reply_tokens: dict[str, list[Any]] = {}
        self.contents: dict[str, bytes] = {}
        self.listener: CallbackListener | None = None
        self._events = itertools.count(1)

    # -- REST -----------------------------------------------------------------------------------

    def _authorized(self, call: ApiCall) -> bool:
        return call.headers.get("authorization") == f"Bearer {self.token}"

    @staticmethod
    def _bad(message: str, status: int = 400) -> httpx.Response:
        return httpx.Response(status, json={"message": message})

    @api("GET", "/v2/bot/info")
    async def bot_info(self, call: ApiCall) -> httpx.Response:
        if not self._authorized(call):
            return self._bad("Authentication failed", 401)
        await self.platform.connect()
        return httpx.Response(
            200,
            json={
                "userId": self.platform.bot_user_id,
                "displayName": self.platform.bot_name,
                "basicId": "@indus",
            },
        )

    def _check_objects(self, messages: object) -> str | None:
        if not isinstance(messages, list) or not 1 <= len(messages) <= 5:
            return "messages: size must be between 1 and 5"
        for item in messages:
            if not isinstance(item, dict):
                return "messages: must be objects"
            kind = item.get("type")
            if kind == "text" and not 0 < _units(str(item.get("text") or "")) <= 5000:
                return "messages[].text: length must be between 1 and 5000"
            if kind == "template":
                template = item.get("template") or {}
                actions = template.get("actions") or []
                if len(str(item.get("altText") or "")) > 400 or not 0 < len(actions) <= 4:
                    return "template: altText or actions out of range"
                if _units(str(template.get("text") or "")) > 160:
                    return "template.text: must be 160 characters or fewer"
                for action in actions:
                    if _units(str(action.get("label") or "")) > 20:
                        return "actions[].label: must be 20 characters or fewer"
                    if len(str(action.get("data") or "")) > 300:
                        return "actions[].data: must be 300 characters or fewer"
            if kind in ("image", "video", "audio"):
                url = str(item.get("originalContentUrl") or "")
                if not url.startswith("https://"):
                    return "originalContentUrl: must be an https URL"
        return None

    async def _post_one(self, chat_id: str, item: dict[str, Any]) -> FakeMessage:
        kind = item["type"]
        if kind == "text":
            return await self.platform.post(chat_id, item["text"])
        if kind == "template":
            template = item["template"]
            buttons = tuple(Button(a["label"], a["data"]) for a in template["actions"])
            return await self.platform.post(chat_id, template["text"], buttons=buttons)
        media_kind = {"image": "photo", "audio": "voice", "video": "video"}[kind]
        media = FakeMedia(media_kind, item["originalContentUrl"])  # type: ignore[arg-type]
        return await self.platform.upload(chat_id, media)

    async def _post_batch(self, chat_id: str, messages: list[dict[str, Any]]) -> httpx.Response:
        posted: list[FakeMessage] = []
        try:
            for item in messages:
                posted.append(await self._post_one(chat_id, item))
        except FakePlatformError:
            for message in posted:  # LINE takes a call whole or not at all
                message.deleted = True
            raise
        sent = [{"id": m.message_id, "quoteToken": f"q{m.message_id}"} for m in posted]
        return httpx.Response(200, json={"sentMessages": sent})

    @api("POST", "/v2/bot/message/reply")
    async def reply(self, call: ApiCall) -> httpx.Response:
        if not self._authorized(call):
            return self._bad("Authentication failed", 401)
        body = call.json()
        problem = self._check_objects(body.get("messages"))
        if problem:
            return self._bad(problem)
        entry = self.reply_tokens.get(str(body.get("replyToken")))
        now = self.platform.clock.monotonic()
        if entry is None or entry[2] or now - entry[1] >= self.token_ttl:
            return self._bad("Invalid reply token")
        entry[2] = True
        return await self._post_batch(entry[0], body["messages"])

    @api("POST", "/v2/bot/message/push")
    async def push(self, call: ApiCall) -> httpx.Response:
        if not self._authorized(call):
            return self._bad("Authentication failed", 401)
        body = call.json()
        problem = self._check_objects(body.get("messages"))
        if problem:
            return self._bad(problem)
        return await self._post_batch(str(body.get("to")), body["messages"])

    @api("POST", "/v2/bot/chat/loading/start")
    async def loading(self, call: ApiCall) -> httpx.Response:
        body = call.json()
        seconds = body.get("loadingSeconds")
        if not isinstance(seconds, int) or seconds % 5 or not 5 <= seconds <= 60:
            return self._bad("loadingSeconds: must be a multiple of 5 from 5 to 60")
        await self.platform.typing(str(body.get("chatId")))
        return httpx.Response(202, json={})

    @api("GET", "/v2/bot/message/{message}/content")
    async def content(self, call: ApiCall, message: str) -> httpx.Response:
        if not self._authorized(call):
            return self._bad("Authentication failed", 401)
        data = self.contents.get(message)
        if data is None:
            return self._bad("Not found", 404)
        return httpx.Response(200, content=data, headers={"Content-Type": "image/jpeg"})

    def error_response(self, exc: FakePlatformError) -> httpx.Response:
        headers: dict[str, str] = {}
        if exc.retry_after is not None and exc.kind.value == "rate_limit":
            headers["Retry-After"] = str(int(exc.retry_after))
        status = ERROR_STATUS.get(exc.kind, 500)
        return httpx.Response(status, json={"message": exc.text}, headers=headers)

    def reply_calls(self) -> list[ApiCall]:
        return self.calls_to("POST", "/v2/bot/message/reply")

    def push_calls(self) -> list[ApiCall]:
        return self.calls_to("POST", "/v2/bot/message/push")

    # -- webhook --------------------------------------------------------------------------------

    def attach_listener(self, listener: CallbackListener) -> None:
        """Deliver callbacks to ``listener`` and see its going down as a disconnect."""
        self.attach(listener.app)
        self.listener = listener
        stop = listener.stop
        platform = self.platform

        async def stopping(budget: float = 5.0) -> None:
            await stop(budget)
            await platform.disconnect()

        listener.stop = stopping  # type: ignore[method-assign]

    def issue_token(self, chat_id: str) -> str:
        token = f"rt{next(self._events):030d}"
        self.reply_tokens[token] = [chat_id, self.platform.clock.monotonic(), False]
        return token

    def event(self, item: FakeInbound) -> dict[str, Any] | None:
        """The LINE webhook event for ``item``, or None when LINE has no such event."""
        if item.thread_id is not None or item.is_bot or item.event == "edited":
            return None
        if item.chat_kind == "dm":
            source: dict[str, Any] = {"type": "user", "userId": item.user_id}
        else:
            source = {"type": "group", "groupId": item.chat_id, "userId": item.user_id}
        base: dict[str, Any] = {
            "mode": "active",
            "timestamp": int(item.at.timestamp() * 1000),
            "source": source,
            "webhookEventId": f"01EV{item.update_id:022d}",
            "deliveryContext": {"isRedelivery": False},
            "replyToken": self.issue_token(item.chat_id),
        }
        if item.event == "button":
            return {**base, "type": "postback", "postback": {"data": item.button_data}}
        message: dict[str, Any] = {"id": item.message_id, "quoteToken": f"q{item.message_id}"}
        if item.media:
            media = item.media[0]
            kind = {"voice": "audio", "audio": "audio", "photo": "image", "video": "video"}.get(
                media.kind, "file"
            )
            message.update(type=kind, contentProvider={"type": "line"})
            if kind == "audio":
                message["duration"] = int((media.duration or 1.0) * 1000)
            if kind == "file":
                message.update(fileName=media.ref, fileSize=len(media.ref))
            self.contents[item.message_id] = b"fake bytes of " + media.ref.encode()
        else:
            text = item.text
            message.update(type="text", text=text)
            if item.mentions_bot:
                handle = f"@{self.platform.bot_name}"
                message["text"] = f"{handle} {text}"
                message["mention"] = {
                    "mentionees": [
                        {
                            "index": 0,
                            "length": _units(handle),
                            "type": "user",
                            "userId": self.platform.bot_user_id,
                            "isSelf": True,
                        }
                    ]
                }
        if item.reply_to:
            message["quotedMessageId"] = item.reply_to
        return {**base, "type": "message", "message": message}

    def signed(self, events: list[dict[str, Any]], *, signature: str | None = None) -> Callback:
        body = json_body({"destination": self.platform.bot_user_id, "events": events})
        headers = {
            "Content-Type": "application/json",
            "X-Line-Signature": signature or line_signature(self.secret, body),
        }
        return Callback(WEBHOOK_PATH, body, headers)

    def render(self, item: FakeInbound) -> Callback | None:
        event = self.event(item)
        return None if event is None else self.signed([event])

    async def fetch(self, url: str) -> httpx.Response:
        """Download a media URL the adapter gave LINE, from the adapter's own listener."""
        parts = urllib.parse.urlsplit(url)
        return await self.deliver(Callback(parts.path, b"", method="GET", query=parts.query))


__all__ = ["API_HOST", "WEBHOOK_PATH", "LineWire", "line_signature"]
