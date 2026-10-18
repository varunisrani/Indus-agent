"""Atomic writes and the cross-process file lock (P01-07)."""

from __future__ import annotations

import errno
import fcntl
import logging
import multiprocessing
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.foundation import fsio
from indus.foundation.fsio import FileLock, LockTimeout, SymlinkRefused
from tests.fakes.clock import FakeClock


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _temps(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if ".tmp-" in p.name]


def test_new_file_is_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    fsio.atomic_write_text(target, "hello")
    assert target.read_text() == "hello"
    assert _mode(target) == 0o600


def test_existing_mode_is_preserved(tmp_path: Path) -> None:
    target = tmp_path / "shared.txt"
    target.write_text("old")
    target.chmod(0o640)
    fsio.atomic_write_text(target, "new")
    assert target.read_text() == "new"
    assert _mode(target) == 0o640


def test_explicit_mode_and_no_preserve(tmp_path: Path) -> None:
    target = tmp_path / "f"
    target.write_text("x")
    target.chmod(0o644)
    fsio.atomic_write_bytes(target, b"y", preserve_mode=False)
    assert _mode(target) == 0o600
    fsio.atomic_write_bytes(target, b"z", mode=0o640)
    assert _mode(target) == 0o640


def test_crash_mid_write_keeps_original_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "keep.txt"
    target.write_text("original")

    def boom(fd: int) -> None:
        raise OSError(errno.EIO, "disk on fire")

    monkeypatch.setattr(fsio.os, "fsync", boom)
    with pytest.raises(OSError, match="disk on fire"):
        fsio.atomic_write_text(target, "replacement")
    assert target.read_text() == "original"
    assert _temps(tmp_path) == []


def test_interrupt_mid_write_cleans_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "keep.txt"
    target.write_text("original")

    def interrupt(src: object, dst: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(fsio.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        fsio.atomic_write_text(target, "replacement")
    assert target.read_text() == "original"
    assert _temps(tmp_path) == []


def test_missing_parent_is_not_created(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        fsio.atomic_write_text(tmp_path / "gone" / "f.txt", "x")
    assert not (tmp_path / "gone").exists()


def test_symlink_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("real")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    with pytest.raises(SymlinkRefused):
        fsio.atomic_write_text(link, "evil")
    assert real.read_text() == "real"
    assert link.is_symlink()


def test_live_symlink_followed_keeps_link(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("old")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    written = fsio.atomic_write_text(link, "new", follow_symlink=True)
    assert written == real
    assert link.is_symlink()
    assert real.read_text() == "new"


def test_dangling_symlink_replaced_by_regular_file(tmp_path: Path) -> None:
    target_of_link = tmp_path / "elsewhere" / "victim.txt"
    link = tmp_path / "link.txt"
    link.symlink_to(target_of_link)
    fsio.atomic_write_text(link, "data", follow_symlink=True)
    assert not link.is_symlink()
    assert link.read_text() == "data"
    assert not target_of_link.exists()


def test_json_writer(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    fsio.atomic_write_json(target, {"b": 1, "a": "é"}, sort_keys=True)
    text = target.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert text.index('"a"') < text.index('"b"')
    assert "é" in text


# -- locks ----------------------------------------------------------------------------------------


def test_lock_reentrant_same_thread(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    first = FileLock(path, timeout=0.1)
    second = FileLock(path, timeout=0.1)
    with first:
        with second:
            assert first.held and second.held
        assert first.held
        assert path.read_text() == str(os.getpid())
    assert not first.held
    assert path.read_text() == ""


def test_lock_released_by_last_holder_even_if_other_object(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    first = FileLock(path, timeout=0.1)
    second = FileLock(path, timeout=0.1)
    first.acquire()
    second.acquire()
    first.release()
    second.release()
    # The kernel lock is free again: a raw non-blocking flock succeeds.
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_lock_timeout_names_holder_pid(tmp_path: Path) -> None:
    path = tmp_path / "held.lock"
    holder = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    os.write(holder, b"4242")
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    try:
        lock = FileLock(path, timeout=2.0, poll=0.5, clock=clock, wait=clock.advance)
        with pytest.raises(LockTimeout) as info:
            lock.acquire()
    finally:
        os.close(holder)
    assert info.value.holder_pid == 4242
    assert "4242" in str(info.value)
    assert clock.monotonic() >= 2.0
    assert not lock.held


def test_lock_degrades_without_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def no_locks(fd: int, op: int) -> None:
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(fcntl, "flock", no_locks)
    monkeypatch.setattr(fsio, "_degrade_warned", False)
    caplog.set_level(logging.WARNING, logger="indus.foundation.fsio")
    for name in ("a.lock", "b.lock"):
        with FileLock(tmp_path / name, timeout=0.1) as lock:
            assert lock.held
    warnings = [r for r in caplog.records if "not supported" in r.getMessage()]
    assert len(warnings) == 1


def test_release_unheld_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        FileLock(tmp_path / "z.lock").release()


def _hold_lock(path: str, ready: object, done: object) -> None:
    lock = FileLock(path, timeout=5.0)
    with lock:
        ready.set()  # type: ignore[attr-defined]
        done.wait(10)  # type: ignore[attr-defined]


@pytest.mark.slow
def test_second_process_times_out(tmp_path: Path) -> None:
    path = tmp_path / "proc.lock"
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    done = ctx.Event()
    proc = ctx.Process(target=_hold_lock, args=(str(path), ready, done))
    proc.start()
    try:
        assert ready.wait(20)
        with pytest.raises(LockTimeout) as info:
            FileLock(path, timeout=0.3, poll=0.05).acquire()
        assert info.value.holder_pid == proc.pid
    finally:
        done.set()
        proc.join(20)
    with FileLock(path, timeout=2.0):
        pass
