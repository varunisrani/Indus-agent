"""The Telegram adapter (P15-33): our Bot API client (``bot_api``) behind the adapter contract.

Connect: ``getMe`` (a refused token is a fatal, non-retryable connect error), then — unless the
open is send-only (``indus send``: nothing else is touched) — the update receipts of this bot are
loaded and updates start flowing: long polling by default (any webhook is deleted first; a cold
boot also drops the server-side backlog unless ``drop_pending_on_cold_boot: false``), or a
webhook when one is configured, and only with its secret. The command menu is registered in the
background (a failure there never fails the connect).

Polling: ``getUpdates`` with a 30 s long-poll; the next call's offset acknowledges what was
received. Failures back off from 1 s to 60 s; a revoked token stops polling (logged). An empty
answer that came back at once (a proxy that does not hold the long poll) is followed by a 1 s
pause so the loop never spins. Every update goes through ``admit``: dropped when its id was
already handled (``inbound.UpdateReceipts``), else queued on the dispatcher (≤32 at a time, in
order per chat) and turned into one event (``inbound.Normaliser``).

Sending: the sender (P15-17) hands us text already in MarkdownV2 (``meta["plain"]`` when it falls
back to plain text), so ``_send`` only picks the parse mode, splits what is over 4,096 UTF-16 units
itself (escaped ``\\(i/n\\)`` markers in MarkdownV2), threads the first piece under ``reply_to``
and puts forum replies in their topic (never thread 1, the General topic, which the API refuses
for sends). A topic that was deleted is retried once in the chat itself. Failures are classified
by the base; a flood wait comes back typed with its ``retry_after`` and is never slept here (the
retry policy sleeps at most ``inline_wait_cap`` = 5 s and hands longer waits to the ledger).

Edits: "message is not modified" is success; a markup refusal is retried once as plain text (cards
are edited with plain Markdown by the prompts module); an interim edit over the cap shows its
first piece; a final edit over the cap edits the first piece in and sends the rest as replies.

P16 additions, each behind its setting: native drafts (``open_stream``, ``drafts.py``; on with
``display.platforms.telegram.native_stream``), DM topics (``topics.py``; ``dm_topics``) and sticker
descriptions (``stickers.py``; ``sticker_descriptions``, on by default).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.gateway.adapters.base import (
    REPLY_TO_KEY,
    Adapter,
    AdapterConnectError,
    ApprovalPrompt,
    Capabilities,
    ClarifyPrompt,
    FetcherSeat,
    InboundSink,
    MediaKind,
    approval_callback,
    clarify_callback,
)
from indus.gateway.adapters.telegram.bot_api import (
    BOT_API_METHODS,
    MAX_DOWNLOAD_BYTES,
    MAX_UPLOAD_BYTES,
    BadRequest,
    BotApi,
    BotCommand,
    FileRef,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Message,
    RetryAfter,
    Unauthorized,
    Update,
    User,
)
from indus.gateway.adapters.telegram.drafts import DraftStream
from indus.gateway.adapters.telegram.inbound import (
    GENERAL_TOPIC,
    MAX_CONCURRENT_UPDATES,
    Normaliser,
    UpdateDispatcher,
    UpdateReceipts,
    attachment_ref,
    menu_commands,
    parse_ref,
)
from indus.gateway.adapters.telegram.stickers import StickerCache, StickerNotes
from indus.gateway.adapters.telegram.topics import DmTopics
from indus.gateway.adapters.telegram.webhook import (
    UvicornServer,
    WebhookOptions,
    WebhookServer,
    webhook_app,
)
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind, SendResult, classify_send_error, is_ambiguous_timeout
from indus.gateway.outbound.chunker import (
    Chunked,
    plain_marker,
    prefix_within,
    split_message,
    utf16_units,
)
from indus.gateway.outbound.formatting import markdownv2_marker, markdownv2_to_plain

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "ALLOWED_UPDATES",
    "BOT_TARGET",
    "LOBBY_TEXT",
    "TelegramAdapter",
    "TelegramOptions",
]

log = logging.getLogger(__name__)

BOT_TARGET: Final = "indus.gateway.adapters.telegram.bot_api:BotApi"
PARSE_MODE: Final = "MarkdownV2"
MAX_LEN: Final = 4096
CAPTION_MAX: Final = 1024
PHOTO_MAX_BYTES: Final = 10 * 1024 * 1024
ALLOWED_UPDATES: Final = ("message", "channel_post", "callback_query")
MENU_SCOPES: Final = ("default", "all_private_chats", "all_group_chats")
POLL_TIMEOUT_S: Final = 30
QUICK_EMPTY_POLL_S: Final = 1.0
IDLE_PAUSE_S: Final = 1.0
BACKOFF_START_S: Final = 1.0
BACKOFF_MAX_S: Final = 60.0
LOBBY_COOLDOWN_S: Final = 30.0
LOBBY_TEXT: Final = (
    "This group talks to me in topics. Open a topic (or start a new one) and write to me there."
)
VOICE_SUFFIXES: Final = frozenset({".ogg", ".oga", ".opus"})
AUDIO_SUFFIXES: Final = frozenset({".mp3", ".m4a"})
# Forum chats that got the command menu in their own scope (topics do not show the group one).
FORUM_MENU_MAX: Final = 1024
# Interim overflow previews remembered so a saturated preview is not re-sent on every frame.
PREVIEW_MEMORY: Final = 512
REACTION_WORKING: Final = "\N{EYES}"
REACTION_DONE: Final = "\N{THUMBS UP SIGN}"
REACTION_FAILED: Final = "\N{THUMBS DOWN SIGN}"


def _flag(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip().lower() not in ("0", "false", "no", "off")
    return default


def _number(value: object, default: int, *, low: int, high: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(str(value).strip()) if value is not None else default
    except ValueError:
        return default
    return number if low <= number <= high else default


@dataclass(frozen=True)
class TelegramOptions:
    """``platforms.telegram`` settings the adapter reads (see the task note)."""

    poll_timeout: int = POLL_TIMEOUT_S
    max_concurrent_updates: int = MAX_CONCURRENT_UPDATES
    drop_pending_on_cold_boot: bool = True
    reactions: bool = False
    require_topics: bool = False
    command_menu: bool = True
    webhook: WebhookOptions | None = field(default=None)
    # P16-34: topics of private chats as separate sessions (needs the bot's Threaded Mode).
    dm_topics: bool = False
    # P16-34: describe static stickers once (cached by unique file id).
    sticker_descriptions: bool = True
    # P16-33: ``display.platforms.telegram.native_stream`` (read by ``create_adapter``).
    native_stream: bool = False

    @classmethod
    def from_extra(
        cls,
        extra: Mapping[str, Any],
        *,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        native_stream: bool = False,
    ) -> TelegramOptions:
        url = (webhook_url or str(extra.get("webhook_url") or "")).strip()
        webhook = None
        if url:
            webhook = WebhookOptions(
                url=url,
                secret=(webhook_secret or "").strip() or None,
                # Loopback unless a public bind is configured explicitly (D-16.330).
                host=str(extra.get("webhook_host") or "127.0.0.1"),
                port=_number(extra.get("webhook_port"), 8443, low=1, high=65535),
            )
        return cls(
            poll_timeout=_number(extra.get("poll_timeout"), POLL_TIMEOUT_S, low=0, high=50),
            max_concurrent_updates=_number(
                extra.get("max_concurrent_updates"), MAX_CONCURRENT_UPDATES, low=1, high=256
            ),
            drop_pending_on_cold_boot=_flag(extra.get("drop_pending_on_cold_boot"), True),
            reactions=_flag(extra.get("reactions"), False),
            require_topics=_flag(extra.get("require_topics"), False),
            command_menu=_flag(extra.get("command_menu"), True),
            webhook=webhook,
            dm_topics=_flag(extra.get("dm_topics"), False),
            sticker_descriptions=_flag(extra.get("sticker_descriptions"), True),
            native_stream=native_stream,
        )


def _message_id(value: object) -> int | None:
    text = str(value).strip() if value is not None else ""
    return int(text) if text.isdigit() else None


def _send_thread(meta: Mapping[str, Any]) -> int | None:
    """A forum topic to send into; the General topic (1) is addressed by omitting it."""
    raw = meta.get("thread_id")
    text = str(raw).strip() if raw is not None else ""
    if not text.isdigit() or text == GENERAL_TOPIC:
        return None
    return int(text)


def _typing_thread(meta: Mapping[str, Any]) -> int | None:
    """Unlike a send, a chat action needs thread 1 to show in a forum's General topic."""
    raw = meta.get("thread_id")
    text = str(raw).strip() if raw is not None else ""
    return int(text) if text.isdigit() else None


def _caption(text: str | None) -> str | None:
    if not text or not text.strip():
        return None
    return text[: prefix_within(text, CAPTION_MAX, utf16_units)]


def _not_modified(exc: BaseException) -> bool:
    return isinstance(exc, BadRequest) and "not modified" in exc.message.lower()


def _bad_markup(exc: BaseException) -> bool:
    return isinstance(exc, BadRequest) and classify_send_error(exc) is ErrorKind.BAD_FORMAT


def _rows(buttons: Sequence[InlineKeyboardButton], width: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        tuple(tuple(buttons[i : i + width]) for i in range(0, len(buttons), width))
    )


def _file_size(path: Path) -> int:
    return path.stat().st_size


class TelegramAdapter(Adapter):
    platform: ClassVar[str] = "telegram"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_LEN,
        len_fn=utf16_units,
        markup="markdownv2",
        splits_natively=True,
        supports_edit=True,
        supports_delete=True,
        supports_buttons=True,
        slot_spacing=1.0,
        inline_wait_cap=5.0,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        BOT_TARGET: (*BOT_API_METHODS, "download_file", "aclose"),
    }

    def __init__(
        self,
        bot: BotApi,
        *,
        clock: Clock,
        settings: PlatformSettings | None = None,
        secrets: Sequence[str] = (),
        sink: InboundSink | None = None,
        options: TelegramOptions | None = None,
        receipts: UpdateReceipts | None = None,
        server_factory: Callable[[], WebhookServer] | None = None,
        send_only: bool = False,
        stickers: StickerNotes | None = None,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=secrets, sink=sink)
        self.bot = bot
        # ``indus send`` (``AdapterConfig.send_only``): ``getMe`` only, no polling or webhook.
        self.send_only = send_only
        self.options = options or TelegramOptions()
        self.receipts = receipts or UpdateReceipts(clock)
        self.normaliser = Normaliser(self.platform)
        self.me: User | None = None
        # Why polling ended on its own ("auth": the token was revoked), else None.
        self.polling_stopped: str | None = None
        self._server_factory: Callable[[], WebhookServer] = server_factory or (
            lambda: UvicornServer(clock)
        )
        self._server: WebhookServer | None = None
        self._dispatcher: UpdateDispatcher | None = None
        self._poller: asyncio.Task[None] | None = None
        self._housekeeping: asyncio.Task[None] | None = None
        self._lobby_sent: dict[str, float] = {}
        self._forum_menus: set[str] = set()
        self._previews: dict[tuple[str, str], str] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.topics = DmTopics(enabled=self.options.dm_topics, clock=clock)
        self.stickers = stickers or StickerNotes(
            StickerCache(None), clock, enabled=self.options.sticker_descriptions
        )
        # Set once Telegram says drafts cannot work for this bot, or inside DM topics
        # (``drafts_unavailable``).
        self.drafts_off = False
        self.topic_drafts_off = False

    # -- lifecycle ---------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        webhook = self.options.webhook
        if webhook is not None and not self.send_only and not webhook.secret:
            raise AdapterConnectError(
                "Telegram webhook mode needs a secret: set TELEGRAM_WEBHOOK_SECRET (without it "
                "anyone who finds the URL could send the bot forged updates)",
                retryable=False,
                code="webhook_secret_missing",
            )
        try:
            await self._start(is_reconnect, webhook)
        except BaseException:
            # a failed open leaves nothing running and no connection pool behind
            await self._stop_listeners()
            with contextlib.suppress(Exception):
                await self.bot.aclose()
            raise

    async def _start(self, is_reconnect: bool, webhook: WebhookOptions | None) -> None:
        try:
            me = await self.bot.get_me()
        except Unauthorized as exc:
            raise AdapterConnectError(
                f"Telegram refused the bot token ({exc.message}); create a new token with "
                "@BotFather and update TELEGRAM_BOT_TOKEN",
                retryable=False,
                code="telegram_auth",
            ) from None
        self.me = me
        self.normaliser.identify(me.id, me.username)
        self.topics.identify(me)
        if self.send_only:
            return
        await self.receipts.bind(me.id)
        self.polling_stopped = None
        self._dispatcher = UpdateDispatcher(self._handle, limit=self.options.max_concurrent_updates)
        drop = True if (not is_reconnect and self.options.drop_pending_on_cold_boot) else None
        if not is_reconnect:
            log.info(
                "telegram: cold start, %s the updates queued while offline",
                "dropping" if drop else "keeping",
            )
        if webhook is not None:
            await self._start_webhook(webhook, drop)
        else:
            await self.bot.delete_webhook(drop_pending_updates=drop)
            self._poller = asyncio.ensure_future(self._poll())
        if self.options.command_menu:
            self._housekeeping = asyncio.ensure_future(self._register_commands())

    async def _close(self) -> None:
        try:
            await self._stop_listeners()
            await self.receipts.flush()
        finally:
            await self.bot.aclose()

    async def _stop_listeners(self) -> None:
        for task in (self._poller, self._housekeeping, *self._tasks):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._poller = self._housekeeping = None
        server, self._server = self._server, None
        if server is not None:
            with contextlib.suppress(Exception):
                await server.stop()
        dispatcher, self._dispatcher = self._dispatcher, None
        if dispatcher is not None:
            await dispatcher.aclose()

    async def _start_webhook(self, webhook: WebhookOptions, drop: bool | None) -> None:
        assert webhook.secret is not None
        app = webhook_app(webhook.secret, self._pushed, webhook.path)
        server = self._server_factory()
        await server.start(app, webhook.host, webhook.port)
        self._server = server
        await self.bot.set_webhook(
            webhook.url,
            secret_token=webhook.secret,
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=drop,
        )
        log.info("telegram: receiving updates by webhook on %s:%d", webhook.host, webhook.port)

    async def _register_commands(self) -> None:
        commands = menu_commands()
        topic = self.topics.menu_entry()
        for scope in MENU_SCOPES:
            private = topic is not None and scope == "all_private_chats"
            rows = [*commands, topic] if private and topic is not None else commands
            await self._set_menu(rows, {"type": scope})

    async def _set_menu(self, commands: Sequence[BotCommand], scope: Mapping[str, Any]) -> None:
        try:
            await self.bot.set_my_commands(commands, scope=scope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "telegram: command menu not set for %s: %s", scope["type"], self.scrub(str(exc))
            )

    def _forum_menu(self, chat_id: str) -> None:
        """Topics of a forum show only a menu set for that chat: set it once per chat."""
        if not self.options.command_menu or chat_id in self._forum_menus:
            return
        if len(self._forum_menus) >= FORUM_MENU_MAX:
            self._forum_menus.clear()
        self._forum_menus.add(chat_id)
        target = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
        task = asyncio.ensure_future(
            self._set_menu(menu_commands(), {"type": "chat", "chat_id": target})
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- inbound -----------------------------------------------------------------------------------

    async def _poll(self) -> None:
        offset: int | None = None
        backoff = BACKOFF_START_S
        while True:
            started = self.clock.monotonic()
            try:
                updates = await self.bot.get_updates(
                    offset=offset,
                    timeout=self.options.poll_timeout,
                    allowed_updates=ALLOWED_UPDATES,
                )
            except asyncio.CancelledError:
                raise
            except Unauthorized as exc:
                self.polling_stopped = "auth"
                log.error(
                    "telegram: polling stopped, the bot token was refused: %s",
                    self.scrub(str(exc)),
                )
                # Revoked token: retrying cannot help; the daemon parks the platform as fatal.
                self.report_fatal(
                    "telegram_auth",
                    "Telegram refused the bot token while polling; create a new token with "
                    "@BotFather and update TELEGRAM_BOT_TOKEN",
                )
                return
            except Exception as exc:
                log.warning(
                    "telegram: polling failed (%s); retrying in %.0f s",
                    self.scrub(f"{type(exc).__name__}: {exc}"),
                    backoff,
                )
                await self.clock.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                continue
            backoff = BACKOFF_START_S
            for update in updates:
                offset = max(offset or 0, update.update_id + 1)
                self.admit(update)
            if not updates and self.clock.monotonic() - started < QUICK_EMPTY_POLL_S:
                await self.clock.sleep(IDLE_PAUSE_S)

    async def _pushed(self, update: Update) -> None:
        self.admit(update)

    def admit(self, update: Update) -> bool:
        """Queue one update unless its id was already seen; True when queued."""
        dispatcher = self._dispatcher
        if dispatcher is None:
            return False
        if not self.receipts.claim(update.update_id):
            log.debug("telegram: update %s seen before; dropped", update.update_id)
            return False
        update_id = update.update_id
        dispatcher.submit(update, after=lambda: self.receipts.done(update_id))
        return True

    async def idle(self) -> None:
        """Wait until every queued update was handled (tests)."""
        if self._dispatcher is not None:
            await self._dispatcher.idle()

    async def _handle(self, update: Update) -> None:
        query = update.callback_query
        if query is not None:
            try:
                await self.bot.answer_callback_query(query.id)
            except Exception as exc:
                log.debug("telegram: callback answer failed: %s", self.scrub(str(exc)))
            event = self.normaliser.button_event(update.update_id, query)
            if event is not None:
                await self.emit(event)
            return
        message = update.message or update.channel_post
        if message is None:
            return
        event = self.normaliser.message_event(update.update_id, message)
        if event is None:
            return
        event = self.topics.place(event, message)
        if event.source.chat_type == "forum":
            self._forum_menu(event.source.chat_id)
        if await self._lobby(event):
            return
        if await self.topics.handle(self, event):
            return
        if message.sticker is not None and event.kind is MessageKind.STICKER:
            event = await self._sticker_event(event, message.sticker)
        await self.emit(event)

    async def _sticker_event(self, event: InboundEvent, sticker: FileRef) -> InboundEvent:
        """The sticker's note as the event text (``stickers.py``)."""
        source = event.source
        addressed = (
            source.chat_type == "dm"
            or event.metadata.get("mentioned") is True
            or event.reply_to_is_own
        )
        try:
            note = await self.stickers.note_for(
                sticker,
                sender=source.user_id or source.chat_id,
                addressed=addressed,
                download=self._sticker_bytes,
            )
        except Exception:
            log.warning("telegram: sticker note failed", exc_info=True)
            return event
        return event.model_copy(update={"text": note})

    def _sticker_bytes(self, sticker: FileRef) -> AsyncIterator[bytes]:
        return self.fetch_media(Attachment(ref=attachment_ref(sticker), mime="image/webp"))

    async def _lobby(self, event: InboundEvent) -> bool:
        """Under ``require_topics``: plain text in a forum's General topic gets a reminder
        (once per 30 s per chat) instead of a turn. True when the event was taken."""
        source = event.source
        if (
            not self.options.require_topics
            or source.chat_type != "forum"
            or source.thread_id != GENERAL_TOPIC
            or source.is_bot
            or event.is_command
        ):
            return False
        now = self.clock.monotonic()
        last = self._lobby_sent.get(source.chat_id)
        if last is None or now - last >= LOBBY_COOLDOWN_S:
            self._lobby_sent[source.chat_id] = now
            await self.send(source.chat_id, self.format(LOBBY_TEXT), reply_to=event.message_id)
        return True

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register ``fetch_media`` with the media intake (P15-23) and let sticker notes use the
        intake's describer and image cache; returns the undo."""
        undo_fetcher = seat.register_fetcher(self.platform, self.fetch_media)
        undo_stickers = self.stickers.attach(seat)

        def undo() -> None:
            undo_stickers()
            undo_fetcher()

        return undo

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The intake's fetcher for ``tg:`` refs: ``getFile``, then the file download."""
        file_id = parse_ref(attachment.ref)
        if file_id is None:
            raise ValueError("not a Telegram file reference")
        info = await self.bot.get_file(file_id)
        if info.file_size is not None and info.file_size > MAX_DOWNLOAD_BYTES:
            raise BadRequest("Bad Request: file is too big")
        if not info.file_path:
            raise BadRequest("Bad Request: the file has no download path")
        async for chunk in self.bot.download_file(info.file_path):
            yield chunk

    # -- outbound: text ----------------------------------------------------------------------------

    def _failure(self, exc: BaseException, delivered: Sequence[str], remainder: str) -> SendResult:
        log.info("telegram: call failed: %s", self.scrub(f"{type(exc).__name__}: {exc}"))
        wait = exc.retry_after if isinstance(exc, RetryAfter) else None
        resumable = bool(delivered) and bool(remainder) and not is_ambiguous_timeout(exc)
        return SendResult.failure(
            exc=exc,
            retry_after=wait,
            delivered_ids=tuple(delivered),
            remainder=remainder if resumable else None,
        )

    async def _post(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None,
        reply_to: object,
        thread: int | None,
        markup: InlineKeyboardMarkup | None = None,
    ) -> Message:
        """sendMessage; a deleted topic is retried once in the chat itself."""
        anchor = _message_id(reply_to)
        try:
            return await self.bot.send_message(
                chat_id,
                text,
                parse_mode=parse_mode,
                reply_to_message_id=anchor,
                message_thread_id=thread,
                reply_markup=markup,
            )
        except BadRequest as exc:
            if thread is None or "thread not found" not in exc.message.lower():
                raise
        log.info("telegram: topic %s of chat %s is gone; sending to the chat", thread, chat_id)
        return await self.bot.send_message(
            chat_id,
            text,
            parse_mode=parse_mode,
            reply_to_message_id=anchor,
            message_thread_id=None,
            reply_markup=markup,
        )

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        plain = meta.get("plain") is True
        parse_mode = None if plain else PARSE_MODE
        chunked = split_message(
            text,
            MAX_LEN,
            len_fn=utf16_units,
            marker=plain_marker if plain else markdownv2_marker,
        )
        thread = _send_thread(meta)
        ids: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            try:
                sent = await self._post(
                    chat_id,
                    chunk.text,
                    parse_mode=parse_mode,
                    reply_to=reply_to if n == 0 else None,
                    thread=thread,
                )
            except Exception as exc:
                return self._failure(exc, ids, chunked.remainder(n))
            ids.append(str(sent.message_id))
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    async def _edit_text(
        self, chat_id: str, message_id: str, text: str, parse_mode: str | None
    ) -> None:
        try:
            await self.bot.edit_message_text(text, chat_id, int(message_id), parse_mode=parse_mode)
        except Exception as exc:
            if _not_modified(exc):
                return
            if parse_mode is None or not _bad_markup(exc):
                raise
            await self._edit_text(chat_id, message_id, markdownv2_to_plain(text), None)

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        plain = meta.get("plain") is True
        parse_mode = None if plain else PARSE_MODE
        marker = plain_marker if plain else markdownv2_marker
        key = (chat_id, message_id)
        if final:
            self._previews.pop(key, None)
        if utf16_units(text) > MAX_LEN:
            chunked = split_message(text, MAX_LEN, len_fn=utf16_units, marker=marker)
            if final:
                return await self._edit_overflow(chat_id, message_id, chunked, parse_mode, meta)
            text = chunked.chunks[0].text
            if self._previews.get(key) == text:
                return SendResult.sent(message_id)  # the saturated preview already shows this
            if len(self._previews) >= PREVIEW_MEMORY:
                self._previews.pop(next(iter(self._previews)))
            self._previews[key] = text
        elif not final:
            self._previews.pop(key, None)
        try:
            await self._edit_text(chat_id, message_id, text, parse_mode)
        except Exception as exc:
            self._previews.pop(key, None)
            return self._failure(exc, (), "")
        return SendResult.sent(message_id)

    async def _edit_overflow(
        self,
        chat_id: str,
        message_id: str,
        chunked: Chunked,
        parse_mode: str | None,
        meta: Mapping[str, Any],
    ) -> SendResult:
        """A final edit over the cap: the first piece in place, the rest as replies."""
        pieces = chunked.chunks
        try:
            await self._edit_text(chat_id, message_id, pieces[0].text, parse_mode)
        except Exception as exc:
            return self._failure(exc, (), "")
        ids = [message_id]
        thread = _send_thread(meta)
        for n, piece in enumerate(pieces[1:], start=1):
            await self.slots.pace(chat_id)
            try:
                sent = await self._post(
                    chat_id, piece.text, parse_mode=parse_mode, reply_to=ids[-1], thread=thread
                )
            except Exception as exc:
                return self._failure(exc, ids, chunked.remainder(n))
            ids.append(str(sent.message_id))
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    async def _delete(self, chat_id: str, message_id: str) -> bool:
        return await self.bot.delete_message(chat_id, int(message_id))

    def is_final_failure(self, result: SendResult) -> bool:
        """A revoked token: neither a retry nor plain text can help."""
        return not result.ok and "unauthorized" in (result.error or "").lower()

    # -- native drafts (P16-33) --------------------------------------------------------------------

    def open_stream(
        self,
        chat_id: str,
        *,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> DraftStream | None:
        """A draft stream for one reply segment in a private chat, or None (drafts off, not a
        private chat, not connected): the caller then streams by edits."""
        if (
            not self.options.native_stream
            or self.drafts_off
            or self.send_only
            or not self.connected
            or not str(chat_id).isdigit()  # private chats have positive ids
        ):
            return None
        thread = _send_thread(meta or {})
        if thread is not None and self.topic_drafts_off:
            return None
        return DraftStream(self, str(chat_id), thread=thread, reply_to=reply_to)

    def drafts_unavailable(self, reason: str, *, in_topics: bool = False) -> None:
        """Telegram said drafts cannot work here (``in_topics``: inside DM topics only): stream
        by edits there from now on."""
        if in_topics:
            if not self.topic_drafts_off:
                self.topic_drafts_off = True
                log.warning("telegram: native drafts off inside topics (%s)", reason)
            return
        if not self.drafts_off:
            self.drafts_off = True
            log.warning("telegram: native drafts switched off (%s); streaming by edits", reason)

    # -- typing and reactions ----------------------------------------------------------------------

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        await self.bot.send_chat_action(chat_id, "typing", message_thread_id=_typing_thread(meta))

    async def _react(self, chat_id: str, message_id: str | None, emoji: str | None) -> bool:
        if not self.options.reactions or _message_id(message_id) is None:
            return False
        try:
            return await self.bot.set_message_reaction(
                chat_id, int(str(message_id)), reaction=emoji
            )
        except Exception as exc:
            log.debug("telegram: reaction failed: %s", self.scrub(str(exc)))
            return False

    async def processing_started(self, chat_id: str, message_id: str | None) -> bool:
        """Mark a message as being worked on (``platforms.telegram.reactions: true``)."""
        return await self._react(chat_id, message_id, REACTION_WORKING)

    async def processing_finished(
        self, chat_id: str, message_id: str | None, *, ok: bool | None
    ) -> bool:
        """Done (ok), failed (not ok) or cleared (None, e.g. a cancelled turn)."""
        emoji = REACTION_DONE if ok else REACTION_FAILED if ok is False else None
        return await self._react(chat_id, message_id, emoji)

    # -- cards -------------------------------------------------------------------------------------

    async def _post_card(
        self, chat_id: str, text: str, markup: InlineKeyboardMarkup, meta: Mapping[str, Any]
    ) -> SendResult:
        thread = _send_thread(meta)
        try:
            try:
                sent = await self._post(
                    chat_id,
                    self.format(text),
                    parse_mode=PARSE_MODE,
                    reply_to=None,
                    thread=thread,
                    markup=markup,
                )
            except Exception as exc:
                if not _bad_markup(exc):
                    raise
                sent = await self._post(
                    chat_id, text, parse_mode=None, reply_to=None, thread=thread, markup=markup
                )
        except Exception as exc:
            return self._failure(exc, (), "")
        return SendResult.sent(str(sent.message_id))

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        buttons = [
            InlineKeyboardButton(a.label, approval_callback(prompt.request_id, a.choice))
            for a in prompt.actions
        ]
        return await self._post_card(chat_id, prompt.text, _rows(buttons, 2), meta)

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        buttons = [
            InlineKeyboardButton(choice[:60], clarify_callback(prompt.clarify_id, n))
            for n, choice in enumerate(prompt.choices)
        ]
        return await self._post_card(chat_id, prompt.question, _rows(buttons, 1), meta)

    # -- media -------------------------------------------------------------------------------------

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        remote = path.startswith(("https://", "http://"))
        source: InputFile | str
        size: int | None = None
        if remote:
            source = path
        else:
            local = Path(path)
            try:
                size = await run_blocking(_file_size, local, label="telegram upload")
            except OSError as exc:
                return SendResult.failure(f"could not read {local.name}: {type(exc).__name__}")
            if size > MAX_UPLOAD_BYTES:
                return SendResult.failure(
                    f"{local.name} is larger than Telegram's 50 MB upload limit"
                )
            source = InputFile(local, local.name, size)
        send = {
            "image": self.bot.send_photo,
            "voice": self.bot.send_voice,
            "video": self.bot.send_video,
            "file": self.bot.send_document,
            "document": self.bot.send_document,
        }[kind]
        if kind == "image" and size is not None and size > PHOTO_MAX_BYTES:
            send = self.bot.send_document
        suffix = Path(path).suffix.lower()
        if kind == "voice" and suffix not in VOICE_SUFFIXES:
            # sendVoice takes Ogg/Opus; MP3 and M4A go as audio, anything else as a file
            send = self.bot.send_audio if suffix in AUDIO_SUFFIXES else self.bot.send_document
        try:
            sent = await send(
                chat_id,
                source,
                caption=_caption(caption),
                reply_to_message_id=_message_id(meta.get(REPLY_TO_KEY)),
                message_thread_id=_send_thread(meta),
            )
        except Exception as exc:
            return self._failure(exc, (), "")
        return SendResult.sent(str(sent.message_id))
