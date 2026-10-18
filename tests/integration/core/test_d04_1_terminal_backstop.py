"""D-04.1 criterion (b): a command with a timeout is killed by timeout + 2 s while the event loop
stays responsive (P05-02, R02.3). Real ``sleep`` children, real time, bounded to a few seconds."""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from indus.backends import spawned
from indus.backends.contract import ExecResult
from indus.backends.local import LocalEnvironment
from indus.foundation.scope import Scope

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not Path("/proc/self/stat").exists(),
    reason="needs bash and /proc",
)

TIMEOUT = 1.0
MAX_LOOP_GAP_S = 0.25


def _gone(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state in ("Z", "X")


async def _tick(seconds: float) -> None:
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    handle = loop.call_later(seconds, lambda: done.done() or done.set_result(None))
    try:
        await done
    finally:
        handle.cancel()


async def _with_heartbeat(work: Awaitable[ExecResult]) -> tuple[ExecResult, float, float]:
    """Run ``work``; return its result, its wall time and the largest gap between 10 ms ticks."""
    gaps: list[float] = []
    stop = False

    async def beat() -> None:
        last = time.monotonic()
        while not stop:
            await _tick(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    beater = asyncio.ensure_future(beat())
    started = time.monotonic()
    try:
        result = await work
    finally:
        stop = True
        await beater
    return result, time.monotonic() - started, max(gaps)


def _env(scope: Scope, tmp_path: Path) -> LocalEnvironment:
    (tmp_path / "t").mkdir()
    return LocalEnvironment(scope, cwd=str(tmp_path), timeout=60, temp_dir=str(tmp_path / "t"))


def test_timed_out_command_is_killed_while_the_loop_stays_responsive(
    scope: Scope, tmp_path: Path
) -> None:
    pid_file = tmp_path / "sleep.pid"

    async def go() -> tuple[ExecResult, float, float]:
        env = _env(scope, tmp_path)
        await env.start()
        try:
            return await _with_heartbeat(
                env.execute(f"sleep 30 & echo $! > {pid_file}; wait", timeout=TIMEOUT)
            )
        finally:
            await env.cleanup()

    result, elapsed, gap = asyncio.run(go())
    assert result.returncode == 124 and result.timed_out
    assert TIMEOUT <= elapsed < TIMEOUT + 2
    assert gap < MAX_LOOP_GAP_S
    assert _gone(int(pid_file.read_text()))


def test_backstop_kills_when_the_wait_itself_never_returns(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inner wait is wedged (it never looks at the deadline): the hard bound of timeout + 2 s
    still kills the real child and answers rc 124."""
    pid_file = tmp_path / "sleep.pid"
    real_wait: Callable[..., Awaitable[ExecResult]] = spawned.wait_process

    async def wedged_wait(handle: Any, capture: Any, **kwargs: Any) -> ExecResult:
        kwargs["timeout"] = 3600.0  # the wait's own deadline will never come in this test
        return await real_wait(handle, capture, **kwargs)

    monkeypatch.setattr(spawned, "wait_process", wedged_wait)

    async def go() -> tuple[ExecResult, float, float]:
        env = _env(scope, tmp_path)
        await env.start()
        try:
            return await _with_heartbeat(
                env.execute(f"sleep 30 & echo $! > {pid_file}; wait", timeout=TIMEOUT)
            )
        finally:
            await env.cleanup()

    result, elapsed, gap = asyncio.run(go())
    assert result.returncode == 124 and result.timed_out
    assert result.output.endswith("[Command timed out after 1s]")
    assert TIMEOUT + 2 <= elapsed < TIMEOUT + 2 + 1
    assert gap < MAX_LOOP_GAP_S
    assert _gone(int(pid_file.read_text()))
