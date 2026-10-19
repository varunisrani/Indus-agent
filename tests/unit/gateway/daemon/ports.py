"""A test double of ``side_services.DaemonPorts`` for the side-service unit tests (P15-27/29).

The real daemon is reached through ``side_services.DaemonBridge`` (see
``tests/integration/gateway/test_control_part.py``); these unit tests script ``connect_platform``
and record the platform rows the watcher reports.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from indus.foundation.scope import Scope
from indus.gateway.control_socket import Handler, identity_payload
from indus.gateway.daemon.reconnect import ConnectOutcome


@dataclass
class PortsDouble:
    scope: Scope
    pipeline: Any = None
    hook_bus: Any = None
    outcomes: list[ConnectOutcome] = field(default_factory=list[ConnectOutcome])
    connects: list[str] = field(default_factory=list[str])
    statuses: list[tuple[str, dict[str, Any] | None]] = field(
        default_factory=list[tuple[str, dict[str, Any] | None]]
    )
    handlers: dict[str, tuple[Handler, float]] = field(
        default_factory=dict[str, tuple[Handler, float]]
    )
    state: str = "running"

    def runtime_status(self) -> Mapping[str, Any]:
        return {"state": self.state, "pid": os.getpid()}

    def identity(self) -> Mapping[str, Any]:
        return identity_payload(self.scope.home, self.scope.env)

    def control_handlers(self) -> Mapping[str, tuple[Handler, float]]:
        return self.handlers

    async def connect_platform(self, name: str) -> ConnectOutcome:
        self.connects.append(name)
        if self.outcomes:
            return self.outcomes.pop(0)
        return ConnectOutcome.retryable("still down")

    def set_platform_status(self, name: str, status: Mapping[str, Any] | None) -> None:
        self.statuses.append((name, dict(status) if status is not None else None))
