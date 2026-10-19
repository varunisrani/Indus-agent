"""A real cron world for tests: the profile's job store, ledger, notepad and health under the
host's ports (``service.cron_host.CronHost``), with a few seams tests need.

``make_world(scope)`` opens the profile's cron context (closed by the autouse fixture in this
folder's ``conftest``). ``world.ports`` are the host's real ports, wrapped so a test can:
switch the store health off (``world.writable[0] = False``), count reaps (``world.reaped``), see
every ``finish`` call (``world.finished``) and, with ``with_monitor=True``, answer the monitor
gate itself (``world.monitor_answers``). ``world.add(**fields)`` writes a job record directly
(an ``every 30m`` prompt job unless told otherwise; ``record(**fields)`` builds one without
storing it); ``world.rows()`` lists the ledger attempts,
oldest first, as dicts.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from indus.cron.classify import RunOutcome
from indus.cron.context import CronContext
from indus.cron.grammar import IntervalSchedule, parse_schedule
from indus.cron.models import CronJob, new_job_id
from indus.cron.ports import CronPorts, JobSpec, MonitorCheck, RunReport
from indus.cron.preflight import PreflightProbes
from indus.cron.runner import AgentRunner
from indus.foundation.scope import Scope
from indus.service.cron_host import CronHost

OPEN: list[CronContext] = []
"""Contexts opened by ``make_world``; the folder's conftest closes them after each test."""


EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def record(**fields: Any) -> CronJob:
    """A job record that is not stored: an ``every 30m`` prompt job unless told otherwise."""
    data: dict[str, Any] = {
        "id": "abc123abc123",
        "created_at": EPOCH,
        "prompt": "check the build",
        "schedule": IntervalSchedule(minutes=30, display="every 30m"),
        "schedule_display": "every 30m",
    }
    data.update(fields)
    return CronJob.model_validate(data)


@dataclass
class Finished:
    """What ``finish`` was handed (the real finish runs too)."""

    calls: list[tuple[JobSpec, RunOutcome]] = field(
        default_factory=list[tuple[JobSpec, RunOutcome]]
    )

    @property
    def outcomes(self) -> list[RunOutcome]:
        return [o for _, o in self.calls]


@dataclass
class World:
    scope: Scope
    host: CronHost
    ctx: CronContext
    ports: CronPorts
    finished: Finished
    writable: list[bool]
    monitor_answers: list[MonitorCheck] = field(default_factory=list[MonitorCheck])
    reaped: int = 0

    @property
    def store(self) -> Any:
        return self.ctx.store

    @property
    def ledger(self) -> Any:
        return self.ports.ledger

    def add(self, schedule: str = "every 30m", **fields: Any) -> CronJob:
        """Write one job record as it stands (scheduler fields included)."""
        now = self.scope.clock.now()
        parsed = parse_schedule(schedule, None, now=now)
        data: dict[str, Any] = {
            "id": new_job_id(),
            "created_at": now,
            "prompt": "check the build",
            "schedule": parsed,
            "schedule_display": parsed.display,
        }
        if parsed.kind == "once":
            data["repeat"] = {"times": 1}
        data.update(fields)
        job = CronJob.model_validate(data)
        with self.ctx.store.transaction() as jobs:
            jobs.append(job)
        return job

    def get(self, job_id: str) -> CronJob | None:
        return self.ctx.store.get(job_id)

    def rows(self) -> list[dict[str, Any]]:
        attempts = self.ctx.ledger.recent(1000)
        return [dataclasses.asdict(a) for a in reversed(attempts)]


def make_world(
    scope: Scope,
    *,
    agent: AgentRunner | None = None,
    probes: PreflightProbes | None = None,
    with_monitor: bool = False,
) -> World:
    host = CronHost(scope, agent=agent, probes=probes)
    ctx = CronContext.open(scope)
    OPEN.append(ctx)
    real = host.ports(ctx)
    world = World(scope, host, ctx, real, Finished(), [True])

    def writable(now: datetime) -> bool:
        return world.writable[0] and real.store_writable(now)

    def reap(now: datetime) -> int:
        world.reaped += 1
        return reap_real(now)

    reap_real = real.ledger.reap
    real.ledger.reap = reap  # type: ignore[method-assign]

    finish_real = real.finish
    assert finish_real is not None

    async def finish(job: JobSpec, outcome: RunOutcome) -> str | None:
        world.finished.calls.append((job, outcome))
        return await finish_real(job, outcome)

    async def monitor(job: JobSpec) -> MonitorCheck:
        return world.monitor_answers.pop(0) if world.monitor_answers else MonitorCheck("changed")

    gate: Callable[[JobSpec], Awaitable[MonitorCheck]] | None = (
        monitor if with_monitor else real.monitor
    )
    world.ports = dataclasses.replace(real, store_writable=writable, finish=finish, monitor=gate)
    return world


class RecordingHost(CronHost):
    """The real backend that also records ``create`` / ``update`` / ``run`` calls; ``runner``
    answers ``run_now`` instead of a real run, ``running`` marks jobs busy elsewhere."""

    def __init__(self, scope: Scope, **kw: Any) -> None:
        super().__init__(scope, **kw)
        self.calls: list[tuple[str, Any]] = []
        self.running: set[str] = set()
        self.runner: Callable[[str, str | None], Awaitable[RunReport]] | None = None

    @property
    def jobs(self) -> dict[str, CronJob]:
        return {job.id: job for job in self.store.load()}

    def add(self, **fields: Any) -> CronJob:
        """Write one record as it stands (``every 30m`` prompt job by default)."""
        fields.setdefault("id", new_job_id())
        fields.setdefault("created_at", self.scope.clock.now())
        job = record(**fields)
        with self.store.transaction() as jobs:
            jobs.append(job)
        return job

    def create(self, spec: Mapping[str, Any]) -> Any:
        self.calls.append(("create", dict(spec)))
        return super().create(spec)

    def update(self, job_id: str, changes: Mapping[str, Any]) -> Any:
        self.calls.append(("update", (job_id, dict(changes))))
        return super().update(job_id, changes)

    def is_running(self, job_id: str) -> bool:
        return job_id in self.running or super().is_running(job_id)

    async def run_now(self, job_id: str, *, context: str | None = None) -> RunReport:
        self.calls.append(("run", (job_id, context)))
        if self.runner is not None:
            return await self.runner(job_id, context)
        return await super().run_now(job_id, context=context)
