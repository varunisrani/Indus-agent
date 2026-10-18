"""P11-20: `indus -w`, `indus worktree`, `/worktree` and the divergence warning."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.backends import worktrees
from indus.cli import chat as chat_mod
from indus.cli.app import rewrite_session_flags
from indus.cli.args import AUTO_WORKTREE, ChatArgs, GlobalArgs
from indus.cli.slash.dispatch import dispatch
from indus.foundation.scope import Scope
from indus.state import registry, sessions
from indus.tools.builtin import read_tracking
from indus.tools.builtin.write_file import write_file
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, script_of, text_turn
from tests.unit.backends.checkpoints._helpers import user_git, write
from tests.unit.cli.commands._helpers_d import run
from tests.unit.cli.slash._fakes import make_ctx
from tests.unit.cli.test_chat_select import RecordingRepl, make_state


@pytest.fixture(autouse=True)
def keep_cwd() -> Iterator[None]:
    here = os.getcwd()
    yield
    os.chdir(here)


@pytest.fixture
def repl(monkeypatch: pytest.MonkeyPatch) -> RecordingRepl:
    stand_in = RecordingRepl()
    monkeypatch.setattr(chat_mod, "load_repl", lambda: stand_in)
    return stand_in


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    user_git(root, "init", "-q", "-b", "main")
    write(root, "app.py", "v1\n")
    user_git(root, "add", "app.py")
    user_git(root, "commit", "-q", "-m", "first")
    return root


def test_flag_rewrite() -> None:
    known = ["chat", "worktree"]
    assert rewrite_session_flags(["-w"], known) == [f"--worktree={AUTO_WORKTREE}"]
    assert rewrite_session_flags(["-w", "chat"], known) == [f"--worktree={AUTO_WORKTREE}", "chat"]
    assert rewrite_session_flags(["-w", "feat", "-m", "x"], known) == [
        "--worktree=feat",
        "-m",
        "x",
    ]
    assert rewrite_session_flags(["chat", "--worktree", "--yolo"], known) == [
        "chat",
        f"--worktree={AUTO_WORKTREE}",
        "--yolo",
    ]


def test_dash_w_starts_inside_a_new_tree(
    scope: Scope, repo: Path, repl: RecordingRepl, tmp_path: Path
) -> None:
    os.chdir(repo)
    state = make_state(scope, FakeModelServer(script_of(text_turn("x"))))
    repl.run_seed = True  # one stored turn makes the session row
    assert chat_mod.run_chat(state, GlobalArgs(worktree="feature"), ChatArgs(query="hi")) == 0
    repl.run_seed = False
    launch = repl.launches[0]
    tree = repo / ".worktrees" / "feature"
    assert Path.cwd() == tree and launch.cwd == tree
    assert any("Worktree:" in note for note in launch.notices)
    (entry,) = worktrees.load_registry(scope)
    assert entry["path"] == str(tree) and entry["session_id"] == launch.session_id
    handle = registry.acquire(scope)
    try:
        row = sessions.get(handle, launch.session_id)
    finally:
        registry.release(handle)
    assert row is not None and row["cwd"] == str(tree)
    from indus.cli.session_select import stored_launch

    stored = stored_launch(row)
    assert stored is not None and stored.worktree == str(tree)

    os.chdir(tmp_path)
    assert chat_mod.run_chat(state, GlobalArgs(resume=launch.session_id), ChatArgs()) == 0
    assert Path.cwd() == tree  # resume re-enters the tree
    again = GlobalArgs(resume=launch.session_id, worktree="x")
    assert chat_mod.run_chat(state, again, ChatArgs()) == 2


def test_dash_w_outside_a_repo_exits_1(
    scope: Scope, tmp_path: Path, repl: RecordingRepl, capsys: pytest.CaptureFixture[str]
) -> None:
    os.chdir(tmp_path)
    state = make_state(scope, FakeModelServer(script_of(text_turn("x"))))
    assert chat_mod.run_chat(state, GlobalArgs(worktree=AUTO_WORKTREE), ChatArgs()) == 1
    assert "not inside a git repository" in capsys.readouterr().err
    assert repl.launches == []


def test_edit_outside_the_tree_warns(scope: Scope, repo: Path) -> None:
    tree = Path(worktrees.create(scope, repo, "w").path)
    in_tree = scope.with_config(DictConfig({"terminal": {"cwd": str(tree)}}))
    read_tracking.reset_all()

    def edit(path: Path) -> dict[str, object]:
        text = asyncio.run(write_file({"path": str(path), "content": "x\n"}, scope=in_tree))
        return json.loads(text)

    outside = edit(repo / "notes.md")
    assert "outside this session's worktree" in str(outside["worktree_warning"])
    assert (repo / "notes.md").exists()
    inside = edit(tree / "notes.md")
    assert "worktree_warning" not in inside
    read_tracking.reset_all()


def test_slash_prune_dry_run_lists_only(scope: Scope, repo: Path, fake_clock: FakeClock) -> None:
    old = Path(worktrees.create(scope, repo, "old").path)
    write(old, "draft.txt", "keep me\n")
    clean = Path(worktrees.create(scope, repo, "clean").path)
    fake_clock.advance(25 * 3600)
    ctx = make_ctx(scope, session_cwd=lambda: str(repo))
    dispatch("/worktree prune --dry-run", ctx)
    assert f"Would remove: {clean}" in ctx.lines
    assert any(line.startswith(f"Kept: {old} (untracked files: draft.txt") for line in ctx.lines)
    assert clean.exists() and old.exists()
    ctx.lines.clear()
    dispatch("/worktree", ctx)
    assert any("clean" in line and line.rstrip().endswith("clean") for line in ctx.lines)
    ctx.lines.clear()
    dispatch("/worktree new extra", ctx)
    assert ctx.lines[0] == f"Worktree created: {repo / '.worktrees' / 'extra'}"
    ctx.lines.clear()
    dispatch("/worktree prune", ctx)
    assert f"Removed: {clean}" in ctx.lines and not clean.exists()
    ctx.lines.clear()
    dispatch("/worktree delete everything", ctx)
    assert ctx.lines == ["Usage: /worktree [list] | new [name] | prune [--dry-run]"]


def test_worktree_command(scope: Scope, repo: Path, tmp_path: Path) -> None:
    os.chdir(repo)
    empty = run(scope, "worktree")
    assert empty.code == 0 and "No worktrees" in empty.out
    made = run(scope, "worktree", "new", "cli-made")
    assert made.code == 0 and "indus/cli-made" in made.out
    listed = run(scope, "worktree", "list")
    assert listed.code == 0 and "cli-made" in listed.out and "indus/cli-made" in listed.out
    pruned = run(scope, "worktree", "prune", "--dry-run")
    assert pruned.code == 0 and "younger than 24 h" in pruned.out
    os.chdir(tmp_path)
    outside = run(scope, "worktree", "list")
    assert outside.code == 1 and "not inside a git repository" in outside.err
