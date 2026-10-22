# pyright: strict
"""The CLI's door to its daemon's language servers (P24-15 follow-ups, wave 45).

Sessions attach to the profile's daemon by default, so the language servers their edits started
run there, not in the CLI process. Two owner-only control-socket verbs (``gateway.control_socket``,
registered by the daemon's control part) reach them:

- ``lsp-restart {}`` → ``{stopped, running}``: every server of the profile's tier stops and their
  failures are forgotten; the next edit starts them again (``lsp_host.restart_servers``).
  ``running`` is false when the daemon runs no tier (LSP off, or no session attached yet).
- ``lsp-release {folder}`` → ``{stopped}``: the servers rooted at or below ``folder`` (an absolute
  path of at most ``MAX_FOLDER_CHARS``) stop, because the folder is about to be removed (a
  worktree; ``lsp_host.release_servers``).

A bad param is answered ``{"refused": "bad_params", "detail": …}`` (the control wire's error
answers belong to the gateway layer). On the CLI side ``restart_in_gateway`` and
``release_in_gateway`` ask the daemon serving a home and answer None when none answers, or when
it is too old to know the verb, so the caller falls back (the CLI's own process, or nothing);
``gateway_releaser(home)`` is the ``before_remove`` step the CLI hands ``backends.worktrees``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from indus.foundation.scope import Scope
from indus.service import gateway_control
from indus.service.gateway_control import ControlReply

__all__ = [
    "MAX_FOLDER_CHARS",
    "RELEASE_VERB",
    "RESTART_VERB",
    "VERB_TIMEOUT_S",
    "LspVerbs",
    "gateway_releaser",
    "release_in_gateway",
    "restart_in_gateway",
]

log = logging.getLogger(__name__)

RESTART_VERB: Final = "lsp-restart"
RELEASE_VERB: Final = "lsp-release"
VERB_TIMEOUT_S: Final = 15.0
"""A server stops within a few seconds (request, grace, TERM, KILL); all stop at once."""
MAX_FOLDER_CHARS: Final = 4096

type Query = Callable[..., ControlReply]


class LspVerbs:
    """``lsp-restart`` and ``lsp-release`` inside the daemon of ``scope``'s profile."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    def handlers(self) -> dict[str, tuple[Callable[[dict[str, Any]], Any], float]]:
        return {
            RESTART_VERB: (self.restart, VERB_TIMEOUT_S),
            RELEASE_VERB: (self.release, VERB_TIMEOUT_S),
        }

    async def restart(self, params: dict[str, Any]) -> Mapping[str, Any]:
        if params:
            return {"refused": "bad_params", "detail": "lsp-restart takes no params"}
        from indus.service import lsp_host

        stopped = await lsp_host.restart_servers(self.scope)
        if stopped is None:
            return {"stopped": 0, "running": False}
        log.info("lsp-restart: %d language server(s) stopped", stopped)
        return {"stopped": stopped, "running": True}

    async def release(self, params: dict[str, Any]) -> Mapping[str, Any]:
        folder = params.get("folder")
        if set(params) != {"folder"} or not isinstance(folder, str):
            return {"refused": "bad_params", "detail": "lsp-release takes {folder}"}
        if len(folder) > MAX_FOLDER_CHARS or "\0" in folder or not os.path.isabs(folder):
            return {"refused": "bad_params", "detail": "folder must be an absolute path"}
        from indus.service import lsp_host

        stopped = await lsp_host.release_servers(self.scope, os.path.normpath(folder))
        return {"stopped": stopped}


def _count(reply: ControlReply, verb: str) -> int | None:
    """``stopped`` from a daemon's answer; None when none answered, the verb is unknown there (an
    older daemon) or it was refused (logged)."""
    result = reply.result
    if not reply.ok or result is None:
        if reply.answered and reply.error not in ("unknown_verb", "unsupported"):
            log.info("the daemon did not take %s: %s", verb, reply.error)
        return None
    if "refused" in result:
        log.info("the daemon refused %s: %s", verb, result.get("detail"))
        return None
    value = result.get("stopped")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def restart_in_gateway(home: Path, *, query: Query | None = None) -> tuple[int, bool] | None:
    """Ask the daemon serving ``home`` to restart its language servers: ``(stopped, running)``,
    or None when no daemon that knows the verb answered."""
    ask = query if query is not None else gateway_control.query
    reply = ask(home, RESTART_VERB, {}, timeout=VERB_TIMEOUT_S + 2)
    stopped = _count(reply, RESTART_VERB)
    if stopped is None:
        return None
    assert reply.result is not None
    return stopped, reply.result.get("running") is True


def release_in_gateway(home: Path, folder: str | Path, *, query: Query | None = None) -> int | None:
    """Ask the daemon serving ``home`` to stop its servers of ``folder`` before it is removed;
    how many stopped, or None when no daemon that knows the verb answered. Never raises."""
    ask = query if query is not None else gateway_control.query
    path = os.path.abspath(str(folder))
    try:
        reply = ask(home, RELEASE_VERB, {"folder": path}, timeout=VERB_TIMEOUT_S + 2)
    except Exception:
        log.warning("could not ask the daemon to release %s", path, exc_info=True)
        return None
    return _count(reply, RELEASE_VERB)


def gateway_releaser(home: Path, *, query: Query | None = None) -> Callable[[str], object]:
    """``before_remove`` for ``backends.worktrees.remove`` / ``prune`` in the CLI: the daemon
    serving ``home`` stops its servers of the tree first (nothing when no daemon answers)."""

    def release(path: str) -> None:
        stopped = release_in_gateway(home, path, query=query)
        if stopped:
            log.info("the gateway stopped %d language server(s) of %s", stopped, path)

    return release
