# pyright: strict
"""Turns in a profile's canonical bot chat run as bot turns (P24-07 follow-up, wave 45).

A profile's canonical chat (``state.bot_chat``: the one session titled ``CANONICAL_TITLE``, or the
live tip its compression lineage leads to; the title is its whole identity) is where other
bots and the person's Bots page talk to this profile's bot. Its agent is built with the platform
``BOTS_PLATFORM``, which opens the gate of the ``bots`` toolset (``message_agent``), and with the
profile's own selection for that platform: ``platform_toolsets.bots`` when the person saved one
(``indus tools``), else the ``indus-bots`` bundle (the shared core plus ``bots``). The ``no_mcp``
marker means something only to ACP and is dropped from a saved list.

``BotChats(scope, store).shape`` is the ``SessionService`` seam ``bot_chats``: it runs when a
session is attached, before the MCP and plugin steps (so their toolsets join a bot chat's explicit
selection as they join any other), and answers the options and selection unchanged for every
session that is not the canonical chat. A row that cannot be read leaves the session as it was
(logged). The gateway daemon hands it to its chat and API services; the webhook surface never gets
it (its read-only floor stands), and hosts without a bot transport (the CLI's in-process host, the
TUI bridge, ACP) do not register ``message_agent`` at all.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Final, cast

from indus.core.agent import AgentOptions
from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.state import sessions
from indus.state.bot_chat import BOT_SOURCE, CANONICAL_TITLE, find_canonical_in
from indus.state.registry import StoreHandle
from indus.tools.toolsets import BUNDLE_PREFIX

__all__ = ["BOTS_PLATFORM", "SETTING", "BotChats", "bots_selection", "is_canonical"]

log = logging.getLogger(__name__)

BOTS_PLATFORM: Final = BOT_SOURCE
SETTING: Final = f"platform_toolsets.{BOTS_PLATFORM}"
_ACP_ONLY_MARKERS: Final = frozenset({"no_mcp"})


def bots_selection(scope: Scope) -> list[str]:
    """The toolsets of a bot turn: the saved ``platform_toolsets.bots``, else ``indus-bots``."""
    config = scope.config
    saved: object = None
    if config is not None:
        try:
            saved = config.get(SETTING)
        except Exception:
            log.debug("%s unreadable; using the bundle", SETTING, exc_info=True)
    if isinstance(saved, list):
        names = [str(n).strip() for n in cast(list[object], saved)]
        chosen = [n for n in names if n and n not in _ACP_ONLY_MARKERS]
        if chosen:
            return list(dict.fromkeys(chosen))
    return [f"{BUNDLE_PREFIX}{BOTS_PLATFORM}"]


def is_canonical(store: StoreHandle, session_id: str) -> bool:
    """Whether ``session_id`` is the profile's canonical bot chat: the row holding the title, or
    the live tip the canonical lookup follows the title's lineage to (never recency)."""
    with store.read() as conn:
        row = sessions.get_in(conn, session_id)
        if row is None:
            return False
        if row.get("title") == CANONICAL_TITLE:
            return True
        return find_canonical_in(conn) == session_id


class BotChats:
    """The ``bot_chats`` seam of one profile's session service (see the module notes)."""

    def __init__(self, scope: Scope, store: StoreHandle) -> None:
        self.scope = scope
        self.store = store

    async def shape(
        self, session_id: str, options: AgentOptions, selection: Sequence[str] | None
    ) -> tuple[AgentOptions, list[str] | None]:
        chosen = list(selection) if selection is not None else None
        try:
            canonical = await run_blocking(
                is_canonical, self.store, session_id, label="bot chat check"
            )
        except Exception:
            log.warning("could not tell whether %s is the bot chat", session_id, exc_info=True)
            return options, chosen
        if not canonical:
            return options, chosen
        loop = options.loop
        if loop is not None:
            loop = replace(loop, platform=BOTS_PLATFORM)
        shaped = replace(options, platform=BOTS_PLATFORM, loop=loop)
        log.debug("session %s runs as the profile's bot chat", session_id)
        return shaped, bots_selection(self.scope)
