"""Shared bits for the LSP tests: the fake server's argv, its message log, a git work tree, and a
driver that moves a fake clock while real child processes do their I/O."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from indus.foundation.clock import SystemClock
from tests.fakes.clock import FakeClock

FAKE_SERVER = Path(__file__).resolve().parents[3] / "fakes" / "lsp_server.py"
REAL = SystemClock()


def server_argv(tmp_path: Path, name: str = "server", **script: Any) -> list[str]:
    """``[python, lsp_server.py, script.json]`` with ``script`` written beside the test."""
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    return [sys.executable, str(FAKE_SERVER), str(path)]


def log_of(path: Path) -> list[dict[str, Any]]:
    """Every message the fake server logged, in order (none before it starts)."""
    entries: list[dict[str, Any]] = []
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            entries.extend(json.loads(raw) for raw in handle if raw.strip())
    return entries


def methods(path: Path) -> list[str]:
    return [m["method"] for m in log_of(path) if "method" in m]


def git_tree(root: Path) -> Path:
    """A folder that counts as a git work tree (LSP only runs inside one)."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    return root


async def pause(seconds: float = 0.01) -> None:
    """Real time for child processes to answer (the product never waits like this)."""
    await REAL.sleep(seconds)


async def wait_until(predicate: Any, limit: float = 10.0) -> None:
    deadline = REAL.monotonic() + limit
    while not predicate():
        if REAL.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await pause()


async def drive[T](
    clock: FakeClock, work: Awaitable[T], step: float = 0.05, limit: float = 30.0
) -> T:
    """Run ``work`` while moving ``clock`` by ``step`` every 20 ms of real time."""
    task = asyncio.ensure_future(work)
    deadline = REAL.monotonic() + limit
    while not task.done():
        if REAL.monotonic() > deadline:
            task.cancel()
            raise AssertionError("work did not finish in time")
        await pause(0.02)
        clock.advance(step)
    return task.result()
