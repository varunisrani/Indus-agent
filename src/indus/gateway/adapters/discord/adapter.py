"""The Discord adapter (P15-34) over discord.py 2.7.1, imported lazily.

``DiscordAdapter`` speaks to Discord through a ``discord``-shaped namespace (``sdk``): the real
module, imported on first connect, or the test wire fake. Nothing here imports discord.py at
module load, so the gateway runs (and the contract suite runs this adapter) without it.

Connect: one ``Client`` with the ``message_content``, ``dm_messages`` and ``guild_messages``
intents, plus ``members`` only when a role allowlist is configured (role checks read member data).
A rejected token or missing privileged intents is fatal; anything else is retryable. After the
client is ready the application commands are synced in the background (``commands.py``).
Send-only (``indus send``): ``login`` over REST only, no gateway connection, no handlers.

Inbound: messages, edits and interactions become ``InboundEvent``s (see the note in
``docs/notes/P15/discord.md``). A mention in a mention-gated guild channel opens a conversation
thread: the event is keyed on the thread the message will start (its own id) and the thread is
created on the first outbound operation for it, with a 1,440-minute archive; the channel's last 20
messages come along as context. Component views are sent stopped, so every press reaches
``on_interaction``; with a role allowlist configured, presses by people without a grant are
refused here, privately.

Outbound: text as given (already formatted and chunked by the sender), split again only past
2,000 code points, ``allowed_mentions`` none unless configured, replies as message references.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx

from indus.gateway.adapters.base import (
    Adapter,
    AdapterConnectError,
    ApprovalPrompt,
    Capabilities,
    ChannelEntry,
    ChatInfo,
    ClarifyPrompt,
    FetcherSeat,
    InboundSink,
    MediaKind,
    approval_callback,
    clarify_callback,
)
from indus.gateway.adapters.discord import commands as cmds
from indus.gateway.adapters.discord import inbound
from indus.gateway.config import GLOBAL_ALLOW_ALL_ENV, GLOBAL_ALLOWED_USERS_ENV, policy_env
from indus.gateway.core.authz import MentionPolicy, allow_set
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source, parse_command
from indus.gateway.core.send import SendResult, is_ambiguous_timeout
from indus.gateway.outbound.chunker import Chunked, code_points, split_message
from indus.gateway.registry import policy_env_name

__all__ = [
    "CDN_HOSTS",
    "HISTORY_LIMIT",
    "ROLES_ENV",
    "THREAD_ARCHIVE_MINUTES",
    "TOKEN_ENV",
    "DiscordAdapter",
    "load_sdk",
]

log = logging.getLogger(__name__)

TOKEN_ENV = "DISCORD_BOT_TOKEN"
ROLES_ENV = policy_env_name("discord", "ALLOWED_ROLES")
MAX_LEN = 2000
BUTTON_LABEL_MAX = 80
BUTTONS_PER_ROW = 5
MAX_ROWS = 5
SELECT_MAX_OPTIONS = 25
THREAD_ARCHIVE_MINUTES = 1440
HISTORY_LIMIT = 20
HISTORY_LINE_MAX = 500
# discord.py sleeps through 429s up to this many seconds itself (its floor), then raises.
LIBRARY_RATELIMIT_WAIT = 30.0
CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
FETCH_TIMEOUT = 60.0

_SEEN_MAX = 4096
_THREADS_MAX = 2048
_PENDING_MAX = 512
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FATAL_STARTUP = {
    "LoginFailure": (
        "auth_failed",
        "Discord rejected the bot token: create a new one in the Discord developer portal and "
        f"update {TOKEN_ENV}",
    ),
    "PrivilegedIntentsRequired": (
        "intents_required",
        "Discord refused the privileged intents: turn on Message Content (and Server Members "
        "when a role allowlist is set) for the bot in the Discord developer portal",
    ),
}
THREAD_FALLBACK_NAME = "Conversation"
REFUSED_PRESS = "You are not allowed to use these buttons."
REFUSED_COMMAND = "You are not allowed to use this bot's commands."


def load_sdk() -> Any:
    """The discord.py module (the ``discord`` extra); a fatal connect error when missing."""
    try:
        return importlib.import_module("discord")
    except ImportError as exc:
        raise AdapterConnectError(
            "discord.py is not installed: pip install 'indus-agent[discord]'",
            retryable=False,
            code="missing_dependency",
        ) from exc


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUTHY


def _setting(extra: Mapping[str, Any], key: str, default: bool) -> bool:
    raw = extra.get(key)
    return default if raw is None else _truthy(raw)


def _retry_after(exc: BaseException) -> float | None:
    value = getattr(exc, "retry_after", None)
    if value is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if headers is not None:
            with contextlib.suppress(Exception):
                value = headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _remember(store: OrderedDict[str, Any], key: str, value: Any, cap: int) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > cap:
        store.popitem(last=False)


class DiscordAdapter(Adapter):
    platform: ClassVar[str] = "discord"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_LEN,
        len_fn=code_points,
        markup="markdown",
        splits_natively=True,
        supports_edit=True,
        supports_delete=True,
        supports_buttons=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
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
        "discord:Intents": (
            "default",
            "message_content",
            "dm_messages",
            "guild_messages",
            "members",
        ),
        "discord.abc:Messageable": ("send", "typing", "fetch_message", "history"),
        "discord:Message": ("edit", "delete", "create_thread"),
        "discord:AllowedMentions": ("none",),
        "discord:MessageReference": (),
        "discord:File": (),
        "discord:ButtonStyle": ("primary", "secondary", "danger"),
        "discord:SelectOption": (),
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

    def __init__(
        self,
        *,
        clock: Any,
        settings: Any = None,
        secrets: Sequence[str] = (),
        sink: InboundSink | None = None,
        token: str | None = None,
        env: Mapping[str, str] | None = None,
        sdk: Any = None,
        send_only: bool = False,
        proxy: str | None = None,
        is_paired: Callable[[str], Awaitable[bool]] | None = None,
        extra_commands: Sequence[tuple[str, str, str]] = (),
        fetch_transport: httpx.AsyncBaseTransport | None = None,
        cdn_hosts: frozenset[str] = CDN_HOSTS,
    ) -> None:
        credentials: Mapping[str, str] = getattr(settings, "credentials", None) or {}
        self._token = token or credentials.get(TOKEN_ENV) or ""
        super().__init__(clock=clock, settings=settings, secrets=(*secrets, self._token), sink=sink)
        self._env: Mapping[str, str] = env or {}
        self._sdk = sdk
        self.send_only = send_only
        self._proxy = proxy
        self._is_paired = is_paired
        self.extra_commands = tuple(extra_commands)
        self._fetch_transport = fetch_transport
        self._cdn_hosts = cdn_hosts
        extra: Mapping[str, Any] = getattr(settings, "extra", None) or {}
        self._extra = extra
        self._roles = frozenset(allow_set(self._env.get(ROLES_ENV))) | frozenset(
            allow_set(extra.get("allowed_roles"))
        )
        self._mention_policy = MentionPolicy.from_extra(extra)
        self._auto_thread = _setting(extra, "auto_thread", True)
        self._no_thread = frozenset(allow_set(extra.get("no_thread_channels")))
        self._backfill = _setting(extra, "history_backfill", True)
        self._sync_policy = str(extra.get("command_sync") or "diff").strip().lower()
        self._client: Any = None
        self._runner: asyncio.Future[Any] | None = None
        self._sync_task: asyncio.Task[None] | None = None
        self._me = ""
        self._seen: OrderedDict[str, None] = OrderedDict()
        # Threads we opened or posted in: messages there are addressed to us.
        self._ours: OrderedDict[str, None] = OrderedDict()
        # Thread id (= starter message id) → (starter message, name), created on first use.
        self._pending: OrderedDict[str, tuple[Any, str]] = OrderedDict()
        self._opening: dict[str, asyncio.Task[Any]] = {}
        # Threads we failed to open: their traffic goes to the parent channel.
        self._fallback: OrderedDict[str, Any] = OrderedDict()
        # Message id → the channel or thread holding it (edits and deletes inside threads).
        self._where: OrderedDict[str, Any] = OrderedDict()

    # -- the library -------------------------------------------------------------------------------

    @property
    def sdk(self) -> Any:
        if self._sdk is None:
            self._sdk = load_sdk()
        return self._sdk

    @property
    def client(self) -> Any:
        return self._client

    @property
    def roles(self) -> frozenset[str]:
        return self._roles

    def _mentions(self) -> Any:
        allowed = self.sdk.AllowedMentions
        wanted = self._extra.get("allowed_mentions")
        if not isinstance(wanted, Mapping) or not wanted:
            return allowed.none()
        return allowed(
            everyone=_truthy(wanted.get("everyone")),
            users=_truthy(wanted.get("users")),
            roles=_truthy(wanted.get("roles")),
        )

    def _intents(self) -> Any:
        intents = self.sdk.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        intents.guild_messages = True
        intents.members = bool(self._roles)
        return intents

    # -- lifecycle ---------------------------------------------------------------------------------

    def _connect_error(self, exc: BaseException) -> AdapterConnectError:
        if isinstance(exc, AdapterConnectError):
            return exc
        fatal = _FATAL_STARTUP.get(type(exc).__name__)
        if fatal is not None:
            return AdapterConnectError(fatal[1], retryable=False, code=fatal[0])
        return AdapterConnectError(f"Discord connection failed: {type(exc).__name__}: {exc}")

    async def _open(self, is_reconnect: bool) -> None:
        if not self._token:
            raise AdapterConnectError(
                f"no Discord bot token ({TOKEN_ENV})", retryable=False, code="missing_credentials"
            )
        sdk = self.sdk
        if self._client is not None:  # never two live clients answering the same messages
            await self._shut(self._client)
        options: dict[str, Any] = {
            "intents": self._intents(),
            "allowed_mentions": self._mentions(),
            "max_ratelimit_timeout": LIBRARY_RATELIMIT_WAIT,
        }
        if self._proxy:
            options["proxy"] = self._proxy
        client = sdk.Client(**options)
        self._client = client
        if self.send_only:
            try:
                await client.login(self._token)
            except Exception as exc:
                await self._shut(client)
                raise self._connect_error(exc) from None
            return
        self._listen(client)
        runner = asyncio.ensure_future(client.start(self._token))
        self._runner = runner
        ready = asyncio.ensure_future(client.wait_until_ready())
        try:
            await asyncio.wait({runner, ready}, return_when=asyncio.FIRST_COMPLETED)
            if runner.done() and not runner.cancelled() and runner.exception() is not None:
                ready.cancel()
                error = runner.exception()
                assert error is not None
                raise self._connect_error(error)
            await ready
        except BaseException:
            ready.cancel()
            await self._shut(client)
            raise
        runner.add_done_callback(self._runner_ended)
        user = getattr(client, "user", None)
        self._me = str(getattr(user, "id", "") or "")
        if self._sync_policy != "off":
            self._sync_task = asyncio.ensure_future(self._sync_commands())

    def _runner_ended(self, runner: asyncio.Future[Any]) -> None:
        if runner.cancelled() or not self.connected:
            return
        error = runner.exception()
        if error is not None:
            log.warning("discord: the gateway connection ended: %s", self.scrub(repr(error)))

    async def _shut(self, client: Any) -> None:
        with contextlib.suppress(Exception):
            await client.close()
        runner, self._runner = self._runner, None
        if runner is not None:
            if not runner.done():
                runner.cancel()
            with contextlib.suppress(BaseException):
                await runner
        if self._client is client:
            self._client = None

    async def _close(self) -> None:
        task, self._sync_task = self._sync_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        for opening in list(self._opening.values()):
            opening.cancel()
        self._opening.clear()
        client = self._client
        if client is not None:
            await self._shut(client)

    def _listen(self, client: Any) -> None:
        async def on_message(message: Any) -> None:
            await self._on_message(message)

        async def on_message_edit(before: Any, after: Any) -> None:
            await self._on_edit(before, after)

        async def on_interaction(interaction: Any) -> None:
            await self._on_interaction(interaction)

        for handler in (on_message, on_message_edit, on_interaction):
            client.event(handler)

    async def _sync_commands(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            app_id = getattr(client, "application_id", None) or self._me
            if not app_id:
                log.warning("discord: no application id; commands not synced")
                return
            specs = cmds.desired_commands(self.extra_commands)
            summary = await cmds.sync_commands(client.http, app_id, specs, clock=self.clock)
            log.info(
                "discord: commands synced (%d created, %d edited, %d deleted, %d unchanged)",
                len(summary.created),
                len(summary.edited),
                len(summary.deleted),
                summary.unchanged,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            wait = _retry_after(exc)
            later = (
                f" (Discord asks to wait {wait:g} s; retried on the next connect)" if wait else ""
            )
            log.warning(
                "discord: command sync failed%s: %s", later, self.scrub(f"{type(exc).__name__}")
            )

    # -- inbound: messages -------------------------------------------------------------------------

    def _first_time(self, message_id: str) -> bool:
        if message_id in self._seen:
            return False
        _remember(self._seen, message_id, None, _SEEN_MAX)
        return True

    async def _on_message(self, message: Any) -> None:
        if not self._first_time(str(getattr(message, "id", ""))):
            return
        event = await self._event_for(message, edited=False)
        if event is not None:
            await self.emit(event)

    async def _on_edit(self, before: Any, after: Any) -> None:
        if str(getattr(before, "content", "")) == str(getattr(after, "content", "")):
            return  # an embed or pin update, not a new text
        event = await self._event_for(after, edited=True)
        if event is not None:
            await self.emit(event)

    def _role_grant(self, user: Any, guild: Any) -> bool:
        """A member of this guild holding one of the allowed roles (never in DMs)."""
        if not self._roles or guild is None:
            return False
        roles = getattr(user, "roles", None)
        if roles is None:
            return False
        return any(str(getattr(role, "id", "")) in self._roles for role in roles)

    def _gated(self, channel_id: str) -> bool:
        policy = self._mention_policy
        return policy.require_mention and channel_id not in policy.free_response

    async def _event_for(self, message: Any, *, edited: bool) -> InboundEvent | None:
        author = getattr(message, "author", None)
        if author is None or not inbound.is_conversation(message):
            return None
        if self._me and str(getattr(author, "id", "")) == self._me:
            return None
        channel = getattr(message, "channel", None)
        kind = inbound.channel_kind(channel)
        channel_id = str(getattr(channel, "id", ""))
        guild = getattr(message, "guild", None) or getattr(channel, "guild", None)
        text = inbound.strip_mentions(str(getattr(message, "content", "") or ""), self._me)
        mentioned = inbound.mentions(message, self._me)
        is_bot = bool(getattr(author, "bot", False))
        reference = getattr(message, "reference", None)
        media, media_kind, duration = inbound.attachments_of(message)
        is_command = parse_command(text) is not None

        chat_id, chat_type, thread_id, parent = channel_id, kind, None, None
        prospective: str | None = None
        if kind == "thread":
            parent = inbound.parent_id(channel)
            chat_id = parent or channel_id
            thread_id = channel_id
            owner = str(getattr(channel, "owner_id", "") or "")
            mentioned = mentioned or channel_id in self._ours or (bool(owner) and owner == self._me)
        if not text and not media and not (kind == "group" and mentioned and not edited):
            return None  # nothing to answer (an embed, a sticker, a bare edit)
        context = ""
        if kind == "group" and not edited and mentioned and not is_command and not is_bot:
            gated = self._gated(channel_id)
            if gated and self._backfill:
                context = await self._channel_context(channel, message)
            if not text and not media and not context:
                return None  # a bare mention with nothing around it
            if (
                gated
                and self._auto_thread
                and channel_id not in self._no_thread
                and reference is None
            ):
                prospective = str(getattr(message, "id", ""))
                name = inbound.thread_name(text, THREAD_FALLBACK_NAME)
                _remember(self._pending, prospective, (message, name), _PENDING_MAX)
                chat_type, thread_id, parent = "thread", prospective, channel_id

        metadata: dict[str, Any] = {}
        if mentioned:
            metadata["mentioned"] = True
        if edited:
            metadata["edited"] = True
        if duration is not None:
            metadata["duration"] = duration
        if context:
            metadata["channel_context"] = True
            said = text or "[They mentioned you without writing anything]"
            text = f"{context}\n\n[The message addressed to you]\n{said}"
        source = Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=self._chat_name(channel, author, kind),
            user_id=str(getattr(author, "id", "")),
            user_name=inbound.display_name(author),
            thread_id=thread_id,
            is_bot=is_bot,
            guild_id=str(guild.id) if guild is not None else None,
            parent_chat_id=parent,
            prospective_thread_id=prospective,
            role_authorized=self._role_grant(author, guild),
        )
        reply_id = getattr(reference, "message_id", None) if reference is not None else None
        resolved = getattr(reference, "resolved", None) if reference is not None else None
        reply_text = str(getattr(resolved, "content", "") or "") or None if resolved else None
        reply_author = getattr(resolved, "author", None) if resolved is not None else None
        return InboundEvent(
            source=source,
            text=text,
            kind=media_kind or MessageKind.TEXT,
            message_id=str(getattr(message, "id", "")) or None,
            media=media,
            reply_to_id=str(reply_id) if reply_id is not None else None,
            reply_to_text=reply_text,
            reply_to_author_id=str(reply_author.id) if reply_author is not None else None,
            reply_to_author_name=(
                inbound.display_name(reply_author) if reply_author is not None else None
            ),
            reply_to_is_own=(
                reply_author is not None and str(getattr(reply_author, "id", "")) == self._me
            ),
            reply_expected=True if kind == "dm" or mentioned else None,
            metadata=metadata,
            timestamp=getattr(message, "created_at", None),
        )

    @staticmethod
    def _chat_name(channel: Any, author: Any, kind: str) -> str | None:
        if kind == "dm":
            return inbound.display_name(author)
        name = getattr(channel, "name", None)
        guild = getattr(channel, "guild", None)
        if name and guild is not None and getattr(guild, "name", None):
            return f"{guild.name} / #{name}"
        return str(name) if name else None

    async def _channel_context(self, channel: Any, before: Any) -> str:
        """The channel's last messages before ``before`` (back to our own last message)."""
        history: Any = getattr(channel, "history", None)
        if not callable(history):
            return ""
        lines: list[str] = []
        try:
            pages: Any = history(limit=HISTORY_LIMIT, before=before)
            async for item in pages:
                author = getattr(item, "author", None)
                if self._me and str(getattr(author, "id", "")) == self._me:
                    break
                if not inbound.is_conversation(item):
                    continue
                content = str(getattr(item, "clean_content", None) or item.content or "").strip()
                if not content and getattr(item, "attachments", None):
                    content = "(an attachment)"
                if not content:
                    continue
                if len(content) > HISTORY_LINE_MAX:
                    content = content[: HISTORY_LINE_MAX - 1] + "…"
                lines.append(f"[{inbound.display_name(author)}] {content}")
        except Exception as exc:
            log.debug("discord: channel history unavailable: %s", type(exc).__name__)
            return ""
        if not lines:
            return ""
        lines.reverse()
        return "[Recent messages in this channel, oldest first]\n" + "\n".join(lines)

    # -- inbound: interactions ---------------------------------------------------------------------

    async def _on_interaction(self, interaction: Any) -> None:
        kind = str(getattr(getattr(interaction, "type", None), "name", "") or "")
        if kind == "component":
            await self._on_press(interaction)
        elif kind == "application_command":
            await self._on_command(interaction)

    async def _respond(self, interaction: Any, text: str | None) -> None:
        response = getattr(interaction, "response", None)
        if response is None:
            return
        try:
            if text is None:
                await response.defer()
            else:
                await response.send_message(text, ephemeral=True)
        except Exception as exc:  # expired or already answered: nothing to do
            log.debug("discord: interaction answer failed: %s", type(exc).__name__)

    async def press_allowed(self, user: Any, guild: Any) -> bool:
        """With a role allowlist set: allow-all, a user allowlist, an allowed role or a pairing
        approval admits a button press or an application command; anything else is refused
        here. Without a role allowlist the authorization chain decides alone."""
        if not self._roles:
            return True
        names = policy_env(self.platform)
        if _truthy(self._env.get(names.allow_all_users)) or _truthy(
            self._env.get(GLOBAL_ALLOW_ALL_ENV)
        ):
            return True
        uid = str(getattr(user, "id", "") or "")
        if not uid:
            return False
        users = (
            allow_set(self._env.get(names.allowed_users))
            | allow_set(self._env.get(GLOBAL_ALLOWED_USERS_ENV))
            | allow_set(self._extra.get("allow_from"))
        )
        if "*" in users or uid in users:
            return True
        if self._role_grant(user, guild):
            return True
        if self._is_paired is not None:
            try:
                return await self._is_paired(uid)
            except Exception:
                log.warning("discord: the pairing store could not be read", exc_info=True)
        return False

    def _interaction_source(self, interaction: Any) -> Source:
        channel = getattr(interaction, "channel", None)
        user = getattr(interaction, "user", None)
        guild = getattr(interaction, "guild", None)
        kind = inbound.channel_kind(channel)
        channel_id = str(getattr(interaction, "channel_id", None) or getattr(channel, "id", ""))
        chat_id, thread_id, parent = channel_id, None, None
        if kind == "thread":
            parent = inbound.parent_id(channel)
            chat_id, thread_id = parent or channel_id, channel_id
        return Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=kind,
            chat_name=self._chat_name(channel, user, kind),
            user_id=str(getattr(user, "id", "")) or None,
            user_name=inbound.display_name(user) if user is not None else None,
            thread_id=thread_id,
            is_bot=bool(getattr(user, "bot", False)),
            guild_id=str(guild.id) if guild is not None else None,
            parent_chat_id=parent,
            role_authorized=self._role_grant(user, guild),
        )

    async def _on_press(self, interaction: Any) -> None:
        data: Mapping[str, Any] = getattr(interaction, "data", None) or {}
        values = data.get("values") or ()
        payload = values[0] if values else data.get("custom_id")
        if not isinstance(payload, str) or not payload:
            return
        user = getattr(interaction, "user", None)
        if not await self.press_allowed(user, getattr(interaction, "guild", None)):
            log.info("discord: refused a button press from %s", getattr(user, "id", "?"))
            await self._respond(interaction, REFUSED_PRESS)
            return
        await self._respond(interaction, None)
        carrying = getattr(interaction, "message", None)
        await self.emit(
            InboundEvent(
                source=self._interaction_source(interaction),
                message_id=str(carrying.id) if carrying is not None else None,
                metadata={"button": payload},
                reply_expected=True,
            )
        )

    async def _on_command(self, interaction: Any) -> None:
        data: Mapping[str, Any] = getattr(interaction, "data", None) or {}
        name = str(data.get("name") or "").strip()
        if not name:
            return
        args = ""
        for option in data.get("options") or ():
            if isinstance(option, Mapping) and option.get("name") == cmds.ARGS_OPTION:
                args = str(option.get("value") or "").strip()
        text = f"/{name} {args}".strip()
        user = getattr(interaction, "user", None)
        if not await self.press_allowed(user, getattr(interaction, "guild", None)):
            log.info("discord: refused /%s from %s", name, getattr(user, "id", "?"))
            await self._respond(interaction, REFUSED_COMMAND)
            return
        await self._respond(interaction, f"Running /{name}…")
        await self.emit(
            InboundEvent(
                source=self._interaction_source(interaction),
                text=text,
                reply_expected=True,
                metadata={"mentioned": True, "app_command": name},
            )
        )

    # -- outbound: where to ------------------------------------------------------------------------

    def _need_client(self) -> Any:
        if self._client is None:
            raise ConnectionError("Discord client is not connected")
        return self._client

    async def _channel(self, channel_id: str) -> Any:
        client = self._need_client()
        ident = int(channel_id)
        found = client.get_channel(ident)
        if found is None:
            found = await client.fetch_channel(ident)
        return found

    async def _open_thread(self, thread_id: str) -> Any:
        """Create the pending thread ``thread_id`` from its starter message (once)."""
        running = self._opening.get(thread_id)
        if running is None:
            running = asyncio.ensure_future(self._create_thread(thread_id))
            self._opening[thread_id] = running
        try:
            return await asyncio.shield(running)
        finally:
            if running.done():
                self._opening.pop(thread_id, None)

    async def _create_thread(self, thread_id: str) -> Any:
        starter, name = self._pending[thread_id]
        try:
            thread = await starter.create_thread(
                name=name, auto_archive_duration=THREAD_ARCHIVE_MINUTES
            )
        except Exception as exc:
            log.warning(
                "discord: could not open a thread; answering in the channel: %s",
                self.scrub(f"{type(exc).__name__}: {exc}"),
            )
            parent = getattr(starter, "channel", None)
            _remember(self._fallback, thread_id, parent, _PENDING_MAX)
            self._pending.pop(thread_id, None)
            return parent
        self._pending.pop(thread_id, None)
        _remember(self._ours, str(getattr(thread, "id", thread_id)), None, _THREADS_MAX)
        return thread

    async def _target(self, chat_id: str, meta: Mapping[str, Any]) -> Any:
        """The channel or thread an outbound operation goes to (opening a pending thread)."""
        thread = meta.get("thread_id")
        wanted = str(thread) if thread else str(chat_id)
        if wanted in self._fallback:
            return self._fallback[wanted] or await self._channel(str(chat_id))
        if wanted in self._pending or wanted in self._opening:
            return await self._open_thread(wanted)
        return await self._channel(wanted)

    def _reference(self, reply_to: str | None, channel: Any) -> Any:
        if not reply_to or not str(reply_to).isdigit():
            return None
        channel_id = getattr(channel, "id", None)
        if str(channel_id) == str(reply_to):
            return None  # the starter message of this very thread
        return self.sdk.MessageReference(
            message_id=int(reply_to), channel_id=channel_id, fail_if_not_exists=False
        )

    def _sent_in(self, channel: Any, *message_ids: str) -> None:
        for message_id in message_ids:
            _remember(self._where, message_id, channel, _SEEN_MAX)
        if inbound.channel_kind(channel) == "thread":
            _remember(self._ours, str(getattr(channel, "id", "")), None, _THREADS_MAX)

    async def _holder(self, chat_id: str, message_id: str, meta: Mapping[str, Any]) -> Any:
        known = self._where.get(str(message_id))
        return known if known is not None else await self._target(chat_id, meta)

    # -- outbound: text ----------------------------------------------------------------------------

    def _failure(self, exc: BaseException, ids: list[str], chunked: Chunked) -> SendResult:
        remainder = None
        if ids and not is_ambiguous_timeout(exc):
            remainder = chunked.remainder(len(ids))
        return self._scrubbed(
            SendResult.failure(
                exc=exc,
                retry_after=_retry_after(exc),
                delivered_ids=tuple(ids),
                remainder=remainder,
            )
        )

    async def _post(
        self,
        chat_id: str,
        channel: Any,
        chunked: Chunked,
        reference: Any,
        ids: list[str],
    ) -> SendResult:
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            try:
                sent = await channel.send(
                    chunk.text,
                    reference=reference if n == 0 else None,
                    allowed_mentions=self._mentions(),
                )
            except Exception as exc:
                return self._failure(exc, ids, chunked)
            ids.append(str(sent.id))
        self._sent_in(channel, *ids)
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        chunked = split_message(text, MAX_LEN, len_fn=code_points)
        try:
            channel = await self._target(chat_id, meta)
        except Exception as exc:
            return self._failure(exc, [], chunked)
        return await self._post(chat_id, channel, chunked, self._reference(reply_to, channel), [])

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        chunked = split_message(text, MAX_LEN, len_fn=code_points)
        try:
            channel = await self._holder(chat_id, message_id, meta)
            message = await channel.fetch_message(int(message_id))
            await message.edit(content=chunked.chunks[0].text, view=None)
        except Exception as exc:
            return self._failure(exc, [], chunked)
        if len(chunked.chunks) == 1:
            return SendResult.sent(message_id)
        rest = Chunked(chunked.source, chunked.chunks[1:])
        return await self._post(chat_id, channel, rest, None, [message_id])

    async def _delete(self, chat_id: str, message_id: str) -> bool:
        channel = await self._holder(chat_id, message_id, {})
        message = await channel.fetch_message(int(message_id))
        await message.delete()
        self._where.pop(str(message_id), None)
        return True

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        channel = await self._target(chat_id, meta)
        async with channel.typing():
            pass

    # -- outbound: media and components ------------------------------------------------------------

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        try:
            channel = await self._target(chat_id, meta)
            sent = await channel.send(
                caption or None,
                file=self.sdk.File(path),
                reference=self._reference(meta.get("reply_to"), channel),
                allowed_mentions=self._mentions(),
            )
        except Exception as exc:
            return self._failure(exc, [], split_message(caption or "", MAX_LEN))
        self._sent_in(channel, str(sent.id))
        return SendResult.sent(str(sent.id))

    def _button(self, label: str, custom_id: str, style: str, index: int) -> Any:
        styles = self.sdk.ButtonStyle
        chosen = {"primary": styles.primary, "danger": styles.danger}.get(style, styles.secondary)
        return self.sdk.ui.Button(
            label=cmds.clip(label, BUTTON_LABEL_MAX) or "…",
            custom_id=custom_id,
            style=chosen,
            row=index // BUTTONS_PER_ROW,
        )

    def _view(self, items: Sequence[Any]) -> Any:
        view = self.sdk.ui.View(timeout=None)
        for item in items:
            view.add_item(item)
        # A stopped view is never dispatched by the library: presses reach on_interaction.
        view.stop()
        return view

    async def _send_view(
        self, chat_id: str, text: str, view: Any, meta: dict[str, Any]
    ) -> SendResult:
        body = split_message(text, MAX_LEN, len_fn=code_points)
        try:
            channel = await self._target(chat_id, meta)
        except Exception as exc:
            return self._failure(exc, [], body)
        ids: list[str] = []
        *lead, last = body.chunks
        if lead:
            head = await self._post(chat_id, channel, Chunked(body.source, tuple(lead)), None, ids)
            if not head.ok:
                return head
        try:
            sent = await channel.send(last.text, view=view, allowed_mentions=self._mentions())
        except Exception as exc:
            return self._failure(exc, ids, body)
        self._sent_in(channel, str(sent.id))
        return SendResult.sent(str(sent.id), tuple(ids))

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        actions = prompt.actions[: BUTTONS_PER_ROW * MAX_ROWS]
        buttons = [
            self._button(a.label, approval_callback(prompt.request_id, a.choice), a.style, n)
            for n, a in enumerate(actions)
        ]
        return await self._send_view(chat_id, prompt.text, self._view(buttons), meta)

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        choices = prompt.choices
        if len(choices) <= BUTTONS_PER_ROW * MAX_ROWS:
            items = [
                self._button(choice, clarify_callback(prompt.clarify_id, n), "", n)
                for n, choice in enumerate(choices)
            ]
        else:
            options = [
                self.sdk.SelectOption(
                    label=cmds.clip(choice, BUTTON_LABEL_MAX) or "…",
                    value=clarify_callback(prompt.clarify_id, n),
                )
                for n, choice in enumerate(choices[:SELECT_MAX_OPTIONS])
            ]
            items = [
                self.sdk.ui.Select(
                    custom_id=f"clr:{prompt.clarify_id}:pick"[:100],
                    options=options,
                    placeholder=cmds.clip(prompt.question, 150) or None,
                    min_values=1,
                    max_values=1,
                )
            ]
        return await self._send_view(chat_id, prompt.question, self._view(items), meta)

    # -- descriptive -------------------------------------------------------------------------------

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        channel = await self._channel(chat_id)
        kind = inbound.channel_kind(channel)
        name = getattr(channel, "name", None) or str(chat_id)
        return ChatInfo(name=str(name), type=kind)

    async def list_channels(self) -> Sequence[ChannelEntry] | None:
        client = self._client
        guilds = getattr(client, "guilds", None) if client is not None else None
        if guilds is None:
            return None
        found: list[ChannelEntry] = []
        for guild in guilds:
            for channel in getattr(guild, "text_channels", None) or ():
                found.append(
                    ChannelEntry(
                        id=str(channel.id),
                        name=str(getattr(channel, "name", "") or channel.id),
                        type="channel",
                        guild=str(getattr(guild, "name", "") or ""),
                    )
                )
        return found

    # -- media download ----------------------------------------------------------------------------

    async def fetch_attachment(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """Stream one attachment from Discord's CDN (https, CDN hosts only, no redirects)."""
        url = attachment.ref
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in self._cdn_hosts:
            raise ValueError("not a Discord attachment address")
        async with (
            httpx.AsyncClient(
                timeout=FETCH_TIMEOUT,
                follow_redirects=False,
                proxy=self._proxy,
                transport=self._fetch_transport,
                trust_env=False,
            ) as http,
            http.stream("GET", url) as response,
        ):
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Hand the download function to the media intake (``MediaIntake.register_fetcher``)."""
        return seat.register_fetcher(self.platform, self.fetch_attachment)
