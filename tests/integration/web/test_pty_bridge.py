"""P18-07: the PTY bridge over a real pseudo-terminal and a fake TUI child.

The child is ``tests/fakes/pty/fake_tui.py`` in raw mode: it answers every input chunk with
``got:<hex>``, prints its size on ``size?`` and exits on ``quit``. Needs ``ptyprocess`` (the
``pty`` extra) on POSIX; the Windows load failure is checked everywhere.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from indus.foundation.clock import SystemClock
from indus.web.pty import bridge as pty_bridge
from indus.web.pty.bridge import WINDOWS_NOTICE, PtyBridge, PtyUnavailable
from indus.web.pty.resize import TermSize
from tests.unit.backends._aio import aio

REPO = Path(__file__).resolve().parents[3]
needs_pty = pytest.mark.skipif(
    sys.platform == "win32" or pty_bridge.unavailable() is not None,
    reason="needs a POSIX PTY and ptyprocess (the pty extra)",
)


def child_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.defpath + os.pathsep + str(Path(sys.executable).parent),
        "PYTHONPATH": str(REPO),
        "HOME": str(home),
        "LANG": "C.UTF-8",
    }


def start(home: Path, mode: str = "echo", size: TermSize = pty_bridge.DEFAULT_SIZE) -> PtyBridge:
    argv = [sys.executable, "-m", "tests.fakes.pty.fake_tui", mode]
    return pty_bridge.spawn(argv, cwd=home, env=child_env(home), size=size)


async def read_until(bridge: PtyBridge, needle: bytes, timeout: float = 15.0) -> bytes:
    seen = b""
    async with asyncio.timeout(timeout):
        while needle not in seen:
            chunk = await bridge.read()
            if chunk is None:
                raise AssertionError(f"EOF before {needle!r}; saw {seen!r}")
            seen += chunk
    return seen


async def read_to_eof(bridge: PtyBridge, timeout: float = 15.0) -> bytes:
    seen = b""
    async with asyncio.timeout(timeout):
        while (chunk := await bridge.read()) is not None:
            seen += chunk
    return seen


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:  # a zombie waiting for a non-reaping init counts as gone
        state = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state == b"Z"


@needs_pty
@aio
async def test_input_is_echoed_and_size_applies(tmp_path: Path) -> None:
    bridge = start(tmp_path, size=TermSize(90, 30))
    try:
        assert b"ready 90x30" in await read_until(bridge, b"ready")
        assert await bridge.write(b"hi")
        assert b"got:" + b"hi".hex().encode() in await read_until(bridge, b"got:")
        bridge.resize(TermSize.of(5000, 5000))
        assert await bridge.write(b"size?")
        assert b"size 2000x1000" in await read_until(bridge, b"size 2")
        assert bridge.is_alive()
    finally:
        await bridge.close()
    assert not bridge.is_alive()
    assert not await bridge.write(b"late")
    assert await bridge.read() is None


@needs_pty
@aio
async def test_eof_when_the_child_exits_and_no_zombie(tmp_path: Path) -> None:
    bridge = start(tmp_path)
    await read_until(bridge, b"ready")
    assert await bridge.write(b"quit")
    await read_to_eof(bridge)
    pid = bridge.pid
    await bridge.close()
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)  # already reaped by the bridge
    await bridge.close()  # idempotent


@needs_pty
@aio
async def test_a_child_that_stops_reading_stalls_the_write(tmp_path: Path) -> None:
    bridge = start(tmp_path, mode="deaf")
    try:
        await read_until(bridge, b"ready")
        assert not await bridge.write(b"x" * (4 * 1024 * 1024), timeout=0.5)
    finally:
        await bridge.close()


@needs_pty
@aio
async def test_close_ends_a_helper_that_ignores_hangup(tmp_path: Path) -> None:
    bridge = start(tmp_path, mode="helper")
    helper: int | None = None
    try:
        seen = await read_until(bridge, b"helper ")
        while b"\r\n" not in seen.split(b"helper ", 1)[1]:
            seen += await read_until(bridge, b"\n")
        helper = int(seen.split(b"helper ", 1)[1].split(b"\r\n", 1)[0])
        assert not gone(helper)
        await bridge.close()
        async with asyncio.timeout(5):
            while not gone(helper):
                await SystemClock().sleep(0.05)  # a real process: real time
        assert gone(bridge.pid)
    finally:
        await bridge.close()
        if helper is not None and not gone(helper):
            os.kill(helper, signal.SIGKILL)


@needs_pty
@aio
async def test_the_child_leads_its_own_process_group(tmp_path: Path) -> None:
    bridge = start(tmp_path)
    try:
        await read_until(bridge, b"ready")
        assert os.getpgid(bridge.pid) == bridge.pid
        assert os.getpgid(bridge.pid) != os.getpgid(0)
    finally:
        await bridge.close()


@needs_pty
@aio
async def test_a_hangup_aware_child_ends_inside_the_grace(tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    bridge = start(tmp_path)
    await read_until(bridge, b"ready")
    began = loop.time()
    await bridge.close()
    assert loop.time() - began < pty_bridge.GRACE_S
    assert pty_bridge.GRACE_S == 1.5 and pty_bridge.MAX_GRACE_S == 2.0


def test_windows_without_conpty_explains_wsl2(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_winpty() -> object:
        raise ImportError("no pywinpty here")

    monkeypatch.setattr(pty_bridge, "_load_windows", no_winpty)
    assert pty_bridge.unavailable("win32") == WINDOWS_NOTICE
    assert "WSL2" in WINDOWS_NOTICE
    with pytest.raises(PtyUnavailable, match="WSL2"):
        pty_bridge.spawn(["indus"], cwd=None, env={}, platform="win32")


def test_posix_without_ptyprocess_says_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_ptyprocess() -> object:
        raise ImportError("no ptyprocess")

    monkeypatch.setattr(pty_bridge, "_load_posix", no_ptyprocess)
    reason = pty_bridge.unavailable("linux")
    assert reason is not None and "dashboard extra" in reason
    with pytest.raises(PtyUnavailable):
        pty_bridge.spawn(["indus"], cwd=None, env={}, platform="linux")


def test_spawn_backfills_term_and_keeps_the_given_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}
    read_end, write_end = os.pipe()

    class FakeProcess:
        fd = read_end
        pid = 4242

        @classmethod
        def spawn(cls, argv: list[str], **kw: object) -> FakeProcess:
            seen.update(kw, argv=argv)
            return cls()

    class FakeModule:
        PtyProcess = FakeProcess

    monkeypatch.setattr(pty_bridge, "_load_posix", lambda: FakeModule)
    try:
        pty_bridge.spawn(["x"], cwd=tmp_path, env={"A": "1"}, platform="linux")
        assert seen["env"] == {"A": "1", "TERM": "xterm-256color"}
        assert seen["dimensions"] == (24, 80) and seen["cwd"] == str(tmp_path)
        pty_bridge.spawn(["x"], cwd=None, env={"TERM": "vt100"}, platform="linux")
        assert seen["env"] == {"TERM": "vt100"}
    finally:
        os.close(read_end)
        os.close(write_end)
