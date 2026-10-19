"""A profile with the real P14 cron backend and a scripted agent, for the gateway cron host tests.

``CronWorld`` gives the scope (a provider and its key so preflight passes), a ``CronHost`` whose
agent runs answer from a per-job script, and ``until`` / ``advance`` helpers that move the fake
clock while letting the daemon loop and worker threads run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from indus.cron.ledger import Attempt
from indus.cron.models import CronJob
from indus.cron.runner import AgentRunRequest, AgentRunResult
from indus.foundation.scope import Scope
from indus.gateway.cron_host import GatewayCronHost
from indus.service.cron_host import CronHost
from tests.fakes.agent import DictConfig
from tests.fakes.clock import FakeClock, checkpoint


def cron_scope(scope: Scope, extra: dict[str, Any] | None = None) -> Scope:
    key = "sk-" + "world-" + "0123456789abcdef" * 2  # built at run time (no literal)
    env = scope.env.with_overlay({"OPENAI_API_KEY": key, "ANTHROPIC_API_KEY": None})
    data: dict[str, Any] = {
        "model": {"provider": "openai", "default": "gpt-5"},
        "cron": {"max_parallel_jobs": 1},
        **(extra or {}),
    }
    return scope.with_env(env).with_config(DictConfig(data))


@dataclass
class CronWorld:
    scope: Scope
    clock: FakeClock
    replies: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    runs: list[str] = field(default_factory=list[str])
    hang: set[str] = field(default_factory=set[str])

    async def agent(self, request: AgentRunRequest) -> AgentRunResult:
        job_id = request.job.id
        self.runs.append(job_id)
        if job_id in self.hang:
            await asyncio.Event().wait()
        script = self.replies.get(job_id) or ["done"]
        text = script.pop(0) if len(script) > 1 else script[0]
        return AgentRunResult(final_text=text, completed=True, api_calls=1)

    def backend(self) -> CronHost:
        return CronHost(self.scope, agent=self.agent)

    def gateway_host(self, **kw: Any) -> GatewayCronHost:
        return GatewayCronHost(self.scope, backend=self.backend(), clock=self.clock, **kw)

    def create(self, **spec: Any) -> CronJob:
        job = self.backend().create(spec)
        assert isinstance(job, CronJob)
        return job

    def job(self, job_id: str) -> CronJob:
        found = self.backend().get(job_id)
        assert isinstance(found, CronJob)
        return found

    def attempts(self, job_id: str) -> list[Attempt]:
        with self.backend().opened() as ctx:
            return list(reversed(ctx.ledger.history(job_id, 100)))

    async def until(
        self, check: Callable[[], object], *, step: float = 1.0, limit: float = 10.0
    ) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + limit
        while not check():
            if loop.time() > end:
                raise AssertionError("the condition never held")
            await self.idle()
            self.clock.advance(step)

    async def idle(self, rounds: int = 3) -> None:
        for _ in range(rounds):
            await checkpoint()
            waiter = asyncio.get_running_loop().create_future()
            await asyncio.wait({waiter}, timeout=0.003)
            waiter.cancel()

    async def advance(self, seconds: float, *, step: float = 5.0) -> None:
        """Move the clock by ``seconds`` in steps, letting the loop and threads run."""
        left = seconds
        while left > 0:
            move = min(step, left)
            self.clock.advance(move)
            left -= move
            await self.idle()


__all__ = ["CronWorld", "cron_scope"]
