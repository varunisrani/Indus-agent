"""Telegram updates → `InboundEvent`s, once each, a bounded number at a time (P15-33).

- ``UpdateReceipts``: an update is handled once. Telegram re-sends an update whose
  acknowledgement was lost (and, after a restart, whatever was not acknowledged), so the ids of
  recently handled updates are kept — at most 4,096, none older than 24 hours (Telegram keeps an
  unconfirmed update for at most a day) — in memory and in a small file per bot under the profile.
- ``UpdateDispatcher``: at most 32 updates are handled at once; updates of one chat are handled in
  arrival order, so a slow turn in one chat never holds up another.
- ``Normaliser``: one message or button press → one event. Chat types: private → ``dm``, a group
  or plain supergroup → ``group``, a forum supergroup → ``forum`` with the topic as ``thread_id``
  (the General topic is ``"1"``), a channel → ``channel``. A reply anchor in a plain group is not
  a thread. Our own messages and commands addressed to another bot are dropped. Mentions of the
  bot (entity or ``@name`` text), replies to the bot, media (``tg:<file id>[:<name>]`` refs that
  the adapter's fetcher downloads), albums and button presses are carried in the event.
- Command menu helpers: registry names → Telegram command names (``-`` → ``_``) and back.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Final, cast

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.fsio import atomic_write_json
from indus.gateway.adapters.telegram.bot_api import (
    BotCommand,
    CallbackQuery,
    Chat,
    Entity,
    FileRef,
    Message,
    Update,
)
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.service.commands.registry import REGISTRY, CommandRegistry

__all__ = [
    "GENERAL_TOPIC",
    "MAX_CONCURRENT_UPDATES",
    "RECEIPT_CAP",
    "RECEIPT_TTL_S",
    "Normaliser",
    "UpdateDispatcher",
    "UpdateReceipts",
    "attachment_ref",
    "chat_key",
    "menu_commands",
    "parse_ref",
]

log = logging.getLogger(__name__)

MAX_CONCURRENT_UPDATES: Final = 32
RECEIPT_CAP: Final = 4096
RECEIPT_TTL_S: Final = 24 * 60 * 60
# Telegram addresses a forum's General topic as thread 1; messages there carry no thread id.
GENERAL_TOPIC: Final = "1"
REF_PREFIX: Final = "tg:"
# setMyCommands limits: 100 commands, names [a-z0-9_]{1,32}, descriptions 1-256 characters.
MENU_MAX: Final = 100
MENU_DESCRIPTION_MAX: Final = 256
# Telegram also refuses a menu whose whole payload is too large (about 4 KB, not documented), so
# the names and descriptions together stay under this many characters.
MENU_TEXT_BUDGET: Final = 3500
_MENU_NAME = re.compile(r"^[a-z0-9_]{1,32}$")
_NAME_SAFE = re.compile(r"[^\w.\- ]")


# -- receipts --------------------------------------------------------------------------------------


class UpdateReceipts:
    """Recently handled update ids of one bot (see the module notes)."""

    def __init__(
        self,
        clock: Clock,
        *,
        path_for: Callable[[int], Path] | None = None,
        cap: int = RECEIPT_CAP,
        ttl: float = RECEIPT_TTL_S,
    ) -> None:
        self.clock = clock
        self._path_for = path_for
        self._path: Path | None = None
        self.cap = cap
        self.ttl = ttl
        self._done: OrderedDict[int, float] = OrderedDict()
        self._busy: set[int] = set()
        self._bot_id: int | None = None
        self._dirty = False
        self._saver: asyncio.Task[None] | None = None

    def _now(self) -> float:
        return self.clock.now().timestamp()

    async def bind(self, bot_id: int) -> None:
        """Use the receipt file of ``bot_id`` (loaded once per bot)."""
        if self._bot_id == bot_id:
            return
        self._bot_id = bot_id
        self._done.clear()
        self._path = self._path_for(bot_id) if self._path_for is not None else None
        if self._path is None:
            return
        stored = await run_blocking(_load_receipts, self._path, label="telegram receipts")
        cutoff = self._now() - self.ttl
        for update_id, at in sorted(stored.items(), key=lambda item: item[1]):
            if at > cutoff:
                self._done[update_id] = at
        self._trim()

    def _trim(self) -> None:
        cutoff = self._now() - self.ttl
        while self._done:
            oldest, at = next(iter(self._done.items()))
            if len(self._done) <= self.cap and at > cutoff:
                break
            del self._done[oldest]

    def claim(self, update_id: int) -> bool:
        """True the first time an update id is seen (handled, being handled or neither)."""
        self._trim()
        if update_id in self._done or update_id in self._busy:
            return False
        self._busy.add(update_id)
        return True

    def done(self, update_id: int) -> None:
        self._busy.discard(update_id)
        self._done[update_id] = self._now()
        self._done.move_to_end(update_id)
        self._trim()
        self._dirty = True
        if self._path is not None and (self._saver is None or self._saver.done()):
            self._saver = asyncio.ensure_future(self._save_loop())

    def __contains__(self, update_id: object) -> bool:
        return update_id in self._done or update_id in self._busy

    def __len__(self) -> int:
        return len(self._done)

    async def _save_loop(self) -> None:
        while self._dirty and self._path is not None:
            self._dirty = False
            snapshot = {str(k): v for k, v in self._done.items()}
            try:
                await run_blocking(_save_receipts, self._path, snapshot, label="telegram receipts")
            except Exception as exc:
                log.warning("telegram: update receipts not saved: %s", type(exc).__name__)
                return

    async def flush(self) -> None:
        saver = self._saver
        if saver is not None:
            with contextlib.suppress(Exception):
                await saver
        if self._dirty and self._path is not None:
            await self._save_loop()


def _load_receipts(path: Path) -> dict[int, float]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("telegram: update receipts unreadable, starting empty: %s", type(exc).__name__)
        return {}
    ids = cast(dict[str, object], raw).get("update_ids") if isinstance(raw, dict) else None
    if not isinstance(ids, dict):
        return {}
    found: dict[int, float] = {}
    for key, at in cast(dict[object, object], ids).items():
        text = str(key)
        if text.lstrip("-").isdigit() and isinstance(at, int | float) and not isinstance(at, bool):
            found[int(text)] = float(at)
    return found


def _save_receipts(path: Path, ids: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, {"update_ids": ids}, indent=None, mode=0o600)


# -- dispatch --------------------------------------------------------------------------------------


def chat_key(update: Update) -> int | None:
    """The chat an update belongs to (None: no chat, runs unordered)."""
    message = update.message or update.channel_post or update.edited_message
    if message is not None:
        return message.chat.id
    query = update.callback_query
    if query is not None and query.message is not None:
        return query.message.chat.id
    return None


class UpdateDispatcher:
    """Runs ``handle(update)`` for each submitted update: at most ``limit`` at once, in order
    within one chat. ``handle`` must not raise (failures are logged here if it does)."""

    def __init__(
        self,
        handle: Callable[[Update], Awaitable[None]],
        *,
        limit: int = MAX_CONCURRENT_UPDATES,
    ) -> None:
        self._handle = handle
        self._gate = asyncio.Semaphore(max(1, limit))
        self._tails: dict[int, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def submit(self, update: Update, *, after: Callable[[], None] | None = None) -> None:
        key = chat_key(update)
        before = self._tails.get(key) if key is not None else None
        task = asyncio.ensure_future(self._run(update, before, after))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if key is not None:
            self._tails[key] = task

            def forget(done: asyncio.Task[None], key: int = key) -> None:
                if self._tails.get(key) is done:
                    del self._tails[key]

            task.add_done_callback(forget)

    async def _run(
        self,
        update: Update,
        before: asyncio.Task[None] | None,
        after: Callable[[], None] | None,
    ) -> None:
        try:
            if before is not None and not before.done():
                await asyncio.wait({before})
            async with self._gate:
                await self._handle(update)
        except asyncio.CancelledError:
            raise  # not handled: the receipt is not written
        except Exception:
            log.exception("telegram: handling update %s failed", update.update_id)
        if after is not None:
            after()

    @property
    def active(self) -> int:
        return len(self._tasks)

    async def idle(self) -> None:
        """Wait until every submitted update was handled."""
        while self._tasks:
            await asyncio.wait(set(self._tasks))

    async def aclose(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tails.clear()


# -- refs and names --------------------------------------------------------------------------------


def attachment_ref(file: FileRef, name: str | None = None) -> str:
    """``tg:<file id>[:<name>]``: no ``/``, so the intake never mistakes it for a local path."""
    label = _NAME_SAFE.sub("_", (name or "").replace("/", "_").replace("\\", "_")).strip()[:80]
    return f"{REF_PREFIX}{file.file_id}" + (f":{label}" if label else "")


def parse_ref(ref: str) -> str | None:
    """The file id of a ``tg:`` ref, else None."""
    if not ref.startswith(REF_PREFIX):
        return None
    file_id = ref[len(REF_PREFIX) :].split(":", 1)[0]
    return file_id or None


def menu_name(name: str) -> str | None:
    """A registry command name as Telegram accepts it, or None when it cannot be one."""
    candidate = name.lower().replace("-", "_")
    return candidate if _MENU_NAME.match(candidate) else None


def menu_commands(
    registry: CommandRegistry = REGISTRY, *, budget: int = MENU_TEXT_BUDGET
) -> list[BotCommand]:
    """The gateway's slash commands for ``setMyCommands`` (registry order, at most 100, names and
    descriptions within ``budget`` characters; the rest still work when typed)."""
    rows: list[BotCommand] = []
    seen: set[str] = set()
    used = 0
    for cmd in registry.commands("gateway"):
        name = menu_name(cmd.name)
        if name is None or name in seen:
            continue
        description = " ".join(registry.describe(cmd).split())[:MENU_DESCRIPTION_MAX] or name
        used += len(name) + len(description)
        if used > budget:
            break
        rows.append(BotCommand(name, description))
        seen.add(name)
        if len(rows) >= MENU_MAX:
            break
    return rows


def _canonical_command_text(text: str, registry: CommandRegistry = REGISTRY) -> str:
    """``/reload_mcp args`` → ``/reload-mcp args`` when only the hyphen form is a command."""
    if not text.startswith("/"):
        return text
    head, sep, rest = text.partition(" ")
    word, at, bot = head[1:].partition("@")
    if "_" not in word or registry.resolve(word) is not None:
        return text
    hyphen = word.replace("_", "-")
    if registry.resolve(hyphen) is None:
        return text
    return f"/{hyphen}{at}{bot}{sep}{rest}"


def _utf16_slice(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2 : (offset + length) * 2].decode("utf-16-le", errors="ignore")


# -- normalising -----------------------------------------------------------------------------------


def _chat_type(chat: Chat) -> ChatType:
    if chat.type == "private":
        return "dm"
    if chat.type == "channel":
        return "channel"
    return "forum" if chat.is_forum else "group"


def _topic(message: Message) -> str | None:
    """The routable thread of a message: a forum topic (General = 1); never a reply anchor."""
    if not message.chat.is_forum or message.chat.type not in ("group", "supergroup"):
        return None
    if message.message_thread_id is not None and message.is_topic_message:
        return str(message.message_thread_id)
    return GENERAL_TOPIC


class Normaliser:
    """Builds events for one bot (its id and username are learned at connect)."""

    def __init__(self, platform: str = "telegram") -> None:
        self.platform = platform
        self.bot_id: int | None = None
        self.username: str = ""

    def identify(self, bot_id: int, username: str | None) -> None:
        self.bot_id = bot_id
        self.username = (username or "").lstrip("@").lower()

    # -- pieces ------------------------------------------------------------------------------------

    def _source(self, message: Message, sender_override: Any = None) -> Source:
        chat = message.chat
        chat_type = _chat_type(chat)
        user = sender_override if sender_override is not None else message.from_user
        if user is not None:
            user_id: str | None = str(user.id)
            user_name: str | None = user.full_name or None
            is_bot = bool(user.is_bot)
        elif message.sender_chat is not None:
            user_id = str(message.sender_chat.id)
            user_name = message.sender_chat.display_name
            is_bot = False
        else:
            user_id = str(chat.id) if chat_type in ("dm", "channel") else None
            user_name = chat.display_name if chat_type == "channel" else None
            is_bot = False
        return Source(
            platform=self.platform,
            chat_id=str(chat.id),
            chat_type=chat_type,
            chat_name=chat.display_name,
            user_id=user_id,
            user_name=user_name,
            thread_id=_topic(message),
            is_bot=is_bot,
        )

    def _own(self, message: Message | None) -> bool:
        user = message.from_user if message is not None else None
        return user is not None and self.bot_id is not None and user.id == self.bot_id

    def mentions_bot(self, message: Message) -> bool:
        text = message.text if message.text is not None else (message.caption or "")
        entities: Sequence[Entity] = message.entities or message.caption_entities
        handle = f"@{self.username}" if self.username else ""
        if handle and text.startswith("/") and text.split(None, 1)[0].lower().endswith(handle):
            return True  # ``/command@ourbot``
        for entity in entities:
            user = entity.user
            if entity.type == "text_mention" and user is not None and user.id == self.bot_id:
                return True
            if entity.type in ("mention", "bot_command") and handle:
                piece = _utf16_slice(text, entity.offset, entity.length).lower()
                if piece == handle or piece.endswith(handle):
                    return True
        if not entities and handle:
            return re.search(rf"(?<![\w@]){re.escape(handle)}\b", text, re.IGNORECASE) is not None
        return False

    def _addressed_elsewhere(self, text: str) -> bool:
        """A ``/command@otherbot`` (only someone else's bot should answer it)."""
        if not text.startswith("/"):
            return False
        head = text.split(None, 1)[0]
        _, at, bot = head.partition("@")
        return bool(at) and bool(self.username) and bot.lower() != self.username

    @staticmethod
    def _media(message: Message) -> tuple[MessageKind, tuple[Attachment, ...], dict[str, Any]]:
        extra: dict[str, Any] = {}
        if message.photo:
            biggest = message.photo[-1]
            return (
                MessageKind.PHOTO,
                (Attachment(ref=attachment_ref(biggest), mime="image/jpeg"),),
                extra,
            )
        if message.voice is not None:
            if message.voice.duration is not None:
                extra["duration"] = message.voice.duration
            mime = message.voice.mime_type or "audio/ogg"
            return (
                MessageKind.VOICE,
                (Attachment(ref=attachment_ref(message.voice), mime=mime),),
                extra,
            )
        if message.audio is not None:
            if message.audio.duration is not None:
                extra["duration"] = message.audio.duration
            ref = attachment_ref(message.audio, message.audio.file_name)
            mime = message.audio.mime_type or "audio/mpeg"
            return MessageKind.AUDIO, (Attachment(ref=ref, mime=mime),), extra
        for video in (message.video, message.video_note, message.animation):
            if video is not None:
                ref = attachment_ref(video, video.file_name)
                return (
                    MessageKind.VIDEO,
                    (Attachment(ref=ref, mime=video.mime_type or "video/mp4"),),
                    extra,
                )
        if message.document is not None:
            doc = message.document
            ref = attachment_ref(doc, doc.file_name)
            return MessageKind.DOCUMENT, (Attachment(ref=ref, mime=doc.mime_type or ""),), extra
        if message.sticker is not None:
            return MessageKind.STICKER, (), extra
        if message.location is not None:
            return MessageKind.LOCATION, (), extra
        return MessageKind.TEXT, (), extra

    # -- events ------------------------------------------------------------------------------------

    def message_event(self, update_id: int, message: Message) -> InboundEvent | None:
        """The event for a message or channel post; None for one we must not act on."""
        if self._own(message):
            return None
        raw = message.text if message.text is not None else (message.caption or "")
        if self._addressed_elsewhere(raw):
            return None
        text = _canonical_command_text(raw)
        kind, media, metadata = self._media(message)
        if kind is MessageKind.STICKER and not text and message.sticker is not None:
            text = message.sticker.emoji or ""
        if kind is MessageKind.LOCATION and not text and message.location is not None:
            lat, lon = message.location
            text = f"[The person shared a location: {lat:.6f}, {lon:.6f}]"
        if self.mentions_bot(message):
            metadata["mentioned"] = True
        if message.media_group_id:
            metadata["media_group_id"] = message.media_group_id
        reply = message.reply_to_message
        reply_text = None
        if reply is not None:
            reply_text = message.quote_text or reply.text or reply.caption
        author = reply.from_user if reply is not None else None
        return InboundEvent(
            source=self._source(message),
            text=text,
            kind=kind,
            message_id=str(message.message_id),
            update_id=update_id,
            media=media,
            reply_to_id=str(reply.message_id) if reply is not None else None,
            reply_to_text=reply_text,
            reply_to_author_id=str(author.id) if author is not None else None,
            reply_to_author_name=(author.full_name or None) if author is not None else None,
            reply_to_is_own=self._own(reply),
            metadata=metadata,
            timestamp=message.date,
        )

    def button_event(self, update_id: int, query: CallbackQuery) -> InboundEvent | None:
        """A press of one of our inline buttons, as the presser's event in that chat."""
        message = query.message
        if not query.data or message is None:
            return None
        return InboundEvent(
            source=self._source(message, sender_override=query.from_user),
            text="",
            message_id=str(message.message_id),
            update_id=update_id,
            metadata={"button": query.data, "callback_id": query.id},
        )
