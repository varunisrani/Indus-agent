"""The runtime-status file writer (P15-24).

``StatusWriter`` owns ``<home>/gateway_state.json`` for one daemon: every state change and every
platform change rewrites it (atomic replace, 0600), and housekeeping calls ``heartbeat`` every
60 s so readers can tell a live daemon from a dead one (stale after 120 s,
``state.gateway_runtime.is_stale``). A write that fails is logged once and never stops the daemon.

``previous_death`` reads the file a previous process left: still in a live state while its owner
is gone means that process died without running its exit path (an unclean death). It is logged
and appended to ``logs/gateway_exits.log``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from indus.foundation.clock import Clock
from indus.state.gateway_runtime import (
    LIVE_STATES,
    GatewayState,
    PlatformStatus,
    ProcessIdentity,
    RuntimeStatus,
    exits_log_path,
    read_status,
    write_status,
)

__all__ = ["StatusWriter", "append_exit_line", "previous_death"]

log = logging.getLogger(__name__)


def append_exit_line(home: Path, entry: Mapping[str, Any]) -> None:
    """One JSON line in ``logs/gateway_exits.log`` (best effort)."""
    path = exits_log_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(entry), sort_keys=True) + "\n")
    except OSError:
        log.warning("could not append to %s", path, exc_info=True)


def previous_death(home: Path, me: ProcessIdentity, **probes: Any) -> RuntimeStatus | None:
    """The previous status when that process died uncleanly, else None."""
    try:
        found = read_status(home)
    except Exception:
        return None
    if found is None or found.state not in LIVE_STATES or found.pid == me.pid:
        return None
    if found.process.is_alive(**probes):
        return None
    return found


class StatusWriter:
    """The status file of one running daemon (see the module notes)."""

    def __init__(
        self,
        home: Path,
        *,
        clock: Clock,
        process: ProcessIdentity,
        profile: str,
        code_version: str,
        epoch: str = "",
    ) -> None:
        self.home = home
        self.clock = clock
        self._status = RuntimeStatus(
            "starting",
            "",
            process,
            profile=profile,
            code_version=code_version,
            epoch=epoch,
        )
        self.history: list[GatewayState] = []
        self._warned = False

    @property
    def state(self) -> GatewayState:
        return self._status.state

    def snapshot(self) -> RuntimeStatus:
        return self._status

    def _write(self) -> None:
        self._status = self._status.stamped(self.clock.now())
        try:
            write_status(self.home, self._status)
        except Exception:
            if not self._warned:
                self._warned = True
                log.warning("could not write the gateway status file", exc_info=True)

    def set_state(
        self,
        state: GatewayState,
        *,
        exit_reason: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        changes: dict[str, Any] = {"state": state}
        if exit_reason is not None:
            changes["exit_reason"] = exit_reason
        if exit_code is not None:
            changes["exit_code"] = exit_code
        self._status = replace(self._status, **changes)
        if not self.history or self.history[-1] != state:
            self.history.append(state)
            log.info("gateway state: %s", state)
        self._write()

    def set_previous_exit(self, label: str) -> None:
        self._status = replace(self._status, previous_exit=label)
        self._write()

    def set_platform(self, name: str, row: PlatformStatus | None) -> None:
        platforms = dict(self._status.platforms)
        if row is None:
            platforms.pop(name, None)
        else:
            platforms[name] = row
        self._status = replace(self._status, platforms=platforms)
        self._write()

    def set_surface(self, name: str, row: Mapping[str, Any] | None) -> None:
        """One HTTP surface's row (``None`` drops it)."""
        surfaces = dict(self._status.surfaces)
        if row is None:
            surfaces.pop(name, None)
        else:
            surfaces[name] = dict(row)
        self._status = replace(self._status, surfaces=surfaces)
        self._write()

    def heartbeat(self) -> None:
        self._write()
