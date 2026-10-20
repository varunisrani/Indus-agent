"""A fake QQ Bot API v2 for the QQ Bot adapter (P16-20): REST, the gateway socket, uploads.

REST runs over one ``httpx.MockTransport`` (any host) on a `FakeChatPlatform`:

- ``POST /app/getAppAccessToken`` (app id + secret → a runtime-built token, ``expires_in`` as a
  string; the platform "connects" here), ``GET /gateway``;
- messages: ``/v2/users/<openid>``, ``/v2/groups/<openid>``, ``/channels/<id>``, ``/dms/<guild>``
  (``msg_type`` 0 text, 2 Markdown, 6 typing, 7 media; ``keyboard`` buttons become platform
  buttons; ``msg_id`` / ``msg_seq`` kept on the op);
- ``PUT /interactions/<id>`` (acknowledged ids in ``acks``);
- uploads: ``upload_prepare`` / ``upload_part_finish`` / ``files`` under ``/v2/{users|groups}/<id>``
  and the pre-signed part URLs ``PUT https://cos.fake.test/cos/<upload>/<part>``; scripted
  failures (``daily_limit``, ``put_failures``, ``finish_busy``, ``complete_failures``);
- media downloads ``GET https://multimedia.nt.qq.com.cn/download/<name>``.
Every QQ call needs ``Authorization: QQBot <token>`` (401 otherwise). Errors use QQ's
``{"code", "message"}`` body.

The gateway socket: ``open(url)`` (an async context manager, standing in for httpx-ws) yields a
`FakeQQSocket`. It says Hello, answers Identify with READY (a new session) and Resume with RESUMED
(same session) or op 9 (unknown session), answers heartbeats with op 11 (unless
``ack_heartbeats`` is off) and then serves dispatch frames rendered from the platform's queued
inbound traffic, numbered by one sequence that survives reconnects. ``drop_socket(code)`` closes it.

Chat ids: C2C chats are the user openid (the platform's DM convention), the others carry the
adapter's prefixes (``group:``, ``channel:``, ``dm:``).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal

import httpx

from indus.gateway.adapters.qqbot.client import SocketClosed
from tests.fakes.clock import checkpoint
from tests.fakes.platform.core import (
    Button,
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakePlatformError,
)
from tests.fakes.platform.http_wire import ERROR_STATUS, ApiCall, HttpCallbackWire, api

APP_ID = "102000001"
GATEWAY_URL = "wss://api.sgroup.qq.com/websocket"
MEDIA_HOST = "multimedia.nt.qq.com.cn"
COS_BASE = "https://cos.fake.test/cos"
HEARTBEAT_MS = 41_250
BOT_ID = "9000"
MAX_LEN = 4000
GUILD = "guild7"
_DROP = object()
_FILE_KINDS: dict[int, Literal["photo", "video", "voice", "document"]] = {
    1: "photo",
    2: "video",
    3: "voice",
    4: "document",
}


def fake_secret() -> str:
    """Built at runtime: never a credential-shaped literal in the source."""
    return "".join(["qq", "-secret-", "5e6f" * 4])


class FakeQQSocket:
    """One open gateway socket."""

    def __init__(self, wire: QQWire, url: str) -> None:
        self.wire = wire
        self.url = url
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._hello = False

    async def receive_text(self) -> str:
        if not self._hello:
            self._hello = True
            return json.dumps({"op": 10, "d": {"heartbeat_interval": self.wire.heartbeat_ms}})
        frame = await self.wire.next_frame()
        if isinstance(frame, SocketClosed):
            self.closed = True
            raise frame
        return frame

    async def send_text(self, data: str) -> None:
        frame = json.loads(data)
        self.sent.append(frame)
        self.wire.client_frames.append(frame)
        op = frame.get("op")
        d = frame.get("d") or {}
        if op == 1:
            self.wire.heartbeats.append(frame.get("d"))
            if self.wire.ack_heartbeats:
                self.wire.push({"op": 11})
        elif op == 2:
            if d.get("token") != f"QQBot {self.wire.token}":
                self.wire.push_close(4004, "invalid token")
                return
            self.wire.identify_count += 1
            self.wire.session_id = f"session-{self.wire.identify_count}"
            self.wire.dispatch(
                "READY",
                {
                    "session_id": self.wire.session_id,
                    "version": 1,
                    "user": {"id": BOT_ID, "bot": True},
                },
            )
        elif op == 6:
            if d.get("session_id") == self.wire.session_id and self.wire.session_id:
                self.wire.resume_frames.append(d)
                self.wire.dispatch("RESUMED", {})
            else:
                self.wire.push({"op": 9, "d": False})


class _Transport(httpx.MockTransport):
    def __init__(self, wire: QQWire) -> None:
        super().__init__(wire._handle)  # pyright: ignore[reportPrivateUsage]
        self._wire = wire

    async def aclose(self) -> None:
        await self._wire.platform.disconnect()


def _qq_error(status: int, message: str, code: int | None = None, **headers: str) -> httpx.Response:
    body: dict[str, Any] = {"message": message}
    if code is not None:
        body["code"] = code
    return httpx.Response(status, json=body, headers=headers)


class QQWire(HttpCallbackWire):
    """The fake QQ (see the module notes)."""

    base_url = "https://api.sgroup.qq.com"

    def __init__(self, platform: FakeChatPlatform) -> None:
        super().__init__(platform)
        self.app_id = APP_ID
        self.secret = fake_secret()
        self.token = "".join(["qq", "tok", "-0001"])
        self.tokens_issued = 0
        self.heartbeat_ms = HEARTBEAT_MS
        self.ack_heartbeats = True
        self.heartbeats: list[Any] = []
        self.client_frames: list[dict[str, Any]] = []
        self.resume_frames: list[dict[str, Any]] = []
        self.identify_count = 0
        self.session_id: str | None = None
        self.sockets: list[FakeQQSocket] = []
        self.socket_urls: list[str] = []
        self.socket_failures: list[BaseException] = []
        self.acks: list[str] = []
        self.media_files: dict[str, bytes] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.daily_limit = False
        self.put_failures: dict[int, int] = {}
        # How many part-finish calls answer "not ready, retry" (40093001) first.
        self.finish_busy = 0
        self.complete_failures = 0
        self.prepare_concurrency = 1
        self.block_size = 4
        self.max_parallel_puts = 0
        # Part PUTs hold until this many run at once (0: no hold), so a parallelism test depends on
        # the uploader's semaphore only, never on when the worker-thread file reads finish.
        self.put_gate = 0
        self._puts_running = 0
        self._gate_open: asyncio.Event | None = None
        self._seq = itertools.count(1)
        self._ids = itertools.count(1)
        self._frames: list[object] = []
        self._ready = asyncio.Event()

    # -- wiring ---------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return _Transport(self)

    def opener(self, http: httpx.AsyncClient) -> Any:
        """The adapter's opener factory (stands in for ``httpx_ws_opener``)."""
        return self.open

    @contextlib.asynccontextmanager
    async def open(self, url: str) -> AsyncIterator[FakeQQSocket]:
        self.socket_urls.append(url)
        if self.socket_failures:
            raise self.socket_failures.pop(0)
        socket = FakeQQSocket(self, url)
        self.sockets.append(socket)
        try:
            yield socket
        finally:
            socket.closed = True

    def _authorized(self, call: ApiCall) -> bool:
        return call.headers.get("authorization") == f"QQBot {self.token}"

    def error_response(self, exc: FakePlatformError) -> httpx.Response:
        headers: dict[str, str] = {}
        if exc.kind is FailureKind.RATE_LIMIT and exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after))
        return _qq_error(ERROR_STATUS.get(exc.kind, 500), exc.text, 11000, **headers)

    # -- the socket's frames ----------------------------------------------------------------------

    def push(self, frame: Mapping[str, Any]) -> None:
        self._frames.append(json.dumps(dict(frame)))
        self._ready.set()

    def dispatch(self, kind: str, data: Mapping[str, Any]) -> int:
        seq = next(self._seq)
        self.push({"op": 0, "s": seq, "t": kind, "d": dict(data)})
        return seq

    def push_close(self, code: int | None, reason: str = "") -> None:
        self._frames.append(SocketClosed(code, reason))
        self._ready.set()

    def drop_socket(self, code: int | None = None) -> None:
        """The server closes the open socket (with ``code``)."""
        self.push_close(code, "dropped by the fake")

    async def next_frame(self) -> str | SocketClosed:
        while True:
            if self._frames:
                frame = self._frames.pop(0)
                return frame if isinstance(frame, SocketClosed) else str(frame)
            item = self.platform.next_inbound()
            if item is not None:
                self._render_into_frames(item)
                continue
            self._ready.clear()
            getter = asyncio.ensure_future(self.platform.receive_inbound())
            ready = asyncio.ensure_future(self._ready.wait())
            try:
                done, _ = await asyncio.wait({getter, ready}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (getter, ready):
                    if not task.done():
                        task.cancel()
            if getter in done and not getter.cancelled():
                self._render_into_frames(getter.result())

    def _render_into_frames(self, item: FakeInbound) -> None:
        rendered = self.event_for(item)
        if rendered is not None:
            self.dispatch(*rendered)

    def _attachments(self, item: FakeInbound) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for media in item.media:
            url = f"//{MEDIA_HOST}/download/{media.ref}"
            self.media_files[media.ref] = f"bytes of {media.ref}".encode()
            if media.kind == "voice":
                self.media_files[media.ref + ".wav"] = b"RIFF wav"
                out.append(
                    {
                        "content_type": "voice",
                        "url": url,
                        "filename": media.ref,
                        "voice_wav_url": url + ".wav",
                        "asr_refer_text": "",
                    }
                )
            elif media.kind == "photo":
                out.append(
                    {"content_type": media.mime or "image/jpeg", "url": url, "filename": media.ref}
                )
            else:
                out.append({"content_type": "file", "url": url, "filename": media.ref})
        return out

    def event_for(self, item: FakeInbound) -> tuple[str, dict[str, Any]] | None:
        """The dispatch QQ would send for ``item`` (None for shapes QQ never delivers)."""
        if item.event == "edited" or item.thread_id:
            return None
        chat = item.chat_id
        when = item.at.isoformat()
        if item.event == "button":
            data: dict[str, Any] = {
                "id": item.button_id,
                "type": 11,
                "data": {
                    "type": 11,
                    "resolved": {
                        "button_data": item.button_data,
                        "button_id": "1",
                        "user_id": item.user_id,
                        "message_id": item.message_id,
                    },
                },
            }
            if chat.startswith("group:"):
                data.update(chat_type=1, group_openid=chat[6:], group_member_openid=item.user_id)
            elif chat.startswith("channel:"):
                data.update(chat_type=0, channel_id=chat[8:], guild_id=GUILD)
            else:
                data.update(chat_type=2, user_openid=item.user_id)
            return "INTERACTION_CREATE", data
        text = item.media[0].caption if item.media else item.text
        base: dict[str, Any] = {"id": item.message_id, "timestamp": when}
        if item.media:
            base["attachments"] = self._attachments(item)
        if chat.startswith("group:"):
            if not item.mentions_bot or item.is_bot:
                return None  # QQ delivers only @-mentions from people in groups
            return "GROUP_AT_MESSAGE_CREATE", {
                **base,
                "content": f" {text}",
                "group_openid": chat[6:],
                "author": {"member_openid": item.user_id, "id": item.user_id},
            }
        if chat.startswith("channel:"):
            content = f"<@!{BOT_ID}> {text}" if item.mentions_bot else text
            return "AT_MESSAGE_CREATE", {
                **base,
                "content": content,
                "channel_id": chat[8:],
                "guild_id": GUILD,
                "author": {
                    "id": item.user_id,
                    "username": item.user_name or item.user_id,
                    "bot": item.is_bot,
                },
                "member": {"nick": item.user_name},
            }
        if chat.startswith("dm:"):
            return "DIRECT_MESSAGE_CREATE", {
                **base,
                "content": text,
                "guild_id": chat[3:],
                "channel_id": "dmchan",
                "author": {"id": item.user_id, "username": item.user_name, "bot": item.is_bot},
            }
        if item.is_bot:
            return None
        return "C2C_MESSAGE_CREATE", {
            **base,
            "content": text,
            "author": {"user_openid": item.user_id, "id": item.user_id},
        }

    # -- REST -----------------------------------------------------------------------------------

    @api("POST", "/app/getAppAccessToken")
    async def get_token(self, call: ApiCall) -> httpx.Response:
        body = call.json() or {}
        if body.get("appId") != self.app_id or body.get("clientSecret") != self.secret:
            return httpx.Response(200, json={"code": 100016, "message": "invalid appid or secret"})
        await self.platform.connect()
        self.tokens_issued += 1
        return httpx.Response(200, json={"access_token": self.token, "expires_in": "7200"})

    @api("GET", "/gateway")
    async def gateway(self, call: ApiCall) -> httpx.Response:
        if not self._authorized(call):
            return _qq_error(401, "token invalid", 11244)
        return httpx.Response(200, json={"url": GATEWAY_URL})

    async def _message(self, call: ApiCall, chat: str) -> httpx.Response:
        if not self._authorized(call):
            return _qq_error(401, "token invalid", 11244)
        body = call.json() or {}
        kind = body.get("msg_type")
        extra = {k: body.get(k) for k in ("msg_id", "msg_seq", "msg_type")}
        if kind == 6:
            await self.platform.typing(chat)
            return httpx.Response(200, json={})
        if kind == 7:
            info = str((body.get("media") or {}).get("file_info") or "")
            upload = self.uploads.get(info)
            if upload is None:
                return _qq_error(400, "bad file_info", 40000)
            sent = await self.platform.upload(
                chat,
                FakeMedia(
                    _FILE_KINDS.get(upload["file_type"], "document"),
                    upload["file_name"],
                    caption=str(body.get("content") or ""),
                ),
                reply_to=body.get("msg_id"),
            )
            return httpx.Response(200, json={"id": sent.message_id})
        text = body.get("content")
        if text is None:
            text = (body.get("markdown") or {}).get("content", "")
        buttons = tuple(
            Button(b["render_data"]["label"], b["action"]["data"])
            for row in ((body.get("keyboard") or {}).get("content") or {}).get("rows") or ()
            for b in row["buttons"]
        )
        sent = await self.platform.post(
            chat, str(text), reply_to=body.get("msg_id"), buttons=buttons, extra=extra
        )
        return httpx.Response(200, json={"id": sent.message_id, "timestamp": "2026-01-01"})

    @api("POST", "/v2/users/{openid}/messages")
    async def send_c2c(self, call: ApiCall, openid: str) -> httpx.Response:
        return await self._message(call, openid)

    @api("POST", "/v2/groups/{group}/messages")
    async def send_group(self, call: ApiCall, group: str) -> httpx.Response:
        return await self._message(call, f"group:{group}")

    @api("POST", "/channels/{channel}/messages")
    async def send_channel(self, call: ApiCall, channel: str) -> httpx.Response:
        return await self._message(call, f"channel:{channel}")

    @api("POST", "/dms/{guild}/messages")
    async def send_dm(self, call: ApiCall, guild: str) -> httpx.Response:
        return await self._message(call, f"dm:{guild}")

    @api("PUT", "/interactions/{interaction}")
    async def ack(self, call: ApiCall, interaction: str) -> httpx.Response:
        if not self._authorized(call):
            return _qq_error(401, "token invalid", 11244)
        self.acks.append(interaction)
        await self.platform.answer(interaction)
        return httpx.Response(200, json={})

    @api("GET", "/download/{name}")
    async def download(self, call: ApiCall, name: str) -> httpx.Response:
        if not self._authorized(call):
            return _qq_error(401, "token invalid")
        if name not in self.media_files:
            return _qq_error(404, "no such file")
        return httpx.Response(200, content=self.media_files[name])

    # -- uploads ----------------------------------------------------------------------------------

    async def _prepare(self, call: ApiCall) -> httpx.Response:
        if not self._authorized(call):
            return _qq_error(401, "token invalid", 11244)
        if self.daily_limit:
            return _qq_error(400, "upload daily limit exceeded", 40093002)
        body = call.json()
        upload_id = f"up{next(self._ids)}"
        size = int(body["file_size"])
        count = max(1, -(-size // self.block_size))
        self.uploads[upload_id] = {
            **body,
            "parts": {},
            "finished": set(),
        }
        parts = [
            {"part_index": n, "presigned_url": f"{COS_BASE}/{upload_id}/{n}"}
            for n in range(1, count + 1)
        ]
        answer: dict[str, Any] = {
            "upload_id": upload_id,
            "block_size": self.block_size,
            "parts": parts,
            "concurrency": self.prepare_concurrency,
        }
        if self.finish_busy:
            answer["retry_timeout"] = 30
        return httpx.Response(200, json=answer)

    async def _finish(self, call: ApiCall) -> httpx.Response:
        body = call.json()
        upload = self.uploads.get(body.get("upload_id"))
        if upload is None:
            return _qq_error(404, "no such upload", 40400)
        if self.finish_busy > 0:
            self.finish_busy -= 1
            return _qq_error(400, "part not ready, retry", 40093001)
        upload["finished"].add(int(body["part_index"]))
        return httpx.Response(200, json={})

    async def _files(self, call: ApiCall) -> httpx.Response:
        body = call.json()
        upload_id = str(body.get("upload_id") or "")
        upload = self.uploads.get(upload_id)
        if upload is None:
            return _qq_error(400, "unknown upload", 40000)
        if self.complete_failures:
            self.complete_failures -= 1
            return _qq_error(502, "busy")
        expected = set(range(1, len(upload["parts"]) + 1)) if upload["parts"] else set()
        if upload["finished"] != expected:
            return _qq_error(400, "parts missing", 40000)
        data = b"".join(upload["parts"][n] for n in sorted(upload["parts"]))
        upload["data"] = data
        info = f"fi-{upload_id}"
        self.uploads[info] = upload
        return httpx.Response(200, json={"file_uuid": upload_id, "file_info": info, "ttl": 0})

    @api("POST", "/v2/users/{target}/upload_prepare")
    async def prepare_c2c(self, call: ApiCall, target: str) -> httpx.Response:
        return await self._prepare(call)

    @api("POST", "/v2/groups/{target}/upload_prepare")
    async def prepare_group(self, call: ApiCall, target: str) -> httpx.Response:
        return await self._prepare(call)

    @api("POST", "/v2/users/{target}/upload_part_finish")
    async def finish_c2c(self, call: ApiCall, target: str) -> httpx.Response:
        return await self._finish(call)

    @api("POST", "/v2/groups/{target}/upload_part_finish")
    async def finish_group(self, call: ApiCall, target: str) -> httpx.Response:
        return await self._finish(call)

    @api("POST", "/v2/users/{target}/files")
    async def files_c2c(self, call: ApiCall, target: str) -> httpx.Response:
        return await self._files(call)

    @api("POST", "/v2/groups/{target}/files")
    async def files_group(self, call: ApiCall, target: str) -> httpx.Response:
        return await self._files(call)

    @api("PUT", "/cos/{upload}/{part}")
    async def put_part(self, call: ApiCall, upload: str, part: str) -> httpx.Response:
        index = int(part)
        if "authorization" in call.headers:
            return httpx.Response(400, text="no QQ token on pre-signed URLs")
        self._puts_running += 1
        self.max_parallel_puts = max(self.max_parallel_puts, self._puts_running)
        try:
            if self.put_gate:
                if self._gate_open is None:
                    self._gate_open = asyncio.Event()
                if self._puts_running >= self.put_gate:
                    self._gate_open.set()
                await self._gate_open.wait()
            await checkpoint()
        finally:
            self._puts_running -= 1
        if self.put_failures.get(index, 0) > 0:
            self.put_failures[index] -= 1
            return httpx.Response(503, text="try again")
        self.uploads[upload]["parts"][index] = call.body
        return httpx.Response(200)


__all__ = [
    "APP_ID",
    "BOT_ID",
    "GATEWAY_URL",
    "GUILD",
    "HEARTBEAT_MS",
    "MAX_LEN",
    "MEDIA_HOST",
    "FakeQQSocket",
    "QQWire",
    "fake_secret",
]
