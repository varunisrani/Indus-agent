"""P24-14: projects — folders, active project, sessions joining, repo discovery."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.protocol.methods import ProtocolError
from indus.protocol.projects import (
    PROJECT_METHODS,
    ProjectsAddPathParams,
    ProjectsCreateParams,
    ProjectsDiscoverParams,
    ProjectsForPathParams,
    ProjectsGetParams,
    ProjectsListParams,
    ProjectsUseParams,
)
from indus.service import projects_rpc
from indus.state.projects import (
    DiscoveryPolicy,
    ProjectInputError,
    ProjectNotFound,
    ProjectStore,
    discover_repos,
    path_is_inside,
)
from indus.tools.builtin import project as project_tool


@pytest.fixture
def store(scope: Scope) -> Iterator[ProjectStore]:
    opened = ProjectStore(scope)
    yield opened
    opened.close()


def _dirs(base: Path, *names: str) -> list[Path]:
    made = []
    for name in names:
        folder = base / name
        folder.mkdir(parents=True, exist_ok=True)
        made.append(folder)
    return made


def test_create_and_slug_collisions(store: ProjectStore, tmp_path: Path) -> None:
    app, docs = _dirs(tmp_path, "app", "docs")
    first = store.create("My App", folders=[app, docs], description="the app")
    assert first.slug == "my-app" and first.id.startswith("p_")
    assert first.primary_path == str(app)
    assert [(f.path, f.primary) for f in first.folders] == [(str(app), True), (str(docs), False)]
    second = store.create("My App")
    assert second.slug == "my-app-2"
    assert store.get("my-app").id == first.id
    assert store.get(first.id).id == first.id
    assert store.get("MY APP").id == first.id
    with pytest.raises(ProjectInputError, match="already the main folder"):
        store.create("Again", folders=[app])
    with pytest.raises(ProjectInputError):
        store.create("   ")
    with pytest.raises(ProjectNotFound):
        store.get("nothing")
    updated = store.update("my-app", name="Renamed", icon="rocket", board_slug="ops")
    assert (updated.name, updated.icon, updated.board_slug) == ("Renamed", "rocket", "ops")
    assert store.update("my-app", icon="").icon is None


def test_removing_the_primary_moves_it(store: ProjectStore, tmp_path: Path) -> None:
    a, b = _dirs(tmp_path, "a", "b")
    project = store.create("p", folders=[a])
    project = store.add_path("p", b, kind="repo")
    assert project.primary_path == str(a)
    assert {f.path: f.kind for f in project.folders}[str(b)] == "repo"
    project = store.remove_path("p", a)
    assert project.primary_path == str(b) and [f.primary for f in project.folders] == [True]
    project = store.remove_path("p", b)
    assert project.primary_path is None and project.folders == ()
    with pytest.raises(ProjectInputError):
        store.remove_path("p", b)
    empty = store.create("q")
    assert store.add_path("q", a).primary_path == str(a)  # first folder becomes primary
    assert store.add_path(empty.id, b, primary=True).primary_path == str(b)


def test_session_inside_a_project_folder_joins_it(store: ProjectStore, tmp_path: Path) -> None:
    outer, inner, other = _dirs(tmp_path, "work", "work/sub/inner", "workbench")
    store.create("outer", folders=[outer])
    nested = store.create("inner", folders=[inner])
    joined = store.join_session("s1", inner / "src")
    assert joined is not None and joined.id == nested.id and joined.sessions == ("s1",)
    assert store.session_project("s1").id == nested.id  # type: ignore[union-attr]
    assert store.for_path(outer / "x").slug == "outer"  # type: ignore[union-attr]
    assert store.for_path(other) is None  # /workbench is not inside /work
    assert store.join_session("s2", other) is None
    assert path_is_inside(str(outer), str(outer)) and not path_is_inside("/a/b", "/a/bc")
    store.archive("inner")
    assert store.for_path(inner).slug == "outer"  # type: ignore[union-attr]


def test_active_project_per_profile(store: ProjectStore, tmp_path: Path) -> None:
    (app,) = _dirs(tmp_path, "app")
    made = store.create("app", folders=[app])
    assert store.active() is None
    assert store.use("app") is not None and store.active().id == made.id  # type: ignore[union-attr]
    store.archive("app")
    assert store.active() is None
    with pytest.raises(ProjectInputError, match="archived"):
        store.use("app")
    store.archive("app", archived=False)
    store.use("app")
    assert store.use(None) is None and store.active() is None
    assert [p.slug for p in store.list()] == ["app"]


def test_bare_chat_stays_detached(store: ProjectStore, tmp_path: Path) -> None:
    app, default = _dirs(tmp_path, "app", "scratch")
    assert store.start_folder() is None
    assert store.start_folder(default_dir=default) == str(default)
    store.create("app", folders=[app])
    assert store.start_folder(project="app") == str(app)
    store.use("app")
    assert store.start_folder(default_dir=default) == str(app)


def test_discovery_honours_depth_and_excludes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for repo in ("code/alpha", "code/nested/beta", "deep/a/b/c/gamma", "node_modules/pkg"):
        (home / repo / ".git").mkdir(parents=True)
    (home / "code/alpha/inner/.git").mkdir(parents=True)  # never walked: alpha is a repo
    (home / ".hidden/delta/.git").mkdir(parents=True)
    (home / "skipme/epsilon/.git").mkdir(parents=True)
    (home / "linked").symlink_to(home / "code")
    found = discover_repos(DiscoveryPolicy(), user_home=home)
    roots = [r["root"] for r in found]
    assert roots == [str(home / "code/alpha"), str(home / "code/nested/beta"),
                     str(home / "skipme/epsilon")]  # fmt: skip
    shallow = discover_repos(DiscoveryPolicy(max_depth=2), user_home=home)
    assert [r["label"] for r in shallow] == ["alpha", "epsilon"]
    excluded = discover_repos(
        DiscoveryPolicy(excludes=("skip*", str(home / "code/nested"))), user_home=home
    )
    # Our own excludes replace the default list, so node_modules is walked this time.
    assert [r["label"] for r in excluded] == ["alpha", "pkg"]
    rooted = discover_repos(DiscoveryPolicy(roots=("~/deep",), max_depth=4), user_home=home)
    assert [r["label"] for r in rooted] == ["gamma"]
    assert discover_repos(DiscoveryPolicy(enabled=False), user_home=home) == []
    assert len(discover_repos(DiscoveryPolicy(), user_home=home, max_dirs=2)) <= 1


@dataclass
class _Ctx:
    scope: Scope


def test_rpc_methods_follow_the_store(scope: Scope, tmp_path: Path) -> None:
    (app,) = _dirs(tmp_path, "app")
    ctx = _Ctx(scope)
    assert [row[0] for row in PROJECT_METHODS] == list(projects_rpc.PROJECT_HANDLERS)
    made = asyncio.run(
        projects_rpc.projects_create(
            ctx, ProjectsCreateParams(name="App", folders=[str(app)], use=True)
        )
    ).project
    listed = asyncio.run(projects_rpc.projects_list(ctx, ProjectsListParams()))
    assert listed.active_id == made.id and listed.projects[0].active
    got = asyncio.run(projects_rpc.projects_get(ctx, ProjectsGetParams(project="app")))
    assert got.project.primary_path == str(app)
    added = asyncio.run(
        projects_rpc.projects_add_path(
            ctx, ProjectsAddPathParams(project="app", path=str(tmp_path / "x"), kind="worktree")
        )
    )
    assert [f.kind for f in added.project.folders] == ["folder", "worktree"]
    found = asyncio.run(
        projects_rpc.projects_for_path(
            ctx, ProjectsForPathParams(path=str(app / "src"), session_id="sess-1")
        )
    )
    assert found.project is not None and found.project.sessions == ["sess-1"]
    with pytest.raises(ProtocolError, match="no project"):
        asyncio.run(projects_rpc.projects_get(ctx, ProjectsGetParams(project="nope")))
    cleared = asyncio.run(projects_rpc.projects_use(ctx, ProjectsUseParams()))
    assert cleared.project is None
    repos = asyncio.run(projects_rpc.projects_discover(ctx, ProjectsDiscoverParams()))
    assert isinstance(repos.repos, list)


def test_project_info_tool(scope: Scope, store: ProjectStore, tmp_path: Path) -> None:
    (app,) = _dirs(tmp_path, "app")
    store.create("App", folders=[app])
    current = json.loads(project_tool.project_info({}, scope=scope, cwd=str(app / "src")))
    assert current["project"]["slug"] == "app" and current["source"] == "folder"
    nowhere = json.loads(project_tool.project_info({}, scope=scope, cwd=str(tmp_path)))
    assert nowhere["project"] is None
    store.use("app")
    active = json.loads(project_tool.project_info({}, scope=scope, cwd=str(tmp_path)))
    assert active["source"] == "active"
    listed = json.loads(project_tool.project_info({"action": "list"}, scope=scope))
    assert listed["projects"][0]["active"] is True
    shown = json.loads(project_tool.project_info({"action": "show", "project": "App"}, scope=scope))
    assert shown["project"]["primary_path"] == str(app)
    assert "error" in json.loads(project_tool.project_info({"action": "drop"}, scope=scope))
