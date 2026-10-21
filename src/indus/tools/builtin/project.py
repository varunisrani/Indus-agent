"""``project_info``: what project the session works in and which folders it owns (P24-14).

``action`` is ``current`` (default: the project owning the session's folder, else the profile's
active project), ``list`` (every project, active one marked) or ``show`` (one project by id, slug
or name). Read-only: creating and switching projects is the person's (``indus project``, the
desktop sidebar). The ``project`` toolset is opt-in; the desktop and TUI enable it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from indus.foundation.scope import Scope, scope_or_none
from indus.state.projects import ProjectsError, ProjectStore, project_rows
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = ["SCHEMA", "TOOLSET", "TOOL_NAME", "project_info", "register_tools"]

TOOL_NAME = "project_info"
TOOLSET = "project"
ACTIONS = ("current", "list", "show")

SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Look up projects (named workspaces that own folders): 'current' gives the project of "
        "this session's folder, 'list' every project, 'show' one by id, slug or name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "project": {"type": "string", "description": "With 'show': id, slug or name."},
        },
        "required": [],
    },
}


def _json(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def project_info(
    args: Mapping[str, Any], *, scope: Scope | None = None, cwd: str | None = None
) -> str:
    current = scope if scope is not None else scope_or_none()
    if current is None:
        return _json(error="no profile is active, so there are no projects to show")
    action = str(args.get("action") or "current").strip().lower()
    if action not in ACTIONS:
        return _json(error=f"action must be one of: {', '.join(ACTIONS)}")
    store = ProjectStore(current)
    try:
        if action == "list":
            active = store.active()
            return _json(projects=project_rows(store.list(), active))
        if action == "show":
            ref = str(args.get("project") or "").strip()
            if not ref:
                return _json(error="'show' needs the project id, slug or name")
            return _json(project=store.get(ref).view())
        folder = cwd or current.env.get("PWD") or os.getcwd()
        found = store.for_path(folder)
        source = "folder"
        if found is None:
            found = store.active()
            source = "active"
        if found is None:
            return _json(project=None, note="this session is not inside a project folder")
        return _json(project=found.view(), source=source)
    except ProjectsError as exc:
        return _json(error=str(exc))
    finally:
        store.close()


def register_tools(registry: ToolRegistry) -> None:
    registry.register(TOOL_NAME, TOOLSET, SCHEMA, project_info, traits=ToolTraits(read_only=True))
