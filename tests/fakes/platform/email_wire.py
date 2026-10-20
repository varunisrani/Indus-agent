"""In-process IMAP and SMTP fakes for the email adapter (P16-09).

``FakeImap`` and ``FakeSmtp`` have the method shapes of ``imaplib.IMAP4`` and ``smtplib.SMTP``
the adapter calls (``SURFACE``; the drift check proves each exists in the standard library), and
no socket: the adapter gets them from its connection factories. Both sit on a `FakeChatPlatform`:

- ``Mailbox`` turns inbound traffic injected with the platform's builders into RFC 5322 messages
  with UIDs and flags (From the sender, a ``Message-ID``, an ``Authentication-Results`` header
  from ``AUTHSERV_ID`` saying DMARC passed, media as attachments). Email has no groups, threads or
  edits, so those builders produce nothing; a bot message becomes an ``Auto-Submitted`` mail.
  Tests can also ``deliver`` raw bytes. Every FETCH is logged in ``fetches``;
- a send lands as one platform message whose id is the mail's ``Message-ID``; armed failures come
  back as the ``smtplib`` exceptions a real server produces.
"""

from __future__ import annotations

import imaplib
import re
import smtplib
from collections.abc import Coroutine
from dataclasses import dataclass, field
from email.message import EmailMessage, Message
from email.parser import BytesHeaderParser
from email.utils import format_datetime
from typing import Any

from tests.fakes.platform.core import (
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMessage,
    FakePlatformError,
)

MAX_LEN = 50_000
BOT_ADDRESS = "agent@indus.test"
AUTHSERV_ID = "mx.indus.test"
SURFACE: dict[str, tuple[str, ...]] = {
    "imaplib:IMAP4": ("login", "select", "response", "uid", "xatom", "logout", "shutdown"),
    "smtplib:SMTP": ("login", "send_message", "quit", "close", "starttls"),
}

SAMPLE_BYTES: dict[str, bytes] = {
    "voice": b"OggS\x00\x02" + b"\x00" * 26,
    "audio": b"ID3\x04\x00" + b"\x00" * 27,
    "photo": b"\xff\xd8\xff\xe0" + b"\x00" * 28,
    "video": b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20,
    "document": b"plain text notes\n",
    "sticker": b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16,
}


def now[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a fake-platform coroutine that never suspends, from synchronous fake code."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise RuntimeError("a fake platform call suspended")


@dataclass
class Stored:
    uid: int
    raw: bytes
    flags: set[str] = field(default_factory=set[str])


def _domain(address: str) -> str:
    return address.rpartition("@")[2]


@dataclass
class Mailbox:
    """The server side: one INBOX, the account password and every command seen."""

    platform: FakeChatPlatform
    address: str = BOT_ADDRESS
    password: str = ""
    uidvalidity: int = 7
    capabilities: tuple[str, ...] = ("IMAP4REV1", "ID")
    messages: dict[int, Stored] = field(default_factory=dict[int, Stored])
    fetches: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])
    commands: list[tuple[str, tuple[Any, ...]]] = field(
        default_factory=list[tuple[str, tuple[Any, ...]]]
    )
    sessions: int = 0
    next_uid: int = 1

    def deliver(self, raw: bytes, *, seen: bool = False) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.messages[uid] = Stored(uid, raw, {r"\Seen"} if seen else set())
        return uid

    def sync(self) -> None:
        """Turn waiting platform traffic into mail."""
        for item in self.platform.drain_inbound():
            message = self.render(item)
            if message is not None:
                self.deliver(message.as_bytes())

    def render(self, item: FakeInbound) -> EmailMessage | None:
        if item.chat_kind != "dm" or item.thread_id or item.event != "message":
            return None  # no groups, threads, edits or buttons in email
        message = EmailMessage()
        name = item.user_name or item.user_id.partition("@")[0]
        message["From"] = f"{name} <{item.user_id}>"
        message["To"] = self.address
        message["Message-ID"] = f"<m{item.message_id}.{item.update_id}@{_domain(item.user_id)}>"
        message["Date"] = format_datetime(item.at)
        message["Authentication-Results"] = (
            f"{AUTHSERV_ID}; dmarc=pass header.from={_domain(item.user_id)}"
        )
        if item.is_bot:
            message["Auto-Submitted"] = "auto-replied"
        if item.reply_to:
            original = self.platform.message(item.reply_to)
            if original is not None:
                message["In-Reply-To"] = original.message_id
        message.set_content(item.text or "")
        for media in item.media:
            data = SAMPLE_BYTES.get(media.kind, b"data")
            maintype, _, subtype = (media.mime or "application/octet-stream").partition("/")
            message.add_attachment(data, maintype=maintype, subtype=subtype, filename=media.ref)
        return message


_FIELDS = re.compile(r"HEADER\.FIELDS \(([^)]*)\)\]<0\.(\d+)>")


def _connect_failure(exc: FakePlatformError) -> Exception:
    if exc.kind is FailureKind.READ_TIMEOUT:
        return TimeoutError("timed out")
    return ConnectionRefusedError(111, "Connection refused")


class FakeImap:
    """``imaplib.IMAP4`` as the adapter uses it, over a `Mailbox`."""

    def __init__(self, mailbox: Mailbox) -> None:
        self.mailbox = mailbox
        self.capabilities = mailbox.capabilities
        self.logged_in = False
        self.closed = False

    def _log(self, name: str, *args: Any) -> None:
        self.mailbox.commands.append((name, args))

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self._log("login", user)
        try:
            now(self.mailbox.platform.connect())
        except FakePlatformError as exc:
            raise _connect_failure(exc) from None
        if user != self.mailbox.address or password != self.mailbox.password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        self.logged_in = True
        self.mailbox.sessions += 1
        return "OK", [b"LOGIN completed"]

    def xatom(self, name: str, *args: Any) -> tuple[str, list[bytes]]:
        self._log("xatom", name, *args)
        return "OK", [b"ID completed"]

    def select(self, mailbox: str = "INBOX") -> tuple[str, list[bytes]]:
        self._log("select", mailbox)
        if not self.logged_in:
            raise imaplib.IMAP4.error("SELECT illegal in state NONAUTH")
        return "OK", [str(len(self.mailbox.messages)).encode()]

    def response(self, code: str) -> tuple[str, list[bytes | None]]:
        if code == "UIDVALIDITY":
            return code, [str(self.mailbox.uidvalidity).encode()]
        return code, [None]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self._log("uid", command.upper(), *args)
        if self.closed or not self.logged_in:
            raise imaplib.IMAP4.abort("socket error: EOF")
        verb = command.upper()
        box = self.mailbox
        if verb == "SEARCH":
            box.sync()
            criterion = str(args[-1]).upper()
            uids = [
                str(m.uid)
                for m in box.messages.values()
                if criterion == "ALL" or r"\Seen" not in m.flags
            ]
            return "OK", [" ".join(uids).encode()]
        if verb == "STORE":
            stored = box.messages.get(int(args[0]))
            if stored is not None and "Seen" in str(args[2]):
                stored.flags.add(r"\Seen")
            return "OK", [b""]
        if verb == "FETCH":
            stored = box.messages.get(int(args[0]))
            if stored is None:
                return "OK", [None]
            return "OK", self._fetch(stored, str(args[1]))
        return "BAD", [b"unknown command"]

    def _fetch(self, stored: Stored, query: str) -> list[Any]:
        box = self.mailbox
        fields = _FIELDS.search(query)
        if fields is not None:
            wanted = {f.lower() for f in fields.group(1).split()}
            limit = int(fields.group(2))
            box.fetches.append((stored.uid, "headers"))
            parsed = BytesHeaderParser().parsebytes(stored.raw)
            lines = [
                f"{name}: {value}\r\n" for name, value in parsed.items() if name.lower() in wanted
            ]
            data = ("".join(lines) + "\r\n").encode("utf-8", "replace")[:limit]
            head = (
                f"{stored.uid} (UID {stored.uid} RFC822.SIZE {len(stored.raw)} "
                f"BODY[HEADER.FIELDS ({fields.group(1)})]<0> {{{len(data)}}}"
            )
            return [(head.encode(), data), b")"]
        box.fetches.append((stored.uid, "body"))
        if "PEEK" not in query:
            stored.flags.add(r"\Seen")
        head = f"{stored.uid} (UID {stored.uid} BODY[] {{{len(stored.raw)}}}"
        return [(head.encode(), stored.raw), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        self._log("logout")
        self.closed = True
        if self.logged_in:
            self.logged_in = False
            now(self.mailbox.platform.disconnect())
        return "BYE", [b"logging out"]

    def shutdown(self) -> None:
        self._log("shutdown")
        self.closed = True


@dataclass
class SmtpServer:
    """The outgoing side: the account password and every accepted message."""

    platform: FakeChatPlatform
    address: str = BOT_ADDRESS
    password: str = ""
    sent: list[Message] = field(default_factory=list[Message])
    logins: int = 0


def _smtp_failure(exc: FakePlatformError, to: str) -> Exception:
    """An armed platform failure the way an SMTP server reports it."""
    kind = exc.kind
    if kind is FailureKind.CONNECT_TIMEOUT:
        return ConnectionRefusedError(111, "Connection refused")
    if kind is FailureKind.READ_TIMEOUT:
        return TimeoutError("timed out")
    if kind is FailureKind.RATE_LIMIT:
        wait = int(exc.retry_after if exc.retry_after is not None else 5)
        return smtplib.SMTPDataError(
            421, f"4.7.28 Too many messages; retry after {wait} s".encode()
        )
    if kind is FailureKind.CHAT_NOT_FOUND:
        return smtplib.SMTPRecipientsRefused({to: (550, b"5.1.1 No such user here")})
    if kind is FailureKind.FORBIDDEN:
        return smtplib.SMTPRecipientsRefused({to: (550, f"5.7.1 {exc.text}".encode())})
    if kind is FailureKind.TOO_LONG:
        return smtplib.SMTPDataError(552, b"5.3.4 Message size exceeds fixed limit")
    return smtplib.SMTPDataError(554, f"5.6.0 {exc.text}".encode())


def _body_of(message: Message) -> tuple[str, list[str]]:
    text = ""
    names: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        if part.get_filename():
            names.append(str(part.get_filename()))
        elif part.get_content_type() == "text/plain" and not text:
            payload = part.get_payload(decode=True)
            text = payload.decode("utf-8") if isinstance(payload, bytes) else ""
    return text.removesuffix("\n"), names


def post_as(
    platform: FakeChatPlatform, message_id: str, chat_id: str, text: str, **kw: Any
) -> FakeMessage:
    """``platform.post`` with a chosen message id (the mail's Message-ID)."""
    saved = platform._ids  # pyright: ignore[reportPrivateUsage]
    platform._ids = iter([message_id])  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    try:
        return now(platform.post(chat_id, text, **kw))
    finally:
        platform._ids = saved  # pyright: ignore[reportPrivateUsage]


class FakeSmtp:
    """``smtplib.SMTP`` as the adapter uses it, over an `SmtpServer`."""

    def __init__(self, server: SmtpServer) -> None:
        self.server = server
        self.authenticated = False
        self.closed = False

    def starttls(self, *args: Any, **kw: Any) -> tuple[int, bytes]:
        return 220, b"2.0.0 Ready to start TLS"

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        if user != self.server.address or password != self.server.password:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication credentials invalid")
        self.authenticated = True
        self.server.logins += 1
        return 235, b"2.7.0 Accepted"

    def send_message(self, msg: Message, *args: Any, **kw: Any) -> dict[str, Any]:
        if not self.authenticated:
            raise smtplib.SMTPSenderRefused(530, b"5.7.0 Authentication required", "")
        to = str(msg["To"])
        text, names = _body_of(msg)
        try:
            post_as(
                self.server.platform,
                str(msg["Message-ID"]),
                to,
                text,
                reply_to=str(msg["In-Reply-To"]) if msg["In-Reply-To"] else None,
                extra={"attachments": names, "subject": str(msg["Subject"])},
            )
        except FakePlatformError as exc:
            raise _smtp_failure(exc, to) from None
        self.server.sent.append(msg)
        return {}

    def quit(self) -> tuple[int, bytes]:
        self.closed = True
        return 221, b"2.0.0 Bye"

    def close(self) -> None:
        self.closed = True


class EmailWire:
    """One fake mail service: the platform (cap 50,000), the mailbox and the SMTP server."""

    SURFACE = SURFACE
    FAKES: dict[str, type] = {"imaplib:IMAP4": FakeImap, "smtplib:SMTP": FakeSmtp}

    def __init__(self, platform: FakeChatPlatform | None = None, *, password: str = "") -> None:
        self.platform = platform or FakeChatPlatform(
            "email", max_len=MAX_LEN, bot_user_id=BOT_ADDRESS
        )
        self.mailbox = Mailbox(self.platform, password=password)
        self.smtp = SmtpServer(self.platform, password=password)
        self.imap_sessions: list[FakeImap] = []

    def open_imap(self, settings: Any) -> FakeImap:
        session = FakeImap(self.mailbox)
        self.imap_sessions.append(session)
        return session

    def open_smtp(self, settings: Any) -> FakeSmtp:
        return FakeSmtp(self.smtp)


__all__ = [
    "AUTHSERV_ID",
    "BOT_ADDRESS",
    "MAX_LEN",
    "SAMPLE_BYTES",
    "SURFACE",
    "EmailWire",
    "FakeImap",
    "FakeSmtp",
    "Mailbox",
    "SmtpServer",
    "now",
    "post_as",
]
