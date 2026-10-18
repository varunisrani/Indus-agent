"""P11-16: the once-per-turn checkpoint trigger and its tool hooks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from indus.backends.checkpoints import trigger as trigger_mod
from indus.backends.checkpoints.store import ShadowStore, project_key
from indus.backends.checkpoints.trigger import CONTAINER_NOTE, CheckpointTrigger, is_destructive
from indus.foundation.scope import Scope
from indus.tools.builtin import mutation_hooks, read_tracking
from indus.tools.builtin.patch import patch
from indus.tools.builtin.write_file import write_file
from tests.fakes.agent import DictConfig
from tests.unit.backends.checkpoints._helpers import write


@pytest.mark.parametrize(
    "command",
    [
        "rm a.txt",
        "rm -rf build",
        "mv a.txt b.txt",
        "echo hi > out.txt",
        "printf x >| out.txt",
        "make &> build.log",
        "git reset --hard HEAD~1",
        "git -C sub checkout -- file.py",
        "git clean -fdx",
        "git stash",
        "sed -i 's/a/b/' f.txt",
        "sed --in-place=.bak 's/a/b/' f.txt",
        "perl -pi -e 's/a/b/' f.txt",
        "find . -name '*.o' -delete",
        "FOO=1 rm x",
        "sudo -n rm x",
        "env A=1 mv a b",
        "echo $(rm x)",
        "truncate -s 0 f.txt",
        "ls && rm x",
        "echo 'never closed",
    ],
)
def test_destructive_commands(command: str) -> None:
    assert is_destructive(command)


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cat a.txt > /dev/null",
        "git status",
        "git log --oneline -5",
        "git diff",
        "echo hi >> log.txt",
        "grep -ri needle .",
        "sed -n 1p f.txt",
        "python3 -c 'print(1)'",
        "pytest -q 2>&1",
        "find . -name '*.py'",
    ],
)
def test_harmless_commands(command: str) -> None:
    assert not is_destructive(command)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write(root, "a.txt", "one\n")
    return root


@pytest.fixture
def on(scope: Scope, project: Path) -> Scope:
    return scope.with_config(
        DictConfig({"checkpoints": {"enabled": True}, "terminal": {"cwd": str(project)}})
    )


@pytest.fixture
def pruned() -> list[Callable[[], None]]:
    return []


@pytest.fixture
def trig(
    monkeypatch: pytest.MonkeyPatch, pruned: list[Callable[[], None]]
) -> Iterator[CheckpointTrigger]:
    fresh = CheckpointTrigger(run_prune=pruned.append)
    monkeypatch.setattr(mutation_hooks, "TRIGGER", fresh)
    monkeypatch.setattr(trigger_mod, "TRIGGER", fresh)
    read_tracking.reset_all()
    yield fresh
    read_tracking.reset_all()


def commits(scope: Scope, project: Path) -> int:
    store = ShadowStore.for_scope(scope)
    return len(store.commits(project_key(project))) if store.exists() else 0


def test_once_per_turn(
    on: Scope, project: Path, trig: CheckpointTrigger, pruned: list[Callable[[], None]]
) -> None:
    assert trig.before_mutation(on, session_id="s", turn_id="t1", workdir=project) is None
    write(project, "a.txt", "two\n")
    assert trig.before_mutation(on, session_id="s", turn_id="t1", workdir=project) is None
    assert commits(on, project) == 1
    assert trig.before_mutation(on, session_id="s", turn_id="t2", workdir=project) is None
    assert commits(on, project) == 2
    trig.before_mutation(on, session_id="other", turn_id="t2", workdir=project)
    assert commits(on, project) == 2  # unchanged since the last checkpoint
    assert len(pruned) == 1  # the automatic prune starts once per process and profile
    pruned[0]()  # runs without error (and is throttled by its own marker)


def test_disabled_takes_nothing(
    scope: Scope, on: Scope, project: Path, trig: CheckpointTrigger
) -> None:
    assert not trig.enabled(scope, "s")
    assert trig.before_mutation(scope, session_id="s", turn_id="t1", workdir=project) is None
    assert commits(scope, project) == 0
    trig.set_default(True)
    assert trig.enabled(scope, "s")
    trig.set_enabled(on, "s", False)
    assert not trig.enabled(on, "s") and trig.enabled(on, "s2")
    assert trig.before_mutation(on, session_id="s", turn_id="t1", workdir=project) is None
    assert commits(on, project) == 0
    trig.set_enabled(on, "s", None)
    trig.set_default(None)
    assert trig.enabled(on, "s")


def test_snapshot_error_warns_once(on: Scope, project: Path) -> None:
    def broken(_scope: Scope) -> ShadowStore:
        raise OSError("disk full")

    trig = CheckpointTrigger(store_for=broken, run_prune=lambda work: None)
    first = trig.before_mutation(on, session_id="s", turn_id="t1", workdir=project)
    assert first is not None and "disk full" in first
    assert trig.before_mutation(on, session_id="s", turn_id="t1", workdir=project) is None
    # without a turn id every call counts as its own turn
    assert trig.before_mutation(on, session_id="s", turn_id=None, workdir=project) is not None
    other = trig.before_mutation(on, session_id="s", turn_id="t9", workdir=project)
    assert other is not None
    container = trig.before_mutation(
        on, session_id="s", turn_id="t10", workdir=project, on_host=False
    )
    assert container == CONTAINER_NOTE


def test_write_file_takes_one_snapshot_per_turn(
    on: Scope, project: Path, trig: CheckpointTrigger
) -> None:
    def run(path: str, turn: str) -> dict[str, object]:
        text = asyncio.run(
            write_file(
                {"path": path, "content": "x\n"},
                scope=on,
                task_id="t",
                session_id="s",
                turn_id=turn,
            )
        )
        return json.loads(text)

    first = run(str(project / "one.txt"), "turn-1")
    assert first.get("bytes_written") == 2 and "checkpoint_warning" not in first
    run(str(project / "two.txt"), "turn-1")
    assert commits(on, project) == 1
    store = ShadowStore.for_scope(on)
    tree = store.must(["ls-tree", "-r", "--name-only", store.commits(project_key(project))[0]])
    assert tree.text.split() == ["a.txt"]  # taken before the first change
    run(str(project / "three.txt"), "turn-2")
    assert commits(on, project) == 2
    patched = json.loads(
        asyncio.run(
            patch(
                {"path": str(project / "a.txt"), "old_string": "one", "new_string": "uno"},
                scope=on,
                task_id="t",
                session_id="s",
                turn_id="turn-3",
            )
        )
    )
    assert patched["success"] is True and (project / "a.txt").read_text() == "uno\n"
    assert commits(on, project) == 3


def test_write_file_runs_when_the_snapshot_fails(
    on: Scope, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_scope: Scope) -> ShadowStore:
        raise OSError("read-only store")

    monkeypatch.setattr(
        mutation_hooks, "TRIGGER", CheckpointTrigger(store_for=broken, run_prune=lambda w: None)
    )
    target = project / "new.txt"
    body = json.loads(
        asyncio.run(
            write_file(
                {"path": str(target), "content": "y\n"},
                scope=on,
                task_id="t",
                session_id="s",
                turn_id="t1",
            )
        )
    )
    assert target.read_text() == "y\n"
    assert "read-only store" in str(body["checkpoint_warning"])


def test_terminal_hook_only_for_changing_commands(
    on: Scope, project: Path, trig: CheckpointTrigger
) -> None:
    async def hook(command: str, turn: str) -> dict[str, str]:
        return await mutation_hooks.before_command(
            on, session_id="s", turn_id=turn, command=command, cwd=str(project), on_host=True
        )

    assert asyncio.run(hook("ls -la", "t1")) == {}
    assert commits(on, project) == 0
    assert asyncio.run(hook("rm a.txt", "t1")) == {}
    assert commits(on, project) == 1
    remote = asyncio.run(
        mutation_hooks.before_command(
            on, session_id="s", turn_id="t2", command="rm x", cwd="/srv", on_host=False
        )
    )
    assert remote == {"checkpoint_warning": CONTAINER_NOTE}
    assert mutation_hooks.with_notes('{"output": "ok"}', remote)
    assert json.loads(mutation_hooks.with_notes('{"a": 1}', {"a": "x", "b": "y"})) == {
        "a": 1,
        "b": "y",
    }
    assert mutation_hooks.with_notes("plain text", remote) == "plain text"
