"""Preflight: nothing spent on a run that cannot succeed, one alert per reason (P14-08, R07.16)."""

from __future__ import annotations

import asyncio
import dataclasses
import secrets
from pathlib import Path

import pytest

from indus.cron.jobroute import RouteChoice
from indus.cron.ports import JobSpec
from indus.cron.preflight import (
    GATEWAY_REASON,
    AlertBook,
    McpState,
    PreflightProbes,
    check,
    key_problem,
    platforms_of,
    structural,
)
from indus.cron.runner import AgentRunRequest, AgentRunResult, RunEnv, run_job
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig
from tests.unit.cron.world import make_world

JOB = JobSpec(id="a1b2c3d4e5f6", prompt="check the build", deliver="local")


@pytest.fixture
def real(scope: Scope) -> Scope:
    return dataclasses.replace(scope, clock=SystemClock())


def no_key_scope(scope: Scope, **config: object) -> Scope:
    tree: dict[str, object] = {"model": {"provider": "openai", "default": "gpt-5"}, **config}
    return scope.with_env(scope.env.with_overlay({"OPENAI_API_KEY": None})).with_config(
        DictConfig(tree)
    )


# -- structure and delivery ------------------------------------------------------------------------


def test_structural_problems(scope: Scope, tmp_path: Path) -> None:
    (scope.home / "scripts").mkdir(parents=True)
    assert structural(scope, JOB) is None
    assert "outside" in (structural(scope, dataclasses.replace(JOB, script="/etc/passwd")) or "")
    gone = dataclasses.replace(JOB, workdir=str(tmp_path / "vanished"))
    assert "does not exist" in (structural(scope, gone) or "")
    relative = dataclasses.replace(JOB, workdir="project")
    assert "absolute" in (structural(scope, relative) or "")
    bare = dataclasses.replace(JOB, no_agent=True)
    assert "names no script" in (structural(scope, bare) or "")


def test_platforms_of() -> None:
    assert platforms_of("telegram:-100123:42, local,Origin") == ["telegram", "local", "origin"]
    assert platforms_of(None) == [] and platforms_of(" ") == []


def test_platform_target_before_the_gateway_is_blocked(scope: Scope) -> None:
    probes = PreflightProbes(key=lambda *_: None)
    for deliver in ("telegram:-100123:42", "local,discord", "all"):
        job = dataclasses.replace(JOB, deliver=deliver)
        reason = check(scope, job, probes)
        assert reason is not None and "needs the gateway" in reason
    failing = dataclasses.replace(JOB, failure_deliver="slack:C1")
    assert check(scope, failing, probes) == GATEWAY_REASON.format(platform="slack")
    for deliver in ("local", "origin", "local,origin"):
        assert check(scope, dataclasses.replace(JOB, deliver=deliver), probes) is None
    known = PreflightProbes(key=lambda *_: None, deliverers=lambda: frozenset({"telegram"}))
    assert check(scope, dataclasses.replace(JOB, deliver="telegram:1"), known) is None


# -- provider key ----------------------------------------------------------------------------------


def test_missing_key_names_the_profile_and_home(scope: Scope) -> None:
    reason = key_problem(no_key_scope(scope), RouteChoice())
    assert reason is not None
    assert repr(scope.profile_id) in reason and str(scope.home) in reason
    assert check(no_key_scope(scope), JOB, PreflightProbes()) == reason


def test_unpinned_job_with_a_fallback_chain_skips_the_key_check(scope: Scope) -> None:
    asked: list[RouteChoice] = []

    def probe(_scope: Scope, route: RouteChoice) -> str | None:
        asked.append(route)
        return "no key"

    chained = no_key_scope(
        scope, fallback_providers=[{"provider": "anthropic", "model": "claude-x"}]
    )
    assert check(chained, JOB, PreflightProbes(key=probe)) is None
    assert asked == []
    pinned = dataclasses.replace(JOB, provider="openai", model="gpt-5")
    assert check(chained, pinned, PreflightProbes(key=probe)) == "no key"
    assert asked and asked[0].pinned


def test_a_check_that_breaks_fails_open(scope: Scope) -> None:
    def broken(_scope: Scope, _route: RouteChoice) -> str | None:
        raise RuntimeError("probe bug")

    assert check(scope, JOB, PreflightProbes(key=broken)) is None


# -- skills and MCP --------------------------------------------------------------------------------


def test_skill_probe_blocks(scope: Scope) -> None:
    probes = PreflightProbes(key=lambda *_: None, skills=lambda names: f"skill {names[0]} unset")
    with_skill = dataclasses.replace(JOB, skills=("weather",))
    assert check(scope, with_skill, probes) == "skill weather unset"
    assert check(scope, JOB, probes) is None  # no skills attached: not asked


def test_reconnecting_mcp_server_passes_and_empty_one_blocks(scope: Scope) -> None:
    states: dict[str, McpState] = {"github": "reconnecting", "notes": "empty", "jira": "ok"}
    configured = scope.with_config(
        DictConfig({"mcp_servers": {"github": {}, "notes": {}, "jira": {}}})
    )
    probes = PreflightProbes(key=lambda *_: None, mcp=lambda name: states.get(name))
    ok_job = dataclasses.replace(JOB, enabled_toolsets=("github", "jira", "file"))
    assert check(configured, ok_job, probes) is None
    bad_job = dataclasses.replace(JOB, enabled_toolsets=("notes", "file"))
    reason = check(configured, bad_job, probes)
    assert reason is not None and "notes" in reason and "clears by itself" in reason


# -- alert once ------------------------------------------------------------------------------------


def test_alert_book_alerts_once_per_reason(tmp_path: Path) -> None:
    book = AlertBook(tmp_path)
    assert book.should_alert("j1", "no key")
    assert not book.should_alert("j1", "no key")
    assert book.should_alert("j1", "skill missing")  # a different reason alerts at once
    assert book.should_alert("j2", "skill missing")
    book.clear("j1")
    assert book.should_alert("j1", "skill missing")


def test_unreadable_alert_book_alerts_again(tmp_path: Path) -> None:
    book = AlertBook(tmp_path)
    assert book.should_alert("j1", "no key")
    book.path.write_text("{not json", encoding="utf-8")
    assert book.should_alert("j1", "no key")


# -- in the pipeline -------------------------------------------------------------------------------


class CountingAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, request: AgentRunRequest) -> AgentRunResult:
        self.calls += 1
        return AgentRunResult(final_text="all good", completed=True)


def test_missing_key_blocks_without_a_model_call_and_alerts_once(real: Scope) -> None:
    world = make_world(real)
    agent = CountingAgent()
    blocked = no_key_scope(real)
    env = RunEnv(scope=blocked, ports=world.ports, agent=agent)

    outcomes = [asyncio.run(run_job(env, JOB)) for _ in range(5)]
    assert agent.calls == 0
    assert [o.status for o in outcomes] == ["blocked_config"] * 5
    assert [o.deliver for o in outcomes] == [True, False, False, False, False]
    assert "no usable credential" in (outcomes[0].error or "")

    # Fixed: a key in the store; the next run goes ahead and clears the alert record.
    assert real.secrets is not None
    value = "sk-" + "proj-" + secrets.token_hex(24)
    real.secrets.set("OPENAI_API_KEY", value)  # type: ignore[attr-defined]
    fixed = blocked.with_secrets(real.secrets)
    healthy = asyncio.run(run_job(RunEnv(scope=fixed, ports=world.ports, agent=agent), JOB))
    assert healthy.status == "ok" and agent.calls == 1
    assert AlertBook(real.home).should_alert(JOB.id, outcomes[0].error or "")


def test_preflight_off_skips_every_check(real: Scope) -> None:
    world = make_world(real)
    agent = CountingAgent()
    off = no_key_scope(real, cron={"preflight": False})
    job = dataclasses.replace(JOB, deliver="telegram:1")
    outcome = asyncio.run(run_job(RunEnv(scope=off, ports=world.ports, agent=agent), job))
    assert outcome.status == "ok" and agent.calls == 1
