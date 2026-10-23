"""Slash commands on the bridge (P12-31): ``commands.catalog``, ``slash.exec`` and
``command.dispatch``.

Commands are looked up in the shared registry (``indus.service.commands``) and run **in process**;
the bridge never imports ``indus.cli``. The commands themselves, the busy and timeout rules and the
dispatch order are ``indus.service.slash_commands`` (shared with the daemon's WebSocket, W48-h);
this module is the bridge's ``SlashHost`` (``BridgeSlashHost``; the session switches ``yolo``,
``reasoning``, ``fast`` go through its ``config.set``) and the handler only an in-process host
has: ``queue`` (this viewer's own prompt queue). Who runs what:

- ``HANDLERS`` — the shared handlers plus the bridge's own (session, control and information);
- ``CLIENT`` — the terminal UI itself (screen, pickers, leaving, files on the client's side);
- everything else is answered ``SLASH_NOT_OWNED`` ("not mine"), so a client can tell "run it
  yourself or later" from a handler that failed (``SLASH_WORKER_FAILED``, also used when a handler
  passes its reply timeout: ``Bridge.slash_timeout``, at least ``MIN_TIMEOUT_S``) (R05.12).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue

from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.protocol.bridge import (
    CommandDispatchParams,
    CommandDispatchResult,
    CommandsCatalogParams,
    CommandsCatalogResult,
    ConfigKey,
    ConfigSetParams,
    SessionBranchParams,
    SessionCompressParams,
    SessionTitleParams,
    SessionUndoParams,
    SlashExecParams,
    SlashExecResult,
)
from indus.service import picker_reads, slash_commands
from indus.service.commands.registry import Surface
from indus.service.session_service import SessionService
from indus.service.slash_commands import SlashHandler, out, running
from indus.tui_bridge import methods_config, methods_session

if TYPE_CHECKING:
    from indus.learning.skills.invoke import CommandTable
    from indus.tui_bridge.bridge import Bridge
    from indus.tui_bridge.dispatch import MethodTable

__all__ = [
    "CLIENT",
    "HANDLERS",
    "MIN_TIMEOUT_S",
    "SURFACE",
    "BridgeSlashHost",
    "quick_commands",
    "register",
    "skill_table",
]

log = logging.getLogger(__name__)

SURFACE: Surface = "tui"
MIN_TIMEOUT_S = slash_commands.MIN_TIMEOUT_S
CLIENT = picker_reads.CLIENT_COMMANDS
_NOTHING: Mapping[str, str] = {}


class BridgeSlashHost:
    """The in-process bridge as the shared commands' host."""

    def __init__(self, bridge: Bridge, service: SessionService) -> None:
        self.bridge = bridge
        self._service = service

    @property
    def scope(self) -> Scope:
        return self.bridge.scope

    @property
    def service(self) -> SessionService:
        return self._service

    @property
    def surface(self) -> Surface:
        return SURFACE

    @property
    def timeout(self) -> float:
        return max(MIN_TIMEOUT_S, self.bridge.slash_timeout)

    @property
    def refused(self) -> Mapping[str, str]:
        return _NOTHING

    @property
    def quick_exec_refusal(self) -> str | None:
        return None

    def skills(self, session_id: str | None) -> CommandTable | None:
        return skill_table(self.bridge, session_id)

    async def queued(self, session_id: str) -> int:
        view = self.bridge.view(session_id)
        return len(view.queue) if view is not None else 0

    async def set_switch(self, session_id: str, key: ConfigKey, value: JsonValue) -> JsonValue:
        params = ConfigSetParams(key=key, value=value, session_id=session_id)
        return (await methods_config.set_value(self.bridge, params)).value

    async def title(self, session_id: str, title: str | None) -> tuple[str | None, bool]:
        result = await methods_session.title(
            self.bridge, SessionTitleParams(session_id=session_id, title=title)
        )
        return result.title, bool(result.pending)

    async def undo(self, session_id: str, count: int) -> tuple[int, str]:
        result = await methods_session.undo(
            self.bridge, SessionUndoParams(session_id=session_id, count=count)
        )
        return result.removed, result.prefill or ""

    async def branch(self, session_id: str, title: str | None) -> tuple[str, str | None]:
        child = await methods_session.branch(
            self.bridge, SessionBranchParams(session_id=session_id, title=title)
        )
        return child.session_id, child.title

    async def compress(self, session_id: str, focus: str | None) -> str:
        done = await methods_session.compress(
            self.bridge, SessionCompressParams(session_id=session_id, focus=focus)
        )
        return done.message or f"Compression: {done.status}."

    def stop(self, session_id: str) -> bool:
        stopped = self._service.interrupt(session_id, hard=True).interrupted
        view = self.bridge.view(session_id)
        if view is not None:
            view.queue.clear()
        return stopped

    def steer(self, session_id: str, text: str) -> bool:
        return self._service.steer(session_id, text).queued

    async def run_quick(self, session_id: str, command: str) -> tuple[str, str, int]:
        from indus.tui_bridge.methods_shell import run_host_command

        ran = await run_host_command(self.bridge, command, session_id=session_id)
        return ran.stdout, ran.stderr, ran.code


def _bridge(host: BridgeSlashHost) -> Bridge:
    return host.bridge


# -- the bridge's own handlers (an in-process host only) -------------------------------------------


async def _queue(host: BridgeSlashHost, sid: str, arg: str) -> SlashExecResult:
    view = _bridge(host).watch(sid)
    word, _, rest = arg.strip().partition(" ")
    word = word.lower()
    if word == "list" or not arg.strip():
        if not view.queue:
            return out("Nothing is queued.")
        return out(*(f"{n}. {text}" for n, text in enumerate(view.queue, 1)))
    if word == "clear":
        count = len(view.queue)
        view.queue.clear()
        return out(f"Cleared {count} queued message(s).")
    if word == "rm":
        if not rest.strip().isdigit() or not 1 <= int(rest) <= len(view.queue):
            return out("Usage: /queue rm N  (N from /queue list)")
        removed = view.queue[int(rest) - 1]
        del view.queue[int(rest) - 1]
        return out(f"Removed: {removed}")
    text = rest.strip() if word == "add" else arg.strip()
    if not running(host.service, sid):
        return out("Nothing is running; sending it now.", submit=text)
    view.queue.append(text)
    return out(f"Queued ({len(view.queue)} waiting).")


HANDLERS: dict[str, SlashHandler] = {
    **slash_commands.HANDLERS,
    "queue": _queue,
}
"""Canonical command name → the bridge's handler (the shared ones and the bridge's own)."""


# -- quick commands and skills ---------------------------------------------------------------------


def quick_commands(bridge: Bridge) -> dict[str, Mapping[str, Any]]:
    """The user's ``quick_commands`` table, keyed by lowercase name."""
    return picker_reads.quick_command_table(bridge.scope)


def skill_table(bridge: Bridge, session_id: str | None) -> CommandTable | None:
    """The skills and bundles a slash word can run here (None when they cannot be listed)."""
    return picker_reads.skill_table(bridge.scope, session_id)


# -- handlers --------------------------------------------------------------------------------------


async def catalog(bridge: Bridge, params: CommandsCatalogParams) -> CommandsCatalogResult:
    return await run_blocking(
        lambda: picker_reads.command_catalog(
            bridge.scope, surface=SURFACE, skills=skill_table(bridge, params.session_id)
        ),
        label="command catalogue",
    )


async def slash_exec(bridge: Bridge, params: SlashExecParams) -> SlashExecResult:
    host = BridgeSlashHost(bridge, await bridge.service())
    return await slash_commands.slash_exec(host, HANDLERS, params)


async def dispatch(bridge: Bridge, params: CommandDispatchParams) -> CommandDispatchResult:
    host = BridgeSlashHost(bridge, await bridge.service())
    return await slash_commands.dispatch(host, HANDLERS, params)


def register(table: MethodTable) -> None:
    table.add("commands.catalog", catalog)
    table.add("slash.exec", slash_exec)
    table.add("command.dispatch", dispatch)
