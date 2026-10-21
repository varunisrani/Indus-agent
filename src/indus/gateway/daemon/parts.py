"""How features plug into the gateway daemon (P15-24).

A ``DaemonPart`` is one feature the daemon runs: the control socket, the delivery ledger sweep,
the cron ticker, the reconnect watcher, the channel directory. The daemon calls, in order:

- ``prepare(daemon)`` once the daemon is assembled and before anything connects: set intake seams
  here (``daemon.pipeline.on_request`` / ``.interceptors`` / ``.enrich``), sender options
  (``daemon.sender_options``) and platform listeners (``daemon.on_platform_up`` / ``_down``);
- ``start(daemon)`` at the part's ``stage``:
  ``CONTROL`` before platforms connect (a ``required`` part that fails stops the start, exit 75),
  ``RECOVERY`` after platforms connected, inside the startup-restore window (inbound held),
  ``BACKGROUND`` once recovery is done, just before the daemon reports ``running``;
- ``stop(daemon)`` at shutdown, in reverse order: ``BACKGROUND`` first, ``CONTROL`` last (the
  control socket keeps answering during the drain). ``stop`` must not raise.

Production parts are listed once, in ``PART_FACTORIES``, as ``"module:attr"`` strings naming a
``(daemon) -> DaemonPart | None`` callable (``None``: not wanted for this profile). Each feature
adds its own line; nothing here imports them until a daemon starts.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Sequence
from enum import IntEnum
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from indus.gateway.daemon.process import GatewayDaemon

__all__ = ["PART_FACTORIES", "DaemonPart", "Stage", "load_parts"]

log = logging.getLogger(__name__)


class Stage(IntEnum):
    """When a part starts (and, reversed, when it stops)."""

    CONTROL = 1
    RECOVERY = 2
    BACKGROUND = 3


class DaemonPart:
    """One feature the daemon runs (see the module notes). Every hook is a no-op by default."""

    name: ClassVar[str] = "part"
    stage: ClassVar[Stage] = Stage.BACKGROUND
    required: ClassVar[bool] = False

    async def prepare(self, daemon: GatewayDaemon) -> None:
        """The daemon is assembled; nothing is connected yet."""

    async def start(self, daemon: GatewayDaemon) -> None:
        """Start at this part's stage. Raising fails the start only for a ``required`` part."""

    async def stop(self, daemon: GatewayDaemon) -> None:
        """Stop (reverse order). Must not raise; bounded by the shutdown watchdog."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} {self.stage.name.lower()}>"


PART_FACTORIES: tuple[str, ...] = (
    "indus.gateway.daemon.side_services:create",
    "indus.gateway.delivery.parts:ledger_part",
    "indus.gateway.delivery.parts:router_part",
    "indus.gateway.extras:create",
    "indus.gateway.http.part:create",
    "indus.gateway.board_part:create",
)
"""``"module:attr"`` factories of the parts every gateway runs (each feature adds its line):
the control socket with the reconnect watcher and gateway hooks (P15-27, P15-29; CONTROL), the
delivery-ledger sweep (P15-19; RECOVERY), the platform deliverers and channel directory (P15-20;
BACKGROUND), cron, chat prompts and media intake (P15-21..23; BACKGROUND), the API server and
the webhook receiver (P15-30..32; BACKGROUND, only when enabled), the task-board watchers and
dispatcher (P24-12; BACKGROUND, boards that exist, ``kanban.dispatch_in_gateway``)."""


def load_parts(
    daemon: GatewayDaemon, factories: Sequence[str] = PART_FACTORIES
) -> list[DaemonPart]:
    """Build the parts ``factories`` name; a factory that fails is logged and skipped."""
    parts: list[DaemonPart] = []
    for target in factories:
        module_name, _, attr = target.partition(":")
        try:
            factory: Any = getattr(importlib.import_module(module_name), attr)
            part = factory(daemon)
        except Exception:
            log.exception("gateway part %s could not be built; it will not run", target)
            continue
        if part is None:
            continue
        if not isinstance(part, DaemonPart):
            log.error("gateway part %s did not return a DaemonPart; ignored", target)
            continue
        parts.append(part)
    return parts
