"""P11-19: session worktrees (create, blockers, prune) against temp repositories."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from indus.backends import worktrees
from indus.backends.worktrees import Git, WorktreeError
from indus.foundation.scope import Scope
from tests.fakes.clock import FakeClock
from tests.unit.backends.checkpoints._helpers import user_git, write

HOUR = 3600.0


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


def new_repo(path: Path, *, commit: bool = True) -> Path:
    path.mkdir(parents=True)
    user_git(path, "init", "-q", "-b", "main")
    if commit:
        write(path, "app.py", "v1\n")
        user_git(path, "add", "app.py")
        user_git(path, "commit", "-q", "-m", "first")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return new_repo(tmp_path / "repo")


def head(path: Path) -> str:
    return user_git(path, "rev-parse", "HEAD").strip()


def branches(path: Path) -> list[str]:
    return user_git(path, "branch", "--format=%(refname:short)").split()


def test_create_from_fetched_upstream(scope: Scope, tmp_path: Path, git: Git) -> None:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    user_git(origin, "init", "-q", "--bare", "-b", "main")
    seed = new_repo(tmp_path / "seed")
    user_git(seed, "remote", "add", "origin", str(origin))
    user_git(seed, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    user_git(tmp_path, "clone", "-q", str(origin), str(clone))
    write(seed, "app.py", "v2\n")
    user_git(seed, "commit", "-q", "-am", "second")
    user_git(seed, "push", "-q", "origin", "main")
    newest = head(seed)
    assert head(clone) != newest  # the clone is behind

    info = worktrees.create(scope, clone, "feature x!", session_id="s1", git=git)
    tree = Path(info.path)
    assert tree == clone / ".worktrees" / "feature-x"
    assert info.branch == "indus/feature-x" and info.base == "origin/main"
    assert head(tree) == newest
    assert user_git(tree, "rev-parse", "--abbrev-ref", "HEAD").strip() == "indus/feature-x"
    assert "upstream" not in user_git(clone, "branch", "-vv")  # no tracking set up
    (entry,) = worktrees.load_registry(scope)
    assert (
        entry["path"] == str(tree) and entry["session_id"] == "s1" and entry["repo"] == str(clone)
    )
    with pytest.raises(WorktreeError, match="already exists"):
        worktrees.create(scope, clone, "feature-x", git=git)
    inside = worktrees.create(scope, tree, git=git)  # from inside a tree: still the main repo
    assert Path(inside.path).parent == clone / ".worktrees"


def test_default_name_and_local_head(scope: Scope, repo: Path, git: Git) -> None:
    info = worktrees.create(scope, repo, git=git)
    assert re.fullmatch(r"indus-20260101-090000-[0-9a-f]{4}", Path(info.path).name)
    assert info.base == "HEAD" and head(Path(info.path)) == head(repo)


def test_gitignore_and_include(scope: Scope, repo: Path, git: Git, tmp_path: Path) -> None:
    write(repo, ".gitignore", "﻿*.pyc")  # BOM, no trailing newline
    write(repo, ".env", "SECRET_NAME=value\n")
    write(repo, "conf/local.toml", "x = 1\n")
    write(tmp_path, "outside.txt", "no\n")
    write(repo, ".worktreeinclude", "# copied\n.env\nconf\n../outside.txt\n/etc/hostname\n")
    user_git(repo, "add", ".gitignore", ".worktreeinclude")
    user_git(repo, "commit", "-q", "-m", "ignore")
    write(repo, ".gitignore", (repo / ".gitignore").read_text() + "\n.env\nconf/\n")
    user_git(repo, "commit", "-q", "-am", "ignore more")
    first = Path(worktrees.create(scope, repo, "one", git=git).path)
    worktrees.create(scope, repo, "two", git=git)
    lines = (repo / ".gitignore").read_text(encoding="utf-8-sig").splitlines()
    assert lines.count(".worktrees/") == 1 and "*.pyc" in lines
    assert (first / ".env").read_text() == "SECRET_NAME=value\n"
    assert (first / "conf" / "local.toml").exists()
    assert not (first.parent / "outside.txt").exists() and not (first / "etc").exists()
    assert worktrees.removal_blockers(git, first) == []  # copies are ignored files


def test_failed_add_leaves_nothing(scope: Scope, tmp_path: Path, git: Git) -> None:
    empty = new_repo(tmp_path / "empty", commit=False)  # HEAD has no commit to branch from
    with pytest.raises(WorktreeError, match="could not be created"):
        worktrees.create(scope, empty, "broken", git=git)
    assert not (empty / ".worktrees" / "broken").exists()
    assert "indus/broken" not in branches(empty)
    assert user_git(empty, "worktree", "list").count("\n") == 1
    assert worktrees.load_registry(scope) == []


def test_not_a_repository(scope: Scope, tmp_path: Path, git: Git) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="not inside a git repository"):
        worktrees.create(scope, plain, git=git)


def test_blockers_keep_trees(scope: Scope, repo: Path, git: Git) -> None:
    tree = Path(worktrees.create(scope, repo, "work", git=git).path)
    assert worktrees.removal_blockers(git, tree) == []
    write(tree, "app.py", "changed\n")
    assert worktrees.removal_blockers(git, tree) == ["uncommitted changes: app.py"]
    user_git(tree, "checkout", "-q", "--", "app.py")
    write(tree, "notes.txt", "draft\n")
    assert worktrees.removal_blockers(git, tree) == ["untracked files: notes.txt"]
    user_git(tree, "add", "notes.txt")
    user_git(tree, "commit", "-q", "-m", "notes")
    (reason,) = worktrees.removal_blockers(git, tree)
    assert "commits that no other branch" in reason
    user_git(repo, "branch", "keep-notes", "indus/work")
    assert worktrees.removal_blockers(git, tree) == []
    (repo / ".git" / "info" / "exclude").write_text("vendor/\n")
    (tree / "vendor" / "lib" / ".git").mkdir(parents=True)
    assert worktrees.removal_blockers(git, tree) == ["embedded repository: vendor/lib"]
    (tree / "vendor" / "lib" / ".git").rmdir()
    user_git(tree, "update-ref", "refs/worktree/mark", "HEAD")
    assert worktrees.removal_blockers(git, tree) == ["per-worktree refs: refs/worktree/mark"]
    assert worktrees.remove(scope, repo, tree, git=git) is not None
    assert tree.exists()


def test_prune_removes_old_clean_merged(
    scope: Scope, repo: Path, git: Git, fake_clock: FakeClock
) -> None:
    clean = Path(worktrees.create(scope, repo, "clean", git=git).path)
    dirty = Path(worktrees.create(scope, repo, "dirty", git=git).path)
    write(dirty, "app.py", "edited\n")
    fake_clock.advance(25 * HOUR)
    young = Path(worktrees.create(scope, repo, "young", git=git).path)

    preview = worktrees.prune(scope, repo, dry_run=True, git=git)
    assert preview.removed == (str(clean),)
    kept = dict(preview.kept)
    assert kept[str(dirty)] == "uncommitted changes: app.py"
    assert kept[str(young)] == "younger than 24 h"
    assert clean.exists() and "indus/clean" in branches(repo)

    done = worktrees.prune(scope, repo, git=git)
    assert done.removed == (str(clean),)
    assert not clean.exists() and "indus/clean" not in branches(repo)
    assert dirty.exists() and young.exists()
    assert [e["path"] for e in worktrees.load_registry(scope)] == [str(dirty), str(young)]


def test_protected_branch_never_deleted(scope: Scope, repo: Path, git: Git) -> None:
    user_git(repo, "checkout", "-q", "-b", "work")
    tree = repo / ".worktrees" / "m"
    user_git(repo, "worktree", "add", "-q", str(tree), "main")
    assert worktrees.remove(scope, repo, tree, git=git) is None
    assert not tree.exists()
    assert "main" in branches(repo)


def test_divergence_note(scope: Scope, repo: Path, git: Git) -> None:
    tree = Path(worktrees.create(scope, repo, "w", git=git).path)
    assert worktrees.worktree_of(scope, tree / "sub") == str(tree)
    assert worktrees.divergence_note(scope, tree, [str(tree / "app.py")]) is None
    note = worktrees.divergence_note(scope, tree, [str(repo / "app.py")])
    assert note is not None and "outside this session's worktree" in note
    assert worktrees.divergence_note(scope, repo, [str(repo / "app.py")]) is None
    worktrees.bind_session(scope, str(tree), "s9")
    assert worktrees.load_registry(scope)[0]["session_id"] == "s9"
