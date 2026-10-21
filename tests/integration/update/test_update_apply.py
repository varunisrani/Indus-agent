"""P23-18: apply, verify, rollback (R04.24) through ``indus update`` on real temp git repos.

The origin carries releases built by ``tests/fixtures/update/make_repo.py``; ``uv`` and the hand-off
``exec`` are fakes (``tests/fakes/updater.py``), so the test sees exactly which environment the
update built and what it would have exec'd.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from indus.cli.commands import update as update_mod
from indus.cli.update import marker
from indus.foundation.scope import Scope
from tests.fakes.clock import FakeClock
from tests.fakes.updater import FakeUv, Snapshots, World, scope_for
from tests.fixtures.update.make_repo import Upstream, git
from tests.unit.cli.commands.cmdrun import Ran, run_cli


@pytest.fixture
def upstream(tmp_path: Path) -> Upstream:
    return Upstream(tmp_path)


@pytest.fixture
def user_scope(tmp_path: Path, upstream: Upstream, fake_clock: FakeClock) -> Scope:
    return scope_for(tmp_path / "indus-root", upstream.home, fake_clock)


def install(upstream: Upstream, tmp_path: Path, at: str) -> Path:
    checkout = upstream.clone(tmp_path / "checkout", at=at)
    venv = checkout / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("old interpreter\n", encoding="utf-8")
    (venv / "marker").write_text("old venv\n", encoding="utf-8")
    return checkout


def run_update(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    scope: Scope,
    world: World,
    *argv: str,
) -> Ran:
    monkeypatch.setattr(update_mod, "wiring", world.wiring)
    return run_cli(capsys, scope, "update", *argv, tty=True)


def head(checkout: Path, upstream: Upstream) -> str:
    return git(checkout, "rev-parse", "HEAD", home=upstream.home)


def receipts(scope: Scope) -> list[dict[str, object]]:
    folder = scope.root / "logs" / "update_receipts"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]


def phases(receipt: dict[str, object]) -> dict[str, str]:
    rows = receipt["phases"]
    assert isinstance(rows, list)
    return {str(r["phase"]): str(r["outcome"]) for r in rows}


def test_success_swaps_venvs_and_execs_finish(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    second = upstream.release("0.2.0")
    checkout = install(upstream, tmp_path, first)
    world = World(checkout)
    ran = run_update(capsys, monkeypatch, user_scope, world, "-y", "--no-gateway-restart")
    assert ran.code == 0, ran.err
    assert head(checkout, upstream) == second
    assert "release 0.2.0" in (checkout / ".venv" / "lock-of").read_text(encoding="utf-8")
    assert (checkout / ".venv-prev" / "marker").read_text(encoding="utf-8") == "old venv\n"
    assert not (checkout / ".venv-next").exists()
    assert world.snapshots.modes == ["quick"]
    # the hand-off: the NEW venv's interpreter runs --finish; nothing pulled ran here
    [(path, argv, _env)] = world.execs.calls
    assert path == str(checkout / ".venv" / "bin" / "python")
    assert argv[:5] == [path, "-m", "indus", "update", "--finish"]
    assert argv[6:] == ["-y", "--no-gateway-restart"]
    request = json.loads(Path(argv[5]).read_text(encoding="utf-8"))
    assert request["to_commit"] == second and request["to_version"] == "0.2.0"
    assert request["from_commit"] == first and request["stash_conflict"] is None
    done = {r["phase"]: r["outcome"] for r in request["receipt"]["phases"]}
    assert done == {
        "snapshot": "ok", "stash": "skipped", "move": "ok", "syntax": "ok", "venv": "ok",
        "swap": "ok", "handoff": "ok",
    }  # fmt: skip
    sync = next(c for c in world.uv.calls if c[1] == "sync")
    assert sync[:3] == ["uv", "sync", "--frozen"] and "--no-dev" in sync
    venv_call = next(c for c in world.uv.calls if c[1] == "venv")
    assert "--relocatable" in venv_call and venv_call[-1] == str(checkout / ".venv-next")
    assert "Updating" in ran.out
    log = (user_scope.root / "logs" / "update.log").read_text(encoding="utf-8")
    assert "Moving the checkout to 0.2.0" in log and "uv sync" in log


def test_syntax_error_in_the_new_release_rolls_back(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    upstream.release("0.2.0", broken_syntax=True, extra={"src/indus/new_in_02.py": "X = 1\n"})
    checkout = install(upstream, tmp_path, first)
    world = World(checkout)
    ran = run_update(capsys, monkeypatch, user_scope, world, "-y")
    assert ran.code == 1
    assert "src/indus/cli/app.py" in ran.err and "broken startup file" in ran.err
    assert head(checkout, upstream) == first
    assert 'VERSION = "0.1.0"' in (checkout / "src/indus/cli/app.py").read_text(encoding="utf-8")
    assert not (checkout / "src/indus/new_in_02.py").exists()
    assert not list(checkout.rglob("__pycache__"))  # compiled into a temp dir only
    assert (checkout / ".venv" / "marker").exists() and not (checkout / ".venv-next").exists()
    assert world.execs.calls == [] and not any(c[1] == "sync" for c in world.uv.calls)
    [receipt] = receipts(user_scope)
    assert receipt["outcome"] == "failed"
    assert phases(receipt)["syntax"] == "failed" and phases(receipt)["rollback"] == "ok"


def test_unresolvable_lock_keeps_the_old_venv(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    upstream.release("0.2.0", broken_lock=True)
    checkout = install(upstream, tmp_path, first)
    world = World(checkout)
    ran = run_update(capsys, monkeypatch, user_scope, world, "-y")
    assert ran.code == 1 and "No solution found" in ran.err
    assert head(checkout, upstream) == first
    assert (checkout / ".venv" / "marker").read_text(encoding="utf-8") == "old venv\n"
    assert not (checkout / ".venv-next").exists() and not (checkout / ".venv-prev").exists()
    assert world.execs.calls == []
    assert phases(receipts(user_scope)[0])["venv"] == "failed"


def test_missing_uv_is_a_failed_build(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    upstream.release("0.2.0")
    checkout = install(upstream, tmp_path, first)
    world = World(checkout, uv_path=None)
    ran = run_update(capsys, monkeypatch, user_scope, world, "-y")
    assert ran.code == 1 and "uv was not found" in ran.err
    assert head(checkout, upstream) == first


def test_local_edits_come_back_on_top_of_the_new_code(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0", extra={"notes.txt": "upstream notes\n"})
    second = upstream.release("0.2.0", extra={"notes.txt": "upstream notes\n"})
    checkout = install(upstream, tmp_path, first)
    (checkout / "notes.txt").write_text("upstream notes\nmine\n", encoding="utf-8")
    (checkout / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    world = World(checkout)
    ran = run_update(capsys, monkeypatch, user_scope, world, "-y")
    assert ran.code == 0, ran.err
    assert head(checkout, upstream) == second
    assert (checkout / "notes.txt").read_text(encoding="utf-8").endswith("mine\n")
    assert (checkout / "scratch.txt").read_text(encoding="utf-8") == "untracked\n"
    assert git(checkout, "stash", "list", home=upstream.home) == ""
    assert "indus-update-autostash-20260101T090000Z" in ran.out


def test_stash_conflict_exits_1_with_its_label(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0", extra={"notes.txt": "one\n"})
    second = upstream.release("0.2.0", extra={"notes.txt": "two\n"})
    checkout = install(upstream, tmp_path, first)
    (checkout / "notes.txt").write_text("mine\n", encoding="utf-8")
    world = World(checkout)
    ran = run_update(capsys, monkeypatch, user_scope, world, "-y", "--no-gateway-restart")
    assert ran.code == 0, ran.err  # the apply half hands off; the finish half reports
    [(_path, argv, _env)] = world.execs.calls
    request = json.loads(Path(argv[5]).read_text(encoding="utf-8"))
    label = request["stash_conflict"]
    assert label == "indus-update-autostash-20260101T090000Z"
    assert head(checkout, upstream) == second
    assert (checkout / "notes.txt").read_text(encoding="utf-8") == "two\n"  # tree clean
    assert label in git(checkout, "stash", "list", home=upstream.home)
    finished = run_update(capsys, monkeypatch, user_scope, world, *argv[4:])
    assert finished.code == 1 and label in finished.err
    final = receipts(user_scope)[-1]
    assert final["outcome"] == "local_changes_not_restored"
    assert phases(final)["restore_changes"] == "failed"


def test_interrupt_during_the_build_restores_everything(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0", extra={"notes.txt": "one\n"})
    upstream.release("0.2.0", extra={"notes.txt": "one\n", "src/indus/added.py": "A = 1\n"})
    checkout = install(upstream, tmp_path, first)
    (checkout / "notes.txt").write_text("one\nmine\n", encoding="utf-8")
    world = World(checkout, uv=FakeUv(interrupt_on="sync"))
    ran = run_update(capsys, monkeypatch, user_scope, world, "-y")
    assert ran.code == 130
    assert head(checkout, upstream) == first
    assert not (checkout / "src/indus/added.py").exists()
    assert (checkout / ".venv" / "marker").exists() and not (checkout / ".venv-next").exists()
    assert (checkout / "notes.txt").read_text(encoding="utf-8") == "one\nmine\n"
    assert git(checkout, "stash", "list", home=upstream.home) == ""
    assert world.execs.calls == []
    assert receipts(user_scope)[0]["outcome"] == "interrupted"


def test_local_commits_are_kept_on_a_backup_ref(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    second = upstream.release("0.2.0")
    checkout = install(upstream, tmp_path, first)
    (checkout / "mine.txt").write_text("local work\n", encoding="utf-8")
    git(checkout, "add", "mine.txt", home=upstream.home)
    git(checkout, "commit", "--quiet", "-m", "local work", home=upstream.home)
    local = head(checkout, upstream)
    world = World(checkout)
    # without an explicit choice a diverged checkout is left alone
    ran = run_update(capsys, monkeypatch, user_scope, world, "-y")
    assert ran.code == 0 and "left where it is" in ran.out and head(checkout, upstream) == local
    ran = run_update(capsys, monkeypatch, user_scope, world, "-y", "--channel", "stable")
    assert ran.code == 0, ran.err
    assert head(checkout, upstream) == second
    refs = git(
        checkout, "for-each-ref", "--format=%(objectname)", "refs/indus/update-backups/",
        home=upstream.home,
    )  # fmt: skip
    assert refs.split() == [local]


def test_one_update_at_a_time(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    upstream.release("0.2.0")
    checkout = install(upstream, tmp_path, first)
    lock_path = user_scope.root / ".update.lock"
    holder = (
        "import fcntl, sys\n"
        "f = open(sys.argv[1], 'a')\n"
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "sys.stdin.read()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", holder, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "held"
        ran = run_update(capsys, monkeypatch, user_scope, World(checkout), "-y")
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=10)
    assert ran.code == 1 and "Another `indus update`" in ran.err
    assert head(checkout, upstream) == first


def test_failed_snapshot_warns_and_the_update_goes_on(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    second = upstream.release("0.2.0")
    checkout = install(upstream, tmp_path, first)
    world = World(checkout, snapshots=Snapshots(fail=True))
    ran = run_update(capsys, monkeypatch, user_scope, world, "-y", "--backup")
    assert ran.code == 0 and "Pre-update snapshot failed (disk full)" in ran.err
    assert world.snapshots.modes == ["full"] and head(checkout, upstream) == second
    [(_path, argv, _env)] = world.execs.calls
    request = json.loads(Path(argv[5]).read_text(encoding="utf-8"))
    rows = {r["phase"]: r for r in request["receipt"]["phases"]}
    assert rows["snapshot"]["outcome"] == "failed" and rows["snapshot"]["detail"] == "disk full"
    skipped = World(checkout)
    git(checkout, "checkout", "--quiet", "--detach", first, home=upstream.home)
    assert run_update(capsys, monkeypatch, user_scope, skipped, "-y", "--no-backup").code == 0
    [(_path, argv, _env)] = skipped.execs.calls
    request = json.loads(Path(argv[5]).read_text(encoding="utf-8"))
    rows = {r["phase"]: r for r in request["receipt"]["phases"]}
    assert rows["snapshot"] == {"phase": "snapshot", "outcome": "skipped", "detail": "--no-backup"}
    assert skipped.snapshots.modes == []


def test_a_finish_that_cannot_start_is_owed_with_its_command(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    second = upstream.release("0.2.0")
    checkout = install(upstream, tmp_path, first)
    world = World(checkout)

    def broken_exec(path: str, argv: object, env_map: object) -> None:
        raise PermissionError(13, "Permission denied", path)

    world.execs = broken_exec  # type: ignore[assignment]
    ran = run_update(capsys, monkeypatch, user_scope, world, "-y")
    assert ran.code == 1 and "--finish" in ran.err
    assert head(checkout, upstream) == second  # the code and venv stay updated
    [receipt] = receipts(user_scope)
    assert receipt["outcome"] == "finish_not_started"
    owed = receipt["owed"]
    assert isinstance(owed, list) and owed[0]["followup"] == "finish"


def test_main_channel_rollback_puts_the_branch_back(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = upstream.release("0.1.0")
    checkout = upstream.clone(tmp_path / "checkout")  # on branch main at 0.1.0
    (checkout / ".venv").mkdir()
    upstream.write_tree("0.2.0", broken_syntax=True)
    upstream.commit("a broken commit on main")
    ran = run_update(capsys, monkeypatch, user_scope, World(checkout), "-y", "--channel", "main")
    assert ran.code == 1
    assert head(checkout, upstream) == good
    assert git(checkout, "symbolic-ref", "--short", "HEAD", home=upstream.home) == "main"
    assert git(checkout, "rev-parse", "refs/heads/main", home=upstream.home) == good
    upstream.write_tree("0.2.1")
    fixed = upstream.commit("fixed on main")
    world = World(checkout)
    assert run_update(capsys, monkeypatch, user_scope, world, "-y", "--channel", "main").code == 0
    assert head(checkout, upstream) == fixed
    assert git(checkout, "symbolic-ref", "--short", "HEAD", home=upstream.home) == "main"


# -- the shared update marker (the desktop app reads it, P20-37) ---------------------------------


def test_marker_is_held_from_the_move_through_the_hand_off(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    upstream.release("0.2.0")
    checkout = install(upstream, tmp_path, first)
    world = World(checkout)
    path = marker.marker_path(user_scope.root)
    seen: list[dict[str, object] | None] = []

    def exec_spy(target: str, argv: object, env_map: object) -> None:
        seen.append(marker.read_marker(path))

    world.execs = exec_spy  # type: ignore[assignment]
    ran = run_update(capsys, monkeypatch, user_scope, world, "-y", "--no-gateway-restart")
    assert ran.code == 0, ran.err
    [body] = seen
    assert body is not None
    assert set(body) == {"format", "owner", "pid", "process_start", "created_at"}
    assert body["format"] == 1 and body["owner"] == "indus-update" and body["pid"] == os.getpid()
    assert marker.is_live(body), "the desktop's judgement: this process owns the runtime now"
    if sys.platform.startswith("linux"):
        assert isinstance(body["process_start"], float)
    assert not path.exists(), "a hand-off that returns (no real exec) releases the marker"


def test_a_live_marker_of_another_process_refuses_before_any_move(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    upstream.release("0.2.0")
    checkout = install(upstream, tmp_path, first)
    other = os.getppid()
    user_scope.root.mkdir(parents=True, exist_ok=True)
    body = {
        "format": 1,
        "owner": "indus-update",
        "pid": other,
        "process_start": marker.process_start(other),
        "created_at": time.time(),
    }
    path = marker.marker_path(user_scope.root)
    path.write_text(json.dumps(body), encoding="utf-8")
    ran = run_update(capsys, monkeypatch, user_scope, World(checkout), "-y")
    assert ran.code == 1 and "another update" in ran.err
    assert head(checkout, upstream) == first
    assert json.loads(path.read_text(encoding="utf-8")) == body, "never removed by a non-owner"


def test_a_stale_marker_is_replaced_by_the_next_update(
    upstream: Upstream,
    user_scope: Scope,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = upstream.release("0.1.0")
    second = upstream.release("0.2.0")
    checkout = install(upstream, tmp_path, first)
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], check=True,
                          capture_output=True, text=True)  # fmt: skip
    gone = int(done.stdout)
    user_scope.root.mkdir(parents=True, exist_ok=True)
    stale = {"format": 1, "owner": "indus-update", "pid": gone, "process_start": 1.0,
             "created_at": time.time()}  # fmt: skip
    marker.marker_path(user_scope.root).write_text(json.dumps(stale), encoding="utf-8")
    ran = run_update(capsys, monkeypatch, user_scope, World(checkout), "-y", "--no-gateway-restart")
    assert ran.code == 0, ran.err
    assert head(checkout, upstream) == second
