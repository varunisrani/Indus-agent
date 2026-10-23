"""``shell.exec``: the composer's ``!command`` on this machine (P12-31).

The checks, the runner and the clean-up live in ``service.host_shell`` (W50-b), shared with the
daemon's WebSocket: the dangerous-command detection (a hardline or dangerous command is refused
with ``SHELL_REFUSED``, R04.27), the environment from ``safety.child_env``, the null device for
input, its own process group, the limit on the bridge clock (``bridge.shell_timeout``,
``SHELL_TIMEOUT``) and redaction before the tail cut. This module is the bridge's host for them:
the session's folder comes from the bridge's own service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.protocol.bridge import ShellExecParams, ShellExecResult
from indus.service import host_shell
from indus.service.host_shell import (
    STDERR_KEEP,
    STDOUT_KEEP,
    TIMEOUT_S,
    Captured,
    refusal,
    run_captured,
)

if TYPE_CHECKING:
    from indus.tui_bridge.bridge import Bridge
    from indus.tui_bridge.dispatch import MethodTable

__all__ = [
    "STDERR_KEEP",
    "STDOUT_KEEP",
    "TIMEOUT_S",
    "Captured",
    "refusal",
    "register",
    "run_captured",
    "run_host_command",
]


async def run_host_command(
    bridge: Bridge, command: str, *, session_id: str | None = None
) -> ShellExecResult:
    """Check, run and clean one host command (``shell.exec`` and quick ``exec`` commands)."""
    return await host_shell.run_host_command(
        bridge.scope,
        command,
        session_id=session_id,
        service=bridge.built,
        timeout=bridge.shell_timeout,
        clock=bridge.clock,
    )


async def shell_exec(bridge: Bridge, params: ShellExecParams) -> ShellExecResult:
    return await run_host_command(bridge, params.command, session_id=params.session_id)


def register(table: MethodTable) -> None:
    table.add("shell.exec", shell_exec)
