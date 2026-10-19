"""The classic CLI's handlers for ``/goal``, ``/subgoal``, ``/loop``, ``/heartbeat`` (``/hb``),
``/btw`` and ``/moa`` (P10-21..26).

The commands themselves live in ``indus.service.automation`` (one implementation for every
surface); these handlers only forward the line through ``ctx.automation`` and print the reply.
A reply that carries text to send (a goal's objective, a ``/moa`` prompt, a resumed goal's
continuation) is handed back as ``SlashResult(submit=...)``, so it goes out as an ordinary
message. ``HostAutomation`` is the ``AutomationPort`` over the in-process host: every call runs on
the host's loop thread.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.cli.slash.context import AutomationReply, Handler, SlashContext, SlashResult

if TYPE_CHECKING:
    from indus.service.automation import SessionAutomation
    from indus.service.host_inprocess import InProcessHost

__all__ = ["HANDLERS", "HostAutomation"]

NOT_HERE = "/{name} needs the session host's automation, which this window does not run."
BTW_WAIT_S = 300.0
"""How long ``/btw`` waits for its answer before giving up on it."""


class HostAutomation:
    """``AutomationPort`` over an ``InProcessHost`` and its ``SessionAutomation``."""

    def __init__(self, host: InProcessHost, automation: SessionAutomation) -> None:
        self.host = host
        self.automation = automation

    def run(self, command: str, session_id: str, args: str) -> AutomationReply:
        timeout = BTW_WAIT_S if command in ("btw", "loop") else -1.0
        reply = self.host.call(self.automation.command(command, session_id, args), timeout=timeout)
        return AutomationReply(tuple(reply.lines), reply.submit)


def _forward(name: str) -> Handler:
    def handler(ctx: SlashContext, args: str) -> SlashResult:
        port = ctx.automation
        if port is None:
            ctx.say(NOT_HERE.format(name=name))
            return SlashResult()
        reply = port.run(name, ctx.session_id, args)
        ctx.say(*reply.lines)
        return SlashResult(submit=reply.submit)

    handler.__name__ = f"{name}_command"
    return handler


HANDLERS: dict[str, Handler] = {
    name: _forward(name) for name in ("btw", "goal", "subgoal", "loop", "heartbeat", "moa")
}
"""Canonical command name → handler."""
