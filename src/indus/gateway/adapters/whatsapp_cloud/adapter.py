"""The WhatsApp Business Cloud adapter (P16-04): Meta's Graph API out, signed webhooks in.

Connect: ``GET /<ver>/<phone-number-id>`` with the access token (a refused token or an unknown
phone-number id is a fatal, non-retryable connect error), then — unless the open is send-only
(``indus send``) — the callback listener from the P16-01 kit (loopback unless the operator opens
it). Receiving needs the app secret: without it the connect is refused, since an unsigned webhook
would let anyone inject messages.

Webhook: ``GET`` is Meta's subscription handshake (``hub.verify_token`` compared in constant time,
``hub.challenge`` echoed). ``POST`` bodies are checked against ``X-Hub-Signature-256`` (HMAC-SHA256
of the raw body with the app secret) before anything is parsed; then each message of each
``messages`` change addressed to our phone-number id (and WABA, when configured) is deduplicated
by its ``wamid`` (Meta retries for days) and normalised; the events are emitted in order after the
200 went out. Status receipts are not events.

Sending: text in pieces of ≤4,096 code points, each piece turned from Markdown into WhatsApp's
markup (``markup.to_whatsapp``) unless the send is plain; the first piece quotes ``reply_to``.
Approval and clarify prompts are interactive messages: ≤3 choices as reply buttons, 4–10 as a
list, otherwise (or when the prompt would not fit the 1,024-character body) the text card. Media
are uploaded to ``/media`` and sent by id. Typing is Meta's read receipt with a typing indicator,
on the chat's last inbound message.

Failures: Graph's error codes are read into typed results. Outside the 24-hour customer-service
window (131047) a send is ``forbidden`` with that reason and final (no retry, no plain resend, no
"delivery failed" notice); a refused token (190 / HTTP 401) is final and reported as a fatal
platform state; throughput and pair limits are rate limits. Phone numbers are masked in every log
line and error text.
"""

from __future__ import annotations

import contextlib
import logging
import mimetypes
import re
import urllib.parse
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

import httpx

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.gateway.adapters._http import (
    BadPayload,
    CallbackListener,
    CallbackRequest,
    ListenerSettings,
    Reply,
    SeenIds,
    constant_time_equal,
    failure_from_exception,
    failure_from_status,
    verify_hmac_sha256_hex,
)
from indus.gateway.adapters._identity import mask_number, mask_numbers
from indus.gateway.adapters.base import (
    REPLY_TO_KEY,
    Adapter,
    AdapterConnectError,
    ApprovalPrompt,
    Capabilities,
    ChatInfo,
    ClarifyPrompt,
    FetcherSeat,
    InboundSink,
    MediaKind,
    approval_callback,
    clarify_callback,
)
from indus.gateway.adapters.whatsapp_cloud.markup import to_whatsapp
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult
from indus.gateway.outbound.chunker import code_points, plain_marker, split_message

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "DEFAULT_API_VERSION",
    "DEFAULT_PATH",
    "DEFAULT_PORT",
    "GRAPH_BASE",
    "GRAPH_ROUTES",
    "MAX_BODY_BYTES",
    "MAX_LEN",
    "CloudSettings",
    "WhatsAppCloudAdapter",
    "mask_number",
]

log = logging.getLogger(__name__)

GRAPH_BASE: Final = "https://graph.facebook.com"
DEFAULT_API_VERSION: Final = "v23.0"
DEFAULT_PORT: Final = 8090
DEFAULT_PATH: Final = "/whatsapp/webhook"
MAX_BODY_BYTES: Final = 3 * 1024 * 1024
MAX_LEN: Final = 4096
# Meta retries an unacknowledged webhook for up to a week; the practical repeat window is short.
WAMID_CACHE: Final = 5000
WAMID_TTL_S: Final = 7 * 24 * 3600.0
INTERACTIVE_CACHE: Final = 1000
# Interactive message limits (Cloud API reference).
BODY_MAX: Final = 1024
MAX_BUTTONS: Final = 3
MAX_ROWS: Final = 10
BUTTON_TITLE_MAX: Final = 20
ROW_TITLE_MAX: Final = 24
ROW_DESCRIPTION_MAX: Final = 72
LIST_BUTTON: Final = "Choose"
CAPTION_MAX: Final = 1024
# Upload caps per media type (Cloud API media reference) and our download cap.
MEDIA_CAPS: Final[Mapping[str, int]] = {
    "image": 5 * 1024 * 1024,
    "video": 16 * 1024 * 1024,
    "audio": 16 * 1024 * 1024,
    "document": 100 * 1024 * 1024,
}
DOWNLOAD_MAX: Final = 100 * 1024 * 1024
REF_PREFIX: Final = "wac:"
# Meta's hosts that may receive the access token when downloading media.
MEDIA_HOSTS: Final = ("fbsbx.com", "facebook.com", "whatsapp.net")

# The Graph routes this adapter calls (``{version}`` is the configured API version).
GRAPH_ROUTES: Final = (
    ("GET", "/{version}/{phone_number_id}"),
    ("POST", "/{version}/{phone_number_id}/messages"),
    ("POST", "/{version}/{phone_number_id}/media"),
    ("GET", "/{version}/{media_id}"),
)

# Graph error codes read into typed results.
CODE_TOKEN: Final = 190
CODE_WINDOW: Final = 131047
CODE_UNDELIVERABLE: Final = 131026
RATE_CODES: Final = frozenset({4, 613, 80007, 130429, 131048, 131056})

WINDOW_READING: Final = (
    "forbidden: outside WhatsApp's 24-hour customer-service window; only an approved template "
    "can reach this person until they write again"
)
TOKEN_READING: Final = "the access token was refused"
# ``SendResult.extra`` key marking failures no retry or plain resend can fix.
FINAL_KEY: Final = "whatsapp_cloud_final"

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_API_VERSION = re.compile(r"^v\d{1,3}\.\d{1,2}$")
_IGNORED_KINDS = frozenset({"reaction", "system", "unsupported", "unknown", "ephemeral"})
_MEDIA_KINDS: Final[Mapping[str, MessageKind]] = {
    "image": MessageKind.PHOTO,
    "video": MessageKind.VIDEO,
    "audio": MessageKind.AUDIO,
    "document": MessageKind.DOCUMENT,
    "sticker": MessageKind.STICKER,
}
_OUTBOUND_KIND: Final[Mapping[str, str]] = {
    "image": "image",
    "video": "video",
    "voice": "audio",
    "file": "document",
    "document": "document",
}
# Types Meta takes per media kind; anything else goes as a document.
_ACCEPTED_MIME: Final[Mapping[str, frozenset[str]]] = {
    "image": frozenset({"image/jpeg", "image/png"}),
    "video": frozenset({"video/mp4", "video/3gpp"}),
    "audio": frozenset({"audio/aac", "audio/amr", "audio/mpeg", "audio/mp4", "audio/ogg"}),
}


def _digits(value: object) -> str:
    return "".join(c for c in str(value or "") if c.isdigit())


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownArgumentType]


def _dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_dict(item) for item in value if isinstance(item, dict)]  # pyright: ignore[reportUnknownVariableType]


def _label(text: str, limit: int) -> str:
    clean = " ".join(str(text).split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _remember[K, V](cache: OrderedDict[K, V], key: K, value: V, cap: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > cap:
        cache.popitem(last=False)


@dataclass(frozen=True)
class CloudSettings:
    """What the adapter needs from Meta (credentials masked in reprs)."""

    access_token: str
    phone_number_id: str
    app_secret: str = ""
    verify_token: str = ""
    waba_id: str = ""
    api_version: str = DEFAULT_API_VERSION
    base_url: str = GRAPH_BASE

    def __repr__(self) -> str:
        return (
            f"CloudSettings(phone_number_id=…, api_version={self.api_version!r}, "
            f"base_url={self.base_url!r})"
        )

    def check(self) -> None:
        if not self.access_token or not self.phone_number_id:
            raise ValueError("the access token and the phone-number id are required")
        if not _SAFE_ID.match(self.phone_number_id):
            raise ValueError("the phone-number id must be letters, digits, '.', '_' or '-'")
        if not _API_VERSION.match(self.api_version):
            raise ValueError(f"api_version {self.api_version!r} is not a Graph version like v23.0")
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an http(s) URL")


class WhatsAppCloudAdapter(Adapter):
    platform: ClassVar[str] = "whatsapp_cloud"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_LEN,
        len_fn=code_points,
        # The sender's Markdown pass (tables to code blocks); WhatsApp markup is applied per piece.
        markup="markdown",
        splits_natively=True,
        supports_buttons=True,
        inline_wait_cap=5.0,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "httpx:AsyncClient": ("get", "post", "stream", "aclose"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        cloud: CloudSettings,
        listener: ListenerSettings,
        client_factory: Callable[[], httpx.AsyncClient],
        serve: bool = True,
        send_only: bool = False,
        settings: PlatformSettings | None = None,
        sink: InboundSink | None = None,
    ) -> None:
        cloud.check()
        secrets = (cloud.access_token, cloud.app_secret, cloud.verify_token)
        super().__init__(clock=clock, settings=settings, secrets=secrets, sink=sink)
        self.cloud = cloud
        # ``indus send`` (``AdapterConfig.send_only``): the credential check only, no listener.
        self.send_only = send_only
        self._client_factory = client_factory
        self._client: httpx.AsyncClient | None = None
        self.listener = CallbackListener(
            listener, name=self.platform, clock=clock, scrub=self.scrub, serve=serve
        )
        self.listener.route(listener.path, self._webhook, methods=("GET", "POST"))
        self.seen = SeenIds(clock, WAMID_TTL_S, WAMID_CACHE)
        # Our interactive messages (wamid → the button ids they offered) and, per chat, the last
        # inbound wamid (typing and read receipts attach to it).
        self._prompts: OrderedDict[str, frozenset[str]] = OrderedDict()
        self._last_inbound: OrderedDict[str, str] = OrderedDict()
        self.display_number = ""

    def scrub(self, text: str) -> str:
        return mask_numbers(super().scrub(text))

    # -- lifecycle ---------------------------------------------------------------------------------

    def _path(self, *parts: str) -> str:
        return "/" + "/".join((self.cloud.api_version, *parts))

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cloud.access_token}"}

    async def _open(self, is_reconnect: bool) -> None:
        if not self.send_only and not self.cloud.app_secret:
            raise AdapterConnectError(
                "WhatsApp Cloud needs WHATSAPP_CLOUD_APP_SECRET to check webhook signatures; "
                "without it anyone could post messages to the bot",
                retryable=False,
                code="whatsapp_cloud_app_secret_missing",
            )
        client = self._client_factory()
        try:
            await self._check_credentials(client)
            if not self.send_only:
                if not self.cloud.verify_token:
                    log.warning(
                        "whatsapp_cloud: WHATSAPP_CLOUD_VERIFY_TOKEN is not set; Meta's webhook "
                        "subscription check will be refused"
                    )
                await self.listener.start()
        except BaseException:
            await client.aclose()
            raise
        self._client = client

    async def _check_credentials(self, client: httpx.AsyncClient) -> None:
        try:
            answer = await client.get(
                self._path(self.cloud.phone_number_id),
                params={"fields": "id,display_phone_number,verified_name"},
                headers=self._auth(),
            )
        except httpx.HTTPError as exc:
            raise AdapterConnectError(
                f"cannot reach the Graph API: {type(exc).__name__}",
                code="whatsapp_cloud_unreachable",
            ) from None
        if answer.status_code < 400:
            data = _dict(_json(answer))
            self.display_number = _digits(data.get("display_phone_number"))
            return
        code, text = _graph_error(answer)
        if answer.status_code == 401 or code == CODE_TOKEN:
            raise AdapterConnectError(
                f"Meta refused the access token ({text}); create a new system-user token and "
                "update WHATSAPP_CLOUD_ACCESS_TOKEN",
                retryable=False,
                code="whatsapp_cloud_auth",
            )
        if 400 <= answer.status_code < 500 and answer.status_code != 429:
            raise AdapterConnectError(
                f"the Graph API refused the phone-number id (HTTP {answer.status_code}: {text}); "
                "check WHATSAPP_CLOUD_PHONE_NUMBER_ID",
                retryable=False,
                code="whatsapp_cloud_config",
            )
        raise AdapterConnectError(
            f"the Graph API answered HTTP {answer.status_code}", code="whatsapp_cloud_unavailable"
        )

    async def _close(self) -> None:
        await self.listener.stop()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # -- inbound: the webhook ----------------------------------------------------------------------

    async def _webhook(self, request: CallbackRequest) -> Reply:
        if request.method == "GET":
            return self._handshake(request)
        secret = self.cloud.app_secret
        offered = request.header("x-hub-signature-256")
        if not secret or not verify_hmac_sha256_hex(
            secret, request.body, offered, prefix="sha256="
        ):
            log.warning("whatsapp_cloud: a webhook with a missing or wrong signature was refused")
            return Reply.json({"error": "bad signature"}, 401)
        try:
            data = request.json()
        except BadPayload:
            return Reply.json({"error": "bad payload"}, 400)
        if not isinstance(data, dict):
            return Reply.json({"error": "bad payload"}, 400)
        events = self.events_from(_dict(data))
        if events:
            self.listener.background(self._emit_all(events))
        return Reply.json({"status": "ok"})

    def _handshake(self, request: CallbackRequest) -> Reply:
        expected = self.cloud.verify_token
        if not expected:
            return Reply.text("verify token not configured", 503)
        query = request.query_params()
        if query.get("hub.mode") != "subscribe":
            return Reply.text("unexpected hub.mode", 400)
        if not constant_time_equal(query.get("hub.verify_token", ""), expected):
            log.warning("whatsapp_cloud: a subscription check with a wrong verify token")
            return Reply.text("verify token mismatch", 403)
        challenge = query.get("hub.challenge", "")
        if not _CHALLENGE.match(challenge):
            return Reply.text("bad hub.challenge", 400)
        return Reply.text(challenge)

    async def _emit_all(self, events: Sequence[InboundEvent]) -> None:
        for event in events:
            await self.emit(event)

    def events_from(self, data: Mapping[str, Any]) -> list[InboundEvent]:
        """The new events in one signed webhook body (duplicates and foreign numbers dropped)."""
        if data.get("object") != "whatsapp_business_account":
            return []
        events: list[InboundEvent] = []
        for entry in _dicts(data.get("entry")):
            for change in _dicts(entry.get("changes")):
                if change.get("field") != "messages":
                    continue
                value = _dict(change.get("value"))
                metadata = _dict(value.get("metadata"))
                if not self._ours(entry, metadata):
                    if value.get("messages"):
                        log.warning(
                            "whatsapp_cloud: a signed webhook for another number or account "
                            "was ignored"
                        )
                    continue
                names = {
                    str(c.get("wa_id")): str(_dict(c.get("profile")).get("name") or "").strip()
                    for c in _dicts(value.get("contacts"))
                    if c.get("wa_id")
                }
                own = _digits(metadata.get("display_phone_number")) or self.display_number
                for message in _dicts(value.get("messages")):
                    event = self._admit(message, names, own)
                    if event is not None:
                        events.append(event)
                statuses = _dicts(value.get("statuses"))
                if statuses:
                    log.debug("whatsapp_cloud: %d status receipt(s) ignored", len(statuses))
        return events

    def _ours(self, entry: Mapping[str, Any], metadata: Mapping[str, Any]) -> bool:
        if str(metadata.get("phone_number_id") or "").strip() != self.cloud.phone_number_id:
            return False
        waba = self.cloud.waba_id
        return not waba or str(entry.get("id") or "").strip() == waba

    def _admit(
        self, message: Mapping[str, Any], names: Mapping[str, str], own: str
    ) -> InboundEvent | None:
        wamid = str(message.get("id") or "").strip()
        if wamid and not self.seen.first_time(wamid):
            log.debug("whatsapp_cloud: a repeated message was dropped")
            return None
        try:
            event = self.normalise(message, names, own)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("whatsapp_cloud: a message could not be read: %s", type(exc).__name__)
            return None
        if event is not None and wamid:
            _remember(self._last_inbound, event.source.chat_id, wamid, INTERACTIVE_CACHE)
        return event

    def normalise(
        self, message: Mapping[str, Any], names: Mapping[str, str], own: str = ""
    ) -> InboundEvent | None:
        """One Cloud API message as an event, or None for kinds that carry nothing to answer."""
        sender = str(message.get("from") or "").strip()
        kind = str(message.get("type") or "").strip().lower()
        if not sender or kind in _IGNORED_KINDS:
            return None
        if message.get("group_id") or message.get("chat"):
            log.warning("whatsapp_cloud: a group message was dropped (groups are not supported)")
            return None
        text = ""
        media: tuple[Attachment, ...] = ()
        event_kind = MessageKind.TEXT
        metadata: dict[str, Any] = {}
        context = _dict(message.get("context"))
        if kind == "text":
            text = str(_dict(message.get("text")).get("body") or "")
        elif kind in _MEDIA_KINDS:
            block = _dict(message.get(kind))
            text = str(block.get("caption") or "")
            media_id = str(block.get("id") or "").strip()
            if media_id:
                media = (
                    Attachment(ref=REF_PREFIX + media_id, mime=str(block.get("mime_type") or "")),
                )
            event_kind = _MEDIA_KINDS[kind]
            if kind == "audio" and block.get("voice") is True:
                event_kind = MessageKind.VOICE
            if kind == "document" and block.get("filename"):
                metadata["filename"] = str(block["filename"])
        elif kind == "interactive":
            inner = _dict(message.get("interactive"))
            reply = _dict(inner.get("button_reply") or inner.get("list_reply"))
            choice = str(reply.get("id") or "").strip()
            if not choice or not self._tap_matches(str(context.get("id") or ""), choice):
                return None
            metadata["button"] = choice
            if reply.get("title"):
                metadata["button_title"] = str(reply["title"])
        elif kind == "button":
            text = str(_dict(message.get("button")).get("text") or "")
        elif kind == "location":
            text = _location_text(_dict(message.get("location")))
            event_kind = MessageKind.LOCATION
        elif kind == "contacts":
            text = _contacts_text(_dicts(message.get("contacts")))
        else:
            return None
        if not text.strip() and not media and "button" not in metadata:
            return None
        name = names.get(sender) or None
        source = Source(
            platform=self.platform,
            chat_id=sender,
            chat_type="dm",
            chat_name=name,
            user_id=sender,
            user_name=name,
            user_id_alt=f"+{sender}" if sender.isdigit() else None,
        )
        reply_to = str(context.get("id") or "").strip() or None
        quoted_from = _digits(context.get("from"))
        return InboundEvent(
            source=source,
            text=text,
            kind=event_kind,
            message_id=str(message.get("id") or "").strip() or None,
            media=media,
            reply_to_id=reply_to,
            reply_to_is_own=bool(reply_to and own and quoted_from == own),
            metadata=metadata,
            timestamp=_timestamp(message.get("timestamp")),
        )

    def _tap_matches(self, prompt_wamid: str, choice: str) -> bool:
        """A tap on one of our prompts must name one of the ids that prompt offered; a prompt
        we no longer remember (restart, eviction) is left to the core's request checks."""
        offered = self._prompts.get(prompt_wamid)
        if offered is not None and choice not in offered:
            log.warning("whatsapp_cloud: a button reply naming an option it never had was dropped")
            return False
        return True

    # -- inbound: media ----------------------------------------------------------------------------

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register ``fetch_media`` with the media intake (P15-23); returns the undo."""
        return seat.register_fetcher(self.platform, self.fetch_media)

    def _token_may_go_to(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        base = (urllib.parse.urlsplit(self.cloud.base_url).hostname or "").lower()
        if parts.scheme != "https" and not (host == base and parts.scheme == "http"):
            return False
        return host == base or any(host == h or host.endswith("." + h) for h in MEDIA_HOSTS)

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The intake's fetcher for ``wac:`` refs: the media's URL from Graph, then its bytes."""
        media_id = attachment.ref.removeprefix(REF_PREFIX)
        if not attachment.ref.startswith(REF_PREFIX) or not _SAFE_ID.match(media_id):
            raise ValueError("not a WhatsApp Cloud media reference")
        client = self._client
        if client is None:
            raise RuntimeError("WhatsApp Cloud is not connected")
        answer = await client.get(self._path(media_id), headers=self._auth())
        if answer.status_code >= 400:
            raise RuntimeError(f"the media lookup failed with HTTP {answer.status_code}")
        info = _dict(_json(answer))
        url = str(info.get("url") or "")
        size = info.get("file_size")
        if isinstance(size, int) and size > DOWNLOAD_MAX:
            raise ValueError("the media file is too large")
        if not self._token_may_go_to(url):
            raise ValueError("the media URL is not on a Meta host")
        received = 0
        async with client.stream("GET", url, headers=self._auth()) as body:
            if body.status_code >= 400:
                raise RuntimeError(f"the media download failed with HTTP {body.status_code}")
            async for chunk in body.aiter_bytes():
                received += len(chunk)
                if received > DOWNLOAD_MAX:
                    raise ValueError("the media file is too large")
                yield chunk

    # -- outbound: shared --------------------------------------------------------------------------

    def _failure(self, answer: httpx.Response) -> SendResult:
        code, text = _graph_error(answer)
        reading: str | None = None
        final: str | None = None
        if code == CODE_WINDOW:
            reading, final = WINDOW_READING, "window"
        elif answer.status_code == 401 or code == CODE_TOKEN:
            reading, final = TOKEN_READING, "auth"
            self.report_fatal(
                "whatsapp_cloud_auth",
                "Meta refused the access token; update WHATSAPP_CLOUD_ACCESS_TOKEN",
            )
        elif code in RATE_CODES:
            reading = "too many requests"
        elif code == CODE_UNDELIVERABLE:
            reading = "chat not found: this number cannot receive WhatsApp messages"
        detail = f"(#{code}) {text}" if code is not None else text
        result = failure_from_status(
            answer.status_code, detail, answer.headers, now=self.clock.now(), reading=reading
        )
        if final is not None:
            result = replace(result, extra={**result.extra, FINAL_KEY: final})
        log.info("whatsapp_cloud: Graph refused a call: %s", self.scrub(result.error or ""))
        return result

    def is_final_failure(self, result: SendResult) -> bool:
        """Outside the 24-hour window or with a refused token, nothing a retry can fix."""
        return not result.ok and result.extra.get(FINAL_KEY) is not None

    async def _call(self, path: str, **kw: Any) -> httpx.Response | SendResult:
        client = self._client
        if client is None:
            return SendResult.failure("WhatsApp Cloud is not connected")
        try:
            answer = await client.post(path, headers=self._auth(), **kw)
        except httpx.HTTPError as exc:
            return failure_from_exception(exc)
        if answer.status_code >= 400:
            return self._failure(answer)
        return answer

    async def _post_message(
        self, chat_id: str, kind: str, block: Mapping[str, Any], reply_to: str | None
    ) -> SendResult:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": chat_id,
            "type": kind,
            kind: dict(block),
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        answer = await self._call(self._path(self.cloud.phone_number_id, "messages"), json=payload)
        if isinstance(answer, SendResult):
            return answer
        sent = _dicts(_dict(_json(answer)).get("messages"))
        wamid = str(sent[0].get("id") or "") if sent else ""
        if not wamid:
            # Meta took the request: never send it again.
            failed = SendResult.failure("Graph accepted the message without returning its id")
            return replace(failed, maybe_delivered=True, retryable=False)
        return SendResult.sent(wamid)

    @staticmethod
    def _partial(result: SendResult, ids: Sequence[str], remainder: str) -> SendResult:
        if not ids:
            return result
        return replace(
            result,
            message_id=ids[-1],
            continuation_ids=tuple(ids[:-1]),
            partial=True,
            remainder=None if result.maybe_delivered else remainder,
        )

    # -- outbound: text ----------------------------------------------------------------------------

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        plain = meta.get("plain") is True
        chunked = split_message(text, MAX_LEN, len_fn=code_points, marker=plain_marker)
        ids: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            body = chunk.text if plain else to_whatsapp(chunk.text)
            block = {"body": body, "preview_url": "http://" in body or "https://" in body}
            result = await self._post_message(chat_id, "text", block, reply_to if n == 0 else None)
            if not result.ok or result.message_id is None:
                return self._partial(result, ids, chunked.remainder(n))
            ids.append(result.message_id)
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    async def _typing(self, chat_id: str, meta: dict[str, Any]) -> None:
        wamid = self._last_inbound.get(chat_id)
        if wamid is None:
            return  # no inbound message to attach the indicator to (e.g. after a restart)
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": wamid,
            "typing_indicator": {"type": "text"},
        }
        answer = await self._call(self._path(self.cloud.phone_number_id, "messages"), json=payload)
        if isinstance(answer, SendResult):
            log.debug("whatsapp_cloud: typing indicator refused: %s", answer.error)

    # -- outbound: prompts -------------------------------------------------------------------------

    async def _send_choices(
        self,
        chat_id: str,
        body: str,
        options: Sequence[tuple[str, str]],
        fallback: str,
        meta: dict[str, Any],
    ) -> SendResult:
        text = to_whatsapp(body)
        if not options or len(options) > MAX_ROWS or len(text) > BODY_MAX:
            return await self._send(chat_id, fallback, None, meta)
        interactive: dict[str, Any]
        if len(options) <= MAX_BUTTONS:
            buttons = [
                {"type": "reply", "reply": {"id": ident, "title": _label(title, BUTTON_TITLE_MAX)}}
                for ident, title in options
            ]
            interactive = {"type": "button", "body": {"text": text}, "action": {"buttons": buttons}}
        else:
            rows: list[dict[str, str]] = []
            for ident, title in options:
                row = {"id": ident, "title": _label(title, ROW_TITLE_MAX)}
                if row["title"] != " ".join(title.split()):
                    row["description"] = _label(title, ROW_DESCRIPTION_MAX)
                rows.append(row)
            interactive = {
                "type": "list",
                "body": {"text": text},
                "action": {"button": LIST_BUTTON, "sections": [{"rows": rows}]},
            }
        result = await self._post_message(chat_id, "interactive", interactive, None)
        if result.ok and result.message_id:
            ids = frozenset(ident for ident, _ in options)
            _remember(self._prompts, result.message_id, ids, INTERACTIVE_CACHE)
        return result

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        options = [
            (approval_callback(prompt.request_id, a.choice), a.label) for a in prompt.actions
        ]
        return await self._send_choices(chat_id, prompt.text, options, prompt.fallback_text, meta)

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        options = [
            (clarify_callback(prompt.clarify_id, n), f"{n + 1}. {choice}")
            for n, choice in enumerate(prompt.choices)
        ]
        body = prompt.question
        if any(len(" ".join(title.split())) > BUTTON_TITLE_MAX for _, title in options):
            body += "\n\n" + "\n".join(title for _, title in options)
        fallback = prompt.fallback_text or prompt.question
        return await self._send_choices(chat_id, body, options, fallback, meta)

    # -- outbound: media ---------------------------------------------------------------------------

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        file = Path(path)
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        wa_kind = _OUTBOUND_KIND[kind]
        if wa_kind in _ACCEPTED_MIME and mime not in _ACCEPTED_MIME[wa_kind]:
            wa_kind = "document"
        try:
            size = file.stat().st_size
        except OSError:
            return SendResult.failure(f"the file to send is missing: {file.name}")
        if size > MEDIA_CAPS[wa_kind]:
            return SendResult.failure(
                f"{file.name} is too long for WhatsApp: {size} bytes over the "
                f"{MEDIA_CAPS[wa_kind]}-byte {wa_kind} cap"
            )
        data = await run_blocking(file.read_bytes, label="whatsapp_cloud upload")
        upload = await self._call(
            self._path(self.cloud.phone_number_id, "media"),
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (file.name, data, mime)},
        )
        if isinstance(upload, SendResult):
            return upload
        media_id = str(_dict(_json(upload)).get("id") or "")
        if not media_id:
            return SendResult.failure("the media upload returned no id")
        block: dict[str, Any] = {"id": media_id}
        text = (caption or "").strip()
        late_caption = ""
        if text and wa_kind != "audio":
            if len(text) <= CAPTION_MAX:
                block["caption"] = text
            else:
                late_caption = text
        if wa_kind == "document":
            block["filename"] = file.name
        reply_to = meta.get(REPLY_TO_KEY)
        result = await self._post_message(
            chat_id, wa_kind, block, str(reply_to) if reply_to else None
        )
        if result.ok and (late_caption or (text and wa_kind == "audio")):
            follow = await self._send(chat_id, late_caption or text, None, meta)
            if not follow.ok:
                log.info("whatsapp_cloud: the caption after a media message failed")
        return result

    # -- descriptive -------------------------------------------------------------------------------

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(name=str(chat_id), type="dm")


# -- helpers --------------------------------------------------------------------------------------


def _json(answer: httpx.Response) -> object:
    try:
        return answer.json()
    except ValueError:
        return None


def _graph_error(answer: httpx.Response) -> tuple[int | None, str]:
    """Graph's error code and message (with its details) from an error answer."""
    error = _dict(_dict(_json(answer)).get("error"))
    if not error:
        return None, " ".join(answer.text.split())[:300]
    raw_code = error.get("code")
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    message = str(error.get("message") or "").strip()
    details = str(_dict(error.get("error_data")).get("details") or "").strip()
    text = f"{message}: {details}" if details and details not in message else message
    return code, text or f"HTTP {answer.status_code}"


def _timestamp(value: object) -> datetime | None:
    with contextlib.suppress(TypeError, ValueError, OverflowError, OSError):
        return datetime.fromtimestamp(int(str(value)), UTC)
    return None


def _location_text(location: Mapping[str, Any]) -> str:
    where = ", ".join(
        str(location[key]).strip() for key in ("name", "address") if location.get(key)
    )
    lat, lon = location.get("latitude"), location.get("longitude")
    point = f"{lat}, {lon}" if lat is not None and lon is not None else ""
    shown = " ".join(part for part in (where, f"({point})" if point else "") if part)
    return f"[Location: {shown}]" if shown else ""


def _contacts_text(contacts: Sequence[Mapping[str, Any]]) -> str:
    names = [str(_dict(c.get("name")).get("formatted_name") or "").strip() for c in contacts]
    shown = ", ".join(n for n in names if n)
    return f"[Shared contact: {shown}]" if shown else ""
