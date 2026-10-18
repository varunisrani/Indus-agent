"""The ``terminal`` and ``process_manage`` tools on the real local backend (P05-11..14)."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.tools.builtin.process_manage import ProcessManageTool
from indus.tools.builtin.terminal import GateRequest, GateVerdict, TerminalRuntime, TerminalTool
from tests.fakes.terminal import Inbox

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not Path("/proc/self/stat").exists(),
    reason="needs bash and /proc",
)


async def _approve(request: GateRequest) -> GateVerdict:
    return GateVerdict("approved")


def test_session_state_background_job_and_completion(scope: Scope, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()

    async def go() -> tuple[list[dict[str, Any]], Inbox]:
        shared = TerminalRuntime.create(clock=SystemClock())
        tool = TerminalTool(shared, gate=_approve, clock=SystemClock())
        manage = ProcessManageTool(shared)
        inbox = Inbox()
        shared.deliveries.register(str(scope.home), "s1", inbox)

        async def run(**args: Any) -> dict[str, Any]:
            return json.loads(await tool(args, scope=scope, session_id="s1"))

        try:
            seen = [
                await run(command=f"cd {work} && export MARK=kept"),
                await run(command='printf "%s|%s" "$PWD" "$MARK"'),
                await run(command="echo from-job; exit 4", background=True, notify=True),
            ]
            job = seen[-1]["session_id"]
            waited = await manage(
                {"action": "wait", "session_id": job, "timeout": 20}, scope=scope, session_id="s1"
            )
            seen.append(json.loads(waited))
            return seen, inbox
        finally:
            await shared.processes.shutdown()
            await shared.environments.shutdown(fence=False)

    seen, inbox = asyncio.run(go())
    moved, state, started, waited = seen
    assert moved["exit_code"] == 0 and moved["cwd"] == str(work)
    assert state["output"] == f"{work}|kept"  # cwd and exported variables carried over
    assert started["output"] == "Background process started" and started["notify_on_complete"]
    assert waited["status"] == "exited" and waited["exit_code"] == 4
    assert "from-job" in waited["output"]
    assert inbox.kinds() == ["completion"] and inbox.events[0].exit_code == 4
