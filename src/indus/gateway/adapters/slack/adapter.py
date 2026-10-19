"""Slack over Socket Mode (P15-35).

Connecting checks the bot token with ``auth.test`` (bot user id, bot id, workspace id), puts our
listeners on a Bolt ``AsyncApp`` (``message``, ``app_mention``, our button actions) and opens the
Socket Mode connection with the app token. A send-only adapter (``indus send``) stops after
``auth.test``: no listener, no socket, so it never takes an event a running gateway is owed.

Inbound, one Events API message becomes one `InboundEvent`:

- ``im`` channels are ``dm``; channels and group DMs are ``group``; a reply inside a thread is
  ``thread`` with the root ``ts`` as thread id (a DM keeps ``dm`` and carries the thread id).
- A top-level channel message names its own ``ts`` as the prospective thread: our reply opens a
  thread under it, so the message and its in-thread follow-ups share one session key.
- The workspace id is the source's ``scope_id``: one channel id in two workspaces is two sessions.
- ``message`` and ``app_mention`` for one message share a ``ts``; the first one wins (dedupe on
  workspace, channel and ``ts``, which also drops Socket Mode redeliveries).
- Our own posts never come back; other bots' posts are passed on marked ``is_bot`` for the gateway's
  bot rules; housekeeping subtypes (joins, topic changes, deletions …) are dropped; an edit of a
  message already taken is dropped, an edit of one never taken is passed on marked ``edited``.
- Our mention is taken out of the text and reported as ``metadata["mentioned"]``; a message in a
  thread we were summoned into, or posted in, counts as mentioned too.
- Files become attachments pointing at Slack's private URL; ``media_fetcher`` downloads them with
  the bot token (Slack hosts only) for media intake.

Outbound text arrives as mrkdwn from the sender and goes to ``chat.postMessage``, threaded under the
metadata's thread id, else (``reply_in_thread``, on by default, not in DMs) under the message it
answers. A user id target is opened as a DM first (cached). Text over the 39,000 cap is refused as
too long before it reaches Slack: the sender splits. Approvals and clarify questions are Block Kit
buttons whose action ids are our callback data; an edit replaces the blocks, so a settled card loses
its buttons. The typing indicator is ``assistant.threads.setStatus`` (Slack shows it only in a
thread), switched off for the process once Slack says the app may not use it.

Settings (``platforms.slack``): ``ignored_channels`` (no inbound, outbound refused),
``reply_in_thread`` (default true), ``typing_status_text`` (default "is thinking…").

The libraries are imported in one place, ``load_sdk``, on the first connect; tests hand in a
`SlackSdk` built over the wire fake instead.
"""

from __future__ import annotations

import logging
import os
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Hashable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast
from urllib.parse import urlsplit

from indus.gateway.adapters.base import (
    REPLY_TO_KEY,
    Adapter,
    AdapterConnectError,
    ApprovalPrompt,
    Capabilities,
    ChatInfo,
    ClarifyPrompt,
    Fetcher,
    FetcherSeat,
    InboundSink,
    MediaKind,
    approval_callback,
    clarify_callback,
    code_points,
)
from indus.gateway.adapters.slack import blocks as kit
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult

if TYPE_CHECKING:
    from indus.foundation.clock import Clock
    from indus.gateway.config import AdapterConfig, PlatformSettings

__all__ = [
    "DEFAULT_STATUS_TEXT",
    "MAX_LEN",
    "SDK_LOADER",
    "SlackAdapter",
    "SlackSdk",
    "is_slack_file_url",
    "load_sdk",
]

log = logging.getLogger(__name__)

PLATFORM = "slack"
MAX_LEN = 39_000
# Bounded memories (oldest forgotten first).
DM_CACHE_MAX = 5_000
SEEN_MAX = 5_000
NAMES_MAX = 5_000
THREADS_MAX = 5_000
DEFAULT_STATUS_TEXT = "is thinking…"
INSTALL_HINT = "pip install 'indus-agent[slack]'"

# Message subtypes that are a person (or another bot) speaking; every other subtype is dropped.
_SPEAKING_SUBTYPES = frozenset(
    {"", "bot_message", "file_share", "thread_broadcast", "me_message", "document_mention"}
)
# Token problems no retry fixes.
_AUTH_ERRORS = frozenset(
    {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"}
)
# ``assistant.threads.setStatus`` answers that mean "this app cannot show a status here".
_STATUS_UNAVAILABLE = frozenset(
    {
        "missing_scope",
        "not_allowed_token_type",
        "unknown_method",
        "method_deprecated",
        "feature_not_enabled",
        "not_allowed",
    }
)
_BLOCK_REFUSALS = frozenset({"invalid_blocks", "invalid_blocks_format"})
# Our button callbacks (``adapters.base.approval_callback`` / ``clarify_callback``).
_OUR_ACTIONS = re.compile(r"^(?:apv|clr):")


# -- the library seam ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SlackSdk:
    """The two things the adapter builds from slack-bolt: the app (whose ``client`` is the async
    Web API client) for a bot token, and the Socket Mode handler for an app token."""

    make_app: Callable[[str, str | None], Any]
    make_socket: Callable[[Any, str, str | None], Any]


def load_sdk() -> SlackSdk:
    """The real libraries (the ``slack`` extra). Raises ImportError when they are missing."""
    from slack_bolt.adapter.socket_mode.async_handler import (  # pyright: ignore[reportMissingImports]
        AsyncSocketModeHandler,  # pyright: ignore[reportUnknownVariableType]
    )
    from slack_bolt.app.async_app import AsyncApp  # pyright: ignore[reportMissingImports]
    from slack_sdk.web.async_client import AsyncWebClient  # pyright: ignore[reportMissingImports]

    def make_app(token: str, proxy: str | None) -> Any:
        client = AsyncWebClient(token=token, proxy=proxy)
        return AsyncApp(token=token, client=client)

    def make_socket(app: Any, app_token: str, proxy: str | None) -> Any:
        return AsyncSocketModeHandler(app, app_token, proxy=proxy)

    return SlackSdk(make_app, make_socket)


SDK_LOADER: Callable[[], SlackSdk] = load_sdk
"""How an adapter without an injected `SlackSdk` gets one (tests replace it)."""


# -- small helpers ---------------------------------------------------------------------------------


class _Recent:
    """An insertion-ordered set that forgets its oldest members past ``limit``."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: OrderedDict[Hashable, None] = OrderedDict()

    def add(self, item: Hashable) -> bool:
        """Remember ``item``; False when it was already known."""
        if item in self._items:
            self._items.move_to_end(item)
            return False
        self._items[item] = None
        while len(self._items) > self._limit:
            self._items.popitem(last=False)
        return True

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def __len__(self) -> int:
        return len(self._items)


class _Cache:
    """A bounded string map (oldest entry dropped first)."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> str | None:
        return self._items.get(key)

    def put(self, key: str, value: str) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _api_error(exc: BaseException) -> str:
    """Slack's error code on a Web API failure (``channel_not_found`` …), else ""."""
    response = getattr(exc, "response", None)
    getter = getattr(response, "get", None)
    if not callable(getter):
        return ""
    try:
        return _text(getter("error"))
    except Exception:
        return ""


def _retry_after(exc: BaseException) -> float | None:
    """The ``Retry-After`` seconds of a rate-limited call (any header spelling)."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    for name, value in cast(Mapping[object, object], headers).items():
        if str(name).lower() == "retry-after":
            try:
                return max(0.0, float(str(value).strip()))
            except ValueError:
                return None
    return None


def _flag(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _channel_set(value: object) -> frozenset[str]:
    if isinstance(value, str):
        items: list[object] = list(value.split(","))
    elif isinstance(value, list | tuple | set | frozenset):
        items = list(cast(list[object], value))
    else:
        items = []
    return frozenset(s for s in (str(i).strip() for i in items) if s)


def _when(ts: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _bot_authored(event: Mapping[str, Any], subtype: str, bot_id: str) -> bool:
    """A bot or app posted it: the bot subtype, a bot id or profile, or an app post without the
    client message id every person's message carries."""
    if subtype == "bot_message" or bot_id or event.get("bot_profile"):
        return True
    if _mapping(event.get("user_profile")).get("is_bot") is True:
        return True
    return bool(event.get("app_id")) and not event.get("client_msg_id")


def _file_kind(file: Mapping[str, Any], mime: str) -> MessageKind:
    """Slack audio clips (``slack_audio``) and audio files are voice notes for intake."""
    if file.get("subtype") == "slack_audio" or mime.startswith("audio/"):
        return MessageKind.VOICE
    if mime.startswith("image/"):
        return MessageKind.PHOTO
    if mime.startswith("video/"):
        return MessageKind.VIDEO
    return MessageKind.DOCUMENT


def is_slack_file_url(url: str) -> bool:
    """True for an ``https`` URL on a Slack host: the only place the bot token may be sent."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return parts.scheme == "https" and (host == "slack.com" or host.endswith(".slack.com"))


# -- the adapter -----------------------------------------------------------------------------------


class SlackAdapter(Adapter):
    platform: ClassVar[str] = PLATFORM
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_LEN,
        len_fn=code_points,
        markup="mrkdwn",
        supports_edit=True,
        supports_delete=True,
        supports_buttons=True,
        # a reply with ``thread_ts`` = the message's ts opens the thread the session is keyed on
        reply_opens_thread=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "slack_sdk.web.async_client:AsyncWebClient": (
            "auth_test",
            "chat_postMessage",
            "chat_update",
            "chat_delete",
            "chat_postEphemeral",
            "files_upload_v2",
            "conversations_open",
            "conversations_info",
            "users_info",
            "assistant_threads_setStatus",
        ),
        "slack_bolt.app.async_app:AsyncApp": ("event", "action", "client"),
        "slack_bolt.adapter.socket_mode.async_handler:AsyncSocketModeHandler": (
            "connect_async",
            "close_async",
        ),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        bot_token: str,
        app_token: str,
        settings: PlatformSettings | None = None,
        proxy: str | None = None,
        send_only: bool = False,
        sdk: SlackSdk | None = None,
        sink: InboundSink | None = None,
        http_transport: Any = None,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(bot_token, app_token), sink=sink)
        self._bot_token = bot_token
        self._app_token = app_token
        self._proxy = proxy
        self.send_only = send_only
        self._sdk = sdk
        self._http_transport = http_transport
        extra: Mapping[str, Any] = settings.extra if settings is not None else {}
        self.ignored_channels = _channel_set(extra.get("ignored_channels"))
        self.reply_in_thread = _flag(extra.get("reply_in_thread"), True)
        self.status_text = _text(extra.get("typing_status_text")) or DEFAULT_STATUS_TEXT
        self._app: Any = None
        self._client: Any = None
        self._socket: Any = None
        self.bot_user_id = ""
        self.bot_id = ""
        self.team_id = ""
        self._seen = _Recent(SEEN_MAX)
        self._threads = _Recent(THREADS_MAX)
        self._dm_channels = _Recent(DM_CACHE_MAX)
        self._dm_open = _Cache(DM_CACHE_MAX)
        self._user_names = _Cache(NAMES_MAX)
        self._chat_names = _Cache(NAMES_MAX)
        self._statuses: dict[str, str] = {}
        self._status_off = False

    @classmethod
    def from_config(cls, config: AdapterConfig, *, sdk: SlackSdk | None = None) -> SlackAdapter:
        credentials = config.settings.credentials
        bot = _text(credentials.get("SLACK_BOT_TOKEN"))
        app = _text(credentials.get("SLACK_APP_TOKEN"))
        missing = [n for n, v in (("SLACK_BOT_TOKEN", bot), ("SLACK_APP_TOKEN", app)) if not v]
        if missing:
            raise ValueError("missing Slack credentials: " + ", ".join(missing))
        return cls(
            clock=config.clock,
            bot_token=bot,
            app_token=app,
            settings=config.settings,
            proxy=config.proxy,
            send_only=config.send_only,
            sdk=sdk,
        )

    # -- lifecycle ---------------------------------------------------------------------------------

    def _auth_refused(self, exc: BaseException, what: str) -> AdapterConnectError | None:
        code = _api_error(exc)
        if code in _AUTH_ERRORS:
            return AdapterConnectError(
                f"Slack refused the {what} ({code})", retryable=False, code="auth_failed"
            )
        return None

    async def _open(self, is_reconnect: bool) -> None:
        sdk = self._sdk
        if sdk is None:
            try:
                sdk = SDK_LOADER()
            except ImportError:
                raise AdapterConnectError(
                    f"the Slack libraries are not installed: {INSTALL_HINT}",
                    retryable=False,
                    code="missing_dependency",
                ) from None
            self._sdk = sdk
        app = sdk.make_app(self._bot_token, self._proxy)
        client = app.client
        try:
            auth = _mapping(await client.auth_test())
        except Exception as exc:
            refused = self._auth_refused(exc, "bot token")
            if refused is not None:
                raise refused from None
            raise
        self.bot_user_id = _text(auth.get("user_id"))
        self.bot_id = _text(auth.get("bot_id"))
        self.team_id = _text(auth.get("team_id"))
        self._app, self._client = app, client
        if self.send_only:
            return
        app.event("message")(self._on_message)
        app.event("app_mention")(self._on_mention)
        app.action(_OUR_ACTIONS)(self._on_action)
        socket = sdk.make_socket(app, self._app_token, self._proxy)
        try:
            await socket.connect_async()
        except Exception as exc:
            self._app = self._client = None
            try:
                await socket.close_async()
            except Exception as close_exc:
                log.debug("slack: closing a failed socket: %s", self.scrub(str(close_exc)))
            refused = self._auth_refused(exc, "app token")
            if refused is not None:
                raise refused from None
            raise
        self._socket = socket
        log.info(
            "slack: connected to workspace %s as %s%s",
            self.team_id or "?",
            self.bot_user_id or "?",
            " (reconnect)" if is_reconnect else "",
        )

    async def _close(self) -> None:
        socket, self._socket = self._socket, None
        self._app = self._client = None
        self._statuses.clear()
        if socket is not None:
            await socket.close_async()

    def _web(self) -> Any:
        if self._client is None:
            raise ConnectionError("not connected to Slack")
        return self._client

    # -- inbound -----------------------------------------------------------------------------------

    async def _on_message(
        self, event: Mapping[str, Any], body: Mapping[str, Any] | None = None, **_: Any
    ) -> None:
        await self._take(event, body, mentioned=False)

    async def _on_mention(
        self, event: Mapping[str, Any], body: Mapping[str, Any] | None = None, **_: Any
    ) -> None:
        await self._take(event, body, mentioned=True)

    async def _take(
        self, event: Mapping[str, Any], body: Mapping[str, Any] | None, *, mentioned: bool
    ) -> None:
        try:
            built = await self.normalise(event, body, mentioned=mentioned)
        except Exception:
            log.exception("slack: an inbound event could not be read")
            return
        if built is not None:
            await self.emit(built)

    def _team_of(self, event: Mapping[str, Any], body: Mapping[str, Any] | None) -> str:
        for payload in (event, body or {}):
            for key in ("team", "team_id"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
                found = _text(_mapping(value).get("id"))
                if found:
                    return found
        for grant in cast(list[object], (body or {}).get("authorizations") or []):
            found = _text(_mapping(grant).get("team_id"))
            if found:
                return found
        return self.team_id

    def is_ignored(self, channel: str) -> bool:
        return "*" in self.ignored_channels or channel in self.ignored_channels

    def _is_dm(self, channel: str) -> bool:
        return channel.startswith("D") or channel in self._dm_channels

    async def normalise(
        self,
        raw: Mapping[str, Any],
        body: Mapping[str, Any] | None = None,
        *,
        mentioned: bool = False,
    ) -> InboundEvent | None:
        """One Events API message as an `InboundEvent`, or None when it is not for us."""
        event: Mapping[str, Any] = raw
        edited = False
        subtype = _text(event.get("subtype"))
        if subtype == "message_changed":
            inner = _mapping(event.get("message"))
            event = {
                **inner,
                "channel": event.get("channel") or inner.get("channel"),
                "channel_type": event.get("channel_type") or inner.get("channel_type"),
                "team": inner.get("team") or event.get("team"),
            }
            edited = True
            subtype = _text(event.get("subtype"))
        if subtype not in _SPEAKING_SUBTYPES:
            return None
        channel = _text(event.get("channel"))
        ts = _text(event.get("ts"))
        if not channel or not ts or self.is_ignored(channel):
            return None
        team = self._team_of(event, body)
        if not self._seen.add((team, channel, ts)):
            return None
        user = _text(event.get("user"))
        bot_id = _text(event.get("bot_id"))
        if (user and user == self.bot_user_id) or (bot_id and bot_id == self.bot_id):
            return None
        is_bot = _bot_authored(event, subtype, bot_id)

        channel_type = _text(event.get("channel_type"))
        if channel_type == "im":
            self._dm_channels.add(channel)
        is_dm = channel_type == "im" or (not channel_type and self._is_dm(channel))
        thread_ts = _text(event.get("thread_ts"))
        in_thread = bool(thread_ts) and thread_ts != ts
        chat_type: ChatType
        if is_dm:
            chat_type = "dm"
        elif in_thread:
            chat_type = "thread"
        else:
            chat_type = "group"
        thread_id = thread_ts if in_thread else None
        prospective = ts if chat_type == "group" and self.reply_in_thread else None

        text = _text(event.get("text"))
        if self.bot_user_id:
            token = f"<@{self.bot_user_id}>"
            if token in text:
                mentioned = True
                text = re.sub(rf"\s*{re.escape(token)}\s*", " ", text).strip()
        anchor = thread_id or prospective
        if anchor and not is_dm:
            if mentioned:
                self._threads.add((channel, anchor))
            elif thread_id and (channel, thread_id) in self._threads:
                mentioned = True

        media: list[Attachment] = []
        kinds: list[MessageKind] = []
        for item in cast(list[object], event.get("files") or []):
            file = _mapping(item)
            url = _text(file.get("url_private_download")) or _text(file.get("url_private"))
            if not url:
                continue
            mime = _text(file.get("mimetype"))
            media.append(Attachment(ref=url, mime=mime))
            kinds.append(_file_kind(file, mime))

        metadata: dict[str, Any] = {}
        if mentioned:
            metadata["mentioned"] = True
        if edited:
            metadata["edited"] = True
        if team:
            metadata["team_id"] = team
        source = Source(
            platform=PLATFORM,
            chat_id=channel,
            chat_type=chat_type,
            chat_name=None if is_dm else await self._chat_name(channel),
            user_id=user or bot_id or None,
            user_name=await self._user_name(user) if user and not is_bot else None,
            thread_id=thread_id,
            is_bot=is_bot,
            scope_id=team or None,
            prospective_thread_id=prospective,
        )
        return InboundEvent(
            source=source,
            text=text,
            kind=kinds[0] if kinds else MessageKind.TEXT,
            message_id=ts,
            media=tuple(media),
            metadata=metadata,
            timestamp=_when(ts),
        )

    async def _user_name(self, user: str) -> str | None:
        cached = self._user_names.get(user)
        if cached is not None:
            return cached or None
        name = ""
        try:
            found = _mapping(_mapping(await self._web().users_info(user=user)).get("user"))
            profile = _mapping(found.get("profile"))
            name = (
                _text(profile.get("display_name"))
                or _text(found.get("real_name"))
                or _text(found.get("name"))
            )
        except Exception as exc:
            log.debug("slack: users.info failed: %s", self.scrub(str(exc)))
        self._user_names.put(user, name)
        return name or None

    async def _chat_name(self, channel: str) -> str | None:
        cached = self._chat_names.get(channel)
        if cached is not None:
            return cached or None
        name = ""
        try:
            found = await self._conversation_info(channel)
            name = _text(found.get("name"))
        except Exception as exc:
            log.debug("slack: conversations.info failed: %s", self.scrub(str(exc)))
        self._chat_names.put(channel, name)
        return name or None

    async def _conversation_info(self, channel: str) -> Mapping[str, Any]:
        response = await self._web().conversations_info(channel=channel)
        return _mapping(_mapping(response).get("channel"))

    async def _on_action(self, ack: Any, body: Mapping[str, Any], **_: Any) -> None:
        """A press of one of our buttons: acknowledged at once, then handed in as an event."""
        try:
            await ack()
        except Exception as exc:
            log.debug("slack: acknowledging an action failed: %s", self.scrub(str(exc)))
        try:
            built = self.normalise_action(body)
        except Exception:
            log.exception("slack: a button press could not be read")
            return
        if built is not None:
            await self.emit(built)

    def normalise_action(self, body: Mapping[str, Any]) -> InboundEvent | None:
        actions = cast(list[object], body.get("actions") or [])
        action = _mapping(actions[0]) if actions else {}
        data = _text(action.get("action_id")) or _text(action.get("value"))
        channel = _text(_mapping(body.get("channel")).get("id"))
        person = _mapping(body.get("user"))
        user = _text(person.get("id"))
        if not data or not channel or not user or self.is_ignored(channel):
            return None
        message = _mapping(body.get("message"))
        card_ts = _text(message.get("ts"))
        thread_ts = _text(message.get("thread_ts"))
        in_thread = bool(thread_ts) and thread_ts != card_ts
        is_dm = self._is_dm(channel)
        chat_type: ChatType = "dm" if is_dm else ("thread" if in_thread else "group")
        team = self._team_of(body, None)
        metadata: dict[str, Any] = {"button": data}
        if card_ts:
            metadata["button_message_id"] = card_ts
        if team:
            metadata["team_id"] = team
        source = Source(
            platform=PLATFORM,
            chat_id=channel,
            chat_type=chat_type,
            user_id=user,
            user_name=_text(person.get("username")) or _text(person.get("name")) or None,
            thread_id=thread_ts if in_thread else None,
            scope_id=team or None,
        )
        return InboundEvent(source=source, text="", metadata=metadata)

    # -- media download ----------------------------------------------------------------------------

    def media_fetcher(self) -> Fetcher:
        """Downloads an attachment's private URL with the bot token, Slack hosts only."""
        token, proxy, transport = self._bot_token, self._proxy, self._http_transport

        async def fetch(attachment: Attachment) -> AsyncIterator[bytes]:
            import httpx

            url = attachment.ref
            if not is_slack_file_url(url):
                raise PermissionError("not a Slack file address")
            async with (
                httpx.AsyncClient(
                    proxy=proxy, transport=transport, follow_redirects=True, timeout=60.0
                ) as http,
                http.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as response,
            ):
                response.raise_for_status()
                kind = response.headers.get("content-type", "").lower()
                if kind.startswith("text/html") and "html" not in attachment.mime.lower():
                    # Slack answers a sign-in page when the app lacks the files:read scope.
                    raise PermissionError("Slack returned a web page instead of the file")
                async for chunk in response.aiter_bytes():
                    yield chunk

        return fetch

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register this adapter's download function with media intake; returns the undo."""
        return seat.register_fetcher(PLATFORM, self.media_fetcher())

    # -- outbound ----------------------------------------------------------------------------------

    def _failure(self, exc: BaseException) -> SendResult:
        log.warning("slack: platform call failed: %s", self.scrub(f"{type(exc).__name__}: {exc}"))
        return SendResult.failure(exc=exc, retry_after=_retry_after(exc))

    def is_final_failure(self, result: SendResult) -> bool:
        error = (result.error or "").lower()
        return any(code in error for code in _AUTH_ERRORS)

    async def _channel(self, chat_id: str) -> str:
        """The conversation to post in: a user id is opened as a DM first (cached)."""
        if chat_id[:1] not in ("U", "W"):
            return chat_id
        cached = self._dm_open.get(chat_id)
        if cached:
            return cached
        response = _mapping(await self._web().conversations_open(users=chat_id))
        opened = _text(_mapping(response.get("channel")).get("id"))
        if not opened:
            raise LookupError("channel_not_found: Slack opened no DM for that user")
        self._dm_open.put(chat_id, opened)
        self._dm_channels.add(opened)
        return opened

    def _thread_for(
        self, channel: str, reply_to: str | None, meta: Mapping[str, Any]
    ) -> str | None:
        explicit = _text(meta.get("thread_id")) or _text(meta.get("thread_ts"))
        if explicit:
            return explicit
        if reply_to and self.reply_in_thread and not self._is_dm(channel):
            return str(reply_to)
        return None

    def _refusal(self, chat_id: str) -> SendResult | None:
        if self.is_ignored(chat_id):
            return SendResult.refused("sending to an ignored Slack channel")
        return None

    @staticmethod
    def _too_long(text: str) -> SendResult | None:
        size = code_points(text)
        if size > MAX_LEN:
            return SendResult.failure(f"msg_too_long: {size} characters (Slack takes {MAX_LEN})")
        return None

    async def _post(self, args: dict[str, Any], *, blocks_optional: bool) -> Mapping[str, Any]:
        """``chat.postMessage``; caller blocks Slack refuses are dropped once when optional."""
        client = self._web()
        try:
            return _mapping(await client.chat_postMessage(**args))
        except Exception as exc:
            if not (blocks_optional and "blocks" in args and _api_error(exc) in _BLOCK_REFUSALS):
                raise
            log.info("slack: blocks refused; sending the text alone")
            plain = {k: v for k, v in args.items() if k != "blocks"}
            return _mapping(await client.chat_postMessage(**plain))

    def _posted(self, response: Mapping[str, Any], channel: str, thread: str | None) -> SendResult:
        ts = _text(response.get("ts")) or _text(response.get("message_ts"))
        if not ts:
            return SendResult.failure("Slack answered without a message ts")
        if thread:
            self._threads.add((channel, thread))
        return SendResult.sent(ts)

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        refused = self._refusal(chat_id) or self._too_long(text)
        if refused is not None:
            return refused
        try:
            channel = await self._channel(chat_id)
            thread = self._thread_for(channel, reply_to, meta)
            args: dict[str, Any] = {"channel": channel, "text": text}
            if thread:
                args["thread_ts"] = thread
            if meta.get("plain"):
                args["mrkdwn"] = False
            else:
                extra_blocks = kit.sanitize(meta.get("blocks"))
                if extra_blocks:
                    args["blocks"] = extra_blocks
            response = await self._post(args, blocks_optional=True)
        except Exception as exc:
            return self._failure(exc)
        return self._posted(response, channel, thread)

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        refused = self._refusal(chat_id) or self._too_long(text)
        if refused is not None:
            return refused
        try:
            channel = await self._channel(chat_id)
            client = self._web()
            # The new blocks (none by default) replace the old ones, so a settled card loses
            # its buttons.
            args: dict[str, Any] = {
                "channel": channel,
                "ts": message_id,
                "text": text,
                "blocks": kit.sanitize(meta.get("blocks")) or [],
            }
            try:
                await client.chat_update(**args)
            except Exception as exc:
                if not args["blocks"] or _api_error(exc) not in _BLOCK_REFUSALS:
                    raise
                await client.chat_update(**{**args, "blocks": []})
        except Exception as exc:
            return self._failure(exc)
        return SendResult.sent(message_id)

    async def _delete(self, chat_id: str, message_id: str) -> bool:
        channel = await self._channel(chat_id)
        await self._web().chat_delete(channel=channel, ts=message_id)
        return True

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        if self._status_off or self.is_ignored(chat_id):
            return
        thread = _text(meta.get("thread_id")) or _text(meta.get("thread_ts"))
        if not thread:
            return  # Slack shows a status only inside a thread
        await self._set_status(chat_id, thread, self.status_text)
        self._statuses[chat_id] = thread

    async def _typing_stop(self, chat_id: str) -> None:
        thread = self._statuses.pop(chat_id, None)
        if thread and not self._status_off:
            await self._set_status(chat_id, thread, "")

    async def _set_status(self, chat_id: str, thread: str, status: str) -> None:
        try:
            await self._web().assistant_threads_setStatus(
                channel_id=chat_id, thread_ts=thread, status=status
            )
        except Exception as exc:
            if _api_error(exc) in _STATUS_UNAVAILABLE:
                self._status_off = True
                log.info("slack: thread status is not available to this app; typing is off")
                return
            raise

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        refused = self._refusal(chat_id)
        if refused is not None:
            return refused
        try:
            channel = await self._channel(chat_id)
            reply_to = meta.get(REPLY_TO_KEY)
            thread = self._thread_for(channel, str(reply_to) if reply_to else None, meta)
            args: dict[str, Any] = {
                "channel": channel,
                "file": path,
                "filename": os.path.basename(path),
                "initial_comment": caption or "",
            }
            if thread:
                args["thread_ts"] = thread
            response = _mapping(await self._web().files_upload_v2(**args))
        except Exception as exc:
            return self._failure(exc)
        files = cast(list[object], response.get("files") or [])
        file_id = _text(_mapping(files[0]).get("id")) if files else ""
        if thread:
            self._threads.add((channel, thread))
        return SendResult.sent(file_id or os.path.basename(path))

    async def _send_card(
        self, chat_id: str, text: str, card: list[dict[str, Any]], meta: dict[str, Any]
    ) -> SendResult:
        try:
            channel = await self._channel(chat_id)
            thread = self._thread_for(channel, None, meta)
            args: dict[str, Any] = {"channel": channel, "text": text, "blocks": card}
            if thread:
                args["thread_ts"] = thread
            # No text-only fallback here: a card without its buttons must fail, so the prompt
            # falls back to the typed /approve card.
            response = await self._post(args, blocks_optional=False)
        except Exception as exc:
            return self._failure(exc)
        return self._posted(response, channel, thread)

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        refused = self._refusal(chat_id)
        if refused is not None:
            return refused
        text = self.format(prompt.text)
        data = [approval_callback(prompt.request_id, a.choice) for a in prompt.actions]
        card = kit.sanitize(kit.approval_blocks(prompt, text, data)) or []
        return await self._send_card(chat_id, text, card, meta)

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        refused = self._refusal(chat_id)
        if refused is not None:
            return refused
        text = self.format(prompt.question)
        data = [clarify_callback(prompt.clarify_id, n) for n in range(len(prompt.choices))]
        card = kit.sanitize(kit.clarify_blocks(prompt, text, data)) or []
        return await self._send_card(chat_id, text, card, meta)

    async def _send_private(
        self, chat_id: str, user_id: str, text: str, meta: dict[str, Any]
    ) -> SendResult:
        refused = self._refusal(chat_id) or self._too_long(text)
        if refused is not None:
            return refused
        if not user_id:
            return SendResult.failure("a private notice needs the user it is for")
        try:
            channel = await self._channel(chat_id)
            args: dict[str, Any] = {"channel": channel, "user": user_id, "text": text}
            thread = self._thread_for(channel, None, meta)
            if thread:
                args["thread_ts"] = thread
            response = _mapping(await self._web().chat_postEphemeral(**args))
        except Exception as exc:
            return self._failure(exc)
        return SendResult.sent(_text(response.get("message_ts")) or _text(response.get("ts")))

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        found = await self._conversation_info(chat_id)
        kind: ChatType | None = None
        if found.get("is_im"):
            kind = "dm"
        elif found.get("is_mpim") or found.get("is_channel") or found.get("is_group"):
            kind = "group"
        return ChatInfo(name=_text(found.get("name")) or chat_id, type=kind)
