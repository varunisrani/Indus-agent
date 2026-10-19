"""A fake of the Discord library surface our adapter touches (P15-01, grown by P15-34).

Shapes follow discord.py: a client whose ``get_channel`` returns messageable channels; channels
``send(content, reference=, view=, allowed_mentions=, file=)``, yield ``typing()`` and page
``history()``; messages ``edit``, ``delete``, ``add_reaction``, ``create_thread``. Errors are
raised as stand-ins named like the library's (``HTTPException`` with ``status``/``code``/``text``,
``Forbidden``, ``NotFound``; a rate limit carries ``retry_after``). Inbound traffic arrives through
the ``on_message`` / ``on_message_edit`` / ``on_interaction`` handlers the adapter registers,
delivered by ``dispatch_pending()``; ``invoke_command`` delivers an application command.
Cap: 2,000 code points.

``DiscordWire.sdk`` is the namespace an adapter uses in place of the ``discord`` module:
``Client(**options)`` hands back the wire's one client and records the options (intents,
allowed mentions, proxy). ``client.login`` is the REST-only open (``logins``); ``client.start``
also opens the gateway (``gateway_sessions``). ``client.http`` keeps the application commands
(``commands``) and logs every mutation with its fake-clock time (``mutations``). Members' roles
come from ``member_roles``; a chat with a guild id makes its senders guild members.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
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
    "discord:Client": (
        "start",
        "login",
        "close",
        "wait_until_ready",
        "get_channel",
        "fetch_channel",
        "event",
        "user",
        "guilds",
        "application_id",
    ),
    "discord:Intents": ("default", "message_content", "dm_messages", "guild_messages", "members"),
    "discord.abc:Messageable": ("send", "typing", "fetch_message", "history"),
    "discord:Message": ("edit", "delete", "add_reaction", "remove_reaction", "create_thread"),
    "discord:AllowedMentions": ("none",),
    "discord:MessageReference": (),
    "discord:File": (),
    "discord:ButtonStyle": ("primary", "secondary", "danger", "success"),
    "discord:SelectOption": (),
    "discord:HTTPException": (),
    "discord:Forbidden": (),
    "discord:NotFound": (),
    "discord:RateLimited": (),
    "discord.ui:View": ("add_item", "stop"),
    "discord.ui:Button": (),
    "discord.ui:Select": (),
    "discord.http:HTTPClient": (
        "get_global_commands",
        "upsert_global_command",
        "edit_global_command",
        "delete_global_command",
    ),
}


class DiscordException(Exception):
    pass


class HTTPException(DiscordException):
    def __init__(
        self, status: int, code: int, text: str, *, retry_after: float | None = None
    ) -> None:
        super().__init__(f"{status} (error code: {code}): {text}")
        self.status = status
        self.code = code
        self.text = text
        # The fake's stand-in for the response's ``Retry-After`` on a 429.
        self.retry_after = retry_after


class Forbidden(HTTPException):
    pass


class NotFound(HTTPException):
    pass


class RateLimited(DiscordException):
    """discord.py raises this (not an ``HTTPException``) for a wait over its own cap."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Too many requests. Retry in {retry_after:.2f} seconds.")
        self.retry_after = retry_after


class LoginFailure(DiscordException):
    pass


class PrivilegedIntentsRequired(DiscordException):
    pass


def _translate(exc: FakePlatformError) -> Exception:
    kind = exc.kind
    if kind is FailureKind.RATE_LIMIT:
        return HTTPException(429, 0, "You are being rate limited.", retry_after=exc.retry_after)
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


@dataclass
class Intents:
    message_content: bool = False
    dm_messages: bool = True
    guild_messages: bool = True
    members: bool = False

    @classmethod
    def default(cls) -> Intents:
        return cls()


class ButtonStyle:
    primary = "primary"
    secondary = "secondary"
    danger = "danger"
    success = "success"


@dataclass(frozen=True)
class Role:
    id: int


@dataclass(frozen=True)
class Author:
    id: int
    name: str
    bot: bool = False
    # Present only for guild members (``discord.Member``); a DM sender is a plain user.
    roles: tuple[Role, ...] | None = None

    @property
    def display_name(self) -> str:
        return self.name


@dataclass
class Guild:
    id: int
    name: str = "guild"
    text_channels: list[Channel] = field(default_factory=list["Channel"])


@dataclass(frozen=True)
class Attachment:
    filename: str
    url: str
    content_type: str
    duration: float | None = None
    voice: bool = False

    def is_voice_message(self) -> bool:
        return self.voice


@dataclass(frozen=True)
class MessageReference:
    message_id: int
    channel_id: int | None = None
    guild_id: int | None = None
    fail_if_not_exists: bool = True


@dataclass(frozen=True)
class File:
    fp: str
    filename: str | None = None

    def __str__(self) -> str:
        return self.fp


@dataclass(frozen=True)
class SelectOption:
    label: str
    value: str


@dataclass
class UIButton:
    """``discord.ui.Button`` stand-in; ``data`` is its ``custom_id`` (what the platform stores)."""

    label: str | None = None
    custom_id: str | None = None
    style: str = ButtonStyle.secondary
    row: int | None = None
    disabled: bool = False

    @property
    def data(self) -> str:
        return self.custom_id or ""


@dataclass
class UISelect:
    """``discord.ui.Select`` stand-in: its options' values are what a press carries."""

    custom_id: str = ""
    options: list[SelectOption] = field(default_factory=list[SelectOption])
    placeholder: str | None = None
    min_values: int = 1
    max_values: int = 1
    row: int | None = None

    @property
    def label(self) -> str:
        return self.placeholder or ""

    @property
    def data(self) -> str:
        return self.custom_id


@dataclass
class View:
    """``discord.ui.View`` stand-in: components with a ``custom_id``."""

    items: list[Any] = field(default_factory=list[Any])
    timeout: float | None = 180.0
    stopped: bool = False

    def add_item(self, item: Any) -> View:
        self.items.append(item)
        return self

    def stop(self) -> None:
        self.stopped = True


def _wrap[T](coro: Awaitable[T]) -> Awaitable[T]:
    async def run() -> T:
        try:
            return await coro
        except FakePlatformError as exc:
            raise _translate(exc) from None

    return run()


def _flat_buttons(view: View | None) -> tuple[Any, ...]:
    if view is None:
        return ()
    flat: list[Any] = []
    for item in view.items:
        if isinstance(item, UISelect):
            flat.extend(Button(o.label, o.value) for o in item.options)
        else:
            flat.append(item)
    return tuple(flat)


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
    type: Any = field(default_factory=lambda: SimpleNamespace(name="default"))

    @property
    def guild(self) -> Guild | None:
        return self.channel.guild

    @property
    def mentions(self) -> list[Author]:
        return [self.channel.wire.client.user] if self.mentions_bot else []

    async def edit(
        self, content: str | None = None, view: View | None = None, **_: Any
    ) -> DiscordMessage:
        buttons = _flat_buttons(view) if view is not None else None
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

    async def create_thread(
        self, name: str, auto_archive_duration: int = 1440, **_: Any
    ) -> Channel:
        wire = self.channel.wire
        if wire.thread_failures > 0:
            wire.thread_failures -= 1
            raise Forbidden(403, 50001, "Missing Access")
        thread_id = self.id  # Discord: a thread started from a message reuses its id
        wire.threads.append((thread_id, name, auto_archive_duration))
        thread = wire.channel(thread_id, kind="thread", parent=self.channel.id)
        thread.guild_id = self.channel.guild_id
        thread.owner_id = wire.client.user.id
        return thread


_CHANNEL_TYPES = {"dm": "private", "thread": "public_thread", "text": "text"}


@dataclass
class Channel:
    id: int
    wire: DiscordWire
    kind: str = "text"  # text, dm, thread
    parent_id: int | None = None
    guild_id: int | None = None
    name: str = ""
    # A thread's creator (``discord.Thread.owner_id``).
    owner_id: int | None = None

    @property
    def type(self) -> SimpleNamespace:
        return SimpleNamespace(name=_CHANNEL_TYPES.get(self.kind, "text"))

    @property
    def guild(self) -> Guild | None:
        return self.wire.guild(self.guild_id) if self.guild_id is not None else None

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
        self.wire.sent_views.append(view)
        reply_to = (
            str(reference.message_id if isinstance(reference, MessageReference) else reference.id)
            if reference
            else None
        )
        if file is not None:
            media = FakeMedia("document", str(file), caption=content or "")
            sent = await _wrap(self.wire.platform.upload(str(self.id), media, reply_to=reply_to))
        else:
            sent = await _wrap(
                self.wire.platform.post(
                    str(self.id), content or "", reply_to=reply_to, buttons=_flat_buttons(view)
                )
            )
        bot = self.wire.client.user
        message = DiscordMessage(int(sent.message_id), self, sent.text, bot)
        self.wire.log(message)
        return message

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

    async def history(
        self, *, limit: int | None = 100, before: Any = None, **_: Any
    ) -> AsyncIterator[DiscordMessage]:
        """Newest first, like discord.py without ``after``."""
        logged = self.wire.logs.get(self.id, [])
        if before is not None:
            cut = next((n for n, m in enumerate(logged) if m.id == before.id), len(logged))
            logged = logged[:cut]
        for message in list(reversed(logged))[: limit or None]:
            yield message


@dataclass
class InteractionResponse:
    interaction: Interaction
    done: bool = False
    sent: list[tuple[str, bool]] = field(default_factory=list[tuple[str, bool]])

    def is_done(self) -> bool:
        return self.done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        await self._answer(None, ephemeral)

    async def send_message(self, content: str | None = None, *, ephemeral: bool = False) -> None:
        await self._answer(content or "", ephemeral)

    async def _answer(self, content: str | None, ephemeral: bool) -> None:
        if self.done:
            raise RuntimeError("This interaction has already been responded to before")
        self.done = True
        if content is not None:
            self.sent.append((content, ephemeral))
        await self.interaction.channel.wire.platform.answer(self.interaction.id, content)


@dataclass
class Interaction:
    """A component press (``custom_id`` in ``data``) or an application command (``name``)."""

    id: str
    user: Author
    channel: Channel
    data: dict[str, Any]
    message: DiscordMessage | None = None
    type: Any = field(default_factory=lambda: SimpleNamespace(name="component"))
    response: InteractionResponse = field(init=False)

    def __post_init__(self) -> None:
        self.response = InteractionResponse(self)

    @property
    def channel_id(self) -> int:
        return self.channel.id

    @property
    def guild(self) -> Guild | None:
        return self.channel.guild


class FakeHttp:
    """``discord.http.HTTPClient`` stand-in: the bot's global application commands."""

    def __init__(self, wire: DiscordWire) -> None:
        self.wire = wire
        self.commands: dict[str, dict[str, Any]] = {}
        self.mutations: list[tuple[str, str, float]] = []
        self._ids = itertools.count(1000)
        self.fail_next: Exception | None = None

    def _log(self, op: str, name: str) -> None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        self.mutations.append((op, name, self.wire.platform.clock.monotonic()))

    async def get_global_commands(self, application_id: int) -> list[dict[str, Any]]:
        return [dict(c) for c in self.commands.values()]

    async def upsert_global_command(
        self, application_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._log("upsert", payload["name"])
        current = next((c for c in self.commands.values() if c["name"] == payload["name"]), None)
        ident = current["id"] if current else str(next(self._ids))
        stored = {**payload, "id": ident}
        self.commands[ident] = stored
        return stored

    async def edit_global_command(
        self, application_id: int, command_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._log("edit", payload.get("name", ""))
        stored = {**self.commands[str(command_id)], **payload}
        self.commands[str(command_id)] = stored
        return stored

    async def delete_global_command(self, application_id: int, command_id: str) -> None:
        gone = self.commands.get(str(command_id))
        self._log("delete", gone["name"] if gone else "")
        self.commands.pop(str(command_id), None)


class FakeClient:
    """``discord.Client`` stand-in: ``start`` connects, handlers receive inbound traffic."""

    def __init__(self, wire: DiscordWire, intents: Intents | None = None) -> None:
        self.wire = wire
        self.intents = intents or Intents.default()
        self.options: dict[str, Any] = {}
        self.handlers: dict[str, Callable[..., Awaitable[None]]] = {}
        self._user = Author(int(wire.platform.bot_user_id), wire.platform.bot_name, True)
        self.http = FakeHttp(wire)
        self.closed = False

    @property
    def user(self) -> Author:
        return self._user

    @property
    def application_id(self) -> int:
        return int(self.wire.platform.bot_user_id)

    @property
    def guilds(self) -> list[Guild]:
        return list(self.wire.guilds.values())

    def event(self, fn: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        self.handlers[fn.__name__] = fn
        return fn

    async def login(self, token: str) -> None:
        if self.wire.login_error is not None:
            raise self.wire.login_error
        self.wire.logins += 1
        self.wire.tokens.append(token)

    async def start(self, token: str) -> None:
        await self.login(token)
        await _wrap(self.wire.platform.connect())
        self.wire.gateway_sessions += 1
        self.closed = False

    async def wait_until_ready(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True
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
        self.guilds: dict[int, Guild] = {}
        self.threads: list[tuple[int, str, int]] = []
        self.removed_reactions: list[tuple[int, str]] = []
        self.sent_allowed_mentions: list[AllowedMentions | None] = []
        self.sent_views: list[View | None] = []
        self.logs: dict[int, list[DiscordMessage]] = {}
        self.member_roles: dict[str, tuple[int, ...]] = {}
        self.thread_failures = 0
        self.logins = 0
        self.gateway_sessions = 0
        self.tokens: list[str] = []
        self.login_error: Exception | None = None
        self.client = FakeClient(self)
        self._interactions = itertools.count(1)

    # -- the ``discord`` module an adapter imports --------------------------------------------

    def make_client(self, *, intents: Intents | None = None, **options: Any) -> FakeClient:
        self.client.intents = intents or Intents.default()
        self.client.options = dict(options)
        return self.client

    @property
    def sdk(self) -> SimpleNamespace:
        return SimpleNamespace(
            Client=self.make_client,
            Intents=Intents,
            AllowedMentions=AllowedMentions,
            MessageReference=MessageReference,
            File=File,
            ButtonStyle=ButtonStyle,
            SelectOption=SelectOption,
            ui=SimpleNamespace(View=View, Button=UIButton, Select=UISelect),
        )

    # -- state ---------------------------------------------------------------------------------

    def guild(self, guild_id: int) -> Guild:
        found = self.guilds.get(int(guild_id))
        if found is None:
            found = Guild(int(guild_id))
            self.guilds[found.id] = found
        return found

    def channel(self, channel_id: int, *, kind: str = "text", parent: int | None = None) -> Channel:
        found = self._channels.get(int(channel_id))
        if found is None:
            found = Channel(int(channel_id), self, kind, parent)
            self._channels[found.id] = found
        return found

    def log(self, message: DiscordMessage) -> None:
        self.logs.setdefault(message.channel.id, []).append(message)

    def _author(self, user_id: str, name: str, is_bot: bool, guild_id: int | None) -> Author:
        roles = None
        if guild_id is not None:
            roles = tuple(Role(r) for r in self.member_roles.get(str(user_id), ()))
        return Author(int(user_id), name or str(user_id), is_bot, roles)

    def _message(self, item: FakeInbound) -> DiscordMessage:
        kind = {"dm": "dm", "thread": "thread"}.get(item.chat_kind, "text")
        channel = self.channel(int(item.thread_id or item.chat_id), kind=kind)
        if item.chat_kind == "thread":
            channel.parent_id = int(item.chat_id)
        if item.guild_id is not None:
            channel.guild_id = int(item.guild_id)
        if item.chat_name:
            channel.name = item.chat_name
        author = self._author(item.user_id, item.user_name, item.is_bot, channel.guild_id)
        attachments = tuple(
            Attachment(
                m.ref,
                f"https://cdn.discordapp.com/attachments/{channel.id}/{m.ref}",
                m.mime,
                m.duration,
                m.kind == "voice",
            )
            for m in item.media
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
                sent = self.platform.message(item.message_id) if item.message_id else None
                carrying = (
                    DiscordMessage(int(item.message_id), message.channel, sent.text, message.author)
                    if sent is not None
                    else None
                )
                data: dict[str, Any] = {"custom_id": item.button_data, "component_type": 2}
                interaction = Interaction(
                    str(item.button_id), message.author, message.channel, data, carrying
                )
                if handler is not None:
                    await handler(interaction)
            else:
                if item.event == "message":
                    self.log(message)
                name = "on_message_edit" if item.event == "edited" else "on_message"
                handler = self.client.handlers.get(name)
                if handler is not None:
                    if name == "on_message_edit":
                        before = DiscordMessage(
                            message.id, message.channel, "", message.author, message.reference
                        )
                        await handler(before, message)
                    else:
                        await handler(message)
            delivered += 1
        return delivered

    async def invoke_command(
        self,
        chat_id: str,
        user_id: str,
        name: str,
        args: str = "",
        *,
        chat_kind: str = "group",
        user_name: str = "",
    ) -> Interaction:
        """An application command typed in ``chat_id`` (handled at once)."""
        kind = {"dm": "dm", "thread": "thread"}.get(chat_kind, "text")
        channel = self.channel(int(chat_id), kind=kind)
        chat = self.platform.chats.get(str(chat_id))
        if chat is not None and chat.guild_id is not None:
            channel.guild_id = int(chat.guild_id)
        user = self._author(user_id, user_name, False, channel.guild_id)
        options = [{"name": "args", "type": 3, "value": args}] if args else []
        interaction = Interaction(
            f"ia{next(self._interactions)}",
            user,
            channel,
            {"name": name, "type": 1, "options": options},
            None,
            SimpleNamespace(name="application_command"),
        )
        handler = self.client.handlers.get("on_interaction")
        if handler is not None:
            await handler(interaction)
        return interaction


DiscordWire.FAKES = {
    "discord:Client": FakeClient,
    "discord:Intents": Intents,
    "discord.abc:Messageable": Channel,
    "discord:Message": DiscordMessage,
    "discord:AllowedMentions": AllowedMentions,
    "discord:MessageReference": MessageReference,
    "discord:File": File,
    "discord:ButtonStyle": ButtonStyle,
    "discord:SelectOption": SelectOption,
    "discord:HTTPException": HTTPException,
    "discord:Forbidden": Forbidden,
    "discord:NotFound": NotFound,
    "discord:RateLimited": RateLimited,
    "discord.ui:View": View,
    "discord.ui:Button": UIButton,
    "discord.ui:Select": UISelect,
    "discord.http:HTTPClient": FakeHttp,
}

__all__ = [
    "SURFACE",
    "AllowedMentions",
    "Attachment",
    "Author",
    "ButtonStyle",
    "Channel",
    "DiscordMessage",
    "DiscordWire",
    "FakeClient",
    "FakeHttp",
    "File",
    "Forbidden",
    "Guild",
    "HTTPException",
    "Intents",
    "Interaction",
    "LoginFailure",
    "MessageReference",
    "NotFound",
    "PrivilegedIntentsRequired",
    "RateLimited",
    "Role",
    "SelectOption",
    "UIButton",
    "UISelect",
    "View",
]
