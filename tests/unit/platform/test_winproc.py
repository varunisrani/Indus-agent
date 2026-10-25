"""Win32 process facts (docs/notes/P25/windows-gateway.md): liveness without ``os.kill``, start
fingerprints, owner SIDs and the detached start. The real cases need Windows."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from indus.platform import winproc
from indus.state import gateway_runtime

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Win32 calls")


def _pause(seconds: float) -> None:
    threading.Event().wait(seconds)


def _sleeper(seconds: float) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


@pytest.mark.skipif(sys.platform == "win32", reason="the non-Windows answers")
def test_other_platforms_answer_unknown() -> None:
    me = os.getpid()
    assert winproc.pid_alive(me) is False
    assert winproc.start_time(me) is None
    assert winproc.process_user_sid(me) is None
    assert winproc.same_user(me) is None
    with open(os.devnull, "wb") as out, pytest.raises(OSError, match="POSIX"):
        winproc.spawn_detached(["true"], cwd="/", env={}, out=out)


@windows_only
def test_liveness_of_ourselves_and_an_exited_child() -> None:
    assert winproc.pid_alive(os.getpid())
    child = _sleeper(0)
    child.wait()
    assert not winproc.pid_alive(child.pid)  # its handle is still open here: exited is gone
    assert not winproc.pid_alive(0) and not winproc.pid_alive(-1)


@windows_only
def test_probing_a_live_process_never_ends_it() -> None:
    # os.kill(pid, 0) terminates a process on Windows; both probes must leave it running.
    child = _sleeper(30)
    try:
        assert winproc.pid_alive(child.pid)
        assert gateway_runtime.pid_alive(child.pid)
        _pause(0.2)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


@windows_only
def test_start_time_is_a_stable_fingerprint() -> None:
    child = _sleeper(30)
    try:
        first = winproc.start_time(child.pid)
        assert first is not None and first == winproc.start_time(child.pid)
        assert first != winproc.start_time(os.getpid())
        assert gateway_runtime.process_start_time(child.pid) == first
        assert gateway_runtime.alive(child.pid, first)
        assert not gateway_runtime.alive(child.pid, first + 1)  # a recycled pid
    finally:
        child.kill()
        child.wait()


@windows_only
def test_a_child_runs_as_our_user() -> None:
    own = winproc.own_user_sid()
    assert own is not None and len(own) >= 8
    child = _sleeper(30)
    try:
        assert winproc.process_user_sid(child.pid) == own
        assert winproc.same_user(child.pid) is True
    finally:
        child.kill()
        child.wait()


@windows_only
def test_spawn_detached_outlives_and_writes_to_out(tmp_path: Path) -> None:
    log = tmp_path / "out.log"
    script = "import sys, time; print('hello', flush=True); time.sleep(30)"
    with open(log, "ab") as out:
        pid = winproc.spawn_detached(
            [sys.executable, "-c", script], cwd=str(tmp_path), env=dict(os.environ), out=out
        )
    try:
        deadline = time.monotonic() + 10
        while b"hello" not in log.read_bytes() and time.monotonic() < deadline:
            _pause(0.05)
        assert b"hello" in log.read_bytes()
        assert winproc.pid_alive(pid)
    finally:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
