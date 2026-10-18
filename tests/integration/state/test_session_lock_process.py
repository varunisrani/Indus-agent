"""Per-session lock across processes (P04-21): busy while a child holds it, free once it dies.

A child process takes the lock and waits on stdin. While it runs the parent gets ``LockBusy``
naming the child's pid; once the child is killed (no release, no cleanup) the kernel drops the
lock and the parent takes it, reporting the dead holder.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from indus.state import holders
from indus.state.session_lock import LockBusy, SessionLock, try_lock_session

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file locks")

_CHILD = (
    "import sys\n"
    "from pathlib import Path\n"
    "from indus.state.session_lock import LockBusy, try_lock_session\n"
    "result = try_lock_session(Path(sys.argv[1]), sys.argv[2], owner='child')\n"
    "if isinstance(result, LockBusy):\n"
    "    print('busy', result.holder, flush=True)\n"
    "    raise SystemExit(0)\n"
    "print('held', flush=True)\n"
    "sys.stdin.readline()\n"
    "result.release()\n"
    "print('released', flush=True)\n"
)


def _child(lock_dir: Path, session_id: str) -> tuple[subprocess.Popen[str], str]:
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(lock_dir), session_id],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    return child, child.stdout.readline().strip()


def _finish(child: subprocess.Popen[str]) -> None:
    if child.poll() is None:
        child.kill()
    child.wait(timeout=30)


def test_busy_while_the_child_holds_then_free_after_it_is_killed(tmp_path: Path) -> None:
    lock_dir = tmp_path / "session-locks"
    child, first = _child(lock_dir, "s1")
    try:
        assert first == "held"
        busy = try_lock_session(lock_dir, "s1")
        assert isinstance(busy, LockBusy) and not busy.in_process
        info = holders.parse_holder(busy.holder)
        assert info is not None and info.pid == child.pid and busy.owner == "child"
        child.kill()
        child.wait(timeout=30)
        taken = try_lock_session(lock_dir, "s1")
        assert isinstance(taken, SessionLock)
        with taken:
            if holders.local_pid_namespace() is not None:
                assert taken.recovered_from is not None
                assert holders.parse_holder(taken.recovered_from) == info
    finally:
        _finish(child)


def test_a_clean_release_in_the_child_frees_the_session(tmp_path: Path) -> None:
    lock_dir = tmp_path / "session-locks"
    child, first = _child(lock_dir, "s1")
    try:
        assert first == "held"
        assert child.stdin is not None and child.stdout is not None
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "released"
        taken = try_lock_session(lock_dir, "s1")
        assert isinstance(taken, SessionLock)
        with taken:
            assert taken.recovered_from is None
    finally:
        _finish(child)


def test_the_child_finds_our_session_busy(tmp_path: Path) -> None:
    lock_dir = tmp_path / "session-locks"
    mine = try_lock_session(lock_dir, "s1", owner="parent")
    assert isinstance(mine, SessionLock)
    with mine:
        child, first = _child(lock_dir, "s1")
        _finish(child)
    word, _, holder = first.partition(" ")
    assert word == "busy"
    info = holders.parse_holder(holder)
    assert info is not None and info.pid == os.getpid()
