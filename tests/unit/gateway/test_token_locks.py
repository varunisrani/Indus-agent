"""P15-06: machine-local credential locks (R06.16, R04.26)."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from indus.gateway import token_locks as tl
from indus.gateway.token_locks import (
    LockHolder,
    TokenLocks,
    credential_hash,
    default_lock_dir,
    lock_file_name,
    pid_alive,
    process_start_time,
)


def _token() -> str:
    # Built at runtime: never a credential-shaped literal in the repo.
    return "123456" + ":" + "fake" + "-token-" + "x" * 20


class World:
    """Pretend process table: pid -> start time (absent = dead)."""

    def __init__(self) -> None:
        self.procs: dict[int, int | None] = {}

    def alive(self, pid: int) -> bool:
        return pid in self.procs

    def start(self, pid: int) -> int | None:
        return self.procs.get(pid)


def _locks(
    directory: Path, world: World, pid: int, start: int | None = 1000, **kw: Any
) -> TokenLocks:
    world.procs[pid] = start
    return TokenLocks(directory, pid=pid, start_time=world.start, alive=world.alive, **kw)


def test_lock_name_hides_credential(tmp_path: Path) -> None:
    world = World()
    locks = _locks(tmp_path / "locks", world, 101, profile="work", home="/h/work")
    token = _token()
    outcome = locks.acquire("telegram", token, startup=True)
    assert outcome.acquired and not outcome.fatal
    path = tmp_path / "locks" / lock_file_name("telegram", token)
    assert path.name == f"telegram-{credential_hash(token)}.lock"
    assert len(credential_hash(token)) == 16
    text = path.read_text(encoding="utf-8")
    assert token not in text and token not in path.name
    record = json.loads(text)
    assert record["pid"] == 101 and record["start_time"] == 1000
    assert record["profile"] == "work" and record["platform"] == "telegram"
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "locks").stat().st_mode) == 0o700
    assert locks.held() == [path]
    assert locks.release("telegram", token)
    assert not path.exists()
    assert not locks.release("telegram", token)


def test_startup_conflict_is_fatal_and_midrun_retryable(tmp_path: Path) -> None:
    world = World()
    first = _locks(tmp_path, world, 101, profile="work")
    second = _locks(tmp_path, world, 202, start=2000, profile="play")
    token = _token()
    assert first.acquire("telegram", token, startup=True).acquired
    at_start = second.acquire("telegram", token, startup=True)
    assert not at_start.acquired and at_start.fatal and not at_start.retryable
    assert at_start.holder is not None and at_start.holder.pid == 101
    assert "profile work (pid 101)" in at_start.message
    assert token not in at_start.message
    mid_run = second.acquire("telegram", token, startup=False)
    assert not mid_run.acquired and mid_run.retryable and not mid_run.fatal
    # Another platform or another token is independent.
    assert second.acquire("discord", token, startup=True).acquired
    assert second.acquire("telegram", token + "2", startup=True).acquired
    # Once the first releases, the retry succeeds.
    first.release_all()
    assert second.acquire("telegram", token, startup=False).acquired


def test_same_process_reacquires(tmp_path: Path) -> None:
    world = World()
    locks = _locks(tmp_path, world, 101)
    token = _token()
    assert locks.acquire("slack", token, startup=True).acquired
    again = locks.acquire("slack", token, startup=False)
    assert again.acquired and again.holder is not None and again.holder.pid == 101


def test_dead_holder_reclaimed(tmp_path: Path) -> None:
    world = World()
    old = _locks(tmp_path, world, 101)
    token = _token()
    assert old.acquire("telegram", token, startup=True).acquired
    del world.procs[101]  # the holder died without releasing
    new = _locks(tmp_path, world, 202, start=5)
    outcome = new.acquire("telegram", token, startup=True)
    assert outcome.acquired
    assert outcome.holder is not None and outcome.holder.pid == 101
    record = json.loads((tmp_path / lock_file_name("telegram", token)).read_text(encoding="utf-8"))
    assert record["pid"] == 202
    # The old owner's release no longer touches the new owner's record.
    assert not old.release("telegram", token)
    assert (tmp_path / lock_file_name("telegram", token)).exists()


def test_recycled_pid_reclaimed(tmp_path: Path) -> None:
    world = World()
    old = _locks(tmp_path, world, 101, start=1000)
    token = _token()
    assert old.acquire("telegram", token, startup=True).acquired
    world.procs[101] = 9999  # pid 101 now belongs to a different process
    new = _locks(tmp_path, world, 202, start=5)
    assert new.acquire("telegram", token, startup=True).acquired


def test_unknown_start_time_counts_as_live(tmp_path: Path) -> None:
    world = World()
    old = _locks(tmp_path, world, 101, start=None)
    token = _token()
    assert old.acquire("telegram", token, startup=True).acquired
    new = _locks(tmp_path, world, 202, start=5)
    outcome = new.acquire("telegram", token, startup=True)
    assert not outcome.acquired and outcome.fatal


def test_garbage_record_reclaimed(tmp_path: Path) -> None:
    world = World()
    token = _token()
    path = tmp_path / lock_file_name("telegram", token)
    for junk in ("", "{not json", '{"pid": "x"}', '{"pid": -4}', "[1, 2]"):
        path.write_text(junk, encoding="utf-8")
        locks = _locks(tmp_path, world, 303)
        assert locks.acquire("telegram", token, startup=True).acquired, junk
        locks.release_all()


def test_empty_credential_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _locks(tmp_path, World(), 1).acquire("telegram", "", startup=True)


def test_holder_record_parsing() -> None:
    assert LockHolder.from_json({"pid": True}) is None
    holder = LockHolder.from_json({"pid": 7, "start_time": "x", "profile": None})
    assert holder is not None and holder.start_time is None and holder.profile == ""
    assert holder.describe() == "another gateway (pid 7)"


def test_default_dir_per_family(tmp_path: Path) -> None:
    home = tmp_path / "u"

    def user() -> Path:
        return home

    linux = default_lock_dir({}, family="linux", user_home=user)
    assert linux == home / ".local" / "state" / "indus" / "gateway-locks"
    xdg = default_lock_dir({"XDG_STATE_HOME": str(tmp_path / "st")}, family="linux", user_home=user)
    assert xdg == tmp_path / "st" / "indus" / "gateway-locks"
    mac = default_lock_dir({}, family="darwin", user_home=user)
    assert mac == home / "Library" / "Application Support" / "indus" / "gateway-locks"
    win = default_lock_dir({"LOCALAPPDATA": str(tmp_path / "la")}, family="win32", user_home=user)
    assert win == tmp_path / "la" / "indus" / "gateway-locks"
    override = default_lock_dir(
        {tl.LOCK_DIR_ENV: str(tmp_path / "o")}, family="linux", user_home=user
    )
    assert override == tmp_path / "o"
    relative_override = default_lock_dir({tl.LOCK_DIR_ENV: "rel"}, family="linux", user_home=user)
    assert relative_override == linux


def test_default_dir_ignores_relative_xdg(tmp_path: Path) -> None:
    got = default_lock_dir({"XDG_STATE_HOME": "state"}, family="linux", user_home=lambda: tmp_path)
    assert got == tmp_path / ".local" / "state" / "indus" / "gateway-locks"


def test_real_process_identity() -> None:
    assert pid_alive(os.getpid())
    assert not pid_alive(0)
    if sys.platform.startswith("linux"):
        mine = process_start_time(os.getpid())
        assert isinstance(mine, int) and mine == process_start_time(os.getpid())
        assert process_start_time(2**22 + 12345) is None


def _race(directory: str, token: str, ready: Any, go: Any, done: Any, results: Any) -> None:
    locks = TokenLocks(Path(directory), profile=f"p{os.getpid()}")
    ready.release()
    go.wait(30)
    outcome = locks.acquire("telegram", token, startup=True)
    results.put((os.getpid(), outcome.acquired, outcome.fatal))
    done.wait(30)  # stay alive so the winner's record stays live while the others check
    locks.release_all()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file locks")
def test_two_processes_race_exactly_one_wins(tmp_path: Path) -> None:
    ctx = mp.get_context("spawn")
    ready = ctx.Semaphore(0)
    go = ctx.Event()
    done = ctx.Event()
    results = ctx.Queue()
    token = _token()
    procs = [
        ctx.Process(target=_race, args=(str(tmp_path), token, ready, go, done, results))
        for _ in range(3)
    ]
    for proc in procs:
        proc.start()
    try:
        for _ in procs:
            assert ready.acquire(timeout=60)
        go.set()
        outcomes = [results.get(timeout=60) for _ in procs]
    finally:
        done.set()
        for proc in procs:
            proc.join(30)
    winners = [o for o in outcomes if o[1]]
    losers = [o for o in outcomes if not o[1]]
    assert len(winners) == 1
    assert all(fatal for _pid, _ok, fatal in losers)
    assert all(proc.exitcode == 0 for proc in procs)
