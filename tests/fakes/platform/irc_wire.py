"""An in-process fake IRC server over the shared fake platform (P16-27), no socket.

``IrcWire.connect(host, port, tls)`` is the adapter's connector: it records the call, connects the
platform (an armed connect failure raises ``TimeoutError`` as a stalled TCP connect would) and
returns a reader/writer pair with the shapes of asyncio's streams. Lines the adapter writes are
handled on ``drain()``; the server's lines wait in the reader.

The server speaks enough RFC 1459/2812 and IRCv3 for the adapter: ``CAP LS 302`` (``caps``),
``REQ``/``ACK``/``NAK``/``END``, SASL PLAIN (``accounts``), ``PASS`` (``server_password``), ``NICK``
(``taken`` nicks get 433), ``USER``, ``001`` once registered, ``PING``/``PONG``, ``JOIN`` (echo +
366; ``closed_channels`` get 473), ``QUIT`` and ``PRIVMSG``. A PRIVMSG is posted on the platform and
echoed back with its ``msgid`` when ``echo-message`` was acknowledged. An armed platform failure
answers as an IRC server would: chat not found → 401, forbidden → 404, too long → 417, a format
problem → ``FAIL PRIVMSG INVALID_UTF8``, a rate limit → ``FAIL PRIVMSG RATE_LIMITED <target>
<seconds>``, a thread error → ``FAIL PRIVMSG MESSAGE_NOT_FOUND``, a connect timeout → ``FAIL
PRIVMSG TEMPORARILY_UNAVAILABLE``; a read timeout gets no answer at all.

``mute()`` makes the server read and answer nothing. ``pump()`` delivers the platform's inbound
items as PRIVMSG lines (tags when message-tags is on: ``time``, ``msgid``, ``bot`` for bot
authors); IRC has no threads, edits, media or buttons, so those items deliver nothing. ``drop()``
closes the connection from the server side and ``ban()`` sends a K-line ``ERROR`` first. Every
line the server read is kept in ``received`` (raw).
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import ssl
from dataclasses import dataclass, field

from indus.gateway.adapters.irc.protocol import IrcLine
from tests.fakes.platform.core import FailureKind, FakeChatPlatform, FakeInbound, FakePlatformError

SERVER = "irc.fake.test"
DEFAULT_CAPS = ("sasl", "message-tags", "server-time", "echo-message", "multi-prefix")


class FakeReader:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.queue.get()


class FakeWriter:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.buffer = b""
        self.closing = False

    def write(self, data: bytes) -> None:
        if self.closing:
            raise ConnectionResetError("the connection is closed")
        self.buffer += data

    async def drain(self) -> None:
        while b"\r\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\r\n", 1)
            await self.connection.handle(raw)

    def close(self) -> None:
        if not self.closing:
            self.closing = True
            self.connection.ended()

    async def wait_closed(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closing


@dataclass
class Connection:
    wire: IrcWire
    tls: bool
    reader: FakeReader = field(default_factory=FakeReader)
    writer: FakeWriter = field(init=False)
    nick: str = "*"
    user: str = ""
    password: str | None = None
    caps: set[str] = field(default_factory=set[str])
    negotiating: bool = False
    registered: bool = False
    account: str | None = None
    open: bool = True
    # A muted server reads lines and answers nothing (a stalled link).
    muted: bool = False
    received: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        self.writer = FakeWriter(self)

    # -- output ---------------------------------------------------------------------------------

    def send(self, line: str) -> None:
        if self.open:
            self.reader.queue.put_nowait((line + "\r\n").encode("utf-8"))

    def numeric(self, code: str, *params: str) -> None:
        self.send(f":{SERVER} {code} {self.nick} " + " ".join(params))

    def ended(self) -> None:
        if self.open:
            self.open = False
            self.reader.queue.put_nowait(b"")
            if self in self.wire.connections:
                self.wire.connections.remove(self)
            asyncio.ensure_future(self.wire.platform.disconnect())

    # -- input ----------------------------------------------------------------------------------

    async def handle(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace")
        self.received.append(text)
        line = IrcLine.parse(text)
        if line is None or not self.open or self.muted:
            return
        handler = getattr(self, f"on_{line.command.lower()}", None)
        if handler is not None:
            await handler(line)

    async def on_cap(self, line: IrcLine) -> None:
        sub = line.param(0).upper()
        if sub == "LS":
            self.negotiating = True
            self.send(f":{SERVER} CAP {self.nick} LS :{' '.join(self.wire.caps)}")
        elif sub == "REQ":
            wanted = line.param(-1).split()
            if all(c in self.wire.caps for c in wanted):
                self.caps.update(wanted)
                self.send(f":{SERVER} CAP {self.nick} ACK :{' '.join(wanted)}")
            else:
                self.send(f":{SERVER} CAP {self.nick} NAK :{' '.join(wanted)}")
        elif sub == "END":
            self.negotiating = False
            self.try_register()

    async def on_authenticate(self, line: IrcLine) -> None:
        arg = line.param(0)
        if arg == "PLAIN":
            self.send("AUTHENTICATE +")
            return
        try:
            authz, user, password = base64.b64decode(arg).decode().split("\x00")
        except (ValueError, UnicodeDecodeError):
            self.numeric("904", ":SASL authentication failed")
            return
        if self.wire.accounts.get(user) == password and authz in ("", user):
            self.account = user
            self.numeric(
                "900", f"{self.nick}!{user}@fake.host", user, f":You are now logged in as {user}"
            )
            self.numeric("903", ":SASL authentication successful")
        else:
            self.numeric("904", ":SASL authentication failed")

    async def on_pass(self, line: IrcLine) -> None:
        self.password = line.param(0)

    async def on_nick(self, line: IrcLine) -> None:
        wanted = line.param(0)
        if wanted.lower() in {n.lower() for n in self.wire.taken}:
            self.numeric("433", wanted, ":Nickname is already in use")
            return
        old, self.nick = self.nick, wanted
        if self.registered:
            self.send(f":{old}!u@fake.host NICK {wanted}")
        self.try_register()

    async def on_user(self, line: IrcLine) -> None:
        self.user = line.param(0)
        self.try_register()

    def try_register(self) -> None:
        if self.registered or self.negotiating or self.nick == "*" or not self.user:
            return
        if self.wire.server_password is not None and self.password != self.wire.server_password:
            self.numeric("464", ":Password incorrect")
            self.send("ERROR :Closing Link: bad password")
            self.ended()
            return
        self.registered = True
        self.numeric("001", f":Welcome to the fake network {self.nick}")

    async def on_ping(self, line: IrcLine) -> None:
        self.send(f":{SERVER} PONG {SERVER} :{line.param(-1)}")

    async def on_join(self, line: IrcLine) -> None:
        for channel in line.param(0).split(","):
            if channel.lower() in self.wire.closed_channels:
                self.numeric("473", channel, ":Cannot join channel (+i)")
                continue
            self.wire.joined.append(channel)
            self.send(f":{self.nick}!{self.user}@fake.host JOIN {channel}")
            self.numeric("366", channel, ":End of /NAMES list.")

    async def on_quit(self, line: IrcLine) -> None:
        self.send("ERROR :Closing Link: fake.host (Quit)")
        self.ended()

    def tags(self, **values: str | None) -> str:
        if "message-tags" not in self.caps and "server-time" not in self.caps:
            return ""
        parts = [k if v == "" else f"{k}={v}" for k, v in values.items() if v is not None]
        return "@" + ";".join(parts) + " " if parts else ""

    def stamp(self) -> str:
        return self.wire.platform.clock.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")

    async def on_privmsg(self, line: IrcLine) -> None:
        target, text = line.param(0), line.param(-1)
        try:
            sent = await self.wire.platform.post(target, text)
        except FakePlatformError as exc:
            self.refuse(target, exc)
            return
        if "echo-message" in self.caps:
            tags = self.tags(time=self.stamp(), msgid=sent.message_id)
            self.send(f"{tags}:{self.nick}!{self.user}@fake.host PRIVMSG {target} :{text}")

    def refuse(self, target: str, exc: FakePlatformError) -> None:
        kind = exc.kind
        if kind is FailureKind.READ_TIMEOUT:
            return  # the server never answers
        if kind is FailureKind.CHAT_NOT_FOUND:
            self.numeric("401", target, f":{exc.text}")
        elif kind is FailureKind.FORBIDDEN:
            self.numeric("404", target, f":{exc.text}")
        elif kind is FailureKind.TOO_LONG:
            self.numeric("417", f":{exc.text}")
        else:
            code = {
                FailureKind.PARSE_ERROR: "INVALID_UTF8",
                FailureKind.RATE_LIMIT: "RATE_LIMITED",
                FailureKind.THREAD_NOT_FOUND: "MESSAGE_NOT_FOUND",
                FailureKind.CONNECT_TIMEOUT: "TEMPORARILY_UNAVAILABLE",
            }[kind]
            wait = f" {int(exc.retry_after)}" if exc.retry_after is not None else ""
            self.send(f":{SERVER} FAIL PRIVMSG {code} {target}{wait} :{exc.text}")


class IrcWire:
    def __init__(
        self,
        platform: FakeChatPlatform,
        *,
        caps: tuple[str, ...] = DEFAULT_CAPS,
        accounts: dict[str, str] | None = None,
        server_password: str | None = None,
    ) -> None:
        self.platform = platform
        self.caps = caps
        self.accounts = dict(accounts or {})
        self.server_password = server_password
        self.taken: set[str] = set()
        self.closed_channels: set[str] = set()
        self.connections: list[Connection] = []
        self.history: list[Connection] = []
        self.opens: list[tuple[str, int, bool]] = []
        self.joined: list[str] = []
        self._ids = itertools.count(1)

    async def connect(
        self, host: str, port: int, tls: ssl.SSLContext | None
    ) -> tuple[FakeReader, FakeWriter]:
        self.opens.append((host, port, tls is not None))
        try:
            await self.platform.connect()
        except FakePlatformError as exc:
            raise TimeoutError(exc.text) from None
        connection = Connection(self, tls is not None)
        self.connections.append(connection)
        self.history.append(connection)
        return connection.reader, connection.writer

    @property
    def current(self) -> Connection:
        live = [c for c in self.connections if c.registered]
        if not live:
            raise RuntimeError("no registered client")
        return live[-1]

    def received(self) -> list[str]:
        return [line for c in self.history for line in c.received]

    def render(self, item: FakeInbound, connection: Connection) -> str | None:
        if item.thread_id or item.media or item.event != "message":
            return None
        text = item.text
        if item.chat_kind == "dm":
            target = connection.nick
        else:
            target = item.chat_id
            if item.mentions_bot:
                text = f"{connection.nick}: {text}"
        tags = connection.tags(
            time=connection.stamp(), msgid=item.message_id, bot="" if item.is_bot else None
        )
        user = item.user_id
        return f"{tags}:{user}!{user}@people.fake PRIVMSG {target} :{text}"

    async def pump(self) -> None:
        connection = self.current
        for item in self.platform.drain_inbound():
            line = self.render(item, connection)
            if line is not None:
                connection.send(line)

    def drop(self) -> None:
        for connection in list(self.connections):
            connection.ended()

    def mute(self) -> None:
        for connection in self.connections:
            connection.muted = True

    def ban(self) -> None:
        for connection in list(self.connections):
            connection.send("ERROR :Closing Link: fake.host (K-Lined)")
            connection.ended()


__all__ = ["DEFAULT_CAPS", "SERVER", "Connection", "FakeReader", "FakeWriter", "IrcWire"]
