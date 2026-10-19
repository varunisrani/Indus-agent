"""The delivery ledger and the platform deliverers in the gateway daemon (P15-19, P15-20).

Two daemon parts (``PART_FACTORIES`` names ``ledger_part`` and ``router_part``):

- ``LedgerPart`` (``Stage.RECOVERY``; absent when ``gateway.delivery_ledger`` is false). In
  ``prepare`` it hands the ledger to every platform's outbound sender (``sender_options``), binds
  redeliveries to the daemon's live senders and takes over boot recovery's owed replies
  (``recovery.crash_reply_sink``): a reply the previous process persisted is recorded in the
  ledger and sent with the recovered label, unless the ledger already holds a row for that turn
  (then the ledger's own sweep sent it, and it is never sent twice). ``start`` is the boot sweep
  (inside the startup-restore window, before boot recovery); a platform that comes back after a
  disconnect gets its reconnect-only rows sent again; ``stop`` cancels the retry timer.
- ``RouterPart`` (``Stage.BACKGROUND``): the delivery router registers a P14 deliverer for each
  platform that connects (and ``all`` while any is attached) and removes it when the platform goes
  down; the channel directory is rebuilt at start and every 5 minutes; mirror notes reach the
  conversations that live in a chat something was delivered to.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from indus.foundation.blocking import run_blocking
from indus.gateway.daemon.parts import DaemonPart, Stage
from indus.gateway.delivery import directory as directory_mod
from indus.gateway.delivery.directory import ChannelDirectory
from indus.gateway.delivery.ledger import DeliveryLedger
from indus.gateway.delivery.mirror import Mirror
from indus.gateway.delivery.router import DeliveryRouter
from indus.state import delivery_ledger as table

if TYPE_CHECKING:
    import asyncio

    from indus.gateway.adapters.base import Adapter
    from indus.gateway.config import HomeChannel
    from indus.gateway.core.events import Source
    from indus.gateway.daemon.process import GatewayDaemon
    from indus.state.gateway_routes import RouteEntry

__all__ = ["LedgerPart", "RouterPart", "ledger_part", "router_part"]

log = logging.getLogger(__name__)


class LedgerPart(DaemonPart):
    """See the module notes."""

    name = "delivery-ledger"
    stage = Stage.RECOVERY

    def __init__(self, ledger: DeliveryLedger) -> None:
        self.ledger = ledger
        self._daemon: GatewayDaemon | None = None
        self._seen: set[str] = set()
        self._undo: list[Callable[[], None]] = []

    async def prepare(self, daemon: GatewayDaemon) -> None:
        self._daemon = daemon
        daemon.sender_options["ledger"] = self.ledger
        self.ledger.bind(daemon.replies.sender_for)
        daemon.recovery.crash_reply_sink = self._crash_reply
        self._undo.append(daemon.on_platform_up(self._up))

    def _up(self, name: str, _adapter: Adapter) -> None:
        daemon = self._daemon
        if name in self._seen and daemon is not None:
            daemon.spawn(self.ledger.platform_reconnected(name), name=f"ledger reconnect {name}")
        self._seen.add(name)

    async def _crash_reply(self, entry: RouteEntry, source: Source, text: str) -> None:
        from indus.gateway.daemon.recovery import RECOVERED_LABEL

        daemon = self._daemon
        assert daemon is not None
        started = entry.active_turn_started_at or 0.0
        try:
            rows = await run_blocking(
                functools.partial(table.rows_for_session_key, daemon.store, entry.session_key),
                label="delivery ledger",
            )
        except Exception:
            log.warning("delivery ledger unreadable; sending the recovered reply", exc_info=True)
            rows = []
        if any(row.created_at >= started for row in rows):
            log.info("the reply owed to %s is in the delivery ledger already", entry.session_key)
            return
        await daemon.pipeline.reply(
            source,
            f"{RECOVERED_LABEL}\n\n{text}",
            kind="final",
            session_key=entry.session_key,
            message_ref=f"{entry.session_id}:recovered:{entry.active_turn_token or ''}",
        )

    async def start(self, daemon: GatewayDaemon) -> None:
        report = await self.ledger.recover()
        if report.redelivered or report.abandoned or report.adopted:
            log.info(
                "delivery ledger: %d sent again (%d labelled), %d adopted, %d abandoned",
                report.redelivered,
                report.labelled,
                report.adopted,
                report.abandoned,
            )

    async def stop(self, daemon: GatewayDaemon) -> None:
        for undo in self._undo:
            undo()
        self._undo.clear()
        await self.ledger.aclose()


class RouterPart(DaemonPart):
    """See the module notes."""

    name = "delivery-router"
    stage = Stage.BACKGROUND

    def __init__(self) -> None:
        self.router: DeliveryRouter | None = None
        self.directory: ChannelDirectory | None = None
        self._task: asyncio.Task[None] | None = None
        self._undo: list[Callable[[], None]] = []

    async def prepare(self, daemon: GatewayDaemon) -> None:
        def home(name: str) -> HomeChannel | None:
            found = daemon.settings.get(name)
            return found.home_channel if found is not None else None

        self.directory = await run_blocking(
            ChannelDirectory(daemon.scope).load, label="channel directory"
        )
        self.router = DeliveryRouter(
            daemon.scope,
            homes=home,
            directory=self.directory,
            mirror=Mirror(daemon.store, daemon.service),
        )
        router = self.router

        def up(name: str, _adapter: Adapter) -> None:
            sender = daemon.senders.get(name)
            if sender is not None:
                router.attach(name, sender)

        def down(name: str, _fatal: bool, _reason: str) -> None:
            router.detach(name)  # its sender is gone with the adapter; up attaches it again

        self._undo += [daemon.on_platform_up(up), daemon.on_platform_down(down)]

    async def start(self, daemon: GatewayDaemon) -> None:
        if self.directory is not None:
            self._task = self.directory.start(lambda: daemon.adapters, daemon.store)

    async def stop(self, daemon: GatewayDaemon) -> None:
        for undo in self._undo:
            undo()
        self._undo.clear()
        task, self._task = self._task, None
        await directory_mod.stop(task)
        if self.router is not None:
            self.router.close()


def ledger_part(daemon: GatewayDaemon) -> LedgerPart | None:
    """``PART_FACTORIES`` entry (None when ``gateway.delivery_ledger`` is false)."""
    ledger = DeliveryLedger.for_scope(daemon.scope, daemon.store)
    return None if ledger is None else LedgerPart(ledger)


def router_part(_daemon: GatewayDaemon) -> RouterPart:
    """``PART_FACTORIES`` entry."""
    return RouterPart()
