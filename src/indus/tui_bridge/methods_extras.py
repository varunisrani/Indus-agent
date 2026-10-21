"""The P24 methods the TUI calls over the bridge (``/pet``, ``/wake``; the sidebar's projects).

``projects.*`` (P24-14), ``pet.*`` (P24-19), ``capabilities.summary`` (P24-26) and ``voice.*``
(P24-35) are the service handlers; ``wake.*`` (P24-20) runs one ``WakeRpc`` per bridge process,
the bridge being the lease owner, and sends ``wake.detected`` through the bridge's own
(thread-safe) notify. The daemon's
WebSocket serves the same methods (``indus.gateway.ws.extras``), so ``/pet`` and ``/wake`` work
attached or local.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from typing import Any

from indus.protocol.base import ProtocolModel
from indus.protocol.wake import WAKE_METHODS
from indus.service import capabilities_rpc, pets_rpc, projects_rpc, voice_rpc
from indus.service.wake_rpc import WakeRpc
from indus.tui_bridge.bridge import Bridge
from indus.tui_bridge.dispatch import MethodTable

__all__ = ["register"]

_lock = threading.Lock()
_wake: WakeRpc | None = None


def _emit(owner: object, method: str, model: ProtocolModel) -> None:
    if isinstance(owner, Bridge):
        owner.notify(method, model.to_wire())


def _wake_for(bridge: Bridge) -> WakeRpc:
    global _wake
    with _lock:
        if _wake is None:
            _wake = WakeRpc(bridge.scope, emit=_emit)
        return _wake


def _wake_handler(name: str) -> Callable[[Bridge, Any], Awaitable[ProtocolModel]]:
    async def handle(bridge: Bridge, params: Any) -> ProtocolModel:
        return await _wake_for(bridge).handlers()[name](bridge, params)

    return handle


def register(table: MethodTable) -> None:
    projects_rpc.register(table)
    pets_rpc.register(table)
    capabilities_rpc.register(table)
    voice_rpc.register(table)
    for name, *_rest in WAKE_METHODS:
        table.add(name, _wake_handler(name))
