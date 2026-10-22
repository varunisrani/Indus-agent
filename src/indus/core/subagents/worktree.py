"""A git worktree per subagent, when trusted config asks for one (P10-07, C37).

With ``delegation.worktree: true`` (or the trusted per-call flag of an internal caller) each child
works in a fresh worktree of the repository holding the parent's working folder, made by the P11
worktree module (``backends.worktrees.create``) as ``.worktrees/subagent-<id>`` on branch
``indus/subagent-<id>``, from the parent's ``HEAD`` (so the child sees the parent's local commits;
no fetch). Only the local terminal backend sees host folders, so on any other backend the request
is ignored with a note. A folder outside a git repository, or a failed create, also gives a note;
the child then works in the parent's folder.

``open_worktree`` returns ``(ChildWorktree | None, note)``; ``scope_in_tree`` gives the child a
scope whose ``terminal.cwd`` is the tree, so its terminal and file tools work there.
``close_worktree`` stops the process's language servers rooted in the tree (wave 45), then applies
P11's retention rule once the child has ended: a tree with commits or
changes is kept (and the reason given), a clean one is removed with its branch. The result names
the tree either way.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from indus.backends import worktrees
from indus.foundation.blocking import run_blocking
from indus.foundation.config.schema import default_for
from indus.foundation.scope import ConfigSource, Scope

__all__ = [
    "ChildWorktree",
    "ConfigOverlay",
    "backend_is_local",
    "close_worktree",
    "open_worktree",
    "scope_in_tree",
]

log = logging.getLogger(__name__)

NAME_PREFIX = "subagent-"
NOT_LOCAL_NOTE = (
    "delegation.worktree is on, but the terminal backend is {backend}, which cannot see host "
    "worktrees; the subagent worked in the parent's folder."
)
NO_REPO_NOTE = (
    "delegation.worktree is on, but {folder} is not inside a git repository; the subagent worked "
    "in the parent's folder."
)
FAILED_NOTE = "No worktree could be made ({reason}); the subagent worked in the parent's folder."


@dataclass(frozen=True)
class ChildWorktree:
    path: str
    branch: str
    repo: str

    def describe(self, *, kept: bool, reason: str = "") -> dict[str, Any]:
        entry: dict[str, Any] = {"path": self.path, "branch": self.branch, "kept": kept}
        if reason:
            entry["kept_because"] = reason
        return entry


class ConfigOverlay:
    """A config view with a few dotted paths answered from ``values`` first."""

    def __init__(self, base: ConfigSource | None, values: Mapping[str, object]) -> None:
        self._base = base
        self._values = dict(values)

    def get(self, path: str, /) -> object:
        if path in self._values:
            return self._values[path]
        return self._base.get(path) if self._base is not None else None


def scope_in_tree(scope: Scope, tree: ChildWorktree) -> Scope:
    """The child's scope: its terminal and file tools start in the worktree."""
    return scope.with_config(ConfigOverlay(scope.config, {"terminal.cwd": tree.path}))


def backend_is_local(scope: Scope) -> tuple[bool, str]:
    """``(True, "local")`` when the terminal backend runs on this machine."""
    value: object = None
    if scope.config is not None:
        try:
            value = scope.config.get("terminal.backend")
        except Exception:
            value = None
    name = str(value if value is not None else default_for("terminal.backend", "local"))
    name = name.strip().lower() or "local"
    return name == "local", name


def _open(
    scope: Scope, folder: Path, subagent_id: str, session_id: str, git: worktrees.Git | None
) -> tuple[ChildWorktree | None, str]:
    runner = git if git is not None else worktrees.git_for(scope)
    if worktrees.repo_root(runner, folder) is None:
        return None, NO_REPO_NOTE.format(folder=folder)
    try:
        info = worktrees.create(
            scope.with_config(ConfigOverlay(scope.config, {"worktrees.sync_base": False})),
            folder,
            NAME_PREFIX + subagent_id,
            session_id=session_id,
            git=runner,
        )
    except worktrees.WorktreeError as exc:
        return None, FAILED_NOTE.format(reason=exc)
    return ChildWorktree(info.path, info.branch, info.repo), ""


async def open_worktree(
    scope: Scope,
    folder: Path,
    subagent_id: str,
    *,
    session_id: str,
    git: worktrees.Git | None = None,
) -> tuple[ChildWorktree | None, str]:
    """A fresh tree for one child, or ``(None, note)`` saying why there is none."""
    local, backend = backend_is_local(scope)
    if not local:
        return None, NOT_LOCAL_NOTE.format(backend=backend)
    return await run_blocking(
        lambda: _open(scope, folder, subagent_id, session_id, git), label="subagent worktree"
    )


async def _release_language_servers(folder: str | Path) -> None:
    """Stop this process's language servers rooted in ``folder`` (only when a host installed the
    tier, so the LSP modules are never loaded for this)."""
    if "indus.tools.lsp.diagnostics" not in sys.modules:
        return
    from indus.tools.lsp import diagnostics

    stopped = await diagnostics.release_folder(str(folder))
    if stopped:
        log.info("stopped %d language server(s) of the worktree %s", stopped, folder)


async def close_worktree(
    scope: Scope, tree: ChildWorktree, *, git: worktrees.Git | None = None
) -> Mapping[str, Any]:
    """Keep the tree when it holds work, else remove it (P11 rule); returns its description.
    The child has ended: the process's language servers of the tree stop first (wave 45)."""
    await _release_language_servers(tree.path)

    def run() -> Mapping[str, Any]:
        try:
            kept = worktrees.remove(scope, tree.repo, tree.path, git=git)
        except Exception as exc:  # fail safe toward keeping
            log.warning("could not check the worktree %s: %s", tree.path, exc)
            kept = f"it could not be checked ({exc})"
        return tree.describe(kept=kept is not None, reason=kept or "")

    return await run_blocking(run, label="subagent worktree")
