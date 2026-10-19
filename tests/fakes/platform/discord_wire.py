"""A fake of the Discord library surface our adapter touches (P15-01).

Shapes follow discord.py: a client whose ``get_channel`` returns messageable channels; channels
``send(content, reference=, view=, allowed_mentions=)`` and yield ``typing()``; messages
``edit``, ``delete``, ``add_reaction``, ``create_thread``. Errors are raised as stand-ins named
like the library's (``HTTPException`` with ``status``/``code``/``text``, ``Forbidden``,
``NotFound``). Inbound traffic arrives through the ``on_message`` / ``on_interaction`` handlers
the adapter registers, delivered by ``dispatch_pending()``. Cap: 2,000 code points.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from tests.fakes.platform.core import (
    Button,
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakePlatformError,
)

MAX_LEN = 2000

SURFACE: dict[str, tuple[str, ...]] = {
    "discord:Client": ("start", "close", "wait_until_ready", "get_channel", "fetch_channel"),
    "discord:Intents": ("default", "message_content", "dm_messages", "guild_messages", "members"),
    "discord.abc:Messageable": ("send", "typing", "fetch_message"),
    "discord:Message": ("edit", "delete", "add_reaction", "remove_reaction", "create_thread"),
    "discord:AllowedMentions": ("none",),
    "discord:HTTPException": (),
    "discord:Forbidden": (),
    "discord:NotFound": (),
    "discord.ui:View": ("add_item",),
    "discord.ui:Button": (),
    "discord.app_commands:CommandTree": ("sync", "add_command", "clear_commands"),
}


class DiscordException(Exception):
    pass


class HTTPException(DiscordException):
    def __init__(self, status: int, code: int, text: str) -> None:
        super().__init__(f"{status} (error code: {code}): {text}")
        self.status = status
        self.code = code
        self.text = text


class Forbidden(HTTPException):
    pass


class NotFound(HTTPException):
    pass


def _translate(exc: FakePlatformError) -> Exception:
    kind = exc.kind
    if kind is FailureKind.RATE_LIMIT:
        return HTTPException(429, 0, "You are being rate limited.")
    if kind is FailureKind.FORBIDDEN:
        return Forbidden(403, 50007, "Cannot send messages to this user")
    if kind is FailureKind.CHAT_NOT_FOUND:
        return NotFound(404, 10003, "Unknown Channel")
    if kind is FailureKind.THREAD_NOT_FOUND:
        return NotFound(404, 10008, "Unknown Message")
    if kind is FailureKind.TOO_LONG:
        return HTTPException(400, 50035, "Invalid Form Body: Must be 2000 or fewer in length.")
    if kind is FailureKind.READ_TIMEOUT:
        return TimeoutError()
    if kind is FailureKind.CONNECT_TIMEOUT:
        return ConnectionRefusedError("Cannot connect to host discord.com:443")
    return HTTPException(400, 50035, "Invalid Form Body")


@dataclass(frozen=True)
class AllowedMentions:
    everyone: bool = False
    users: bool = False
    roles: bool = False

    @classmethod
    def none(cls) -> AllowedMentions:
        return cls()


@dataclass(frozen=True)
class Intents:
    message_content: bool = False
    dm_messages: bool = True
    guild_messages: bool = True
    members: bool = False

    @classmethod
    def default(cls) -> Intents:
        return cls()


@dataclass(frozen=True)
class Author:
    id: int
    name: str
    bot: bool = False


@dataclass(frozen=True)
class Attachment:
    filename: str
    url: str
    content_type: str


@dataclass(frozen=True)
class MessageReference:
    message_id: int


@dataclass
class View:
    """``discord.ui.View`` stand-in: buttons with ``custom_id``."""

    items: list[Button] = field(default_factory=list[Button])

    def add_item(self, item: Button) -> None:
        self.items.append(item)


def _wrap[T](coro: Awaitable[T]) -> Awaitable[T]:
    async def run() -> T:
        try:
            return await coro
        except FakePlatformError as exc:
            raise _translate(exc) from None

    return run()


@dataclass
class DiscordMessage:
    id: int
    channel: Channel
    content: str
    author: Author
    reference: MessageReference | None = None
    attachments: tuple[Attachment, ...] = ()
    mentions_bot: bool = False
    created_at: Any = None

    async def edit(self, content: str | None = None, view: View | None = None) -> DiscordMessage:
        buttons = tuple(view.items) if view is not None else None
        text = content if content is not None else self.content
        await _wrap(
            self.channel.wire.platform.edit(
                str(self.channel.id), str(self.id), text, buttons=buttons
            )
        )
        self.content = text
        return self

    async def delete(self) -> None:
        await _wrap(self.channel.wire.platform.delete(str(self.channel.id), str(self.id)))

    async def add_reaction(self, emoji: str) -> None:
        await _wrap(self.channel.wire.platform.react(str(self.channel.id), str(self.id), emoji))

    async def remove_reaction(self, emoji: str, member: Any = None) -> None:
        self.channel.wire.removed_reactions.append((self.id, emoji))

    async def create_thread(self, name: str, auto_archive_duration: int = 1440) -> Channel:
        thread_id = self.id  # Discord: a thread started from a message reuses its id
        self.channel.wire.threads.append((thread_id, name, auto_archive_duration))
        return self.channel.wire.channel(thread_id, kind="thread", parent=self.channel.id)


@dataclass
class Channel:
    id: int
    wire: DiscordWire
    kind: str = "text"  # text, dm, thread
    parent_id: int | None = None
    guild_id: int | None = None

    async def send(
        self,
        content: str | None = None,
        *,
        reference: MessageReference | DiscordMessage | None = None,
        view: View | None = None,
        allowed_mentions: AllowedMentions | None = None,
        file: Any = None,
    ) -> DiscordMessage:
        self.wire.sent_allowed_mentions.append(allowed_mentions)
        reply_to = (
            str(reference.message_id if isinstance(reference, MessageReference) else reference.id)
            if reference
            else None
        )
        if file is not None:
            media = FakeMedia("document", str(file), caption=content or "")
            sent = await _wrap(self.wire.platform.upload(str(self.id), media, reply_to=reply_to))
        else:
            buttons = tuple(view.items) if view is not None else ()
            sent = await _wrap(
                self.wire.platform.post(
                    str(self.id), content or "", reply_to=reply_to, buttons=buttons
                )
            )
        bot = Author(int(self.wire.platform.bot_user_id), self.wire.platform.bot_name, True)
        return DiscordMessage(int(sent.message_id), self, sent.text, bot)

    @asynccontextmanager
    async def typing(self) -> AsyncIterator[None]:
        await _wrap(self.wire.platform.typing(str(self.id)))
        yield

    async def fetch_message(self, message_id: int) -> DiscordMessage:
        found = self.wire.platform.message(str(message_id))
        if found is None or found.deleted:
            raise NotFound(404, 10008, "Unknown Message")
        author = Author(int(self.wire.platform.bot_user_id), "bot", found.from_bot)
        return DiscordMessage(message_id, self, found.text, author)


@dataclass
class Interaction:
    """A component press: ``custom_id`` in ``data``."""

    id: str
    user: Author
    channel: Channel
    data: dict[str, Any]
    message: DiscordMessage | None = None


class CommandTree:
    """``discord.app_commands.CommandTree`` stand-in: records registrations and syncs."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.commands: list[Any] = []
        self.synced: list[list[str]] = []

    def add_command(self, command: Any) -> None:
        self.commands.append(command)

    def clear_commands(self, *, guild: Any = None) -> None:
        self.commands.clear()

    async def sync(self, *, guild: Any = None) -> list[Any]:
        names = [str(getattr(c, "name", c)) for c in self.commands]
        self.synced.append(names)
        return list(self.commands)


class FakeClient:
    """``discord.Client`` stand-in: ``start`` connects, handlers receive inbound traffic."""

    def __init__(self, wire: DiscordWire, intents: Intents | None = None) -> None:
        self.wire = wire
        self.intents = intents or Intents.default()
        self.handlers: dict[str, Callable[..., Awaitable[None]]] = {}
        self.user = Author(int(wire.platform.bot_user_id), wire.platform.bot_name, True)
        self.tree = CommandTree(self)

    def event(self, fn: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        self.handlers[fn.__name__] = fn
        return fn

    async def start(self, token: str) -> None:
        await _wrap(self.wire.platform.connect())

    async def wait_until_ready(self) -> None:
        return None

    async def close(self) -> None:
        await self.wire.platform.disconnect()

    def get_channel(self, channel_id: int) -> Channel | None:
        return self.wire.channel(channel_id)

    async def fetch_channel(self, channel_id: int) -> Channel:
        return self.wire.channel(channel_id)


class DiscordWire:
    SURFACE = SURFACE
    # Which fake stands in for each surface object (the wire self-check reads it).
    FAKES: dict[str, type] = {}

    def __init__(self, platform: FakeChatPlatform | None = None) -> None:
        self.platform = platform or FakeChatPlatform("discord", max_len=MAX_LEN, bot_user_id="900")
        self._channels: dict[int, Channel] = {}
        self.threads: list[tuple[int, str, int]] = []
        self.removed_reactions: list[tuple[int, str]] = []
        self.sent_allowed_mentions: list[AllowedMentions | None] = []
        self.client = FakeClient(self)

    def channel(self, channel_id: int, *, kind: str = "text", parent: int | None = None) -> Channel:
        found = self._channels.get(int(channel_id))
        if found is None:
            found = Channel(int(channel_id), self, kind, parent)
            self._channels[found.id] = found
        return found

    def _message(self, item: FakeInbound) -> DiscordMessage:
        kind = {"dm": "dm", "thread": "thread"}.get(item.chat_kind, "text")
        channel = self.channel(int(item.thread_id or item.chat_id), kind=kind)
        if item.chat_kind == "thread":
            channel.parent_id = int(item.chat_id)
        if item.guild_id is not None:
            channel.guild_id = int(item.guild_id)
        author = Author(int(item.user_id), item.user_name or item.user_id, item.is_bot)
        attachments = tuple(
            Attachment(m.ref, f"https://cdn.example/{m.ref}", m.mime) for m in item.media
        )
        reference = MessageReference(int(item.reply_to)) if item.reply_to else None
        return DiscordMessage(
            int(item.message_id or 0),
            channel,
            item.text,
            author,
            reference,
            attachments,
            item.mentions_bot,
            item.at,
        )

    async def dispatch_pending(self) -> int:
        """Deliver every queued inbound update to the registered handlers."""
        delivered = 0
        for item in self.platform.drain_inbound():
            message = self._message(item)
            if item.event == "button":
                handler = self.client.handlers.get("on_interaction")
                interaction = Interaction(
                    str(item.button_id),
                    message.author,
                    message.channel,
                    {"custom_id": item.button_data},
                    None,
                )
                if handler is not None:
                    await handler(interaction)
            else:
                name = "on_message_edit" if item.event == "edited" else "on_message"
                handler = self.client.handlers.get(name)
                if handler is not None:
                    if name == "on_message_edit":
                        await handler(message, message)
                    else:
                        await handler(message)
            delivered += 1
        return delivered


DiscordWire.FAKES = {
    "discord:Client": FakeClient,
    "discord:Intents": Intents,
    "discord.abc:Messageable": Channel,
    "discord:Message": DiscordMessage,
    "discord:AllowedMentions": AllowedMentions,
    "discord:HTTPException": HTTPException,
    "discord:Forbidden": Forbidden,
    "discord:NotFound": NotFound,
    "discord.ui:View": View,
    "discord.ui:Button": Button,
    "discord.app_commands:CommandTree": CommandTree,
}

__all__ = [
    "SURFACE",
    "CommandTree",
    "AllowedMentions",
    "Author",
    "Channel",
    "DiscordMessage",
    "DiscordWire",
    "FakeClient",
    "Forbidden",
    "HTTPException",
    "Intents",
    "Interaction",
    "NotFound",
    "View",
]
