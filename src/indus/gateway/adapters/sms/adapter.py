"""The SMS adapter (P16-10): Twilio Programmable Messaging REST out, signed webhooks in.

Connect: ``GET /2010-04-01/Accounts/{sid}.json`` with the account's Basic auth (refused
credentials or an unknown account are fatal, non-retryable connect errors), then — unless the open
is send-only (``indus send``) — the callback listener from the P16-01 kit (loopback unless the
operator opens it). Receiving needs the public URL Twilio calls: the signature covers it, so
without it no callback could be checked and the connect is refused.

Webhook: ``POST`` form data checked against ``X-Twilio-Signature`` (base64 HMAC-SHA1 with the
auth token over the public URL, the request's query string and every form field sorted by name;
the URL is also tried with the scheme's default port added or removed, as Twilio may sign either)
before any field is read. Then: a repeated ``MessageSid`` is dropped, so are our own number as the
sender (a loop), messages to another number of the account, and opt-out / opt-in / help keywords
(Twilio answers those itself and keeps the opt-out list). MMS media become attachments. Twilio gets
an empty TwiML document at once; replies always go through the REST API.

Sending: plain text (the P15 sender strips Markdown: ``markup="plain"``) in pieces of ≤1,600
UTF-16 units, one ``Messages.json`` call each. Twilio's error codes are read into typed results:
an unsubscribed number (21610) is ``forbidden`` and final, an invalid number is "chat not found",
20429 is a rate limit, 21617 too long, and a refused credential (20003 / HTTP 401) is final and
reported as a fatal platform state. No edits, typing or buttons. Phone numbers are masked in every
log line and error text.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar, Final

import httpx

from indus.foundation.clock import Clock
from indus.gateway.adapters._http import (
    BadPayload,
    CallbackListener,
    CallbackRequest,
    ListenerSettings,
    Reply,
    SeenIds,
    failure_from_exception,
    failure_from_status,
    verify_url_signature,
)
from indus.gateway.adapters._identity import mask_number, mask_numbers
from indus.gateway.adapters.base import (
    Adapter,
    AdapterConnectError,
    Capabilities,
    ChatInfo,
    FetcherSeat,
    InboundSink,
)
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult
from indus.gateway.outbound.chunker import plain_marker, split_message, utf16_units

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "API_VERSION",
    "DEFAULT_PATH",
    "DEFAULT_PORT",
    "KEYWORDS",
    "MAX_BODY_BYTES",
    "MAX_LEN",
    "TWILIO_BASE",
    "TWILIO_ROUTES",
    "SmsAdapter",
    "TwilioSettings",
    "mask_number",
]

log = logging.getLogger(__name__)

TWILIO_BASE: Final = "https://api.twilio.com"
API_VERSION: Final = "2010-04-01"
DEFAULT_PORT: Final = 8080
DEFAULT_PATH: Final = "/webhooks/twilio"
MAX_BODY_BYTES: Final = 64 * 1024
# Twilio's cap on one message body (about ten segments).
MAX_LEN: Final = 1600
SID_CACHE: Final = 5000
SID_TTL_S: Final = 24 * 3600.0
MAX_MEDIA: Final = 10
DOWNLOAD_MAX: Final = 16 * 1024 * 1024
EMPTY_TWIML: Final = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
# Twilio's standard opt-out, opt-in and help keywords: Twilio answers them, the agent never does.
KEYWORDS: Final = frozenset(
    {
        "STOP",
        "STOPALL",
        "UNSUBSCRIBE",
        "CANCEL",
        "END",
        "QUIT",
        "START",
        "YES",
        "UNSTOP",
        "HELP",
        "INFO",
    }
)

# The REST routes this adapter calls.
TWILIO_ROUTES: Final = (
    ("GET", "/2010-04-01/Accounts/{AccountSid}.json"),
    ("POST", "/2010-04-01/Accounts/{AccountSid}/Messages.json"),
    ("GET", "/2010-04-01/Accounts/{AccountSid}/Messages/{MessageSid}/Media/{MediaSid}"),
)

# Twilio error codes read into typed results.
CODE_AUTH: Final = 20003
CODE_RATE: Final = 20429
CODE_UNSUBSCRIBED: Final = 21610
CODE_REGION: Final = 21408
CODE_TOO_LONG: Final = 21617
INVALID_TO_CODES: Final = frozenset({21211, 21614})

UNSUBSCRIBED_READING: Final = (
    "forbidden: this number replied STOP and is unsubscribed; Twilio blocks messages to it "
    "until they text START"
)
REGION_READING: Final = "forbidden: the Twilio account may not send to this number's region"
TOKEN_READING: Final = "the Twilio credentials were refused"
# ``SendResult.extra`` key marking failures no retry or plain resend can fix.
FINAL_KEY: Final = "sms_final"

_SID = re.compile(r"^[A-Za-z0-9]{2,64}$")
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_DEFAULT_PORTS: Final[Mapping[str, int]] = {"https": 443, "http": 80}


def _twiml(status: int = 200) -> Reply:
    return Reply(status, EMPTY_TWIML.encode(), "application/xml")


def _url_without_query(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def url_variants(url: str) -> list[str]:
    """``url`` and, for a default port, the same URL with that port written out or removed."""
    parts = urllib.parse.urlsplit(url)
    default = _DEFAULT_PORTS.get(parts.scheme)
    host = parts.hostname or ""
    if default is None or not host:
        return [url]
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        return [url]
    if port is None:
        netloc = f"{host}:{default}"
    elif port == default:
        netloc = host
    else:
        return [url]
    if parts.username or parts.password:
        return [url]
    return [url, urllib.parse.urlunsplit(parts._replace(netloc=netloc))]


@dataclass(frozen=True)
class TwilioSettings:
    """What the adapter needs from Twilio (credentials masked in reprs)."""

    account_sid: str
    auth_token: str
    phone_number: str
    # The URL Twilio calls (the listener's ``webhook_public_url``); signatures cover it.
    public_url: str | None = None
    base_url: str = TWILIO_BASE

    def __repr__(self) -> str:
        return f"TwilioSettings(account_sid=…, base_url={self.base_url!r})"

    def check(self) -> None:
        if not self.account_sid or not self.auth_token or not self.phone_number:
            raise ValueError("the account SID, auth token and phone number are required")
        if not _SID.match(self.account_sid):
            raise ValueError("the account SID must be letters and digits")
        if not _E164.match(self.phone_number):
            raise ValueError("the phone number must be in E.164 form, like +15551234567")
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an http(s) URL")


class SmsAdapter(Adapter):
    platform: ClassVar[str] = "sms"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_LEN,
        len_fn=utf16_units,
        markup="plain",
        splits_natively=True,
        inline_wait_cap=5.0,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "httpx:AsyncClient": ("get", "post", "stream", "aclose"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        twilio: TwilioSettings,
        listener: ListenerSettings,
        client_factory: Callable[[], httpx.AsyncClient],
        serve: bool = True,
        send_only: bool = False,
        settings: PlatformSettings | None = None,
        sink: InboundSink | None = None,
    ) -> None:
        twilio.check()
        secrets = (twilio.auth_token, twilio.account_sid, twilio.phone_number)
        super().__init__(clock=clock, settings=settings, secrets=secrets, sink=sink)
        self.twilio = twilio
        # ``indus send`` (``AdapterConfig.send_only``): the credential check only, no listener.
        self.send_only = send_only
        self._client_factory = client_factory
        self._client: httpx.AsyncClient | None = None
        self.listener = CallbackListener(
            listener, name=self.platform, clock=clock, scrub=self.scrub, serve=serve
        )
        self.listener.route(listener.path, self._webhook, methods=("POST",))
        self.seen = SeenIds(clock, SID_TTL_S, SID_CACHE)

    def scrub(self, text: str) -> str:
        return mask_numbers(super().scrub(text))

    # -- lifecycle ---------------------------------------------------------------------------------

    def _account_path(self, *parts: str) -> str:
        return "/".join(("", API_VERSION, "Accounts", self.twilio.account_sid, *parts))

    def _auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.twilio.account_sid, self.twilio.auth_token)

    async def _open(self, is_reconnect: bool) -> None:
        if not self.send_only and not self.twilio.public_url:
            raise AdapterConnectError(
                "SMS needs platforms.sms.webhook_public_url (the URL set in the Twilio console) "
                "to check Twilio's signatures; unsigned callbacks are never accepted",
                retryable=False,
                code="sms_public_url_missing",
            )
        client = self._client_factory()
        try:
            await self._check_credentials(client)
            if not self.send_only:
                await self.listener.start()
        except BaseException:
            await client.aclose()
            raise
        self._client = client

    async def _check_credentials(self, client: httpx.AsyncClient) -> None:
        try:
            answer = await client.get(f"{self._account_path()}.json", auth=self._auth())
        except httpx.HTTPError as exc:
            raise AdapterConnectError(
                f"cannot reach Twilio: {type(exc).__name__}", code="sms_unreachable"
            ) from None
        if answer.status_code < 400:
            return
        code, text = _twilio_error(answer)
        if answer.status_code == 401 or code == CODE_AUTH:
            raise AdapterConnectError(
                "Twilio refused the account SID or auth token; check TWILIO_ACCOUNT_SID and "
                "TWILIO_AUTH_TOKEN",
                retryable=False,
                code="sms_auth",
            )
        if 400 <= answer.status_code < 500 and answer.status_code != 429:
            raise AdapterConnectError(
                f"Twilio refused the account (HTTP {answer.status_code}: {text})",
                retryable=False,
                code="sms_config",
            )
        raise AdapterConnectError(
            f"Twilio answered HTTP {answer.status_code}", code="sms_unavailable"
        )

    async def _close(self) -> None:
        await self.listener.stop()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # -- inbound: the webhook ----------------------------------------------------------------------

    def signature_ok(self, request: CallbackRequest, pairs: Sequence[tuple[str, str]]) -> bool:
        """Twilio's signature over the URL it called (query included) and the sorted fields."""
        token = self.twilio.auth_token
        public = self.twilio.public_url
        offered = request.header("x-twilio-signature")
        if not token or not public or not offered:
            return False
        url = request.signed_url(_url_without_query(public))
        return any(
            verify_url_signature(token, candidate, pairs, offered)
            for candidate in url_variants(url)
        )

    async def _webhook(self, request: CallbackRequest) -> Reply:
        try:
            pairs = request.form_pairs()
        except BadPayload:
            return _twiml(400)
        if not self.signature_ok(request, pairs):
            log.warning("sms: a callback with a missing or wrong Twilio signature was refused")
            return _twiml(403)
        fields: dict[str, str] = {}
        for name, value in pairs:
            fields.setdefault(name, value)
        event = self.accept(fields)
        if event is not None:
            self.listener.background(self.emit(event))
        return _twiml()

    def accept(self, fields: Mapping[str, str]) -> InboundEvent | None:
        """The event for one signed incoming-message callback, or None when it is not for us."""
        sid = (fields.get("MessageSid") or fields.get("SmsMessageSid") or "").strip()
        account = (fields.get("AccountSid") or "").strip()
        sender = (fields.get("From") or "").strip()
        to = (fields.get("To") or "").strip()
        own = self.twilio.phone_number
        if account and account != self.twilio.account_sid:
            log.warning("sms: a callback for another Twilio account was ignored")
            return None
        if not sender or sender == own:
            log.debug("sms: a message from our own number was ignored")
            return None
        if to and to != own:
            log.warning(
                "sms: a message to %s (not our number %s) was ignored",
                mask_number(to),
                mask_number(own),
            )
            return None
        if sid and not self.seen.first_time(sid):
            log.debug("sms: a repeated message was dropped")
            return None
        body = fields.get("Body") or ""
        if fields.get("OptOutType") or body.strip().upper() in KEYWORDS:
            log.info("sms: a keyword message from %s was left to Twilio", mask_number(sender))
            return None
        media = _media(fields)
        if not body.strip() and not media:
            return None
        digits = "".join(c for c in sender if c.isdigit())
        source = Source(
            platform=self.platform,
            chat_id=sender,
            chat_type="dm",
            user_id=sender,
            user_id_alt=digits if digits and digits != sender else None,
        )
        return InboundEvent(
            source=source,
            text=body,
            kind=_kind(media[0].mime) if media else MessageKind.TEXT,
            message_id=sid or None,
            media=media,
        )

    # -- inbound: media ----------------------------------------------------------------------------

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register ``fetch_media`` with the media intake (P15-23); returns the undo."""
        return seat.register_fetcher(self.platform, self.fetch_media)

    def _ours(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(self.twilio.base_url)
        return (
            parts.scheme == base.scheme
            and (parts.hostname or "").lower() == (base.hostname or "").lower()
            and parts.port == base.port
            and parts.path.startswith(self._account_path("Messages") + "/")
            and ".." not in parts.path
        )

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The intake's fetcher for MMS media: Twilio's media URL with the account's auth (only
        ever to the configured Twilio API host; a redirect to the CDN drops the credentials)."""
        if not self._ours(attachment.ref):
            raise ValueError("not a Twilio media URL of this account")
        client = self._client
        if client is None:
            raise RuntimeError("SMS is not connected")
        received = 0
        async with client.stream(
            "GET", attachment.ref, auth=self._auth(), follow_redirects=True
        ) as body:
            if body.status_code >= 400:
                raise RuntimeError(f"the media download failed with HTTP {body.status_code}")
            async for chunk in body.aiter_bytes():
                received += len(chunk)
                if received > DOWNLOAD_MAX:
                    raise ValueError("the media file is too large")
                yield chunk

    # -- outbound ----------------------------------------------------------------------------------

    def _failure(self, answer: httpx.Response) -> SendResult:
        code, text = _twilio_error(answer)
        reading: str | None = None
        final: str | None = None
        if answer.status_code == 401 or code == CODE_AUTH:
            reading, final = TOKEN_READING, "auth"
            self.report_fatal(
                "sms_auth", "Twilio refused the credentials; update TWILIO_AUTH_TOKEN"
            )
        elif code == CODE_UNSUBSCRIBED:
            reading, final = UNSUBSCRIBED_READING, "unsubscribed"
        elif code == CODE_REGION:
            reading, final = REGION_READING, "region"
        elif code in INVALID_TO_CODES:
            reading = "chat not found: Twilio cannot deliver to this number"
        elif code == CODE_RATE:
            reading = "too many requests"
        elif code == CODE_TOO_LONG:
            reading = "too long"
        detail = f"Twilio error {code}: {text}" if code is not None else text
        result = failure_from_status(
            answer.status_code, detail, answer.headers, now=self.clock.now(), reading=reading
        )
        if final is not None:
            result = replace(result, extra={**result.extra, FINAL_KEY: final})
        log.info("sms: Twilio refused a send: %s", self.scrub(result.error or ""))
        return result

    def is_final_failure(self, result: SendResult) -> bool:
        """Unsubscribed, a blocked region or refused credentials: nothing a retry can fix."""
        return not result.ok and result.extra.get(FINAL_KEY) is not None

    async def _post(self, chat_id: str, body: str) -> SendResult:
        client = self._client
        if client is None:
            return SendResult.failure("SMS is not connected")
        form = {"To": chat_id, "From": self.twilio.phone_number, "Body": body}
        try:
            answer = await client.post(
                self._account_path("Messages.json"), data=form, auth=self._auth()
            )
        except httpx.HTTPError as exc:
            return failure_from_exception(exc)
        if answer.status_code >= 400:
            return self._failure(answer)
        sid = str(_dict(_json(answer)).get("sid") or "")
        if not sid:
            failed = SendResult.failure("Twilio accepted the message without returning its sid")
            return replace(failed, maybe_delivered=True, retryable=False)
        return SendResult.sent(sid)

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        chunked = split_message(text, MAX_LEN, len_fn=utf16_units, marker=plain_marker)
        ids: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            result = await self._post(chat_id, chunk.text)
            if not result.ok or result.message_id is None:
                if not ids:
                    return result
                return replace(
                    result,
                    message_id=ids[-1],
                    continuation_ids=tuple(ids[:-1]),
                    partial=True,
                    remainder=None if result.maybe_delivered else chunked.remainder(n),
                )
            ids.append(result.message_id)
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    # -- descriptive -------------------------------------------------------------------------------

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(name=str(chat_id), type="dm")


# -- helpers --------------------------------------------------------------------------------------


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownArgumentType]


def _json(answer: httpx.Response) -> object:
    try:
        return answer.json()
    except ValueError:
        return None


def _twilio_error(answer: httpx.Response) -> tuple[int | None, str]:
    """Twilio's error code and message from an error answer."""
    data = _dict(_json(answer))
    raw_code = data.get("code")
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    message = str(data.get("message") or "").strip()
    if not message:
        message = " ".join(answer.text.split())[:300] or f"HTTP {answer.status_code}"
    return code, message


def _media(fields: Mapping[str, str]) -> tuple[Attachment, ...]:
    try:
        count = min(max(int(fields.get("NumMedia") or 0), 0), MAX_MEDIA)
    except ValueError:
        return ()
    found: list[Attachment] = []
    for n in range(count):
        url = (fields.get(f"MediaUrl{n}") or "").strip()
        if url:
            found.append(Attachment(ref=url, mime=(fields.get(f"MediaContentType{n}") or "")))
    return tuple(found)


def _kind(mime: str) -> MessageKind:
    major = mime.split("/", 1)[0].lower()
    if major == "image":
        return MessageKind.PHOTO
    if major == "audio":
        return MessageKind.VOICE
    if major == "video":
        return MessageKind.VIDEO
    return MessageKind.DOCUMENT
