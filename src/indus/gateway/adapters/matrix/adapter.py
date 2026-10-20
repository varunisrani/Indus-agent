"""The Matrix adapter, plain (P16-06): any homeserver, an access token, our own HTTP client.

Connecting proves the token (``whoami``) and nothing more. The ``/sync`` long poll starts only once
an inbound sink is installed (the daemon does that right after connecting) and stops when it is
removed; a send-only adapter (``AdapterConfig.send_only``, ``indus send``) never starts it, so a
running gateway keeps every inbound event. A token the homeserver refuses while syncing is reported
through `Adapter.report_fatal` (``matrix_auth``).

Inbound rules (each message is one `InboundEvent`):

- dropped: our own events, edits (``m.replace``), bridge ghosts (``@_…``), senders matching
  ``ignore_user_patterns``, repeats (the last 1,000 event ids), and events sent more than 5 s
  before the listener started (the backlog an initial sync replays);
- rooms are groups; a room the ``m.direct`` account data lists, or one with two joined members, is a
  DM; ``allowed_rooms`` (when set) drops other rooms, DMs exempt;
- an ``m.thread`` relation makes a thread (``chat_type`` ``thread`` in rooms; a DM keeps ``dm`` with
  its thread id); ``auto_thread: true`` makes every top-level room message a thread root;
- a real reply (``m.in_reply_to`` that is not a thread fallback) fills the reply fields and the
  quoted fallback is cut from the text;
- the bot is mentioned by an intentional mention (``m.mentions``), its full user id, ``@localpart``,
  a matrix.to pill or a reply to one of its messages (the mention text is removed); a message in a
  thread the bot already takes part in counts as addressed to it (``thread_require_mention: true``
  turns that off);
- ``!command`` becomes ``/command`` for commands the registry knows (Matrix clients keep ``/`` for
  their own commands); ``m.notice`` (the bot convention) is marked ``is_bot``;
- media keep the ``mxc://`` URI as the attachment ref; `fetch_media` streams it for the intake.

Outbound: text is cut at 15,000 UTF-8 bytes (Markdown ``body`` plus an HTML ``formatted_body``;
event content stays under 45,000 bytes, well inside the 65,536-byte event limit, R06.11); edits are
``m.replace`` events, deletes are redactions, threads and replies go in ``m.relates_to``; every
message carries ``m.mentions`` so only the users the reply names are notified (never ``@room``
unless ``allow_room_mentions``).

End-to-end encryption (P16-07, ``platforms.matrix.e2ee: optional | required``) plugs in through
the `EndToEnd` seam: ``matrix.crypto.create_end_to_end`` builds it at connect. With it, encrypted
timeline events are decrypted before the rules above, events to encrypted rooms are encrypted, and
attachments in encrypted rooms travel as encrypted files (``file`` instead of ``url``).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import mimetypes
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, cast

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.gateway.adapters.base import (
    REPLY_TO_KEY,
    Adapter,
    AdapterConnectError,
    Capabilities,
    ChatInfo,
    FetcherSeat,
    MediaKind,
)
from indus.gateway.adapters.matrix.client import (
    MatrixClient,
    MatrixError,
    compact_json,
    parse_mxc,
)
from indus.gateway.adapters.matrix.html import mentioned_users, to_html
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult, is_ambiguous_timeout
from indus.gateway.outbound.chunker import split_message

if TYPE_CHECKING:
    import httpx

    from indus.foundation.scope import Scope
    from indus.gateway.config import PlatformSettings

__all__ = [
    "CHUNK_BYTES",
    "MAX_CONTENT_BYTES",
    "STARTUP_GRACE_S",
    "EndToEnd",
    "EndToEndFactory",
    "MatrixAdapter",
    "MatrixOptions",
    "utf8_bytes",
]

log = logging.getLogger(__name__)

# The server refuses an event over 65,536 bytes; content stays under this budget so ids, hashes,
# signatures (and E2EE's base64 growth) still fit.
MAX_CONTENT_BYTES = 45_000
# One piece of text: the body and its HTML both ride in the content, so a third of the budget.
CHUNK_BYTES = MAX_CONTENT_BYTES // 3
STARTUP_GRACE_S = 5.0
SYNC_TIMEOUT_MS = 30_000
SEEN_EVENTS = 1_000
DEFAULT_MAX_MEDIA_BYTES = 100 * 1024 * 1024
BACKOFF_FIRST_S = 2.0
BACKOFF_CAP_S = 60.0
TYPING_TIMEOUT_MS = 30_000
TOKEN_ENV = "MATRIX_ACCESS_TOKEN"
HOMESERVER_ENV = "MATRIX_HOMESERVER"
USER_ENV = "MATRIX_USER_ID"
DEVICE_ENV = "MATRIX_DEVICE_ID"

# Sync filter: no presence, members loaded lazily (only the senders we see), a bounded timeline.
SYNC_FILTER = json.dumps(
    {
        "presence": {"not_types": ["*"]},
        "room": {
            "state": {"lazy_load_members": True},
            "timeline": {"limit": 50},
            "ephemeral": {"not_types": ["*"]},
        },
    },
    separators=(",", ":"),
)

_MEDIA_TYPES: Mapping[str, MessageKind] = {
    "m.image": MessageKind.PHOTO,
    "m.video": MessageKind.VIDEO,
    "m.file": MessageKind.DOCUMENT,
    "m.audio": MessageKind.AUDIO,
}
_TEXT_TYPES = frozenset({"m.text", "m.notice", "m.emote"})
_OUT_TYPES: Mapping[str, str] = {
    "image": "m.image",
    "video": "m.video",
    "voice": "m.audio",
    "file": "m.file",
    "document": "m.file",
}
_VOICE_FLAG = "org.matrix.msc3245.voice"
_BANG = re.compile(r"^!([A-Za-z][A-Za-z0-9_-]*)(?=$|\s)(.*)$", re.DOTALL)
_FALLBACK_AUTHOR = re.compile(r"^>\s*<(@[^>\s]+)>\s?(.*)$")
_TRUTHY = frozenset({"1", "true", "yes", "on"})

type InviteMode = Literal["allowlisted", "all", "off"]
type E2eeMode = Literal["off", "optional", "required"]
# Which devices get room keys (P16-07): signed by their owner (default) or verified by us.
type E2eeTrust = Literal["owner_signed", "verified"]


def utf8_bytes(text: str) -> int:
    """Length in UTF-8 bytes: the unit of the homeserver's event cap."""
    return len(text.encode("utf-8"))


class EndToEnd(Protocol):
    """End-to-end encryption for one adapter (P16-07 implements it in ``matrix.crypto``)."""

    async def absorb_sync(self, sync: Mapping[str, Any]) -> None:
        """Take one ``/sync`` answer (to-device events, device lists, one-time key counts)."""
        ...

    async def decrypt(self, room_id: str, event: Mapping[str, Any]) -> dict[str, Any] | None:
        """The clear event for an ``m.room.encrypted`` one; None when it cannot be decrypted."""
        ...

    async def is_encrypted(self, room_id: str) -> bool:
        """Does the room have end-to-end encryption on (``m.room.encryption`` state)?"""
        ...

    async def encrypt(
        self, room_id: str, event_type: str, content: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """``(m.room.encrypted, ciphertext content)`` for an outbound event."""
        ...

    async def encrypt_attachment(self, data: bytes) -> tuple[bytes, dict[str, Any]]:
        """``(ciphertext, file info without its url)`` for an attachment to an encrypted room."""
        ...

    async def decrypt_attachment(self, data: bytes, info: Mapping[str, Any]) -> bytes:
        """The clear bytes of a downloaded encrypted attachment (``info`` is its ``file``)."""
        ...

    async def aclose(self) -> None: ...


type EndToEndFactory = Callable[[MatrixAdapter, MatrixClient], Awaitable[EndToEnd]]


def _truthy(value: object, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _ids(value: object) -> frozenset[str]:
    if isinstance(value, str):
        items: Iterable[object] = value.split(",")
    elif isinstance(value, list | tuple | set | frozenset):
        items = value  # pyright: ignore[reportUnknownVariableType]
    else:
        return frozenset()
    return frozenset(str(v).strip() for v in items if str(v).strip())  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


@dataclass(frozen=True)
class MatrixOptions:
    """``platforms.matrix.*`` settings the adapter reads (all optional)."""

    allowed_rooms: frozenset[str] = frozenset()
    auto_thread: bool = False
    ignore_user_patterns: tuple[re.Pattern[str], ...] = ()
    invites: InviteMode = "allowlisted"
    reactions: bool = True
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES
    e2ee: E2eeMode = "off"
    e2ee_trust: E2eeTrust = "owner_signed"
    allow_room_mentions: bool = False
    thread_require_mention: bool = False
    # Who may pull the bot into a room (``invites: allowlisted``); read from the policy lists.
    inviters: frozenset[str] = field(default_factory=frozenset[str])
    anyone_may_invite: bool = False

    @classmethod
    def read(cls, extra: Mapping[str, Any], env: Mapping[str, str]) -> MatrixOptions:
        patterns: list[re.Pattern[str]] = []
        for raw in sorted(_ids(extra.get("ignore_user_patterns"))):
            try:
                patterns.append(re.compile(raw))
            except re.error as exc:
                log.warning("matrix: ignore_user_patterns entry %r is not a regex: %s", raw, exc)
        invites = str(extra.get("invites") or "allowlisted").strip().lower()
        e2ee = str(extra.get("e2ee") or "off").strip().lower()
        try:
            cap = int(extra.get("max_media_bytes") or DEFAULT_MAX_MEDIA_BYTES)
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_MEDIA_BYTES
        inviters = (
            _ids(env.get("INDUS_MATRIX_ALLOWED_USERS", ""))
            | _ids(env.get("INDUS_GATEWAY_ALLOWED_USERS", ""))
            | _ids(extra.get("allow_from"))
        )
        anyone = (
            _truthy(env.get("INDUS_MATRIX_ALLOW_ALL_USERS"), False)
            or _truthy(env.get("INDUS_GATEWAY_ALLOW_ALL_USERS"), False)
            or "*" in inviters
        )
        return cls(
            allowed_rooms=_ids(extra.get("allowed_rooms"))
            | _ids(env.get("INDUS_MATRIX_ALLOWED_ROOMS", "")),
            auto_thread=_truthy(extra.get("auto_thread"), False),
            ignore_user_patterns=tuple(patterns),
            invites=invites if invites in ("all", "off") else "allowlisted",  # pyright: ignore[reportArgumentType]
            reactions=_truthy(extra.get("reactions"), True),
            max_media_bytes=max(1, cap),
            e2ee=e2ee if e2ee in ("optional", "required") else "off",  # pyright: ignore[reportArgumentType]
            e2ee_trust=(
                "verified"
                if str(extra.get("e2ee_trust") or "").strip().lower() == "verified"
                else "owner_signed"
            ),
            allow_room_mentions=_truthy(extra.get("allow_room_mentions"), False),
            thread_require_mention=_truthy(extra.get("thread_require_mention"), False),
            inviters=inviters,
            anyone_may_invite=anyone,
        )


def _resolve_bang(text: str) -> str:
    """``!name rest`` → ``/name rest`` when the command registry knows the name."""
    match = _BANG.match(text)
    if match is None:
        return text
    from indus.service.commands.registry import REGISTRY

    name = match.group(1).lower()
    for candidate in dict.fromkeys((name, name.replace("_", "-"))):
        if REGISTRY.resolve(candidate) is not None:
            return f"/{candidate}{match.group(2)}"
    return text


def _split_fallback(body: str) -> tuple[str | None, str | None, str]:
    """``> <@a:b> quoted\\n> more\\n\\nreply`` → (quoted text, its author, reply text)."""
    if not body.startswith(">"):
        return None, None, body
    lines = body.split("\n")
    quoted: list[str] = []
    author: str | None = None
    index = 0
    while index < len(lines) and lines[index].startswith(">"):
        line = lines[index]
        found = _FALLBACK_AUTHOR.match(line) if index == 0 else None
        if found is not None:
            author = found.group(1)
            quoted.append(found.group(2))
        else:
            quoted.append(line[1:].lstrip(" ") if len(line) > 1 else "")
        index += 1
    if index < len(lines) and lines[index] == "":
        index += 1
    text = "\n".join(lines[index:])
    return ("\n".join(quoted).strip() or None), author, text


def _looks_like_filename(body: str) -> bool:
    """A media body that is only the uploaded file's name (no caption)."""
    candidate = body.strip()
    if not candidate or any(c.isspace() for c in candidate) or "/" in candidate:
        return False
    stem, dot, ext = candidate.rpartition(".")
    return bool(stem and dot and ext and len(ext) <= 5 and ext.isalnum())


def _localpart(user_id: str) -> str:
    return user_id[1:].split(":", 1)[0] if user_id.startswith("@") and ":" in user_id else ""


class MatrixAdapter(Adapter):
    platform: ClassVar[str] = "matrix"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=CHUNK_BYTES,
        len_fn=utf8_bytes,
        markup="markdown",
        splits_natively=True,
        supports_edit=True,
        supports_delete=True,
        supports_buttons=False,
        dm_is_private=True,
        push_capable=True,
    )
    # Our own Client-Server API client (no SDK): the drift check proves the methods exist; the
    # vendor routes they call are ``client.ROUTES`` (served by the wire fake, checked in tests).
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "indus.gateway.adapters.matrix.client:MatrixClient": (
            "whoami",
            "sync",
            "send_event",
            "redact",
            "set_typing",
            "account_data",
            "set_account_data",
            "resolve_alias",
            "join",
            "room_state",
            "upload",
            "download",
            "joined_members",
        ),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        homeserver: str,
        token: str,
        settings: PlatformSettings | None = None,
        user_id: str = "",
        device_id: str = "",
        options: MatrixOptions | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        scope: Scope | None = None,
        end_to_end: EndToEndFactory | None = None,
        send_only: bool = False,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(token,))
        # ``indus send`` (``AdapterConfig.send_only``): ``whoami`` only, never a ``/sync``.
        self.send_only = send_only
        self._homeserver = homeserver.strip().rstrip("/")
        self._token = token.strip()
        self._configured_user = user_id.strip()
        self.device_id = device_id.strip()
        self.options = options or MatrixOptions()
        self._transport = transport
        self._proxy = proxy
        self._scope = scope
        self._end_to_end_factory = end_to_end
        self.user_id = ""
        self._client: MatrixClient | None = None
        self._crypto: EndToEnd | None = None
        self._listener: asyncio.Task[None] | None = None
        self._listen_started_ms = 0
        self._side_tasks: set[asyncio.Task[Any]] = set()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._direct_rooms: set[str] = set()
        self._direct_by_user: dict[str, list[str]] = {}
        self._member_counts: dict[str, int] = {}
        self._joined: set[str] = set()
        self._names: dict[str, str] = {}
        self._display: dict[tuple[str, str], str] = {}
        self._targets: dict[str, str] = {}
        self._own_events: OrderedDict[str, None] = OrderedDict()
        self._warned_encrypted: set[str] = set()
        self._invites_seen: set[str] = set()
        self._pending_reactions: dict[tuple[str, str], str] = {}
        self._our_threads: OrderedDict[str, None] = OrderedDict()
        # Encrypted attachments seen inbound: mxc URI → its ``file`` block (key, iv, hash).
        self._encrypted_media: OrderedDict[str, dict[str, Any]] = OrderedDict()

    @property
    def scope(self) -> Scope | None:
        """The profile this adapter serves (the encryption layer keeps its store there)."""
        return self._scope

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<MatrixAdapter {self._homeserver or '?'} {state}>"

    # -- lifecycle ---------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        if not self._homeserver or not self._token:
            missing = [
                name
                for name, value in ((HOMESERVER_ENV, self._homeserver), (TOKEN_ENV, self._token))
                if not value
            ]
            raise AdapterConnectError(
                "Matrix is not configured: set " + " and ".join(missing),
                retryable=False,
                code="config",
            )
        if not self._homeserver.startswith(("https://", "http://")):
            raise AdapterConnectError(
                "MATRIX_HOMESERVER must be an http(s) URL", retryable=False, code="config"
            )
        client = MatrixClient(
            self._homeserver, self._token, transport=self._transport, proxy=self._proxy
        )
        try:
            who = await client.whoami()
            if not who.user_id:
                raise MatrixError("unexpected answer from the homeserver: no user id")
            self._adopt_identity(who.user_id, who.device_id)
            crypto = await self._start_end_to_end(client)
        except MatrixError as exc:
            await client.aclose()
            if exc.is_auth:
                raise AdapterConnectError(
                    f"the homeserver refused the access token ({exc.errcode or exc.status})",
                    retryable=False,
                    code="auth_failed",
                ) from None
            raise AdapterConnectError(str(exc), retryable=True) from None
        except BaseException:
            await client.aclose()
            raise
        self._client = client
        self._crypto = crypto
        log.info("matrix: signed in as %s on %s", self.user_id, self._homeserver)
        if self._sink is not None:
            self._start_listening()

    def _adopt_identity(self, user_id: str, device_id: str) -> None:
        """The token's user and device win over configured ones (keys belong to the device)."""
        if self._configured_user and self._configured_user.lower() != user_id.lower():
            log.warning(
                "matrix: the access token belongs to %s, not the configured %s; using the token's",
                user_id,
                self._configured_user,
            )
        if device_id and self.device_id and device_id != self.device_id:
            # A token can only act for its own device (end-to-end keys depend on it).
            log.warning(
                "matrix: the access token belongs to device %s, not the configured %s; "
                "using the token's",
                device_id,
                self.device_id,
            )
        if device_id:
            self.device_id = device_id
        self.user_id = user_id

    async def _start_end_to_end(self, client: MatrixClient) -> EndToEnd | None:
        mode = self.options.e2ee
        if mode == "off":
            return None
        factory = self._end_to_end_factory
        hint = (
            "end-to-end encryption is not available in this install (install the "
            "matrix-e2ee extra on Linux: pip install 'indus-agent[matrix-e2ee]')"
        )
        if factory is None:
            if mode == "required":
                raise AdapterConnectError(
                    hint + "; set platforms.matrix.e2ee to off or optional to run without it",
                    retryable=False,
                    code="e2ee_unavailable",
                )
            log.warning("matrix: %s; encrypted rooms are skipped", hint)
            return None
        try:
            return await factory(self, client)
        except AdapterConnectError:
            raise
        except Exception as exc:
            if mode == "required":
                raise AdapterConnectError(
                    "end-to-end encryption could not start: " + self.scrub(str(exc)),
                    retryable=False,
                    code="e2ee_failed",
                ) from None
            log.warning("matrix: end-to-end encryption could not start (%s)", self.scrub(str(exc)))
            return None

    async def _close(self) -> None:
        self._stop_listening()
        listener, self._listener = self._listener, None
        if listener is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener
        side = [t for t in self._side_tasks if not t.done()]
        for task in side:
            task.cancel()
        for task in side:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._side_tasks.clear()
        crypto, self._crypto = self._crypto, None
        if crypto is not None:
            with contextlib.suppress(Exception):
                await crypto.aclose()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def set_sink(self, sink: Any) -> None:
        """Installing a sink starts the sync loop (never when send-only); removing it stops
        reading."""
        super().set_sink(sink)
        if sink is None:
            self._stop_listening()
        elif self.connected and self._client is not None:
            self._start_listening()

    @property
    def listening(self) -> bool:
        return self._listener is not None and not self._listener.done()

    def _start_listening(self) -> None:
        if self.listening or self.send_only:
            return
        self._listener = asyncio.ensure_future(self._listen())

    def _stop_listening(self) -> None:
        if self._listener is not None and not self._listener.done():
            self._listener.cancel()

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    def is_final_failure(self, result: SendResult) -> bool:
        """A refused token: no retry and no plain-text resend can help."""
        return "unauthorized" in (result.error or "")

    def _require_client(self) -> MatrixClient:
        if self._client is None:
            raise ConnectionError("not connected to the Matrix homeserver")
        return self._client

    # -- inbound -----------------------------------------------------------------------------------

    def _now_ms(self) -> int:
        return int(self.clock.now().timestamp() * 1000)

    async def _listen(self) -> None:
        started_ms = self._now_ms()
        self._listen_started_ms = started_ms
        since: str | None = None
        delay = BACKOFF_FIRST_S
        while True:
            client = self._client
            if client is None:
                return
            try:
                data = await client.sync(
                    since=since,
                    timeout_ms=0 if since is None else SYNC_TIMEOUT_MS,
                    filter_json=SYNC_FILTER,
                )
            except asyncio.CancelledError:
                raise
            except MatrixError as exc:
                if exc.is_auth:
                    log.error("matrix: the homeserver refused the access token; inbound stops")
                    self.report_fatal(
                        "matrix_auth",
                        "the homeserver refused the access token while syncing; create a new "
                        "token and update MATRIX_ACCESS_TOKEN",
                    )
                    return
                wait = max(delay, exc.retry_after or 0.0)
                log.warning("matrix: sync failed (%s); retrying in %.0f s", exc, wait)
                await self.clock.sleep(wait)
                delay = min(delay * 2, BACKOFF_CAP_S)
                continue
            except Exception as exc:
                log.warning(
                    "matrix: sync failed (%s); retrying in %.0f s",
                    self.scrub(f"{type(exc).__name__}: {exc}"),
                    delay,
                )
                await self.clock.sleep(delay)
                delay = min(delay * 2, BACKOFF_CAP_S)
                continue
            delay = BACKOFF_FIRST_S
            try:
                await self._absorb(data, started_ms)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("matrix: a sync answer could not be handled")
            following = str(data.get("next_batch") or "")
            if not following:
                log.warning("matrix: sync answer without next_batch; retrying in %.0f s", delay)
                await self.clock.sleep(delay)
                continue
            since = following

    async def _absorb(self, data: Mapping[str, Any], started_ms: int) -> None:
        if self._crypto is not None:
            await self._crypto.absorb_sync(data)
        for event in _events(data.get("account_data")):
            if event.get("type") == "m.direct":
                self._set_direct(event.get("content"))
        rooms = _mapping(data.get("rooms"))
        for room_id, room in _mapping(rooms.get("join")).items():
            await self._absorb_room(str(room_id), _mapping(room), started_ms)
        for room_id, invite in _mapping(rooms.get("invite")).items():
            self._on_invite(str(room_id), _mapping(invite))
        for room_id in _mapping(rooms.get("leave")):
            self._forget_room(str(room_id))

    async def _absorb_room(self, room_id: str, room: Mapping[str, Any], started_ms: int) -> None:
        self._joined.add(room_id)
        count = _mapping(room.get("summary")).get("m.joined_member_count")
        if isinstance(count, int):
            self._member_counts[room_id] = count
        for event in _events(room.get("state")):
            self._note_state(room_id, event)
        for event in _events(room.get("timeline")):
            if "state_key" in event:
                self._note_state(room_id, event)
                continue
            await self._on_timeline_event(room_id, event, started_ms)

    def _note_state(self, room_id: str, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        content = _mapping(event.get("content"))
        if kind == "m.room.name":
            self._names[room_id] = str(content.get("name") or "")
        elif kind == "m.room.canonical_alias" and not self._names.get(room_id):
            alias = str(content.get("alias") or "")
            if alias:
                self._names.setdefault(room_id, alias)
        elif kind == "m.room.member":
            who = str(event.get("state_key") or "")
            name = content.get("displayname")
            if who and isinstance(name, str) and name.strip():
                self._display[(room_id, who)] = name.strip()

    def _forget_room(self, room_id: str) -> None:
        self._joined.discard(room_id)
        self._member_counts.pop(room_id, None)
        self._names.pop(room_id, None)

    async def _on_timeline_event(
        self, room_id: str, event: Mapping[str, Any], started_ms: int
    ) -> None:
        kind = event.get("type")
        if kind == "m.room.encrypted":
            clear = await self._decrypt(room_id, event)
            if clear is None:
                return
            event, kind = clear, clear.get("type")
        if kind != "m.room.message":
            return
        normalised = self.normalise(room_id, event, started_ms)
        if normalised is not None:
            await self.emit(normalised)

    async def take_late_event(self, room_id: str, event: Mapping[str, Any]) -> None:
        """An encrypted event whose key arrived after it (decrypted by the encryption layer):
        it goes through the same rules as any timeline event."""
        if event.get("type") != "m.room.message":
            return
        normalised = self.normalise(room_id, event, self._listen_started_ms)
        if normalised is not None:
            await self.emit(normalised)

    async def _decrypt(self, room_id: str, event: Mapping[str, Any]) -> dict[str, Any] | None:
        if self._crypto is None:
            if room_id not in self._warned_encrypted:
                self._warned_encrypted.add(room_id)
                log.warning(
                    "matrix: room %s sends encrypted messages and end-to-end encryption is off; "
                    "its messages are skipped (set platforms.matrix.e2ee)",
                    room_id,
                )
            return None
        try:
            clear = await self._crypto.decrypt(room_id, event)
        except Exception as exc:
            log.warning("matrix: decrypting an event in %s failed: %s", room_id, exc)
            return None
        if clear is None:
            return None
        merged = dict(event)
        merged.update(clear)
        return merged

    def _set_direct(self, content: object) -> None:
        mapping = _mapping(content)
        by_user: dict[str, list[str]] = {}
        rooms: set[str] = set()
        for user, listed in mapping.items():
            if isinstance(listed, list):
                ids = [str(r) for r in listed if isinstance(r, str)]  # pyright: ignore[reportUnknownVariableType]
                by_user[str(user)] = ids
                rooms.update(ids)
        self._direct_by_user = by_user
        self._direct_rooms = rooms

    def is_dm(self, room_id: str) -> bool:
        if room_id in self._direct_rooms:
            return True
        count = self._member_counts.get(room_id)
        return count is not None and count <= 2

    def _join_thread(self, root: str) -> None:
        self._our_threads[root] = None
        self._our_threads.move_to_end(root)
        while len(self._our_threads) > SEEN_EVENTS:
            self._our_threads.popitem(last=False)

    def _seen_before(self, event_id: str) -> bool:
        if event_id in self._seen:
            return True
        self._seen[event_id] = None
        while len(self._seen) > SEEN_EVENTS:
            self._seen.popitem(last=False)
        return False

    def _is_own(self, sender: str) -> bool:
        # Without our id we cannot prove a sender is someone else: dropping beats an echo loop.
        own = self.user_id.strip().lower()
        return not own or sender.strip().lower() == own

    def _ignored_sender(self, sender: str) -> bool:
        local = _localpart(sender)
        if not local or local.startswith("_"):
            return True  # an application-service ghost (bridge) or a malformed id
        return any(p.search(sender) for p in self.options.ignore_user_patterns)

    def mentions_me(self, body: str, content: Mapping[str, Any]) -> bool:
        own = self.user_id
        if not own:
            return False
        mentions = content.get("m.mentions")
        if isinstance(mentions, Mapping):
            listed = mentions.get("user_ids")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(listed, list) and own in listed:
                return True
        if own.lower() in body.lower():
            return True
        local = _localpart(own)
        if local and re.search(rf"(?<![\w@])@{re.escape(local)}\b", body, re.IGNORECASE):
            return True
        formatted = content.get("formatted_body")
        if isinstance(formatted, str):
            low = formatted.lower()
            return f"matrix.to/#/{own.lower()}" in low or f"matrix.to/#/%40{own[1:].lower()}" in low
        return False

    def _strip_mention(self, text: str) -> str:
        own = self.user_id
        if own:
            text = re.sub(re.escape(own) + r"[:,]?", "", text, flags=re.IGNORECASE)
        local = _localpart(own)
        if local:
            text = re.sub(rf"(?<![\w@])@{re.escape(local)}\b[:,]?", "", text, flags=re.IGNORECASE)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    def normalise(
        self, room_id: str, event: Mapping[str, Any], started_ms: int
    ) -> InboundEvent | None:
        """One timeline ``m.room.message`` → an `InboundEvent`, or None when it is dropped."""
        sender = str(event.get("sender") or "")
        event_id = str(event.get("event_id") or "")
        if not sender or not event_id or self._is_own(sender) or self._ignored_sender(sender):
            return None
        if self._seen_before(event_id):
            return None
        sent_ms = event.get("origin_server_ts")
        if isinstance(sent_ms, int | float) and sent_ms < started_ms - STARTUP_GRACE_S * 1000:
            return None
        content = _mapping(event.get("content"))
        relates = _mapping(content.get("m.relates_to"))
        if relates.get("rel_type") == "m.replace":
            return None
        dm = self.is_dm(room_id)
        if not dm and self.options.allowed_rooms and room_id not in self.options.allowed_rooms:
            log.debug("matrix: %s is not in allowed_rooms; message ignored", room_id)
            return None
        msgtype = str(content.get("msgtype") or "")
        body = str(content.get("body") or "")
        in_thread = relates.get("rel_type") == "m.thread"
        thread_root = str(relates.get("event_id") or "") if in_thread else ""
        reply = _mapping(relates.get("m.in_reply_to"))
        reply_to = str(reply.get("event_id") or "") or None
        if thread_root and relates.get("is_falling_back") is True:
            reply_to = None  # the thread fallback, not a reply the user chose
        quoted = quoted_author = None
        if reply_to is not None and msgtype in _TEXT_TYPES:
            quoted, quoted_author, body = _split_fallback(body)
        media: tuple[Attachment, ...] = ()
        metadata: dict[str, Any] = {}
        if msgtype in _TEXT_TYPES:
            kind = MessageKind.TEXT
            text = body
        elif msgtype in _MEDIA_TYPES:
            found = self._media_of(room_id, event_id, msgtype, content)
            if found is None:
                return None
            kind, text, attachment, duration = found
            media = (attachment,)
            if duration is not None:
                metadata["duration"] = duration
        else:
            return None
        reply_to_own = reply_to is not None and (
            reply_to in self._own_events
            or (quoted_author is not None and quoted_author.lower() == self.user_id.lower())
        )
        named = self.mentions_me(text, content)
        if named:
            text = self._strip_mention(text)
        # A thread the bot already takes part in needs no new mention (``thread_require_mention``
        # turns that off, for rooms where several bots share threads).
        in_our_thread = bool(thread_root) and thread_root in self._our_threads
        if in_our_thread and self.options.thread_require_mention:
            in_our_thread = False
        if named or reply_to_own or in_our_thread:
            metadata["mentioned"] = True
        if kind is MessageKind.TEXT:
            text = _resolve_bang(text)
            if msgtype == "m.emote":
                text = f"* {text}"
        chat_type: ChatType = "dm" if dm else "group"
        thread_id: str | None = thread_root or None
        if thread_id is None and not dm and self.options.auto_thread:
            thread_id = event_id
        if thread_id is not None and not dm:
            chat_type = "thread"
        if thread_id is not None and (named or reply_to_own):
            self._join_thread(thread_id)
        when = None
        if isinstance(sent_ms, int | float):
            when = datetime.fromtimestamp(sent_ms / 1000, UTC)
        source = Source(
            platform=self.platform,
            chat_id=room_id,
            chat_type=chat_type,
            chat_name=self._names.get(room_id) or None,
            user_id=sender,
            user_name=self._display.get((room_id, sender)) or _localpart(sender) or sender,
            thread_id=thread_id,
            is_bot=msgtype == "m.notice",
            parent_chat_id=room_id if chat_type == "thread" else None,
        )
        return InboundEvent(
            source=source,
            text=text,
            kind=kind,
            message_id=event_id,
            media=media,
            reply_to_id=reply_to,
            reply_to_text=quoted,
            reply_to_author_id=quoted_author,
            reply_to_author_name=(
                self._display.get((room_id, quoted_author)) or _localpart(quoted_author)
                if quoted_author
                else None
            ),
            reply_to_is_own=reply_to_own,
            metadata=metadata,
            timestamp=when,
        )

    def _media_of(
        self, room_id: str, event_id: str, msgtype: str, content: Mapping[str, Any]
    ) -> tuple[MessageKind, str, Attachment, float | None] | None:
        url = content.get("url")
        encrypted: dict[str, Any] | None = None
        sealed = content.get("file")
        # An encrypted attachment carries ``file.url``: only the E2EE layer can read it.
        if (not isinstance(url, str) or not url) and isinstance(sealed, Mapping) and self._crypto:
            encrypted = dict(sealed)  # pyright: ignore[reportUnknownArgumentType]
            url = encrypted.get("url")
        if not isinstance(url, str) or not url:
            log.info("matrix: attachment %s in %s has no readable URL; skipped", event_id, room_id)
            return None
        if parse_mxc(url) is None:
            log.warning("matrix: attachment %s has a non-mxc URL; refused", event_id)
            return None
        info = _mapping(content.get("info"))
        size = info.get("size")
        if isinstance(size, int) and size > self.options.max_media_bytes:
            log.warning(
                "matrix: attachment %s is %d bytes (cap %d); refused",
                event_id,
                size,
                self.options.max_media_bytes,
            )
            return None
        body = str(content.get("body") or "")
        filename = content.get("filename")
        if isinstance(filename, str) and filename:
            caption = body if body and body != filename else ""
        else:
            caption = "" if _looks_like_filename(body) else body
        kind = _MEDIA_TYPES[msgtype]
        if msgtype == "m.audio" and _VOICE_FLAG in content:
            kind = MessageKind.VOICE
        mime = str(info.get("mimetype") or "")
        duration = info.get("duration")
        seconds = float(duration) / 1000 if isinstance(duration, int | float) else None
        if encrypted is not None:
            self._encrypted_media[url] = encrypted
            while len(self._encrypted_media) > SEEN_EVENTS:
                self._encrypted_media.popitem(last=False)
        return kind, caption, Attachment(ref=url, mime=mime), seconds

    def _on_invite(self, room_id: str, invite: Mapping[str, Any]) -> None:
        # A pending invite is listed by every sync until it is answered: decide once.
        if self.options.invites == "off" or room_id in self._joined | self._invites_seen:
            return
        self._invites_seen.add(room_id)
        inviter, direct = "", False
        for event in _events(invite.get("invite_state")):
            if event.get("type") == "m.room.member" and event.get("state_key") == self.user_id:
                inviter = str(event.get("sender") or "")
                direct = _mapping(event.get("content")).get("is_direct") is True
        self._spawn(self._accept_invite(room_id, inviter, direct))

    async def _may_invite(self, inviter: str) -> bool:
        if not inviter or self._ignored_sender(inviter):
            return False
        options = self.options
        if options.invites == "all" or options.anyone_may_invite or inviter in options.inviters:
            return True
        scope = self._scope
        if scope is None:
            return False
        from indus.state.pairing import PairingStore

        store = PairingStore(scope)
        try:
            return await run_blocking(
                lambda: store.is_approved(self.platform, inviter), label="matrix invite"
            )
        except Exception:
            return False

    async def _accept_invite(self, room_id: str, inviter: str, direct: bool) -> None:
        if not await self._may_invite(inviter):
            log.info("matrix: invite to %s from %s ignored (not allowed)", room_id, inviter or "?")
            return
        client = self._client
        if client is None:
            return
        try:
            await client.join(room_id)
        except MatrixError as exc:
            log.warning("matrix: joining %s failed: %s", room_id, exc)
            return
        self._joined.add(room_id)
        log.info("matrix: joined %s (invited by %s)", room_id, inviter)
        if direct:
            await self._remember_direct(client, inviter, room_id)

    async def _remember_direct(self, client: MatrixClient, user: str, room_id: str) -> None:
        self._direct_rooms.add(room_id)
        self._direct_by_user.setdefault(user, []).append(room_id)
        try:
            current = await client.account_data(self.user_id, "m.direct") or {}
            listed = current.get(user)
            rooms = [str(r) for r in listed] if isinstance(listed, list) else []  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            if room_id not in rooms:
                rooms.append(room_id)
                await client.set_account_data(self.user_id, "m.direct", {**current, user: rooms})
        except MatrixError as exc:
            log.warning("matrix: recording %s as a direct chat failed: %s", room_id, exc)

    # -- media for the intake ----------------------------------------------------------------------

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The bytes of an inbound attachment (registered by `register_fetchers`)."""
        client = self._require_client()
        sealed = self._encrypted_media.get(attachment.ref)
        if sealed is None:
            async for chunk in client.download(attachment.ref, cap=self.options.max_media_bytes):
                yield chunk
            return
        crypto = self._crypto
        if crypto is None:
            raise MatrixError("the attachment is encrypted and end-to-end encryption is off")
        parts = [c async for c in client.download(attachment.ref, cap=self.options.max_media_bytes)]
        yield await crypto.decrypt_attachment(b"".join(parts), sealed)

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register `fetch_media` with the media intake (P15-23); returns the undo."""
        return seat.register_fetcher(self.platform, self.fetch_media)

    # -- outbound ----------------------------------------------------------------------------------

    async def room_for(self, chat_id: str) -> str:
        """The room a target names: a room id as is, ``#alias:server`` through the directory,
        ``@user:server`` through ``m.direct`` (an existing direct chat; none is created)."""
        target = chat_id.strip()
        if not target or target.startswith("!"):
            return target
        cached = self._targets.get(target)
        if cached is not None:
            return cached
        client = self._require_client()
        if target.startswith("#"):
            room = await client.resolve_alias(target)
        elif target.startswith("@"):
            room = await self._direct_room_with(client, target)
        else:
            return target
        self._targets[target] = room
        return room

    async def _direct_room_with(self, client: MatrixClient, user: str) -> str:
        rooms = self._direct_by_user.get(user)
        if not rooms:
            data = await client.account_data(self.user_id, "m.direct")
            if data is not None:
                self._set_direct(data)
            rooms = self._direct_by_user.get(user)
        if not rooms:
            raise MatrixError("chat not found", detail=f"no direct chat with {user}")
        joined = [r for r in rooms if r in self._joined]
        return (joined or rooms)[-1]

    def _text_content(self, text: str, plain: bool, msgtype: str = "m.text") -> dict[str, Any]:
        content: dict[str, Any] = {"msgtype": msgtype, "body": text}
        users = [u for u in mentioned_users(text) if u.lower() != self.user_id.lower()]
        mentions: dict[str, Any] = {"user_ids": users} if users else {}
        if self.options.allow_room_mentions and re.search(r"(?<![\w@])@room\b", text):
            mentions["room"] = True
        content["m.mentions"] = mentions
        if not plain:
            html = to_html(text)
            if html != escape(text, quote=False):
                content["format"] = "org.matrix.custom.html"
                content["formatted_body"] = html
                if utf8_bytes(compact_json(content)) > MAX_CONTENT_BYTES:
                    del content["format"], content["formatted_body"]
        return content

    @staticmethod
    def _relate(content: dict[str, Any], reply_to: str | None, thread_id: str | None) -> None:
        if thread_id:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_id,
                "is_falling_back": reply_to is None,
                "m.in_reply_to": {"event_id": reply_to or thread_id},
            }
        elif reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}

    async def _send_event(
        self, room_id: str, content: dict[str, Any], event_type: str = "m.room.message"
    ) -> str:
        client = self._require_client()
        if self._crypto is not None and await self._crypto.is_encrypted(room_id):
            event_type, content = await self._crypto.encrypt(room_id, event_type, content)
        event_id = await client.send_event(room_id, event_type, content)
        self._own_events[event_id] = None
        while len(self._own_events) > SEEN_EVENTS:
            self._own_events.popitem(last=False)
        return event_id

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        room = await self.room_for(chat_id)
        plain = meta.get("plain") is True
        thread_id = str(meta.get("thread_id") or "") or None
        if thread_id is not None:
            self._join_thread(thread_id)
        chunked = split_message(text, CHUNK_BYTES, len_fn=utf8_bytes)
        sent: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            content = self._text_content(chunk.text, plain)
            self._relate(content, reply_to if n == 0 else None, thread_id)
            try:
                sent.append(await self._send_event(room, content))
            except Exception as exc:
                remainder = None if is_ambiguous_timeout(exc) else chunked.remainder(n)
                return SendResult.failure(
                    exc=exc,
                    retry_after=getattr(exc, "retry_after", None),
                    delivered_ids=tuple(sent),
                    remainder=remainder if sent else None,
                )
        return SendResult.sent(sent[-1], tuple(sent[:-1]))

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        if utf8_bytes(text) > CHUNK_BYTES:
            return SendResult.failure("message is too long for one Matrix edit")
        room = await self.room_for(chat_id)
        fresh = self._text_content(text, meta.get("plain") is True)
        fresh.pop("m.mentions", None)
        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": f"* {text}",
            "m.new_content": fresh,
            "m.relates_to": {"rel_type": "m.replace", "event_id": message_id},
            # The edit notifies nobody again.
            "m.mentions": {},
        }
        if "formatted_body" in fresh:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = f"* {fresh['formatted_body']}"
        if utf8_bytes(compact_json(content)) > MAX_CONTENT_BYTES:
            # Edit-aware clients show m.new_content; the top-level copy is only a fallback.
            content.pop("format", None)
            content.pop("formatted_body", None)
            content["body"] = "* (edited)"
        try:
            await self._send_event(room, content)
        except Exception as exc:
            return SendResult.failure(exc=exc, retry_after=getattr(exc, "retry_after", None))
        return SendResult.sent(message_id)

    async def _delete(self, chat_id: str, message_id: str) -> bool:
        room = await self.room_for(chat_id)
        await self._require_client().redact(room, message_id)
        return True

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        room = await self.room_for(chat_id)
        await self._require_client().set_typing(
            room, self.user_id, typing=True, timeout_ms=TYPING_TIMEOUT_MS
        )

    async def _typing_stop(self, chat_id: str) -> None:
        room = await self.room_for(chat_id)
        await self._require_client().set_typing(room, self.user_id, typing=False)

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        room = await self.room_for(chat_id)
        file = Path(path)
        size = (await run_blocking(file.stat, label="matrix upload")).st_size
        if size > self.options.max_media_bytes:
            return SendResult.failure(
                f"the file is too large for Matrix ({size} bytes, the cap is "
                f"{self.options.max_media_bytes})"
            )
        data = await run_blocking(file.read_bytes, label="matrix upload")
        name = file.name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        size = len(data)
        sealed: dict[str, Any] | None = None
        upload_mime = mime
        if self._crypto is not None and await self._crypto.is_encrypted(room):
            # The event is encrypted; the file must be too, or the media store would serve it
            # in clear to anyone holding the URI.
            data, sealed = await self._crypto.encrypt_attachment(data)
            upload_mime = "application/octet-stream"
        try:
            uri = await self._require_client().upload(data, mime=upload_mime, filename=name)
        except Exception as exc:
            return SendResult.failure(exc=exc, retry_after=getattr(exc, "retry_after", None))
        content: dict[str, Any] = {
            "msgtype": _OUT_TYPES[kind],
            "body": caption or name,
            "info": {"mimetype": mime, "size": size},
            "m.mentions": {},
        }
        if sealed is not None:
            content["file"] = {**sealed, "url": uri}
        else:
            content["url"] = uri
        if caption:
            content["filename"] = name
        if kind == "voice":
            content[_VOICE_FLAG] = {}
            content["org.matrix.msc1767.audio"] = {}
        reply_to = meta.get(REPLY_TO_KEY)
        self._relate(
            content,
            str(reply_to) if reply_to else None,
            str(meta.get("thread_id") or "") or None,
        )
        try:
            event_id = await self._send_event(room, content)
        except Exception as exc:
            return SendResult.failure(exc=exc, retry_after=getattr(exc, "retry_after", None))
        return SendResult.sent(event_id)

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        room = await self.room_for(chat_id)
        name = self._names.get(room)
        if name is None:
            try:
                state = await self._require_client().room_state(room, "m.room.name")
                name = str(state.get("name") or "")
            except MatrixError as exc:
                if exc.status != 404 and exc.errcode != "M_NOT_FOUND":
                    raise
                name = ""
            self._names[room] = name
        return ChatInfo(name=name or room, type="dm" if self.is_dm(room) else "group")

    # -- reactions (processing state) --------------------------------------------------------------

    async def react(self, chat_id: str, message_id: str, key: str) -> str | None:
        """Annotate ``message_id`` with ``key``; the reaction's event id, or None."""
        try:
            room = await self.room_for(chat_id)
            content = {
                "m.relates_to": {"rel_type": "m.annotation", "event_id": message_id, "key": key}
            }
            return await self._send_event(room, content, "m.reaction")
        except Exception as exc:
            log.debug("matrix: reaction failed: %s", self.scrub(str(exc)))
            return None

    async def processing_started(self, chat_id: str, message_id: str) -> None:
        """Show that a message is being worked on (``reactions: true``)."""
        if not self.options.reactions:
            return
        reaction = await self.react(chat_id, message_id, "\U0001f440")
        if reaction is not None:
            self._pending_reactions[(chat_id, message_id)] = reaction

    async def processing_finished(self, chat_id: str, message_id: str, *, ok: bool) -> None:
        """Replace the working mark with a done (or failed) one."""
        if not self.options.reactions:
            return
        working = self._pending_reactions.pop((chat_id, message_id), None)
        if working is not None:
            await self.delete(chat_id, working)
        await self.react(chat_id, message_id, "✅" if ok else "❌")


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value  # pyright: ignore[reportUnknownVariableType]
    return {}


def _events(section: object) -> list[Mapping[str, Any]]:
    """The ``events`` list of a sync section, keeping only objects."""
    raw = _mapping(section).get("events")
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, Mapping)]  # pyright: ignore[reportUnknownVariableType]


def load_end_to_end() -> EndToEndFactory | None:
    """The E2EE factory ``matrix.crypto.create_end_to_end`` (P16-07), or None when absent."""
    name = "indus.gateway.adapters.matrix.crypto"
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name in (name, "indus.gateway.adapters.matrix"):
            return None
        log.warning("matrix: the encryption module could not load: %s", exc)
        return None
    factory = getattr(module, "create_end_to_end", None)
    return cast("EndToEndFactory", factory) if callable(factory) else None
