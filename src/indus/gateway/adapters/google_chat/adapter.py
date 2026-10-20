"""The Google Chat adapter (P16-13): Chat API v1 out, HTTP callbacks or Pub/Sub pull in.

Credentials: the service account's key JSON (``GOOGLE_CHAT_SERVICE_ACCOUNT_JSON``, secret store
first, then the environment) signs the assertion for an access token (``auth``); nothing else.

Connecting fetches a token (proving the key), then, unless the adapter is send-only (``indus
send``): in ``http`` mode starts the callback listener (P16-01 kit; 127.0.0.1:8647
``/google-chat`` unless configured; body ≤ 1 MiB) and builds the token verifier; in ``pubsub``
mode checks the subscription exists. Pulling starts once an inbound sink is installed and stops
when it is removed (pulled messages are acknowledged, so pulling without a sink would lose them).

HTTP callback, in order: Google's bearer token verified (401, nothing parsed, when it fails); no
sink installed, or the gateway draining (``set_drain_probe``) → 503 with ``Retry-After`` so Google
delivers it again later; the JSON parsed (400 when it is not an object); a message already seen
(resource name, one hour) dropped; the event handed on in the background; 200 ``{}`` at once.

Pub/Sub: pull up to 10 messages, hand each on, acknowledge the batch; paused while draining. A
failed pull waits 2 s doubling to 120 s (20 % jitter); 401, 403 or 404 — or ten failures in a
row — stop the loop and report the platform fatal.

Outbound: text split at 4,000 characters (the shared chunker); ``meta["thread_id"]`` threads the
message (``REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD``, so a vanished thread still delivers); a
``users/<id>`` chat id is resolved to its DM space (``spaces:findDirectMessage``, cached); edits
replace the text and drop cards; approvals and clarify prompts are cards v2 with one button per
choice. Media uploads need a person's own credentials in Chat, so sending files is refused (the
sender then tells the chat it could not send them). Chat has no typing API.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast
from urllib.parse import urlsplit

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
)
from indus.gateway.adapters.base import (
    Adapter,
    AdapterConnectError,
    ApprovalPrompt,
    Capabilities,
    ChatInfo,
    ClarifyPrompt,
    FetcherSeat,
    InboundSink,
    approval_callback,
    clarify_callback,
)
from indus.gateway.adapters.google_chat.auth import (
    CHAT_ISSUER,
    CredentialError,
    GoogleTokenVerifier,
    JwkCache,
    KeyFetcher,
    ServiceAccount,
    ServiceAccountTokens,
    TokenError,
    TokenRejected,
    http_key_fetcher,
)
from indus.gateway.adapters.google_chat.client import (
    ChatClient,
    GoogleApiError,
    PubSubClient,
    ReceivedMessage,
    is_message_name,
    is_space_name,
    is_subscription_name,
    is_user_name,
)
from indus.gateway.adapters.google_chat.events import (
    BUTTON_FUNCTION,
    BUTTON_PARAM,
    MEDIA_SCHEME,
    read_event,
    short_id,
    to_inbound,
)
from indus.gateway.core.events import Attachment, InboundEvent
from indus.gateway.core.send import SendResult, is_ambiguous_timeout
from indus.gateway.outbound.chunker import split_message

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "ACCOUNT_ENV",
    "AUDIENCE_ENV",
    "DEFAULT_PATH",
    "DEFAULT_PORT",
    "MAX_TEXT",
    "SUBSCRIPTION_ENV",
    "TOKEN_EMAIL_ENV",
    "GoogleChatAdapter",
    "GoogleChatOptions",
    "is_trusted_media_url",
]

log = logging.getLogger(__name__)

ACCOUNT_ENV = "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON"
SUBSCRIPTION_ENV = "GOOGLE_CHAT_SUBSCRIPTION_NAME"
AUDIENCE_ENV = "GOOGLE_CHAT_HTTP_EVENTS_AUDIENCE"
TOKEN_EMAIL_ENV = "GOOGLE_CHAT_HTTP_EVENTS_SERVICE_ACCOUNT_EMAIL"

# Chat takes 4,096 characters; 4,000 leaves room for the chunk marker.
MAX_TEXT = 4000
DEFAULT_PORT = 8647
DEFAULT_PATH = "/google-chat"
MAX_BODY_BYTES = 1024 * 1024
DEFAULT_MAX_MEDIA_BYTES = 100 * 1024 * 1024
SEEN_TTL_S = 3600.0
SEEN_MAX = 5000
PULL_MAX_MESSAGES = 10
BACKOFF_FIRST_S = 2.0
BACKOFF_CAP_S = 120.0
BACKOFF_JITTER = 0.2
MAX_PULL_FAILURES = 10
DRAIN_PAUSE_S = 5.0
RETRY_LATER_S = 30
CARD_BUTTON_LABEL_MAX = 80
# Attachment download hosts the bot token may be sent to: exact https hosts only (a suffix match
# would include hosts that serve other people's content, such as storage buckets).
TRUSTED_MEDIA_HOSTS: frozenset[str] = frozenset(
    {
        "chat.googleapis.com",
        "www.googleapis.com",
        "chat.google.com",
        "drive.google.com",
        "docs.google.com",
        *(f"lh{n}.googleusercontent.com" for n in range(3, 7)),
    }
)

type Mode = Literal["http", "pubsub"]


def is_trusted_media_url(url: str) -> bool:
    """https and a Google-owned host: the only URLs the bot token is ever sent to."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    return host in TRUSTED_MEDIA_HOSTS


@dataclass(frozen=True)
class GoogleChatOptions:
    """``platforms.google_chat.*`` settings (vendor env names as fallbacks)."""

    mode: Mode = "http"
    subscription: str = ""
    audience: str = ""
    audience_type: str = ""
    token_emails: tuple[str, ...] = (CHAT_ISSUER,)
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES
    pull_max_messages: int = PULL_MAX_MESSAGES

    @classmethod
    def read(
        cls, extra: Mapping[str, Any], lookup: Callable[[str], str | None] = lambda _n: None
    ) -> GoogleChatOptions:
        def text(key: str, env: str = "") -> str:
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            return ((lookup(env) if env else None) or "").strip()

        subscription = text("subscription", SUBSCRIPTION_ENV)
        mode_text = text("mode").lower()
        mode: Mode = (
            cast(Mode, mode_text)
            if mode_text in ("http", "pubsub")
            else ("pubsub" if subscription else "http")
        )
        emails = text("token_email", TOKEN_EMAIL_ENV)
        try:
            cap = int(extra.get("max_media_bytes") or DEFAULT_MAX_MEDIA_BYTES)
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_MEDIA_BYTES
        return cls(
            mode=mode,
            subscription=subscription,
            audience=text("audience", AUDIENCE_ENV),
            audience_type=text("audience_type"),
            token_emails=tuple(e.strip() for e in emails.split(",") if e.strip()) or (CHAT_ISSUER,),
            max_media_bytes=max(1, cap),
        )


class GoogleChatAdapter(Adapter):
    platform: ClassVar[str] = "google_chat"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_TEXT,
        markup="mrkdwn",
        splits_natively=True,
        supports_edit=True,
        supports_delete=True,
        supports_buttons=True,
        dm_is_private=True,
        push_capable=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        # Our own REST clients (no Google SDK); their routes are ``client.CHAT_ROUTES`` /
        # ``client.PUBSUB_ROUTES``, served by the wire fake.
        "indus.gateway.adapters.google_chat.client:ChatClient": (
            "create_message",
            "patch_message",
            "delete_message",
            "get_space",
            "find_direct_message",
            "download_media",
        ),
        "indus.gateway.adapters.google_chat.client:PubSubClient": (
            "get_subscription",
            "pull",
            "acknowledge",
        ),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        account_json: str,
        settings: PlatformSettings | None = None,
        options: GoogleChatOptions | None = None,
        listener: ListenerSettings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        key_fetcher: KeyFetcher | None = None,
        serve: bool = True,
        send_only: bool = False,
        rng: Callable[[], float] = random.random,
    ) -> None:
        held = [account_json]
        with contextlib.suppress(CredentialError):
            held.append(ServiceAccount.parse(account_json).private_key)
        super().__init__(clock=clock, settings=settings, secrets=tuple(held))
        self._account_json = account_json
        self.options = options or GoogleChatOptions()
        self.send_only = send_only
        self._transport = transport
        self._proxy = proxy
        self._key_fetcher = key_fetcher
        self._rng = rng
        self._http: httpx.AsyncClient | None = None
        self._tokens: ServiceAccountTokens | None = None
        self.chat: ChatClient | None = None
        self.pubsub: PubSubClient | None = None
        self.verifier: GoogleTokenVerifier | None = None
        self._seen = SeenIds(clock, SEEN_TTL_S, SEEN_MAX)
        self._dm_spaces: dict[str, str] = {}
        self._puller: asyncio.Task[None] | None = None
        self.listener: CallbackListener | None = None
        if self.options.mode == "http" and not send_only:
            settings_ = listener or ListenerSettings(
                port=DEFAULT_PORT, max_body_bytes=MAX_BODY_BYTES, path=DEFAULT_PATH
            )
            self.listener = CallbackListener(
                settings_, name=self.platform, clock=clock, scrub=self.scrub, serve=serve
            )
            self.listener.route(settings_.path, self._callback, methods=("POST",))

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<GoogleChatAdapter {self.options.mode} {state}>"

    @staticmethod
    def listener_settings(extra: Mapping[str, Any]) -> ListenerSettings:
        """The callback listener's settings from ``platforms.google_chat.webhook_*``."""
        return ListenerSettings.from_extra(
            extra, port=DEFAULT_PORT, max_body_bytes=MAX_BODY_BYTES, path=DEFAULT_PATH
        )

    # -- lifecycle ---------------------------------------------------------------------------------

    # While the base drain probe (``set_drain_probe``, installed by the daemon on platform up) is
    # truthy, callbacks answer 503 and Pub/Sub pulling pauses, so Google keeps the events.

    def _config_problem(self) -> str | None:
        opts = self.options
        if self.send_only:
            return None
        if opts.mode == "pubsub":
            if not is_subscription_name(opts.subscription):
                return (
                    f"set {SUBSCRIPTION_ENV} (or platforms.google_chat.subscription) to "
                    "projects/<project>/subscriptions/<name>"
                )
        elif not opts.audience:
            return (
                f"set {AUDIENCE_ENV} (or platforms.google_chat.audience) to the Chat app's project "
                "number or its HTTP endpoint URL"
            )
        return None

    async def _open(self, is_reconnect: bool) -> None:
        try:
            account = ServiceAccount.parse(self._account_json)
        except CredentialError as exc:
            raise AdapterConnectError(str(exc), retryable=False, code="config") from None
        problem = self._config_problem()
        if problem is not None:
            raise AdapterConnectError(
                f"Google Chat is not configured: {problem}", retryable=False, code="config"
            )
        http = httpx.AsyncClient(
            transport=self._transport,
            proxy=None if self._transport is not None else self._proxy,
            timeout=httpx.Timeout(30.0, read=90.0),
            trust_env=False,
            follow_redirects=False,
        )
        tokens = ServiceAccountTokens(account, http, clock=self.clock)
        try:
            await tokens.token()
            if not self.send_only:
                await self._open_inbound(http, tokens)
        except TokenError as exc:
            await http.aclose()
            raise AdapterConnectError(
                str(exc), retryable=exc.retryable, code="google_chat_auth"
            ) from None
        except CredentialError as exc:
            await http.aclose()
            raise AdapterConnectError(str(exc), retryable=False, code="config") from None
        except BaseException:
            await http.aclose()
            raise
        self._http, self._tokens = http, tokens
        self.chat = ChatClient(http, tokens)
        log.info(
            "google_chat: signed in as %s (%s inbound)",
            account.client_email,
            "no" if self.send_only else self.options.mode,
        )
        if self._sink is not None:
            self._start_pulling()

    async def _open_inbound(self, http: httpx.AsyncClient, tokens: ServiceAccountTokens) -> None:
        opts = self.options
        if opts.mode == "pubsub":
            pubsub = PubSubClient(http, tokens)
            try:
                await pubsub.get_subscription(opts.subscription)
            except GoogleApiError as exc:
                if exc.status in (403, 404):
                    what = "not found" if exc.status == 404 else "not readable by the account"
                    raise AdapterConnectError(
                        f"the Pub/Sub subscription is {what} (HTTP {exc.status})",
                        retryable=False,
                        code="google_chat_subscription",
                    ) from None
                raise AdapterConnectError(str(exc), retryable=True) from None
            self.pubsub = pubsub
            return
        kind = GoogleTokenVerifier.kind_for(opts.audience, opts.audience_type)
        fetcher = self._key_fetcher or http_key_fetcher(http)
        self.verifier = GoogleTokenVerifier(
            audience=opts.audience,
            kind=kind,
            keys=JwkCache(fetcher, self.clock),
            clock=self.clock,
            emails=opts.token_emails,
        )
        if self.listener is not None:
            await self.listener.start()

    async def _close(self) -> None:
        if self.listener is not None:
            await self.listener.stop()
        self._stop_pulling()
        puller, self._puller = self._puller, None
        if puller is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await puller
        http, self._http = self._http, None
        self.chat = self.pubsub = None
        self.verifier = None
        self._tokens = None
        if http is not None:
            await http.aclose()

    def set_sink(self, sink: InboundSink | None) -> None:
        """Installing a sink starts Pub/Sub pulling (never when send-only); removing it stops."""
        super().set_sink(sink)
        if sink is None:
            self._stop_pulling()
        elif self.connected:
            self._start_pulling()

    def _start_pulling(self) -> None:
        if self.pubsub is None or self.send_only:
            return
        if self._puller is None or self._puller.done():
            self._puller = asyncio.ensure_future(self._pull_loop())

    def _stop_pulling(self) -> None:
        if self._puller is not None and not self._puller.done():
            self._puller.cancel()

    @property
    def pulling(self) -> bool:
        return self._puller is not None and not self._puller.done()

    def is_final_failure(self, result: SendResult) -> bool:
        """A refused token (401 twice): no retry or plain-text resend can help."""
        return (result.error or "").startswith("HTTP 401")

    # -- inbound -----------------------------------------------------------------------------------

    def _event_of(self, envelope: Mapping[str, Any]) -> InboundEvent | None:
        ev = read_event(envelope)
        if ev.kind in ("added", "removed"):
            log.info(
                "google_chat: %s space %s",
                "added to" if ev.kind == "added" else "removed from",
                ev.space.get("name") or "?",
            )
            return None
        if ev.kind == "message" and ev.name and not self._seen.first_time(ev.name):
            log.debug("google_chat: message %s seen before; dropped", ev.name)
            return None
        return to_inbound(self.platform, ev)

    async def _callback(self, request: CallbackRequest) -> Reply:
        verifier = self.verifier
        if verifier is None:
            return Reply.json({"error": "not ready"}, 503, headers=_retry_later())
        try:
            await verifier.verify(request.header("authorization"))
        except TokenRejected as exc:
            log.warning(
                "google_chat: callback from %s refused: %s", request.client or "?", exc.reason
            )
            return Reply.json({"error": "unauthorized"}, 401)
        if self._sink is None or self.draining():
            return Reply.json({"error": "try again later"}, 503, headers=_retry_later())
        try:
            envelope = request.json()
        except BadPayload:
            return Reply.json({"error": "bad payload"}, 400)
        if not isinstance(envelope, dict):
            return Reply.json({"error": "bad payload"}, 400)
        event = self._event_of(cast(dict[str, Any], envelope))
        if event is not None and self.listener is not None:
            self.listener.background(self.emit(event))
        return Reply.json({})

    async def _pull_loop(self) -> None:
        failures = 0
        delay = BACKOFF_FIRST_S
        while True:
            pubsub = self.pubsub
            if pubsub is None:
                return
            if self.draining():
                await self.clock.sleep(DRAIN_PAUSE_S)
                continue
            subscription = self.options.subscription
            try:
                received = await pubsub.pull(subscription, self.options.pull_max_messages)
            except asyncio.CancelledError:
                raise
            except GoogleApiError as exc:
                if exc.status in (401, 403, 404):
                    log.error("google_chat: Pub/Sub refused the pull (HTTP %s)", exc.status)
                    self.report_fatal(
                        "google_chat_pubsub",
                        f"Pub/Sub refused the pull (HTTP {exc.status}); check the subscription "
                        "and the service account's subscriber role",
                    )
                    return
                failures += 1
                log.warning("google_chat: Pub/Sub pull failed: %s", self.scrub(str(exc)))
            except Exception as exc:
                failures += 1
                log.warning("google_chat: Pub/Sub pull failed: %s", type(exc).__name__)
            else:
                failures, delay = 0, BACKOFF_FIRST_S
                await self._handle_pulled(pubsub, subscription, received)
                if not received:
                    await self.clock.sleep(1.0)
                continue
            if failures >= MAX_PULL_FAILURES:
                self.report_fatal(
                    "google_chat_pubsub", f"Pub/Sub pull failed {failures} times in a row"
                )
                return
            await self.clock.sleep(delay * (1 + BACKOFF_JITTER * self._rng()))
            delay = min(delay * 2, BACKOFF_CAP_S)

    async def _handle_pulled(
        self, pubsub: PubSubClient, subscription: str, received: list[ReceivedMessage]
    ) -> None:
        acks: list[str] = []
        for message in received:
            acks.append(message.ack_id)
            try:
                envelope = _json_object(message.data)
            except ValueError:
                log.warning("google_chat: a Pub/Sub message is not a JSON object; dropped")
                continue
            event = self._event_of(envelope)
            if event is not None:
                await self.emit(event)
        try:
            await pubsub.acknowledge(subscription, acks)
        except Exception as exc:
            # Redelivered messages are dropped by the seen list.
            log.warning("google_chat: acknowledging failed: %s", self.scrub(str(exc)))

    # -- media for the intake ----------------------------------------------------------------------

    async def fetch_media(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The bytes of an inbound attachment (registered by `register_fetchers`)."""
        chat = self._require_chat()
        ref = attachment.ref
        cap = self.options.max_media_bytes
        if ref.startswith(MEDIA_SCHEME):
            async for chunk in chat.download_media(ref[len(MEDIA_SCHEME) :], cap=cap):
                yield chunk
            return
        if not is_trusted_media_url(ref):
            raise ValueError("refusing to fetch a Google Chat attachment from a non-Google host")
        async for chunk in chat.stream(ref, cap=cap):
            yield chunk

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        return seat.register_fetcher(self.platform, self.fetch_media)

    # -- outbound ----------------------------------------------------------------------------------

    def _require_chat(self) -> ChatClient:
        if self.chat is None:
            raise ConnectionError("not connected to Google Chat")
        return self.chat

    async def _space_for(self, chat_id: str) -> str:
        """The space a send goes to: a space name as is, ``users/<id>`` → its DM space."""
        target = chat_id.strip()
        if is_space_name(target):
            return target
        if is_user_name(target):
            cached = self._dm_spaces.get(target)
            if cached:
                return cached
            found = await self._require_chat().find_direct_message(target)
            space = str(found.get("name") or "")
            if not is_space_name(space):
                raise GoogleApiError(404, "chat not found: no direct message with that user")
            self._dm_spaces[target] = space
            return space
        raise GoogleApiError(404, "chat not found: not a Google Chat space or user name")

    def _failure(
        self,
        exc: BaseException,
        *,
        reading: str | None = None,
        sent: list[str] | None = None,
        remainder: str | None = None,
    ) -> SendResult:
        if isinstance(exc, GoogleApiError):
            # The reading names what a 404 means for this call; other statuses speak for
            # themselves (a 401 must never read as a vanished chat).
            local = reading if exc.status == 404 else None
            if local == "chat not found" and "thread" in exc.detail.lower():
                local = "thread not found"
            base = failure_from_status(
                exc.status, exc.detail, exc.headers, now=self.clock.now(), reading=local
            )
        elif isinstance(exc, httpx.HTTPError):
            base = failure_from_exception(exc)
        elif isinstance(exc, ValueError):
            base = SendResult.failure(f"chat not found: {exc}")
        else:
            base = SendResult.failure(exc=exc)
        ids = tuple(sent or ())
        if not ids:
            return base
        keep = None if (base.maybe_delivered or is_ambiguous_timeout(exc)) else remainder
        return replace(
            base,
            message_id=ids[-1],
            continuation_ids=ids[:-1],
            partial=True,
            remainder=keep,
        )

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        chat = self._require_chat()
        thread = str(meta.get("thread_id") or "")
        try:
            space = await self._space_for(chat_id)
        except Exception as exc:
            return self._failure(exc, reading="chat not found")
        chunked = split_message(text, MAX_TEXT)
        sent: list[str] = []
        for n, chunk in enumerate(chunked.chunks):
            if n:
                await self.slots.pace(chat_id)
            body: dict[str, Any] = {"text": chunk.text}
            if thread:
                body["thread"] = {"name": thread}
            try:
                message = await chat.create_message(space, body, in_thread=bool(thread))
            except Exception as exc:
                return self._failure(
                    exc, reading="chat not found", sent=sent, remainder=chunked.remainder(n)
                )
            sent.append(short_id(str(message.get("name") or "")))
        return SendResult.sent(sent[-1], tuple(sent[:-1]))

    def _message_name(self, chat_id: str, message_id: str) -> str:
        if is_message_name(message_id):
            return message_id
        return f"{chat_id}/messages/{message_id}"

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        if len(text) > MAX_TEXT:
            return SendResult.failure("message is too long for one Google Chat edit")
        try:
            space = await self._space_for(chat_id)
            name = self._message_name(space, message_id)
            await self._require_chat().patch_message(
                name, {"text": text, "cardsV2": []}, update_mask="text,cardsV2"
            )
        except Exception as exc:
            return self._failure(exc, reading="message not found")
        return SendResult.sent(message_id)

    async def _delete(self, chat_id: str, message_id: str) -> bool:
        space = await self._space_for(chat_id)
        await self._require_chat().delete_message(self._message_name(space, message_id))
        return True

    async def _card(
        self,
        chat_id: str,
        card_id: str,
        title: str,
        text: str,
        fallback: str,
        buttons: list[tuple[str, str]],
        meta: dict[str, Any],
    ) -> SendResult:
        widgets: list[dict[str, Any]] = [{"textParagraph": {"text": text}}]
        widgets.append(
            {
                "buttonList": {
                    "buttons": [
                        {
                            "text": _label(label),
                            "onClick": {
                                "action": {
                                    "function": BUTTON_FUNCTION,
                                    "parameters": [{"key": BUTTON_PARAM, "value": data}],
                                }
                            },
                        }
                        for label, data in buttons
                    ]
                }
            }
        )
        body: dict[str, Any] = {
            "text": fallback,
            "cardsV2": [
                {
                    "cardId": card_id,
                    "card": {"header": {"title": title}, "sections": [{"widgets": widgets}]},
                }
            ],
        }
        thread = str(meta.get("thread_id") or "")
        if thread:
            body["thread"] = {"name": thread}
        try:
            space = await self._space_for(chat_id)
            message = await self._require_chat().create_message(space, body, in_thread=bool(thread))
        except Exception as exc:
            return self._failure(exc, reading="chat not found")
        return SendResult.sent(short_id(str(message.get("name") or "")))

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        buttons = [
            (action.label, approval_callback(prompt.request_id, action.choice))
            for action in prompt.actions
        ]
        return await self._card(
            chat_id,
            f"approval-{prompt.request_id}",
            "Approval needed",
            prompt.text,
            prompt.text,
            buttons,
            meta,
        )

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        buttons = [
            (choice, clarify_callback(prompt.clarify_id, n))
            for n, choice in enumerate(prompt.choices)
        ]
        return await self._card(
            chat_id,
            f"clarify-{prompt.clarify_id}",
            "Question",
            prompt.question,
            prompt.fallback_text or prompt.question,
            buttons,
            meta,
        )

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        space = await self._require_chat().get_space(await self._space_for(chat_id))
        kind = str(space.get("spaceType") or space.get("type") or "").upper()
        dm = kind in ("DIRECT_MESSAGE", "DM") or space.get("singleUserBotDm") is True
        return ChatInfo(name=str(space.get("displayName") or chat_id), type="dm" if dm else "group")


def _label(text: str) -> str:
    clean = " ".join(text.split())
    if len(clean) <= CARD_BUTTON_LABEL_MAX:
        return clean
    return clean[: CARD_BUTTON_LABEL_MAX - 1] + "…"


def _retry_later() -> tuple[tuple[str, str], ...]:
    return (("retry-after", str(RETRY_LATER_S)),)


def _json_object(data: bytes) -> dict[str, Any]:
    try:
        loaded = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("not JSON") from None
    if not isinstance(loaded, dict):
        raise ValueError("not a JSON object")
    return cast(dict[str, Any], loaded)
