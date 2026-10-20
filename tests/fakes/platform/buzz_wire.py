"""A fake Buzz community relay over the shared `FakeChatPlatform` (P16-29).

``BuzzRelay.open`` is the adapter's socket opener (opening connects the platform, an armed connect
failure refuses the socket; closing disconnects it). The relay speaks NIP-01 frames: ``REQ`` →
``EVENT``… ``EOSE``, ``EVENT`` → ``OK``, ``AUTH`` (NIP-42: with ``require_auth`` it challenges on
open and closes requests with ``auth-required:`` until answered), ``CLOSE``.

Stored and derived events: chat messages (kind 9, ``h`` tag = conversation) from the platform's
inbound items and from our publishes; profiles (kind 0, with the NIP-24 ``bot`` flag); members
(kind 39002) and metadata (kind 39000) for every channel and DM the platform knows — a DM is a
conversation named ``DM`` with exactly the user and the bot, and a member list for any pubkey
asked about is made on the spot (every member can be DMed). Channels are platform chat ids; a DM's
platform chat id is the user's pubkey and its conversation id ``dm-<pubkey prefix>``.

Signatures use `FakeNostrCrypto` (keyed hashes, no curve) unless a real `NostrCrypto` is given.
Publishing an event re-keys the platform message to the event id, so the contract's id checks find
it. Armed send failures: a connect failure makes the write raise (nothing sent), a read timeout
leaves the event unanswered, anything else answers ``OK false`` with a NIP-01 prefix.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from indus.gateway.adapters.buzz.crypto import NostrCrypto, NostrKeys, signed_event, verify_event
from tests.fakes.platform.core import FailureKind, FakeChatPlatform, FakeInbound, FakePlatformError

RELAY_URL = "https://community.buzz.test"
MAX_LEN = 16000
# Built at runtime: never a credential-shaped literal in the source.
BOT_KEY = "1f" * 32
RELAY_KEY = "2e" * 32

_PREFIX = {
    FailureKind.RATE_LIMIT: "rate-limited",
    FailureKind.FORBIDDEN: "blocked",
}


class FakeNostrCrypto:
    """A test stand-in for BIP-340: public keys and signatures are keyed hashes (no curve maths,
    no security). Signing depends only on the public key, so the fake relay can sign as anyone."""

    def public_key(self, secret: bytes) -> str:
        if len(secret) != 32 or not any(secret):
            raise ValueError("not a valid secret")
        return hashlib.sha256(b"pub" + secret).hexdigest()

    def sign(self, secret: bytes, digest: bytes) -> bytes:
        return self.sign_as(self.public_key(secret), digest)

    @staticmethod
    def sign_as(pubkey: str, digest: bytes) -> bytes:
        head = hashlib.sha256(b"sig" + bytes.fromhex(pubkey) + digest).digest()
        return head + hashlib.sha256(head).digest()

    def verify(self, pubkey: str, digest: bytes, signature: bytes) -> bool:
        try:
            return signature == self.sign_as(pubkey, digest)
        except ValueError:
            return False


def dm_conversation(pubkey: str) -> str:
    return f"dm-{pubkey[:20]}"


class _Closed(Exception):
    pass


class FakeRelaySocket:
    def __init__(self, relay: BuzzRelay) -> None:
        self.relay = relay
        self.frames: asyncio.Queue[str | None] = asyncio.Queue()
        self.authed = False
        self.open = True
        self.challenge = "challenge-" + str(relay.opens)

    async def receive_text(self) -> str:
        frame = await self.frames.get()
        if frame is None:
            self.open = False
            raise _Closed("socket dropped")
        return frame

    async def send_text(self, data: str) -> None:
        if not self.open:
            raise _Closed("socket closed")
        await self.relay.handle(self, json.loads(data))

    def send(self, *message: Any) -> None:
        self.frames.put_nowait(json.dumps(list(message), ensure_ascii=False))


class BuzzRelay:
    def __init__(
        self,
        platform: FakeChatPlatform,
        *,
        crypto: NostrCrypto | None = None,
        require_auth: bool = True,
    ) -> None:
        self.platform = platform
        self.crypto = crypto or FakeNostrCrypto()
        self.bot = NostrKeys.load(self.crypto, BOT_KEY)
        self.relay_keys = NostrKeys.load(self.crypto, RELAY_KEY)
        self.require_auth = require_auth
        self.accept_auth = True
        self.refuse = False
        self.socket: FakeRelaySocket | None = None
        self.opens = 0
        self.chats: list[dict[str, Any]] = []
        self.profiles: dict[str, tuple[str, bool]] = {}
        self.blobs: dict[str, bytes] = {}
        self.requests: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []
        self.auths: list[dict[str, Any]] = []
        self.restricted: set[str] = set()
        self.extra: list[dict[str, Any]] = []
        self.dm_users: dict[str, str] = {}

    # -- the opener -----------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def open(self, url: str) -> AsyncIterator[FakeRelaySocket]:
        if self.refuse:
            raise ConnectionRefusedError("connection refused")
        try:
            await self.platform.connect()
        except FakePlatformError as exc:
            raise ConnectionRefusedError(str(exc)) from None
        self.opens += 1
        socket = FakeRelaySocket(self)
        self.socket = socket
        if self.require_auth:
            socket.send("AUTH", socket.challenge)
        try:
            yield socket
        finally:
            socket.open = False
            if self.socket is socket:
                self.socket = None
            await self.platform.disconnect()

    def drop(self) -> None:
        if self.socket is not None:
            self.socket.frames.put_nowait(None)

    # -- signing as others ----------------------------------------------------------------------

    def sign_as(
        self, pubkey: str, kind: int, tags: list[list[str]], content: str, created_at: int
    ) -> dict[str, Any]:
        from indus.gateway.adapters.buzz.crypto import event_id

        ident = event_id(pubkey, created_at, kind, tags, content)
        if isinstance(self.crypto, FakeNostrCrypto):
            sig = self.crypto.sign_as(pubkey, bytes.fromhex(ident))
        else:
            raise RuntimeError("only the fake crypto can sign as someone else")
        return {
            "id": ident,
            "pubkey": pubkey,
            "created_at": created_at,
            "kind": kind,
            "tags": tags,
            "content": content,
            "sig": sig.hex(),
        }

    def now(self) -> int:
        return int(self.platform.clock.now().timestamp())

    # -- conversations --------------------------------------------------------------------------

    def conversation_of(self, chat_id: str, kind: str) -> str:
        return dm_conversation(chat_id) if kind == "dm" else chat_id

    def chat_of(self, conversation: str) -> str:
        for chat in self.platform.chats.values():
            if chat.kind == "dm" and dm_conversation(chat.chat_id) == conversation:
                return chat.chat_id
        return self.dm_users.get(conversation, conversation)

    def _relay_events(self, flt: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Members and metadata of every conversation the platform knows (signed by the relay)."""
        out: list[dict[str, Any]] = []
        asked = [p for p in flt.get("#p", []) if p != self.bot.pubkey]
        convs: dict[str, tuple[set[str], str, str]] = {}
        for chat in self.platform.chats.values():
            if chat.kind == "dm":
                conv = dm_conversation(chat.chat_id)
                convs[conv] = ({self.bot.pubkey, chat.chat_id}, "DM", "")
            else:
                convs[chat.chat_id] = ({self.bot.pubkey}, chat.name, "a channel")
        for pubkey in asked:
            self.dm_users[dm_conversation(pubkey)] = pubkey
            convs.setdefault(dm_conversation(pubkey), ({self.bot.pubkey, pubkey}, "DM", ""))
        stamp = 1_700_000_000
        for conv, (members, name, about) in convs.items():
            members_tags = [["d", conv], *(["p", m] for m in sorted(members))]
            out.append(self._relay_signed(39002, members_tags, stamp))
            meta = [["d", conv], ["name", name], ["about", about]]
            out.append(self._relay_signed(39000, meta, stamp))
        for pubkey, (name, bot) in self.profiles.items():
            content = json.dumps({"name": name, "bot": bot})
            out.append(self.sign_as(pubkey, 0, [], content, stamp))
        bot_profile = json.dumps({"display_name": "Indus"})
        out.append(self.sign_as(self.bot.pubkey, 0, [], bot_profile, stamp))
        return out

    def _relay_signed(self, kind: int, tags: list[list[str]], stamp: int) -> dict[str, Any]:
        return signed_event(
            self.crypto, self.relay_keys, kind=kind, tags=tags, content="", created_at=stamp
        )

    @staticmethod
    def matches(event: Mapping[str, Any], flt: Mapping[str, Any]) -> bool:
        if "kinds" in flt and event["kind"] not in flt["kinds"]:
            return False
        if "authors" in flt and event["pubkey"] not in flt["authors"]:
            return False
        if "since" in flt and event["created_at"] < flt["since"]:
            return False
        for key, wanted in flt.items():
            if key.startswith("#"):
                values = {t[1] for t in event["tags"] if len(t) > 1 and t[0] == key[1:]}
                if not values & set(wanted):
                    return False
        return True

    def query(self, flt: Mapping[str, Any]) -> list[dict[str, Any]]:
        pool = [*self.chats, *self.extra, *self._relay_events(flt)]
        found = [e for e in pool if self.matches(e, flt)]
        found.sort(key=lambda e: e["created_at"], reverse=True)
        limit = flt.get("limit")
        return found[:limit] if isinstance(limit, int) else found

    # -- frames ---------------------------------------------------------------------------------

    async def handle(self, socket: FakeRelaySocket, message: list[Any]) -> None:
        head = message[0]
        if head == "REQ":
            sub, flt = message[1], message[2]
            self.requests.append(flt)
            if self.require_auth and not socket.authed:
                socket.send("CLOSED", sub, "auth-required: say who you are")
                return
            for conv in flt.get("#h", []):
                if conv in self.restricted:
                    socket.send("CLOSED", sub, "restricted: not a channel member")
                    return
            for event in self.query(flt):
                socket.send("EVENT", sub, event)
            socket.send("EOSE", sub)
        elif head == "AUTH":
            event = message[1]
            self.auths.append(event)
            ok = verify_event(self.crypto, event) and self.accept_auth
            challenge = [t[1] for t in event["tags"] if t[0] == "challenge"]
            ok = ok and challenge == [socket.challenge]
            socket.authed = ok
            socket.send("OK", event["id"], ok, "" if ok else "restricted: not a member")
        elif head == "EVENT":
            await self._publish(socket, message[1])

    async def _publish(self, socket: FakeRelaySocket, event: dict[str, Any]) -> None:
        if not verify_event(self.crypto, event):
            socket.send("OK", event.get("id", ""), False, "invalid: bad signature")
            return
        self.published.append(event)
        conv = next((t[1] for t in event["tags"] if t[0] == "h"), "")
        root = next((t[1] for t in event["tags"] if t[0] == "e" and t[3:] == ["root"]), None)
        reply = next((t[1] for t in event["tags"] if t[0] == "e" and t[3:] == ["reply"]), None)
        try:
            sent = await self.platform.post(
                self.chat_of(conv), event["content"], reply_to=reply, thread_id=root
            )
        except FakePlatformError as exc:
            if exc.kind is FailureKind.CONNECT_TIMEOUT:
                raise _Closed("the write did not go through") from None
            if exc.kind is FailureKind.READ_TIMEOUT:
                return
            socket.send("OK", event["id"], False, f"{_PREFIX.get(exc.kind, 'invalid')}: {exc.text}")
            return
        messages = self.platform._messages  # pyright: ignore[reportPrivateUsage]
        messages.pop(sent.message_id, None)
        sent.message_id = event["id"]
        messages[event["id"]] = sent
        self.chats.append(event)
        socket.send("OK", event["id"], True, "")

    # -- inbound --------------------------------------------------------------------------------

    def event_for(self, item: FakeInbound) -> dict[str, Any] | None:
        """The signed kind-9 event for ``item`` (None for shapes Buzz lacks: edits, buttons)."""
        if item.event != "message":
            return None
        conv = self.conversation_of(item.chat_id, item.chat_kind)
        tags: list[list[str]] = [["h", conv]]
        if item.mentions_bot:
            tags.append(["p", self.bot.pubkey])
        if item.thread_id:
            tags.append(["e", item.thread_id, "", "root"])
        if item.reply_to:
            tags.append(["e", item.reply_to, "", "reply"])
        for media in item.media:
            data = b"data-" + media.ref.encode()
            digest = hashlib.sha256(data).hexdigest()
            url = f"{RELAY_URL}/media/{digest}"
            self.blobs[url] = data
            tags.append(
                ["imeta", f"url {url}", f"m {media.mime}", f"x {digest}", f"size {len(data)}"]
            )
        self.profiles[item.user_id] = (item.user_name or "", item.is_bot)
        return self.sign_as(item.user_id, 9, tags, item.text, self.now())

    def store_inbound(self) -> int:
        """Put every inbound item waiting on the platform on the relay; how many were stored."""
        count = 0
        for item in self.platform.drain_inbound():
            event = self.event_for(item)
            if event is not None:
                self.chats.append(event)
                count += 1
        return count

    def media_transport(self) -> httpx.MockTransport:
        def serve(request: httpx.Request) -> httpx.Response:
            data = self.blobs.get(str(request.url))
            if data is None:
                return httpx.Response(404)
            return httpx.Response(200, content=data)

        return httpx.MockTransport(serve)


__all__ = [
    "BOT_KEY",
    "MAX_LEN",
    "RELAY_URL",
    "BuzzRelay",
    "FakeNostrCrypto",
    "FakeRelaySocket",
    "dm_conversation",
]
