"""Completion methods of the bridge (P12-31): ``complete.slash`` and ``complete.path``.

``complete.slash`` asks the shared registry (``indus.service.commands``) with the names this
surface offers plus the user's quick commands and the runnable skills and bundles.

``complete.path`` completes the word under the cursor in the client's folder, else the session's;
the rules (reference forms, fuzzy ``@word`` search, folder listings run where the session's
commands run) live in ``indus.service.path_completion``, which the daemon's WebSocket calls too
(W48-b), so an attached TUI completes what ``--local`` completes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.protocol.bridge import CompletePathParams, CompleteResult, CompleteSlashParams
from indus.service import path_completion
from indus.service.path_completion import FUZZY_MIN, LISTING_SCRIPT, MAX_ITEMS, fuzzy_rank
from indus.service.picker_reads import slash_completions
from indus.tui_bridge import methods_slash

if TYPE_CHECKING:
    from indus.tui_bridge.bridge import Bridge
    from indus.tui_bridge.dispatch import MethodTable

__all__ = ["FUZZY_MIN", "LISTING_SCRIPT", "MAX_ITEMS", "fuzzy_rank", "register"]


async def complete_slash(bridge: Bridge, params: CompleteSlashParams) -> CompleteResult:
    return slash_completions(
        bridge.scope, params.text, surface=methods_slash.SURFACE, session_id=params.session_id
    )


async def complete_path(bridge: Bridge, params: CompletePathParams) -> CompleteResult:
    return await path_completion.complete_path(
        bridge.scope,
        params,
        folder=path_completion.session_folder(bridge.built, params.session_id),
        environments=bridge.environments,
    )


def register(table: MethodTable) -> None:
    table.add("complete.slash", complete_slash)
    table.add("complete.path", complete_path)
