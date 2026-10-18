"""Per-session advisory lock (P04-21, D-16.69): in-process rules, file rules, stale holders."""

from __future__ import annotations

import errno
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.protocol.errors import SESSION_BUSY, error_for_indus_code
from indus.state import holders, session_lock
from indus.state.session_lock import (
    LockBusy,
    SessionBusy,
    SessionLock,
    lock_dir_for,
    lock_file_name,
    lock_session,
    try_lock_session,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file locks")

NS = "4026531999"
HOST = "box-z"
GONE_PID = 999_001
LIVE_PID = 999_002


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    return tmp_path / "state" / "session-locks"


@pytest.fixture
def same_ns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend we know our namespace and that only ``GONE_PID`` has exited."""
    monkeypatch.setattr(holders, "local_pid_namespace", lambda: NS)
    monkeypatch.setattr(holders, "_host", lambda: HOST)
    monkeypatch.setattr(holders, "_pid_gone", lambda pid: pid == GONE_PID)


@pytest.fixture(autouse=True)
def _no_leftovers() -> Iterator[None]:
    yield
    assert session_lock._table == {}  # pyright: ignore[reportPrivateUsage]


def _stamp(pid: int, ns: str = NS, host: str = HOST) -> str:
    return f"pid={pid}:pidns={ns}:{host}"


def _held(result: SessionLock | LockBusy) -> SessionLock:
    assert isinstance(result, SessionLock), result
    return result


def _busy(result: SessionLock | LockBusy) -> LockBusy:
    assert isinstance(result, LockBusy), result
    return result


def _hold_raw(path: Path, content: str) -> int:
    """Lock ``path`` through a separate descriptor, as another holder would."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, content.encode())
    return fd


def test_take_release_and_take_again(lock_dir: Path) -> None:
    lock = _held(try_lock_session(lock_dir, "20261018_101500_abc123", owner="repl"))
    path = lock_dir / "20261018_101500_abc123.lock"
    assert lock.path == path and lock.held and lock.os_locked
    first, second = path.read_text().splitlines()
    assert first == holders.holder_stamp() and second == "repl"
    assert lock.recovered_from is None
    lock.release()
    assert not lock.held and not path.exists()
    lock.release()  # idempotent
    with _held(try_lock_session(lock_dir, "20261018_101500_abc123")) as again:
        assert again.held
    assert not again.held


def test_second_take_in_this_process_is_busy(lock_dir: Path) -> None:
    with _held(try_lock_session(lock_dir, "s1", owner="tui")):
        busy = _busy(try_lock_session(lock_dir, "s1", owner="repl"))
        assert busy.in_process and busy.owner == "tui" and busy.session_id == "s1"
        assert "this process" in busy.describe() and "(tui)" in busy.describe()
        with pytest.raises(SessionBusy) as caught:
            lock_session(lock_dir, "s1")
        assert caught.value.code == "session.busy" and caught.value.session_id == "s1"
        assert error_for_indus_code(caught.value.code) is SESSION_BUSY
        # other sessions are independent
        with lock_session(lock_dir, "s2") as other:
            assert other.held


def test_another_thread_sees_the_session_busy(lock_dir: Path) -> None:
    seen: list[SessionLock | LockBusy] = []
    with _held(try_lock_session(lock_dir, "s1")):
        worker = threading.Thread(target=lambda: seen.append(try_lock_session(lock_dir, "s1")))
        worker.start()
        worker.join(5)
    assert isinstance(seen[0], LockBusy) and seen[0].in_process


def test_racing_threads_get_exactly_one_lock(lock_dir: Path) -> None:
    start = threading.Barrier(8)
    results: list[SessionLock | LockBusy] = []
    guard = threading.Lock()

    def race() -> None:
        start.wait(5)
        result = try_lock_session(lock_dir, "shared")
        with guard:
            results.append(result)

    threads = [threading.Thread(target=race) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    winners = [r for r in results if isinstance(r, SessionLock)]
    assert len(results) == 8 and len(winners) == 1
    winners[0].release()


@pytest.mark.parametrize(
    ("session_id", "plain"),
    [
        ("20261018_101500_abc123", True),
        ("cron_nightly-job.v2_20261018_101500", True),
        ("Cron_Nightly_20261018_101500", False),
        ("../escape", False),
        ("a/b", False),
        ("..", False),
        ("x" * 129, False),
    ],
)
def test_file_names(session_id: str, plain: bool) -> None:
    name = lock_file_name(session_id)
    assert name.endswith(".lock") and "/" not in name
    assert (name == session_id + ".lock") is plain
    if not plain:
        assert name.startswith("h-") and len(name) == len("h-") + 40 + len(".lock")


def test_file_names_keep_case_variants_apart() -> None:
    assert lock_file_name("Cron_A_20261018_101500") != lock_file_name("cron_a_20261018_101500")
    with pytest.raises(ValueError):
        lock_file_name("")


def test_lock_dir_sits_beside_the_store(tmp_path: Path) -> None:
    assert lock_dir_for(tmp_path / "state.db") == tmp_path / "session-locks"


def test_a_crashed_holder_is_reported_as_recovered(lock_dir: Path, same_ns: None) -> None:
    lock_dir.mkdir(parents=True)
    (lock_dir / "s1.lock").write_text(_stamp(GONE_PID) + "\nrepl\n")
    with _held(try_lock_session(lock_dir, "s1")) as lock:
        assert lock.recovered_from == _stamp(GONE_PID)


def test_a_cleanly_left_file_of_a_live_process_is_not_recovered(
    lock_dir: Path, same_ns: None
) -> None:
    lock_dir.mkdir(parents=True)
    (lock_dir / "s1.lock").write_text(_stamp(LIVE_PID) + "\n")
    with _held(try_lock_session(lock_dir, "s1")) as lock:
        assert lock.recovered_from is None


def test_held_by_a_live_process_is_busy(lock_dir: Path, same_ns: None) -> None:
    fd = _hold_raw(lock_dir / "s1.lock", _stamp(LIVE_PID) + "\ntui\n")
    try:
        busy = _busy(try_lock_session(lock_dir, "s1"))
        assert not busy.in_process and busy.holder == _stamp(LIVE_PID) and busy.owner == "tui"
        assert f"process {LIVE_PID}" in busy.describe()
        assert busy.error().holder == _stamp(LIVE_PID)
    finally:
        os.close(fd)


def test_held_through_an_orphaned_descriptor_of_a_dead_holder_is_taken_over(
    lock_dir: Path, same_ns: None
) -> None:
    path = lock_dir / "s1.lock"
    fd = _hold_raw(path, _stamp(GONE_PID) + "\n")
    old_inode = os.fstat(fd).st_ino
    try:
        with _held(try_lock_session(lock_dir, "s1")) as lock:
            assert os.stat(path).st_ino != old_inode
            assert path.read_text().splitlines()[0] == holders.holder_stamp()
            assert lock.recovered_from is None  # the fresh file had no previous holder
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "stamp",
    [
        _stamp(GONE_PID, ns="4026530000"),  # another container's namespace
        _stamp(GONE_PID, host="box-other"),
        "not a stamp",
        "",
    ],
)
def test_holders_we_cannot_judge_keep_the_lock(lock_dir: Path, same_ns: None, stamp: str) -> None:
    fd = _hold_raw(lock_dir / "s1.lock", stamp + "\n")
    try:
        busy = _busy(try_lock_session(lock_dir, "s1"))
        assert busy.holder == (stamp or None)
        assert "another process" in busy.describe() or str(GONE_PID) in busy.describe()
    finally:
        os.close(fd)


def test_file_systems_without_locks_fall_back_to_this_process(
    lock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_locks(fd: int) -> None:
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(session_lock, "_os_try_lock", no_locks)
    with _held(try_lock_session(lock_dir, "s1")) as lock:
        assert not lock.os_locked
        assert _busy(try_lock_session(lock_dir, "s1")).in_process
    _held(try_lock_session(lock_dir, "s1")).release()


def test_other_file_faults_raise_and_leave_no_entry(
    lock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(fd: int) -> None:
        raise OSError(errno.EIO, "i/o error")

    with monkeypatch.context() as patch:
        patch.setattr(session_lock, "_os_try_lock", broken)
        with pytest.raises(OSError):
            try_lock_session(lock_dir, "s1")
    with _held(try_lock_session(lock_dir, "s1")):
        pass


def test_release_keeps_a_file_someone_else_replaced(lock_dir: Path) -> None:
    lock = _held(try_lock_session(lock_dir, "s1"))
    lock.path.unlink()
    lock.path.write_text("someone else\n")
    lock.release()
    assert lock.path.read_text() == "someone else\n"
