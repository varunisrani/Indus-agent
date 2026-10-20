"""The Python attach channel: JSON-RPC 2.0 over the daemon's WebSocket (P17-16).

A ``Channel`` owns one connection to the profile's daemon at a time and gets a new one when it is
lost. Every connection starts with a dial (``Dial``): ``home_dialer`` asks the owner-only control
socket for the daemon's ids (``describe``) and a fresh single-use ticket (``session-ticket``),
then opens the loopback ``ws_url`` that came with it, offering ``indus-gateway-v1`` and
``indus-gateway-ticket.<ticket>``. A ticket is never reused (R08.4) and never shown: not in a URL,
an argv, a log line or a ``repr``.

Profile binding (P17-24, ``profile_guard``): ``describe`` must name this home's own profile id
before any ticket is asked for, else the dial fails ``profile_mismatch`` (terminal).

After the upgrade the server must echo exactly ``indus-gateway-v1`` and send ``gateway.ready``
first (within ``ready_timeout``), for the profile the grant named — otherwise the socket is closed
4005 before anything is sent (``profile_mismatch``); the channel then tells it the
surface and features (``client.capabilities``) and runs the ``on_ready`` hooks (the remote host
re-subscribes its sessions there) before callers' requests flow again.

Requests: ids are client integers (a server's own request ids are ``srq-…`` strings); each
waits at most its method's budget (``ChannelLimits.timeout_for``: 120 s, ``prompt.submit``
1,800 s, ``server_request.answer`` 330 s, ``session.compress`` 660 s). A request made while no
connection is up waits for the next one within its budget, else fails ``NotSent`` (nothing reached
the daemon). When the connection goes, every request in flight on it fails ``TransportLost`` at
once: it was sent, so its outcome is unknown and it must not be sent again on its own.

Liveness (R05.9): when ``gateway.ready`` says ``heartbeat``, a ``gateway.ping`` goes out every 15 s
(at most 8 outstanding); any inbound frame counts as life; no frame for 45 s closes the socket.
Reconnect: after 1 s, doubling to 30 s, no jitter; reaching ready resets the count. A socket that
was replaced is ignored (its frames and its close), so a late close never ends the new one.

Inbound notifications go to ``on_notification`` listeners in arrival order. A JSON-RPC request
from the server that no ``on_call`` handler takes is answered ``-32601`` at once (R05.3).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock, SystemClock
from indus.protocol.bridge import READY_METHOD
from indus.protocol.errors import METHOD_NOT_FOUND
from indus.service import gateway_control
from indus.service.gateway_ensure import RecoverBudget, ensure
from indus.service.remote import profile_guard
from indus.service.ws_auth import SUBPROTOCOL, TICKET_PROTOCOL_PREFIX, WS_INTERACTIVE

__all__ = [
    "ChannelError",
    "ChannelLimits",
    "ChannelState",
    "Channel",
    "Dial",
    "DialRefused",
    "Dialed",
    "Grant",
    "NotSent",
    "RequestTimedOut",
    "RpcError",
    "Transport",
    "TransportLost",
    "grant_for",
    "home_dialer",
]

log = logging.getLogger(__name__)

type ChannelState = Literal["idle", "connecting", "ready", "reconnecting", "closed", "failed"]
type Notification = Callable[[str, Mapping[str, Any]], object]
type CallHandler = Callable[[str | int, str, Mapping[str, Any]], bool]
type StateListener = Callable[[ChannelState, Mapping[str, Any]], object]
type ReadyHook = Callable[["Channel"], Awaitable[None]]

PING_METHOD: Final = "gateway.ping"
CAPABILITIES_METHOD: Final = "client.capabilities"
TRANSPORT_LOST: Final = (
    "The connection to the gateway was lost; the outcome is unknown. Do not send it again: "
    "resume the session to see whether it completed."
)
NOT_SENT: Final = "The gateway is not connected; nothing was sent."


# -- errors ---------------------------------------------------------------------------------------


class ChannelError(Exception):
    """Base of every channel failure."""


class NotSent(ChannelError):
    """Nothing reached the daemon (no connection came up in time, or the channel is closed)."""

    def __init__(self, message: str = NOT_SENT) -> None:
        super().__init__(message)


class TransportLost(ChannelError):
    """The request was sent and the connection went before its answer: the outcome is unknown."""

    def __init__(self, message: str = TRANSPORT_LOST) -> None:
        super().__init__(message)


class RequestTimedOut(TransportLost):
    """The request was sent and no answer came within its budget (outcome unknown too)."""


class RpcError(ChannelError):
    """The daemon answered with a JSON-RPC error: ``code``, a bounded ``message`` and scalar
    ``data`` only (nothing nested from the far side is passed on)."""

    def __init__(self, code: int, message: str, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = dict(data or {})


class DialRefused(ChannelError):
    """A dial did not produce a usable connection. ``reason`` is a short code; ``terminal`` means
    redialling will not help until someone acts (unsafe home, another profile, stopped on
    purpose)."""

    def __init__(self, reason: str, detail: str = "", *, terminal: bool = False) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.terminal = terminal


def _bounded(value: object, limit: int = 200) -> str:
    text = value if isinstance(value, str) else ""
    return text[:limit]


def _scalar_data(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in cast(dict[object, object], data).items():
        if len(out) >= 16 or not isinstance(key, str) or len(key) > 64:
            continue
        short_text = isinstance(value, str) and len(value) <= 200
        if value is None or isinstance(value, bool | int | float) or short_text:
            out[key] = value
    return out


def _rpc_error(error: object) -> RpcError:
    body = cast(dict[str, object], error) if isinstance(error, dict) else {}
    code = body.get("code")
    number = code if isinstance(code, int) and not isinstance(code, bool) else -32603
    return RpcError(
        number, _bounded(body.get("message")) or "request failed", _scalar_data(body.get("data"))
    )


# -- transports and dialing -----------------------------------------------------------------------


class Transport(Protocol):
    """One open WebSocket (``ws_client.WsClientTransport``, or a test double)."""

    @property
    def subprotocol(self) -> str | None: ...

    async def send(self, text: str) -> None: ...

    async def recv(self) -> str:
        """The next text message; raises once the socket is gone."""
        ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


@dataclass(frozen=True)
class Dialed:
    """An opened transport and the ids its grant named (checked against ``gateway.ready``)."""

    transport: Transport
    profile_id: str | None = None
    instance_id: str | None = None


type Dial = Callable[[], Awaitable[Dialed]]


@dataclass(frozen=True)
class Grant:
    """One dial's ticket and where to use it. The ticket is never in ``repr``."""

    ws_url: str
    ticket: str = field(repr=False)
    profile_id: str
    instance_id: str

    def subprotocols(self) -> list[str]:
        return [SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + self.ticket]


_TERMINAL_GRANT: Final = frozenset({"unsafe_home", "profile_mismatch", "forbidden"})


def grant_for(home: Path, *, purpose: str = WS_INTERACTIVE, timeout: float = 5.0) -> Grant:
    """``describe`` then a fresh ``session-ticket`` for its ids (blocking). ``DialRefused`` when
    no daemon answers or it will not mint."""
    if not gateway_control.home_is_safe(home):
        raise DialRefused(
            "unsafe_home", "the profile home is writable by other users", terminal=True
        )
    described = gateway_control.describe(home, timeout=timeout)
    if described is None:
        raise DialRefused("no_daemon", "no gateway answers for this profile")
    try:
        # Our own home's id, not the one the daemon reported: a socket in this home that leads
        # to another profile's daemon gets no ticket request at all (P17-24).
        profile_guard.check_described(home, described)
    except profile_guard.ProfileMismatch as mismatch:
        raise DialRefused(mismatch.code, str(mismatch), terminal=True) from None
    reply = gateway_control.session_ticket(
        home,
        purpose,
        profile_id=str(described["profile_id"]),
        instance_id=str(described["instance_id"]),
        timeout=timeout,
    )
    if not reply.ok:
        reason = reply.error or "failed"
        raise DialRefused(reason, reply.detail, terminal=reason in _TERMINAL_GRANT)
    return Grant(reply.ws_url, reply.ticket, reply.profile_id, reply.instance_id)


type Connect = Callable[[str, list[str]], Awaitable[Transport]]


def home_dialer(
    home: Path,
    *,
    env: Mapping[str, str] | None = None,
    budget: RecoverBudget | None = None,
    connect: Connect | None = None,
) -> Dial:
    """Dial the daemon serving ``home``: a fresh grant per dial (``grant_for``). When no daemon
    answers and ``env`` is given, an automatic recovery (``ensure(mode="recover")``) within
    ``budget`` starts it again first."""

    async def opened(url: str, protocols: list[str]) -> Transport:
        if connect is not None:
            return await connect(url, protocols)
        from indus.service.remote.ws_client import connect as ws_connect

        return await ws_connect(url, protocols)

    async def dial() -> Dialed:
        try:
            grant = await run_blocking(grant_for, home, label="attach grant")
        except DialRefused as refused:
            if refused.reason != "no_daemon" or env is None:
                raise
            values = env
            ensured = await run_blocking(
                lambda: ensure(home, values, mode="recover", budget=budget), label="gateway ensure"
            )
            if not ensured.attachable:
                raise DialRefused(
                    ensured.reason or ensured.state, ensured.detail, terminal=ensured.terminal
                ) from None
            grant = await run_blocking(grant_for, home, label="attach grant")
        return Dialed(
            await opened(grant.ws_url, grant.subprotocols()), grant.profile_id, grant.instance_id
        )

    return dial


# -- the channel ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelLimits:
    """Numbers of the shared channel (research 05 §3.2, 08 §3.9)."""

    request_timeout: float = 120.0
    method_timeouts: Mapping[str, float] = field(
        default_factory=lambda: {
            "prompt.submit": 1800.0,
            "server_request.answer": 330.0,
            "session.compress": 660.0,
        }
    )
    ready_timeout: float = 15.0
    ping_interval: float = 15.0
    liveness_deadline: float = 45.0
    max_pings: int = 8
    backoff_first: float = 1.0
    backoff_cap: float = 30.0

    def timeout_for(self, method: str) -> float:
        return self.method_timeouts.get(method, self.request_timeout)

    def backoff(self, attempt: int) -> float:
        """Delay before reconnect ``attempt`` (1-based): 1, 2, 4 … capped, no jitter."""
        return min(self.backoff_cap, self.backoff_first * (2 ** max(0, attempt - 1)))


class _Live:
    """One connected transport and what belongs to it."""

    def __init__(self, transport: Transport, epoch: int, now: float) -> None:
        self.transport = transport
        self.epoch = epoch
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.pings: set[int] = set()
        self.last_inbound = now
        self.heartbeat = False
        self.tasks: list[asyncio.Task[None]] = []
        self.gone = False


class Channel:
    """The attach channel (see the module notes)."""

    def __init__(
        self,
        dial: Dial,
        *,
        surface: str = "cli",
        features: Iterable[str] = ("server_requests",),
        clock: Clock | None = None,
        limits: ChannelLimits | None = None,
    ) -> None:
        self._dial = dial
        self.surface = surface
        self.features = list(features)
        self.clock: Clock = clock or SystemClock()
        self.limits = limits or ChannelLimits()
        self.state: ChannelState = "idle"
        self.epoch = 0
        self.ready_params: dict[str, Any] = {}
        self.enabled_features: list[str] = []
        self.failure: DialRefused | None = None
        self._ids = itertools.count(1)
        self._live: _Live | None = None
        self._ready = asyncio.Event()
        self._open = asyncio.Event()
        self._changed = asyncio.Event()
        self._closed = False
        self._reconnect: asyncio.Task[None] | None = None
        self._notifications: list[Notification] = []
        self._calls: list[CallHandler] = []
        self._states: list[StateListener] = []
        self._hooks: list[ReadyHook] = []

    # -- listeners --------------------------------------------------------------------------------

    def on_notification(self, listener: Notification) -> Callable[[], None]:
        self._notifications.append(listener)
        return lambda: self._drop(self._notifications, listener)

    def on_call(self, handler: CallHandler) -> Callable[[], None]:
        """A server→client request: return True when the handler answers it (``respond``)."""
        self._calls.append(handler)
        return lambda: self._drop(self._calls, handler)

    def on_state(self, listener: StateListener) -> Callable[[], None]:
        self._states.append(listener)
        return lambda: self._drop(self._states, listener)

    def on_ready(self, hook: ReadyHook) -> Callable[[], None]:
        """Run after every reconnect's handshake, before callers' requests flow again."""
        self._hooks.append(hook)
        return lambda: self._drop(self._hooks, hook)

    @staticmethod
    def _drop(items: list[Any], item: object) -> None:
        with contextlib.suppress(ValueError):
            items.remove(item)

    def _tell(self, state: ChannelState, **info: Any) -> None:
        self.state = state
        self._changed.set()
        self._changed = asyncio.Event()
        for listener in list(self._states):
            try:
                listener(state, info)
            except Exception:
                log.exception("a channel state listener failed")

    # -- lifetime ---------------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._open.is_set() and self._live is not None

    async def start(self) -> None:
        """The first connection; raises its dial failure (nothing retries a first dial)."""
        if self._closed:
            raise NotSent()
        if self._live is not None:
            return
        try:
            await self._connect()
        except DialRefused as refused:
            self.failure = refused
            self._tell("failed", reason=refused.reason)
            raise
        except Exception as exc:
            refused = DialRefused("connect_failed", type(exc).__name__)
            self.failure = refused
            self._tell("failed", reason=refused.reason)
            raise refused from exc

    async def close(self) -> None:
        """End the channel for good: no redial; requests in flight fail ``TransportLost``."""
        self._closed = True
        task, self._reconnect = self._reconnect, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        live = self._live
        if live is not None:
            await self._drop_live(live, code=1000, reason="client closed")
        self._open.set()
        self._ready.set()
        self._tell("closed")

    async def _connect(self) -> None:
        self._tell("connecting", attempt=self.epoch + 1)
        dialed = await self._dial()
        transport = dialed.transport
        if self._closed:
            await transport.close(1000, "client closed")
            raise NotSent()
        try:
            ready = await self._handshake(transport, dialed.profile_id)
        except DialRefused as refused:
            code = (
                profile_guard.CLOSE_PROFILE_MISMATCH
                if refused.reason == profile_guard.MISMATCH_CODE
                else 1002
            )
            with contextlib.suppress(Exception):
                await transport.close(code, "handshake failed")
            raise
        except BaseException:
            with contextlib.suppress(Exception):
                await transport.close(1002, "handshake failed")
            raise
        self.epoch += 1
        live = _Live(transport, self.epoch, self.clock.monotonic())
        live.heartbeat = ready.get("heartbeat") is True
        self.ready_params = dict(ready)
        self._live = live
        live.tasks.append(asyncio.ensure_future(self._read(live)))
        if live.heartbeat:
            live.tasks.append(asyncio.ensure_future(self._beat(live)))
        self._ready.set()
        try:
            answer = await self.request(
                CAPABILITIES_METHOD,
                {"features": self.features, "surface": self.surface},
                before_open=True,
            )
            features = answer.get("features")
            names = cast(list[object], features) if isinstance(features, list) else []
            self.enabled_features = [str(name) for name in names]
            for hook in list(self._hooks):
                await hook(self)
        except BaseException:
            if self._live is live:
                await self._drop_live(live, code=1011, reason="setup failed")
            raise
        if self._live is not live:
            raise TransportLost()
        self._open.set()
        self._tell("ready", epoch=live.epoch)

    async def _handshake(self, transport: Transport, profile_id: str | None) -> dict[str, Any]:
        if transport.subprotocol != SUBPROTOCOL:
            raise DialRefused("protocol_mismatch", "the server did not speak indus-gateway-v1")
        text = await self._timed(transport.recv(), self.limits.ready_timeout)
        if text is None:
            raise DialRefused("ready_timeout", "no gateway.ready in time")
        frame = _parse(text)
        if frame is None or frame.get("method") != READY_METHOD:
            raise DialRefused("no_ready", "the first frame was not gateway.ready")
        params = frame.get("params")
        ready = cast(dict[str, Any], params) if isinstance(params, dict) else {}
        try:
            profile_guard.check_ready(profile_id, ready)  # before any request is sent
        except profile_guard.ProfileMismatch as mismatch:
            raise DialRefused(mismatch.code, str(mismatch), terminal=True) from None
        return ready

    async def _timed[T](self, awaitable: Awaitable[T], seconds: float) -> T | None:
        """``awaitable``'s value, or None when ``seconds`` pass on the channel's clock first."""
        work = asyncio.ensure_future(awaitable)
        timer = asyncio.ensure_future(self.clock.sleep(max(0.0, seconds)))
        try:
            await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
        if work.done():
            return work.result()
        work.cancel()
        with contextlib.suppress(BaseException):
            await work
        return None

    # -- requests ---------------------------------------------------------------------------------

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        before_open: bool = False,
    ) -> dict[str, Any]:
        """Send ``method`` and wait for its result (see the module notes for the errors).
        ``before_open`` is for ``on_ready`` hooks only."""
        budget = self.limits.timeout_for(method) if timeout is None else timeout
        end = self.clock.monotonic() + budget
        live = await self._live_until(end, before_open=before_open)
        rid = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        live.pending[rid] = future
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            frame["params"] = dict(params)
        try:
            try:
                await live.transport.send(json.dumps(frame, ensure_ascii=False))
            except Exception:
                await self._lost(live)
                raise TransportLost() from None
            left = end - self.clock.monotonic()
            answer = await self._timed(asyncio.shield(future), left)
            if answer is None and not future.done():
                raise RequestTimedOut(
                    f"The gateway did not answer {method} in time; the outcome is unknown."
                )
            return cast(dict[str, Any], future.result())
        finally:
            live.pending.pop(rid, None)
            if future.done() and not future.cancelled():
                future.exception()  # retrieved: a lost connection's error is raised above

    async def _live_until(self, end: float, *, before_open: bool) -> _Live:
        while True:
            if self._closed:
                raise NotSent()
            gate = self._ready if before_open else self._open
            if gate.is_set() and self._live is not None:
                return self._live
            if self.state == "failed" and self._reconnect is None:
                raise NotSent()
            left = end - self.clock.monotonic()
            if left <= 0:
                raise NotSent()
            changed = self._changed
            await self._timed(_either(gate, changed), left)

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> bool:
        """Send a notification on the live connection; False when none is up."""
        live = self._live
        if live is None or not self._open.is_set():
            return False
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = dict(params)
        try:
            await live.transport.send(json.dumps(frame, ensure_ascii=False))
        except Exception:
            await self._lost(live)
            return False
        return True

    async def respond(
        self,
        request_id: str | int,
        result: Mapping[str, Any] | None = None,
        *,
        error: tuple[int, str] | None = None,
    ) -> bool:
        """Answer a server→client request on the live connection."""
        live = self._live
        if live is None:
            return False
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            frame["error"] = {"code": error[0], "message": error[1]}
        else:
            frame["result"] = dict(result or {})
        try:
            await live.transport.send(json.dumps(frame, ensure_ascii=False))
        except Exception:
            await self._lost(live)
            return False
        return True

    # -- reading and liveness ---------------------------------------------------------------------

    async def _read(self, live: _Live) -> None:
        try:
            while True:
                text = await live.transport.recv()
                if self._live is not live:
                    return  # a replaced socket: whatever it still says is ignored
                live.last_inbound = self.clock.monotonic()
                await self._take(live, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await self._lost(live)

    async def _take(self, live: _Live, text: str) -> None:
        frame = _parse(text)
        if frame is None:
            log.debug("dropped a frame that is not a JSON object")
            return
        method = frame.get("method")
        if isinstance(method, str):
            params = frame.get("params")
            body = cast(dict[str, Any], params) if isinstance(params, dict) else {}
            if "id" in frame and frame.get("id") is not None:
                await self._server_call(frame.get("id"), method, body)
                return
            for listener in list(self._notifications):
                try:
                    result = listener(method, body)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    log.exception("a notification listener failed on %s", method)
            return
        rid = frame.get("id")
        if not isinstance(rid, int) or isinstance(rid, bool):
            return
        if rid in live.pings:
            live.pings.discard(rid)
            return
        future = live.pending.get(rid)
        if future is None or future.done():
            return
        if "error" in frame:
            future.set_exception(_rpc_error(frame.get("error")))
        else:
            result = frame.get("result")
            future.set_result(result if isinstance(result, dict) else {})

    async def _server_call(self, request_id: object, method: str, params: dict[str, Any]) -> None:
        if not isinstance(request_id, str | int) or isinstance(request_id, bool):
            return
        for handler in list(self._calls):
            try:
                if handler(request_id, method, params):
                    return
            except Exception:
                log.exception("a server request handler failed on %s", method)
        await self.respond(request_id, error=(METHOD_NOT_FOUND.code, "not handled by this client"))

    async def _beat(self, live: _Live) -> None:
        limits = self.limits
        next_ping = self.clock.monotonic() + limits.ping_interval
        while self._live is live:
            now = self.clock.monotonic()
            dead_at = live.last_inbound + limits.liveness_deadline
            if now >= dead_at:
                log.warning(
                    "no frame from the gateway for %.0f s; reconnecting", now - live.last_inbound
                )
                await self._lost(live, code=1001, reason="no heartbeat")
                return
            if now >= next_ping:
                if len(live.pings) < limits.max_pings:
                    rid = next(self._ids)
                    live.pings.add(rid)
                    frame = {"jsonrpc": "2.0", "id": rid, "method": PING_METHOD}
                    try:
                        await live.transport.send(json.dumps(frame))
                    except Exception:
                        await self._lost(live)
                        return
                next_ping = now + limits.ping_interval
            await self.clock.sleep(max(0.0, min(dead_at, next_ping) - now))

    @property
    def outstanding_pings(self) -> int:
        live = self._live
        return len(live.pings) if live is not None else 0

    # -- loss and reconnect -----------------------------------------------------------------------

    async def drop(self, *, code: int = 1001, reason: str = "dropped") -> None:
        """Close the current socket as if it were lost (tests and forced reconnects)."""
        live = self._live
        if live is not None:
            await self._lost(live, code=code, reason=reason)

    async def _lost(self, live: _Live, *, code: int = 1006, reason: str = "lost") -> None:
        if live.gone or self._live is not live:
            return  # identity gate: a replaced socket's close changes nothing
        await self._drop_live(live, code=code, reason=reason)
        if self._closed:
            return
        self._tell("reconnecting", epoch=live.epoch)
        if self._reconnect is None:
            self._reconnect = asyncio.ensure_future(self._redial())

    async def _drop_live(self, live: _Live, *, code: int, reason: str) -> None:
        live.gone = True
        if self._live is live:
            self._live = None
            self._open.clear()
            self._ready.clear()
        for future in live.pending.values():
            if not future.done():
                future.set_exception(TransportLost())
        me = asyncio.current_task()
        for task in live.tasks:
            if task is not me:
                task.cancel()
        with contextlib.suppress(Exception):
            await live.transport.close(code, reason)

    async def _redial(self) -> None:
        attempt = 0
        try:
            while not self._closed:
                attempt += 1
                delay = self.limits.backoff(attempt)
                self._tell("reconnecting", attempt=attempt, delay=delay)
                await self.clock.sleep(delay)
                if self._closed:
                    return
                try:
                    await self._connect()
                except DialRefused as refused:
                    if refused.terminal:
                        self.failure = refused
                        self._reconnect = None
                        self._tell("failed", reason=refused.reason)
                        return
                    log.info("reconnect attempt %d failed: %s", attempt, refused.reason)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.info("reconnect attempt %d failed: %s", attempt, type(exc).__name__)
                    continue
                return
        finally:
            if self._reconnect is asyncio.current_task():
                self._reconnect = None


async def _either(first: asyncio.Event, second: asyncio.Event) -> None:
    waits = {asyncio.ensure_future(first.wait()), asyncio.ensure_future(second.wait())}
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waits:
            task.cancel()


def _parse(text: str) -> dict[str, Any] | None:
    try:
        value: object = json.loads(text)
    except ValueError:
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None
