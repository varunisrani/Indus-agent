"""A fake Photon sidecar (our P16-25 sidecar's HTTP API), served through ``httpx.MockTransport``.

Routes as the sidecar serves them: ``GET /health``, ``GET /events`` (NDJSON stream with a cursor and
the run's epoch), ``GET /media``, ``POST /send``, ``/send-link``, ``/send-file``, ``/typing``,
``/react``, ``/unreact``, ``/read``; every route needs ``Authorization: Bearer <token>`` (401
otherwise). Backed by one `FakeChatPlatform`: inbound traffic injected with the platform's builders
becomes sidecar events (a DM's space is ``any;-;<user>``, the platform's chat id is the user;
iMessage has no threads, no bot flag and no edits on this API, so those builders produce nothing),
sends land as platform messages, and armed failures come back the way the sidecar reports them
(status + ``{"error": code}``, transport errors).

One ``/events`` call streams the events after the cursor, waits ``hold`` seconds on the fake clock
for more (a heartbeat line first), streams those and ends; the adapter reopens it. ``FakeLauncher``
stands in for ``sidecar.PhotonSidecar`` with this one sidecar; ``FakeSpawner`` is a process spawner
for the real owner whose children "run" this sidecar.
"""

from __future__ import annotations

import itertools
import json
import os
import secrets
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from indus.gateway.adapters.photon.sidecar import SidecarEndpoint
from tests.fakes.platform.core import (
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakePlatformError,
)
from tests.fakes.platform.whatsapp_wire import FakeChild

MAX_LEN = 8000
DM_PREFIX = "any;-;"
ROUTES: tuple[str, ...] = (
    "/health",
    "/events",
    "/media",
    "/send",
    "/send-link",
    "/send-file",
    "/typing",
    "/react",
    "/unreact",
    "/read",
)
SAMPLE_BYTES: Mapping[str, bytes] = {
    "voice": b"caff\x00\x01" + b"\x00" * 26,
    "photo": b"\xff\xd8\xff\xe0" + b"\x00" * 28,
    "document": b"plain text notes\n",
}
_KIND_OUT = {
    "voice": "voice",
    "audio": "audio",
    "photo": "image",
    "video": "video",
    "document": "file",
    "sticker": "image",
}


def fake_token() -> str:
    """A sidecar token built at run time (never a literal)."""
    return "tok-" + secrets.token_hex(20)


def space_of(chat_id: str) -> str:
    """The sidecar's space id for a platform chat (DMs are ``any;-;<user>``)."""
    return chat_id if ";" in chat_id else f"{DM_PREFIX}{chat_id}"


def chat_of(space: str) -> str:
    return space.removeprefix(DM_PREFIX)


@dataclass
class Call:
    method: str
    path: str
    body: dict[str, Any]
    at: float


@dataclass
class FakeSidecar:
    platform: FakeChatPlatform = field(
        default_factory=lambda: FakeChatPlatform("photon", max_len=MAX_LEN)
    )
    token: str = field(default_factory=fake_token)
    profile: str = "default"
    status: str = "open"
    # False: every request fails with a connect error (the process is gone).
    alive: bool = True
    # Seconds one /events call waits on the fake clock for new events before it ends.
    hold: float = 1.0
    calls: list[Call] = field(default_factory=list[Call])
    # True: /health hangs until the client gives up (a wedged process).
    hang_health: bool = False
    # Codes for the next link sends (``{"error": code}`` with its status), one each.
    refusals: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])

    def __post_init__(self) -> None:
        self.transport = httpx.MockTransport(self.handle)
        self.epoch = secrets.token_hex(4)
        self._seq = itertools.count(1)
        self._ring: list[dict[str, Any]] = []
        self._raw: deque[dict[str, Any]] = deque()
        self.media: dict[str, bytes] = {}
        self.streams = 0

    # -- test controls -------------------------------------------------------------------------

    def restart(self, *, token: str | None = None, profile: str | None = None) -> None:
        """A new sidecar run: new epoch, empty ring, alive."""
        self.alive = True
        self.epoch = secrets.token_hex(4)
        self._ring.clear()
        self._seq = itertools.count(1)
        if token is not None:
            self.token = token
        if profile is not None:
            self.profile = profile

    def push_event(self, **event: Any) -> dict[str, Any]:
        """Queue one raw sidecar event (defaults filled in) for the stream."""
        base: dict[str, Any] = {
            "id": f"RAW{len(self._raw) + len(self._ring) + 1}",
            "kind": "message",
            "space": {"id": f"{DM_PREFIX}+15550000200", "type": "dm", "name": ""},
            "sender": {"id": "+15550000200", "name": "Ana"},
            "at": 1_767_258_000_000,
            "text": "hello",
            "media": [],
            "link": None,
            "reply": None,
            "reaction": None,
        }
        base.update(event)
        self._raw.append(base)
        return base

    def media_item(self, data: bytes, **item: Any) -> dict[str, Any]:
        """A media entry for `push_event` whose bytes `/media` serves."""
        ref = secrets.token_hex(12)
        self.media[ref] = data
        base: dict[str, Any] = {
            "ref": ref,
            "kind": "file",
            "mime": "application/octet-stream",
            "name": "",
            "size": len(data),
            "preview": False,
        }
        base.update(item)
        return base

    def replay(self) -> None:
        """Deliver every kept event again (an upstream redelivery)."""
        for event in list(self._ring):
            copy = dict(event)
            copy.pop("seq", None)
            self._raw.append(copy)

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
                return self._events(request)
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
        if self.hang_health:
            raise httpx.ReadTimeout("health check timed out", request=request)
        try:
            await self.platform.connect()
        except FakePlatformError as exc:
            raise httpx.ConnectTimeout(exc.text, request=request) from None
        return httpx.Response(
            200,
            json={
                "status": self.status,
                "profile": self.profile,
                "epoch": self.epoch,
                "version": "1",
                "kept": len(self._ring),
                "link": {"state": self.status, "since": 0, "detail": ""},
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

    def _events(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        after = int(params.get("after", "0") or 0)
        if params.get("epoch") != self.epoch:
            after = 0
        self.streams += 1
        epoch = self.epoch

        async def lines() -> AsyncIterator[bytes]:
            sent = after
            self._collect()
            for event in [e for e in self._ring if e["seq"] > sent]:
                sent = event["seq"]
                yield (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            yield b"\n"
            await self.platform.clock.sleep(self.hold)
            if not self.alive or epoch != self.epoch:
                raise httpx.ReadError("connection closed", request=request)
            self._collect()
            for event in [e for e in self._ring if e["seq"] > sent]:
                sent = event["seq"]
                yield (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")

        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson", "x-photon-epoch": epoch},
            content=lines(),
        )

    # -- inbound shapes ----------------------------------------------------------------------------

    def event_for(self, item: FakeInbound) -> dict[str, Any] | None:
        if item.is_bot or item.thread_id or item.event != "message":
            return (
                None  # iMessage has no bot flag, no threads; edits and buttons are not on the API
            )
        group = item.chat_kind != "dm"
        space = item.chat_id if group else space_of(item.user_id)
        reply = None
        if item.reply_to:
            original = self.platform.message(item.reply_to)
            reply = {
                "id": item.reply_to,
                "text": original.text if original is not None else "",
                "fromUs": original is not None and original.from_bot,
            }
        media = []
        for m in item.media:
            data = SAMPLE_BYTES.get(m.kind, b"data")
            media.append(
                self.media_item(
                    data,
                    kind=_KIND_OUT[m.kind],
                    mime=m.mime,
                    name=m.ref,
                )
            )
        return {
            "id": item.message_id,
            "kind": "message",
            "space": {"id": space, "type": "group" if group else "dm", "name": item.chat_name},
            "sender": {"id": item.user_id, "name": item.user_name},
            "at": int(item.at.timestamp() * 1000),
            "text": item.text,
            "media": media,
            "link": None,
            "reply": reply,
            "reaction": None,
        }

    # -- outbound ----------------------------------------------------------------------------------

    async def _post(
        self, path: str, body: Mapping[str, Any], request: httpx.Request
    ) -> httpx.Response:
        if self.status not in ("open", "degraded"):
            return httpx.Response(503, json={"error": "not_connected"})
        space = str(body.get("space") or "")
        if not space:
            return httpx.Response(400, json={"error": "bad_request"})
        chat = chat_of(space)
        if self.refusals and path.startswith("/send"):
            status, code = self.refusals.pop(0)
            return httpx.Response(status, json={"error": code})
        if path == "/send":
            sent = await self.platform.post(
                chat, str(body.get("text") or ""), extra={"sidecar_body": dict(body)}
            )
            return httpx.Response(200, json={"id": sent.message_id})
        if path == "/send-link":
            sent = await self.platform.post(
                chat, str(body.get("url") or ""), extra={"sidecar_body": dict(body)}
            )
            return httpx.Response(200, json={"id": sent.message_id})
        if path == "/send-file":
            kind = "voice" if body.get("voice") is True else "document"
            media = FakeMedia(kind, os.path.basename(str(body.get("path") or "")), "")
            sent = await self.platform.upload(chat, media)
            return httpx.Response(200, json={"id": sent.message_id})
        if path == "/typing":
            if body.get("on") is False:
                await self.platform.typing_stop(chat)
            else:
                await self.platform.typing(chat)
            return httpx.Response(200, json={"ok": True})
        if path == "/react":
            await self.platform.react(chat, str(body.get("id")), str(body.get("emoji") or ""))
            return httpx.Response(200, json={"ok": True})
        if path == "/unreact":
            await self.platform.react(chat, str(body.get("id")), "")
            return httpx.Response(200, json={"ok": True})
        if path == "/read":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(405, json={"error": "method_not_allowed"})

    def _failure(self, exc: FakePlatformError, request: httpx.Request) -> httpx.Response:
        """An armed failure the way the sidecar shows it."""
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
            FailureKind.CHAT_NOT_FOUND: (404, "space_not_found"),
            FailureKind.THREAD_NOT_FOUND: (404, "message_not_found"),
            FailureKind.FORBIDDEN: (403, "forbidden"),
            FailureKind.PARSE_ERROR: (400, "bad_request"),
            FailureKind.TOO_LONG: (413, "too_large"),
        }
        status, code = codes.get(kind, (502, "failed"))
        return httpx.Response(status, json={"error": code})


@dataclass
class FakeLauncher:
    """``sidecar.PhotonSidecar`` for one already-running fake sidecar."""

    sidecar: FakeSidecar
    port: int = 8789
    ensures: int = 0
    attaches: int = 0
    stops: int = 0
    restarts: int = 0

    def endpoint(self) -> SidecarEndpoint:
        return SidecarEndpoint(f"http://127.0.0.1:{self.port}", self.port, self.sidecar.token)

    async def ensure(self) -> SidecarEndpoint:
        self.ensures += 1
        return self.endpoint()

    async def attach(self) -> SidecarEndpoint:
        self.attaches += 1
        return self.endpoint()

    def exited(self) -> bool:
        return not self.sidecar.alive

    async def restart(self) -> SidecarEndpoint:
        self.restarts += 1
        self.sidecar.restart()
        return self.endpoint()

    async def stop(self) -> None:
        self.stops += 1
        await self.sidecar.platform.disconnect()


@dataclass
class FakeSpawner:
    """Spawns `FakeChild` processes that run ``sidecar`` (it takes the child's token and
    profile); ``crash_at_start`` makes the next starts exit at once with ``crash_code`` after
    writing ``crash_line`` to the log."""

    sidecar: FakeSidecar
    children: list[FakeChild] = field(default_factory=list[FakeChild])
    argvs: list[list[str]] = field(default_factory=list[list[str]])
    crash_at_start: int = 0
    crash_code: int = 1
    crash_line: str = ""
    next_pid: int = 5_000_001

    async def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], log_path: Path
    ) -> FakeChild:
        child = FakeChild(self.next_pid, dict(env))
        self.next_pid += 1
        self.children.append(child)
        self.argvs.append(list(argv))
        if self.crash_at_start > 0:
            self.crash_at_start -= 1
            child.returncode = self.crash_code
            self.sidecar.alive = False
            if self.crash_line:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(self.crash_line + "\n")
        else:
            self.sidecar.restart(
                token=env["INDUS_PHOTON_TOKEN"], profile=env["INDUS_PHOTON_PROFILE"]
            )
            child.on_exit = self._down
        return child

    def _down(self) -> None:
        self.sidecar.alive = False

    def crash(self) -> None:
        """The running child dies (and the sidecar with it)."""
        if self.children:
            self.children[-1].returncode = 1
        self.sidecar.alive = False

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
    "DM_PREFIX",
    "MAX_LEN",
    "ROUTES",
    "Call",
    "FakeLauncher",
    "FakeSidecar",
    "FakeSpawner",
    "chat_of",
    "fake_token",
    "space_of",
]
