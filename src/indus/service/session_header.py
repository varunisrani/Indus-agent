"""``session.info`` fields: what a client's status bar shows about a session (model, provider,
reasoning level, folder, tools, title, whether a turn runs). One definition for the TUI's
in-process bridge and the daemon (W48-h, D-16.550); the transport says which title and which
tool-progress mode apply to the connection.
"""

from __future__ import annotations

import logging
from typing import Any

from indus.service.session_service import SessionService

__all__ = ["session_info"]

log = logging.getLogger(__name__)


def session_info(
    service: SessionService, session_id: str, *, title: str | None, tool_progress: str
) -> dict[str, Any]:
    """``SessionInfo`` fields for an attached session (no ``type``, ``session_id`` or ``seq``)."""
    agent = service.agent(session_id)
    fields: dict[str, Any] = {
        "title": title,
        "model": service.route.model,
        "provider": service.route.provider,
        "cwd": str(agent.options.cwd) if agent.options.cwd is not None else None,
        "tools": [],
        "running": agent.current is not None,
        "tool_progress": tool_progress,
    }
    try:
        state = agent.binding.state
        fields["model"] = state.model
        fields["provider"] = state.provider
        fields["reasoning_effort"] = state.reasoning_effort
    except Exception:
        log.debug("route state not ready", exc_info=True)
    try:
        fields["tools"] = list(agent.tool_names)
    except Exception:
        log.debug("tool names not ready", exc_info=True)
    if fields["cwd"] is None:
        cwd = agent.options.session_fields.get("cwd")
        fields["cwd"] = str(cwd) if cwd else None
    return fields
