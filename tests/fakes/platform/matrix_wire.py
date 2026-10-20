"""A fake Matrix homeserver behind ``httpx.MockTransport`` (P16-06).

The Matrix adapter speaks plain HTTP (our own Client-Server API client), so the wire fake is the
homeserver's HTTP surface: every route in ``ROUTES`` (the client's ``client.ROUTES`` must be a
subset, checked by the adapter tests), answering in the spec's JSON shapes over one
`FakeChatPlatform`. Nothing opens a socket.

- Rooms: a group chat's id is its room id (``!ops:example.org``); a DM chat (the platform keys it by
  the user id) lives in the room ``!dm.<localpart>:example.org``, which ``m.direct`` lists.
- ``/sync`` without ``since`` answers at once with whatever is queued (plus ``m.direct``); with
  ``since`` it waits on the fake clock for inbound traffic up to its ``timeout``. Inbound updates
  become timeline events: ``m.text`` (``m.notice`` for bots), media with ``mxc://`` URLs (voice
  flagged), ``m.mentions`` for a mention of the bot, ``m.thread`` / ``m.in_reply_to`` relations,
  ``m.replace`` for edits. Raw events can be queued with ``push_event``.
- Sends map back onto the platform: messages, threads and replies (``post``), media (``upload``),
  edits (``m.replace`` → ``edit``), redactions (``delete``), reactions (``react``), typing.
  Repeating a transaction id returns the first event id without a second post.
- Platform failures become the homeserver's answers: ``M_LIMIT_EXCEEDED`` with ``retry_after_ms``,
  ``M_NOT_FOUND`` ("Unknown room" / "Event not found"), ``M_FORBIDDEN``, ``M_BAD_JSON``,
  ``M_TOO_LARGE``; connect and read timeouts raise httpx's own exceptions.
- A request without the right bearer token gets 401 ``M_UNKNOWN_TOKEN``.
- End-to-end encryption (P16-07): ``room_encryption`` marks rooms encrypted (their
  ``m.room.encryption`` state is served and sent in syncs), ``members`` lists joined members, and
  ``keys`` (`KeyServer`) is the keys API (``KEY_ROUTES``: device and one-time keys, claims,
  to-device messages, cross-signing keys and signatures). Other users act through their own tokens
  (``add_device``); to-device messages for the bot's device arrive in its next sync.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
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
    from indus.gateway.adapters.matrix.adapter import MatrixAdapter, MatrixOptions

SERVER = "example.org"
HOMESERVER = "https://matrix.example.org"
BOT = "@indus:example.org"
MAX_BYTES = 15_000

Handler = Callable[[httpx.Request, list[str]], Awaitable[httpx.Response]]

# Route name → "METHOD template", the same spelling as ``client.ROUTES``.
ROUTES: dict[str, str] = {
    "whoami": "GET /_matrix/client/v3/account/whoami",
    "sync": "GET /_matrix/client/v3/sync",
    "send_event": "PUT /_matrix/client/v3/rooms/{roomId}/send/{eventType}/{txnId}",
    "redact": "PUT /_matrix/client/v3/rooms/{roomId}/redact/{eventId}/{txnId}",
    "set_typing": "PUT /_matrix/client/v3/rooms/{roomId}/typing/{userId}",
    "account_data": "GET /_matrix/client/v3/user/{userId}/account_data/{type}",
    "set_account_data": "PUT /_matrix/client/v3/user/{userId}/account_data/{type}",
    "resolve_alias": "GET /_matrix/client/v3/directory/room/{roomAlias}",
    "join": "POST /_matrix/client/v3/join/{roomIdOrAlias}",
    "room_state": "GET /_matrix/client/v3/rooms/{roomId}/state/{eventType}/{stateKey}",
    "joined_members": "GET /_matrix/client/v3/rooms/{roomId}/joined_members",
    "upload": "POST /_matrix/media/v3/upload",
    "download": "GET /_matrix/client/v1/media/download/{serverName}/{mediaId}",
    "download_legacy": "GET /_matrix/media/v3/download/{serverName}/{mediaId}",
}


# The keys API the encryption layer calls through ``MatrixClient.request`` (P16-07).
KEY_ROUTES: dict[str, str] = {
    "keys_upload": "POST /_matrix/client/v3/keys/upload",
    "keys_query": "POST /_matrix/client/v3/keys/query",
    "keys_claim": "POST /_matrix/client/v3/keys/claim",
    "send_to_device": "PUT /_matrix/client/v3/sendToDevice/{eventType}/{txnId}",
    "device_signing_upload": "POST /_matrix/client/v3/keys/device_signing/upload",
    "signatures_upload": "POST /_matrix/client/v3/keys/signatures/upload",
}


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def fake_token() -> str:
    """A token-looking value built at runtime (never a credential-shaped literal)."""
    return "-".join(["mx", "fake", "token", "0" * 8])


def _json(status: int, body: Mapping[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=dict(body))


def _matrix_error(status: int, errcode: str, error: str, **extra: Any) -> httpx.Response:
    return _json(status, {"errcode": errcode, "error": error, **extra})


def _failure(exc: FakePlatformError, request: httpx.Request) -> httpx.Response:
    """A platform failure the way a homeserver reports it (timeouts raise like httpx does)."""
    kind = exc.kind
    if kind is FailureKind.CONNECT_TIMEOUT:
        raise httpx.ConnectTimeout(exc.text, request=request)
    if kind is FailureKind.READ_TIMEOUT:
        raise httpx.ReadTimeout(exc.text, request=request)
    if kind is FailureKind.RATE_LIMIT:
        wait_ms = int((exc.retry_after if exc.retry_after is not None else 5.0) * 1000)
        return _matrix_error(429, "M_LIMIT_EXCEEDED", "Too many requests", retry_after_ms=wait_ms)
    if kind is FailureKind.CHAT_NOT_FOUND:
        return _matrix_error(404, "M_NOT_FOUND", "Unknown room")
    if kind is FailureKind.THREAD_NOT_FOUND:
        return _matrix_error(404, "M_NOT_FOUND", "Event not found")
    if kind is FailureKind.FORBIDDEN:
        return _matrix_error(403, "M_FORBIDDEN", "You are not allowed to send messages here")
    if kind is FailureKind.PARSE_ERROR:
        return _matrix_error(400, "M_BAD_JSON", "Content is not valid")
    return _matrix_error(413, "M_TOO_LARGE", "Event is too large")


def _pattern(template: str) -> re.Pattern[str]:
    method, _, path = template.partition(" ")
    # A state key may be empty (``/state/m.room.encryption/``); every other part may not.
    body = re.escape(path).replace(r"\{stateKey\}", "([^/]*)")
    body = re.sub(r"\\\{[A-Za-z]+\\\}", "([^/]+)", body)
    return re.compile(f"^{method} {body}$")


class KeyServer:
    """The keys half of the homeserver: what each user's devices published, one-time key claims,
    to-device queues and cross-signing keys with their signatures. Signatures are stored, never
    checked (the clients check them)."""

    def __init__(self) -> None:
        self.device_keys: dict[str, dict[str, dict[str, Any]]] = {}
        self.one_time: dict[tuple[str, str], dict[str, Any]] = {}
        self.fallback: dict[tuple[str, str], dict[str, Any]] = {}
        self.cross_signing: dict[str, dict[str, dict[str, Any]]] = {}
        self.to_device: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.uploads: list[tuple[str, str]] = []
        self.changed: set[str] = set()

    def counts(self, user: str, device: str) -> dict[str, int]:
        keys = self.one_time.get((user, device), {})
        return {"signed_curve25519": sum(1 for k in keys if k.startswith("signed_curve25519:"))}

    def upload(self, user: str, device: str, body: Mapping[str, Any]) -> dict[str, Any]:
        self.uploads.append((user, device))
        published = body.get("device_keys")
        if isinstance(published, Mapping):
            self.device_keys.setdefault(user, {})[device] = json.loads(json.dumps(published))
            self.changed.add(user)
        for key_id, key in dict(body.get("one_time_keys") or {}).items():
            self.one_time.setdefault((user, device), {})[str(key_id)] = key
        for key_id, key in dict(body.get("fallback_keys") or {}).items():
            self.fallback.setdefault((user, device), {})[str(key_id)] = key
        return {"one_time_key_counts": self.counts(user, device)}

    def query(self, asker: str, body: Mapping[str, Any]) -> dict[str, Any]:
        answer: dict[str, Any] = {
            "device_keys": {},
            "master_keys": {},
            "self_signing_keys": {},
            "user_signing_keys": {},
            "failures": {},
        }
        for user, wanted in dict(body.get("device_keys") or {}).items():
            devices = self.device_keys.get(user, {})
            names = list(wanted) if wanted else list(devices)
            answer["device_keys"][user] = {d: devices[d] for d in names if d in devices}
            keys = self.cross_signing.get(user, {})
            for usage, field_name in (
                ("master", "master_keys"),
                ("self_signing", "self_signing_keys"),
                ("user_signing", "user_signing_keys"),
            ):
                if usage in keys and (usage != "user_signing" or user == asker):
                    answer[field_name][user] = keys[usage]
        return answer

    def claim(self, body: Mapping[str, Any]) -> dict[str, Any]:
        claimed: dict[str, dict[str, dict[str, Any]]] = {}
        for user, devices in dict(body.get("one_time_keys") or {}).items():
            for device, algorithm in dict(devices).items():
                pool = self.one_time.get((user, device), {})
                key_id = next((k for k in pool if k.startswith(f"{algorithm}:")), None)
                if key_id is not None:
                    claimed.setdefault(user, {})[device] = {key_id: pool.pop(key_id)}
                    continue
                spare = self.fallback.get((user, device), {})
                key_id = next((k for k in spare if k.startswith(f"{algorithm}:")), None)
                if key_id is not None:
                    claimed.setdefault(user, {})[device] = {key_id: spare[key_id]}
        return {"one_time_keys": claimed, "failures": {}}

    def send_to_device(self, sender: str, event_type: str, messages: Mapping[str, Any]) -> None:
        for user, devices in messages.items():
            for device, content in dict(devices).items():
                targets = list(self.device_keys.get(user, {})) if device == "*" else [device]
                for target in targets:
                    self.to_device.setdefault((user, target), []).append(
                        {"type": event_type, "sender": sender, "content": content}
                    )

    def take(self, user: str, device: str) -> list[dict[str, Any]]:
        return self.to_device.pop((user, device), [])

    def set_cross_signing(self, user: str, body: Mapping[str, Any]) -> None:
        for usage in ("master", "self_signing", "user_signing"):
            key = body.get(f"{usage}_key")
            if isinstance(key, Mapping):
                self.cross_signing.setdefault(user, {})[usage] = json.loads(json.dumps(key))
        self.changed.add(user)

    def add_signatures(self, body: Mapping[str, Any]) -> None:
        for user, signed in body.items():
            for key_id, obj in dict(signed).items():
                target = self.device_keys.get(user, {}).get(key_id)
                if target is None:
                    for key in self.cross_signing.get(user, {}).values():
                        if f"ed25519:{key_id}" in key.get("keys", {}):
                            target = key
                if target is None:
                    continue
                for signer, sigs in dict(obj.get("signatures") or {}).items():
                    target.setdefault("signatures", {}).setdefault(signer, {}).update(sigs)
                self.changed.add(user)


@dataclass
class SentEvent:
    room: str
    event_type: str
    content: dict[str, Any]
    txn: str


class _Transport(httpx.MockTransport):
    """Closing the adapter's HTTP client is the platform's disconnect."""

    def __init__(self, wire: MatrixWire) -> None:
        super().__init__(wire.handle)
        self._wire = wire

    async def aclose(self) -> None:
        await self._wire.platform.disconnect()


class MatrixWire:
    """The fake homeserver (see the module notes)."""

    ROUTES = ROUTES

    def __init__(
        self,
        platform: FakeChatPlatform | None = None,
        *,
        clock: FakeClock | None = None,
        bot_user: str = BOT,
        token: str | None = None,
    ) -> None:
        self.platform = platform or FakeChatPlatform(
            "matrix", clock=clock, max_len=MAX_BYTES, len_fn=utf8_len, bot_user_id=bot_user
        )
        self.clock: FakeClock = self.platform.clock
        self.bot_user = bot_user
        self.token = token or fake_token()
        self.device_id = "DEVICEONE"
        # Token → (user, device) of other users' devices (``add_device``); ``token`` is the bot's.
        self.identities: dict[str, tuple[str, str]] = {}
        self.keys = KeyServer()
        self.room_encryption: dict[str, dict[str, Any]] = {}
        self.members: dict[str, list[str]] = {}
        self.user_data: dict[tuple[str, str], dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.sent: list[SentEvent] = []
        self.account_data: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, str] = {}
        self.media: dict[str, tuple[bytes, str]] = {}
        self.joined: list[str] = []
        self.invites: dict[str, dict[str, Any]] = {}
        self.member_counts: dict[str, int] = {}
        self.sync_errors: list[httpx.Response | Exception] = []
        self.legacy_media_only = False
        self.typing: list[tuple[str, bool]] = []
        self._raw: list[tuple[str, dict[str, Any]]] = []
        self._raw_ready = asyncio.Event()
        self._txns: dict[str, str] = {}
        self._ids = itertools.count(1)
        self._batches = itertools.count(1)
        self._routes: list[tuple[re.Pattern[str], str, Handler]] = [
            (_pattern(template), name, getattr(self, f"_route_{name}"))
            for name, template in {**ROUTES, **KEY_ROUTES}.items()
        ]

    # -- wiring ------------------------------------------------------------------------------------

    def transport(self) -> httpx.AsyncBaseTransport:
        return _Transport(self)

    def adapter(self, *, options: MatrixOptions | None = None, **kw: Any) -> MatrixAdapter:
        """A `MatrixAdapter` on this fake (the factory's job in production)."""
        from indus.gateway.adapters.matrix.adapter import MatrixAdapter

        kw.setdefault("clock", self.clock)
        return MatrixAdapter(
            homeserver=HOMESERVER,
            token=self.token,
            transport=self.transport(),
            options=options,
            **kw,
        )

    def room_of(self, chat_id: str) -> str:
        chat = self.platform.chats.get(chat_id)
        if chat is not None and chat.kind == "dm" and chat_id.startswith("@"):
            return f"!dm.{chat_id[1:].split(':', 1)[0]}:{SERVER}"
        return chat_id

    def chat_of(self, room: str) -> str:
        for chat_id, chat in self.platform.chats.items():
            if chat.kind == "dm" and self.room_of(chat_id) == room:
                return chat_id
        return room

    def direct_map(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        for chat_id, chat in self.platform.chats.items():
            if chat.kind == "dm" and chat_id.startswith("@"):
                mapping[chat_id] = [self.room_of(chat_id)]
        stored = self.account_data.get("m.direct") or {}
        for user, rooms in stored.items():
            mapping[user] = list(dict.fromkeys([*mapping.get(user, []), *rooms]))
        return mapping

    def push_event(self, room: str, event: Mapping[str, Any]) -> None:
        """Queue a raw timeline event for the next sync."""
        self._raw.append((room, dict(event)))
        self._raw_ready.set()

    def event(
        self, sender: str, content: Mapping[str, Any], *, event_type: str = "m.room.message"
    ) -> dict[str, Any]:
        """A timeline event sent now (a fresh event id and the fake clock's time)."""
        return {
            "type": event_type,
            "event_id": f"$raw{next(self._ids)}",
            "sender": sender,
            "origin_server_ts": int(self.clock.now().timestamp() * 1000),
            "content": dict(content),
        }

    def invite(self, room: str, inviter: str, *, direct: bool = False) -> None:
        self.invites[room] = {
            "invite_state": {
                "events": [
                    {
                        "type": "m.room.member",
                        "state_key": self.bot_user,
                        "sender": inviter,
                        "content": {"membership": "invite", "is_direct": direct},
                    }
                ]
            }
        }
        self._raw_ready.set()

    def add_device(self, user: str, device: str) -> str:
        """Another user's device on this homeserver; its access token (built at run time)."""
        token = "-".join(["mx", "fake", user.strip("@").split(":", 1)[0], device.lower()])
        self.identities[token] = (user, device)
        return token

    def encrypt_room(self, room: str, **content: Any) -> None:
        self.room_encryption[room] = {"algorithm": "m.megolm.v1.aes-sha2", **content}

    def wake(self) -> None:
        """Let a waiting sync answer (to-device traffic arrived)."""
        self._raw_ready.set()

    def sync_paths(self) -> list[str]:
        return [path for method, path in self.requests if path.endswith("/sync")]

    # -- dispatch ----------------------------------------------------------------------------------

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        raw = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
        self.requests.append((request.method, raw))
        header = request.headers.get("Authorization", "")
        given = header[len("Bearer ") :] if header.startswith("Bearer ") else None
        who = (self.bot_user, self.device_id) if given == self.token else None
        if who is None and given is not None:
            who = self.identities.get(given)
        if who is None:
            return _matrix_error(401, "M_UNKNOWN_TOKEN", "Invalid access token passed.")
        request.extensions["matrix_who"] = who
        line = f"{request.method} {raw}"
        for pattern, _name, handler in self._routes:
            found = pattern.match(line)
            if found is not None:
                args = [unquote(part) for part in found.groups()]
                return await handler(request, args)
        return _matrix_error(404, "M_UNRECOGNIZED", "Unrecognized request")

    @staticmethod
    def _body(request: httpx.Request) -> dict[str, Any]:
        return json.loads(request.content or b"{}")

    @staticmethod
    def _who(request: httpx.Request) -> tuple[str, str]:
        return request.extensions["matrix_who"]

    # -- routes ------------------------------------------------------------------------------------

    async def _route_whoami(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        try:
            await self.platform.connect()
        except FakePlatformError as exc:
            return _failure(exc, request)
        return _json(200, {"user_id": self.bot_user, "device_id": self.device_id})

    async def _route_sync(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        if self.sync_errors:
            failure = self.sync_errors.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        since = request.url.params.get("since")
        timeout = int(request.url.params.get("timeout") or "0") / 1000.0
        items: list[FakeInbound]
        if since is None:
            items = self.platform.drain_inbound()
        else:
            items = await self._wait(timeout)
        return _json(200, self._sync_body(items, initial=since is None))

    async def _wait(self, timeout: float) -> list[FakeInbound]:
        first = self.platform.next_inbound()
        pending = self.keys.to_device.get((self.bot_user, self.device_id))
        if first is None and not self._raw and not self.invites and not pending:
            self._raw_ready.clear()
            getter = asyncio.ensure_future(self.platform.receive_inbound())
            raw = asyncio.ensure_future(self._raw_ready.wait())
            sleeper = asyncio.ensure_future(self.clock.sleep(timeout))
            waiting = {getter, raw, sleeper}
            try:
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waiting:
                    if not task.done():
                        task.cancel()
            if getter in done and not getter.cancelled():
                first = getter.result()
        items = [first] if first is not None else []
        items.extend(self.platform.drain_inbound())
        return items

    def _sync_body(self, items: list[FakeInbound], *, initial: bool) -> dict[str, Any]:
        join: dict[str, dict[str, Any]] = {}

        def room_entry(room: str, kind: str) -> dict[str, Any]:
            if room not in join:
                count = self.member_counts.get(room, 2 if kind == "dm" else 3)
                join[room] = {
                    "summary": {"m.joined_member_count": count},
                    "state": {"events": []},
                    "timeline": {"events": []},
                }
            return join[room]

        for item in items:
            event = self._event_of(item)
            if event is None:
                continue
            room = self.room_of(item.chat_id)
            entry = room_entry(room, item.chat_kind)
            if item.user_name:
                entry["state"]["events"].append(
                    {
                        "type": "m.room.member",
                        "state_key": item.user_id,
                        "sender": item.user_id,
                        "content": {"membership": "join", "displayname": item.user_name},
                    }
                )
            entry["timeline"]["events"].append(event)
        raw, self._raw = self._raw, []
        for room, event in raw:
            room_entry(room, "dm" if room.startswith("!dm.") else "group")["timeline"][
                "events"
            ].append(event)
        for room, content in self.room_encryption.items():
            if initial or room in join:
                room_entry(room, "dm" if room.startswith("!dm.") else "group")["state"][
                    "events"
                ].append({"type": "m.room.encryption", "state_key": "", "content": content})
        body: dict[str, Any] = {"next_batch": f"s{next(self._batches)}", "rooms": {"join": join}}
        to_device = self.keys.take(self.bot_user, self.device_id)
        if to_device:
            body["to_device"] = {"events": to_device}
        if self.keys.uploads:
            body["device_one_time_keys_count"] = self.keys.counts(self.bot_user, self.device_id)
        if self.keys.changed:
            body["device_lists"] = {"changed": sorted(self.keys.changed), "left": []}
            self.keys.changed = set()
        if self.invites:
            body["rooms"]["invite"], self.invites = self.invites, {}
        if initial:
            body["account_data"] = {"events": [{"type": "m.direct", "content": self.direct_map()}]}
        return body

    def _event_of(self, item: FakeInbound) -> dict[str, Any] | None:
        if item.event == "button":
            return None
        content: dict[str, Any]
        if item.media:
            media = item.media[0]
            msgtype = {
                "voice": "m.audio",
                "audio": "m.audio",
                "photo": "m.image",
                "sticker": "m.image",
                "video": "m.video",
                "document": "m.file",
            }[media.kind]
            media_id = f"in{item.message_id}"
            data = f"bytes of {media.ref}".encode()
            self.media[media_id] = (data, media.mime)
            info: dict[str, Any] = {"mimetype": media.mime, "size": len(data)}
            if media.duration is not None:
                info["duration"] = int(media.duration * 1000)
            content = {"msgtype": msgtype, "url": f"mxc://{SERVER}/{media_id}", "info": info}
            if media.caption:
                content.update(body=media.caption, filename=media.ref)
            else:
                content["body"] = media.ref
            if media.kind == "voice":
                content["org.matrix.msc3245.voice"] = {}
        else:
            content = {"msgtype": "m.notice" if item.is_bot else "m.text", "body": item.text}
        if item.mentions_bot:
            content["m.mentions"] = {"user_ids": [self.bot_user]}
        if item.thread_id:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": item.thread_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": item.thread_id},
            }
        elif item.reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": item.reply_to}}
        event_id = item.message_id
        if item.event == "edited":
            content = {
                "msgtype": "m.text",
                "body": f"* {item.text}",
                "m.new_content": {"msgtype": "m.text", "body": item.text},
                "m.relates_to": {"rel_type": "m.replace", "event_id": item.message_id},
            }
            event_id = f"{item.message_id}.edit{item.update_id}"
        return {
            "type": "m.room.message",
            "event_id": event_id,
            "sender": item.user_id,
            "origin_server_ts": int(item.at.timestamp() * 1000),
            "content": content,
        }

    async def _route_send_event(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        room, event_type, txn = args
        if txn in self._txns:
            return _json(200, {"event_id": self._txns[txn]})
        body = self._body(request)
        self.sent.append(SentEvent(room, event_type, body, txn))
        chat = self.chat_of(room)
        try:
            event_id = await self._deliver(chat, event_type, body)
        except FakePlatformError as exc:
            return _failure(exc, request)
        self._txns[txn] = event_id
        return _json(200, {"event_id": event_id})

    async def _deliver(self, chat: str, event_type: str, body: dict[str, Any]) -> str:
        relates = body.get("m.relates_to") or {}
        if event_type == "m.reaction":
            await self.platform.react(chat, str(relates.get("event_id")), str(relates.get("key")))
            return f"$reaction{next(self._ids)}"
        if event_type != "m.room.message":
            return f"$other{next(self._ids)}"
        if relates.get("rel_type") == "m.replace":
            fresh = body.get("m.new_content") or {}
            await self.platform.edit(chat, str(relates.get("event_id")), str(fresh.get("body", "")))
            return f"$edit{next(self._ids)}"
        thread = str(relates["event_id"]) if relates.get("rel_type") == "m.thread" else None
        reply = (relates.get("m.in_reply_to") or {}).get("event_id")
        if thread is not None and relates.get("is_falling_back") is True:
            reply = None
        msgtype = str(body.get("msgtype") or "")
        kinds = {"m.image": "photo", "m.video": "video", "m.audio": "voice", "m.file": "document"}
        if msgtype in kinds:
            info = body.get("info") or {}
            name = str(body.get("filename") or body.get("body") or "")
            caption = str(body.get("body") or "") if body.get("filename") else ""
            media = FakeMedia(
                kinds[msgtype],  # pyright: ignore[reportArgumentType]
                str(body.get("url") or name),
                mime=str(info.get("mimetype") or ""),
                caption=caption,
            )
            sent = await self.platform.upload(chat, media, reply_to=reply, thread_id=thread)
            return sent.message_id
        sent = await self.platform.post(
            chat,
            str(body.get("body") or ""),
            reply_to=reply,
            thread_id=thread,
            extra={"content": body},
        )
        return sent.message_id

    async def _route_redact(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        room, event_id, _txn = args
        try:
            await self.platform.delete(self.chat_of(room), event_id)
        except FakePlatformError as exc:
            return _failure(exc, request)
        return _json(200, {"event_id": f"$redaction{next(self._ids)}"})

    async def _route_set_typing(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        room, _user = args
        chat = self.chat_of(room)
        on = self._body(request).get("typing") is True
        self.typing.append((room, on))
        try:
            if on:
                await self.platform.typing(chat)
            else:
                await self.platform.typing_stop(chat)
        except FakePlatformError as exc:
            return _failure(exc, request)
        return _json(200, {})

    async def _route_account_data(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        user, kind = args
        if user != self._who(request)[0]:
            return _matrix_error(403, "M_FORBIDDEN", "Cannot read another user's account data")
        if user != self.bot_user:
            content = self.user_data.get((user, kind))
        else:
            content = self.direct_map() if kind == "m.direct" else self.account_data.get(kind)
        if not content:
            return _matrix_error(404, "M_NOT_FOUND", "Account data not found")
        return _json(200, content)

    async def _route_set_account_data(
        self, request: httpx.Request, args: list[str]
    ) -> httpx.Response:
        user, kind = args
        if user != self._who(request)[0]:
            return _matrix_error(403, "M_FORBIDDEN", "Cannot write another user's account data")
        if user != self.bot_user:
            self.user_data[(user, kind)] = self._body(request)
        else:
            self.account_data[kind] = self._body(request)
        return _json(200, {})

    async def _route_resolve_alias(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        (alias,) = args
        room = self.aliases.get(alias)
        if room is None:
            return _matrix_error(404, "M_NOT_FOUND", "Room alias not found")
        return _json(200, {"room_id": room, "servers": [SERVER]})

    async def _route_join(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        (target,) = args
        room = self.aliases.get(target, target)
        self.joined.append(room)
        return _json(200, {"room_id": room})

    async def _route_room_state(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        room, event_type, _key = args
        if event_type == "m.room.encryption":
            found = self.room_encryption.get(room)
            if found is None:
                return _matrix_error(404, "M_NOT_FOUND", "Event not found")
            return _json(200, found)
        try:
            chat = await self.platform.chat(self.chat_of(room))
        except FakePlatformError:
            return _matrix_error(403, "M_FORBIDDEN", "You are not in this room")
        if event_type == "m.room.name" and chat.name and chat.name != chat.chat_id:
            return _json(200, {"name": chat.name})
        return _matrix_error(404, "M_NOT_FOUND", "Event not found")

    async def _route_joined_members(
        self, request: httpx.Request, args: list[str]
    ) -> httpx.Response:
        (room,) = args
        users = self.members.get(room)
        if users is None:
            chat = self.chat_of(room)
            users = [self.bot_user] + ([chat] if chat.startswith("@") else [])
        return _json(200, {"joined": {user: {} for user in users}})

    # -- the keys API (P16-07) --------------------------------------------------------------------

    async def _route_keys_upload(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        user, device = self._who(request)
        return _json(200, self.keys.upload(user, device, self._body(request)))

    async def _route_keys_query(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        return _json(200, self.keys.query(self._who(request)[0], self._body(request)))

    async def _route_keys_claim(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        return _json(200, self.keys.claim(self._body(request)))

    async def _route_send_to_device(
        self, request: httpx.Request, args: list[str]
    ) -> httpx.Response:
        event_type, _txn = args
        messages = self._body(request).get("messages") or {}
        self.keys.send_to_device(self._who(request)[0], event_type, messages)
        self.wake()
        return _json(200, {})

    async def _route_device_signing_upload(
        self, request: httpx.Request, args: list[str]
    ) -> httpx.Response:
        self.keys.set_cross_signing(self._who(request)[0], self._body(request))
        return _json(200, {})

    async def _route_signatures_upload(
        self, request: httpx.Request, args: list[str]
    ) -> httpx.Response:
        self.keys.add_signatures(self._body(request))
        return _json(200, {"failures": {}})

    async def _route_upload(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        media_id = f"up{next(self._ids)}"
        self.media[media_id] = (request.content, request.headers.get("Content-Type", ""))
        return _json(200, {"content_uri": f"mxc://{SERVER}/{media_id}"})

    async def _serve_media(self, args: list[str]) -> httpx.Response:
        _server, media_id = args
        found = self.media.get(media_id)
        if found is None:
            return _matrix_error(404, "M_NOT_FOUND", "Media not found")
        data, mime = found
        return httpx.Response(200, content=data, headers={"Content-Type": mime or "x/unknown"})

    async def _route_download(self, request: httpx.Request, args: list[str]) -> httpx.Response:
        if self.legacy_media_only:
            return _matrix_error(404, "M_UNRECOGNIZED", "Unrecognized request")
        return await self._serve_media(args)

    async def _route_download_legacy(
        self, request: httpx.Request, args: list[str]
    ) -> httpx.Response:
        return await self._serve_media(args)


__all__ = [
    "BOT",
    "HOMESERVER",
    "KEY_ROUTES",
    "ROUTES",
    "KeyServer",
    "SERVER",
    "MatrixWire",
    "SentEvent",
    "fake_token",
    "utf8_len",
]
