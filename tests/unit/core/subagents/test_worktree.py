"""A git worktree per subagent (P10-07, ACC-C37-1)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from indus.backends.worktrees import Git
from indus.core.subagents.worktree import (
    backend_is_local,
    close_worktree,
    open_worktree,
    scope_in_tree,
)
from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig, run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.subagents import RoutedModel, parent_agent
from tests.unit.backends.checkpoints._helpers import user_git, write


@pytest.fixture
def git(tmp_path: Path) -> Git:
    return Git(
        {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    user_git(path, "init", "-q", "-b", "main")
    write(path, "app.py", "v1\n")
    user_git(path, "add", "app.py")
    user_git(path, "commit", "-q", "-m", "first")
    # A local commit the parent has not pushed anywhere: children must still see it.
    write(path, "app.py", "v2\n")
    user_git(path, "commit", "-q", "-am", "second")
    return path


def branches(path: Path) -> list[str]:
    return user_git(path, "branch", "--format=%(refname:short)").split()


def test_two_children_get_two_trees_clean_removed_dirty_kept(
    scope: Scope, repo: Path, git: Git
) -> None:
    async def main() -> tuple[Any, Any, dict[str, Any], dict[str, Any]]:
        first, note_a = await open_worktree(scope, repo, "sa-0-aaaa", session_id="c1", git=git)
        second, note_b = await open_worktree(scope, repo, "sa-1-bbbb", session_id="c2", git=git)
        assert first is not None and second is not None and not note_a and not note_b
        assert first.path != second.path and first.branch != second.branch
        assert (Path(first.path) / "app.py").read_text() == "v2\n"  # from the parent's HEAD
        write(Path(second.path), "notes.md", "work in progress\n")  # left uncommitted
        clean = dict(await close_worktree(scope, first, git=git))
        dirty = dict(await close_worktree(scope, second, git=git))
        return first, second, clean, dirty

    first, second, clean, dirty = asyncio.run(main())
    assert first.branch == "indus/subagent-sa-0-aaaa"
    assert clean == {"path": first.path, "branch": first.branch, "kept": False}
    assert not Path(first.path).exists() and first.branch not in branches(repo)
    assert dirty["kept"] is True and "notes.md" in dirty["kept_because"]
    assert Path(second.path).exists() and second.branch in branches(repo)


def test_other_backends_and_plain_folders_get_a_note(
    scope: Scope, tmp_path: Path, repo: Path, git: Git
) -> None:
    docker = scope.with_config(DictConfig({"terminal": {"backend": "docker"}}))
    assert backend_is_local(docker) == (False, "docker")
    assert backend_is_local(scope) == (True, "local")
    tree, note = asyncio.run(open_worktree(docker, repo, "sa-0-x", session_id="c", git=git))
    assert tree is None and "docker" in note
    assert not (repo / ".worktrees").exists()
    plain = tmp_path / "plain"
    plain.mkdir()
    tree, note = asyncio.run(open_worktree(scope, plain, "sa-0-y", session_id="c", git=git))
    assert tree is None and "not inside a git repository" in note


def test_child_scope_works_in_its_tree(scope: Scope, repo: Path, git: Git) -> None:
    tree, _ = asyncio.run(open_worktree(scope, repo, "sa-0-z", session_id="c", git=git))
    assert tree is not None
    inside = scope_in_tree(scope, tree)
    assert inside.config is not None and inside.config.get("terminal.cwd") == tree.path
    assert inside.config.get("terminal.backend") is None  # everything else falls through


def test_delegated_children_report_their_worktrees(
    scope: Scope, fake_clock: FakeClock, repo: Path
) -> None:
    configured = scope.with_config(DictConfig({"delegation": {"worktree": True}}))
    model = RoutedModel(
        fake_clock,
        parent=[
            tool_batch(
                ("delegate_task", {"tasks": [{"goal": "edit things"}, {"goal": "only look"}]})
            ),
            text_turn("ok"),
        ],
        children={
            "edit things": [
                tool_batch(("write_file", {"path": "notes.md", "content": "draft\n"})),
                text_turn("wrote notes.md"),
            ],
            "only look": [text_turn("nothing to change")],
        },
    )

    async def main() -> Any:
        parent = await parent_agent(configured, model, cwd=repo)
        result = await parent.run_turn("go")
        await parent.close()
        return result

    result = asyncio.run(run_driven(main(), fake_clock))
    entries = json.loads(next(r.content for r in result.rows if r.role == "tool"))["results"]
    edit, look = entries
    assert edit["status"] == look["status"] == "completed"
    assert edit["worktree"]["kept"] is True
    assert Path(edit["worktree"]["path"], "notes.md").read_text() == "draft\n"
    assert not (repo / "notes.md").exists()  # the parent's checkout is untouched
    assert look["worktree"]["kept"] is False and not Path(look["worktree"]["path"]).exists()
    assert edit["worktree"]["branch"].startswith("indus/subagent-sa-0-")


def test_docker_backend_delegation_notes_and_skips_the_tree(
    scope: Scope, fake_clock: FakeClock, repo: Path
) -> None:
    configured = scope.with_config(
        DictConfig({"delegation": {"worktree": True}, "terminal": {"backend": "docker"}})
    )
    model = RoutedModel(
        fake_clock,
        parent=[tool_batch(("delegate_task", {"goal": "look"})), text_turn("ok")],
        children={"look": [text_turn("looked")]},
    )

    async def main() -> Any:
        parent = await parent_agent(configured, model, cwd=repo, selection=["delegation"])
        result = await parent.run_turn("go")
        await parent.close()
        return result

    result = asyncio.run(run_driven(main(), fake_clock))
    entry = json.loads(next(r.content for r in result.rows if r.role == "tool"))["results"][0]
    assert "worktree" not in entry
    assert "terminal backend is docker" in entry["note"]
    assert not (repo / ".worktrees").exists()


def test_closing_a_child_tree_stops_its_language_servers_first(
    scope: Scope, repo: Path, git: Git, tmp_path: Path
) -> None:
    """Wave 45 (D-16.982): the process's tier releases the tree before it is removed."""
    from indus.tools.lsp import diagnostics
    from indus.tools.lsp.diagnostics import EditDiagnostics
    from tests.unit.tools.lsp import test_manager as fakes

    fakes.FakeClient.created = []
    lsp = fakes.manager(tmp_path)
    diagnostics.install_active(EditDiagnostics(lsp))

    async def main() -> tuple[bool, int, bool]:
        tree, _ = await open_worktree(scope, repo, "sa-0-cccc", session_id="c1", git=git)
        assert tree is not None
        await lsp.client_for(str(Path(tree.path) / "a.fk"))
        running = len(lsp.clients) == 1
        result = dict(await close_worktree(scope, tree, git=git))
        stopped = sum(c.stopped for c in fakes.FakeClient.created)
        await lsp.close()
        return running, stopped, bool(result["kept"])

    try:
        running, stopped, kept = asyncio.run(main())
    finally:
        diagnostics.install_active(None)
    assert running and stopped == 1 and not kept
