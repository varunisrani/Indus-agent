"""The browser-control broker: one external controller per identity, exact routing (P24-47).

A browser controller (for example a browser extension that drives the user's own browser)
registers through the API server, then holds a WebSocket; callers on any transport reach it
through ``BrowserBroker.call``. Everything the broker decides:

Identity. ``Identity`` has six attributes: transport family, principal, browser profile,
controller id, session and profile. The register route fills the transport family (``api``), the
principal (the API key id) and the profile (this daemon's profile id) itself; only the controller
id, the browser profile and the session come from the controller. A call is routed only to the
connected controller whose identity equals the caller's on all six and whose negotiated set holds
the capability; anything else is ``ControllerUnavailable``, never another controller.

Tickets. ``register`` mints a ticket in the shared ``TicketBook`` for the ``browser-controller``
purpose (30 s, single use; ``service/ws_auth.py``) and remembers only its SHA-256 digest with the
identity. ``admit`` decides the socket upgrade with the one helper, ``authenticate_upgrade``: the
peer must be loopback, an Origin must be allow-listed, exactly one
``indus-browser-control-ticket.<ticket>`` must be offered beside ``indus-browser-control-v1``, a
ticket in the query is spent and refused. Reused, expired, unknown or other-purpose tickets close
4401. Only the base protocol is echoed. No ticket value is logged, formatted or put in a URL.

Capabilities. Allowed: ``noop``, ``back``, ``click``, ``navigate``, ``press``, ``screenshot``,
``scroll``, ``snapshot``, ``tab_activate``, ``tabs``, ``type``, plus ``artifact_download`` and
``artifact_upload`` (whose only argument is a server-minted artefact id that must be live for the
caller's scope key). ``cdp`` and ``evaluate`` are developer capabilities: kept at negotiation only
while developer mode is on, and refused at call time whenever developer mode is off, even when
negotiated. Anything else offered is dropped; anything else asked is ``CapabilityRefused``.

Commands. Each command has a fresh id, waits at most ``COMMAND_TIMEOUT_S`` (30 s) on the injected
clock and completes at most once: a second answer, or an answer from another identity, is
ignored. ``detach`` (the controller said goodbye, or a new controller took its lane) fails every
outstanding command with ``ControllerCancelled``; ``disconnect`` (the socket dropped) only marks
the controller offline: its commands keep waiting for a reconnect of the same identity or their
timeout, and new calls are refused until it is back. A cancel meant for an offline controller is
kept for its reconnect, at most ``MAX_DEFERRED_CANCELS`` (512, newest kept), and sent first when
it attaches again. Wire protocol version 1.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any, Final, cast

from indus.foundation.clock import Clock
from indus.gateway.browser_broker.artifacts import ArtifactRefused, ArtifactStore
from indus.service.ws_auth import (
    BROWSER_CONTROLLER,
    CLOSE_BAD_TICKET,
    TicketBook,
    TicketRefused,
    UpgradeRefused,
    authenticate_upgrade,
    offered_tickets,
)

__all__ = [
    "ALLOWED_CAPABILITIES",
    "ARTIFACT_CAPABILITIES",
    "COMMAND_TIMEOUT_S",
    "DEVELOPER_CAPABILITIES",
    "FRAME_CANCEL",
    "FRAME_COMMAND",
    "MAX_DEFERRED_CANCELS",
    "PROTOCOL_VERSION",
    "SUBPROTOCOL",
    "TICKET_PREFIX",
    "BrokerError",
    "BrowserBroker",
    "CapabilityRefused",
    "ControllerCancelled",
    "ControllerRejected",
    "ControllerTimeout",
    "ControllerUnavailable",
    "Identity",
    "Registration",
    "RegistrationRefused",
    "negotiate",
    "protocol_supported",
]

log = logging.getLogger(__name__)

PROTOCOL_VERSION: Final = 1
SUBPROTOCOL: Final = "indus-browser-control-v1"
TICKET_PREFIX: Final = "indus-browser-control-ticket."
COMMAND_TIMEOUT_S: Final = 30.0
MAX_DEFERRED_CANCELS: Final = 512

ALLOWED_CAPABILITIES: frozenset[str] = frozenset(
    {"noop", "back", "click", "navigate", "press", "screenshot", "scroll", "snapshot",
     "tab_activate", "tabs", "type"}
)  # fmt: skip
ARTIFACT_CAPABILITIES: frozenset[str] = frozenset({"artifact_download", "artifact_upload"})
DEVELOPER_CAPABILITIES: frozenset[str] = frozenset({"cdp", "evaluate"})
_KNOWN = ALLOWED_CAPABILITIES | ARTIFACT_CAPABILITIES | DEVELOPER_CAPABILITIES

FRAME_HELLO: Final = "browser.controller.hello"
FRAME_READY: Final = "browser.controller.ready"
FRAME_COMMAND: Final = "browser.controller.command"
FRAME_CANCEL: Final = "browser.controller.cancel"
FRAME_RESULT: Final = "browser.controller.result"
FRAME_DETACH: Final = "browser.controller.detach"

_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")

type Send = Callable[[dict[str, Any]], Awaitable[None]]


class BrokerError(Exception):
    """A broker refusal; the message is short and never carries a ticket or arguments."""


class RegistrationRefused(BrokerError):
    """The register request is malformed or the feature is off."""


class ControllerUnavailable(BrokerError):
    """No connected controller matches the identity and holds the capability."""


class CapabilityRefused(BrokerError):
    """The capability is not on the allow-list, or needs developer mode which is off."""


class ControllerCancelled(BrokerError):
    """The command was cancelled (by the caller, or because the controller detached)."""


class ControllerTimeout(BrokerError):
    """The controller did not answer in time."""


class ControllerRejected(BrokerError):
    """The controller answered ``ok: false``, or an artefact reference was refused."""


def protocol_supported(value: object) -> bool:
    """Exactly the int 1 (a bool is not an int here)."""
    return type(value) is int and value == PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class Identity:
    """Who a controller is and who may call it; equality is over all six attributes."""

    transport_family: str
    principal: str
    browser_profile: str
    controller_id: str
    session: str
    profile: str

    def scope_key(self) -> str:
        """A stable digest of the identity (the artefact store's scope key)."""
        joined = "\0".join(getattr(self, f.name) for f in fields(self))
        return hashlib.sha256(b"indus-browser-control\0" + joined.encode("utf-8")).hexdigest()

    def lane(self) -> tuple[str, str, str, str]:
        """The session lane: one controller per (principal, profile, session, transport)."""
        return (self.principal, self.profile, self.session, self.transport_family)


def check_part(name: str, value: object) -> str:
    if not isinstance(value, str) or not _PART.match(value):
        raise RegistrationRefused(f"{name} must be a short id (letters, digits, . _ : @ -)")
    return value


def negotiate(offered: object, *, developer_mode: bool) -> frozenset[str]:
    """The allowed subset of what a controller offers (a non-list offers nothing)."""
    if not isinstance(offered, list | tuple):
        return frozenset()
    allowed = ALLOWED_CAPABILITIES | ARTIFACT_CAPABILITIES
    if developer_mode:
        allowed |= DEVELOPER_CAPABILITIES
    return frozenset(
        c for c in cast(Sequence[object], offered) if isinstance(c, str) and c in allowed
    )


@dataclass(frozen=True, slots=True)
class Registration:
    """What the register route answers. ``ticket`` is the secret: body only, never logged."""

    ticket: str = field(repr=False)
    expires_in: float

    def body(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "expires_in": self.expires_in,
            "subprotocol": SUBPROTOCOL,
            "ticket_prefix": TICKET_PREFIX,
            "protocol": PROTOCOL_VERSION,
        }


@dataclass
class _Controller:
    identity: Identity
    capabilities: frozenset[str]
    send: Send
    owner: object
    connected: bool = True
    deferred: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _Pending:
    command_id: str
    identity: Identity
    call_id: str | None
    future: asyncio.Future[tuple[bool, Any]]


def _digest(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8", errors="surrogatepass")).hexdigest()


def _cancel_frame(pending: _Pending) -> dict[str, Any]:
    return {
        "method": FRAME_CANCEL,
        "params": {"command_id": pending.command_id, "call_id": pending.call_id},
    }


class BrowserBroker:
    """The broker of one daemon (see the module notes). Runs on the daemon's event loop."""

    def __init__(
        self,
        book: TicketBook,
        *,
        clock: Clock,
        developer_mode: Callable[[], bool] = lambda: False,
        enabled: Callable[[], bool] = lambda: True,
        artifacts: ArtifactStore | None = None,
        command_timeout: float = COMMAND_TIMEOUT_S,
    ) -> None:
        if BROWSER_CONTROLLER not in book.purposes:
            raise ValueError("the ticket book does not mint browser-controller tickets")
        self.book = book
        self.clock = clock
        self.developer_mode = developer_mode
        self.enabled = enabled
        self.artifacts = artifacts
        self.command_timeout = command_timeout
        self._bound: dict[str, tuple[Identity, float]] = {}
        self._controllers: dict[Identity, _Controller] = {}
        self._pending: dict[str, _Pending] = {}

    # -- registration and the upgrade -------------------------------------------------------------

    def _developer(self) -> bool:
        try:
            return self.developer_mode() is True
        except Exception:
            log.warning("the developer-mode switch could not be read; treating it as off")
            return False

    def _prune_bound(self) -> None:
        now = self.clock.monotonic()
        for key in [k for k, (_, until) in self._bound.items() if until <= now]:
            del self._bound[key]

    def register(self, identity: Identity) -> Registration:
        """Mint a ticket bound to ``identity`` (30 s, single use)."""
        if not self.enabled():
            raise RegistrationRefused("browser control is turned off")
        self._prune_bound()
        try:
            minted = self.book.mint(BROWSER_CONTROLLER)
        except TicketRefused as refused:
            raise RegistrationRefused(f"no ticket: {refused.reason}") from None
        self._bound[_digest(minted.ticket)] = (identity, self.clock.monotonic() + minted.expires_in)
        log.info("browser controller %s registered; ticket expires in %.0f s",
                 identity.controller_id, minted.expires_in)  # fmt: skip
        return Registration(ticket=minted.ticket, expires_in=minted.expires_in)

    def admit(
        self, scope: Mapping[str, Any], *, origins: Sequence[str] = ()
    ) -> Identity | UpgradeRefused:
        """Decide one controller socket upgrade (see the module notes)."""
        raw: object = scope.get("subprotocols") or ()
        offered = list(cast(Sequence[object], raw)) if isinstance(raw, list | tuple) else []
        tickets = offered_tickets(offered, TICKET_PREFIX)
        decided = authenticate_upgrade(
            scope,
            BROWSER_CONTROLLER,
            book=self.book,
            origins=origins,
            subprotocol=SUBPROTOCOL,
            ticket_prefix=TICKET_PREFIX,
        )
        bound = [self._bound.pop(_digest(t), None) for t in tickets]
        if isinstance(decided, UpgradeRefused):
            return decided
        found = bound[0] if bound else None
        if not self.enabled():
            return UpgradeRefused(CLOSE_BAD_TICKET, "browser control is turned off")
        if found is None or found[1] <= self.clock.monotonic():
            return UpgradeRefused(CLOSE_BAD_TICKET, "ticket unknown")
        return found[0]

    # -- controllers ------------------------------------------------------------------------------

    async def attach(
        self, identity: Identity, offered: object, send: Send, *, owner: object
    ) -> frozenset[str]:
        """Attach (or re-attach) the controller of ``identity``; the negotiated capabilities.

        A reconnect of the same identity keeps its outstanding commands and first gets the
        cancels kept for it; another controller in the same lane replaces the old one, whose
        commands fail."""
        capabilities = negotiate(offered, developer_mode=self._developer())
        for other in [
            i for i in self._controllers if i != identity and i.lane() == identity.lane()
        ]:
            await self.detach(other)
        existing = self._controllers.get(identity)
        if existing is None:
            self._controllers[identity] = _Controller(identity, capabilities, send, owner)
            return capabilities
        async with existing.lock:
            existing.connected = False
            existing.capabilities, existing.send, existing.owner = capabilities, send, owner
            deferred, existing.deferred = existing.deferred, []
            for index, frame in enumerate(deferred):
                try:
                    await send(frame)
                except Exception:
                    existing.deferred = deferred[index:][-MAX_DEFERRED_CANCELS:]
                    existing.owner = None
                    raise ControllerUnavailable("the kept cancels could not be sent") from None
            existing.connected = True
        return capabilities

    def disconnect(self, identity: Identity, *, owner: object) -> bool:
        """The socket of ``owner`` dropped: mark its controller offline (work keeps waiting)."""
        controller = self._controllers.get(identity)
        if controller is None or controller.owner is not owner:
            return False
        controller.connected = False
        controller.owner = None
        return True

    async def detach(self, identity: Identity, *, owner: object | None = None) -> int:
        """Remove the controller and fail its outstanding commands; how many failed."""
        controller = self._controllers.get(identity)
        if controller is None or (owner is not None and controller.owner is not owner):
            return 0
        del self._controllers[identity]
        failed = [p for p in self._pending.values() if p.identity == identity]
        for pending in failed:
            self._resolve(pending, ControllerCancelled("the controller detached"))
        if controller.connected:
            async with controller.lock:
                for pending in failed:
                    with contextlib.suppress(Exception):
                        await controller.send(_cancel_frame(pending))
        return len(failed)

    def lane(self, *, session: str, principal: str, transport_family: str) -> Identity | None:
        """The one controller identity bound to a caller's session lane, or None."""
        found = [
            i
            for i in self._controllers
            if (i.session, i.principal, i.transport_family)
            == (session, principal, transport_family)
        ]
        return found[0] if len(found) == 1 else None

    def is_connected(self, identity: Identity) -> bool:
        controller = self._controllers.get(identity)
        return controller is not None and controller.connected

    def deferred_cancels(self, identity: Identity) -> int:
        controller = self._controllers.get(identity)
        return len(controller.deferred) if controller is not None else 0

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # -- commands ---------------------------------------------------------------------------------

    def _resolve(self, pending: _Pending, outcome: tuple[bool, Any] | BaseException) -> None:
        self._pending.pop(pending.command_id, None)
        if pending.future.done():
            return
        if isinstance(outcome, BaseException):
            pending.future.set_exception(outcome)
        else:
            pending.future.set_result(outcome)

    def _check_artifact(
        self, identity: Identity, capability: str, arguments: Mapping[str, Any]
    ) -> None:
        if set(arguments) != {"artifact_id"}:
            raise ControllerRejected(f"{capability} takes exactly one argument, artifact_id")
        if self.artifacts is None:
            raise ControllerRejected(f"{capability} needs the artefact store")
        try:
            self.artifacts.check(arguments["artifact_id"], identity.scope_key())
        except ArtifactRefused as refused:
            raise ControllerRejected(f"{capability}: artefact refused ({refused.reason})") from None

    async def call(
        self,
        identity: Identity,
        capability: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        call_id: str | None = None,
    ) -> Any:
        """Run one command on the controller of exactly ``identity``; its result."""
        if capability not in _KNOWN:
            raise CapabilityRefused(f"{capability!r} is not a browser-control capability")
        if capability in DEVELOPER_CAPABILITIES and not self._developer():
            raise CapabilityRefused(f"{capability} needs developer mode")
        controller = self._controllers.get(identity)
        if (
            controller is None
            or not controller.connected
            or capability not in controller.capabilities
        ):
            raise ControllerUnavailable(
                f"no connected controller for this identity with {capability}"
            )
        args = dict(arguments or {})
        if capability in ARTIFACT_CAPABILITIES:
            self._check_artifact(identity, capability, args)
        loop = asyncio.get_running_loop()
        pending = _Pending(secrets.token_hex(16), identity, call_id, loop.create_future())
        frame = {
            "method": FRAME_COMMAND,
            "params": {"command_id": pending.command_id, "capability": capability,
                       "arguments": args, "call_id": call_id},
        }  # fmt: skip
        async with controller.lock:
            if self._controllers.get(identity) is not controller or not controller.connected:
                raise ControllerUnavailable("the controller went away before the command was sent")
            self._pending[pending.command_id] = pending
            try:
                await controller.send(frame)
            except Exception:
                self._pending.pop(pending.command_id, None)
                raise ControllerUnavailable("the command could not be sent") from None
        return await self._wait(pending)

    async def _wait(self, pending: _Pending) -> Any:
        timer = asyncio.ensure_future(self.clock.sleep(self.command_timeout))
        try:
            await asyncio.wait({pending.future, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer
        if not pending.future.done():
            self._resolve(pending, ControllerTimeout("no answer within the command timeout"))
            await self._send_cancel(pending)
        ok, result = await pending.future
        if not ok:
            raise ControllerRejected("the controller refused the command")
        return result

    async def _send_cancel(self, pending: _Pending) -> None:
        controller = self._controllers.get(pending.identity)
        if controller is None:
            return
        frame = _cancel_frame(pending)
        if not controller.connected:
            controller.deferred.append(frame)
            del controller.deferred[:-MAX_DEFERRED_CANCELS]
            return
        async with controller.lock:
            with contextlib.suppress(Exception):
                await controller.send(frame)

    def complete(
        self, command_id: object, identity: Identity, *, ok: bool, result: Any = None
    ) -> bool:
        """Settle one command (from its own controller only); False when nothing was waiting."""
        if not isinstance(command_id, str):
            return False
        pending = self._pending.get(command_id)
        if pending is None or pending.identity != identity or pending.future.done():
            return False
        self._resolve(pending, (ok is True, result))
        return True

    async def cancel(self, identity: Identity, call_id: str) -> bool:
        """Cancel the caller's outstanding command ``call_id`` (kept for an offline controller)."""
        target = next(
            (p for p in self._pending.values() if p.identity == identity and p.call_id == call_id),
            None,
        )
        if target is None:
            return False
        self._resolve(target, ControllerCancelled("the command was cancelled"))
        await self._send_cancel(target)
        return True

    async def aclose(self) -> None:
        """Detach every controller and fail what is left (daemon stop)."""
        for identity in list(self._controllers):
            await self.detach(identity)
        for pending in list(self._pending.values()):
            self._resolve(pending, ControllerCancelled("the broker stopped"))
        self._bound.clear()
        if self.artifacts is not None:
            self.artifacts.clear()
