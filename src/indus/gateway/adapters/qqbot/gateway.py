"""The QQ Bot gateway WebSocket session (P16-20).

One `QQGateway` keeps one bot online:

1. fetch the gateway URL, open the socket; the server says Hello (op 10, ``heartbeat_interval`` ms);
2. send Identify (op 2: token, intents, shard ``[0, 1]``) — or Resume (op 6: token, session id and
   the last sequence number) when a session survives from the previous socket;
3. READY (op 0) gives the session id, RESUMED confirms a resume; every dispatch's ``s`` is kept
   (it only grows) and the event goes to ``on_event(type, data)``;
4. heartbeats (op 1 with the last sequence) go out at 80 % of the interval; the server's ACK
   (op 11) must arrive before the next beat, else the socket counts as dead and is replaced;
5. op 7 (reconnect) ends the socket and resumes; op 9 (invalid session) ends it too, resuming only
   when its data is true.

Ending a socket: waits 2, 5, 10, 30, 60 s (then 60 s) between attempts; the wait resets once a
session is ready again. Close codes: 4004 drops the access token; 4006, 4007 and 4900–4913 drop the
session (identify next time); 4008 (rate limited) waits 60 s; 4001, 4002, 4010–4014, 4914 and 4915
can never recover and stop the loop as fatal. 100 failed attempts in a row, or three sockets in a
row that close within 5 s of opening, are fatal too (usually missing bot permissions).

All waiting goes through the injected clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from indus.foundation.clock import Clock
from indus.gateway.adapters.qqbot.client import QQSocket, SocketClosed

__all__ = [
    "BACKOFF_S",
    "FATAL_CLOSE_CODES",
    "INTENTS",
    "MAX_ATTEMPTS",
    "QQGateway",
    "ZombieSocket",
]

log = logging.getLogger(__name__)

# C2C and group @-messages (1 << 25), button interactions (1 << 26), guild DMs (1 << 12) and
# public guild @-messages (1 << 30).
INTENTS = (1 << 25) | (1 << 26) | (1 << 12) | (1 << 30)
BACKOFF_S: tuple[float, ...] = (2.0, 5.0, 10.0, 30.0, 60.0)
MAX_ATTEMPTS = 100
RATE_LIMIT_WAIT_S = 60.0
QUICK_CLOSE_S = 5.0
MAX_QUICK_CLOSES = 3
HEARTBEAT_SHARE = 0.8
DEFAULT_HEARTBEAT_S = 30.0
FATAL_CLOSE_CODES: Mapping[int, str] = {
    4001: "invalid opcode",
    4002: "invalid payload",
    4010: "invalid shard",
    4011: "sharding required",
    4012: "invalid API version",
    4013: "invalid intents",
    4014: "intents not allowed for this bot",
    4914: "the bot is offline or sandbox-only",
    4915: "the bot is banned",
}
SESSION_CLOSE_CODES = frozenset({4006, 4007, *range(4900, 4914)})

type Opener = Callable[[], Awaitable[contextlib.AbstractAsyncContextManager[QQSocket]]]
type EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class ZombieSocket(Exception):
    """The server stopped acknowledging heartbeats."""


class _Reconnect(Exception):
    """The server asked for a new socket (op 7 or op 9)."""


class QQGateway:
    """See the module notes."""

    def __init__(
        self,
        *,
        open_socket: Opener,
        token: Callable[[], Awaitable[str]],
        drop_token: Callable[[], None],
        on_event: EventHandler,
        on_fatal: Callable[[str, str], None],
        clock: Clock,
        intents: int = INTENTS,
    ) -> None:
        self._open = open_socket
        self._token = token
        self._drop_token = drop_token
        self._on_event = on_event
        self._on_fatal = on_fatal
        self.clock = clock
        self.intents = intents
        self.session_id: str | None = None
        self.seq: int | None = None
        self.heartbeat_s = DEFAULT_HEARTBEAT_S
        self.ready = False
        self.opens = 0
        self.identifies = 0
        self.resumes = 0
        self.failures = 0
        self.waits: list[float] = []
        self._acked = True

    # -- the loop ---------------------------------------------------------------------------------

    async def run(self) -> None:
        quick = 0
        while True:
            opened_at = self.clock.monotonic()
            opens_before = self.opens
            wait: float | None = None
            asked = False
            try:
                await self._one_socket()
            except asyncio.CancelledError:
                raise
            except _Reconnect:
                asked = True
            except SocketClosed as exc:
                code = exc.code
                reason = FATAL_CLOSE_CODES.get(code) if code is not None else None
                if reason is not None:
                    log.error("qqbot: the gateway closed with %s (%s)", code, reason)
                    self._on_fatal("qqbot_closed", f"QQ closed the gateway: {reason} ({code})")
                    return
                if code == 4004:
                    self._drop_token()
                if code in SESSION_CLOSE_CODES:
                    self._forget_session()
                if code == 4008:
                    wait = RATE_LIMIT_WAIT_S
                log.warning("qqbot: gateway socket closed (code %s)", code)
            except ZombieSocket:
                log.warning("qqbot: no heartbeat acknowledgement; replacing the socket")
            except ModuleNotFoundError as exc:
                self._on_fatal("missing_dependency", str(exc))
                return
            except Exception as exc:
                log.warning("qqbot: gateway socket failed: %s", type(exc).__name__)
            if self.ready:
                self.failures = 0
            self.failures += 1
            lived = self.clock.monotonic() - opened_at
            if self.opens > opens_before and not asked and lived < QUICK_CLOSE_S:
                quick += 1
            elif self.opens > opens_before:
                quick = 0
            if quick >= MAX_QUICK_CLOSES:
                self._on_fatal(
                    "qqbot_quick_disconnect",
                    "the QQ gateway closed three sockets right after they opened; check the app "
                    "id, secret and the bot's permissions",
                )
                return
            if self.failures >= MAX_ATTEMPTS:
                self._on_fatal(
                    "qqbot_reconnect", f"the QQ gateway failed {self.failures} times in a row"
                )
                return
            if wait is None:
                wait = BACKOFF_S[min(self.failures - 1, len(BACKOFF_S) - 1)]
            self.waits.append(wait)
            await self.clock.sleep(wait)

    def _forget_session(self) -> None:
        self.session_id = None
        self.seq = None

    async def _one_socket(self) -> None:
        self.ready = False
        self._acked = True
        self.heartbeat_s = DEFAULT_HEARTBEAT_S
        manager = await self._open()
        async with manager as socket:
            self.opens += 1
            hello = await self._read(socket)
            if hello.get("op") != 10:
                raise SocketClosed(None, "no Hello from the gateway")
            interval = _map(hello.get("d")).get("heartbeat_interval")
            if isinstance(interval, int | float) and interval > 0:
                self.heartbeat_s = float(interval) / 1000.0 * HEARTBEAT_SHARE
            await self._authenticate(socket)
            reader = asyncio.ensure_future(self._read_loop(socket))
            beater = asyncio.ensure_future(self._beat(socket))
            try:
                done, _ = await asyncio.wait({reader, beater}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (reader, beater):
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task
            for task in done:
                task.result()

    async def _authenticate(self, socket: QQSocket) -> None:
        token = f"QQBot {await self._token()}"
        if self.session_id and self.seq is not None:
            self.resumes += 1
            frame: dict[str, Any] = {
                "op": 6,
                "d": {"token": token, "session_id": self.session_id, "seq": self.seq},
            }
        else:
            self.identifies += 1
            frame = {
                "op": 2,
                "d": {
                    "token": token,
                    "intents": self.intents,
                    "shard": [0, 1],
                    "properties": {
                        "$os": platform.system().lower() or "unknown",
                        "$browser": "indus-agent",
                        "$device": "indus-agent",
                    },
                },
            }
        await socket.send_text(json.dumps(frame))

    async def _read(self, socket: QQSocket) -> dict[str, Any]:
        while True:
            raw = await socket.receive_text()
            try:
                frame = json.loads(raw)
            except ValueError:
                log.debug("qqbot: a gateway frame is not JSON; skipped")
                continue
            if isinstance(frame, dict):
                return cast(dict[str, Any], frame)

    async def _read_loop(self, socket: QQSocket) -> None:
        while True:
            await self._handle(await self._read(socket))

    async def _handle(self, frame: Mapping[str, Any]) -> None:
        op = frame.get("op")
        seq = frame.get("s")
        if (
            isinstance(seq, int)
            and not isinstance(seq, bool)
            and (self.seq is None or seq > self.seq)
        ):
            self.seq = seq
        if op == 11:
            self._acked = True
        elif op == 7:
            raise _Reconnect()
        elif op == 9:
            if frame.get("d") is not True:
                self._forget_session()
            raise _Reconnect()
        elif op == 0:
            kind = str(frame.get("t") or "")
            data = _map(frame.get("d"))
            if kind == "READY":
                session = data.get("session_id")
                self.session_id = str(session) if session else None
                self.ready = True
                log.info("qqbot: gateway session ready")
            elif kind == "RESUMED":
                self.ready = True
                log.info("qqbot: gateway session resumed")
            elif kind:
                await self._on_event(kind, data)

    async def _beat(self, socket: QQSocket) -> None:
        while True:
            await self.clock.sleep(self.heartbeat_s)
            if not self._acked:
                raise ZombieSocket()
            self._acked = False
            await socket.send_text(json.dumps({"op": 1, "d": self.seq}))


def _map(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}
