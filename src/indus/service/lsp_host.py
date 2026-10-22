# pyright: strict
"""The host side of language servers for edits (P24-15/16 wiring, D-16.799).

A process holds at most one language-server tier: one ``LspManager`` wrapped in one
``EditDiagnostics`` and installed with ``tools.lsp.diagnostics.install_active`` (the checker
``lint.lint_edit`` and the LSP stop gate consult). Every host that runs agents makes one
``LspUpkeep`` per session service (``SessionService(lsp=...)``); the services of one process (the
gateway's chat and surface services, several profiles) share the tier:

- ``start()``: the first upkeep whose profile has ``lsp.enabled`` builds and installs the tier;
  later ones join it (a count of users). Nothing starts a server here: a server is spawned on the
  first edit of a file it handles, inside a git work tree, when its binary is already installed.
- ``trust(cwd)``: a session's own work tree (the git root of its folder, else the folder) is
  trusted for servers that run project code (rust-analyzer, gopls, …).
- ``release(folder)``: stop the servers of a folder about to disappear (a removed worktree).
- ``shutdown()``: the last user out stops every server and uninstalls the tier.

The tier's servers get their environment from the child-env builder of the profile that started
it (R02.22: no provider key reaches a server). With ``lsp.enabled: false`` the upkeep does nothing;
the edit seam and the stop gates still run (they record which files a turn edited for
verify-on-stop), they just never hear from a server.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from indus.foundation.scope import Scope
from indus.tools.lsp import diagnostics
from indus.tools.lsp.diagnostics import EditDiagnostics
from indus.tools.lsp.manager import LspManager, LspSettings
from indus.tools.lsp.servers import find_git_root

__all__ = ["LspUpkeep", "settings_for"]

log = logging.getLogger(__name__)


@dataclass
class _Tier:
    manager: LspManager
    checker: EditDiagnostics
    users: int = 0


_TIER: list[_Tier] = []


def settings_for(scope: Scope) -> LspSettings:
    """The profile's ``lsp`` settings (the seam tests replace to keep real servers away)."""
    return LspSettings.from_scope(scope)


class LspUpkeep:
    """One session service's share of the process's language-server tier (module notes)."""

    def __init__(self, scope: Scope, *, settings: LspSettings | None = None) -> None:
        self.scope = scope
        self.settings = settings or settings_for(scope)
        self._joined = False

    @property
    def manager(self) -> LspManager | None:
        return _TIER[0].manager if self._joined and _TIER else None

    def start(self) -> None:
        """Join (or build) the process's tier; once per upkeep, never raises."""
        if self._joined or not self.settings.enabled:
            return
        try:
            if not _TIER:
                manager = LspManager(self.settings, scope=self.scope)
                checker = EditDiagnostics(manager)
                diagnostics.install_active(checker)
                _TIER.append(_Tier(manager, checker))
                log.debug("language-server checks on for this process")
            _TIER[0].users += 1
            self._joined = True
        except Exception:
            log.warning("language-server checks could not start", exc_info=True)

    def trust(self, cwd: str | Path | None) -> None:
        """Trust the session folder's work tree for servers that run project code."""
        manager = self.manager
        if manager is None or not cwd:
            return
        folder = os.path.abspath(os.path.expanduser(str(cwd)))
        manager.trust(find_git_root(folder) or folder)

    async def release(self, folder: str | Path) -> int:
        """Stop the servers below ``folder`` (call before removing it); how many stopped."""
        manager = self.manager
        return await manager.release(str(folder)) if manager is not None else 0

    async def shutdown(self) -> None:
        """Leave the tier; the last user stops every server and uninstalls it. Never raises."""
        if not self._joined:
            return
        self._joined = False
        if not _TIER:
            return
        tier = _TIER[0]
        tier.users -= 1
        if tier.users > 0:
            return
        _TIER.clear()
        if diagnostics.active() is tier.checker:
            diagnostics.install_active(None)
        try:
            await tier.manager.close()
        except Exception:
            log.warning("stopping the language servers failed", exc_info=True)
