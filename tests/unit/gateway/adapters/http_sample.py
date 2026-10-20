"""A sample callback adapter and its wire fake: the template for HTTP-callback adapters (P16-01).

"Hook" is a made-up vendor shaped like the real callback platforms:

- REST: ``GET /v1/me`` (credential check; the fake platform connects), ``POST /v1/logout``,
  ``POST /v1/messages`` ``{chat, text, reply_to, thread, buttons}`` → ``{id}``,
  ``PATCH /v1/messages/{message}`` ``{chat, text}``, ``POST /v1/typing`` ``{chat}``,
  ``GET /v1/chats/{chat}`` → ``{name, type}``; bearer token auth.
- Callbacks: ``POST /hook`` with ``X-Hook-Timestamp`` (unix seconds), ``X-Hook-Delivery`` (a unique
  id) and ``X-Hook-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<body>">``; JSON
  ``{type: message|edited|button, chat: {id, type, name}, from: {id, name, bot},
  message: {id, text, thread, reply_to, mentions_bot, media: [{kind, ref, mime}]},
  button: {id, data}}``.

``HookAdapter`` shows the kit in use: the listener built from ``ListenerSettings`` and started in
``_open``, the signature and replay checks in the handler, the event handed to ``background`` so
the vendor gets its 200 at once, outbound failures through ``failure_from_exception`` /
``failure_from_status``. ``hook_case()`` runs it through the shared contract suite.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from typing import Any, ClassVar

import httpx

from indus.gateway.adapters._http import (
    BadPayload,
    CallbackListener,
    CallbackRequest,
    ListenerSettings,
    ReplayWindow,
    Reply,
    failure_from_exception,
    failure_from_status,
    hmac_sha256_hex,
    verify_hmac_sha256_hex,
)
from indus.gateway.adapters.base import (
    Adapter,
    AdapterConnectError,
    ApprovalPrompt,
    Capabilities,
    ChatInfo,
    approval_callback,
)
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult
from tests.contract.gateway.conftest import AdapterCase, CaseEnv
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import Button, FakeChatPlatform, FakeInbound
from tests.fakes.platform.http_wire import ApiCall, Callback, HttpCallbackWire, api, json_body

# Built at runtime: never a credential-shaped literal in the source.
TOKEN = "".join(["hook", "-tok-", "a1b2" * 4])
SIGNING_SECRET = "".join(["hook", "-sign-", "c3d4" * 4])

_KINDS = {
    "voice": MessageKind.VOICE,
    "audio": MessageKind.AUDIO,
    "photo": MessageKind.PHOTO,
    "video": MessageKind.VIDEO,
    "document": MessageKind.DOCUMENT,
    "sticker": MessageKind.STICKER,
}


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac_sha256_hex(secret, timestamp.encode() + b"." + body)


class HookWire(HttpCallbackWire):
    """The Hook vendor over a `FakeChatPlatform` (see the module notes)."""

    def __init__(self, platform: FakeChatPlatform, secret: str = SIGNING_SECRET) -> None:
        super().__init__(platform)
        self.secret = secret

    # -- REST -----------------------------------------------------------------------------------

    @api("GET", "/v1/me")
    async def me(self, call: ApiCall) -> httpx.Response:
        if call.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": {"message": "invalid token"}})
        await self.platform.connect()
        return httpx.Response(200, json={"id": self.platform.bot_user_id})

    @api("POST", "/v1/logout")
    async def logout(self, call: ApiCall) -> httpx.Response:
        await self.platform.disconnect()
        return httpx.Response(200, json={})

    @api("POST", "/v1/messages")
    async def post_message(self, call: ApiCall) -> httpx.Response:
        body = call.json()
        buttons = tuple(Button(b["label"], b["data"]) for b in body.get("buttons") or ())
        sent = await self.platform.post(
            body["chat"],
            body["text"],
            reply_to=body.get("reply_to"),
            thread_id=body.get("thread"),
            buttons=buttons,
        )
        return httpx.Response(200, json={"id": sent.message_id})

    @api("PATCH", "/v1/messages/{message}")
    async def edit_message(self, call: ApiCall, message: str) -> httpx.Response:
        body = call.json()
        await self.platform.edit(body["chat"], message, body["text"])
        return httpx.Response(200, json={"id": message})

    @api("POST", "/v1/typing")
    async def typing(self, call: ApiCall) -> httpx.Response:
        await self.platform.typing(call.json()["chat"])
        return httpx.Response(204)

    @api("GET", "/v1/chats/{chat}")
    async def chat(self, call: ApiCall, chat: str) -> httpx.Response:
        found = await self.platform.chat(chat)
        return httpx.Response(200, json={"name": found.name, "type": found.kind})

    # -- callbacks ------------------------------------------------------------------------------

    def payload(self, item: FakeInbound) -> dict[str, Any]:
        return {
            "type": item.event,
            "chat": {"id": item.chat_id, "type": item.chat_kind, "name": item.chat_name},
            "from": {"id": item.user_id, "name": item.user_name, "bot": item.is_bot},
            "message": {
                "id": item.message_id,
                "text": item.text,
                "thread": item.thread_id,
                "reply_to": item.reply_to,
                "mentions_bot": item.mentions_bot,
                "media": [{"kind": m.kind, "ref": m.ref, "mime": m.mime} for m in item.media],
            },
            "button": {"id": item.button_id, "data": item.button_data},
        }

    def signed(self, body: bytes, delivery: str, *, timestamp: str | None = None) -> Callback:
        stamp = timestamp or str(int(self.platform.clock.now().timestamp()))
        headers = {
            "Content-Type": "application/json",
            "X-Hook-Timestamp": stamp,
            "X-Hook-Delivery": delivery,
            "X-Hook-Signature": sign(self.secret, stamp, body),
        }
        return Callback("/hook", body, headers)

    def render(self, item: FakeInbound) -> Callback | None:
        return self.signed(json_body(self.payload(item)), f"d{item.update_id}")


class HookAdapter(Adapter):
    platform: ClassVar[str] = "hooktest"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=2000, supports_edit=True, supports_buttons=True, push_capable=True
    )

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        listener: ListenerSettings,
        serve: bool = True,
        token: str = TOKEN,
        signing_secret: str = SIGNING_SECRET,
        **kw: Any,
    ) -> None:
        super().__init__(secrets=(token, signing_secret), **kw)
        self.client = client
        self.token = token
        self.signing_secret = signing_secret
        self.listener = CallbackListener(
            listener, name=self.platform, clock=self.clock, scrub=self.scrub, serve=serve
        )
        self.listener.route(listener.path, self._callback)
        self.replays = ReplayWindow(self.clock, 300.0)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    # -- lifecycle ------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        try:
            answer = await self.client.get("/v1/me", headers=self._auth())
        except httpx.HTTPError as exc:
            raise AdapterConnectError(f"cannot reach Hook: {type(exc).__name__}") from None
        if answer.status_code == 401:
            raise AdapterConnectError("Hook refused the token", retryable=False, code="auth")
        if answer.status_code >= 400:
            raise AdapterConnectError(f"Hook answered {answer.status_code}")
        await self.listener.start()

    async def _close(self) -> None:
        await self.listener.stop()
        with contextlib.suppress(httpx.HTTPError):
            await self.client.post("/v1/logout", headers=self._auth())

    # -- inbound --------------------------------------------------------------------------------

    async def _callback(self, request: CallbackRequest) -> Reply:
        stamp = request.header("x-hook-timestamp") or ""
        signed = stamp.encode() + b"." + request.body
        offered = request.header("x-hook-signature")
        if not verify_hmac_sha256_hex(self.signing_secret, signed, offered, prefix="sha256="):
            return Reply.json({"error": "bad signature"}, 401)
        if not self.replays.fresh(stamp):
            return Reply.json({"error": "stale timestamp"}, 401)
        if not self.replays.first_time(request.header("x-hook-delivery") or offered or ""):
            return Reply.json({"status": "duplicate"})
        try:
            event = self.normalise(request.json())
        except (BadPayload, KeyError, TypeError, ValueError):
            return Reply.json({"error": "bad payload"}, 400)
        self.listener.background(self.emit(event))
        return Reply.json({"status": "ok"})

    def normalise(self, data: dict[str, Any]) -> InboundEvent:
        chat, sender, message = data["chat"], data["from"], data["message"]
        thread = message.get("thread")
        source = Source(
            platform=self.platform,
            chat_id=str(chat["id"]),
            chat_type="thread" if thread else chat["type"],
            chat_name=chat.get("name") or None,
            user_id=str(sender["id"]),
            user_name=sender.get("name") or None,
            thread_id=thread,
            is_bot=bool(sender.get("bot")),
        )
        metadata: dict[str, Any] = {}
        if data["type"] == "edited":
            metadata["edited"] = True
        if data["type"] == "button":
            metadata["button"] = data["button"]["data"]
        if message.get("mentions_bot"):
            metadata["mentioned"] = True
        media: Sequence[dict[str, str]] = message.get("media") or ()
        kind = _KINDS.get(media[0]["kind"], MessageKind.TEXT) if media else MessageKind.TEXT
        return InboundEvent(
            source=source,
            text=message.get("text") or "",
            kind=kind,
            message_id=message.get("id") or None,
            media=tuple(Attachment(ref=m["ref"], mime=m.get("mime", "")) for m in media),
            reply_to_id=message.get("reply_to"),
            metadata=metadata,
        )

    # -- outbound -------------------------------------------------------------------------------

    async def _call(self, method: str, path: str, body: dict[str, Any]) -> SendResult:
        try:
            answer = await self.client.request(method, path, json=body, headers=self._auth())
        except httpx.HTTPError as exc:
            return failure_from_exception(exc)
        if answer.status_code >= 400:
            return failure_from_status(
                answer.status_code, answer.text, answer.headers, now=self.clock.now()
            )
        sent = answer.json() if answer.content else {}
        return SendResult.sent(str(sent.get("id", "")))

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        body = {
            "chat": chat_id,
            "text": text,
            "reply_to": reply_to,
            "thread": meta.get("thread_id"),
        }
        return await self._call("POST", "/v1/messages", body)

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        return await self._call(
            "PATCH", f"/v1/messages/{message_id}", {"chat": chat_id, "text": text}
        )

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        await self.client.post("/v1/typing", json={"chat": chat_id}, headers=self._auth())

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        buttons = [
            {"label": a.label, "data": approval_callback(prompt.request_id, a.choice)}
            for a in prompt.actions
        ]
        return await self._call(
            "POST", "/v1/messages", {"chat": chat_id, "text": prompt.text, "buttons": buttons}
        )

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        answer = await self.client.get(f"/v1/chats/{chat_id}", headers=self._auth())
        answer.raise_for_status()
        data = answer.json()
        return ChatInfo(data["name"], data["type"])


# -- the contract case --------------------------------------------------------------------------


def make_env(clock: FakeClock) -> CaseEnv:
    platform = FakeChatPlatform("hooktest", clock=clock, max_len=2000)
    return CaseEnv(clock, platform, HookWire(platform))


def build(env: CaseEnv) -> HookAdapter:
    wire: HookWire = env.wire
    settings = ListenerSettings(port=0, max_body_bytes=65_536, path="/hook")
    adapter = HookAdapter(client=wire.client(), listener=settings, serve=False, clock=env.clock)
    wire.attach(adapter.listener.app)
    return adapter


async def pump(env: CaseEnv, adapter: Adapter) -> None:
    await env.wire.pump()
    assert isinstance(adapter, HookAdapter)
    await adapter.listener.idle()


def hook_case() -> AdapterCase:
    return AdapterCase("hooktest", make_env, build, pump=pump)
