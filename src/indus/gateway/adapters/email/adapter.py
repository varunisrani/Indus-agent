"""Email over IMAP and SMTP with the Python standard library (P16-09).

One IMAP session stays logged in. New mail is noticed with IDLE when the server offers it and
Python has ``IMAP4.idle`` (3.14 and later), else by a check every ``poll_seconds`` (60). Mail that
was in the inbox when the adapter first started is remembered as seen and never answered.

Every new message is screened on a bounded header fetch (``HEADER_LIMIT``) before its body is
downloaded: our own address, automated mail (auto-replies, bounces, list mail), a missing or
ambiguous From, an oversized message, an unknown sender (email ignores strangers unless
``unauthorized_dm_behavior`` is ``pair`` or ``decline``, R06.8) and a From domain the receiving
server did not authenticate (``parsing.verify_sender``) are all dropped unread and marked read.
Admitted mail is fetched whole (which marks it read) and becomes one direct-message event per
sender address; attachments wait in memory for the media intake (``fetch_attachment``, registered
by `register_fetchers`). A send-only adapter (``AdapterConfig.send_only``, ``indus send``) probes
SMTP and sends; it never logs in to the mailbox. A login the IMAP server refuses after connect is
reported through `Adapter.report_fatal` (``email_auth``) and the mailbox loop stops.

Replies go out one SMTP connection each, as plain UTF-8 text threaded with ``In-Reply-To`` and
``References``, ``Re:`` subjects and a fresh ``Message-ID`` that is the send's message id. Blocking
mail calls run on the ``email`` pool, never on the event loop. The password is passed only to
``login`` and is masked in every error and log line.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import imaplib
import logging
import mimetypes
import os
import re
import secrets
import smtplib
import socket
import ssl
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage, Message
from email.parser import BytesHeaderParser
from email.policy import SMTP as SMTP_POLICY
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any, ClassVar, Literal, Protocol
from urllib.parse import quote, unquote

from indus.foundation.blocking import BlockingPool, run_blocking
from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits
from indus.gateway.adapters.base import (
    REPLY_TO_KEY,
    Adapter,
    AdapterConnectError,
    Capabilities,
    ChatInfo,
    FetcherSeat,
    MediaKind,
)
from indus.gateway.adapters.email.parsing import (
    NO_PIN,
    MailPart,
    attachments_of,
    body_text,
    clean_ids,
    decode_header_value,
    display_name_of,
    is_automated,
    one_line,
    sender_address,
    verify_sender,
)
from indus.gateway.config import AdapterConfig, PlatformSettings, read_secret
from indus.gateway.core.authz import allow_set
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind, Source
from indus.gateway.core.send import ErrorKind, SendResult
from indus.gateway.intake.media import MediaRejected

__all__ = [
    "ATTACHMENT_REF",
    "EMAIL_POOL",
    "HEADER_LIMIT",
    "MAX_BODY",
    "EmailAdapter",
    "EmailSettings",
    "open_imap",
    "open_smtp",
]

log = logging.getLogger(__name__)

MAX_BODY = 50_000
POLL_SECONDS = 60.0
IDLE_SECONDS = 300.0
CONNECT_TIMEOUT = 30.0
HEADER_LIMIT = 64 * 1024
MESSAGE_LIMIT = 50 * 1024 * 1024
OUTBOUND_FILE_LIMIT = 25 * 1024 * 1024
INBOUND_TEXT_MAX = 100_000
SEEN_MAX = 2000
THREADS_MAX = 1000
PARTS_PER_MESSAGE = 32
STORE_BYTES = 64 * 1024 * 1024
STORE_SECONDS = 1800.0
FAILURE_WAIT_MAX = 900.0
DEFAULT_SUBJECT = "Indus Agent"
ATTACHMENT_REF = "email-attachment:"

_SCREEN_FIELDS = (
    "FROM",
    "AUTHENTICATION-RESULTS",
    "AUTO-SUBMITTED",
    "PRECEDENCE",
    "LIST-ID",
    "LIST-UNSUBSCRIBE",
)
# One byte past the cap is asked for, so a cut-off answer shows it was too long.
SCREEN_QUERY = (
    f"(RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({' '.join(_SCREEN_FIELDS)})]<0.{HEADER_LIMIT + 1}>)"
)
FULL_QUERY = "(BODY[])"
_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")
_REPLY_SUBJECT = re.compile(r"^\s*(re|aw|sv|antw)\s*:", re.IGNORECASE)
_WAIT = re.compile(r"(?:retry after|try again in)\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_ENHANCED = re.compile(r"^\s*([245])\.(\d{1,3})\.(\d{1,3})")
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_AUTH_REFUSED = ("authenticationfailed", "authentication failed", "invalid credentials")

EMAIL_POOL = BlockingPool("email", 4, queue_limit=32)


def _login_refused(exc: BaseException) -> bool:
    """The server refused the credentials (as opposed to a network or server failure)."""
    return isinstance(exc, smtplib.SMTPAuthenticationError) or (
        isinstance(exc, imaplib.IMAP4.error) and any(n in str(exc).lower() for n in _AUTH_REFUSED)
    )


# The gateway builds a fresh adapter for every reconnect: what was seen survives here, per mailbox
# and per process, so mail that arrived during an outage is read instead of being skipped.
_SNAPSHOTS: dict[tuple[str, str], tuple[str | None, frozenset[str]]] = {}
_SNAPSHOTS_LOCK = threading.Lock()

Security = Literal["tls", "starttls", "plain"]
_SECURITY: Mapping[str, Security] = {
    "tls": "tls",
    "ssl": "tls",
    "implicit": "tls",
    "starttls": "starttls",
    "plain": "plain",
    "none": "plain",
}

type Runner = Callable[..., Awaitable[Any]]


class ImapLike(Protocol):
    def login(self, user: str, password: str) -> tuple[str, list[Any]]: ...
    def select(self, mailbox: str = ...) -> tuple[str, list[Any]]: ...
    def response(self, code: str) -> tuple[str, list[Any]]: ...
    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]: ...
    def xatom(self, name: str, *args: Any) -> tuple[str, list[Any]]: ...
    def logout(self) -> tuple[str, list[Any]]: ...
    def shutdown(self) -> None: ...


class SmtpLike(Protocol):
    def login(self, user: str, password: str) -> Any: ...
    def send_message(self, msg: Message) -> Any: ...
    def quit(self) -> Any: ...
    def close(self) -> None: ...


def _flag(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in _TRUTHY


def _port(value: object, default: int) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return port if 0 < port < 65536 else default


def _security(value: object, default: Security) -> Security:
    raw = str(value or "").strip().lower().replace("-", "").replace("_", "")
    if not raw:
        return default
    found = _SECURITY.get(raw)
    if found is None:
        log.warning("email: unknown security mode %r; using %s", value, default)
        return default
    return found


@dataclass(frozen=True)
class EmailSettings:
    address: str
    password: str = field(default="", repr=False)
    imap_host: str = ""
    smtp_host: str = ""
    imap_port: int = 993
    smtp_port: int = 587
    imap_security: Security = "tls"
    smtp_security: Security = "starttls"
    imap_tls_verify: bool = True
    smtp_tls_verify: bool = True
    poll_seconds: float = POLL_SECONDS
    authserv_id: str = ""
    require_authenticated_sender: bool = True
    skip_attachments: bool = False
    allow_from: frozenset[str] = frozenset()
    # What a stranger gets: ``ignore`` (always, unless opted into ``pair`` or ``decline``).
    unknown_senders: Literal["ignore", "pair", "decline"] = "ignore"

    def missing(self) -> list[str]:
        wanted = (
            ("EMAIL_ADDRESS", self.address),
            ("EMAIL_PASSWORD", self.password),
            ("EMAIL_IMAP_HOST", self.imap_host),
            ("EMAIL_SMTP_HOST", self.smtp_host),
        )
        return [name for name, value in wanted if not value]

    @property
    def domain(self) -> str:
        return self.address.rpartition("@")[2] or "localhost"

    @classmethod
    def from_config(cls, config: AdapterConfig) -> EmailSettings:
        scope, extra = config.scope, config.extra

        def setting(env: str, key: str) -> str:
            return (read_secret(scope, env) or str(extra.get(key) or "")).strip()

        imap_port = _port(read_secret(scope, "EMAIL_IMAP_PORT") or extra.get("imap_port"), 993)
        smtp_port = _port(read_secret(scope, "EMAIL_SMTP_PORT") or extra.get("smtp_port"), 587)
        behaviour = str(extra.get("unauthorized_dm_behavior") or "").strip().lower()
        unknown: Literal["ignore", "pair", "decline"] = (
            "pair" if behaviour == "pair" else "decline" if behaviour == "decline" else "ignore"
        )
        try:
            poll = max(5.0, float(extra.get("poll_seconds") or POLL_SECONDS))
        except (TypeError, ValueError):
            poll = POLL_SECONDS
        return cls(
            address=setting("EMAIL_ADDRESS", "address").lower(),
            password=read_secret(scope, "EMAIL_PASSWORD") or "",
            imap_host=setting("EMAIL_IMAP_HOST", "imap_host"),
            smtp_host=setting("EMAIL_SMTP_HOST", "smtp_host"),
            imap_port=imap_port,
            smtp_port=smtp_port,
            imap_security=_security(
                extra.get("imap_security"), "starttls" if imap_port == 143 else "tls"
            ),
            smtp_security=_security(
                extra.get("smtp_security"), "tls" if smtp_port == 465 else "starttls"
            ),
            imap_tls_verify=_flag(extra.get("imap_tls_verify"), True),
            smtp_tls_verify=_flag(extra.get("smtp_tls_verify"), True),
            poll_seconds=poll,
            authserv_id=(
                str(extra.get("authserv_id") or "") or read_secret(scope, "EMAIL_AUTHSERV_ID") or ""
            )
            .strip()
            .lower(),
            require_authenticated_sender=_flag(extra.get("require_authenticated_sender"), True),
            skip_attachments=_flag(extra.get("skip_attachments"), False),
            allow_from=frozenset(a.lower() for a in allow_set(extra.get("allow_from"))),
            unknown_senders=unknown,
        )


# -- connections (the default factories) -----------------------------------------------------------


def _tls_context(verify: bool, host: str) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify:
        if host not in _LOOPBACK:
            log.warning("email: certificate checks are off for %s (not a loopback host)", host)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _plain_warning(kind: str, host: str) -> None:
    if host not in _LOOPBACK:
        log.warning("email: %s to %s is not encrypted; the password crosses in clear", kind, host)


def open_imap(settings: EmailSettings) -> imaplib.IMAP4:
    """An IMAP connection with TLS set up (implicit TLS, STARTTLS, or plain when asked)."""
    host, port = settings.imap_host, settings.imap_port
    context = _tls_context(settings.imap_tls_verify, host)
    if settings.imap_security == "tls":
        return imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=CONNECT_TIMEOUT)
    imap = imaplib.IMAP4(host, port, timeout=CONNECT_TIMEOUT)
    if settings.imap_security == "starttls":
        try:
            imap.starttls(ssl_context=context)
        except Exception:
            with contextlib.suppress(Exception):
                imap.shutdown()
            raise
    else:
        _plain_warning("IMAP", host)
    return imap


def open_smtp(settings: EmailSettings) -> smtplib.SMTP:
    """An SMTP connection with TLS set up (connect timeout ``CONNECT_TIMEOUT``)."""
    host, port = settings.smtp_host, settings.smtp_port
    context = _tls_context(settings.smtp_tls_verify, host)
    if settings.smtp_security == "tls":
        return smtplib.SMTP_SSL(host, port, context=context, timeout=CONNECT_TIMEOUT)
    smtp = smtplib.SMTP(host, port, timeout=CONNECT_TIMEOUT)
    if settings.smtp_security == "starttls":
        try:
            smtp.starttls(context=context)
        except Exception:
            smtp.close()
            raise
    else:
        _plain_warning("SMTP", host)
    return smtp


# -- small helpers ---------------------------------------------------------------------------------


def _payload(data: Sequence[Any]) -> tuple[bytes, bytes] | None:
    """``(response line, literal)`` of a one-message FETCH answer, or None for another shape."""
    for item in data or ():
        if isinstance(item, tuple) and len(item) >= 2:  # pyright: ignore[reportUnknownArgumentType]
            head, body = item[0], item[1]  # pyright: ignore[reportUnknownVariableType]
            if isinstance(head, bytes | bytearray) and isinstance(body, bytes | bytearray):
                return bytes(head), bytes(body)
    return None


def _uid_text(value: object) -> str:
    return value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)


def _read_file(path: str, limit: int) -> bytes:
    size = os.stat(path).st_size
    if size > limit:
        raise ValueError(f"file too large for email ({size} bytes; the limit is {limit})")
    with open(path, "rb") as handle:
        return handle.read()


class _Staged(Exception):
    """An SMTP failure and the stage it happened in (``connect`` or ``send``)."""

    def __init__(self, stage: str, error: BaseException) -> None:
        super().__init__(str(error))
        self.stage = stage
        self.error = error


@dataclass(frozen=True)
class _Thread:
    subject: str
    message_id: str
    references: tuple[str, ...]


@dataclass(frozen=True)
class _Mail:
    uid: str
    sender: str
    name: str
    subject: str
    message_id: str
    in_reply_to: str
    references: tuple[str, ...]
    body: str
    parts: tuple[MailPart, ...]
    sent_at: datetime | None


class _PartStore:
    """Received attachments waiting for the intake: bounded by bytes and age."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, MailPart]] = OrderedDict()
        self._bytes = 0

    def _evict(self) -> None:
        now = self._clock.monotonic()
        while self._items:
            token, (expires, part) = next(iter(self._items.items()))
            if expires > now and self._bytes <= STORE_BYTES:
                break
            del self._items[token]
            self._bytes -= len(part.data)

    def put(self, part: MailPart) -> str:
        token = secrets.token_hex(8)
        self._items[token] = (self._clock.monotonic() + STORE_SECONDS, part)
        self._bytes += len(part.data)
        self._evict()
        return token

    def get(self, token: str) -> MailPart | None:
        self._evict()
        found = self._items.get(token)
        return found[1] if found is not None else None


_NOT_AN_ADDRESS = SendResult(
    ok=False,
    error="chat not found: the chat id is not one plain email address",
    kind=ErrorKind.NOT_FOUND,
    chat_level=True,
)


async def _pooled(fn: Callable[..., Any], *args: Any) -> Any:
    return await run_blocking(fn, *args, pool=EMAIL_POOL, label="email")


# -- the adapter -----------------------------------------------------------------------------------


class EmailAdapter(Adapter):
    platform: ClassVar[str] = "email"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_BODY,
        markup="plain",
        owns_access_policy=True,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "imaplib:IMAP4": ("login", "select", "response", "uid", "xatom", "logout", "shutdown"),
        "smtplib:SMTP": ("login", "send_message", "quit", "close", "starttls"),
    }

    def __init__(
        self,
        *,
        clock: Clock,
        mail: EmailSettings,
        settings: PlatformSettings | None = None,
        env: Mapping[str, str] | None = None,
        is_approved: Callable[[str], bool] | None = None,
        imap_factory: Callable[[EmailSettings], ImapLike] = open_imap,
        smtp_factory: Callable[[EmailSettings], SmtpLike] = open_smtp,
        runner: Runner | None = None,
        send_only: bool = False,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(mail.password,))
        self.mail = mail
        # ``indus send`` (``AdapterConfig.send_only``): SMTP only, never the mailbox.
        self.send_only = send_only
        self._env: Mapping[str, str] = env if env is not None else {}
        self._is_approved = is_approved or (lambda _sender: False)
        self._imap_factory = imap_factory
        self._smtp_factory = smtp_factory
        self._run: Runner = runner or _pooled
        self._imap: ImapLike | None = None
        self._imap_lock = threading.Lock()
        self._seen: set[str] = set()
        self._baselined = False
        self._uidvalidity: str | None = None
        self._running = False
        self._poller: asyncio.Task[None] | None = None
        self._threads: OrderedDict[str, _Thread] = OrderedDict()
        self._by_id: OrderedDict[str, _Thread] = OrderedDict()
        self._parts = _PartStore(clock)
        self._pin_warned = False

    @classmethod
    def from_config(cls, config: AdapterConfig) -> EmailAdapter:
        from indus.state.pairing import PairingStore

        store = PairingStore(config.scope)
        return cls(
            clock=config.clock,
            mail=EmailSettings.from_config(config),
            settings=config.settings,
            env=config.scope.env,
            is_approved=lambda sender: store.is_approved("email", sender),
            send_only=config.send_only,
        )

    def __repr__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"<EmailAdapter {self.mail.address or '?'} {state}>"

    # -- who may write ---------------------------------------------------------------------------

    def _env_on(self, name: str) -> bool:
        return str(self._env.get(name) or "").strip().lower() in _TRUTHY

    def _listed(self, sender: str) -> bool:
        """Admitted by a list or an allow-all switch, read the way the gateway's chain reads
        them (the global switch counts only while no env allowlist is set)."""
        if self._env_on("INDUS_EMAIL_ALLOW_ALL_USERS"):
            return True
        env_lists = [
            allow_set(self._env.get(name))
            for name in ("INDUS_EMAIL_ALLOWED_USERS", "INDUS_GATEWAY_ALLOWED_USERS")
        ]
        lists = set(self.mail.allow_from)
        for found in env_lists:
            lists |= {a.lower() for a in found}
        if "*" in lists or sender in lists:
            return True
        return not any(env_lists) and self._env_on("INDUS_GATEWAY_ALLOW_ALL_USERS")

    def _known(self, sender: str) -> bool:
        if self._listed(sender):
            return True
        try:
            return self._is_approved(sender) is True
        except Exception as exc:
            log.warning("email: pairing approvals could not be read: %s", type(exc).__name__)
            return False

    def access_policy(self, kind: Literal["dm", "group"]) -> str:
        if kind != "dm":
            return ""
        return "pairing" if self.mail.unknown_senders == "pair" else "allowlist"

    def allows_dm(self, user_id: str) -> bool:
        return self._listed(str(user_id).strip().lower())

    # -- lifecycle -------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        missing = self.mail.missing()
        if missing:
            raise AdapterConnectError(
                f"Email is not configured: set {', '.join(missing)}",
                retryable=False,
                code="config",
            )
        if is_reconnect and not self._baselined:
            self._restore_seen()
        if not self.send_only:
            try:
                await self._run(self._open_mailbox)
            except Exception as exc:
                raise self._connect_error("IMAP", exc) from None
        try:
            await self._run(self._smtp_probe)
        except Exception as exc:
            if not self.send_only:
                await self._run(self._logout)
            raise self._connect_error("SMTP", exc) from None
        unpinned = self.mail.require_authenticated_sender and not self.mail.authserv_id
        if unpinned and not self._pin_warned:
            self._pin_warned = True
            log.warning(
                "email: no authserv-id is pinned, so no From address can be trusted and every "
                "message is dropped; set platforms.email.extra.authserv_id (or "
                "EMAIL_AUTHSERV_ID) to the id your mail server writes first in "
                "Authentication-Results, or set require_authenticated_sender: false"
            )
        self._running = True
        if not self.send_only:
            self._poller = asyncio.ensure_future(self._inbox_loop())
        log.info("email: connected as %s", self.mail.address)

    def _connect_error(self, what: str, exc: BaseException) -> AdapterConnectError:
        text = self.scrub(f"{type(exc).__name__}: {exc}")
        if _login_refused(exc):
            return AdapterConnectError(
                f"{what} login refused for {self.mail.address}: check EMAIL_PASSWORD ({text})",
                retryable=False,
                code="auth",
            )
        host = self.mail.imap_host if what == "IMAP" else self.mail.smtp_host
        return AdapterConnectError(f"{what} connection to {host} failed: {text}", retryable=True)

    async def _close(self) -> None:
        self._running = False
        self._interrupt()
        if self._poller is not None:
            poller, self._poller = self._poller, None
            if not poller.done():
                poller.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await poller
        with contextlib.suppress(Exception):
            await self._run(self._logout)

    def _interrupt(self) -> None:
        """Break a blocking IDLE wait (another thread) so the session can be closed."""
        imap = self._imap
        if imap is not None and self._imap_lock.locked():
            with contextlib.suppress(Exception):
                imap.shutdown()

    # -- the mailbox (worker thread) -------------------------------------------------------------

    def _open_mailbox(self) -> None:
        with self._imap_lock:
            self._drop_session()
            self._imap = self._login()

    def _login(self) -> ImapLike:
        imap = self._imap_factory(self.mail)
        try:
            imap.login(self.mail.address, self.mail.password)
            if "ID" in tuple(getattr(imap, "capabilities", ()) or ()):
                with contextlib.suppress(Exception):
                    imap.xatom("ID", '("name" "Indus Agent")')
            status, _ = imap.select("INBOX")
            if status != "OK":
                raise imaplib.IMAP4.error("INBOX could not be opened")
            _, validity = imap.response("UIDVALIDITY")
            current = _uid_text(validity[0]) if validity and validity[0] is not None else None
            if current is not None and self._uidvalidity not in (None, current):
                log.info("email: the mailbox was renumbered (UIDVALIDITY); seen list cleared")
                self._seen.clear()
            self._uidvalidity = current
            if not self._baselined:
                status, data = imap.uid("SEARCH", None, "ALL")
                if status == "OK" and data and data[0]:
                    self._seen.update(_uid_text(u) for u in data[0].split())
                self._trim_seen()
                self._baselined = True
                log.info("email: %d existing messages will not be answered", len(self._seen))
            self._save_seen()
        except BaseException:
            self._quietly_close(imap)
            raise
        return imap

    @staticmethod
    def _quietly_close(imap: ImapLike) -> None:
        try:
            imap.logout()
        except Exception:
            with contextlib.suppress(Exception):
                imap.shutdown()

    def _drop_session(self) -> None:
        imap, self._imap = self._imap, None
        if imap is not None:
            self._quietly_close(imap)

    def _logout(self) -> None:
        if self._imap_lock.acquire(timeout=5.0):
            try:
                self._drop_session()
            finally:
                self._imap_lock.release()
        else:
            self._interrupt()

    def _session(self) -> ImapLike:
        if self._imap is None:
            self._imap = self._login()
        return self._imap

    def _trim_seen(self) -> None:
        if len(self._seen) <= SEEN_MAX:
            return
        ordered = sorted(self._seen, key=lambda u: int(u) if is_digits(u) else -1)
        self._seen = set(ordered[-(SEEN_MAX // 2) :])

    def _mailbox_key(self) -> tuple[str, str]:
        return self.mail.address, self.mail.imap_host

    def _restore_seen(self) -> None:
        with _SNAPSHOTS_LOCK:
            found = _SNAPSHOTS.get(self._mailbox_key())
        if found is not None:
            self._uidvalidity, seen = found
            self._seen = set(seen)
            self._baselined = True

    def _save_seen(self) -> None:
        with _SNAPSHOTS_LOCK:
            _SNAPSHOTS[self._mailbox_key()] = (self._uidvalidity, frozenset(self._seen))

    def _mark_seen(self, uid: str) -> None:
        self._seen.add(uid)
        self._trim_seen()

    def _consume(self, imap: ImapLike, uid: str) -> None:
        """Remember a dropped message and mark it read without downloading it."""
        self._mark_seen(uid)
        with contextlib.suppress(Exception):
            imap.uid("STORE", uid, "+FLAGS.SILENT", r"(\Seen)")

    def _fetch_new(self) -> tuple[list[_Mail], Exception | None]:
        """New admitted mail, and the error that stopped the check part way (if one did)."""
        found: list[_Mail] = []
        with self._imap_lock:
            if not self._running:
                return found, None
            try:
                imap = self._session()
                status, data = imap.uid("SEARCH", None, "UNSEEN")
                if status != "OK":
                    raise imaplib.IMAP4.error(f"SEARCH answered {status}")
                for raw in data[0].split() if data and data[0] else []:
                    uid = _uid_text(raw)
                    if uid in self._seen:
                        continue
                    mail = self._fetch_one(imap, uid)
                    if mail is not None:
                        found.append(mail)
            except Exception as exc:
                self._drop_session()
                return found, exc
            finally:
                self._save_seen()
        return found, None

    def _fetch_one(self, imap: ImapLike, uid: str) -> _Mail | None:
        status, data = imap.uid("FETCH", uid, SCREEN_QUERY)
        if status != "OK":
            return None
        answer = _payload(data)
        if answer is None or len(answer[1]) > HEADER_LIMIT:
            log.warning("email: message %s has unusable headers; skipped", uid)
            self._consume(imap, uid)
            return None
        sized = _SIZE.search(answer[0])
        size = int(sized.group(1)) if sized else None
        headers = BytesHeaderParser().parsebytes(answer[1])
        sender = self._screen(headers, size)
        if sender is None:
            self._consume(imap, uid)
            return None
        status, data = imap.uid("FETCH", uid, FULL_QUERY)
        if status != "OK":
            return None  # left unread: the next check tries again
        self._mark_seen(uid)
        whole = _payload(data)
        if whole is None:
            log.warning("email: message %s came back in an unexpected shape; skipped", uid)
            return None
        try:
            return self._parse(uid, sender, whole[1])
        except Exception as exc:
            log.warning("email: message %s could not be read: %s", uid, type(exc).__name__)
            return None

    def _screen(self, headers: Message, size: int | None) -> str | None:
        """The sender when this message may be downloaded, else None (and why, logged)."""
        senders = headers.get_all("From") or []
        sender = sender_address(senders[0]) if len(senders) == 1 else ""
        if not sender:
            log.debug("email: message without one usable From address dropped")
            return None
        if sender == self.mail.address:
            return None
        if is_automated(sender, headers):
            log.debug("email: automated mail from %s dropped", sender)
            return None
        if size is not None and size > MESSAGE_LIMIT:
            log.warning("email: message of %d bytes from %s dropped (limit 50 MB)", size, sender)
            return None
        known = self._known(sender)
        if not known and self.mail.unknown_senders == "ignore":
            log.debug("email: mail from unknown sender %s ignored", sender)
            return None
        if self.mail.require_authenticated_sender:
            results = headers.get_all("Authentication-Results") or []
            verdict, reason = verify_sender(results, sender, authserv_id=self.mail.authserv_id)
            if not verdict:
                # A missing pin is announced once at connect, not once per message.
                level = logging.WARNING if known and reason != NO_PIN else logging.DEBUG
                log.log(
                    level,
                    "email: mail from %s dropped: From not authenticated (%s)",
                    sender,
                    reason,
                )
                return None
        return sender

    def _parse(self, uid: str, sender: str, raw: bytes) -> _Mail | None:
        message = email.message_from_bytes(raw)
        if sender_address(message.get("From")) != sender:
            log.warning("email: message %s changed sender between fetches; dropped", uid)
            return None
        subject = one_line(decode_header_value(message.get("Subject")), 300)
        ids = clean_ids(message.get("Message-ID"))
        parents = clean_ids(message.get("In-Reply-To"))
        sent_at: datetime | None = None
        with contextlib.suppress(Exception):
            sent_at = parsedate_to_datetime(str(message.get("Date") or ""))
        body = body_text(message)
        if len(body) > INBOUND_TEXT_MAX:
            body = body[:INBOUND_TEXT_MAX] + "\n[... the rest of this email was cut]"
        parts = (
            ()
            if self.mail.skip_attachments
            else tuple(attachments_of(message, limit=PARTS_PER_MESSAGE))
        )
        return _Mail(
            uid=uid,
            sender=sender,
            name=display_name_of(message.get("From")),
            subject=subject,
            message_id=ids[0] if ids else "",
            in_reply_to=parents[-1] if parents else "",
            references=tuple(clean_ids(message.get("References"), message.get("In-Reply-To"))),
            body=body,
            parts=parts,
            sent_at=sent_at,
        )

    def _idle_ready(self) -> bool:
        imap = self._imap
        caps = tuple(getattr(imap, "capabilities", ()) or ()) if imap is not None else ()
        return imap is not None and hasattr(imap, "idle") and "IDLE" in caps

    def _idle_once(self) -> None:
        """Wait (IMAP IDLE, RFC 2177) until the server reports a change or ``IDLE_SECONDS``."""
        with self._imap_lock:
            if not self._running:
                return
            imap = self._session()
            idle = getattr(imap, "idle", None)
            if idle is None:
                return
            try:
                with idle(duration=IDLE_SECONDS) as idler:
                    for _response in idler:
                        break
            except Exception:
                self._drop_session()
                raise

    # -- the inbox loop --------------------------------------------------------------------------

    async def _inbox_loop(self) -> None:
        failures = 0
        while self._running:
            try:
                if self._idle_ready():
                    await self._run(self._idle_once)
                else:
                    await self.clock.sleep(self.mail.poll_seconds)
                if not self._running:
                    return
                found, error = await self._run(self._fetch_new)
                # What was fetched before a failure is already marked read: hand it on first.
                for mail in found:
                    event = self._event_for(mail)
                    if event is not None:
                        await self.emit(event)
                if error is not None:
                    raise error
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _login_refused(exc):
                    # Retrying a refused login cannot help (and may lock the account).
                    log.error("email: the IMAP server refused the login; the mailbox loop stops")
                    self.report_fatal(
                        "email_auth",
                        "the IMAP server refused the login; check EMAIL_ADDRESS and "
                        "EMAIL_PASSWORD (an app password where the provider needs one)",
                    )
                    return
                failures += 1
                wait = min(FAILURE_WAIT_MAX, self.mail.poll_seconds * 2 ** (failures - 1))
                log.warning(
                    "email: mailbox check failed (%s); trying again in %.0f s",
                    self.scrub(f"{type(exc).__name__}: {exc}"),
                    wait,
                )
                await self.clock.sleep(wait)

    def _remember(self, key: str, thread: _Thread, store: OrderedDict[str, _Thread]) -> None:
        store.pop(key, None)
        store[key] = thread
        while len(store) > THREADS_MAX:
            store.popitem(last=False)

    def _event_for(self, mail: _Mail) -> InboundEvent | None:
        subject = mail.subject
        text = mail.body
        if subject and not _REPLY_SUBJECT.match(subject):
            text = f"[Subject: {subject}]\n\n{text}" if text else f"[Subject: {subject}]"
        media: list[Attachment] = []
        kind = MessageKind.TEXT
        for part in mail.parts:
            token = self._parts.put(part)
            media.append(
                Attachment(
                    ref=f"{ATTACHMENT_REF}{token}#{quote(part.name, safe='')}", mime=part.mime
                )
            )
        if mail.parts:
            major = mail.parts[0].mime.split("/", 1)[0]
            kind = {
                "image": MessageKind.PHOTO,
                "audio": MessageKind.VOICE,
                "video": MessageKind.VIDEO,
            }.get(major, MessageKind.DOCUMENT)
        if not text.strip() and not media:
            return None
        thread = _Thread(
            subject=subject,
            message_id=mail.message_id,
            references=tuple(clean_ids(" ".join(mail.references), mail.message_id)),
        )
        self._remember(mail.sender, thread, self._threads)
        if mail.message_id:
            self._remember(mail.message_id, thread, self._by_id)
        name = mail.name or mail.sender
        return InboundEvent(
            source=Source(
                platform=self.platform,
                chat_id=mail.sender,
                chat_type="dm",
                chat_name=name,
                user_id=mail.sender,
                user_name=name,
            ),
            text=text,
            kind=kind,
            message_id=mail.message_id or f"uid:{mail.uid}",
            media=tuple(media),
            reply_to_id=mail.in_reply_to or None,
            metadata={"subject": subject},
            timestamp=mail.sent_at,
        )

    def register_fetchers(self, seat: FetcherSeat) -> Callable[[], None] | None:
        """Register `fetch_attachment` with the media intake (P15-23); returns the undo."""
        return seat.register_fetcher(self.platform, self.fetch_attachment)

    async def fetch_attachment(self, attachment: Attachment) -> AsyncIterator[bytes]:
        """The intake's fetcher (registered by `register_fetchers`): a held attachment."""
        if not attachment.ref.startswith(ATTACHMENT_REF):
            raise MediaRejected("it is not an email attachment")
        token = unquote(attachment.ref[len(ATTACHMENT_REF) :].split("#", 1)[0])
        part = self._parts.get(token)
        if part is None:
            raise MediaRejected("it is no longer available")
        yield part.data

    # -- outbound --------------------------------------------------------------------------------

    def _compose(self, to: str, body: str, reply_to: object) -> tuple[EmailMessage, str]:
        parent = reply_to if isinstance(reply_to, str) and clean_ids(reply_to) == [reply_to] else ""
        context = (self._by_id.get(parent) if parent else None) or self._threads.get(to)
        subject = context.subject if context is not None and context.subject else DEFAULT_SUBJECT
        if not _REPLY_SUBJECT.match(subject):
            subject = f"Re: {subject}"
        if not parent and context is not None:
            parent = context.message_id
        references = list(context.references) if context is not None else []
        if parent and parent not in references:
            references.append(parent)
        message_id = f"<indus-{secrets.token_hex(8)}@{self.mail.domain}>"
        message = EmailMessage(policy=SMTP_POLICY)
        message["From"] = self.mail.address
        message["To"] = to
        message["Subject"] = one_line(subject)
        message["Date"] = format_datetime(self.clock.now())
        message["Message-ID"] = message_id
        # RFC 3834: other robots must not answer us (no mail loops between agents).
        message["Auto-Submitted"] = "auto-replied"
        if parent:
            message["In-Reply-To"] = parent
            message["References"] = " ".join(references[-20:])
        message.set_content(body or "", charset="utf-8")
        return message, message_id

    def _deliver_now(self, message: EmailMessage) -> None:
        stage = "connect"
        try:
            smtp = self._smtp_factory(self.mail)
        except Exception as exc:
            raise _Staged(stage, exc) from None
        try:
            smtp.login(self.mail.address, self.mail.password)
            stage = "send"
            smtp.send_message(message)
        except Exception as exc:
            raise _Staged(stage, exc) from None
        finally:
            try:
                smtp.quit()
            except Exception:
                with contextlib.suppress(Exception):
                    smtp.close()

    def _smtp_probe(self) -> None:
        smtp = self._smtp_factory(self.mail)
        try:
            smtp.login(self.mail.address, self.mail.password)
        finally:
            try:
                smtp.quit()
            except Exception:
                with contextlib.suppress(Exception):
                    smtp.close()

    async def _deliver(self, message: EmailMessage, message_id: str) -> SendResult:
        try:
            await self._run(self._deliver_now, message)
        except _Staged as failed:
            return self._smtp_failure(failed.error, failed.stage)
        except Exception as exc:
            return self._smtp_failure(exc, "connect")
        log.info("email: sent %s to %s", message_id, message["To"])
        return SendResult.sent(message_id)

    def _reply_failure(self, code: int, raw: object, *, recipient: bool) -> SendResult:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        text = one_line(text, 300)
        lowered = text.lower()
        enhanced = _ENHANCED.match(text)
        if 400 <= code < 500:
            wait = _WAIT.search(text)
            if wait is not None or any(
                n in lowered for n in ("rate", "too many", "throttl", "try again later")
            ):
                return SendResult.failure(
                    f"rate limit: SMTP {code} {text}",
                    retry_after=float(wait.group(1)) if wait is not None else None,
                )
            return SendResult(
                ok=False,
                error=f"SMTP {code} {text} (temporary)",
                retryable=True,
                kind=ErrorKind.TRANSIENT,
            )
        if code == 552 or (enhanced is not None and enhanced.group(2, 3) == ("3", "4")):
            return SendResult.failure(f"message is too big: SMTP {code} {text}")
        if enhanced is not None and enhanced.group(2) == "7":
            return SendResult.failure(f"forbidden: SMTP {code} {text}")
        if recipient or (enhanced is not None and enhanced.group(2) == "1"):
            return SendResult.failure(f"chat not found: SMTP {code} {text}")
        return SendResult.failure(f"SMTP {code} {text}")

    def _smtp_failure(self, exc: BaseException, stage: str) -> SendResult:
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            return SendResult(
                ok=False,
                error="the SMTP server refused the login; check EMAIL_ADDRESS and EMAIL_PASSWORD",
                kind=ErrorKind.UNKNOWN,
                extra={"auth": True},
            )
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            refused = list(exc.recipients.values())
            code, raw = refused[0] if refused else (550, b"recipient refused")
            return self._reply_failure(int(code), raw, recipient=True)
        if isinstance(exc, smtplib.SMTPConnectError | ConnectionRefusedError | socket.gaierror):
            return SendResult(
                ok=False,
                error=f"connection refused: {type(exc).__name__}: {exc}",
                retryable=True,
                kind=ErrorKind.TRANSIENT,
            )
        if isinstance(exc, smtplib.SMTPResponseException):
            return self._reply_failure(int(exc.smtp_code), exc.smtp_error, recipient=False)
        if stage == "connect":
            return SendResult(
                ok=False,
                error=f"connection error: {type(exc).__name__}: {exc}",
                retryable=True,
                kind=ErrorKind.TRANSIENT,
            )
        # The message may already be with the server: never send it again (R06.4).
        result = SendResult.failure(exc=exc)
        if not result.maybe_delivered:
            result = SendResult(
                ok=False,
                error=f"the connection broke while sending: {type(exc).__name__}: {exc}",
                kind=ErrorKind.UNKNOWN,
                maybe_delivered=True,
            )
        return result

    def is_final_failure(self, result: SendResult) -> bool:
        return result.extra.get("auth") is True

    @staticmethod
    def _recipient(chat_id: str) -> str:
        """``chat_id`` as one plain lower-cased address, or "" (no display names, no lists)."""
        text = str(chat_id or "").strip()
        if not text or any(ord(c) < 32 or c == "\x7f" for c in text):
            return ""
        address = sender_address(text)
        return address if address == text.lower() else ""

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        if len(text) > MAX_BODY:
            return SendResult.failure(
                f"message is too long for one email ({len(text)} characters; limit {MAX_BODY})"
            )
        to = self._recipient(chat_id)
        if not to:
            return _NOT_AN_ADDRESS
        message, message_id = self._compose(to, text, reply_to)
        return await self._deliver(message, message_id)

    async def _send_media(
        self, kind: MediaKind, chat_id: str, path: str, caption: str | None, meta: dict[str, Any]
    ) -> SendResult:
        to = self._recipient(chat_id)
        if not to:
            return _NOT_AN_ADDRESS
        try:
            data = await self._run(_read_file, path, OUTBOUND_FILE_LIMIT)
        except (OSError, ValueError) as exc:
            return SendResult(ok=False, error=f"the file could not be attached: {exc}")
        message, message_id = self._compose(to, caption or "", meta.get(REPLY_TO_KEY))
        guessed, _ = mimetypes.guess_type(path)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        message.add_attachment(
            data, maintype=maintype, subtype=subtype, filename=os.path.basename(path)
        )
        return await self._deliver(message, message_id)

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(name=chat_id, type="dm")
