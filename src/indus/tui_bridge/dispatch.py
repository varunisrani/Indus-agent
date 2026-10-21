"""The bridge's method table (P12-28) on the shared dispatcher (P17-12).

The dispatcher itself lives in ``indus.service.rpc.dispatch`` so the daemon's WebSocket endpoint
uses the same one (the gateway may not import ``tui_bridge``); this module keeps the bridge's
handler modules and re-exports the shared names under their P12 spellings.

Each module in ``HANDLER_MODULES`` exposes ``register(table)``; later tasks add their module to
that tuple.
"""

from __future__ import annotations

import importlib

from indus.service.rpc.dispatch import DEADLINES, DEFAULT_DEADLINE_S, MethodTable, error_frame
from indus.service.rpc.dispatch import Dispatcher as SharedDispatcher
from indus.service.rpc.dispatch import Handler as SharedHandler
from indus.tui_bridge.bridge import Bridge

__all__ = [
    "HANDLER_MODULES",
    "Dispatcher",
    "Handler",
    "MethodTable",
    "build_table",
    "error_frame",
]

type Handler = SharedHandler[Bridge]
"""``handler(bridge, params model) -> result model``."""

HANDLER_MODULES: tuple[str, ...] = (
    "indus.tui_bridge.methods_session",  # P12-29
    "indus.tui_bridge.methods_prompt",  # P12-29
    "indus.tui_bridge.server_requests",  # P12-30
    "indus.tui_bridge.methods_complete",  # P12-31
    "indus.tui_bridge.methods_slash",  # P12-31
    "indus.tui_bridge.methods_shell",  # P12-31
    "indus.tui_bridge.methods_config",  # P12-31
    "indus.tui_bridge.methods_composer",  # P12-31
    "indus.tui_bridge.methods_extras",  # P24-14, 19, 20, 26 (projects, pet, wake, capabilities)
)
"""Modules whose ``register(table)`` adds handlers, in order."""


def build_table(modules: tuple[str, ...] = HANDLER_MODULES) -> MethodTable[Bridge]:
    """A table with every handler module registered."""
    table = MethodTable[Bridge]()
    for name in modules:
        importlib.import_module(name).register(table)
    return table


class Dispatcher(SharedDispatcher[Bridge]):
    """The shared dispatcher bound to one bridge; the full handler table by default."""

    def __init__(
        self,
        bridge: Bridge,
        table: MethodTable[Bridge] | None = None,
        *,
        deadline: float = DEFAULT_DEADLINE_S,
    ) -> None:
        super().__init__(
            bridge,
            table if table is not None else build_table(),
            deadline=deadline,
            deadlines=DEADLINES,
        )

    @property
    def bridge(self) -> Bridge:
        return self.context
