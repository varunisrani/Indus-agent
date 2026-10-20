"""``messages_send`` of ``indus mcp serve`` (P19-16): one text to one chat, through the daemon.

The MCP server holds no platform connection. ``Sender.send(target, message)`` works out where the
text goes and asks the profile's running daemon (control socket, owner-only ``deliver-message``
verb, ``indus.service.messaging_bridge``); the daemon sends it through its delivery router and the
platform's outbound sender, which records the delivery-ledger row before the platform call. It is
a library for the bridge, never a tool offered to the model (D-16.37).

Targets: ``platform:chat_id`` (``:thread_id`` optional, in the platform's own address grammar) or
``platform:<friendly name>``. A friendly name is looked up in the gateway's saved channel
directory: an exact chat id, then the name without ``#`` and case, then ``guild/name``, then a name
only one chat starts with. Several chats matching is an error that lists them; no match sends the
text after the platform as written, and the daemon decides (an unknown ``#name`` is its
``unknown_channel``). A bare platform (home channel), ``all`` and ``origin`` are refused: the bridge
sends to one named chat.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Final

from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.mcp_serve.read_tools import Channel, load_directory
from indus.service import messaging_bridge as bridge
from indus.service.gateway_control import ControlReply

__all__ = ["NO_DAEMON", "Sender", "resolve_name"]

log = logging.getLogger(__name__)

NO_DAEMON: Final = "daemon not running"
_RESERVED: Final = frozenset({"all", "origin", "local"})
_LOCAL_ERRORS: Final = frozenset({"no_socket", "unreachable", "timeout"})

type Deliver = Callable[..., ControlReply]


def _norm(name: str) -> str:
    return name.strip().lstrip("#").strip().lower()


def resolve_name(entries: list[Channel], query: str) -> list[Channel]:
    """The directory chats ``query`` names: one, several (ambiguous) or none (see the notes)."""
    raw = query.strip()
    if not raw:
        return []
    exact = [e for e in entries if e.chat_id == raw]
    if exact:
        return exact[:1]
    wanted = _norm(raw)
    named = [e for e in entries if _norm(e.name) == wanted]
    if named:
        return named
    if "/" in wanted:
        guild, _, name = wanted.rpartition("/")
        in_guild = [
            e for e in entries if e.guild.strip().lower() == guild and _norm(e.name) == name
        ]
        if in_guild:
            return in_guild
    return [e for e in entries if wanted and _norm(e.name).startswith(wanted)]


def daemon_error(reply: ControlReply) -> dict[str, Any]:
    """The ``error`` answer for a control reply that did not go through."""
    if reply.error in _LOCAL_ERRORS:
        return {"error": NO_DAEMON}
    if reply.error == "unsafe_home":
        return {"error": "the profile folder is writable by others; the daemon is not asked"}
    if reply.error in ("unknown_verb", "unsupported"):
        return {"error": "the running daemon does not offer this (update and restart it)"}
    detail = redact_text(reply.detail) if reply.detail else ""
    return {"error": f"{reply.error}: {detail}" if detail else str(reply.error)}


class Sender:
    """See the module notes."""

    def __init__(self, scope: Scope, *, deliver: Deliver = bridge.deliver) -> None:
        self.scope = scope
        self._deliver = deliver

    def send(self, target: object, message: object) -> dict[str, Any]:
        """Blocking (a socket exchange): call it off the loop."""
        text = message if isinstance(message, str) else ""
        where = target.strip() if isinstance(target, str) else ""
        if not where or not text.strip():
            return {"error": "both target and message are required"}
        if len(text) > bridge.MAX_MESSAGE_CHARS:
            return {"error": f"the message is longer than {bridge.MAX_MESSAGE_CHARS} characters"}
        platform, sep, rest = where.partition(":")
        platform = platform.strip().lower()
        if not platform or platform in _RESERVED or not sep or not rest.strip():
            return {
                "error": "the target must name one chat: platform:chat_id or platform:<chat name>"
            }
        entries = load_directory(self.scope).get(platform, [])
        matches = resolve_name(entries, rest)
        if len(matches) > 1:
            return {
                "error": f"{rest.strip()!r} matches more than one {platform} chat; use one target",
                "candidates": [f"{c.target} ({c.name})" for c in matches],
            }
        if matches:
            chat = matches[0]
            reply = self._deliver(
                self.scope.home,
                text,
                platform=chat.platform,
                chat_id=chat.chat_id,
                thread_id=chat.thread_id,
            )
        else:
            reply = self._deliver(self.scope.home, text, target=f"{platform}:{rest.strip()}")
        if not reply.ok or reply.result is None:
            return daemon_error(reply)
        result = dict(reply.result)
        if not result.get("sent"):
            return {**result, "error": str(result.get("error") or "the message was not sent")}
        return {"target": where, **result}
