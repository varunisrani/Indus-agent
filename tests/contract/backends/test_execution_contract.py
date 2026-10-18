"""The execution contract every backend meets (P05-01/02/04): ACC-D1-1.

Runs once per backend listed in ``conftest.contract_entries`` (the local backend with real bash,
and the fake backend). Real waits are bounded: the longest case ends at a 1 s command deadline.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

from indus.backends.contract import BackendFlags, Environment, ExecResult
from tests.contract.backends.conftest import (
    BIG_COMMAND,
    BIG_OUTPUT,
    ODD_COMMAND,
    ODD_OUTPUT,
    Backend,
)


def _run(backend: Backend, command: str, **kwargs: object) -> tuple[ExecResult, str]:
    async def go() -> tuple[ExecResult, str]:
        async with backend.open() as env:
            result = await env.execute(command, **kwargs)  # type: ignore[arg-type]
            return result, env.cwd

    return asyncio.run(go())


def test_meets_the_protocol(backend: Backend) -> None:
    async def go() -> Environment:
        async with backend.open() as env:
            assert isinstance(env, Environment)
            assert isinstance(env.flags, BackendFlags)
            assert env.name == backend.name and env.describe()
            return env

    env = asyncio.run(go())
    asyncio.run(env.cleanup())  # a second cleanup is harmless


def test_output_status_and_marker_stripped(backend: Backend) -> None:
    result, cwd = _run(backend, "printf %s hello")
    assert result.output == "hello" and result.returncode == 0
    assert result.cwd_observed == backend.workdir == cwd
    assert not result.timed_out and not result.interrupted and "__INDUS_CWD_" not in result.output


def test_trailing_newline_kept(backend: Backend) -> None:
    result, _ = _run(backend, "echo two words")
    assert result.output == "two words\n"


def test_cd_moves_the_environment(backend: Backend) -> None:
    async def go() -> tuple[ExecResult, ExecResult, str]:
        async with backend.open() as env:
            moved = await env.execute("cd sub")
            again = await env.execute("printf %s here")
            return moved, again, env.cwd

    moved, again, cwd = asyncio.run(go())
    target = os.path.join(backend.workdir, "sub")
    assert moved.cwd_observed == target and cwd == target
    assert again.cwd_observed == target and again.output == "here"


def test_exit_without_marker_keeps_the_cwd(backend: Backend) -> None:
    result, cwd = _run(backend, "exit 3")
    assert result.returncode == 3 and result.cwd_observed is None
    assert cwd == backend.workdir


def test_odd_output_survives_marker_parsing(backend: Backend) -> None:
    result, _ = _run(backend, ODD_COMMAND)
    assert result.output == ODD_OUTPUT and result.returncode == 0


def test_deadline_gives_124_and_the_flag(backend: Backend) -> None:
    started = time.monotonic()
    result, cwd = _run(backend, "sleep 30", timeout=1)
    elapsed = time.monotonic() - started
    assert result.returncode == 124 and result.timed_out
    assert result.output.endswith("[Command timed out after 1s]")
    assert result.cwd_observed is None and cwd == backend.workdir
    assert elapsed < 1 + 2 + 1.5  # timeout + backstop grace, plus environment start-up


def test_own_exit_124_is_not_our_timeout(backend: Backend) -> None:
    result, _ = _run(backend, "exit 124")
    assert result.returncode == 124 and not result.timed_out


def test_interrupt_gives_130(backend: Backend) -> None:
    stop = threading.Event()

    async def go() -> ExecResult:
        async with backend.open() as env:
            asyncio.get_running_loop().call_later(0.3, stop.set)
            return await env.execute("sleep 30", timeout=20, cancel=stop)

    started = time.monotonic()
    result = asyncio.run(go())
    assert result.returncode == 130 and result.interrupted and not result.timed_out
    assert result.output.endswith("[Command interrupted]")
    assert time.monotonic() - started < 5


def test_stdin_reaches_the_command(backend: Backend) -> None:
    result, _ = _run(backend, "cat", stdin="first\nsecond")
    assert result.output == "first\nsecond" and result.stdin_error is None


def test_bounded_capture_cuts_and_keeps_the_full_text(backend: Backend) -> None:
    result, _ = _run(backend, BIG_COMMAND)
    assert len(result.output) <= 50_000 and result.output.startswith("x" * 19_000)
    assert result.output_total_chars is not None and result.output_total_chars >= 60_000
    assert result.full_output_path is not None
    assert Path(result.full_output_path).read_text().startswith(BIG_OUTPUT)


def test_unbounded_capture_keeps_everything(backend: Backend) -> None:
    result, _ = _run(backend, BIG_COMMAND, bounded=False)
    assert result.output == BIG_OUTPUT and result.full_output_path is None
