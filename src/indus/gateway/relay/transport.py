# pyright: strict
"""The gateway's outbound WebSocket to a relay connector (P24-08).

Our own contract (D-16.1), version 1; ``docs/user/relay-contract.md`` is the connector author's
copy. The gateway dials **out**, so a gateway with no public address can still serve the chat
platforms a connector fronts. Every WebSocket text message is one JSON object with a ``type``.

Handshake. The first frame is a signed ``hello``::

    {"type": "hello", "v": 1, "gateway_id": ..., "ts": <unix seconds>, "nonce": <32 hex>,
     "sig": hex(HMAC-SHA256(secret, "indus-relay-v1\\n<gateway_id>\\n<ts>\\n<nonce>"))}

The connector checks the signature, keeps ``ts`` within ±300 s of its own clock and remembers
nonces inside that window (a captured hello cannot be replayed). It answers ``refused`` (with a
``reason``; ``auth`` for a bad signature) and closes, or one ``descriptor`` per fronted platform and
then ``ready``. The secret itself never leaves the process: only the HMAC does.

After ``ready`` (connector → gateway): ``event`` (one inbound chat event for ``platform``; a
replayed one carries ``buffer_id``), ``interrupt`` (stop the turn of a chat), ``result`` (the answer
to an ``op``), ``idle_ack``, ``descriptor`` (a re-advertised platform) and ``refused`` (credentials
revoked). Gateway → connector: ``op`` (``id``, ``platform``, ``action``), ``interrupt``, ``idle``
and ``ack`` (``buffer_id``, sent only after the event was handed to the gateway).

Reconnect. A socket that drops after a successful handshake is dialled again after 1 s, the delay
doubling on each failed attempt to a 60 s ceiling, and back to 1 s once a handshake succeeds again.
A refusal for bad credentials is never retried: at ``start`` it is a configuration problem, later
it means the secret was revoked and the fatal handler is told.

Dormant. ``go_dormant`` asks the connector to buffer (``idle``), waits for ``idle_ack`` (the
connector now holds new events for us), then closes the socket and does not re-dial until
``wake`` (or the next gateway start). The connector replays the buffer on the next handshake, in
order; each event is acknowledged only after the handler took it, and a buffer id already handled
is acknowledged again but not delivered twice.

Requests never raise: ``request`` answers a result mapping. Nothing sent (socket down) is a
definite ``unavailable``; a frame that left without an answer in time, or whose socket dropped,
is ``timeout`` (the connector may have applied it, so it is never re-sent).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
from collections import OrderedDict, deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from indus.foundation.clock import Clock
from indus.gateway.relay.descriptor import Descriptor, DescriptorError

__all__ = [
    "BACKOFF_CAP_S",
    "BACKOFF_START_S",
    "HELLO_CONTEXT",
    "SECRET_ENV",
    "TIMESTAMP_WINDOW_S",
    "NonceMemory",
    "RelayConnectError",
    "RelaySettings",
    "RelayTransport",
    "SocketOpener",
    "WebSocketLike",
    "backoff_delays",
    "dial_url",
    "httpx_ws_opener",
    "make_hello",
    "media_base_url",
    "sign_hello",
    "verify_hello",
]

log = logging.getLogger(__name__)

HELLO_CONTEXT: Final = "indus-relay-v1"
SECRET_ENV: Final = "INDUS_RELAY_SECRET"
TIMESTAMP_WINDOW_S: Final = 300
BACKOFF_START_S: Final = 1.0
BACKOFF_CAP_S: Final = 60.0
# Buffer ids we already handed over, remembered so a replay after a lost ack is not delivered twice.
SEEN_BUFFERS_MAX: Final = 1024
MAX_FRAME_BYTES: Final = 4 * 1024 * 1024
# How long ``stop`` lets operations already sent be answered (inside the 5 s disconnect budget).
STOP_DRAIN_S: Final = 2.0
INSTALL_HINT: Final = "pip install 'indus-agent[relay]' (httpx-ws)"

type State = Literal["closed", "connecting", "live", "dormant", "refused"]
type EventHandler = Callable[[str, Mapping[str, Any]], Awaitable[None]]
type InterruptHandler = Callable[[str, str, str | None], Awaitable[None]]
type FatalHandler = Callable[[str, str], object]


# -- signing -------------------------------------------------------------------------------------


def _signed_text(gateway_id: str, timestamp: int, nonce: str) -> bytes:
    return f"{HELLO_CONTEXT}\n{gateway_id}\n{timestamp}\n{nonce}".encode()


def sign_hello(secret: str, gateway_id: str, timestamp: int, nonce: str) -> str:
    """The hex HMAC-SHA256 a hello carries."""
    mac = hmac.new(secret.encode("utf-8"), _signed_text(gateway_id, timestamp, nonce), "sha256")
    return mac.hexdigest()


def make_hello(gateway_id: str, secret: str, *, now: datetime, nonce: str) -> dict[str, Any]:
    timestamp = int(now.timestamp())
    return {
        "type": "hello",
        "v": 1,
        "gateway_id": gateway_id,
        "ts": timestamp,
        "nonce": nonce,
        "sig": sign_hello(secret, gateway_id, timestamp, nonce),
    }


@dataclass
class NonceMemory:
    """Nonces seen inside the timestamp window (the connector side of replay protection)."""

    window_s: float = TIMESTAMP_WINDOW_S
    _seen: OrderedDict[str, float] = field(default_factory=OrderedDict[str, float])

    def admit(self, nonce: str, now: float) -> bool:
        """False when ``nonce`` was already used inside the window."""
        while self._seen:
            oldest, at = next(iter(self._seen.items()))
            if now - at <= 2 * self.window_s:
                break
            del self._seen[oldest]
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True


def verify_hello(
    frame: Mapping[str, Any],
    secret: str,
    *,
    now: datetime,
    seen: NonceMemory,
    window_s: float = TIMESTAMP_WINDOW_S,
) -> str | None:
    """Why a hello is refused, or None when it is good (for connector authors and fakes)."""
    gateway_id, ts, nonce, sig = (frame.get(k) for k in ("gateway_id", "ts", "nonce", "sig"))
    if frame.get("type") != "hello" or frame.get("v") != 1:
        return "not a version 1 hello"
    if not isinstance(gateway_id, str) or not gateway_id:
        return "no gateway id"
    if not isinstance(ts, int) or isinstance(ts, bool):
        return "no timestamp"
    if not isinstance(nonce, str) or not 16 <= len(nonce) <= 128:
        return "no nonce"
    if not isinstance(sig, str):
        return "no signature"
    expected = sign_hello(secret, gateway_id, ts, nonce)
    if not hmac.compare_digest(sig.encode("utf-8"), expected.encode("utf-8")):
        return "bad signature"
    clock_now = now.timestamp()
    if abs(clock_now - ts) > window_s:
        return "timestamp outside the window"
    if not seen.admit(nonce, clock_now):
        return "nonce already used"
    return None


def backoff_delays(start: float = BACKOFF_START_S, cap: float = BACKOFF_CAP_S) -> Iterator[float]:
    """1, 2, 4, ... doubling to ``cap``, then ``cap`` forever."""
    delay = start
    while True:
        yield delay
        delay = min(delay * 2, cap)


# -- URLs ----------------------------------------------------------------------------------------


def dial_url(base: str) -> str:
    """The connector's WebSocket URL from its configured base (``https`` → ``wss``, path
    ``/relay``)."""
    parts = urlsplit(base.strip())
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme, parts.scheme)
    if scheme not in ("ws", "wss") or not parts.netloc:
        raise ValueError("the relay URL must be an http(s) or ws(s) URL with a host")
    path = parts.path.rstrip("/")
    if not path.endswith("/relay"):
        path = f"{path}/relay"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def media_base_url(base: str) -> str:
    """The connector's media endpoint (``/relay/media`` over http(s))."""
    parts = urlsplit(dial_url(base))
    scheme = "https" if parts.scheme == "wss" else "http"
    return urlunsplit((scheme, parts.netloc, f"{parts.path}/media", "", ""))


# -- the socket seam -----------------------------------------------------------------------------


class WebSocketLike(Protocol):
    """What the transport needs from a WebSocket session (httpx-ws's ``AsyncWebSocketSession``)."""

    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...


type SocketOpener = Callable[[str], contextlib.AbstractAsyncContextManager[WebSocketLike]]


@contextlib.asynccontextmanager
async def _httpx_ws_session(url: str, proxy: str | None) -> AsyncGenerator[WebSocketLike]:
    try:
        import httpx_ws  # pyright: ignore[reportMissingImports]
    except ModuleNotFoundError:
        raise RelayConnectError(
            f"the relay transport needs httpx-ws: {INSTALL_HINT}", retryable=False
        ) from None
    import httpx

    http_url = "http" + url[2:] if url.startswith("ws") else url
    async with httpx.AsyncClient(
        trust_env=False, proxy=proxy, timeout=httpx.Timeout(30.0, connect=10.0)
    ) as client:
        # typed through our protocol: httpx-ws is optional, so it may be absent at type-check time
        session = cast(
            "contextlib.AbstractAsyncContextManager[WebSocketLike]",
            httpx_ws.aconnect_ws(  # pyright: ignore[reportUnknownMemberType]
                http_url, client, max_message_size_bytes=MAX_FRAME_BYTES
            ),
        )
        async with session as socket:
            yield socket


def httpx_ws_opener(proxy: str | None = None) -> SocketOpener:
    """The production opener: an httpx-ws session to the connector."""

    def open_socket(url: str) -> contextlib.AbstractAsyncContextManager[WebSocketLike]:
        return _httpx_ws_session(url, proxy)

    return open_socket


# -- settings and errors -------------------------------------------------------------------------


class RelayConnectError(Exception):
    """The transport could not start; ``retryable`` false means a configuration problem."""

    def __init__(self, message: str, *, retryable: bool = True, code: str = "relay_connect"):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(f"the relay connector refused this gateway ({reason})")
        self.reason = reason


@dataclass(frozen=True)
class RelaySettings:
    url: str
    gateway_id: str
    secret: str = field(repr=False)
    connect_timeout: float = 30.0
    request_timeout: float = 30.0
    idle_ack_timeout: float = 10.0

    def check(self) -> None:
        """``RelayConnectError`` (not retryable) for settings that cannot work."""
        if not self.gateway_id.strip():
            raise RelayConnectError("gateway.relay.gateway_id is empty", retryable=False)
        if not self.secret:
            raise RelayConnectError(f"{SECRET_ENV} is not set", retryable=False)
        try:
            dial_url(self.url)
        except ValueError as exc:
            raise RelayConnectError(f"gateway.relay.url: {exc}", retryable=False) from None


def _dict(value: object) -> dict[str, Any]:
    return dict(cast(Mapping[str, Any], value)) if isinstance(value, Mapping) else {}


def _unavailable(why: str) -> dict[str, Any]:
    return {"ok": False, "code": "unavailable", "error": f"relay: {why} (connection refused)"}


def _no_answer(why: str) -> dict[str, Any]:
    return {"ok": False, "code": "timeout", "error": f"relay: {why} (timed out)"}


# -- the transport -------------------------------------------------------------------------------


class RelayTransport:
    """One connection to one connector, shared by every relay adapter (see the module notes)."""

    def __init__(
        self,
        settings: RelaySettings,
        *,
        clock: Clock,
        opener: SocketOpener | None = None,
        nonce: Callable[[], str] | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self._opener: SocketOpener = opener or httpx_ws_opener()
        self._nonce = nonce or (lambda: secrets.token_hex(16))
        self._state: State = "closed"
        self._descriptors: dict[str, Descriptor] = {}
        self._on_event: EventHandler | None = None
        self._on_interrupt: InterruptHandler | None = None
        self._on_fatal: FatalHandler | None = None
        self._socket: WebSocketLike | None = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ids = 0
        self._generation = 0
        self._idle_ack: asyncio.Future[None] | None = None
        self._hangup = asyncio.Event()
        self._wake = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None
        self._first: asyncio.Future[None] | None = None
        self._stopping = False
        self._seen_buffers: OrderedDict[str, None] = OrderedDict()
        self._users = 0
        # The most recent reconnect delays, newest last (status lines and tests).
        self.recent_delays: deque[float] = deque(maxlen=32)

    def __repr__(self) -> str:
        return f"<RelayTransport {self.settings.gateway_id} {self._state}>"

    # -- facts ------------------------------------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def live(self) -> bool:
        return self._state == "live" and self._socket is not None

    @property
    def descriptors(self) -> Mapping[str, Descriptor]:
        return dict(self._descriptors)

    def descriptor(self, platform: str) -> Descriptor | None:
        return self._descriptors.get(platform)

    def fronts(self, platform: str) -> bool:
        """Does the connector advertise ``platform`` on this connection?"""
        return platform in self._descriptors

    # -- handlers ---------------------------------------------------------------------------------

    def set_event_handler(self, handler: EventHandler | None) -> None:
        self._on_event = handler

    def set_interrupt_handler(self, handler: InterruptHandler | None) -> None:
        self._on_interrupt = handler

    def set_fatal_handler(self, handler: FatalHandler | None) -> None:
        self._on_fatal = handler

    # -- shared use by adapters -------------------------------------------------------------------

    async def acquire(self) -> None:
        """One more user; the first one starts the transport. Raises `RelayConnectError`."""
        self._users += 1
        try:
            await self.start()
        except BaseException:
            self._users -= 1
            raise

    async def release(self) -> None:
        """One user fewer; the last one stops the transport."""
        self._users = max(0, self._users - 1)
        if self._users == 0:
            await self.stop()

    # -- lifecycle --------------------------------------------------------------------------------

    async def start(self) -> None:
        """Dial, authenticate and collect the descriptors; afterwards keep the connection up.
        Idempotent while running. Raises `RelayConnectError` when the first dial fails."""
        if self._supervisor is not None and not self._supervisor.done():
            if self._first is not None:
                await asyncio.shield(self._first)
            return
        self.settings.check()
        self._stopping = False
        self._wake.clear()
        loop = asyncio.get_running_loop()
        self._first = loop.create_future()
        self._state = "connecting"
        self._supervisor = asyncio.ensure_future(self._supervise(self._first))
        try:
            await asyncio.shield(self._first)
        except BaseException:
            await self._end_supervisor()
            self._state = "closed"
            raise

    async def stop(self) -> None:
        """Close the socket and stop re-dialling. Operations already sent get up to
        ``STOP_DRAIN_S`` to be answered (a turn's last edit is often in flight); the rest fail."""
        waiting = [f for f in self._pending.values() if not f.done()]
        if waiting and self.live:
            with contextlib.suppress(TimeoutError):
                await self._within(asyncio.wait(waiting), STOP_DRAIN_S)
        self._stopping = True
        self._hangup.set()
        self._wake.set()
        await self._end_supervisor()
        self._fail_pending(_unavailable("the relay transport stopped"))
        self._state = "closed"

    async def _end_supervisor(self) -> None:
        task, self._supervisor = self._supervisor, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def go_dormant(self) -> bool:
        """Ask the connector to buffer our events, then close the socket (see the module notes).
        False (and still connected) when no ``idle_ack`` came in time."""
        if not self.live:
            return False
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._idle_ack = fut
        try:
            await self._send({"type": "idle"})
            await self._within(fut, self.settings.idle_ack_timeout)
        except Exception:
            log.warning("relay: the connector did not confirm going dormant; staying connected")
            return False
        finally:
            self._idle_ack = None
        self._state = "dormant"
        self._wake.clear()
        self._hangup.set()
        return True

    async def wake(self) -> None:
        """Leave dormant mode: dial again now (the connector replays what it buffered)."""
        if self._state == "dormant":
            self._state = "connecting"
            self._wake.set()

    # -- outbound ---------------------------------------------------------------------------------

    async def request(self, platform: str, action: Mapping[str, Any]) -> dict[str, Any]:
        """Send one operation and wait for its result (see the module notes); never raises."""
        if not self.live:
            return _unavailable("the relay is not connected")
        self._ids += 1
        request_id = f"{self._generation}-{self._ids}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        frame = {"type": "op", "id": request_id, "platform": platform, "action": dict(action)}
        try:
            try:
                await self._send(frame)
            except Exception:
                return _unavailable("the connection dropped before the operation was sent")
            try:
                return await self._within(fut, self.settings.request_timeout)
            except TimeoutError:
                return _no_answer("no answer from the connector in time")
        finally:
            self._pending.pop(request_id, None)

    async def interrupt(
        self, platform: str, chat_id: str, *, session_key: str | None = None, reason: str = ""
    ) -> bool:
        """Tell the connector a turn was stopped here (it forwards the stop to its other ends)."""
        if not self.live:
            return False
        frame: dict[str, Any] = {"type": "interrupt", "platform": platform, "chat_id": chat_id}
        if session_key:
            frame["session_key"] = session_key
        if reason:
            frame["reason"] = reason
        try:
            await self._send(frame)
        except Exception:
            return False
        return True

    # -- internals: timing ------------------------------------------------------------------------

    async def _within[T](self, awaitable: Awaitable[T], seconds: float) -> T:
        """``awaitable`` with a deadline on the transport's clock (``TimeoutError`` past it)."""
        work = asyncio.ensure_future(awaitable)
        timer = asyncio.ensure_future(self.clock.sleep(seconds))
        try:
            done, _ = await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            work.cancel()
            timer.cancel()
            raise
        timer.cancel()
        if work in done:
            return work.result()
        work.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await work
        raise TimeoutError

    # -- internals: the connection ----------------------------------------------------------------

    async def _supervise(self, first: asyncio.Future[None]) -> None:
        delays = backoff_delays()
        while not self._stopping:
            handshaken = False
            try:
                handshaken = await self._session(first)
            except asyncio.CancelledError:
                raise
            except _Refused as exc:
                self._state = "refused"
                if not first.done():
                    retryable = exc.reason != "auth"
                    first.set_exception(
                        RelayConnectError(str(exc), retryable=retryable, code="relay_refused")
                    )
                    return
                log.warning("relay: %s; not reconnecting", exc)
                self._report_fatal("relay_refused", str(exc))
                return
            except Exception as exc:
                if not first.done():
                    first.set_exception(RelayConnectError(f"{type(exc).__name__}: {exc}"))
                    return
                log.warning("relay: connection failed: %s", type(exc).__name__)
            if self._stopping:
                return
            if self._state == "dormant":
                await self._wake.wait()
                self._wake.clear()
                delays = backoff_delays()
                continue
            if handshaken:
                delays = backoff_delays()
            self._state = "connecting"
            delay = next(delays)
            self.recent_delays.append(delay)
            await self.clock.sleep(delay)

    async def _session(self, first: asyncio.Future[None]) -> bool:
        """One socket from dial to drop; True when its handshake succeeded."""
        self._hangup.clear()
        url = dial_url(self.settings.url)
        async with contextlib.AsyncExitStack() as stack:
            socket = await self._within(
                stack.enter_async_context(self._opener(url)), self.settings.connect_timeout
            )
            self._generation += 1
            self._socket = socket
            try:
                early = await self._within(self._handshake(socket), self.settings.connect_timeout)
                self._state = "live"
                if not first.done():
                    first.set_result(None)
                for frame in early:
                    await self._dispatch(frame)
                reader = asyncio.ensure_future(self._read(socket))
                hangup = asyncio.ensure_future(self._hangup.wait())
                try:
                    await asyncio.wait({reader, hangup}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    hangup.cancel()
                    if not reader.done():
                        reader.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await reader
                if reader.done() and not reader.cancelled():
                    error = reader.exception()
                    if isinstance(error, _Refused):
                        raise error
                return True
            finally:
                self._socket = None
                if self._state == "live":
                    self._state = "connecting"
                self._fail_pending(_no_answer("the connection dropped before the answer"))

    async def _handshake(self, socket: WebSocketLike) -> list[dict[str, Any]]:
        """Send the hello and read until ``ready``; frames that came early are returned."""
        hello = make_hello(
            self.settings.gateway_id,
            self.settings.secret,
            now=self.clock.now(),
            nonce=self._nonce(),
        )
        await self._send(hello, socket)
        found: dict[str, Descriptor] = {}
        early: list[dict[str, Any]] = []
        while True:
            frame = self._decode(await socket.receive_text())
            if frame is None:
                continue
            kind = frame.get("type")
            if kind == "refused":
                raise _Refused(str(frame.get("reason") or "refused"))
            if kind == "descriptor":
                self._adopt(frame, found)
            elif kind == "ready":
                self._descriptors = found
                return early
            else:
                early.append(frame)

    def _adopt(self, frame: Mapping[str, Any], into: dict[str, Descriptor]) -> None:
        try:
            descriptor = Descriptor.parse(frame.get("descriptor"))
        except DescriptorError as exc:
            log.warning("relay: a descriptor was refused: %s", exc)
            return
        into[descriptor.platform] = descriptor

    @staticmethod
    def _decode(text: str) -> dict[str, Any] | None:
        try:
            value = json.loads(text)
        except ValueError:
            log.warning("relay: skipped a frame that is not JSON")
            return None
        if not isinstance(value, dict):
            log.warning("relay: skipped a frame that is not a JSON object")
            return None
        return cast(dict[str, Any], value)

    async def _read(self, socket: WebSocketLike) -> None:
        while True:
            frame = self._decode(await socket.receive_text())
            if frame is not None:
                await self._dispatch(frame)

    async def _dispatch(self, frame: Mapping[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "result":
            fut = self._pending.get(str(frame.get("id") or ""))
            if fut is not None and not fut.done():
                fut.set_result(_dict(frame.get("result")))
        elif kind == "event":
            await self._event(frame)
        elif kind == "interrupt":
            await self._interrupt(frame)
        elif kind == "idle_ack":
            if self._idle_ack is not None and not self._idle_ack.done():
                self._idle_ack.set_result(None)
        elif kind == "descriptor":
            self._adopt(frame, self._descriptors)
        elif kind == "refused":
            raise _Refused(str(frame.get("reason") or "refused"))
        else:
            log.debug("relay: ignored a frame of type %r", kind)

    async def _event(self, frame: Mapping[str, Any]) -> None:
        platform = str(frame.get("platform") or "")
        buffer_id = frame.get("buffer_id")
        tag = str(buffer_id) if isinstance(buffer_id, str | int) and buffer_id != "" else None
        if tag is not None and tag in self._seen_buffers:
            await self._ack(tag)
            return
        handler = self._on_event
        if handler is None:
            log.warning(
                "relay: an event for %s arrived with no handler; left unacknowledged", platform
            )
            return
        try:
            await handler(platform, _dict(frame.get("event")))
        except Exception:
            log.exception("relay: the handler for a %s event failed; left unacknowledged", platform)
            return
        if tag is not None:
            self._seen_buffers[tag] = None
            while len(self._seen_buffers) > SEEN_BUFFERS_MAX:
                self._seen_buffers.popitem(last=False)
            await self._ack(tag)

    async def _ack(self, buffer_id: str) -> None:
        try:
            await self._send({"type": "ack", "buffer_id": buffer_id})
        except Exception:
            log.debug("relay: an ack could not be sent (the connector will replay)")

    async def _interrupt(self, frame: Mapping[str, Any]) -> None:
        handler = self._on_interrupt
        if handler is None:
            return
        session_key = frame.get("session_key")
        try:
            await handler(
                str(frame.get("platform") or ""),
                str(frame.get("chat_id") or ""),
                session_key if isinstance(session_key, str) and session_key else None,
            )
        except Exception:
            log.exception("relay: the interrupt handler failed")

    async def _send(self, frame: Mapping[str, Any], socket: WebSocketLike | None = None) -> None:
        target = socket or self._socket
        if target is None:
            raise ConnectionError("the relay socket is not open")
        text = json.dumps(frame, separators=(",", ":"), ensure_ascii=False)
        async with self._send_lock:
            await target.send_text(text)

    def _fail_pending(self, result: dict[str, Any]) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(dict(result))

    def _report_fatal(self, code: str, message: str) -> None:
        handler = self._on_fatal
        if handler is None:
            return
        try:
            handler(code, message)
        except Exception:
            log.warning("relay: the fatal handler failed", exc_info=True)
