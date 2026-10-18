"""D-04.1 criterion (b) against the real ``terminal`` tool (P05-11/12, R02.3).

The whole tool path — normalise, plan, environment, guards, approval, foreground run, result — on
the real local backend: a command past its timeout is stopped (SIGTERM, then SIGKILL inside the
budget) and the tool answers by timeout + 2 s while the event loop keeps ticking. Real children,
real time, a few seconds per test."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from indus.backends import spawned
from indus.backends.contract import ExecResult
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.tools.builtin.terminal import GateRequest, GateVerdict, TerminalRuntime, TerminalTool

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not Path("/proc/self/stat").exists(),
    reason="needs bash and /proc",
)

TIMEOUT = 1
MAX_LOOP_GAP_S = 0.25


async def _approve(request: GateRequest) -> GateVerdict:
    return GateVerdict("approved")


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


async def _with_heartbeat(work: Awaitable[str]) -> tuple[dict[str, Any], float, float]:
    """Run ``work``; its parsed result, its wall time and the largest gap between 10 ms ticks."""
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
        text = await work
    finally:
        stop = True
        await beater
    return json.loads(text), time.monotonic() - started, max(gaps)


def _run(scope: Scope, command: str) -> tuple[dict[str, Any], float, float]:
    async def go() -> tuple[dict[str, Any], float, float]:
        shared = TerminalRuntime.create(clock=SystemClock())
        tool = TerminalTool(shared, gate=_approve, clock=SystemClock())
        try:
            return await _with_heartbeat(
                tool({"command": command, "timeout": TIMEOUT}, scope=scope, session_id="d041")
            )
        finally:
            await shared.processes.shutdown()
            await shared.environments.shutdown(fence=False)

    return asyncio.run(go())


def test_tool_stops_a_command_at_its_timeout(scope: Scope, tmp_path: Path) -> None:
    pid_file = tmp_path / "sleep.pid"
    body, elapsed, gap = _run(scope, f"echo $$ > {pid_file}; exec sleep 30")
    assert body["exit_code"] == 124 and body["error"] is None
    assert body["output"].endswith("[Command timed out after 1s]")
    assert "ran out of time" in body["hint"]
    assert TIMEOUT <= elapsed < TIMEOUT + 2
    assert gap < MAX_LOOP_GAP_S
    assert _gone(int(pid_file.read_text()))


def test_tool_sigkills_a_command_that_ignores_sigterm(scope: Scope, tmp_path: Path) -> None:
    pid_file = tmp_path / "loop.pid"
    body, elapsed, gap = _run(
        scope, f"trap '' TERM; echo $$ > {pid_file}; while :; do sleep 0.05; done"
    )
    assert body["exit_code"] == 124
    assert TIMEOUT <= elapsed < TIMEOUT + 2
    assert gap < MAX_LOOP_GAP_S
    assert _gone(int(pid_file.read_text()))


def test_tool_answers_through_the_backstop_when_the_wait_is_wedged(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inner wait never looks at its deadline: the hard bound of timeout + 2 s still kills
    the child, and the tool answers exit 124."""
    pid_file = tmp_path / "sleep.pid"
    real_wait: Callable[..., Awaitable[ExecResult]] = spawned.wait_process

    async def wedged_wait(handle: Any, capture: Any, **kwargs: Any) -> ExecResult:
        kwargs["timeout"] = 3600.0
        return await real_wait(handle, capture, **kwargs)

    monkeypatch.setattr(spawned, "wait_process", wedged_wait)
    body, elapsed, gap = _run(scope, f"echo $$ > {pid_file}; exec sleep 30")
    assert body["exit_code"] == 124
    assert body["output"].endswith("[Command timed out after 1s]")
    assert TIMEOUT + 2 <= elapsed < TIMEOUT + 2 + 1
    assert gap < MAX_LOOP_GAP_S
    assert _gone(int(pid_file.read_text()))
