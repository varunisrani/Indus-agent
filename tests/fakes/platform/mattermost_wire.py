"""A fake Mattermost server: REST v4 behind ``httpx.MockTransport`` plus a fake event socket
(P16-08).

The Mattermost adapter speaks plain HTTP (our REST client) and a WebSocket opened through a
``SocketOpener``; this fake serves every route in ``ROUTES`` (the client's ``client.ROUTES`` must be
a subset, checked by the adapter tests) over one `FakeChatPlatform`, and its ``open`` stands in for
``httpx_ws.aconnect_ws``. Nothing opens a socket.

- Ids are the platform's: channel id = chat id, post id = message id; a DM chat is a ``D`` channel,
  any other chat an ``O`` channel.
- The socket first sends ``hello``, then one ``posted`` frame per inbound update (``post`` and
  ``mentions`` JSON-encoded inside the frame, as the server does): ``root_id`` for threads,
  ``props.from_bot`` for bots, ``file_ids`` plus ``metadata.files`` for media. Edits arrive as
  ``post_edited``. ``drop_socket()`` closes the current socket; ``socket_failures`` make the next
  opens fail (a ``FakeUpgradeError`` carries the refused upgrade's response).
- Platform failures become the server's answers: 429 with ``X-Ratelimit-Reset``, 404
  ``app.channel.get.existing.app_error``, 400 ``api.post.create_post.root_id.app_error``, 403
  ``api.context.permissions.app_error``, 400 ``api.context.invalid_body_param.app_error``, 400
  ``model.post.is_valid.msg.app_error``; connect and read timeouts raise httpx's own exceptions.
- A request (or socket upgrade) without the right bearer token gets 401.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from email.parser import BytesParser
from email.policy import HTTP
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import httpx

from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import (
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakePlatformError,
)

if TYPE_CHECKING:
    from indus.gateway.adapters.mattermost.adapter import MattermostAdapter, MattermostOptions

BASE_URL = "https://mm.example.org"
BOT_ID = "botuser0000000000000000001"
BOT_NAME = "indusbot"
MAX_LEN = 4000

Handler = Callable[[httpx.Request, list[str]], Awaitable[httpx.Response]]

ROUTES: dict[str, str] = {
    "me": "GET /api/v4/users/me",
    "create_post": "POST /api/v4/posts",
    "patch_post": "PUT /api/v4/posts/{postId}/patch",
    "delete_post": "DELETE /api/v4/posts/{postId}",
    "get_post": "GET /api/v4/posts/{postId}",
    "typing": "POST /api/v4/users/{userId}/typing",
    "channel": "GET /api/v4/channels/{channelId}",
    "upload": "POST /api/v4/files",
    "download": "GET /api/v4/files/{fileId}",
    "teams": "GET /api/v4/users/me/teams",
    "team_channels": "GET /api/v4/users/me/teams/{teamId}/channels",
    "add_reaction": "POST /api/v4/reactions",
    "remove_reaction": "DELETE /api/v4/users/{userId}/posts/{postId}/reactions/{emojiName}",
    "websocket": "GET /api/v4/websocket",
}


def fake_token() -> str:
    """A token-looking value built at runtime (never a credential-shaped literal)."""
    return "".join(["mm", "fake", "tok", "9" * 20])


def _error(status: int, error_id: str, message: str, **headers: str) -> httpx.Response:
    body = {"id": error_id, "message": message, "status_code": status, "request_id": "req1"}
    return httpx.Response(status, json=body, headers=headers)


def _failure(
    exc: FakePlatformError, request: httpx.Request, *, on_post: bool = False
) -> httpx.Response:
    kind = exc.kind
    if kind is FailureKind.CONNECT_TIMEOUT:
        raise httpx.ConnectTimeout(exc.text, request=request)
    if kind is FailureKind.READ_TIMEOUT:
        raise httpx.ReadTimeout(exc.text, request=request)
    if kind is FailureKind.RATE_LIMIT:
        wait = exc.retry_after if exc.retry_after is not None else 5.0
        headers = {"X-Ratelimit-Reset": str(int(wait))}
        return _error(429, "api.context.rate_limit.app_error", "Too many requests", **headers)
    if kind is FailureKind.CHAT_NOT_FOUND:
        return _error(404, "app.channel.get.existing.app_error", "Unable to find the channel.")
    if kind is FailureKind.THREAD_NOT_FOUND:
        if on_post:
            return _error(404, "app.post.get.app_error", "Unable to get the post.")
        return _error(400, "api.post.create_post.root_id.app_error", "Invalid RootId parameter.")
    if kind is FailureKind.FORBIDDEN:
        return _error(
            403, "api.context.permissions.app_error", "You do not have the appropriate permissions."
        )
    if kind is FailureKind.PARSE_ERROR:
        return _error(
            400, "api.context.invalid_body_param.app_error", "Invalid or missing post in body."
        )
    return _error(400, "model.post.is_valid.msg.app_error", "Invalid Message: message too long")


def _pattern(template: str) -> re.Pattern[str]:
    method, _, path = template.partition(" ")
    body = re.sub(r"\\\{[A-Za-z]+\\\}", "([^/]+)", re.escape(path))
    return re.compile(f"^{method} {body}$")


class FakeUpgradeError(Exception):
    """A refused WebSocket upgrade, shaped like ``httpx_ws.WebSocketUpgradeError``."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"upgrade refused (HTTP {response.status_code})")
        self.response = response


class FakeSocket:
    """One open event socket."""

    def __init__(self, wire: MattermostWire) -> None:
        self.wire = wire
        self.sent: list[str] = []
        self.closed = False
        self._hello = False

    async def receive_text(self) -> str:
        if not self._hello:
            self._hello = True
            return json.dumps({"event": "hello", "data": {"server_version": "10.0"}, "seq": 0})
        while True:
            frame = await self.wire.next_frame()
            if frame is not None:
                return frame

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class _Transport(httpx.MockTransport):
    def __init__(self, wire: MattermostWire) -> None:
        super().__init__(wire.handle)
        self._wire = wire

    async def aclose(self) -> None:
        await self._wire.platform.disconnect()


_DROP = object()


class MattermostWire:
    """The fake server (see the module notes)."""

    ROUTES = ROUTES

    def __init__(
        self,
        platform: FakeChatPlatform | None = None,
        *,
        clock: FakeClock | None = None,
        bot_id: str = BOT_ID,
        username: str = BOT_NAME,
        token: str | None = None,
    ) -> None:
        self.platform = platform or FakeChatPlatform(
            "mattermost", clock=clock, max_len=MAX_LEN, bot_user_id=bot_id, bot_name=username
        )
        self.clock: FakeClock = self.platform.clock
        self.bot_id = bot_id
        self.username = username
        self.token = token or fake_token()
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, dict[str, Any]]] = []
        self.files: dict[str, tuple[str, bytes, str]] = {}
        self.reactions: list[tuple[str, str, str]] = []
        self.typing_calls: list[dict[str, Any]] = []
        self.socket_failures: list[BaseException] = []
        self.sockets: list[FakeSocket] = []
        self.socket_urls: list[str] = []
        self.socket_attempts: list[float] = []
        self.teams = [{"id": "team1", "name": "acme", "display_name": "Acme"}]
        self._frames: list[object] = []
        self._ready = asyncio.Event()
        self._ids = itertools.count(1)
        self._seq = itertools.count(1)
        self._routes: list[tuple[re.Pattern[str], Handler]] = [
            (_pattern(template), getattr(self, f"_route_{name}"))
            for name, template in ROUTES.items()
            if name != "websocket"
        ]

    # -- wiring ------------------------------------------------------------------------------------

    def transport(self) -> httpx.AsyncBaseTransport:
        return _Transport(self)

    def adapter(self, *, options: MattermostOptions | None = None, **kw: Any) -> MattermostAdapter:
        from indus.gateway.adapters.mattermost.adapter import MattermostAdapter

        kw.setdefault("clock", self.clock)
        kw.setdefault("rng", lambda: 0.0)
        return MattermostAdapter(
            base_url=BASE_URL,
            token=self.token,
            transport=self.transport(),
            opener=self.open,
            options=options,
            **kw,
        )

    @contextlib.asynccontextmanager
    async def open(self, client: httpx.AsyncClient, url: str) -> AsyncIterator[FakeSocket]:
        """Stands in for ``httpx_ws.aconnect_ws(url, client)``."""
        self.socket_urls.append(url)
        self.socket_attempts.append(self.clock.monotonic())
        if client.headers.get("Authorization") != f"Bearer {self.token}":
            raise FakeUpgradeError(httpx.Response(401, request=httpx.Request("GET", url)))
        if self.socket_failures:
            raise self.socket_failures.pop(0)
        socket = FakeSocket(self)
        self.sockets.append(socket)
        try:
            yield socket
        finally:
            socket.closed = True

    def push_frame(self, frame: Mapping[str, Any] | str) -> None:
        """Queue a raw socket frame."""
        self._frames.append(frame if isinstance(frame, str) else json.dumps(frame))
        self._ready.set()

    def drop_socket(self) -> None:
        """The server closes the current socket."""
        self._frames.append(_DROP)
        self._ready.set()

    def posted(self, post: Mapping[str, Any], **data: Any) -> dict[str, Any]:
        """A ``posted`` frame around ``post`` (``data`` adds or overrides event fields)."""
        fields: dict[str, Any] = {"channel_type": "O", "sender_name": "@someone"}
        fields.update(data)
        fields["post"] = json.dumps(dict(post))
        return {"event": "posted", "data": fields, "seq": next(self._seq)}

    async def next_frame(self) -> str | None:
        """The next frame for the open socket; waits for traffic (no clock involved)."""
        while True:
            if self._frames:
                frame = self._frames.pop(0)
                if frame is _DROP:
                    raise ConnectionResetError("the server closed the socket")
                return str(frame)
            item = self.platform.next_inbound()
            if item is not None:
                return self._frame_of(item)
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
                return self._frame_of(getter.result())

    def _frame_of(self, item: FakeInbound) -> str | None:
        if item.event == "button":
            return None
        post: dict[str, Any] = {
            "id": item.message_id,
            "channel_id": item.chat_id,
            "user_id": item.user_id,
            "message": item.text,
            "root_id": item.thread_id or "",
            "create_at": int(item.at.timestamp() * 1000),
            "type": "",
            "props": {"from_bot": "true"} if item.is_bot else {},
        }
        if item.media:
            infos: list[dict[str, Any]] = []
            for media in item.media:
                file_id = f"file{next(self._ids)}"
                blob = f"bytes of {media.ref}".encode()
                self.files[file_id] = (media.ref, blob, media.mime)
                infos.append(
                    {"id": file_id, "name": media.ref, "mime_type": media.mime, "size": len(blob)}
                )
            post["file_ids"] = [info["id"] for info in infos]
            post["metadata"] = {"files": infos}
            post["message"] = item.media[0].caption
        data: dict[str, Any] = {
            "channel_type": "D" if item.chat_kind == "dm" else "O",
            "channel_name": item.chat_id,
            "channel_display_name": item.chat_name,
            "sender_name": "@" + (item.user_name or item.user_id),
            "team_id": "team1",
            "post": json.dumps(post),
        }
        if item.mentions_bot:
            data["mentions"] = json.dumps([self.bot_id])
        event = "post_edited" if item.event == "edited" else "posted"
        return json.dumps({"event": event, "data": data, "seq": next(self._seq)})

    # -- REST ------------------------------------------------------------------------------------

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        raw = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
        self.requests.append((request.method, raw))
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return _error(
                401, "api.context.session_expired.app_error", "Invalid or expired session"
            )
        line = f"{request.method} {raw}"
        for pattern, handler in self._routes:
            found = pattern.match(line)
            if found is not None:
                return await handler(request, [unquote(p) for p in found.groups()])
        return _error(404, "api.context.404.app_error", "Sorry, we could not find the page.")

    def _json(self, request: httpx.Request) -> dict[str, Any]:
        body = json.loads(request.content or b"{}")
        self.bodies.append((request.url.path, body))
        return body

    @staticmethod
    def _post_json(message: Any, **extra: Any) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": message.message_id,
                "channel_id": message.chat_id,
                "root_id": message.thread_id or "",
                "message": message.text,
                **extra,
            },
        )

    async def _route_me(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        try:
            await self.platform.connect()
        except FakePlatformError as exc:
            return _failure(exc, request)
        return httpx.Response(200, json={"id": self.bot_id, "username": self.username})

    async def _route_create_post(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        body = self._json(request)
        channel = str(body.get("channel_id") or "")
        root = str(body.get("root_id") or "") or None
        message = str(body.get("message") or "")
        if root is not None and self.platform.message(root) is None:
            return _error(
                400, "api.post.create_post.root_id.app_error", "Invalid RootId parameter."
            )
        try:
            file_ids = list(body.get("file_ids") or [])
            if file_ids:
                name, _data, mime = self.files[file_ids[0]]
                kind = (
                    "photo"
                    if mime.startswith("image/")
                    else "voice"
                    if mime.startswith("audio/")
                    else "video"
                    if mime.startswith("video/")
                    else "document"
                )
                media = FakeMedia(kind, name, mime=mime, caption=message)  # pyright: ignore[reportArgumentType]
                sent = await self.platform.upload(channel, media, thread_id=root)
            else:
                sent = await self.platform.post(channel, message, thread_id=root)
        except FakePlatformError as exc:
            return _failure(exc, request)
        return self._post_json(sent)

    async def _route_patch_post(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        (post_id,) = args
        body = self._json(request)
        found = self.platform.message(post_id)
        if found is None:
            return _error(404, "app.post.get.app_error", "Unable to get the post.")
        try:
            edited = await self.platform.edit(found.chat_id, post_id, str(body.get("message", "")))
        except FakePlatformError as exc:
            return _failure(exc, request, on_post=True)
        return self._post_json(edited)

    async def _route_delete_post(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        (post_id,) = args
        found = self.platform.message(post_id)
        if found is None:
            return _error(404, "app.post.get.app_error", "Unable to get the post.")
        try:
            await self.platform.delete(found.chat_id, post_id)
        except FakePlatformError as exc:
            return _failure(exc, request, on_post=True)
        return httpx.Response(200, json={"status": "OK"})

    async def _route_get_post(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        (post_id,) = args
        found = self.platform.message(post_id)
        if found is None:
            return _error(404, "app.post.get.app_error", "Unable to get the post.")
        return self._post_json(found)

    async def _route_typing(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        body = self._json(request)
        self.typing_calls.append(body)
        try:
            await self.platform.typing(
                str(body.get("channel_id") or ""),
                thread_id=str(body.get("parent_id") or "") or None,
            )
        except FakePlatformError as exc:
            return _failure(exc, request)
        return httpx.Response(200, json={"status": "OK"})

    def _channel_json(self, chat_id: str) -> dict[str, Any]:
        chat = self.platform.chats[chat_id]
        code = {"dm": "D", "group": "G"}.get(chat.kind, "O")
        return {"id": chat_id, "name": chat_id, "display_name": chat.name, "type": code}

    async def _route_channel(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        (channel_id,) = args
        try:
            await self.platform.chat(channel_id)
        except FakePlatformError as exc:
            return _failure(exc, request)
        return httpx.Response(200, json=self._channel_json(channel_id))

    async def _route_upload(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        content_type = request.headers.get("Content-Type", "")
        message = BytesParser(policy=HTTP).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content
        )
        channel = ""
        infos: list[dict[str, Any]] = []
        for part in message.iter_parts():  # pyright: ignore[reportAttributeAccessIssue]
            field = part.get_param("name", header="content-disposition")
            raw = part.get_payload(decode=True)
            payload = raw if isinstance(raw, bytes) else b""
            if field == "channel_id":
                channel = payload.decode()
            elif field == "files":
                file_id = f"up{next(self._ids)}"
                name = str(part.get_filename() or file_id)
                mime = part.get_content_type()
                self.files[file_id] = (name, payload, mime)
                infos.append({"id": file_id, "name": name, "size": len(payload)})
        if not channel:
            return _error(400, "api.file.upload_file.storage.app_error", "No channel id")
        return httpx.Response(201, json={"file_infos": infos, "client_ids": []})

    async def _route_download(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        (file_id,) = args
        found = self.files.get(file_id)
        if found is None:
            return _error(404, "app.file_info.get.app_error", "Unable to get the file info.")
        _name, data, mime = found
        return httpx.Response(200, content=data, headers={"Content-Type": mime or "x/unknown"})

    async def _route_teams(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        return httpx.Response(200, json=self.teams)

    async def _route_team_channels(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        return httpx.Response(200, json=[self._channel_json(c) for c in self.platform.chats])

    async def _route_add_reaction(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        body = self._json(request)
        post_id = str(body.get("post_id") or "")
        emoji = str(body.get("emoji_name") or "")
        found = self.platform.message(post_id)
        if found is None:
            return _error(404, "app.post.get.app_error", "Unable to get the post.")
        await self.platform.react(found.chat_id, post_id, emoji)
        self.reactions.append(("add", post_id, emoji))
        return httpx.Response(201, json=body)

    async def _route_remove_reaction(
        self, request: httpx.Request, args: list[str]
    ) -> httpx.Response:
        _user, post_id, emoji = args
        self.reactions.append(("remove", post_id, emoji))
        return httpx.Response(200, json={"status": "OK"})


__all__ = [
    "BASE_URL",
    "BOT_ID",
    "BOT_NAME",
    "ROUTES",
    "FakeSocket",
    "FakeUpgradeError",
    "MattermostWire",
    "fake_token",
]
