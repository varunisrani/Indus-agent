"""``/mcp`` and ``/reload-mcp`` in the classic REPL (P13-08, P13-12).

Both talk to the profile's ``service.mcp_host.McpUpkeep`` (the session service's MCP servers),
running its work on the service loop:

- ``/mcp``: the ``indus mcp list`` table with the running servers' live states and tool counts,
  plus one line per server that needs attention (lazy, needs sign-in again, last error);
- ``/reload-mcp``: asks first while ``approvals.mcp_reload_confirm`` is on (a REPL without a
  person refuses and names the switch), then re-reads the config and reconciles the servers. The
  current session keeps its tool array and system prompt until its next compaction; new sessions
  get the new tools at once (R07.23).

The MCP stack is imported only when a command runs, never at REPL start.
"""

from __future__ import annotations

import logging

from indus.cli.slash.context import Handler, SlashContext, SlashResult
from indus.service import mcp_host

__all__ = ["HANDLERS", "QUESTION", "cmd_mcp", "cmd_reload_mcp"]

log = logging.getLogger(__name__)

QUESTION = (
    "Reload the MCP servers from the config? Their tools change, so the provider's prompt cache "
    "starts over; this session keeps its current tools until its next compaction. "
    "(Turn this question off with approvals.mcp_reload_confirm: false.)"
)
RELOAD_TIMEOUT_S = 120.0


def cmd_mcp(ctx: SlashContext, args: str) -> SlashResult:
    from indus.cli.commands.mcp import LiveStatus, status_lines, status_rows

    upkeep = mcp_host.upkeep_for(ctx.scope)
    states: dict[str, mcp_host.ServerLive] = {}
    if upkeep is not None and upkeep.host is not None:
        try:
            states = upkeep.call(upkeep.live())
        except Exception as exc:
            log.warning("MCP status unavailable", exc_info=True)
            ctx.say(f"The running MCP servers could not be asked ({type(exc).__name__}).")
    live = {name: LiveStatus(s.state, s.tools) for name, s in states.items()}
    rows = status_rows(ctx.scope, live=live or None)
    ctx.say(*status_lines(rows))
    for name, state in sorted(states.items()):
        note = state.detail or state.error
        if note:
            ctx.say(f"  {name}: {note}")
    if rows and (upkeep is None or upkeep.host is None):
        ctx.say("No MCP server runs in this session yet; /reload-mcp starts the configured ones.")
    return SlashResult()


def cmd_reload_mcp(ctx: SlashContext, args: str) -> SlashResult:
    from indus.mcp.reload import confirm_on, fresh_scope

    upkeep = mcp_host.upkeep_for(ctx.scope)
    if upkeep is None:
        ctx.say("This session host does not run MCP servers.")
        return SlashResult()
    current = fresh_scope(ctx.scope)
    if upkeep.host is None and not mcp_host.configured(current):
        ctx.say("No MCP servers are configured.")
        return SlashResult()
    if confirm_on(current):
        if not ctx.interactive:
            ctx.say(
                "Reloading MCP servers needs a confirmation and nobody can answer here; set "
                "approvals.mcp_reload_confirm: false to reload without asking."
            )
            return SlashResult()
        if not ctx.confirm(QUESTION):
            ctx.say("MCP reload cancelled; nothing changed.")
            return SlashResult()
    try:
        outcome = upkeep.call(upkeep.reload(), timeout=RELOAD_TIMEOUT_S)
    except Exception as exc:
        log.warning("MCP reload failed", exc_info=True)
        ctx.say(f"MCP reload failed: {type(exc).__name__}: {exc}")
        return SlashResult()
    ctx.say(str(getattr(outcome, "message", "") or "MCP servers reloaded."))
    if getattr(outcome, "reloaded", False):
        ctx.say(
            "This session keeps its current tools until its next compaction; new sessions get "
            "the new ones."
        )
    return SlashResult()


HANDLERS: dict[str, Handler] = {"mcp": cmd_mcp, "reload-mcp": cmd_reload_mcp}
