"""The IRC adapter (P16-27): one TLS connection, IRCv3 where the server has it.

Connecting: TLS by default (verified against the system's trust store). We ask for ``CAP LS 302``
and request what we use of ``message-tags``, ``server-time``, ``echo-message`` and — only when an
account is configured — ``sasl``; SASL PLAIN runs before ``CAP END``. ``PASS``, SASL and NickServ
``IDENTIFY`` passwords go out only over TLS unless ``allow_plaintext_auth`` is set; otherwise the
connect fails as a fatal configuration problem before anything is sent. A taken nick gets ``_``
appended, at most three times. After ``001`` the configured channels are joined; other channels are
joined when we first send to them.

Inbound: PRIVMSGs to our nick are DMs (the chat is the sender's nick); channel PRIVMSGs are groups,
``mentioned`` (with the ``nick:`` / ``nick,`` address cut) when they start by addressing us — the
gateway's mention policy decides about the rest. A ``bot`` tag marks bot authors, CTCP ACTION
reads ``* nick text``, other CTCP and NOTICEs are ignored, and our own (echoed) lines never come
in. Every line gets its own message id (the server's ``msgid`` or ours).

Outbound: plain text, CR/NUL removed, one line per line break, each payload ≤450 UTF-8 bytes after
``PRIVMSG <target> :`` and cut on character boundaries; one line per 0.5 s per target (the chat
slot's spacing). Each line is confirmed before the next — by its echo (``echo-message``, which also
gives its ``msgid``) or else by a ``PING`` sent after it — and an error numeric or ``FAIL`` reply in
between is that line's refusal. No confirmation within 15 s means "maybe delivered" (never resent,
R06.4). A lost connection is reopened inside the adapter (2 s doubling to 60 s); a ban is reported
as a fatal platform state.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import re
import ssl
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final, Protocol

from indus.foundation.clock import Clock
from indus.gateway.adapters.base import Adapter, AdapterConnectError, Capabilities, ChatInfo
from indus.gateway.adapters.irc.protocol import (
    IrcLine,
    clean_text,
    format_line,
    is_channel,
    payload_budget,
    sasl_plain_chunks,
    split_payload,
    utf8_bytes,
    valid_target,
)
from indus.gateway.core.events import InboundEvent, Source
from indus.gateway.core.send import SendResult

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "CONFIRM_S",
    "NICK_RETRIES",
    "WANTED_CAPS",
    "Connector",
    "IrcAdapter",
    "IrcOptions",
    "LineReader",
    "LineWriter",
    "open_tcp",
]

log = logging.getLogger(__name__)

WANTED_CAPS: Final = ("message-tags", "server-time", "echo-message")
NICK_RETRIES: Final = 3
CONNECT_S: Final = 30.0
REGISTER_S: Final = 30.0
CONFIRM_S: Final = 15.0
JOIN_S: Final = 10.0
SILENCE_S: Final = 240.0
BACKOFF_START_S: Final = 2.0
BACKOFF_MAX_S: Final = 60.0
NICKSERV_WAIT_S: Final = 2.0
LINE_SPACING_S: Final = 0.5
_BAN = re.compile(r"\b(?:k-?lined|g-?lined|z-?lined|banned)\b", re.IGNORECASE)
_JOIN_ERRORS: Final = frozenset({"403", "405", "471", "473", "474", "475"})
_SEND_ERRORS: Final = frozenset({"401", "403", "404", "407", "412", "417", "481"})


class LineReader(Protocol):
    async def readline(self) -> bytes: ...


class LineWriter(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...

    def is_closing(self) -> bool: ...


Connector = Callable[[str, int, ssl.SSLContext | None], Awaitable[tuple[LineReader, LineWriter]]]


async def open_tcp(
    host: str, port: int, tls: ssl.SSLContext | None
) -> tuple[LineReader, LineWriter]:
    """A real connection (asyncio streams), TLS with hostname checks when ``tls`` is given."""
    reader, writer = await asyncio.open_connection(
        host, port, ssl=tls, server_hostname=host if tls is not None else None, limit=8192
    )
    return reader, writer


async def within[T](clock: Clock, work: Awaitable[T], seconds: float) -> T:
    """``work``'s result, or ``TimeoutError`` once ``clock`` moved ``seconds`` on first."""
    task = asyncio.ensure_future(work)
    timer = asyncio.ensure_future(clock.sleep(seconds))
    try:
        await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    if task.cancelled():
        raise TimeoutError
    return task.result()


@dataclass(frozen=True)
class IrcOptions:
    server: str
    nickname: str
    port: int = 6697
    channels: tuple[str, ...] = ()
    use_tls: bool = True
    realname: str = "Indus Agent"
    allow_plaintext_auth: bool = False
    sasl_username: str | None = None
    server_password: str | None = field(default=None, repr=False)
    sasl_password: str | None = field(default=None, repr=False)
    nickserv_password: str | None = field(default=None, repr=False)

    @property
    def passwords(self) -> tuple[str, ...]:
        found = (self.server_password, self.sasl_password, self.nickserv_password)
        return tuple(p for p in found if p)


class _Lost(Exception):
    """The connection ended (EOF, an ERROR line or silence)."""


@dataclass
class _Pending:
    target: str
    future: asyncio.Future[str | SendResult]
    token: str | None = None


@dataclass
class _Link:
    reader: LineReader
    writer: LineWriter
    caps: frozenset[str] = frozenset()


def _failure_of(line: IrcLine) -> SendResult:
    """A refusal reply (error numeric or ``FAIL``) read as a classified send failure."""
    text = line.param(-1)
    if line.command == "FAIL":
        code = line.param(1).upper()
        if code == "INVALID_UTF8":
            return SendResult.failure(f"can't parse the message: {text}")
        if "RATE" in code or "THROTTL" in code or "FLOOD" in code:
            waits = [p for p in line.params[2:-1] if p.isdigit()]
            wait = float(waits[-1]) if waits else None
            return SendResult.failure(f"rate limited by the IRC server: {text}", retry_after=wait)
        if code.endswith("UNAVAILABLE"):
            return SendResult.failure(f"connect error: the IRC server cannot take it now: {text}")
        return SendResult.failure(f"FAIL {code}: {text}")
    if line.command in ("401", "403"):
        return SendResult.failure(f"chat not found (IRC {line.command}: {text})")
    if line.command == "404":
        return SendResult.failure(f"forbidden: cannot send to this channel (IRC 404: {text})")
    if line.command == "417":
        return SendResult.failure(f"message is too long (IRC 417: {text})")
    return SendResult.failure(f"IRC {line.command}: {text}")


class IrcAdapter(Adapter):
    """One IRC network identity (see the module notes)."""

    platform: ClassVar[str] = "irc"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=450,
        len_fn=utf8_bytes,
        markup="plain",
        splits_natively=True,
        slot_spacing=LINE_SPACING_S,
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "asyncio:StreamReader": ("readline",),
        "asyncio:StreamWriter": ("write", "drain", "close", "wait_closed", "is_closing"),
    }

    def __init__(
        self,
        options: IrcOptions,
        *,
        clock: Clock,
        connector: Connector = open_tcp,
        settings: PlatformSettings | None = None,
        secrets: Sequence[str] = (),
        send_only: bool = False,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=(*secrets, *options.passwords))
        self.options = options
        self.connector = connector
        self.send_only = send_only
        base = options.nickname
        self.wanted_nick = f"{base[:25]}-send" if send_only else base
        self.nick = self.wanted_nick
        self._link: _Link | None = None
        self._runner: asyncio.Task[None] | None = None
        self._pending: _Pending | None = None
        self._wire = asyncio.Lock()
        self._joined: set[str] = set()
        self._joining: dict[str, asyncio.Future[str | None]] = {}
        self._ids = itertools.count(1)
        self._stopping = False
        # Times the connection was opened again after a loss, for status and tests.
        self.reopened = 0

    # -- lifecycle ------------------------------------------------------------------------------

    def _tls(self) -> ssl.SSLContext | None:
        return ssl.create_default_context() if self.options.use_tls else None

    async def _open(self, is_reconnect: bool) -> None:
        opts = self.options
        if opts.passwords and not opts.use_tls and not opts.allow_plaintext_auth:
            raise AdapterConnectError(
                "refusing to send an IRC password over a plain connection: turn use_tls on, or "
                "set allow_plaintext_auth: true if this network really has no TLS",
                retryable=False,
                code="irc_plaintext_auth",
            )
        link = await self._session()
        self._runner = asyncio.ensure_future(self._run(link))

    async def _close(self) -> None:
        self._stopping = True
        runner, self._runner = self._runner, None
        link, self._link = self._link, None
        if link is not None:
            with contextlib.suppress(Exception):
                await within(
                    self.clock, self._write_to(link, "QUIT", "Indus Agent signing off"), 2.0
                )
        if runner is not None:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runner
        if link is not None:
            await self._shut(link)
        self._fail_pending("connect error: the adapter disconnected")
        self._joined.clear()
        self._stopping = False

    async def _shut(self, link: _Link) -> None:
        with contextlib.suppress(Exception):
            link.writer.close()
            await within(self.clock, link.writer.wait_closed(), 2.0)

    # -- the wire -------------------------------------------------------------------------------

    async def _write_to(self, link: _Link, command: str, *params: str) -> None:
        link.writer.write((format_line(command, *params) + "\r\n").encode("utf-8"))
        await link.writer.drain()

    async def _write(self, command: str, *params: str) -> None:
        link = self._link
        if link is None or link.writer.is_closing():
            raise ConnectionError("not connected to the IRC server")
        await self._write_to(link, command, *params)

    @staticmethod
    async def _read(link: _Link) -> IrcLine | None:
        """The next parsable line; None at the end of the connection."""
        while True:
            raw = await link.reader.readline()
            if not raw:
                return None
            line = IrcLine.parse(raw.decode("utf-8", "replace"))
            if line is not None:
                return line

    # -- registration ---------------------------------------------------------------------------

    async def _session(self) -> _Link:
        """Connect, negotiate and register; the ready link (``AdapterConnectError`` otherwise)."""
        opts = self.options
        try:
            reader, writer = await within(
                self.clock, self.connector(opts.server, opts.port, self._tls()), CONNECT_S
            )
        except (OSError, TimeoutError, ssl.SSLError) as exc:
            raise AdapterConnectError(
                f"cannot connect to {opts.server}:{opts.port}: {type(exc).__name__}"
            ) from None
        link = _Link(reader, writer)
        try:
            await within(self.clock, self._register(link), REGISTER_S)
        except TimeoutError:
            await self._shut(link)
            raise AdapterConnectError("the IRC server did not finish registration") from None
        except AdapterConnectError:
            await self._shut(link)
            raise
        except (OSError, ConnectionError, ValueError) as exc:
            await self._shut(link)
            raise AdapterConnectError(f"IRC registration failed: {type(exc).__name__}") from None
        self._link = link
        self._joined.clear()
        if opts.nickserv_password and not self.send_only:
            await self._write("PRIVMSG", "NickServ", f"IDENTIFY {opts.nickserv_password}")
            await self.clock.sleep(NICKSERV_WAIT_S)
        if opts.channels and not self.send_only:
            await self._write("JOIN", ",".join(opts.channels))
        return link

    async def _register(self, link: _Link) -> None:
        opts = self.options
        wants_sasl = bool(opts.sasl_password)
        await self._write_to(link, "CAP", "LS", "302")
        if opts.server_password:
            await self._write_to(link, "PASS", opts.server_password)
        self.nick = self.wanted_nick
        await self._write_to(link, "NICK", self.nick)
        await self._write_to(link, "USER", self.nick[:16], "0", "*", opts.realname)
        offered: set[str] = set()
        tries = 0
        while True:
            line = await self._read(link)
            if line is None:
                raise AdapterConnectError("the IRC server closed the connection while registering")
            cmd = line.command
            if cmd == "PING":
                await self._write_to(link, "PONG", line.param(-1))
            elif cmd == "CAP":
                sub = line.param(1).upper()
                if sub == "LS":
                    offered.update(c.split("=", 1)[0] for c in line.param(-1).split())
                    if line.param(2) == "*":
                        continue  # more capabilities follow
                    wanted = [c for c in WANTED_CAPS if c in offered]
                    if wants_sasl:
                        if "sasl" not in offered:
                            raise AdapterConnectError(
                                "the IRC server offers no SASL; remove the SASL account or "
                                "use another network",
                                retryable=False,
                                code="irc_sasl",
                            )
                        wanted.append("sasl")
                    if wanted:
                        await self._write_to(link, "CAP", "REQ", " ".join(wanted))
                    else:
                        await self._write_to(link, "CAP", "END")
                elif sub == "ACK":
                    link.caps = link.caps | frozenset(line.param(-1).split())
                    if wants_sasl and "sasl" in link.caps:
                        await self._write_to(link, "AUTHENTICATE", "PLAIN")
                    else:
                        await self._write_to(link, "CAP", "END")
                elif sub == "NAK":
                    if wants_sasl:
                        raise AdapterConnectError(
                            "the IRC server refused SASL", retryable=False, code="irc_sasl"
                        )
                    await self._write_to(link, "CAP", "END")
            elif cmd == "AUTHENTICATE" and line.param(0) == "+":
                user = opts.sasl_username or opts.nickname
                for chunk in sasl_plain_chunks(user, opts.sasl_password or ""):
                    await self._write_to(link, "AUTHENTICATE", chunk)
            elif cmd == "903":
                await self._write_to(link, "CAP", "END")
            elif cmd in ("902", "904", "905", "906", "908"):
                raise AdapterConnectError(
                    "the IRC server refused the SASL login", retryable=False, code="irc_sasl"
                )
            elif cmd in ("433", "436", "437"):
                tries += 1
                if tries > NICK_RETRIES:
                    raise AdapterConnectError(
                        f"the nick {self.wanted_nick} and its variants are taken"
                    )
                self.nick = self.wanted_nick + "_" * tries
                await self._write_to(link, "NICK", self.nick)
            elif cmd == "432":
                raise AdapterConnectError(
                    f"the IRC server refuses the nick {self.wanted_nick}",
                    retryable=False,
                    code="irc_nick",
                )
            elif cmd == "464":
                raise AdapterConnectError(
                    "the IRC server refused the server password", retryable=False, code="irc_auth"
                )
            elif cmd == "465" or (cmd == "ERROR" and _BAN.search(line.param(-1))):
                raise AdapterConnectError(
                    "the IRC server bans this client", retryable=False, code="irc_banned"
                )
            elif cmd == "ERROR":
                raise AdapterConnectError("the IRC server closed the link while registering")
            elif cmd == "001":
                self.nick = line.param(0) or self.nick
                return

    # -- the running connection -----------------------------------------------------------------

    async def _run(self, link: _Link) -> None:
        backoff = BACKOFF_START_S
        while True:
            try:
                await self._serve(link)
                reason = "closed"
            except _Lost as exc:
                reason = str(exc)
            except (OSError, ConnectionError) as exc:
                reason = type(exc).__name__
            if self._link is link:
                self._link = None
            await self._shut(link)
            self._fail_pending(
                "the connection to the IRC server closed before it confirmed the message "
                "(response ended prematurely)"
            )
            self._joined.clear()
            if self._stopping:
                return
            if reason.startswith("banned"):
                self.report_fatal("irc_banned", "the IRC server bans this client")
                return
            log.info("irc: the connection %s; reconnecting", reason)
            while True:
                await self.clock.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                try:
                    link = await self._session()
                except AdapterConnectError as exc:
                    if not exc.retryable:
                        self.report_fatal(exc.code, str(exc))
                        return
                    log.info("irc: reconnecting failed: %s", self.scrub(str(exc)))
                    continue
                backoff = BACKOFF_START_S
                self.reopened += 1
                break

    async def _serve(self, link: _Link) -> None:
        quiet = False
        while True:
            try:
                line = await within(self.clock, self._read(link), SILENCE_S)
            except TimeoutError:
                if quiet:
                    raise _Lost(f"was silent for {2 * SILENCE_S:g} s") from None
                quiet = True
                await self._write_to(link, "PING", "indus-alive")
                continue
            quiet = False
            if line is None:
                return
            await self._dispatch(line)

    async def _dispatch(self, line: IrcLine) -> None:
        cmd = line.command
        if cmd == "PING":
            await self._write("PONG", line.param(-1))
        elif cmd == "PONG":
            pending = self._pending
            if pending is not None and pending.token and line.param(-1) == pending.token:
                self._settle(f"irc-{next(self._ids)}")
        elif cmd == "PRIVMSG" and len(line.params) >= 2:
            if line.nick.lower() == self.nick.lower():
                self._echoed(line)
            else:
                await self._inbound(line)
        elif cmd == "NICK" and line.nick.lower() == self.nick.lower():
            self.nick = line.param(0) or self.nick
        elif cmd == "JOIN" and line.nick.lower() == self.nick.lower():
            channel = line.param(0).lower()
            self._joined.add(channel)
            self._join_done(channel, None)
        elif cmd in _JOIN_ERRORS and line.param(1).lower() in self._joining:
            self._join_done(line.param(1).lower(), f"IRC {cmd}: {line.param(-1)}")
        elif cmd == "FAIL" or cmd in _SEND_ERRORS:
            pending = self._pending
            if pending is not None and (
                cmd in ("FAIL", "417") or line.param(1).lower() == pending.target.lower()
            ):
                self._settle(_failure_of(line))
        elif cmd == "465" or (cmd == "ERROR" and _BAN.search(line.param(-1))):
            raise _Lost("banned")
        elif cmd == "ERROR":
            raise _Lost(f"was closed by the server ({line.param(-1)[:80]})")

    def _echoed(self, line: IrcLine) -> None:
        pending = self._pending
        if (
            pending is not None
            and pending.token is None
            and line.param(0).lower() == pending.target.lower()
        ):
            self._settle(line.tags.get("msgid") or f"irc-{next(self._ids)}")

    def _settle(self, outcome: str | SendResult) -> None:
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_result(outcome)

    def _fail_pending(self, reason: str) -> None:
        self._settle(SendResult.failure(reason))
        for future in self._joining.values():
            if not future.done():
                future.set_result(reason)

    def _join_done(self, channel: str, error: str | None) -> None:
        future = self._joining.pop(channel, None)
        if future is not None and not future.done():
            future.set_result(error)

    async def _inbound(self, line: IrcLine) -> None:
        sender, target, text = line.nick, line.param(0), line.param(-1)
        if text.startswith("\x01"):
            body = text.strip("\x01")
            if not body.upper().startswith("ACTION "):
                return  # other CTCP requests are not chat
            text = f"* {sender} {body[7:]}"
        metadata: dict[str, Any] = {}
        if is_channel(target):
            chat_id, chat_type = target, "group"
            for mark in (":", ",", " "):
                address = f"{self.nick}{mark}"
                if text[: len(address)].lower() == address.lower():
                    text = text[len(address) :].strip()
                    metadata["mentioned"] = True
                    break
        else:
            chat_id, chat_type = sender, "dm"
        stamp: datetime | None = None
        raw_time = line.tags.get("time")
        if raw_time:
            with contextlib.suppress(ValueError):
                stamp = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        source = Source(
            platform=self.platform,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=chat_id,
            user_id=sender,
            # IRC nicks are case-insensitive: allowlists match the lower-case form too.
            user_id_alt=sender.lower() if sender.lower() != sender else None,
            user_name=sender,
            is_bot="bot" in line.tags or "draft/bot" in line.tags,
        )
        message_id = line.tags.get("msgid") or f"irc-in-{next(self._ids)}"
        await self.emit(
            InboundEvent(
                source=source,
                text=text,
                message_id=message_id,
                metadata=metadata,
                timestamp=stamp or self.clock.now(),
            )
        )

    # -- outbound -------------------------------------------------------------------------------

    async def _ensure_joined(self, channel: str) -> str | None:
        """Join ``channel`` before the first send to it; an error text when the server refused."""
        key = channel.lower()
        if key in self._joined:
            return None
        future = self._joining.get(key)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._joining[key] = future
            await self._write("JOIN", channel)
        try:
            return await within(self.clock, asyncio.shield(future), JOIN_S)
        except TimeoutError:
            return None  # no answer: try the send anyway

    async def _line(self, target: str, payload: str) -> str | SendResult:
        """Send one PRIVMSG and wait for its confirmation: its id, or a failure."""
        async with self._wire:
            link = self._link
            if link is None or link.writer.is_closing():
                return SendResult.failure("connect error: not connected to the IRC server")
            pending = _Pending(target, asyncio.get_running_loop().create_future())
            if "echo-message" not in link.caps:
                pending.token = f"indus-{next(self._ids)}"
            self._pending = pending
            try:
                try:
                    await self._write_to(link, "PRIVMSG", target, payload)
                    if pending.token is not None:
                        await self._write_to(link, "PING", pending.token)
                except (OSError, ConnectionError) as exc:
                    return SendResult.failure(f"connect error: {type(exc).__name__}")
                try:
                    return await within(self.clock, asyncio.shield(pending.future), CONFIRM_S)
                except TimeoutError:
                    return SendResult.failure(
                        "read timeout: the IRC server did not confirm the message"
                    )
            finally:
                self._pending = None

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        if not valid_target(chat_id):
            return SendResult.failure(f"chat not found: {chat_id[:60]!r} is not an IRC target")
        body = clean_text(text)
        pieces = split_payload(body, payload_budget(chat_id))
        if not pieces:
            return SendResult(ok=False, error="nothing to send")
        if is_channel(chat_id):
            refused = await self._ensure_joined(chat_id)
            if refused is not None:
                return SendResult.failure(f"forbidden: cannot join {chat_id} ({refused})")
        ids: list[str] = []
        for n, (payload, offset) in enumerate(pieces):
            if n:
                await self.slots.pace(chat_id)
            outcome = await self._line(chat_id, payload)
            if isinstance(outcome, SendResult):
                if not ids:
                    return outcome
                rest = None if outcome.maybe_delivered else body[offset:]
                return SendResult.failure(
                    outcome.error,
                    retry_after=outcome.retry_after,
                    delivered_ids=tuple(ids),
                    remainder=rest,
                )
            ids.append(outcome)
        return SendResult.sent(ids[-1], tuple(ids[:-1]))

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(name=chat_id, type="group" if is_channel(chat_id) else "dm")
