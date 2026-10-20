"""A fake ``simplex-chat`` daemon WebSocket over the shared `FakeChatPlatform` (P16-28).

``SimplexWire.open`` is the adapter's socket opener: opening connects the platform (an armed
connect failure refuses the socket), closing disconnects it. Commands the fake answers (each answer
carries the command's ``corrId``): ``/user``, ``/_send @<id>|#<id> json [...]``, ``/_accept <id>``,
``/freceive <id>``, ``/contacts``, ``/groups``. Armed send failures behave like the daemon and the
socket would: a connect failure makes the write raise (nothing sent), a read timeout leaves the
command unanswered, anything else answers ``chatCmdError`` with the failure text.

Inbound: ``push_inbound()`` renders every item queued on the platform as a ``newChatItems`` event
(edits as ``chatItemUpdated``); DMs are contacts (chat id = contact id), groups are
``group:<groupId>`` with the sender as the group member. SimpleX has no threads or buttons, so
those render to nothing. ``push(frame)`` sends any frame; ``drop()`` breaks the open socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from tests.fakes.platform.core import (
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakePlatformError,
)

MAX_LEN = 8000
URL = "ws://127.0.0.1:5225"
BOT_NAME = "indus"

_SEND = re.compile(r"^/_send ([@#])(\d+) json (.*)$", re.DOTALL)
_KIND = {"photo": "image", "voice": "voice", "video": "video", "document": "file"}


class _Closed(Exception):
    """The fake socket was dropped (httpx-ws raises its own disconnect error)."""


class FakeSocket:
    def __init__(self, wire: SimplexWire) -> None:
        self.wire = wire
        self.frames: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.open = True

    async def receive_text(self) -> str:
        frame = await self.frames.get()
        if frame is None:
            self.open = False
            raise _Closed("socket dropped")
        return frame

    async def send_text(self, data: str) -> None:
        if not self.open:
            raise _Closed("socket closed")
        message = json.loads(data)
        self.sent.append(message)
        await self.wire.handle(self, message["corrId"], message["cmd"])

    def answer(self, corr: str, resp: dict[str, Any]) -> None:
        self.frames.put_nowait(json.dumps({"corrId": corr, "resp": resp}))


class SimplexWire:
    def __init__(
        self,
        platform: FakeChatPlatform | None = None,
        *,
        files_folder: Path | None = None,
    ) -> None:
        self.platform = platform or FakeChatPlatform("simplex", max_len=MAX_LEN)
        self.files_folder = files_folder
        self.socket: FakeSocket | None = None
        self.opens = 0
        self.refuse = False
        self.mute = False
        self.accepted: list[str] = []
        self.received: list[str] = []
        self.commands: list[str] = []
        self.contacts: dict[str, str] = {}
        self._file_ids = itertools.count(1)
        self.file_names: dict[str, str] = {}

    # -- the opener -----------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def open(self, url: str) -> AsyncIterator[FakeSocket]:
        if self.refuse:
            raise ConnectionRefusedError("connection refused")
        try:
            await self.platform.connect()
        except FakePlatformError as exc:
            raise ConnectionRefusedError(str(exc)) from None
        self.opens += 1
        socket = FakeSocket(self)
        self.socket = socket
        try:
            yield socket
        finally:
            socket.open = False
            if self.socket is socket:
                self.socket = None
            await self.platform.disconnect()

    def push(self, frame: dict[str, Any] | str) -> None:
        assert self.socket is not None, "no socket is open"
        text = frame if isinstance(frame, str) else json.dumps(frame)
        self.socket.frames.put_nowait(text)

    def drop(self) -> None:
        if self.socket is not None:
            self.socket.frames.put_nowait(None)

    # -- commands -------------------------------------------------------------------------------

    async def handle(self, socket: FakeSocket, corr: str, command: str) -> None:
        self.commands.append(command)
        if self.mute:
            return
        if command == "/user":
            user = {"userId": 1, "localDisplayName": BOT_NAME, "profile": {"displayName": BOT_NAME}}
            socket.answer(corr, {"type": "activeUser", "user": user})
            return
        if command.startswith("/_accept "):
            self.accepted.append(command.split(" ", 1)[1])
            socket.answer(corr, {"type": "acceptingContactRequest", "contact": {}})
            return
        if command.startswith("/freceive "):
            file_id = command.split(" ", 1)[1]
            self.received.append(file_id)
            socket.answer(corr, {"type": "rcvFileAccepted", "chatItem": {}})
            self._complete(file_id)
            return
        if command == "/contacts":
            rows = [{"contactId": int(c), "localDisplayName": n} for c, n in self.contacts.items()]
            socket.answer(corr, {"type": "contactsList", "contacts": rows})
            return
        if command == "/groups":
            groups = [
                [{"groupId": int(c.chat_id[6:]), "localDisplayName": c.name}, {}]
                for c in self.platform.chats.values()
                if c.chat_id.startswith("group:")
            ]
            socket.answer(corr, {"type": "groupsList", "groups": groups})
            return
        found = _SEND.match(command)
        if found is not None:
            await self._send(socket, corr, found.group(1), found.group(2), found.group(3))
            return
        socket.answer(corr, {"type": "chatCmdError", "chatError": {"type": "error"}})

    async def _send(self, socket: FakeSocket, corr: str, sigil: str, ident: str, raw: str) -> None:
        chat_id = f"group:{ident}" if sigil == "#" else ident
        items = json.loads(raw)
        rows: list[dict[str, Any]] = []
        for item in items:
            content = item["msgContent"]
            quoted = item.get("quotedItemId")
            reply_to = str(quoted) if quoted is not None else None
            try:
                source = item.get("fileSource") or {}
                if source.get("filePath"):
                    kind = "voice" if content["type"] == "voice" else "document"
                    media = FakeMedia(kind, Path(source["filePath"]).name, caption=content["text"])
                    sent = await self.platform.upload(chat_id, media, reply_to=reply_to)
                else:
                    sent = await self.platform.post(chat_id, content["text"], reply_to=reply_to)
            except FakePlatformError as exc:
                if exc.kind is FailureKind.CONNECT_TIMEOUT:
                    raise _Closed("the write did not go through") from None
                if exc.kind is FailureKind.READ_TIMEOUT:
                    return  # never answered
                error = {
                    "type": "error",
                    "errorType": {"type": "commandError", "message": exc.text},
                }
                socket.answer(corr, {"type": "chatCmdError", "chatError": error})
                return
            info = self._chat_info(chat_id)
            rows.append(
                {
                    "chatInfo": info,
                    "chatItem": {
                        "chatDir": {"type": "groupSnd" if sigil == "#" else "directSnd"},
                        "meta": {"itemId": int(sent.message_id)},
                        "content": {"type": "sndMsgContent", "msgContent": content},
                    },
                }
            )
        socket.answer(corr, {"type": "newChatItems", "chatItems": rows})

    def _complete(self, file_id: str) -> None:
        if self.files_folder is None or self.socket is None:
            return
        path = self.files_folder / self.file_names.get(file_id, f"file-{file_id}")
        path.write_bytes(b"OggS" + file_id.encode())
        item = {"file": {"fileId": int(file_id), "fileSource": {"filePath": str(path)}}}
        self.push({"resp": {"type": "rcvFileComplete", "chatItem": {"chatItem": item}}})

    # -- inbound --------------------------------------------------------------------------------

    def _chat_info(self, chat_id: str) -> dict[str, Any]:
        chat = self.platform.chats.get(chat_id)
        name = chat.name if chat is not None else chat_id
        if chat_id.startswith("group:"):
            return {
                "type": "group",
                "groupInfo": {"groupId": int(chat_id[6:]), "localDisplayName": name},
            }
        return {
            "type": "direct",
            "contact": {"contactId": int(chat_id), "localDisplayName": name, "profile": {}},
        }

    def row(self, item: FakeInbound) -> dict[str, Any] | None:
        """The ``{chatInfo, chatItem}`` row for ``item`` (None for shapes SimpleX lacks)."""
        if item.event == "button" or item.thread_id:
            return None
        profile: dict[str, Any] = {"displayName": item.user_name or item.user_id}
        if item.is_bot:
            profile["peerType"] = "bot"
        info = self._chat_info(item.chat_id)
        if item.chat_kind == "dm":
            info["contact"]["localDisplayName"] = item.user_name or item.user_id
            info["contact"]["profile"] = profile
            direction: dict[str, Any] = {"type": "directRcv"}
        else:
            member = {
                "memberId": item.user_id,
                "localDisplayName": item.user_name or item.user_id,
                "memberProfile": profile,
            }
            direction = {"type": "groupRcv", "groupMember": member}
        content: dict[str, Any] = {"type": "text", "text": item.text}
        chat_item: dict[str, Any] = {
            "chatDir": direction,
            "meta": {
                "itemId": int(item.message_id),
                "itemTs": item.at.isoformat(),
                "userMention": item.mentions_bot,
            },
            "content": {"type": "rcvMsgContent", "msgContent": content},
        }
        if item.media:
            media = item.media[0]
            file_id = str(next(self._file_ids))
            self.file_names[file_id] = media.ref
            content["type"] = _KIND[media.kind]
            if media.kind == "voice":
                content["duration"] = int(media.duration or 0)
            chat_item["file"] = {"fileId": int(file_id), "fileName": media.ref, "fileSize": 10}
        if item.reply_to:
            chat_item["quotedItem"] = {
                "itemId": int(item.reply_to),
                "chatDir": {"type": "directSnd"},
            }
        return {"chatInfo": info, "chatItem": chat_item}

    def push_inbound(self) -> int:
        """Render and send every inbound item waiting on the platform; how many were sent."""
        count = 0
        for item in self.platform.drain_inbound():
            row = self.row(item)
            if row is None:
                continue
            if item.event == "edited":
                self.push({"resp": {"type": "chatItemUpdated", "chatItem": row}})
            else:
                self.push({"resp": {"type": "newChatItems", "chatItems": [row]}})
            count += 1
        return count


__all__ = ["BOT_NAME", "MAX_LEN", "URL", "FakeSocket", "SimplexWire"]
