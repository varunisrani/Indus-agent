"""A fake WhatsApp bridge (our P16-02 sidecar's HTTP API), served through ``httpx.MockTransport``.

Routes as the bridge serves them: ``GET /health``, ``GET /events`` (cursor long-poll),
``GET /media``, ``POST /send``, ``/send-media``, ``/typing``, ``/react``, ``/read``; every route
needs ``Authorization: Bearer <token>`` (401 otherwise). Backed by one `FakeChatPlatform`: inbound
traffic injected with the platform's builders becomes bridge events (direct chats are
``<digits>@s.whatsapp.net``, the platform's chat id is the digits; WhatsApp has no threads and no
bot flag, so those builders produce nothing), sends land as platform messages, and armed failures
come back the way the bridge reports them (status + ``{"error": code}``, transport errors).

``FakeLauncher`` stands in for ``sidecar.SidecarOwner`` with this one bridge; ``FakeSpawner`` is a
process spawner for the real owner whose children "run" this bridge (start, crash, refuse).
"""

from __future__ import annotations

import itertools
import json
import os
import secrets
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from indus.gateway.adapters.whatsapp.sidecar import BridgeEndpoint
from tests.fakes.platform.core import (
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakePlatformError,
)

MAX_LEN = 4096
BOT_JID = "15550000999@s.whatsapp.net"
BOT_LID = "88800000999@lid"
USER_SERVER = "@s.whatsapp.net"
ROUTES: tuple[str, ...] = (
    "/health",
    "/events",
    "/media",
    "/send",
    "/send-media",
    "/typing",
    "/react",
    "/read",
)

SAMPLE_BYTES: Mapping[str, bytes] = {
    "voice": b"OggS\x00\x02" + b"\x00" * 26,
    "photo": b"\xff\xd8\xff\xe0" + b"\x00" * 28,
    "document": b"plain text notes\n",
}
_KIND_OUT = {
    "voice": "voice",
    "audio": "audio",
    "photo": "image",
    "video": "video",
    "document": "document",
    "sticker": "sticker",
}
_KIND_IN = {"image": "photo", "video": "video", "audio": "audio", "voice": "voice"}


def fake_token() -> str:
    """A bridge token built at run time (never a literal)."""
    return "tok-" + secrets.token_hex(20)


def jid_of(chat_id: str) -> str:
    return chat_id if "@" in chat_id else f"{chat_id}{USER_SERVER}"


def chat_of(jid: str) -> str:
    return jid.removesuffix(USER_SERVER)


@dataclass
class Call:
    method: str
    path: str
    body: dict[str, Any]
    at: float


@dataclass
class FakeBridge:
    platform: FakeChatPlatform = field(
        default_factory=lambda: FakeChatPlatform("whatsapp", max_len=MAX_LEN, bot_user_id=BOT_JID)
    )
    token: str = field(default_factory=fake_token)
    profile: str = "default"
    status: str = "open"
    self_jid: str = BOT_JID
    self_lid: str = BOT_LID
    # False: every request fails with a connect error (the process is gone).
    alive: bool = True
    calls: list[Call] = field(default_factory=list[Call])

    def __post_init__(self) -> None:
        self.transport = httpx.MockTransport(self.handle)
        self.epoch = secrets.token_hex(4)
        self._seq = itertools.count(1)
        self._ring: list[dict[str, Any]] = []
        self._raw: deque[dict[str, Any]] = deque()
        self.media: dict[str, bytes] = {}

    # -- test controls -------------------------------------------------------------------------

    def restart(self, *, token: str | None = None, profile: str | None = None) -> None:
        """A new bridge run: new epoch, empty ring, alive."""
        self.alive = True
        self.epoch = secrets.token_hex(4)
        self._ring.clear()
        self._seq = itertools.count(1)
        if token is not None:
            self.token = token
        if profile is not None:
            self.profile = profile

    def push_event(self, **event: Any) -> None:
        """Queue one raw bridge event (defaults filled in) for the next poll."""
        base: dict[str, Any] = {
            "id": f"RAW{len(self._raw) + 1}",
            "chat": "15550000200@s.whatsapp.net",
            "sender": "15550000200@s.whatsapp.net",
            "senderAlt": None,
            "senderName": "Ana",
            "group": False,
            "chatName": "",
            "text": "hello",
            "timestamp": 1_767_258_000,
            "fromMe": False,
            "mentions": [],
            "mentionsMe": False,
            "quoted": None,
            "media": [],
            "edited": False,
        }
        base.update(event)
        self._raw.append(base)

    def paths(self, path: str) -> list[Call]:
        return [c for c in self.calls if c.path == path]

    # -- HTTP --------------------------------------------------------------------------------------

    def _now(self) -> float:
        return self.platform.clock.monotonic()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: dict[str, Any] = {}
        if request.content:
            body = json.loads(request.content)
        self.calls.append(Call(request.method, path, body, self._now()))
        if not self.alive:
            raise httpx.ConnectError("connection refused", request=request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if path not in ROUTES:
            return httpx.Response(404, json={"error": "not_found"})
        try:
            if path == "/health" and request.method == "GET":
                return await self._health(request)
            if path == "/events" and request.method == "GET":
                return await self._events(request)
            if path == "/media" and request.method == "GET":
                data = self.media.get(request.url.params.get("ref", ""))
                if data is None:
                    return httpx.Response(404, json={"error": "message_not_found"})
                return httpx.Response(200, content=data)
            if request.method == "POST":
                return await self._post(path, body, request)
        except FakePlatformError as exc:
            return self._failure(exc, request)
        return httpx.Response(405, json={"error": "method_not_allowed"})

    async def _health(self, request: httpx.Request) -> httpx.Response:
        try:
            await self.platform.connect()
        except FakePlatformError as exc:
            raise httpx.ConnectTimeout(exc.text, request=request) from None
        own = {"jid": self.self_jid, "lid": self.self_lid} if self.status == "open" else None
        return httpx.Response(
            200,
            json={
                "status": self.status,
                "profile": self.profile,
                "epoch": self.epoch,
                "self": own,
                "queued": len(self._ring),
                "version": "1",
            },
        )

    def _collect(self) -> None:
        while self._raw:
            event = self._raw.popleft()
            event["seq"] = next(self._seq)
            self._ring.append(event)
        for item in self.platform.drain_inbound():
            event = self.event_for(item)
            if event is not None:
                event["seq"] = next(self._seq)
                self._ring.append(event)

    async def _events(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        after = int(params.get("after", "0") or 0)
        if params.get("epoch") != self.epoch:
            after = 0
        wait = min(float(params.get("wait", "0") or 0), 1.0)
        self._collect()
        found = [e for e in self._ring if e["seq"] > after]
        if not found and wait > 0:
            await self.platform.clock.sleep(wait)
            if not self.alive:
                raise httpx.ReadError("connection closed", request=request)
            self._collect()
            found = [e for e in self._ring if e["seq"] > after]
        cursor = found[-1]["seq"] if found else after
        return httpx.Response(200, json={"epoch": self.epoch, "cursor": cursor, "events": found})

    # -- inbound shapes ----------------------------------------------------------------------------

    def event_for(self, item: FakeInbound) -> dict[str, Any] | None:
        if item.is_bot or item.thread_id or item.event == "button":
            return None  # WhatsApp has no bot flag, no threads and no buttons
        chat = jid_of(item.chat_id)
        group = item.chat_kind != "dm"
        if group and "@" not in item.chat_id:
            chat = f"{item.chat_id}@g.us"
        quoted = None
        if item.reply_to:
            original = self.platform.message(item.reply_to)
            mine = original is not None and original.from_bot
            quoted = {
                "id": item.reply_to,
                "sender": self.self_jid if mine else jid_of(item.user_id),
                "text": original.text if original is not None else "",
                "fromMe": mine,
            }
        media = []
        for m in item.media:
            ref = secrets.token_hex(12)
            self.media[ref] = SAMPLE_BYTES.get(m.kind, b"data")
            media.append(
                {
                    "ref": ref,
                    "kind": _KIND_OUT[m.kind],
                    "mime": m.mime,
                    "name": m.ref if m.kind == "document" else "",
                    "size": len(self.media[ref]),
                }
            )
        return {
            "id": item.message_id,
            "chat": chat,
            "sender": jid_of(item.user_id),
            "senderAlt": None,
            "senderName": item.user_name,
            "group": group,
            "chatName": item.chat_name if group else "",
            "text": item.text,
            "timestamp": int(item.at.timestamp()),
            "fromMe": False,
            "mentions": [self.self_jid] if item.mentions_bot else [],
            "mentionsMe": item.mentions_bot,
            "quoted": quoted,
            "media": media,
            "edited": item.event == "edited",
        }

    # -- outbound ----------------------------------------------------------------------------------

    async def _post(
        self, path: str, body: Mapping[str, Any], request: httpx.Request
    ) -> httpx.Response:
        if self.status != "open":
            return httpx.Response(503, json={"error": "not_connected"})
        chat = chat_of(str(body.get("chat") or ""))
        if not chat or "@" not in str(body.get("chat") or ""):
            return httpx.Response(400, json={"error": "bad_request"})
        if path == "/send":
            sent = await self.platform.post(
                chat,
                str(body.get("text") or ""),
                reply_to=body.get("replyTo"),
                extra={"bridge_body": dict(body)},
            )
            return httpx.Response(200, json={"id": sent.message_id})
        if path == "/send-media":
            kind = _KIND_IN.get(str(body.get("kind")), "document")
            media = FakeMedia(
                kind,  # pyright: ignore[reportArgumentType]
                os.path.basename(str(body.get("path") or "")),
                str(body.get("mime") or ""),
                str(body.get("caption") or ""),
            )
            sent = await self.platform.upload(chat, media, reply_to=body.get("replyTo"))
            return httpx.Response(200, json={"id": sent.message_id})
        if path == "/typing":
            if body.get("state") == "paused":
                await self.platform.typing_stop(chat)
            else:
                await self.platform.typing(chat)
            return httpx.Response(200, json={"ok": True})
        if path == "/react":
            await self.platform.react(chat, str(body.get("id")), str(body.get("emoji") or ""))
            return httpx.Response(200, json={"ok": True})
        if path == "/read":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(405, json={"error": "method_not_allowed"})

    def _failure(self, exc: FakePlatformError, request: httpx.Request) -> httpx.Response:
        """An armed failure the way the bridge shows it."""
        kind = exc.kind
        if kind is FailureKind.CONNECT_TIMEOUT:
            raise httpx.ConnectTimeout(exc.text, request=request)
        if kind is FailureKind.READ_TIMEOUT:
            raise httpx.ReadTimeout(exc.text, request=request)
        if kind is FailureKind.RATE_LIMIT:
            wait = exc.retry_after if exc.retry_after is not None else 30
            return httpx.Response(
                429, headers={"retry-after": str(int(wait))}, json={"error": "rate_limited"}
            )
        codes = {
            FailureKind.CHAT_NOT_FOUND: (404, "chat_not_found"),
            FailureKind.THREAD_NOT_FOUND: (404, "message_not_found"),
            FailureKind.FORBIDDEN: (403, "forbidden"),
            FailureKind.PARSE_ERROR: (400, "bad_request"),
            FailureKind.TOO_LONG: (413, "too_large"),
        }
        status, code = codes.get(kind, (502, "failed"))
        return httpx.Response(status, json={"error": code})


@dataclass
class FakeLauncher:
    """``sidecar.SidecarOwner`` for one already-running fake bridge."""

    bridge: FakeBridge
    port: int = 3000
    ensures: int = 0
    attaches: int = 0
    stops: int = 0
    restarts: int = 0

    def endpoint(self) -> BridgeEndpoint:
        return BridgeEndpoint(f"http://127.0.0.1:{self.port}", self.port, self.bridge.token)

    async def ensure(self) -> BridgeEndpoint:
        self.ensures += 1
        return self.endpoint()

    async def attach(self) -> BridgeEndpoint:
        self.attaches += 1
        return self.endpoint()

    def exited(self) -> bool:
        return not self.bridge.alive

    async def restart(self) -> BridgeEndpoint:
        self.restarts += 1
        self.bridge.restart()
        return self.endpoint()

    async def stop(self) -> None:
        self.stops += 1
        await self.bridge.platform.disconnect()


@dataclass
class FakeChild:
    pid: int
    env: dict[str, str]
    returncode: int | None = None
    signals: list[int] = field(default_factory=list[int])
    # Called once when a signal ends the child (the bridge it ran goes with it).
    on_exit: Callable[[], None] | None = None

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if self.returncode is None:
            self.returncode = -sig
            if self.on_exit is not None:
                self.on_exit()


@dataclass
class FakeSpawner:
    """Spawns `FakeChild` processes that run ``bridge`` (it takes the child's token and
    profile); ``crash_at_start`` makes the next starts exit at once with ``crash_code``."""

    bridge: FakeBridge
    children: list[FakeChild] = field(default_factory=list[FakeChild])
    argvs: list[list[str]] = field(default_factory=list[list[str]])
    times: list[float] = field(default_factory=list[float])
    crash_at_start: int = 0
    crash_code: int = 1
    next_pid: int = 4_000_001

    async def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], log_path: Path
    ) -> FakeChild:
        child = FakeChild(self.next_pid, dict(env))
        self.next_pid += 1
        self.children.append(child)
        self.argvs.append(list(argv))
        self.times.append(self.bridge.platform.clock.monotonic())
        if self.crash_at_start > 0:
            self.crash_at_start -= 1
            child.returncode = self.crash_code
            self.bridge.alive = False
        else:
            self.bridge.restart(token=env["INDUS_WA_TOKEN"], profile=env["INDUS_WA_PROFILE"])
            child.on_exit = self._bridge_down
        return child

    def _bridge_down(self) -> None:
        self.bridge.alive = False

    def crash(self) -> None:
        """The running child dies (and the bridge with it)."""
        if self.children:
            self.children[-1].returncode = 1
        self.bridge.alive = False

    def kill(self, pid: int, sig: int) -> None:
        for child in self.children:
            if child.pid == pid and child.returncode is None:
                child.send_signal(sig)

    def alive(self, pid: int) -> bool:
        return any(c.pid == pid and c.returncode is None for c in self.children)

    def start_time(self, pid: int) -> int | None:
        for child in self.children:
            if child.pid == pid and child.returncode is None:
                return pid * 10
        return None


__all__ = [
    "BOT_JID",
    "BOT_LID",
    "MAX_LEN",
    "ROUTES",
    "Call",
    "FakeBridge",
    "FakeChild",
    "FakeLauncher",
    "FakeSpawner",
    "chat_of",
    "fake_token",
    "jid_of",
]
