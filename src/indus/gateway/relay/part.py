"""The relay connector inside the gateway daemon (P24-08/09 wiring, wave 42).

``RelayPart`` (``Stage.BACKGROUND``; ``PART_FACTORIES`` names ``create``, which skips it when
``gateway.relay.url`` is unset) runs the profile's relay front:

1. ``build_front`` (settings from ``gateway.relay``, the secret ``INDUS_RELAY_SECRET``), then the
   transport starts: dial out, signed hello, one descriptor per fronted platform. A first dial that
   fails for a retryable reason is tried again with the transport's backoff (1 s doubling to
   60 s) until it works or the daemon stops; a configuration problem (no secret, a bad URL, a
   refused hello) is reported once and not retried. The status row ``relay`` says which.
2. one relay adapter per descriptor (``create_relay_adapter``), connected through the daemon
   (``GatewayDaemon.attach_relayed``): intake, outbound sender, status row and platform listeners
   exactly like a native adapter, and the delivery router attaches it ``via_relay`` (P24-09: an
   enabled native adapter wins, so a platform whose native adapter is enabled is not taken over —
   logged — while an explicitly disabled one never hides the relay).
3. ``on_interrupt`` (the connector's interrupt frame: the person pressed stop on the platform)
   stops the turn running for that chat: the session key the frame names, else every running turn
   of that platform and chat, in this profile or, on a multiplexing host, any served profile.

At stop, when the daemon is leaving because it was idle (P17-23 ``idle_exit``), the transport first
asks the connector to go dormant (it buffers our events and replays them to the next start);
otherwise the socket simply closes when the last relay adapter disconnects. A platform served only
through the relay does not keep an auto-started daemon from idling out (``idle_exit``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from indus.gateway.daemon.idle_exit import IDLE_EXIT_REASON
from indus.gateway.daemon.parts import DaemonPart, Stage
from indus.gateway.relay import build_front, relay_settings
from indus.gateway.relay.adapter import RelayFront, create_relay_adapter
from indus.gateway.relay.transport import RelayConnectError, SocketOpener, backoff_delays

if TYPE_CHECKING:
    from indus.gateway.daemon.process import GatewayDaemon

__all__ = ["RelayPart", "create"]

log = logging.getLogger(__name__)

_SURFACE = "relay"


def _key_matches(key: str, platform: str, chat_id: str) -> bool:
    """Does the session key ``<ns>:<platform>:<chat_type>:…`` name this platform and chat?"""
    parts = key.split(":")
    return len(parts) >= 3 and parts[1] == platform and chat_id in parts[3:]


class RelayPart(DaemonPart):
    """See the module notes."""

    name = "relay"
    stage = Stage.BACKGROUND

    def __init__(self, *, opener: SocketOpener | None = None) -> None:
        self.opener = opener
        self.front: RelayFront | None = None
        self.platforms: list[str] = []
        self._daemon: GatewayDaemon | None = None
        self._dial: asyncio.Task[None] | None = None
        self._holding = False

    # -- start -----------------------------------------------------------------------------------

    async def start(self, daemon: GatewayDaemon) -> None:
        self._daemon = daemon
        front = build_front(daemon.scope, daemon.clock, opener=self.opener)
        if front is None:
            return
        front.on_interrupt = self._interrupt
        front.hold()  # replayed events wait for their adapters
        self.front = front
        try:
            await self._acquire()
        except RelayConnectError as exc:
            self._report(problem=str(exc), retrying=exc.retryable)
            if exc.retryable:
                self._dial = asyncio.ensure_future(self._keep_dialling())
            return
        await self._serve_platforms()

    async def _acquire(self) -> None:
        front = self.front
        assert front is not None
        await front.transport.acquire()  # the part holds the transport while it runs
        self._holding = True

    async def _keep_dialling(self) -> None:
        daemon = self._daemon
        assert daemon is not None
        for delay in backoff_delays():
            await daemon.clock.sleep(delay)
            if daemon.lifecycle.stopping:
                return
            try:
                await self._acquire()
            except RelayConnectError as exc:
                if not exc.retryable:
                    self._report(problem=str(exc), retrying=False)
                    return
                continue
            await self._serve_platforms()
            return

    async def _serve_platforms(self) -> None:
        daemon, front = self._daemon, self.front
        assert daemon is not None and front is not None
        settings = relay_settings(daemon.scope)
        timeout = settings.connect_timeout if settings is not None else 30.0
        served: list[str] = []
        for name, descriptor in sorted(front.transport.descriptors.items()):
            if daemon.native_state(name) is True:
                log.info(
                    "relay: %s keeps its own (native) adapter; the relay does not take it", name
                )
                continue
            adapter = create_relay_adapter(
                front, descriptor, clock=daemon.clock, settings=daemon.settings.get(name)
            )
            if await daemon.attach_relayed(name, adapter, timeout=timeout):
                served.append(name)
        self.platforms = served
        front.release()
        self._report(problem=None, retrying=False)
        log.info("relay: serving %s", ", ".join(served) or "no platform")

    def _report(self, *, problem: str | None, retrying: bool) -> None:
        daemon = self._daemon
        if daemon is None:
            return
        row: dict[str, Any] = {"platforms": list(self.platforms)}
        if problem is not None:
            row["problem"] = problem
            row["retrying"] = retrying
            log.warning("relay: %s%s", problem, " (trying again)" if retrying else "")
        front = self.front
        if front is not None:
            row["state"] = front.transport.state
        daemon.status.set_surface(_SURFACE, row)

    # -- interrupts ------------------------------------------------------------------------------

    async def _interrupt(self, platform: str, chat_id: str, session_key: str | None) -> None:
        """The connector says the person stopped the turn in this chat."""
        daemon = self._daemon
        if daemon is None:
            return
        mux = daemon.mux
        daemons = list(mux.daemons.values()) if mux is not None else [daemon]
        stopped = 0
        for serving in daemons:
            pipeline = getattr(serving, "pipeline", None)
            if pipeline is None:
                continue
            for key in list(pipeline.claims.keys()):
                if session_key is not None and key != session_key:
                    continue
                if session_key is None and not _key_matches(key, platform, chat_id):
                    continue
                claim = pipeline.claims.current(key)
                if claim is None:
                    continue
                with contextlib.suppress(Exception):
                    await pipeline.interrupt_claim(claim)
                    stopped += 1
        log.info("relay: interrupt for %s %s stopped %d turn(s)", platform, chat_id, stopped)

    # -- stop ------------------------------------------------------------------------------------

    async def stop(self, daemon: GatewayDaemon) -> None:
        dial, self._dial = self._dial, None
        if dial is not None:
            dial.cancel()
            with contextlib.suppress(BaseException):
                await dial
        front = self.front
        if front is None:
            return
        front.release()
        transport = front.transport
        if daemon.lifecycle.reason == IDLE_EXIT_REASON and transport.live:
            try:
                dormant = await transport.go_dormant()
            except Exception:
                dormant = False
            log.info(
                "relay: %s",
                "the connector keeps our events until the next start"
                if dormant
                else "the connector did not confirm going dormant",
            )
        for name in self.platforms:
            if daemon.adapters.get(name) is not None:
                await daemon.disconnect_platform(name, fatal=False, reason="stopping")
            daemon.relayed.discard(name)
        self.platforms = []
        if self._holding:
            self._holding = False
            with contextlib.suppress(Exception):
                await transport.release()
        self.front = None


def create(daemon: GatewayDaemon) -> RelayPart | None:
    """``PART_FACTORIES`` entry (None without ``gateway.relay.url``)."""
    return RelayPart() if relay_settings(daemon.scope) is not None else None
