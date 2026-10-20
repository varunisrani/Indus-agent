"""Telegram DM topics (P16-34, K46).

A bot whose Threaded Mode is on (BotFather → Bot Settings → Threads Settings; ``getMe`` reports
``has_topics_enabled``) can have topics in its private chats. With ``platforms.telegram.dm_topics``
on as well, every such topic is its own conversation: `DmTopics.place` puts the topic id on the
event as its thread, so the session key gets a thread part (two topics, two sessions; the root of
the chat keeps a session of its own), and replies go back into the topic through the usual
``thread_id`` routing.

``/topic [name]`` in a private chat opens a new topic (``createForumTopic``; the name cut to 128
characters, "New chat" when none is given), greets the person inside it and confirms where the
command was sent. It is the adapter's own command, handled before the event reaches the core: at
most one topic per chat every 10 s, and only in private chats while the mode is configured.
Without Threaded Mode the answer says how to turn it on. With the mode off in config the command
goes on to the core like any other text.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol

from indus.foundation.clock import Clock
from indus.gateway.adapters.telegram.bot_api import BotCommand, TelegramError
from indus.gateway.core.events import InboundEvent
from indus.gateway.core.send import SendResult

if TYPE_CHECKING:
    from indus.gateway.adapters.telegram.bot_api import BotApi, Message, User

__all__ = [
    "COOLDOWN_S",
    "DEFAULT_TOPIC_NAME",
    "GREETING",
    "NO_THREADED_MODE",
    "TOPIC_COMMAND",
    "TOPIC_NAME_MAX",
    "DmTopics",
    "TopicHost",
]

log = logging.getLogger(__name__)

TOPIC_COMMAND: Final = "topic"
DEFAULT_TOPIC_NAME: Final = "New chat"
TOPIC_NAME_MAX: Final = 128
COOLDOWN_S: Final = 10.0
OPENED_MEMORY: Final = 1024
MENU_DESCRIPTION: Final = "Open a new topic: a separate conversation with its own history"
GREETING: Final = "This topic is a new conversation with me. Write here to start it."
OPENED: Final = 'Opened the topic "{name}". Continue there; this chat keeps its own conversation.'
WAIT: Final = "A topic was opened here a moment ago; try again in a few seconds."
FAILED: Final = "I could not open a topic here ({reason})."
NO_THREADED_MODE: Final = (
    "Topics in this chat need the bot's Threaded Mode. In @BotFather open this bot, then Bot "
    "Settings → Threads Settings, turn Threaded Mode on and restart the gateway."
)


class TopicHost(Protocol):
    """What topic handling needs from the Telegram adapter."""

    clock: Clock
    bot: BotApi

    def format(self, text: str) -> str: ...

    def scrub(self, text: str) -> str: ...

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> SendResult: ...


def _clean_name(raw: str) -> str:
    name = " ".join(raw.split())
    return name[:TOPIC_NAME_MAX].strip() or DEFAULT_TOPIC_NAME


class DmTopics:
    """DM topic mode for one bot (see the module notes)."""

    def __init__(self, *, enabled: bool, clock: Clock) -> None:
        self.enabled = enabled
        self.clock = clock
        # The bot's Threaded Mode, learned from ``getMe`` at connect.
        self.threaded = False
        self._opened: dict[str, float] = {}

    @property
    def active(self) -> bool:
        return self.enabled and self.threaded

    def identify(self, me: User) -> None:
        self.threaded = me.has_topics_enabled
        if self.enabled and not self.threaded:
            log.warning(
                "telegram: dm_topics is on but the bot's Threaded Mode is off; private chats stay "
                "one conversation each (turn it on in @BotFather: Bot Settings → Threads Settings)"
            )

    def menu_entry(self) -> BotCommand | None:
        """The private-chat menu row for ``/topic`` while the mode is active."""
        return BotCommand(TOPIC_COMMAND, MENU_DESCRIPTION) if self.active else None

    # -- inbound -----------------------------------------------------------------------------------

    def place(self, event: InboundEvent, message: Message) -> InboundEvent:
        """The event keyed on its DM topic when the mode is active (else unchanged)."""
        if (
            not self.active
            or message.chat.type != "private"
            or message.message_thread_id is None
            or event.source.thread_id is not None
        ):
            return event
        source = event.source.model_copy(update={"thread_id": str(message.message_thread_id)})
        return event.model_copy(update={"source": source})

    def wants(self, event: InboundEvent) -> bool:
        """``/topic`` in a private chat while the mode is configured: ours to answer."""
        return (
            self.enabled
            and event.source.chat_type == "dm"
            and not event.source.is_bot
            and event.command == TOPIC_COMMAND
        )

    async def handle(self, host: TopicHost, event: InboundEvent) -> bool:
        """Answer ``/topic [name]``; True when the event was taken (see the module notes)."""
        if not self.wants(event):
            return False
        chat_id = event.source.chat_id
        meta = {"thread_id": event.source.thread_id} if event.source.thread_id else {}

        async def answer(text: str) -> None:
            await host.send(chat_id, host.format(text), reply_to=event.message_id, meta=meta)

        if not self.threaded:
            await answer(NO_THREADED_MODE)
            return True
        now = self.clock.monotonic()
        last = self._opened.get(chat_id)
        if last is not None and now - last < COOLDOWN_S:
            await answer(WAIT)
            return True
        if len(self._opened) >= OPENED_MEMORY:
            self._opened = {k: t for k, t in self._opened.items() if now - t < COOLDOWN_S}
        self._opened[chat_id] = now
        name = _clean_name(event.command_args)
        try:
            topic = await host.bot.create_forum_topic(chat_id, name)
        except TelegramError as exc:
            reason = host.scrub(exc.message) or type(exc).__name__
            log.info("telegram: opening a topic in chat %s failed: %s", chat_id, reason)
            if "forum" in reason.lower():  # not a forum: Threaded Mode is off
                await answer(NO_THREADED_MODE)
            else:
                await answer(FAILED.format(reason=reason))
            return True
        thread = str(topic.message_thread_id)
        log.info("telegram: opened topic %s in chat %s", thread, chat_id)
        await host.send(chat_id, host.format(GREETING), meta={"thread_id": thread})
        await answer(OPENED.format(name=topic.name or name))
        return True
