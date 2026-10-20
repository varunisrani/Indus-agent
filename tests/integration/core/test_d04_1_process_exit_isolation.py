"""A host exit simulated in one test must not stop the next test's commands (root cause of the
wave-25 "load-only" failures of the D-04.1 terminal backstop tests).

The two tests run in file order in one interpreter. The first does what every host's exit path
does (``indus cron tick``, ``indus -z``, the gateway daemon's stop): it shuts the process's
terminal runtime down, which closes the shared environment registry and raises the one-way
foreground fence. Before the root ``reopen_after_a_host_exit`` fixture the fence stayed up for the
rest of the run, and the second test (like every terminal test after
``tests/unit/gateway/test_cron_host.py::test_cron_tick_refuses_while_a_live_gateway_owns_the_profile``
in a run that ordered it first) got rc 130, "[host is exiting: command not started]".
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from indus.backends.local import LocalEnvironment
from indus.backends.wait import EXITING_NOTE, FOREGROUND
from indus.foundation.scope import Scope
from indus.tools.builtin import terminal

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


async def _echo(scope: Scope, tmp_path: Path) -> tuple[int, str]:
    (tmp_path / "t").mkdir(exist_ok=True)
    env = LocalEnvironment(scope, cwd=str(tmp_path), timeout=30, temp_dir=str(tmp_path / "t"))
    await env.start()
    try:
        result = await env.execute("echo still-serving", timeout=10)
    finally:
        await env.cleanup()
    return result.returncode, result.output


def test_a_host_exit_raises_the_process_wide_fence(scope: Scope, tmp_path: Path) -> None:
    asyncio.run(terminal.runtime().shutdown())
    assert FOREGROUND.fenced
    code, output = asyncio.run(_echo(scope, tmp_path))
    assert (code, output) == (130, EXITING_NOTE)  # the product rule: nothing starts after exit


def test_the_next_test_can_spawn_again(scope: Scope, tmp_path: Path) -> None:
    assert not FOREGROUND.fenced
    code, output = asyncio.run(_echo(scope, tmp_path))
    assert code == 0 and "still-serving" in output
    assert not terminal.runtime().environments._closed  # pyright: ignore[reportPrivateUsage]
