"""A terminal tool wired to a scripted environment, fake job handles and a fake clock."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from indus.backends.contract import Environment
from indus.backends.process_notify import Deliveries
from indus.backends.processes import LaunchSpec, ProcessRegistry
from indus.backends.registry import BackendEntry, CreateRequest, EnvironmentRegistry
from indus.foundation.scope import Scope
from indus.tools.builtin.process_manage import ProcessManageTool
from indus.tools.builtin.terminal import (
    GUARDS,
    ApprovalGate,
    GateRequest,
    GateVerdict,
    PreExecGuard,
    TerminalRuntime,
    TerminalTool,
)
from tests.fakes.clock import FakeClock, checkpoint, hold_for_off_loop
from tests.fakes.terminal import FakeJob, Inbox, ScriptedEnv, terminal_scope


async def approve_all(request: GateRequest) -> GateVerdict:
    return GateVerdict("approved")


class Rig:
    """Everything a terminal/process_manage test needs, on one fake clock."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        config: Mapping[str, Any] | None = None,
        gate: ApprovalGate = approve_all,
        guards: Sequence[PreExecGuard] = GUARDS,
        env: ScriptedEnv | None = None,
    ) -> None:
        self.clock = FakeClock(datetime(2026, 10, 18, tzinfo=UTC))
        base: dict[str, Any] = {"terminal": {"backend": "scripted"}}
        for key, value in (config or {}).items():
            if isinstance(value, Mapping) and isinstance(base.get(key), Mapping):
                base[key] = {**base[key], **value}
            else:
                base[key] = value
        self.scope: Scope = terminal_scope(tmp_path, base)
        self.env = env if env is not None else ScriptedEnv()
        self.created: list[CreateRequest] = []
        self.jobs: list[FakeJob] = []
        self.specs: list[LaunchSpec] = []
        self.fail_with: BaseException | None = None

        async def factory(request: CreateRequest) -> Environment:
            self.created.append(request)
            if self.fail_with is not None:
                raise self.fail_with
            return self.env

        environments = EnvironmentRegistry(
            [BackendEntry("scripted", factory, self.env.flags)], clock=self.clock
        )
        deliveries = Deliveries()
        processes = ProcessRegistry(
            deliveries, clock=self.clock, launcher=self._launch, watch_exits=False
        )
        self.shared = TerminalRuntime(environments, processes, deliveries)
        self.tool = TerminalTool(self.shared, gate=gate, guards=guards, clock=self.clock)
        self.manage = ProcessManageTool(self.shared)
        self.inbox = Inbox()

    def _launch(self, spec: LaunchSpec) -> FakeJob:
        self.specs.append(spec)
        job = FakeJob(pid=7000 + len(self.jobs))
        self.jobs.append(job)
        return job

    def deliverable(self, session_id: str = "s1") -> None:
        self.shared.deliveries.register(str(self.scope.home), session_id, self.inbox)

    async def call(self, session_id: str = "s1", **args: Any) -> dict[str, Any]:
        text = await self.tool(args, scope=self.scope, session_id=session_id, turn_id="t1")
        return json.loads(text)

    async def manage_call(self, conversation: str = "s1", **args: Any) -> dict[str, Any]:
        """``process_manage``; ``session_id`` in ``args`` is the process id, as in the schema."""
        text = await self.manage(args, scope=self.scope, session_id=conversation)
        return json.loads(text)

    def run(self, work: Awaitable[Any], *, hold: bool = True) -> Any:
        return asyncio.run(_drive(self.clock, work, hold=hold))


async def _drive(
    clock: FakeClock, work: Awaitable[Any], step: float = 0.5, *, hold: bool = True
) -> Any:
    """Run ``work``; whenever it is parked on the fake clock, move the clock on. With ``hold``
    the clock never moves while an awaited off-loop call (a guard on its pool, file work) is in
    flight; a test that simulates a call stuck in real time past a clock deadline passes
    ``hold=False``."""
    task = asyncio.ensure_future(work)
    for _ in range(100_000):
        for _ in range(20):
            await checkpoint()
        if task.done():
            return task.result()
        await asyncio.wait({task}, timeout=0.002)  # let worker threads hand results back
        if hold:
            await hold_for_off_loop(clock)
        if not task.done() and clock.pending:
            clock.advance(step)
    task.cancel()
    raise AssertionError("the work did not finish")
