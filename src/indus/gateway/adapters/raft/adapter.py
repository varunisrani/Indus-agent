"""The Raft adapter (P16-30): a loopback wake endpoint for Raft's bridge.

Raft keeps the conversation itself: its command-line tool reads and sends messages, and Raft's
bridge process only tells an external agent that something may be waiting. This adapter is that
agent's door:

- an HTTP listener on 127.0.0.1 (port ``platforms.raft.port``, an ephemeral one by default) on the
  P16-01 kit; requests whose peer is not a loopback address are refused (403) before anything is
  read, bodies over 16 KiB are refused (413) before they are read;
- a port file, ``<profile home>/platforms/raft/bridge.json`` (0600, directory 0700), written once
  the listener is up and removed at stop: the wake URL, the port, the header name and the bridge
  token (``RAFT_CHANNEL_TOKEN`` from the secret store / environment, else a new one per start);
- ``POST /wake`` (token in ``x-raft-bridge-token``): a content-free JSON object — any string
  longer than 120 characters is refused (413), any field named body / content / message /
  messages / preview / snippet / text is refused (400). Wakes queue (500 at most: the oldest goes,
  with a log line) and become internal turns whose text is our fixed wake prompt; wakes that wait
  together are folded into one turn (they carry nothing but "look at Raft");
- ``POST /activity`` and ``GET /activity/drain?max=N``: the bridge's activity telemetry
  (``raft-activity.v1`` events: tool input/output cut to 4,096 characters; 500 queued at most,
  drops counted and reported by the next drain as ``raft-activity-drain.v1``).

The agent answers through the Raft CLI, so ``send`` only records the reply. Every chat reads as a
DM but is the bridge, never a person: ``dm_is_private`` is False, so pairing codes and login links
never go there (R06.24). A send-only adapter (``indus send``) opens no listener.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import itertools
import json
import logging
import os
import re
import secrets
from collections import deque
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

from indus.foundation.clock import Clock
from indus.foundation.fsio import atomic_write_json
from indus.gateway.adapters._http import (
    BadPayload,
    CallbackListener,
    CallbackRequest,
    ListenerSettings,
    Reply,
    constant_time_equal,
)
from indus.gateway.adapters.base import Adapter, AdapterConnectError, Capabilities, ChatInfo
from indus.gateway.core.events import InboundEvent, Source
from indus.gateway.core.send import SendResult
from indus.gateway.http.serving import Asgi

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "ACTIVITY_SCHEMA",
    "CONTENT_MAX",
    "DRAIN_SCHEMA",
    "FIELD_MAX",
    "MAX_BODY",
    "PROFILE_ENV",
    "QUEUE_CAP",
    "TOKEN_ENV",
    "TOKEN_HEADER",
    "ActivityError",
    "ActivityQueue",
    "RaftAdapter",
    "WakeQueue",
    "port_file",
    "validate_activity",
    "wake_prompt",
]

log = logging.getLogger(__name__)

PROFILE_ENV: Final = "RAFT_PROFILE"
TOKEN_ENV: Final = "RAFT_CHANNEL_TOKEN"
TOKEN_HEADER: Final = "x-raft-bridge-token"
WAKE_PATH: Final = "/wake"
ACTIVITY_PATH: Final = "/activity"
DRAIN_PATH: Final = "/activity/drain"
MAX_BODY: Final = 16 * 1024
FIELD_MAX: Final = 120
CONTENT_MAX: Final = 4096
QUEUE_CAP: Final = 500
DRAIN_DEFAULT: Final = 200
MAX_DEPTH: Final = 5
REPLIES_KEPT: Final = 50
ACTIVITY_SCHEMA: Final = "raft-activity.v1"
DRAIN_SCHEMA: Final = "raft-activity-drain.v1"
_CONTENT_KEYS: Final = frozenset(
    {"body", "content", "message", "messages", "preview", "snippet", "text"}
)
_WAKE_IDS: Final = ("eventId", "attemptId", "messageId", "delivery_id", "wake_id", "id")
_SAFE: Final = re.compile(r"^[A-Za-z0-9._:@/ -]{1,120}$")
_REQUIRED: Final = ("eventId", "sessionId", "hookEventName", "occurredAt")
_FLAGS: Final = ("truncated", "toolInputTruncated", "toolOutputTruncated")
_TEXTS: Final = ("toolInput", "toolOutput")
_ACTIVITY_FIELDS: Final = frozenset(
    {"schema", "status", "durationMs", "toolName", "errorClass", *_REQUIRED, *_FLAGS, *_TEXTS}
)


def port_file(home: Path) -> Path:
    return home / "platforms" / "raft" / "bridge.json"


def wake_prompt(profile: str = "") -> str:
    """The fixed text of a wake turn (ours)."""
    how = f", passing --profile {profile} on every call" if _SAFE.match(profile or "") else ""
    return (
        "Raft says there may be new messages for you in its workspace. Read and answer them with "
        f"the raft command-line tool{how}. If you have not read its manual in this session, "
        "start with `raft manual get raft-cli-overview`. Nobody reads this chat: answer through "
        "Raft, not here."
    )


# -- queues ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Wake:
    wake_id: str
    payload: Mapping[str, Any]


class WakeQueue:
    """Wakes waiting for the gateway, oldest first; past the cap the oldest is dropped."""

    def __init__(self, cap: int = QUEUE_CAP) -> None:
        self.cap = cap
        self._items: deque[Wake] = deque()
        self.dropped = 0

    def push(self, wake: Wake) -> Wake | None:
        """Queue ``wake``; the wake dropped to make room (None when there was room)."""
        self._items.append(wake)
        if len(self._items) <= self.cap:
            return None
        self.dropped += 1
        return self._items.popleft()

    def take_all(self) -> list[Wake]:
        items = list(self._items)
        self._items.clear()
        return items

    def __len__(self) -> int:
        return len(self._items)


class ActivityError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _safe(value: object) -> bool:
    return isinstance(value, str) and bool(_SAFE.match(value))


def validate_activity(value: object) -> dict[str, Any]:
    """One activity event, checked and with tool input/output cut to `CONTENT_MAX`;
    `ActivityError` (413 for an overlong scalar, else 400) when it does not fit."""
    if not isinstance(value, Mapping):
        raise ActivityError("an activity event must be an object")
    event = dict(cast(Mapping[str, Any], value))
    if event.get("schema") != ACTIVITY_SCHEMA:
        raise ActivityError("unsupported activity schema")
    unknown = sorted(set(event) - _ACTIVITY_FIELDS)
    if unknown:
        raise ActivityError(f"field {unknown[0]!r} is not allowed")
    for key in (*_REQUIRED, "toolName", "errorClass"):
        found = event.get(key)
        if found is None and key not in _REQUIRED:
            continue
        if isinstance(found, str) and len(found) > FIELD_MAX:
            raise ActivityError(f"{key} is longer than {FIELD_MAX} characters", 413)
        if not _safe(found):
            raise ActivityError(f"{key} must be a short plain string")
    if event.get("status") not in ("ok", "error"):
        raise ActivityError("status must be ok or error")
    duration = event.get("durationMs")
    if duration is not None:
        if isinstance(duration, bool) or not isinstance(duration, int | float) or duration < 0:
            raise ActivityError("durationMs must be a non-negative number")
        event["durationMs"] = int(duration)
    for key in _FLAGS:
        if event.get(key) is not None and not isinstance(event.get(key), bool):
            raise ActivityError(f"{key} must be true or false")
    for key in _TEXTS:
        text = event.get(key)
        if text is None:
            continue
        if not isinstance(text, str):
            raise ActivityError(f"{key} must be a string")
        if len(text) > CONTENT_MAX:
            event[key] = text[:CONTENT_MAX]
            event[f"{key}Truncated"] = True
            event["truncated"] = True
    return event


class ActivityQueue:
    """Validated activity events for the bridge to drain; past the cap the oldest is dropped
    and counted."""

    def __init__(self, cap: int = QUEUE_CAP) -> None:
        self.cap = cap
        self._events: deque[dict[str, Any]] = deque()
        self._dropped = 0

    def push(self, event: object) -> None:
        self._events.append(validate_activity(event))
        while len(self._events) > self.cap:
            self._events.popleft()
            self._dropped += 1

    def drain(self, limit: int = DRAIN_DEFAULT) -> dict[str, Any]:
        count = min(max(limit, 1), len(self._events))
        events = [self._events.popleft() for _ in range(count)]
        dropped, self._dropped = self._dropped, 0
        return {"schema": DRAIN_SCHEMA, "events": events, "dropped": dropped}

    def __len__(self) -> int:
        return len(self._events)


# -- the payload rules -----------------------------------------------------------------------------


def _problem(value: object, depth: int = 0) -> tuple[int, str] | None:
    """(status, error) for a wake payload that breaks the rules, else None."""
    if depth > MAX_DEPTH:
        return 400, "invalid_payload"
    if isinstance(value, str):
        return (413, "field_too_large") if len(value) > FIELD_MAX else None
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value)
        for key, inner in items.items():
            name = str(key)
            if len(name) > FIELD_MAX:
                return 413, "field_too_large"
            found = _problem(inner, depth + 1)
            if found is not None:
                return found
        for key in items:
            if str(key).strip().lower() in _CONTENT_KEYS:
                return 400, "content_not_allowed"
        return None
    if isinstance(value, list):
        for inner in cast(list[object], value):
            found = _problem(inner, depth + 1)
            if found is not None:
                return found
    return None


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped is not None and mapped.is_loopback))


class LoopbackOnly:
    """An ASGI layer that answers 403 to any peer that is not this machine, unread."""

    def __init__(self, app: Asgi) -> None:
        self.app = app

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[..., Any],
        send: Callable[..., Any],
    ) -> None:
        if scope.get("type") == "http":
            client = scope.get("client")
            host = str(client[0]) if client else None
            if not _loopback(host):
                body = b'{"error":"loopback only"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# -- the adapter -----------------------------------------------------------------------------------


class RaftAdapter(Adapter):
    platform: ClassVar[str] = "raft"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=100_000,
        markup="plain",
        splits_natively=True,
        supports_edit=False,
        supports_delete=False,
        supports_buttons=False,
        dm_is_private=False,
        push_capable=False,
    )

    def __init__(
        self,
        *,
        clock: Clock,
        home: Path | None,
        profile: str = "",
        token: str | None = None,
        port: int = 0,
        runtime_session: str = "default",
        settings: PlatformSettings | None = None,
        send_only: bool = False,
        serve: bool = True,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(token or "",))
        self.home = home
        self.profile = profile.strip()
        self._configured_token = (token or "").strip()
        self._token = ""
        self.port = port
        self.runtime_session = runtime_session if _safe(runtime_session) else "default"
        self.send_only = send_only
        self.serve = serve
        self.listener: CallbackListener | None = None
        self.wakes = WakeQueue()
        self.activity = ActivityQueue()
        self.replies: deque[tuple[str, str]] = deque(maxlen=REPLIES_KEPT)
        self._kick = asyncio.Event()
        self._pump_task: asyncio.Task[None] | None = None
        self._ids = itertools.count(1)
        self._written: Path | None = None

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<RaftAdapter {self.runtime_session} {state}>"

    @property
    def token(self) -> str:
        return self._token

    # -- lifecycle ---------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        if self.send_only:
            return
        self._token = self._configured_token or secrets.token_urlsafe(32)
        if self._token not in self._secrets:
            self._secrets = (*self._secrets, self._token)
        settings = ListenerSettings(port=self.port, max_body_bytes=MAX_BODY, path=WAKE_PATH)
        listener = CallbackListener(
            settings, name="raft", clock=self.clock, scrub=self.scrub, serve=self.serve
        )
        listener.route(WAKE_PATH, self._wake)
        listener.route(ACTIVITY_PATH, self._activity)
        listener.route(DRAIN_PATH, self._drain, methods=("GET",))
        listener.app = LoopbackOnly(listener.app)
        await listener.start()
        self.listener = listener
        try:
            self._write_port_file()
        except OSError as exc:
            await listener.stop()
            self.listener = None
            raise AdapterConnectError(
                f"cannot write the Raft port file: {exc.strerror or type(exc).__name__}",
                retryable=False,
                code="raft_port_file",
            ) from None
        self._pump_task = asyncio.ensure_future(self._pump())
        if not self.profile:
            log.warning(
                "raft: %s is not set; the agent cannot tell the raft CLI which profile to use",
                PROFILE_ENV,
            )
        log.info("raft: wake endpoint ready at %s", self.wake_url)

    async def _close(self) -> None:
        task, self._pump_task = self._pump_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        listener, self.listener = self.listener, None
        if listener is not None:
            await listener.stop()
        self._remove_port_file()

    @property
    def bound_port(self) -> int:
        listener = self.listener
        address = listener.address if listener is not None else None
        return address[1] if address is not None else self.port

    @property
    def wake_url(self) -> str:
        return f"http://127.0.0.1:{self.bound_port}{WAKE_PATH}"

    def _write_port_file(self) -> None:
        if self.home is None:
            return
        target = port_file(self.home)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        record = {
            "url": self.wake_url,
            "activity_url": f"http://127.0.0.1:{self.bound_port}{ACTIVITY_PATH}",
            "port": self.bound_port,
            "header": TOKEN_HEADER,
            "token": self._token,
            "runtime_session": self.runtime_session,
            "pid": os.getpid(),
        }
        atomic_write_json(target, record, mode=0o600)
        self._written = target

    def _remove_port_file(self) -> None:
        target, self._written = self._written, None
        if target is None:
            return
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        # Only our own file: another gateway may have written its own since.
        if isinstance(current, dict) and current.get("token") == self._token:
            with contextlib.suppress(OSError):
                target.unlink()

    # -- requests ----------------------------------------------------------------------------------

    def _authorized(self, request: CallbackRequest) -> bool:
        offered = request.header(TOKEN_HEADER) or ""
        return bool(self._token and offered) and constant_time_equal(
            offered.encode("utf-8", "replace"), self._token.encode("utf-8")
        )

    async def _wake(self, request: CallbackRequest) -> Reply:
        if not self._authorized(request):
            return Reply.json({"ok": False, "error": "unauthorized"}, 401)
        try:
            payload = request.json() if request.body.strip() else {}
        except BadPayload:
            return Reply.json({"ok": False, "error": "invalid_json"}, 400)
        if not isinstance(payload, dict):
            return Reply.json({"ok": False, "error": "invalid_payload"}, 400)
        problem = _problem(payload)
        if problem is not None:
            status, error = problem
            return Reply.json({"ok": False, "error": error}, status)
        if self._sink is None:
            log.warning("raft: a wake arrived before the gateway was ready")
            return Reply.json(
                {"ok": False, "error": "not_ready", "runtimeSession": self.runtime_session}, 503
            )
        fields = cast(dict[str, Any], payload)
        wake_id = next(
            (str(fields[k]) for k in _WAKE_IDS if _safe(fields.get(k))),
            f"raft-wake-{next(self._ids)}",
        )
        dropped = self.wakes.push(Wake(wake_id, fields))
        if dropped is not None:
            log.warning(
                "raft: the wake queue is full (%d); dropped the oldest wake %s",
                QUEUE_CAP,
                dropped.wake_id,
            )
        self._kick.set()
        return Reply.json(
            {"ok": True, "queued": len(self.wakes), "runtimeSession": self.runtime_session}, 202
        )

    async def _activity(self, request: CallbackRequest) -> Reply:
        if not self._authorized(request):
            return Reply.json({"ok": False, "error": "unauthorized"}, 401)
        try:
            payload = request.json()
        except BadPayload:
            return Reply.json({"ok": False, "error": "invalid_json"}, 400)
        try:
            self.activity.push(payload)
        except ActivityError as exc:
            return Reply.json({"ok": False, "error": str(exc)}, exc.status)
        return Reply.json({"ok": True}, 202)

    async def _drain(self, request: CallbackRequest) -> Reply:
        if not self._authorized(request):
            return Reply.json({"ok": False, "error": "unauthorized"}, 401)
        raw = request.query_params().get("max", "")
        limit = int(raw) if raw.isdigit() else DRAIN_DEFAULT
        return Reply.json(self.activity.drain(min(limit, QUEUE_CAP)))

    def report_activity(self, event: Mapping[str, Any]) -> bool:
        """Queue one activity event from the agent's own hooks; False when it is refused."""
        try:
            self.activity.push(event)
        except ActivityError as exc:
            log.debug("raft: an activity event was dropped: %s", exc)
            return False
        return True

    # -- wakes to turns ----------------------------------------------------------------------------

    def _event(self, batch: list[Wake]) -> InboundEvent:
        source = Source(
            platform=self.platform,
            chat_id=self.runtime_session,
            chat_type="dm",
            chat_name="Raft channel",
            user_id="raft-bridge",
            user_name="Raft bridge",
        )
        return InboundEvent(
            source=source,
            text=wake_prompt(self.profile),
            message_id=batch[-1].wake_id,
            internal=True,
            allow_control=False,
            metadata={"raft_wakes": [w.wake_id for w in batch]},
        )

    async def _pump(self) -> None:
        while True:
            await self._kick.wait()
            self._kick.clear()
            while len(self.wakes):
                batch = self.wakes.take_all()
                await self.emit(self._event(batch))

    # -- outbound ----------------------------------------------------------------------------------

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        """Recorded only: the agent answers through the Raft CLI."""
        message_id = f"raft-reply-{next(self._ids)}"
        self.replies.append((chat_id, text))
        log.debug("raft: recorded a %d-character reply (sent through the Raft CLI)", len(text))
        return SendResult.sent(message_id)

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(name=f"Raft {chat_id}", type="dm")
