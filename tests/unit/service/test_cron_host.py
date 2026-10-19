"""The cron host wiring (P14): one backend for the process, the curator's reference source at
host start, the MCP preflight probe and the delivery-target preflight."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from indus.cron import ports, preflight
from indus.cron.jobroute import RouteChoice
from indus.cron.ports import JobSpec
from indus.cron.preflight import PreflightProbes
from indus.cron.runner import AgentRunRequest, AgentRunResult, run_job
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.learning import curator
from indus.learning.skills import project_trust
from indus.mcp import discovery
from indus.mcp.discovery import ServerReport
from indus.service import cron_host
from tests.fakes.clock import FakeClock
from tests.unit.service.test_host_inprocess import host_for


def test_install_registers_the_host_backend_once(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ports, "BACKEND_FACTORY", None)
    cron_host.install()
    assert ports.BACKEND_FACTORY is cron_host.CronHost
    assert isinstance(ports.backend_for(scope), cron_host.CronHost)

    def other(s: Scope) -> Any:
        return object()

    monkeypatch.setattr(ports, "BACKEND_FACTORY", other)
    cron_host.install()  # a factory already set (a test, another host) is kept
    assert ports.BACKEND_FACTORY is other


def test_host_start_protects_the_skills_of_cron_jobs(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    curator.clear_reference_sources()
    cron_host.CronHost(scope).create(
        {"schedule": "every 1h", "prompt": "digest", "skills": ["nightly-report"]}
    )
    host = host_for(scope, fake_clock, tmp_path)
    with host:
        assert [s.name for s in curator.reference_sources()] == ["cron"]
        assert curator.referenced_skills() == {"nightly-report"}
    assert curator.reference_sources() == []


def test_mcp_probe_reads_the_host_status(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = cron_host._mcp_probe(scope)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(discovery, "host_for", lambda _scope: None)
    assert probe("files") is None  # no MCP host in this process: not checked
    reports = [
        ServerReport("files", "ready", "stdio", tools=3),
        ServerReport("flaky", "reconnecting", "http", tools=0),
        ServerReport("dead", "parked", "http", tools=0),
    ]
    fake = SimpleNamespace(status=lambda: reports)
    monkeypatch.setattr(discovery, "host_for", lambda _scope: fake)
    assert probe("files") == "ok"
    assert probe("flaky") == "reconnecting"  # never blocks (R07.16)
    assert probe("dead") == "empty"
    assert probe("unknown") is None


def test_delivery_preflight_reads_targets_with_the_parser(scope: Scope) -> None:
    probes = PreflightProbes(key=lambda *_: None)
    job = JobSpec(id="a1b2c3d4e5f6", prompt="p", deliver="myspace:1")
    assert "cannot be used" in (preflight.check(scope, job, probes) or "")
    gateway = JobSpec(id="a1b2c3d4e5f6", prompt="p", deliver="local, telegram:-100:7")
    assert "needs the gateway" in (preflight.check(scope, gateway, probes) or "")
    fine = JobSpec(id="a1b2c3d4e5f6", prompt="p", deliver="origin,local")
    assert preflight.check(scope, fine, probes) is None


def test_a_run_reads_project_skills_from_the_job_workdir(scope: Scope, tmp_path: Path) -> None:
    """``project_trust.bind_project_cwd(job.workdir)`` covers the whole run: preflight, skill
    loading and the agent (P13-28)."""
    real = dataclasses.replace(scope, clock=SystemClock())
    work = tmp_path / "repo"
    work.mkdir()
    seen: dict[str, Path] = {}

    def key(_scope: Scope, _route: Any) -> None:
        seen["preflight"] = project_trust.project_cwd(real)
        return None

    async def agent(request: AgentRunRequest) -> AgentRunResult:
        seen["agent"] = project_trust.project_cwd(real)
        return AgentRunResult("done", completed=True)

    host = cron_host.CronHost(real, agent=agent, probes=PreflightProbes(key=key))
    with host.opened() as ctx:
        job = JobSpec(id="a1b2c3d4e5f6", prompt="p", workdir=str(work))
        outcome = asyncio.run(run_job(host.env(ctx), job))
    assert outcome.status == "ok"
    assert seen == {"preflight": work, "agent": work}
    assert project_trust.project_cwd(real) != work  # only for the run


def test_the_agent_runner_binds_the_workdir_itself(scope: Scope, tmp_path: Path) -> None:
    runner = cron_host.CronAgentRunner(scope)
    seen: list[Path] = []

    async def fake_run(_scope: Scope, _request: AgentRunRequest) -> AgentRunResult:
        seen.append(project_trust.project_cwd(scope))
        return AgentRunResult("done", completed=True)

    runner._run = fake_run  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    request = AgentRunRequest(
        job=JobSpec(id="a1b2c3d4e5f6", prompt="p", workdir=str(tmp_path)),
        prompt="p",
        session_id="cron_a1b2c3d4e5f6_20260101_090000",
        toolsets=["indus-cron"],
        denied_tools=frozenset({"clarify", "cronjob_manage"}),
        route=RouteChoice(),
        workdir=tmp_path,
        inactivity_s=600.0,
    )
    asyncio.run(runner(request))
    assert seen == [tmp_path]
