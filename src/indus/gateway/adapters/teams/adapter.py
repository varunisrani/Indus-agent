"""Microsoft Teams through the Bot Framework REST API (P16-11).

Inbound: the Bot Connector POSTs activities to our listener (P16-01 kit; ``/api/messages`` on
port 3978, loopback unless ``webhook_public``, body ≤ 1 MiB). Every request must carry a valid Bot
Framework token (``botauth``) before its body is parsed; then the key's endorsements must name the
activity's channel and the token's ``serviceUrl`` claim, when present, must match the activity's.
The activity's service URL must be on the Microsoft allow-list (``conversations.service_url_ok``),
otherwise it is dropped: that URL is where replies and the bot token would go.

- ``message`` → an `InboundEvent` (personal → dm, groupChat → group, channel → channel, a
  channel reply chain ``…;messageid=<root>`` → thread); the ``<at>`` tag of a bot mention is taken
  out of the text and recorded as ``metadata["mentioned"]``; our own messages are dropped;
  attachments become media references on Microsoft hosts (see ``media_access``);
- ``messageUpdate`` (``editMessage``) → the same event marked ``edited``;
- ``invoke`` ``adaptiveCard/action`` (a press on one of our cards) → a button event carrying our
  callback data, answered at once in the HTTP response; the core decides who may answer;
- ``conversationUpdate`` only refreshes the stored conversation reference;
- activity ids are remembered for an hour, so a retried delivery is not handled twice.

Every verified activity stores its conversation's reference (service URL, type, tenant) in the
`ConversationBook`, which a later proactive send — cron, ``indus send``, a restart — reads.

Outbound: a bot token from the client-credentials grant (`ClientCredentials`, cached), then
``POST {serviceUrl}v3/conversations/{id}/activities`` for messages (split at 28,000 code points,
``textFormat: markdown``), typing and Adaptive Cards (approvals and clarify choices with
``Action.Execute`` buttons; a card the service refuses falls back to the text card), and
``PUT …/activities/{id}`` for edits. A 401 drops the cached token and retries once. Errors are
classified through the kit (`failure_from_status`, `failure_from_exception`) with the Bot
Framework error code as the reading.

Never logged or put in an error: the client secret, the bot token, the inbound JWT.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import mimetypes
import re
import urllib.parse
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

import httpx

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.gateway.adapters._entra import BOT_FRAMEWORK_SCOPE, ClientCredentials, TokenError
from indus.gateway.adapters._http import (
    BadPayload,
    CallbackListener,
    CallbackRequest,
    ListenerSettings,
    Reply,
    SeenIds,
    failure_from_exception,
    failure_from_status,
)
from indus.gateway.adapters.base import (
    CALLBACK_MAX_BYTES,
    Adapter,
    AdapterConnectError,
    ApprovalPrompt,
    Capabilities,
    ChatInfo,
    ClarifyPrompt,
    FetcherSeat,
    MediaKind,
    approval_callback,
    clarify_callback,
)
from indus.gateway.adapters.teams.botauth import (
    OPENID_METADATA_URL,
    BotKeys,
    BotTokenVerifier,
    FetchJson,
    TokenRejected,
)
from indus.gateway.adapters.teams.conversations import (
    DEFAULT_SERVICE_URL,
    SERVICE_HOSTS,
    ConversationBook,
    ConversationRef,
    activity_id_ok,
    conversation_ok,
    join_conversation,
    service_url_ok,
    split_conversation,
    valid_host,
)
from indus.gateway.core.events import Attachment, ChatType, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult, is_ambiguous_timeout
from indus.gateway.outbound.chunker import code_points, split_message

__all__ = [
    "CARD_TYPE",
    "DEFAULT_PORT",
    "MAX_BODY_BYTES",
    "MAX_LEN",
    "WEBHOOK_PATH",
    "TeamsAdapter",
    "TeamsOptions",
    "media_access",
]

log = logging.getLogger(__name__)

DEFAULT_PORT = 3978
WEBHOOK_PATH = "/api/messages"
MAX_BODY_BYTES = 1_048_576
MAX_LEN = 28_000
CARD_TYPE = "application/vnd.microsoft.card.adaptive"
FILE_INFO_TYPE = "application/vnd.microsoft.teams.file.download.info"
CALLBACK_KEY = "indus_callback"
# Activity ids seen within this window are dropped (Bot Connector retries reuse them).
DEDUPE_TTL_S = 3600.0
API_TIMEOUT_S = 30.0
DOWNLOAD_TIMEOUT_S = 60.0
MAX_ATTACHMENTS = 10
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_DOC_BYTES = 256 * 1024
# Teams shows images sent inline (base64) up to 1 MiB; other files need the file-consent flow.
MAX_INLINE_IMAGE_BYTES = 1024 * 1024
# Our recent activity ids, so a reply to one of them counts as addressed to us.
SENT_MEMORY = 500
CARD_TEXT_LIMIT = 4000

_CHAT_TYPES: dict[str, ChatType] = {"personal": "dm", "groupChat": "group", "channel": "channel"}
_AT_TAG = re.compile(r"<at>(.*?)</at>", re.IGNORECASE | re.DOTALL)
_VOICE_TYPES = frozenset({"audio/ogg", "audio/opus", "audio/webm"})
# Order wins when an activity carries several kinds: a document first (its text is read), then
# pictures, video, voice and other audio.
_KIND_ORDER = (
    MessageKind.DOCUMENT,
    MessageKind.PHOTO,
    MessageKind.VIDEO,
    MessageKind.VOICE,
    MessageKind.AUDIO,
)
_TOKEN_MEDIA_SUFFIXES = (".asm.skype.com",)
_OPEN_MEDIA_SUFFIXES = (".sharepoint.com", ".sharepoint.us")
_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_STYLES = {"primary": "positive", "danger": "destructive"}


# -- settings -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TeamsOptions:
    """``platforms.teams`` keys beyond the listener's ``webhook_*`` ones."""

    service_url: str = DEFAULT_SERVICE_URL
    extra_service_hosts: tuple[str, ...] = ()
    openid_metadata_url: str = OPENID_METADATA_URL

    @classmethod
    def read(cls, extra: Mapping[str, Any]) -> TeamsOptions:
        """The options in ``extra``; ``ValueError`` for a value that cannot be used safely."""
        raw_hosts = extra.get("extra_service_hosts") or ()
        if isinstance(raw_hosts, str):
            raw_hosts = raw_hosts.split(",")
        hosts: list[str] = []
        for raw in raw_hosts:  # pyright: ignore[reportUnknownVariableType]
            host = str(raw).strip().lower()  # pyright: ignore[reportUnknownArgumentType]
            if not host:
                continue
            if not valid_host(host):
                raise ValueError(f"extra_service_hosts: {host!r} is not a host name")
            hosts.append(host)
        configured = str(extra.get("service_url") or "").strip() or DEFAULT_SERVICE_URL
        service = service_url_ok(configured, hosts)
        if service is None:
            raise ValueError(
                "service_url must be an https Bot Connector URL on the Microsoft list "
                f"({', '.join(sorted(SERVICE_HOSTS))}) or in extra_service_hosts"
            )
        metadata = str(extra.get("openid_metadata_url") or "").strip() or OPENID_METADATA_URL
        if not metadata.startswith("https://"):
            raise ValueError("openid_metadata_url must be an https URL")
        return cls(service, tuple(hosts), metadata)


def media_access(url: str, extra_hosts: Sequence[str] = ()) -> Literal["token", "open"] | None:
    """How an attachment URL may be fetched: with the bot token (Bot Connector and Teams media
    hosts), without one (pre-authorised SharePoint links), or not at all."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if host in SERVICE_HOSTS or host in extra_hosts or host.endswith(_TOKEN_MEDIA_SUFFIXES):
        return "token"
    if host.endswith(_OPEN_MEDIA_SUFFIXES):
        return "open"
    return None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _text(value: object, limit: int = 200) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# -- the adapter ----------------------------------------------------------------------------------


@dataclass
class _Parsed:
    """What one verified activity says, before it becomes an event."""

    activity: dict[str, Any]
    kind: str
    conversation: str
    service_url: str
    sender: dict[str, Any] = field(default_factory=dict[str, Any])


class TeamsAdapter(Adapter):
    platform: ClassVar[str] = "teams"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_LEN,
        len_fn=code_points,
        markup="markdown",
        splits_natively=True,
        supports_edit=True,
        supports_buttons=True,
        push_capable=True,
    )
    # No vendor library: the Bot Framework is spoken over httpx (P15-01 drift check).
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "httpx:AsyncClient": ("request", "post", "stream", "aclose"),
        "httpx:Response": ("json", "aiter_bytes"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        app_id: str,
        app_secret: str,
        tenant_id: str,
        listener: ListenerSettings,
        options: TeamsOptions | None = None,
        book: ConversationBook | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        key_fetch: FetchJson | None = None,
        send_only: bool = False,
        serve: bool = True,
        **kw: Any,
    ) -> None:
        super().__init__(clock=clock, secrets=(app_secret,), **kw)
        self.app_id = app_id.strip()
        self.options = options or TeamsOptions()
        self.credentials = ClientCredentials(
            tenant_id=tenant_id,
            client_id=self.app_id,
            client_secret=app_secret,
            scope=BOT_FRAMEWORK_SCOPE,
            clock=clock,
        )
        self.book = (
            book
            if book is not None
            else ConversationBook(None, extra_hosts=self.options.extra_service_hosts)
        )
        self.send_only = send_only
        self._transport = transport
        self._proxy = proxy
        self._client: httpx.AsyncClient | None = None
        self.keys = BotKeys(
            key_fetch or self._fetch_json, clock, metadata_url=self.options.openid_metadata_url
        )
        self.verifier = BotTokenVerifier(self.keys, clock, app_id=self.app_id or "-")
        self.seen = SeenIds(clock, DEDUPE_TTL_S, max_entries=5000)
        self.listener = CallbackListener(
            listener, name=self.platform, clock=clock, scrub=self.scrub, serve=serve
        )
        self.listener.route(listener.path, self._callback, methods=("POST",))
        self._own_ids = frozenset({self.app_id, f"28:{self.app_id}"} - {"", "28:"})
        self._sent: deque[str] = deque(maxlen=SENT_MEMORY)

    def scrub(self, text: str) -> str:
        clean = super().scrub(text)
        token = self.credentials.held()
        return clean.replace(token, "****") if token else clean

    @property
    def listening(self) -> bool:
        return self.listener.accepting

    # -- lifecycle ---------------------------------------------------------------------------------

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            proxy=None if self._transport is not None else self._proxy,
            timeout=API_TIMEOUT_S,
            follow_redirects=False,
        )

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("the Teams adapter is not connected")
        return self._client

    async def _open(self, is_reconnect: bool) -> None:
        if not self.credentials.configured:
            raise AdapterConnectError(
                "TEAMS_CLIENT_ID, TEAMS_CLIENT_SECRET and TEAMS_TENANT_ID are all required",
                retryable=False,
                code="teams_config",
            )
        client = self._new_client()
        self._client = client
        try:
            await self.credentials.token(client, fresh=True)
            await self.book.load()
            if not self.send_only:
                await self.keys.refresh()
                await self.listener.start()
        except TokenError as exc:
            await self._drop_client()
            raise AdapterConnectError(
                f"Teams sign-in failed: {exc}",
                retryable=exc.retryable,
                code="connect_failed" if exc.retryable else "teams_auth",
            ) from None
        except BaseException:
            await self.listener.stop()
            await self._drop_client()
            raise

    async def _drop_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def _close(self) -> None:
        await self.listener.stop()
        with contextlib.suppress(Exception):
            await self.book.flush()
        await self._drop_client()
        self.credentials.invalidate()

    async def _fetch_json(self, url: str) -> Any:
        """GET one JSON document (OpenID metadata, keys) over the adapter's client."""
        async with self._http().stream("GET", url, follow_redirects=False) as answer:
            if answer.status_code != 200:
                raise ValueError(f"HTTP {answer.status_code} from the key service")
            body = bytearray()
            async for chunk in answer.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_DOC_BYTES:
                    raise ValueError("the key document is too large")
        return httpx.Response(200, content=bytes(body)).json()

    # -- inbound -----------------------------------------------------------------------------------

    async def _callback(self, request: CallbackRequest) -> Reply:
        try:
            verified = await self.verifier.verify(request.header("authorization"))
        except TokenRejected as exc:
            log.info("teams: request refused (%s)", exc.reason)
            return Reply.json({"error": "unauthorized"}, 401)
        try:
            data = request.json()
        except BadPayload:
            return Reply.json({"error": "bad payload"}, 400)
        if not isinstance(data, dict):
            return Reply.json({"error": "bad payload"}, 400)
        activity: dict[str, Any] = data  # pyright: ignore[reportUnknownVariableType]
        channel = _text(activity.get("channelId"), 64)
        if verified.endorsements is not None and channel not in verified.endorsements:
            log.info("teams: request refused (key not endorsed for channel %r)", channel)
            return Reply.json({"error": "unauthorized"}, 401)
        raw_service = _text(activity.get("serviceUrl"), 512)
        claimed = verified.service_url
        if claimed is not None and claimed.rstrip("/") != raw_service.rstrip("/"):
            log.info("teams: request refused (serviceUrl differs from the token's)")
            return Reply.json({"error": "unauthorized"}, 401)
        parsed = self._parse(activity, raw_service)
        if parsed is None:
            return Reply.json({"error": "bad activity"}, 400)
        return await self._dispatch(parsed)

    def _parse(self, activity: dict[str, Any], raw_service: str) -> _Parsed | None:
        service = service_url_ok(raw_service, self.options.extra_service_hosts)
        if service is None:
            log.warning("teams: activity dropped: its serviceUrl is not on the Bot Connector list")
            return None
        conversation = _text(_dict(activity.get("conversation")).get("id"), 512)
        if not conversation_ok(conversation):
            log.warning("teams: activity dropped: malformed conversation id")
            return None
        kind = _text(activity.get("type"), 64)
        return _Parsed(activity, kind, conversation, service, _dict(activity.get("from")))

    async def _dispatch(self, parsed: _Parsed) -> Reply:
        activity = parsed.activity
        self._remember(parsed)
        if parsed.kind == "invoke":
            return self._on_invoke(parsed)
        if parsed.kind not in ("message", "messageUpdate"):
            return Reply.empty(200)
        if parsed.kind == "messageUpdate":
            event_type = _text(_dict(activity.get("channelData")).get("eventType"), 64)
            if event_type and event_type != "editMessage":
                return Reply.empty(200)
        if self._from_us(parsed):
            return Reply.empty(200)
        activity_id = _text(activity.get("id"), 200)
        if activity_id and not self.seen.first_time(self._receipt(parsed, activity_id)):
            return Reply.empty(200)
        event = self._message_event(parsed)
        if event is not None:
            self.listener.background(self.emit(event))
        return Reply.empty(200)

    def _receipt(self, parsed: _Parsed, activity_id: str) -> str:
        if parsed.kind == "messageUpdate":
            text = _text(parsed.activity.get("text"), 100_000)
            digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
            return f"update:{parsed.conversation}:{activity_id}:{digest}"
        return f"{parsed.kind}:{parsed.conversation}:{activity_id}"

    def _remember(self, parsed: _Parsed) -> None:
        conversation = _dict(parsed.activity.get("conversation"))
        tenant = _text(conversation.get("tenantId"), 64) or _text(
            _dict(_dict(parsed.activity.get("channelData")).get("tenant")).get("id"), 64
        )
        base, _ = split_conversation(parsed.conversation)
        self.book.remember(
            ConversationRef(
                base,
                parsed.service_url,
                _text(conversation.get("conversationType"), 32),
                tenant,
                _text(conversation.get("name"), 200),
            )
        )

    def _from_us(self, parsed: _Parsed) -> bool:
        sender = _text(parsed.sender.get("id"), 200)
        recipient = _text(_dict(parsed.activity.get("recipient")).get("id"), 200)
        return bool(sender) and (sender in self._own_ids or sender == recipient)

    def _source(self, parsed: _Parsed) -> Source:
        activity = parsed.activity
        conversation = _dict(activity.get("conversation"))
        base, thread = split_conversation(parsed.conversation)
        chat_type = _CHAT_TYPES.get(_text(conversation.get("conversationType"), 32), "dm")
        if thread is not None:
            chat_type = "thread"
        sender = parsed.sender
        raw_id = _text(sender.get("id"), 200)
        aad = _text(sender.get("aadObjectId"), 64)
        tenant = _text(conversation.get("tenantId"), 64) or _text(
            _dict(_dict(activity.get("channelData")).get("tenant")).get("id"), 64
        )
        return Source(
            platform=self.platform,
            chat_id=base,
            chat_type=chat_type,
            chat_name=_text(conversation.get("name"), 200) or None,
            user_id=aad or raw_id or None,
            user_name=_text(sender.get("name"), 200) or None,
            thread_id=thread,
            user_id_alt=raw_id if aad and raw_id else None,
            is_bot=_text(sender.get("role"), 16).lower() == "bot",
            scope_id=tenant or None,
            parent_chat_id=base if thread is not None else None,
        )

    def _mentions_us(self, activity: dict[str, Any]) -> tuple[bool, list[str]]:
        """Whether a mention entity names this bot, and the ``<at>`` texts of those mentions."""
        recipient = _text(_dict(activity.get("recipient")).get("id"), 200)
        ours = self._own_ids | ({recipient} if recipient else set())
        tags: list[str] = []
        found = False
        for raw in activity.get("entities") or ():
            entity = _dict(raw)
            if entity.get("type") != "mention":
                continue
            if _text(_dict(entity.get("mentioned")).get("id"), 200) in ours:
                found = True
                tag = _text(entity.get("text"), 400)
                if tag:
                    tags.append(tag)
        return found, tags

    def _message_event(self, parsed: _Parsed) -> InboundEvent | None:
        activity = parsed.activity
        source = self._source(parsed)
        value = _dict(activity.get("value"))
        callback = _text(value.get(CALLBACK_KEY), 128)
        text = _text(activity.get("text"), 200_000)
        if parsed.kind == "message" and not text.strip() and callback:
            # A press on an ``Action.Submit`` button arrives as a message carrying our data.
            return self._button_event(parsed, callback)
        mentioned, tags = self._mentions_us(activity)
        for tag in tags:
            text = text.replace(tag, " ")
        text = _AT_TAG.sub(lambda m: m.group(1), text).strip()
        metadata: dict[str, Any] = {}
        if mentioned:
            metadata["mentioned"] = True
        if parsed.kind == "messageUpdate":
            metadata["edited"] = True
        media, kind = self._attachments(activity)
        reply_to = _text(activity.get("replyToId"), 200) or None
        own = (reply_to is not None and reply_to in self._sent) or (
            source.thread_id is not None and source.thread_id in self._sent
        )
        return InboundEvent(
            source=source,
            text=text,
            kind=kind,
            message_id=_text(activity.get("id"), 200) or None,
            media=media,
            reply_to_id=reply_to,
            reply_to_is_own=own,
            metadata=metadata,
            timestamp=_timestamp(activity.get("timestamp")),
        )

    def _button_event(self, parsed: _Parsed, callback: str) -> InboundEvent | None:
        if len(callback.encode("utf-8")) > CALLBACK_MAX_BYTES:
            return None
        activity = parsed.activity
        return InboundEvent(
            source=self._source(parsed),
            text="",
            message_id=_text(activity.get("id"), 200) or None,
            reply_to_id=_text(activity.get("replyToId"), 200) or None,
            metadata={"button": callback},
            timestamp=_timestamp(activity.get("timestamp")),
        )

    def _on_invoke(self, parsed: _Parsed) -> Reply:
        activity = parsed.activity
        if _text(activity.get("name"), 64) != "adaptiveCard/action":
            return Reply.json({}, 200)
        action = _dict(_dict(activity.get("value")).get("action"))
        callback = _text(_dict(action.get("data")).get(CALLBACK_KEY), 128)
        activity_id = _text(activity.get("id"), 200)
        event = None
        fresh = bool(callback) and (
            not activity_id or self.seen.first_time(self._receipt(parsed, activity_id))
        )
        if fresh and not self._from_us(parsed):
            event = self._button_event(parsed, callback)
        if event is not None:
            self.listener.background(self.emit(event))
        answer = "Received." if event is not None or callback else "This button is not ours."
        return Reply.json(
            {
                "statusCode": 200,
                "type": "application/vnd.microsoft.activity.message",
                "value": answer,
            }
        )

    def _attachments(self, activity: dict[str, Any]) -> tuple[tuple[Attachment, ...], MessageKind]:
        found: list[tuple[MessageKind, Attachment]] = []
        for raw in list(activity.get("attachments") or ())[:MAX_ATTACHMENTS]:
            item = self._attachment(_dict(raw))
            if item is not None:
                found.append(item)
        kinds = {kind for kind, _ in found}
        kind = next((k for k in _KIND_ORDER if k in kinds), MessageKind.TEXT)
        return tuple(a for _, a in found), kind

    def _attachment(self, raw: dict[str, Any]) -> tuple[MessageKind, Attachment] | None:
        content_type = _text(raw.get("contentType"), 200).lower()
        url = _text(raw.get("contentUrl"), 2048)
        name = _text(raw.get("name"), 200)
        if content_type.startswith("application/vnd.microsoft.card"):
            return None
        if content_type == FILE_INFO_TYPE:
            info = _dict(raw.get("content"))
            url = _text(info.get("downloadUrl"), 2048)
            file_type = _text(info.get("fileType"), 16).lstrip(".")
            guess = name or (f"file.{file_type}" if file_type else "")
            mime = mimetypes.guess_type(guess)[0] or "application/octet-stream"
            kind = MessageKind.DOCUMENT
        elif not url:
            return None  # the text/html mirror of the message body, and similar
        elif content_type.startswith("image/"):
            mime, kind = content_type, MessageKind.PHOTO
        elif content_type.startswith("video/"):
            mime, kind = content_type, MessageKind.VIDEO
        elif content_type.startswith("audio/"):
            voice = content_type.split(";")[0] in _VOICE_TYPES
            mime, kind = content_type, (MessageKind.VOICE if voice else MessageKind.AUDIO)
        else:
            mime, kind = content_type or "application/octet-stream", MessageKind.DOCUMENT
        if not url or media_access(url, self.options.extra_service_hosts) is None:
            log.info("teams: attachment skipped: not on a Microsoft media host")
            return None
        return kind, Attachment(ref=url, mime=mime)

    # -- media for the intake ----------------------------------------------------------------------

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The bytes of an inbound attachment (registered by `register_fetchers`)."""
        access = media_access(attachment.ref, self.options.extra_service_hosts)
        if access is None:
            raise ValueError("not a Teams attachment URL")
        client = self._http()
        headers: dict[str, str] = {}
        if access == "token":
            headers["Authorization"] = f"Bearer {await self.credentials.token(client)}"
        async with client.stream(
            "GET",
            attachment.ref,
            headers=headers,
            follow_redirects=False,
            timeout=DOWNLOAD_TIMEOUT_S,
        ) as answer:
            if answer.status_code != 200:
                raise ValueError(f"the attachment download answered HTTP {answer.status_code}")
            total = 0
            async for chunk in answer.aiter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("the attachment is too large")
                yield chunk

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        return seat.register_fetcher(self.platform, self.fetch_media)

    # -- outbound ----------------------------------------------------------------------------------

    def _service_for(self, chat_id: str) -> str | None:
        ref = self.book.get(chat_id)
        url = ref.service_url if ref is not None else self.options.service_url
        return service_url_ok(url, self.options.extra_service_hosts)

    async def _bearer(self, *, fresh: bool = False) -> str | SendResult:
        try:
            return await self.credentials.token(self._http(), fresh=fresh)
        except TokenError as exc:
            if exc.retryable:
                return SendResult.failure(f"connect error: {exc}")
            self.report_fatal("teams_auth", f"Teams refused the app credentials: {exc}")
            return SendResult.failure(f"Teams refused the app credentials: {exc}")

    async def _activity_call(
        self,
        method: str,
        conversation: str,
        body: dict[str, Any],
        *,
        activity_id: str | None = None,
    ) -> SendResult:
        if not conversation_ok(conversation):
            return SendResult.failure("not a Teams conversation id")
        if activity_id is not None and not activity_id_ok(activity_id):
            return SendResult.failure("not a Teams activity id")
        service = self._service_for(conversation)
        if service is None:
            return SendResult.failure("the conversation's service URL is not on the allowed list")
        path = urllib.parse.quote(conversation, safe=":@._-;=")
        url = f"{service}v3/conversations/{path}/activities"
        if activity_id is not None:
            url = f"{url}/{urllib.parse.quote(activity_id, safe=':@._-|')}"
        answer: httpx.Response | None = None
        for fresh in (False, True):
            token = await self._bearer(fresh=fresh)
            if isinstance(token, SendResult):
                return token
            try:
                answer = await self._http().request(
                    method, url, json=body, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                return failure_from_exception(exc)
            if answer.status_code != 401:
                break
            self.credentials.invalidate()
        assert answer is not None
        if answer.status_code >= 400:
            return self._failure(answer)
        try:
            data = _dict(answer.json()) if answer.content else {}
        except ValueError:
            data = {}
        sent_id = _text(data.get("id"), 200) or (activity_id or "")
        if sent_id:
            self._sent.append(sent_id)
        return SendResult.sent(sent_id)

    def _failure(self, answer: httpx.Response) -> SendResult:
        code, message = _bf_error(answer)
        status = answer.status_code
        reading: str | None = None
        if status == 404:
            reading = "chat not found" if code == "ConversationNotFound" else "message not found"
        elif code in ("MessageSizeTooBig", "MessageTooLarge"):
            reading = "too long"
        text = f"{code}: {message}" if code else message
        return failure_from_status(
            status, text, answer.headers, now=self.clock.now(), reading=reading
        )

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        conversation = join_conversation(chat_id, str(meta.get("thread_id") or "") or None)
        text_format = "plain" if meta.get("plain") else "markdown"
        chunked = split_message(text, MAX_LEN, len_fn=code_points)
        sent: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            body: dict[str, Any] = {
                "type": "message",
                "text": chunk.text,
                "textFormat": text_format,
            }
            if reply_to and n == 0:
                body["replyToId"] = reply_to
            result = await self._activity_call("POST", conversation, body)
            if not result.ok:
                if not sent:
                    return result
                ambiguous = result.maybe_delivered or is_ambiguous_timeout(text=result.error or "")
                return replace(
                    result,
                    message_id=sent[-1],
                    continuation_ids=tuple(sent[:-1]),
                    partial=True,
                    remainder=None if ambiguous else chunked.remainder(n),
                )
            sent.append(result.message_id or "")
        return SendResult.sent(sent[-1], tuple(sent[:-1]))

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        if code_points(text) > MAX_LEN:
            return SendResult.failure("message is too long for one Teams edit")
        conversation = join_conversation(chat_id, str(meta.get("thread_id") or "") or None)
        body = {"type": "message", "id": message_id, "text": text, "textFormat": "markdown"}
        result = await self._activity_call("PUT", conversation, body, activity_id=message_id)
        return SendResult.sent(message_id) if result.ok else result

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        conversation = join_conversation(chat_id, str(meta.get("thread_id") or "") or None)
        result = await self._activity_call("POST", conversation, {"type": "typing"})
        if not result.ok:
            log.debug("teams: typing failed: %s", self.scrub(result.error or ""))

    async def _send_card(
        self, chat_id: str, card: dict[str, Any], fallback: str, meta: dict[str, Any]
    ) -> SendResult:
        conversation = join_conversation(chat_id, str(meta.get("thread_id") or "") or None)
        body = {
            "type": "message",
            "attachments": [{"contentType": CARD_TYPE, "content": card}],
        }
        result = await self._activity_call("POST", conversation, body)
        if result.ok or result.retryable or result.maybe_delivered or result.chat_unreachable:
            return result
        log.info("teams: card refused (%s); sending the text card", self.scrub(result.error or ""))
        return await self._send(chat_id, fallback, None, meta)

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        actions: list[dict[str, Any]] = []
        for action in prompt.actions:
            entry: dict[str, Any] = {
                "type": "Action.Execute",
                "title": action.label,
                "verb": "indus.approval",
                "data": {CALLBACK_KEY: approval_callback(prompt.request_id, action.choice)},
            }
            if action.style in _STYLES:
                entry["style"] = _STYLES[action.style]
            actions.append(entry)
        card = _card(prompt.text, prompt.fallback_text, actions)
        return await self._send_card(chat_id, card, prompt.fallback_text, meta)

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        actions = [
            {
                "type": "Action.Execute",
                "title": choice[:80],
                "verb": "indus.clarify",
                "data": {CALLBACK_KEY: clarify_callback(prompt.clarify_id, n)},
            }
            for n, choice in enumerate(prompt.choices)
        ]
        fallback = prompt.fallback_text or prompt.question
        card = _card(prompt.question, fallback, actions)
        return await self._send_card(chat_id, card, fallback, meta)

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        """Images go inline (a base64 data URI, at most 1 MiB); other files are refused: Teams
        takes them only through the file-consent card and the user's OneDrive."""
        mime = mimetypes.guess_type(path)[0] or ""
        if kind != "image" or not mime.startswith("image/"):
            return SendResult.refused(f"sending {kind}s")
        file = Path(path)
        size = (await run_blocking(file.stat, label="teams image")).st_size
        if size > MAX_INLINE_IMAGE_BYTES:
            return SendResult.failure(
                f"the image is too large for Teams ({size} bytes, the cap is "
                f"{MAX_INLINE_IMAGE_BYTES})"
            )
        data = await run_blocking(file.read_bytes, label="teams image")
        uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        body: dict[str, Any] = {
            "type": "message",
            "attachments": [{"contentType": mime, "contentUrl": uri, "name": file.name[:200]}],
        }
        if caption:
            body["text"] = caption
            body["textFormat"] = "markdown"
        conversation = join_conversation(chat_id, str(meta.get("thread_id") or "") or None)
        return await self._activity_call("POST", conversation, body)

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        ref = self.book.get(chat_id)
        if ref is None:
            return ChatInfo(name=chat_id)
        return ChatInfo(name=ref.name or chat_id, type=_CHAT_TYPES.get(ref.kind))


def _card(text: str, fallback: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "fallbackText": fallback[:CARD_TEXT_LIMIT],
        "body": [{"type": "TextBlock", "text": text[:CARD_TEXT_LIMIT], "wrap": True}],
        "actions": actions,
    }


def _bf_error(answer: httpx.Response) -> tuple[str, str]:
    """The Bot Framework error code (vetted) and message (shortened) of an error answer."""
    try:
        data: Any = answer.json()
    except ValueError:
        return "", " ".join(answer.text.split())[:300]
    error = _dict(_dict(data).get("error"))
    code = _text(error.get("code"), 64)
    message = " ".join(_text(error.get("message"), 1000).split())[:300]
    return (code if _ERROR_CODE.match(code) else ""), message
