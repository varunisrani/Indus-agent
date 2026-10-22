# pyright: strict
"""The host side of language servers for edits (P24-15/16 wiring, D-16.799).

A process holds one language-server tier per profile it serves (wave 45, SEC-14): one
``LspManager`` wrapped in one ``EditDiagnostics``, installed with
``tools.lsp.diagnostics.install_active`` under the profile's tier key (its home; the checker
``lint.lint_edit`` and the LSP stop gate consult is the one of the edit's profile). Every host that
runs agents makes one ``LspUpkeep`` per session service (``SessionService(lsp=...)``); the services
of one profile in one process (the gateway's chat and surface services) share its tier:

- ``start()``: the first upkeep of a profile with ``lsp.enabled`` builds and installs that
  profile's tier from its own scope and settings; later ones of the same profile join it (a count
  of users). Nothing starts a server here: a server is spawned on the first edit of a file it
  handles, inside a git work tree, when its binary is already installed.
- ``trust(cwd)``: a session's own work tree (the git root of its folder, else the folder) is
  trusted for servers that run project code (rust-analyzer, gopls, …).
- ``release(folder)``: stop the servers of a folder about to disappear (a removed worktree).
- ``shutdown()``: the last user of a tier stops its servers and uninstalls it.

Entry points for a profile's daemon (wave 45, ``service.lsp_control``): ``restart_servers(scope)``
stops every server of the tier serving ``scope``'s profile and forgets their failures (the next
edit starts them again; ``indus lsp restart``), ``release_servers(scope, folder)`` stops its
servers of a folder about to be removed (a worktree the CLI removes). Both reach only that
profile's tier; another served profile's servers are left alone.

A tier's servers get their environment from the child-env builder of its own profile (R02.22: no
provider key reaches a server) and its own ``lsp.servers`` settings: on a multiplex host profile
B's servers never run with profile A's passthrough values or settings. With ``lsp.enabled: false``
the upkeep does nothing (that profile's edits are not checked, even when another served profile
has a tier); the edit seam and the stop gates still run (they record which files a turn edited for
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

__all__ = ["LspUpkeep", "release_servers", "restart_servers", "settings_for"]

log = logging.getLogger(__name__)


@dataclass
class _Tier:
    manager: LspManager
    checker: EditDiagnostics
    users: int = 0


_TIERS: dict[str, _Tier] = {}  # by tier key (the profile's home)


def settings_for(scope: Scope) -> LspSettings:
    """The profile's ``lsp`` settings (the seam tests replace to keep real servers away)."""
    return LspSettings.from_scope(scope)


class LspUpkeep:
    """One session service's share of its profile's language-server tier (module notes)."""

    def __init__(self, scope: Scope, *, settings: LspSettings | None = None) -> None:
        self.scope = scope
        self.settings = settings or settings_for(scope)
        self.key = diagnostics.tier_key(scope) or ""
        self._joined = False

    @property
    def manager(self) -> LspManager | None:
        tier = _TIERS.get(self.key) if self._joined else None
        return tier.manager if tier is not None else None

    def start(self) -> None:
        """Join (or build) this profile's tier; once per upkeep, never raises."""
        if self._joined or not self.settings.enabled:
            return
        try:
            tier = _TIERS.get(self.key)
            if tier is None:
                manager = LspManager(self.settings, scope=self.scope)
                tier = _Tier(manager, EditDiagnostics(manager))
                diagnostics.install_active(tier.checker, key=self.key)
                _TIERS[self.key] = tier
                log.debug("language-server checks on for profile %s", self.scope.profile_id)
            tier.users += 1
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
        """Leave the tier; its last user stops its servers and uninstalls it. Never raises."""
        if not self._joined:
            return
        self._joined = False
        tier = _TIERS.get(self.key)
        if tier is None:
            return
        tier.users -= 1
        if tier.users > 0:
            return
        del _TIERS[self.key]
        if diagnostics.installed().get(self.key) is tier.checker:
            diagnostics.install_active(None, key=self.key)
        try:
            await tier.manager.close()
        except Exception:
            log.warning("stopping the language servers failed", exc_info=True)


def _manager_for(scope: Scope) -> LspManager | None:
    """The manager of the tier serving ``scope``'s profile in this process, if one runs (one tier
    per profile, keyed by its home: SEC-14; another profile's tier is never returned)."""
    tier = _TIERS.get(diagnostics.tier_key(scope) or "")
    return tier.manager if tier is not None else None


async def restart_servers(scope: Scope) -> int | None:
    """Stop every server of ``scope``'s tier and forget their failures; how many stopped, or None
    when no tier runs here. Never raises."""
    manager = _manager_for(scope)
    if manager is None:
        return None
    try:
        return await manager.restart()
    except Exception:
        log.warning("restarting the language servers failed", exc_info=True)
        return 0


async def release_servers(scope: Scope, folder: str | Path) -> int:
    """Stop ``scope``'s tier's servers rooted at or below ``folder`` (call before removing it);
    how many stopped. Never raises."""
    manager = _manager_for(scope)
    if manager is None:
        return 0
    try:
        return await manager.release(str(folder))
    except Exception:
        log.warning("releasing the language servers of %s failed", folder, exc_info=True)
        return 0
