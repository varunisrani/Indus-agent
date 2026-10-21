"""The task boards inside the gateway daemon (P24-12 wiring).

``BoardPart`` (a ``Stage.BACKGROUND`` part named in ``PART_FACTORIES``) runs, when
``kanban.dispatch_in_gateway`` is on (the default), one ``BoardWatchers`` loop per board under the
root: a dispatcher pass every ``kanban.dispatch_interval_seconds`` (workers spawned as their own
``indus`` processes), task notices to the chats subscribed to a task, hourly upkeep. Notices go
out through the daemon's delivery router (the platform's sender, its dead-chat list, the ledger).
Boards are listed once at start and only boards whose file exists are watched (a profile that never
used a board gets no loop and no file); a board created later is picked up at the next start
(``indus kanban dispatch`` runs a pass by hand meanwhile).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from indus.board.dispatcher import Dispatcher, DispatchSettings
from indus.board.watchers import BoardWatchers
from indus.cron.targets import DeliveryTarget
from indus.foundation.blocking import run_blocking
from indus.foundation.config import loader as config_loader
from indus.gateway.daemon.parts import DaemonPart, Stage
from indus.state.board import BoardStore, board_path, list_boards

if TYPE_CHECKING:
    from indus.gateway.daemon.process import GatewayDaemon
    from indus.gateway.delivery.router import DeliveryRouter

__all__ = ["BoardPart", "create", "dispatch_enabled"]

log = logging.getLogger(__name__)

_LABEL = "a task board notice"


def _setting(view: Any, key: str, default: Any) -> Any:
    try:
        value = view.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def dispatch_enabled(view: Any) -> bool:
    return _setting(view, "kanban.dispatch_in_gateway", True) is True


class BoardPart(DaemonPart):
    name = "boards"
    stage = Stage.BACKGROUND

    def __init__(self) -> None:
        self._stores: list[BoardStore] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._daemon: GatewayDaemon | None = None

    def _router(self) -> DeliveryRouter | None:
        from indus.gateway.delivery.parts import RouterPart

        for part in getattr(self._daemon, "parts", ()):
            if isinstance(part, RouterPart):
                return part.router
        return None

    async def _deliver(self, target: Mapping[str, str | None], text: str) -> bool:
        router = self._router()
        platform, chat_id = target.get("platform"), target.get("chat_id")
        if router is None or not platform or not chat_id:
            return False
        receipt = await router.send(
            DeliveryTarget(platform, chat_id, target.get("thread_id") or None, is_explicit=True),
            text,
            label=_LABEL,
        )
        return receipt.ok

    async def start(self, daemon: GatewayDaemon) -> None:
        scope = daemon.scope
        view: Any = scope.config if scope.config is not None else config_loader.load(scope)
        if not dispatch_enabled(view):
            log.info("task boards: dispatch runs outside the gateway (kanban.dispatch_in_gateway)")
            return
        self._daemon = daemon
        interval = float(_setting(view, "kanban.dispatch_interval_seconds", 60))
        settings = DispatchSettings.from_config(view)
        slugs = await run_blocking(list_boards, scope.root, label="boards")
        for slug in [s for s in slugs if board_path(scope.root, s).is_file()]:
            try:
                store = await run_blocking(BoardStore, scope, slug, label="board open")
            except Exception:
                log.warning("task board %s could not open; not watched", slug, exc_info=True)
                continue
            self._stores.append(store)
            watchers = BoardWatchers(
                store,
                deliver=self._deliver,
                dispatcher=Dispatcher(store, scope=scope, settings=settings),
                dispatch_interval_s=interval,
            )
            self._tasks.append(asyncio.ensure_future(watchers.run(self._stop)))

    async def stop(self, daemon: GatewayDaemon) -> None:
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=5.0)
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(BaseException):
                    await task
        stores, self._stores = self._stores, []
        for store in stores:
            try:
                store.close()
            except Exception:
                log.debug("task board %s did not close cleanly", store.slug, exc_info=True)


def create(_daemon: GatewayDaemon) -> BoardPart:
    return BoardPart()
