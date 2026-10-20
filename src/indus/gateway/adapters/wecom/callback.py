"""The WeCom callback adapter (P16-18): a self-built WeCom app that receives encrypted XML
callbacks over HTTP and answers through the corp API.

Inbound runs on the P16-01 listener kit (127.0.0.1:8645 ``/wecom/callback`` by default, bodies
≤64 KiB). Every request is checked in this order, and fails closed:

1. ``GET`` (WeCom's URL check): the SHA1 signature over token, ``timestamp``, ``nonce`` and
   ``echostr`` is compared in constant time; then ``echostr`` is decrypted and its plaintext
   returned exactly as it is;
2. ``POST``: the outer XML (parsed with DTDs, entities and external references refused) gives
   ``Encrypt``; the signature over it is checked in constant time **before** any decryption
   (401 otherwise), then the timestamp must be within ±300 s; decryption checks the padding, the
   declared length and that the receive id is our corp id (400 otherwise); the inner XML (same safe
   parser) becomes one `InboundEvent` handed to the gateway in the background, and WeCom gets
   ``success`` at once. A repeated ``MsgId`` (WeCom retries slow answers) is answered without a
   second event.

People write to the app one to one, so every chat is a DM keyed by the user id. Text, image, voice,
video, location and link messages are taken; ``enter_agent`` / ``subscribe`` events are not.
Media arrive as ``wecom-media://<MediaId>`` attachments that `fetch_media` reads from
``cgi-bin/media/get``.

Outbound: ``cgi-bin/message/send`` text messages of at most 2,048 UTF-8 bytes (WeCom silently cuts
longer ones), with an access token from ``cgi-bin/gettoken`` cached until 5 minutes before it
expires and fetched again once when WeCom calls it invalid (40014, 42001, 40001). The token and
the corp secret are added to the URL inside the transport, so no log line, ``response.request`` or
error text carries them.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from indus.foundation.clock import Clock
from indus.foundation.redact import redact_text
from indus.gateway.adapters._http import (
    CallbackListener,
    CallbackRequest,
    ListenerSettings,
    ReplayWindow,
    Reply,
    SeenIds,
    failure_from_exception,
    failure_from_status,
)
from indus.gateway.adapters.base import (
    Adapter,
    AdapterConnectError,
    Capabilities,
    ChatInfo,
    FetcherSeat,
)
from indus.gateway.adapters.wecom.crypto import (
    CallbackCrypto,
    CryptoError,
    MissingCrypto,
    UnsafeXml,
    parse_xml,
    require_cipher,
)
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import SendResult, is_ambiguous_timeout
from indus.gateway.outbound.chunker import split_message

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "AGENT_ID_ENV",
    "CORP_ID_ENV",
    "CORP_SECRET_ENV",
    "DEFAULT_PATH",
    "DEFAULT_PORT",
    "ENCODING_AES_KEY_ENV",
    "MAX_MESSAGE_BYTES",
    "TOKEN_ENV",
    "WeComCallbackAdapter",
    "utf8_bytes",
]

log = logging.getLogger(__name__)

CORP_ID_ENV = "WECOM_CALLBACK_CORP_ID"
CORP_SECRET_ENV = "WECOM_CALLBACK_CORP_SECRET"
AGENT_ID_ENV = "WECOM_CALLBACK_AGENT_ID"
TOKEN_ENV = "WECOM_CALLBACK_TOKEN"
ENCODING_AES_KEY_ENV = "WECOM_CALLBACK_ENCODING_AES_KEY"
API_BASE = "https://qyapi.weixin.qq.com"
DEFAULT_PORT = 8645
DEFAULT_PATH = "/wecom/callback"
MAX_BODY_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 2048
REPLAY_WINDOW_S = 300.0
DEDUPE_S = 300.0
TOKEN_MARGIN_S = 300.0
MAX_MEDIA_BYTES = 20 * 1024 * 1024
QUERY_EXTENSION = "indus.secret_query"
MEDIA_SCHEME = "wecom-media://"
_STALE_TOKEN = frozenset({40014, 42001, 40001})
_RATE_CODES = frozenset({45009, 45033, 45011})
_READINGS: Mapping[int, str] = {
    81013: "chat not found",
    60111: "chat not found",
    40003: "chat not found",
    40007: "message not found",
    60011: "forbidden",
    48002: "forbidden",
    60020: "forbidden",
    40058: "can't parse the message",
    40008: "can't parse the message",
    44004: "can't parse the message",
    45002: "message is too long",
}
_STATED_WAIT = re.compile(r"(?:retry after|wait)\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


class _SecretQuery(httpx.AsyncBaseTransport):
    """Adds ``extensions[QUERY_EXTENSION]`` to the URL inside the transport (never logged)."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        extra = request.extensions.get(QUERY_EXTENSION)
        if isinstance(extra, Mapping):
            params = {str(k): str(v) for k, v in extra.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            request = httpx.Request(
                request.method,
                request.url.copy_merge_params(params),
                headers=request.headers,
                stream=request.stream,
                extensions={k: v for k, v in request.extensions.items() if k != QUERY_EXTENSION},
            )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class _Refused(Exception):
    def __init__(self, answer: Mapping[str, Any]) -> None:
        super().__init__(f"WeCom errcode {answer.get('errcode')}: {answer.get('errmsg') or ''}")
        self.answer = answer


def _code(answer: Mapping[str, Any]) -> int:
    try:
        return int(answer.get("errcode") or 0)
    except (TypeError, ValueError):
        return -1


class WeComCallbackAdapter(Adapter):
    platform: ClassVar[str] = "wecom_callback"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_MESSAGE_BYTES,
        len_fn=utf8_bytes,
        markup="plain",
        splits_natively=True,
        supports_edit=False,
        supports_delete=False,
        supports_buttons=False,
        dm_is_private=True,
        push_capable=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "indus.gateway.adapters.wecom.crypto:CallbackCrypto": ("verify", "open", "decrypt"),
        "cryptography.hazmat.primitives.ciphers:Cipher": ("decryptor", "encryptor"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        corp_id: str,
        corp_secret: str,
        agent_id: str,
        token: str,
        encoding_aes_key: str,
        listener: ListenerSettings,
        settings: PlatformSettings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        serve: bool = True,
        api_base: str = API_BASE,
        send_only: bool = False,
    ) -> None:
        super().__init__(
            clock=clock, settings=settings, secrets=(corp_secret, token, encoding_aes_key)
        )
        self.send_only = send_only
        self.corp_id = corp_id.strip()
        self.agent_id = agent_id.strip()
        self._corp_secret = corp_secret.strip()
        self._token = token.strip()
        self._aes_key = encoding_aes_key.strip()
        self._transport = transport
        self._proxy = proxy
        self._api_base = api_base.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._access: str | None = None
        self._access_until = 0.0
        self._crypto: CallbackCrypto | None = None
        self.listener = CallbackListener(
            listener, name=self.platform, clock=clock, scrub=self.scrub, serve=serve
        )
        self.listener.route(listener.path, self._callback, methods=("GET", "POST"))
        self.replays = ReplayWindow(clock, REPLAY_WINDOW_S)
        self.seen = SeenIds(clock, DEDUPE_S, 5_000)

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<WeComCallbackAdapter {self.corp_id or '?'} {state}>"

    def scrub(self, text: str) -> str:
        return redact_text(super().scrub(text), [self._access] if self._access else [])

    # -- lifecycle ---------------------------------------------------------------------------------

    def _check_config(self) -> None:
        missing = [
            name
            for name, value in (
                (CORP_ID_ENV, self.corp_id),
                (CORP_SECRET_ENV, self._corp_secret),
                (AGENT_ID_ENV, self.agent_id),
                (TOKEN_ENV, self._token),
                (ENCODING_AES_KEY_ENV, self._aes_key),
            )
            if not value
        ]
        if missing:
            raise AdapterConnectError(
                "WeCom callback is not configured: set " + ", ".join(missing),
                retryable=False,
                code="config",
            )
        if not self.agent_id.isdigit():
            raise AdapterConnectError(
                f"{AGENT_ID_ENV} must be the app's numeric agent id", retryable=False, code="config"
            )
        try:
            self._crypto = CallbackCrypto(self._token, self._aes_key, self.corp_id)
        except CryptoError as exc:
            raise AdapterConnectError(
                f"{ENCODING_AES_KEY_ENV}: {exc}", retryable=False, code="config"
            ) from None

    async def _open(self, is_reconnect: bool) -> None:
        self._check_config()
        inner = self._transport or httpx.AsyncHTTPTransport(proxy=self._proxy, trust_env=False)
        self._client = httpx.AsyncClient(
            base_url=self._api_base,
            transport=_SecretQuery(inner),
            timeout=httpx.Timeout(20.0, connect=10.0),
            trust_env=False,
            follow_redirects=False,
        )
        try:
            await self._access_token(force=True)
        except _Refused as exc:
            await self._drop_client()
            raise AdapterConnectError(
                f"WeCom refused the corp id or secret ({exc})", retryable=False, code="auth_failed"
            ) from None
        except httpx.HTTPError as exc:
            await self._drop_client()
            raise AdapterConnectError(
                f"cannot reach WeCom: {type(exc).__name__}", retryable=True
            ) from None
        except BaseException:
            await self._drop_client()
            raise
        if self.send_only:
            return
        try:
            require_cipher()
        except MissingCrypto as exc:
            await self._drop_client()
            raise AdapterConnectError(
                str(exc), retryable=False, code="missing_dependency"
            ) from None
        try:
            await self.listener.start()
        except BaseException:
            await self._drop_client()
            raise

    async def _drop_client(self) -> None:
        client, self._client = self._client, None
        self._access = None
        if client is not None:
            await client.aclose()

    async def _close(self) -> None:
        await self.listener.stop()
        await self._drop_client()

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ConnectionError("not connected to WeCom")
        return self._client

    # -- access token ------------------------------------------------------------------------------

    async def _access_token(self, *, force: bool = False) -> str:
        now = self.clock.monotonic()
        if self._access and not force and now < self._access_until:
            return self._access
        client = self._require_client()
        response = await client.get(
            "/cgi-bin/gettoken",
            extensions={QUERY_EXTENSION: {"corpid": self.corp_id, "corpsecret": self._corp_secret}},
        )
        if response.status_code >= 400:
            raise _Refused({"errcode": response.status_code, "errmsg": "HTTP error"})
        answer = dict(response.json())
        token = str(answer.get("access_token") or "")
        if _code(answer) != 0 or not token:
            raise _Refused(answer)
        try:
            lifetime = float(answer.get("expires_in") or 7200)
        except (TypeError, ValueError):
            lifetime = 7200.0
        self._access = token
        self._access_until = now + max(lifetime - TOKEN_MARGIN_S, 60.0)
        return token

    # -- inbound -----------------------------------------------------------------------------------

    async def _callback(self, request: CallbackRequest) -> Reply:
        crypto = self._crypto
        if crypto is None:
            return Reply.json({"error": "not ready"}, 503)
        query = request.query_params()
        signature = query.get("msg_signature")
        timestamp = query.get("timestamp", "")
        nonce = query.get("nonce", "")
        if request.method == "GET":
            echo = query.get("echostr", "")
            if not echo or not crypto.verify(signature, timestamp, nonce, echo):
                return Reply.text("signature verification failed", 401)
            try:
                plain = crypto.decrypt(echo)
            except (CryptoError, MissingCrypto):
                return Reply.text("bad echostr", 400)
            return Reply.text(plain.decode("utf-8", errors="replace"))
        try:
            outer = parse_xml(request.body)
        except UnsafeXml:
            return Reply.text("bad payload", 400)
        encrypted = outer.get("Encrypt", "").strip()
        if not encrypted:
            return Reply.text("bad payload", 400)
        if not crypto.verify(signature, timestamp, nonce, encrypted):
            return Reply.text("signature verification failed", 401)
        if not self.replays.fresh(timestamp):
            return Reply.text("stale timestamp", 401)
        try:
            inner = parse_xml(crypto.decrypt(encrypted))
        except (CryptoError, UnsafeXml):
            return Reply.text("bad payload", 400)
        except MissingCrypto:
            return Reply.text("not ready", 503)
        key = inner.get("MsgId") or f"{inner.get('FromUserName')}:{inner.get('CreateTime')}"
        if not self.seen.first_time(key):
            return Reply.text("success")
        event = self.normalise(inner)
        if event is not None:
            self.listener.background(self.emit(event))
        return Reply.text("success")

    def normalise(self, fields: Mapping[str, str]) -> InboundEvent | None:
        """One decrypted message → an `InboundEvent` (None for lifecycle events and others)."""
        user = fields.get("FromUserName", "").strip()
        kind = fields.get("MsgType", "").strip().lower()
        if not user or not kind:
            return None
        text = ""
        media: list[Attachment] = []
        event_kind = MessageKind.TEXT
        if kind == "text":
            text = fields.get("Content", "")
        elif kind == "image":
            event_kind = MessageKind.PHOTO
            media.append(_media(fields.get("MediaId", ""), "image/jpeg"))
        elif kind == "voice":
            event_kind = MessageKind.VOICE
            fmt = fields.get("Format", "amr").strip().lower() or "amr"
            media.append(_media(fields.get("MediaId", ""), f"audio/{fmt}"))
        elif kind == "video":
            event_kind = MessageKind.VIDEO
            media.append(_media(fields.get("MediaId", ""), "video/mp4"))
        elif kind == "location":
            event_kind = MessageKind.LOCATION
            label = fields.get("Label", "")
            spot = f"{fields.get('Location_X', '')}, {fields.get('Location_Y', '')}"
            text = f"{label} ({spot})" if label else spot
        elif kind == "link":
            text = " ".join(p for p in (fields.get("Title", ""), fields.get("Url", "")) if p)
        else:
            # Events (``enter_agent``, ``subscribe``, …) and unknown types start no turn.
            return None
        media = [m for m in media if m.ref != MEDIA_SCHEME]
        text = text.strip()
        if not text and not media:
            return None
        source = Source(
            platform=self.platform,
            chat_id=user,
            chat_type="dm",
            user_id=user,
            user_name=user,
        )
        return InboundEvent(
            source=source,
            text=text,
            kind=event_kind,
            message_id=fields.get("MsgId") or None,
            media=tuple(media),
        )

    # -- media for the intake ----------------------------------------------------------------------

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        if not attachment.ref.startswith(MEDIA_SCHEME):
            raise ValueError("not a WeCom media reference")
        media_id = attachment.ref[len(MEDIA_SCHEME) :]
        client = self._require_client()
        token = await self._access_token()
        params = {"access_token": token, "media_id": media_id}
        async with client.stream(
            "GET", "/cgi-bin/media/get", extensions={QUERY_EXTENSION: params}, timeout=120.0
        ) as response:
            if response.status_code >= 400:
                raise ConnectionError(f"the media download failed (HTTP {response.status_code})")
            if response.headers.get("content-type", "").startswith("application/json"):
                raise LookupError("WeCom has no such media (it keeps media for three days)")
            seen = 0
            async for chunk in response.aiter_bytes():
                seen += len(chunk)
                if seen > MAX_MEDIA_BYTES:
                    raise ValueError("the media file is larger than WeCom allows")
                yield chunk

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        return seat.register_fetcher(self.platform, self.fetch_media)

    # -- outbound ----------------------------------------------------------------------------------

    def _failure(self, answer: Mapping[str, Any]) -> SendResult:
        code = _code(answer)
        message = str(answer.get("errmsg") or "")
        text = f"WeCom errcode {code}: {message}"
        if code in _RATE_CODES:
            stated = _STATED_WAIT.search(message)
            wait = float(stated.group(1)) if stated else 60.0
            return SendResult.failure(f"rate limited: {text}", retry_after=wait)
        reading = _READINGS.get(code)
        return SendResult.failure(f"{reading} ({text})" if reading else text)

    async def _send_one(self, chat_id: str, text: str, *, retried: bool = False) -> SendResult:
        client = self._require_client()
        try:
            token = await self._access_token()
        except _Refused as exc:
            return SendResult.failure(f"unauthorized: WeCom refused the corp secret ({exc})")
        except httpx.HTTPError as exc:
            return failure_from_exception(exc)
        body = {
            "touser": chat_id,
            "msgtype": "text",
            "agentid": int(self.agent_id),
            "text": {"content": text},
            "safe": 0,
        }
        try:
            response = await client.post(
                "/cgi-bin/message/send",
                json=body,
                extensions={QUERY_EXTENSION: {"access_token": token}},
            )
        except httpx.HTTPError as exc:
            return failure_from_exception(exc)
        if response.status_code >= 400:
            return failure_from_status(
                response.status_code, response.text, response.headers, now=self.clock.now()
            )
        answer: dict[str, Any] = {}
        with contextlib.suppress(ValueError, UnicodeDecodeError):
            answer = dict(response.json())
        code = _code(answer)
        if code in _STALE_TOKEN and not retried:
            self._access = None
            return await self._send_one(chat_id, text, retried=True)
        if code != 0:
            return self._failure(answer)
        return SendResult.sent(str(answer.get("msgid") or ""))

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        chunked = split_message(text, MAX_MESSAGE_BYTES, len_fn=utf8_bytes)
        sent: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            result = await self._send_one(chat_id, chunk.text)
            if not result.ok:
                if not sent:
                    return result
                ambiguous = result.maybe_delivered or is_ambiguous_timeout(text=result.error or "")
                return SendResult.failure(
                    result.error,
                    retry_after=result.retry_after,
                    delivered_ids=tuple(sent),
                    remainder=None if ambiguous else chunked.remainder(n),
                )
            sent.append(result.message_id or "")
        return SendResult.sent(sent[-1], tuple(sent[:-1]))

    def is_final_failure(self, result: SendResult) -> bool:
        return "unauthorized" in (result.error or "")

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(name=chat_id, type="dm")


def _media(media_id: str, mime: str) -> Attachment:
    return Attachment(ref=f"{MEDIA_SCHEME}{media_id.strip()}", mime=mime)
