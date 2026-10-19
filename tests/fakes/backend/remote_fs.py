"""A non-local terminal backend over an in-memory file tree, for path completion (P12-31, R05.13).

``RemoteTree`` holds folders as nested dicts (a dict is a folder, ``None`` a file). Its
``Environment`` answers only the bridge's listing command (``sh -c <script> sh <folder>``) by
printing the folder's entries as the script would (``name`` or ``name/``); anything else exits 127.
``registry_with(tree)`` is an ``EnvironmentRegistry`` whose ``remotefs`` backend serves the tree.
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass, field
from typing import Any

from indus.backends.contract import BackendFlags, ExecResult
from indus.backends.registry import BackendEntry, CreateRequest, EnvironmentRegistry

BACKEND = "remotefs"

type Tree = dict[str, Any]


@dataclass
class RemoteTree:
    root: Tree
    home: str = "/home/remote"
    commands: list[str] = field(default_factory=list[str])

    def folder(self, path: str) -> Tree | None:
        if path == "~" or path.startswith("~/"):
            path = self.home + path[1:]
        node: Any = self.root
        for part in [p for p in posixpath.normpath(path).split("/") if p]:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, dict) else None


class RemoteEnvironment:
    """Just enough of ``Environment`` for the bridge's listing call."""

    flags = BackendFlags(isolated=True, container_paths=True)

    def __init__(self, tree: RemoteTree) -> None:
        self.tree = tree

    async def execute(self, command: str, **_options: Any) -> ExecResult:
        self.tree.commands.append(command)
        words = shlex.split(command)
        if words[:2] != ["sh", "-c"] or len(words) != 5:
            return ExecResult(output="unknown command", returncode=127)
        folder = self.tree.folder(words[4])
        if folder is None:
            return ExecResult(output="", returncode=0)
        lines = [
            f"{name}/" if isinstance(node, dict) else name for name, node in sorted(folder.items())
        ]
        return ExecResult(output="\n".join(lines) + ("\n" if lines else ""), returncode=0)

    async def cleanup(self) -> None:
        return None


def registry_with(tree: RemoteTree) -> EnvironmentRegistry:
    async def create(request: CreateRequest) -> Any:
        return RemoteEnvironment(tree)

    entry = BackendEntry(BACKEND, create, BackendFlags(isolated=True, container_paths=True))
    return EnvironmentRegistry([entry])
