"""``projects.*``: the projects methods the desktop sidebar and the TUI call (P24-14).

``PROJECT_METHODS`` lists ``(name, params, result, extra errors)`` rows in the shape of the other
method groups; the handlers live in ``indus.service.projects_rpc``. Adding the rows to the method
catalogue (and the generated TypeScript) is a separate wiring step.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from indus.protocol.base import ProtocolModel
from indus.protocol.errors import ErrorCode

__all__ = [
    "PROJECT_METHODS",
    "DiscoveredRepo",
    "ProjectFolderInfo",
    "ProjectInfo",
    "ProjectResult",
    "ProjectSummary",
    "ProjectsAddPathParams",
    "ProjectsArchiveParams",
    "ProjectsCreateParams",
    "ProjectsDiscoverParams",
    "ProjectsDiscoverResult",
    "ProjectsForPathParams",
    "ProjectsGetParams",
    "ProjectsListParams",
    "ProjectsListResult",
    "ProjectsRemovePathParams",
    "ProjectsUpdateParams",
    "ProjectsUseParams",
    "ProjectsUseResult",
]

FolderKind = Literal["folder", "repo", "worktree"]


class ProjectFolderInfo(ProtocolModel):
    """One folder a project owns."""

    path: str
    """Absolute folder path."""
    kind: FolderKind
    """``folder``, ``repo`` or ``worktree``."""
    primary: bool
    """True for the project's main folder."""
    label: str | None = None
    """An optional display label."""


class ProjectInfo(ProtocolModel):
    """A project with its folders and the sessions that joined it."""

    id: str
    """The project id (``p_…``)."""
    slug: str
    """The unique short name."""
    name: str
    """The display name."""
    description: str | None = None
    """What the project is about."""
    icon: str | None = None
    """An icon name or emoji."""
    colour: str | None = None
    """A display colour."""
    board_slug: str | None = None
    """The task board bound to this project."""
    primary_path: str | None = None
    """The main folder."""
    archived: bool = False
    """True when hidden from the sidebar."""
    folders: list[ProjectFolderInfo] = Field(default_factory=list[ProjectFolderInfo])
    """Every folder the project owns, main folder first."""
    sessions: list[str] = Field(default_factory=list[str])
    """Ids of sessions that started inside the project's folders."""


class ProjectSummary(ProtocolModel):
    """One row of the projects list."""

    id: str
    """The project id."""
    slug: str
    """The unique short name."""
    name: str
    """The display name."""
    primary_path: str | None = None
    """The main folder."""
    folders: int
    """How many folders the project owns."""
    archived: bool
    """True when archived."""
    active: bool
    """True for the profile's active project."""


class ProjectsListParams(ProtocolModel):
    """List the profile's projects."""

    include_archived: bool = False
    """Also list archived projects."""


class ProjectsListResult(ProtocolModel):
    """The projects, oldest first."""

    projects: list[ProjectSummary]
    """One row per project."""
    active_id: str | None = None
    """The active project, if any."""


class ProjectsGetParams(ProtocolModel):
    """Read one project by id, slug or name."""

    project: str = Field(min_length=1)
    """Id, slug or name."""


class ProjectResult(ProtocolModel):
    """The project after the call."""

    project: ProjectInfo
    """The project."""


class ProjectsCreateParams(ProtocolModel):
    """Create a project."""

    name: str = Field(min_length=1)
    """The display name."""
    folders: list[str] = Field(default_factory=list[str])
    """Folders it owns (the first is the main folder unless ``primary`` is given)."""
    primary: str | None = None
    """The main folder."""
    slug: str | None = None
    """A short name; made from the name when left out."""
    description: str | None = None
    """What the project is about."""
    icon: str | None = None
    """An icon name or emoji."""
    colour: str | None = None
    """A display colour."""
    board_slug: str | None = None
    """A task board to bind."""
    use: bool = False
    """Also make it the active project."""


class ProjectsUpdateParams(ProtocolModel):
    """Change a project's fields; ``""`` clears icon, colour and board."""

    project: str = Field(min_length=1)
    """Id, slug or name."""
    name: str | None = None
    """A new display name."""
    description: str | None = None
    """A new description."""
    icon: str | None = None
    """A new icon."""
    colour: str | None = None
    """A new colour."""
    board_slug: str | None = None
    """A new bound board."""


class ProjectsAddPathParams(ProtocolModel):
    """Add a folder to a project."""

    project: str = Field(min_length=1)
    """Id, slug or name."""
    path: str = Field(min_length=1)
    """The folder."""
    kind: FolderKind = "folder"
    """``folder``, ``repo`` or ``worktree``."""
    primary: bool = False
    """Make it the main folder."""
    label: str | None = None
    """An optional display label."""


class ProjectsRemovePathParams(ProtocolModel):
    """Remove a folder from a project (the main folder moves to the next one)."""

    project: str = Field(min_length=1)
    """Id, slug or name."""
    path: str = Field(min_length=1)
    """The folder."""


class ProjectsArchiveParams(ProtocolModel):
    """Archive (or restore) a project."""

    project: str = Field(min_length=1)
    """Id, slug or name."""
    archived: bool = True
    """False restores it."""


class ProjectsUseParams(ProtocolModel):
    """Set the active project; leave ``project`` out to clear it."""

    project: str | None = None
    """Id, slug or name."""


class ProjectsUseResult(ProtocolModel):
    """The active (or found) project, if any."""

    project: ProjectInfo | None = None
    """The project, or nothing."""


class ProjectsForPathParams(ProtocolModel):
    """The project owning a folder; with ``session_id`` the session joins it."""

    path: str = Field(min_length=1)
    """A folder (a session's working folder)."""
    session_id: str | None = None
    """A session started in that folder."""


class ProjectsDiscoverParams(ProtocolModel):
    """Find git repositories under the scan roots (the profile's discovery settings)."""


class DiscoveredRepo(ProtocolModel):
    """One repository found by the scan."""

    root: str
    """The repository folder."""
    label: str
    """Its folder name."""


class ProjectsDiscoverResult(ProtocolModel):
    """The repositories found, sorted by path."""

    repos: list[DiscoveredRepo]
    """The repositories."""


type _Row = tuple[str, type[ProtocolModel], type[ProtocolModel], tuple[ErrorCode, ...]]

PROJECT_METHODS: tuple[_Row, ...] = (
    ("projects.list", ProjectsListParams, ProjectsListResult, ()),
    ("projects.get", ProjectsGetParams, ProjectResult, ()),
    ("projects.create", ProjectsCreateParams, ProjectResult, ()),
    ("projects.update", ProjectsUpdateParams, ProjectResult, ()),
    ("projects.add_path", ProjectsAddPathParams, ProjectResult, ()),
    ("projects.remove_path", ProjectsRemovePathParams, ProjectResult, ()),
    ("projects.archive", ProjectsArchiveParams, ProjectResult, ()),
    ("projects.use", ProjectsUseParams, ProjectsUseResult, ()),
    ("projects.for_path", ProjectsForPathParams, ProjectsUseResult, ()),
    ("projects.discover", ProjectsDiscoverParams, ProjectsDiscoverResult, ()),
)
"""``(name, params, result, extra errors)`` for the projects methods, in declaration order."""
