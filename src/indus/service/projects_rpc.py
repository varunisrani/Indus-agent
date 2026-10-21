"""``projects.*`` handlers over the profile's ``ProjectStore`` (P24-14).

Each handler is ``async handler(context, params) -> result`` like every other method table entry:
the context carries the caller's ``Scope``, the store work runs off the event loop
(``run_blocking``), and a project that is not found or a value that breaks a rule is answered with
``INVALID_PARAMS`` and the reason. ``PROJECT_HANDLERS`` maps the method names of
``indus.protocol.projects.PROJECT_METHODS`` to their handlers; ``register(table)`` adds them to a
dispatcher table once the names are in the method catalogue.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from indus.foundation import config
from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.protocol.base import ProtocolModel
from indus.protocol.errors import INVALID_PARAMS
from indus.protocol.methods import ProtocolError
from indus.protocol.projects import (
    DiscoveredRepo,
    ProjectFolderInfo,
    ProjectInfo,
    ProjectResult,
    ProjectsAddPathParams,
    ProjectsArchiveParams,
    ProjectsCreateParams,
    ProjectsDiscoverParams,
    ProjectsDiscoverResult,
    ProjectsForPathParams,
    ProjectsGetParams,
    ProjectsListParams,
    ProjectsListResult,
    ProjectsRemovePathParams,
    ProjectSummary,
    ProjectsUpdateParams,
    ProjectsUseParams,
    ProjectsUseResult,
)
from indus.state.projects import (
    DiscoveryPolicy,
    Project,
    ProjectsError,
    ProjectStore,
    discover_repos,
    project_rows,
)

__all__ = [
    "PROJECT_HANDLERS",
    "ProjectsContext",
    "info_of",
    "join_session",
    "list_result",
    "register",
]

log = logging.getLogger(__name__)


class ProjectsContext(Protocol):
    @property
    def scope(self) -> Scope: ...


def info_of(project: Project) -> ProjectInfo:
    return ProjectInfo(
        id=project.id,
        slug=project.slug,
        name=project.name,
        description=project.description,
        icon=project.icon,
        colour=project.colour,
        board_slug=project.board_slug,
        primary_path=project.primary_path,
        archived=project.archived,
        folders=[
            ProjectFolderInfo(path=f.path, kind=f.kind, primary=f.primary, label=f.label)  # type: ignore[arg-type]
            for f in project.folders
        ],
        sessions=list(project.sessions),
    )


def list_result(store: ProjectStore, *, include_archived: bool = False) -> ProjectsListResult:
    """The list both ``projects.list`` and ``indus project list --json`` print."""
    active = store.active()
    rows = project_rows(store.list(include_archived=include_archived), active)
    return ProjectsListResult(
        projects=[ProjectSummary(**dict(row)) for row in rows],
        active_id=active.id if active is not None else None,
    )


def join_session(scope: Scope, session_id: str, cwd: str | None) -> str | None:
    """Record a new session in the project owning ``cwd`` (``ProjectStore.join_session``); the id
    of that project, or None. Profiles that never made a project have no ``projects.db`` and are
    left alone (no file is created); a failure is logged, never raised."""
    if not cwd or not (scope.home / "projects.db").is_file():
        return None
    try:
        store = ProjectStore(scope)
        try:
            project = store.join_session(session_id, cwd)
        finally:
            store.close()
    except Exception:
        log.warning("the session could not join its project", exc_info=True)
        return None
    return project.id if project is not None else None


async def _with_store[T](scope: Scope, fn: Callable[[ProjectStore], T]) -> T:
    def call() -> T:
        store = ProjectStore(scope)
        try:
            return fn(store)
        except ProjectsError as exc:
            raise ProtocolError(INVALID_PARAMS, str(exc)) from exc
        finally:
            store.close()

    return await run_blocking(call, label="projects rpc")


async def projects_list(ctx: ProjectsContext, params: ProjectsListParams) -> ProjectsListResult:
    return await _with_store(
        ctx.scope, lambda s: list_result(s, include_archived=params.include_archived)
    )


async def projects_get(ctx: ProjectsContext, params: ProjectsGetParams) -> ProjectResult:
    return await _with_store(
        ctx.scope, lambda s: ProjectResult(project=info_of(s.get(params.project)))
    )


async def projects_create(ctx: ProjectsContext, params: ProjectsCreateParams) -> ProjectResult:
    def fn(store: ProjectStore) -> ProjectResult:
        made = store.create(
            params.name,
            folders=params.folders,
            primary=params.primary,
            slug=params.slug,
            description=params.description,
            icon=params.icon,
            colour=params.colour,
            board_slug=params.board_slug,
        )
        if params.use:
            store.use(made.id)
        return ProjectResult(project=info_of(made))

    return await _with_store(ctx.scope, fn)


async def projects_update(ctx: ProjectsContext, params: ProjectsUpdateParams) -> ProjectResult:
    def fn(store: ProjectStore) -> ProjectResult:
        updated = store.update(
            params.project,
            name=params.name,
            description=params.description,
            icon=params.icon,
            colour=params.colour,
            board_slug=params.board_slug,
        )
        return ProjectResult(project=info_of(updated))

    return await _with_store(ctx.scope, fn)


async def projects_add_path(ctx: ProjectsContext, params: ProjectsAddPathParams) -> ProjectResult:
    def fn(store: ProjectStore) -> ProjectResult:
        project = store.add_path(
            params.project,
            params.path,
            kind=params.kind,
            primary=params.primary,
            label=params.label,
        )
        return ProjectResult(project=info_of(project))

    return await _with_store(ctx.scope, fn)


async def projects_remove_path(
    ctx: ProjectsContext, params: ProjectsRemovePathParams
) -> ProjectResult:
    return await _with_store(
        ctx.scope,
        lambda s: ProjectResult(project=info_of(s.remove_path(params.project, params.path))),
    )


async def projects_archive(ctx: ProjectsContext, params: ProjectsArchiveParams) -> ProjectResult:
    return await _with_store(
        ctx.scope,
        lambda s: ProjectResult(
            project=info_of(s.archive(params.project, archived=params.archived))
        ),
    )


async def projects_use(ctx: ProjectsContext, params: ProjectsUseParams) -> ProjectsUseResult:
    def fn(store: ProjectStore) -> ProjectsUseResult:
        chosen = store.use(params.project)
        return ProjectsUseResult(project=info_of(chosen) if chosen is not None else None)

    return await _with_store(ctx.scope, fn)


async def projects_for_path(
    ctx: ProjectsContext, params: ProjectsForPathParams
) -> ProjectsUseResult:
    def fn(store: ProjectStore) -> ProjectsUseResult:
        if params.session_id:
            found = store.join_session(params.session_id, params.path)
        else:
            found = store.for_path(params.path)
        return ProjectsUseResult(project=info_of(found) if found is not None else None)

    return await _with_store(ctx.scope, fn)


def _user_home(scope: Scope) -> Path:
    text = scope.env.get("HOME") or scope.env.get("USERPROFILE")
    return Path(text) if text else scope.home.parent


async def projects_discover(
    ctx: ProjectsContext, params: ProjectsDiscoverParams
) -> ProjectsDiscoverResult:
    scope = ctx.scope

    def scan() -> ProjectsDiscoverResult:
        policy = DiscoveryPolicy.from_config(config.load(scope))
        repos = discover_repos(policy, user_home=_user_home(scope))
        return ProjectsDiscoverResult(repos=[DiscoveredRepo(**r) for r in repos])

    return await run_blocking(scan, label="projects discover")


type _Handler = Callable[[Any, Any], Awaitable[ProtocolModel]]

PROJECT_HANDLERS: Mapping[str, _Handler] = {
    "projects.list": projects_list,
    "projects.get": projects_get,
    "projects.create": projects_create,
    "projects.update": projects_update,
    "projects.add_path": projects_add_path,
    "projects.remove_path": projects_remove_path,
    "projects.archive": projects_archive,
    "projects.use": projects_use,
    "projects.for_path": projects_for_path,
    "projects.discover": projects_discover,
}


def register(table: Any) -> None:
    """Add every handler to a ``MethodTable`` (the names must be in the method catalogue)."""
    for name, handler in PROJECT_HANDLERS.items():
        table.add(name, handler)
