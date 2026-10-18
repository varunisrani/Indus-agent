"""Opening a store (P02-01): isolation guard, profile-safe mkdir, quarantine, modes, preflight."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from indus.foundation.errors import (
    ProfileTombstoned,
    StoreIsolationRefused,
    StoreNotWritable,
    StoreUnopenable,
)
from indus.foundation.fsio import FileLock
from indus.foundation.profiles import tombstone
from indus.foundation.scope import Scope, make_scope
from indus.state import connection
from indus.state.connection import (
    GUARD_BYPASS_ENV,
    SQLITE_MAGIC,
    check_isolation,
    looks_like_test_run,
    open_store,
    read_header,
    real_default_root,
)

ROOT = Path(__file__).resolve().parents[3]


def _close(store: connection.StoreFile) -> None:
    store.conn.close()


# -- isolation guard ----------------------------------------------------------------------------


@pytest.fixture
def fake_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend the passwd home of this account is a temp dir; returns the 'real' root."""
    account = tmp_path / "account"
    account.mkdir()
    monkeypatch.setattr(connection, "_passwd_home", lambda: account)
    return account / ".indus"


def test_real_root_comes_from_passwd_not_home_env(
    fake_account: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/somewhere/else")
    assert real_default_root() == fake_account


def test_guard_refuses_real_root_and_named_profiles(fake_account: Path) -> None:
    env = {"PYTEST_CURRENT_TEST": "x"}
    with pytest.raises(StoreIsolationRefused):
        check_isolation(fake_account / "state.db", env)
    with pytest.raises(StoreIsolationRefused):
        check_isolation(fake_account / "profiles" / "work" / "state.db", env)
    # deeper scratch paths are allowed
    check_isolation(fake_account / "cache" / "scratch" / "x" / "state.db", env)
    check_isolation(fake_account / "profiles" / "work" / "sub" / "state.db", env)


def test_guard_runs_before_anything_touches_disk(
    fake_account: Path, scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_home = make_scope(fake_account, env=dict(os.environ), clock=scope.clock)
    with pytest.raises(StoreIsolationRefused):
        open_store(real_home)
    assert not fake_account.exists()


def test_bypass_env_lets_the_real_root_open(fake_account: Path, scope: Scope) -> None:
    env = {"PYTEST_CURRENT_TEST": "x", GUARD_BYPASS_ENV: "1"}
    real_home = make_scope(fake_account, env=env, clock=scope.clock)
    store = open_store(real_home, environ=env)
    try:
        assert store.path == fake_account / "state.db"
    finally:
        _close(store)


def test_outside_a_test_run_the_real_root_is_allowed(fake_account: Path) -> None:
    check_isolation(fake_account / "state.db", {}, ancestry=lambda: False)


def test_the_computed_real_root_is_refused_here() -> None:
    root = real_default_root()
    assert root is not None
    existed = root.exists()
    with pytest.raises(StoreIsolationRefused):
        check_isolation(root / "state.db")
    assert root.exists() == existed


def test_test_run_detection() -> None:
    assert looks_like_test_run({"PYTEST_CURRENT_TEST": "a::b"}, ancestry=lambda: False)
    assert looks_like_test_run({"INDUS_TEST_ISOLATION": "1"}, ancestry=lambda: False)
    assert looks_like_test_run({}, ancestry=lambda: True)
    assert not looks_like_test_run({}, ancestry=lambda: False)


def test_runner_names_in_argv() -> None:
    check = connection._cmdline_is_test_runner  # pyright: ignore[reportPrivateUsage]
    assert check(["/usr/bin/python3", "-m", "pytest", "-q"])
    assert check(["/home/u/.venv/bin/pytest"])
    assert check(["python", "-m", "unittest", "discover"])
    assert not check(["/tmp/pytest-of-u/pytest-3/x/indus", "chat"])
    assert not check(["indus", "chat"])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_ancestry_finds_pytest_from_a_child() -> None:
    code = "from indus.state import connection as c; print(c._has_test_runner_ancestor())"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "INDUS_"))}
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    )
    assert done.stdout.strip() == "True"


# -- parent folder ------------------------------------------------------------------------------


def test_default_store_path_comes_from_the_scope(
    scope: Scope, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The process env names the default root; the scope (work profile) decides.
    monkeypatch.setenv("INDUS_HOME", str(isolated_home))
    store = open_store(scope)
    try:
        assert store.path == scope.home / "state.db"
        assert not (isolated_home / "state.db").exists()
    finally:
        _close(store)


def test_deleted_profile_is_not_recreated(scope: Scope) -> None:
    root = scope.root
    shutil.rmtree(scope.home)
    tombstone(root, "work")
    with pytest.raises(ProfileTombstoned):
        open_store(scope)
    assert not scope.home.exists()


# -- quarantine ---------------------------------------------------------------------------------


def _backups(directory: Path, kind: str) -> list[Path]:
    return sorted(p for p in directory.iterdir() if f".{kind}-" in p.name and p.suffix == ".bak")


@pytest.mark.parametrize(("content", "kind"), [(b"", "zeroed"), (b"\0" * 4096, "zeroed")])
def test_zeroed_file_is_quarantined_with_sidecars(scope: Scope, content: bytes, kind: str) -> None:
    db = scope.home / "state.db"
    db.write_bytes(content)
    Path(f"{db}-wal").write_bytes(b"old wal")
    Path(f"{db}-shm").write_bytes(b"old shm")
    store = open_store(scope)
    try:
        assert store.quarantined_to is not None
        assert store.quarantined_to in _backups(scope.home, kind)
        assert Path(f"{store.quarantined_to}-wal").read_bytes() == b"old wal"
        assert Path(f"{store.quarantined_to}-shm").read_bytes() == b"old shm"
        store.conn.execute("CREATE TABLE t(x)")
        assert db.read_bytes()[:16] == SQLITE_MAGIC
    finally:
        _close(store)


def test_garbage_file_is_quarantined_as_notadb(scope: Scope) -> None:
    db = scope.home / "state.db"
    db.write_bytes(b"this is not a database at all" * 10)
    store = open_store(scope)
    try:
        assert store.quarantined_to is not None
        assert ".notadb-" in store.quarantined_to.name
        assert store.quarantined_to.read_bytes().startswith(b"this is not")
    finally:
        _close(store)


def test_backup_names_are_numbered_on_clash(scope: Scope) -> None:
    db = scope.home / "state.db"
    stamp = scope.clock.now().strftime("%Y%m%d-%H%M%S")
    taken = scope.home / f"state.db.zeroed-{stamp}-{os.getpid()}.bak"
    taken.write_bytes(b"earlier")
    db.write_bytes(b"")
    store = open_store(scope)
    try:
        assert store.quarantined_to == scope.home / f"{taken.stem}-1.bak"
        assert taken.read_bytes() == b"earlier"
    finally:
        _close(store)


def test_failed_move_of_an_invalid_file_refuses(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = scope.home / "state.db"
    db.write_bytes(b"garbage" * 20)

    def no_rename(src: object, dst: object) -> None:
        raise OSError("read-only mount")

    monkeypatch.setattr(connection.os, "rename", no_rename)
    with pytest.raises(StoreUnopenable):
        open_store(scope)
    assert db.read_bytes().startswith(b"garbage")


def _hold_quarantine_lock(scope: Scope) -> tuple[threading.Thread, threading.Event]:
    taken, done = threading.Event(), threading.Event()

    def hold() -> None:
        with FileLock(scope.home / connection.QUARANTINE_LOCK, timeout=5):
            taken.set()
            done.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert taken.wait(10)
    return holder, done


def test_busy_quarantine_lock_never_moves_an_invalid_file(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "QUARANTINE_LOCK_SECONDS", 0.05)
    db = scope.home / "state.db"
    db.write_bytes(b"garbage" * 20)
    holder, done = _hold_quarantine_lock(scope)
    try:
        with pytest.raises(StoreUnopenable):
            open_store(scope)
        assert db.read_bytes().startswith(b"garbage")
    finally:
        done.set()
        holder.join(10)


def test_busy_quarantine_lock_still_creates_a_missing_store(
    scope: Scope, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(connection, "QUARANTINE_LOCK_SECONDS", 0.05)
    holder, done = _hold_quarantine_lock(scope)
    try:
        store = open_store(scope)
        _close(store)
        assert any("going on without it" in r.getMessage() for r in caplog.records)
    finally:
        done.set()
        holder.join(10)


def test_valid_store_is_left_alone(scope: Scope) -> None:
    first = open_store(scope)
    first.conn.execute("CREATE TABLE t(x)")
    _close(first)
    second = open_store(scope)
    try:
        assert second.quarantined_to is None
        assert _backups(scope.home, "zeroed") == [] and _backups(scope.home, "notadb") == []
    finally:
        _close(second)


# -- modes and preflight ------------------------------------------------------------------------


def test_new_store_is_owner_only_even_with_umask_zero(scope: Scope) -> None:
    old = os.umask(0)
    try:
        store = open_store(scope)
    finally:
        os.umask(old)
    try:
        store.conn.execute("CREATE TABLE t(x)")
        assert os.stat(store.path).st_mode & 0o777 == 0o600
    finally:
        _close(store)


def test_existing_store_is_tightened(scope: Scope) -> None:
    first = open_store(scope)
    first.conn.execute("CREATE TABLE t(x)")
    _close(first)
    os.chmod(first.path, 0o644)
    second = open_store(scope)
    try:
        assert os.stat(second.path).st_mode & 0o777 == 0o600
    finally:
        _close(second)


def test_read_only_file_names_the_path(scope: Scope) -> None:
    first = open_store(scope)
    first.conn.execute("CREATE TABLE t(x)")
    _close(first)
    os.chmod(first.path, 0o444)
    try:
        with pytest.raises(StoreNotWritable) as caught:
            open_store(scope)
        assert str(first.path) in str(caught.value)
        assert caught.value.hint
    finally:
        os.chmod(first.path, 0o600)


def test_read_only_sidecar_is_reported(scope: Scope) -> None:
    first = open_store(scope)
    first.conn.execute("CREATE TABLE t(x)")
    _close(first)
    wal = Path(f"{first.path}-wal")
    wal.write_bytes(b"")
    os.chmod(wal, 0o444)
    try:
        with pytest.raises(StoreNotWritable) as caught:
            open_store(scope)
        assert str(wal) in str(caught.value)
    finally:
        os.chmod(wal, 0o600)


def test_connection_settings(scope: Scope) -> None:
    store = open_store(scope)
    try:
        conn = store.conn
        assert conn.isolation_level is None
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1000
        assert store.identity == connection.file_identity(store.path)
    finally:
        _close(store)


def test_on_connected_failure_closes_the_connection(scope: Scope) -> None:
    seen: list[sqlite3.Connection] = []

    def boom(conn: sqlite3.Connection) -> None:
        seen.append(conn)
        raise RuntimeError("setup failed")

    with pytest.raises(RuntimeError):
        open_store(scope, on_connected=boom)
    with pytest.raises(sqlite3.ProgrammingError):
        seen[0].execute("SELECT 1")


# -- header descriptor --------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor cache")
def test_header_reads_reuse_one_descriptor_and_follow_a_new_inode(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    sqlite3.connect(db).execute("CREATE TABLE t(x)").connection.close()
    assert (read_header(db) or b"").startswith(SQLITE_MAGIC)
    key = os.path.abspath(db)
    fd = connection._probe_fds[key][0]  # pyright: ignore[reportPrivateUsage]
    read_header(db)
    assert connection._probe_fds[key][0] == fd  # pyright: ignore[reportPrivateUsage]
    other = tmp_path / "y.db"
    other.write_bytes(b"different file")
    os.replace(other, db)
    assert read_header(db) == b"different file"
    assert connection._probe_fds[key][0] != fd  # pyright: ignore[reportPrivateUsage]
    connection.release_header_fd(db)


# -- two processes racing on one zeroed file ----------------------------------------------------

_RACER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from indus.foundation.scope import make_scope
    from indus.state.connection import open_store

    home = Path(sys.argv[1])
    store = open_store(
        make_scope(home), on_connected=lambda c: c.execute("CREATE TABLE IF NOT EXISTS t(x)")
    )
    print(store.quarantined_to or "")
    store.conn.close()
    """
)


@pytest.mark.slow
def test_processes_racing_on_a_zeroed_file_make_one_backup(scope: Scope) -> None:
    db = scope.home / "state.db"
    db.write_bytes(b"")
    env = dict(os.environ)
    env["INDUS_HOME"] = str(scope.home)
    env["PYTHONPATH"] = str(ROOT / "src")
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _RACER, str(scope.home)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        for _ in range(4)
    ]
    outputs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), outputs
    backups = _backups(scope.home, "zeroed")
    assert len(backups) == 1
    assert sum(1 for out, _err in outputs if out.strip()) == 1
    assert db.read_bytes()[:16] == SQLITE_MAGIC
