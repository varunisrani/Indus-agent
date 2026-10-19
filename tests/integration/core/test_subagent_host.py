"""P10 integration of delegation and ``execute_code`` with the CLI host (slices G + H on P09).

Through the real CLI path (``cli.runtime.build_service`` → ``agent_factory`` → ``learning.host``):

- a subagent's prompt carries none of the parent's session-level learning blocks (frozen memory,
  skills index, ``-s`` preloads), and a subagent never gets a background review;
- a closed session's code kernels end with it (``/new``), host exit closes both kernel pools, and
  only an interactive host runs the idle reaper.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from indus.cli import oneshot
from indus.cli.args import CliState, GlobalArgs, HostOverrides
from indus.cli.runtime import ServiceSpec, make_host
from indus.cli.session_select import LaunchPolicy
from indus.cli.slash.dispatch import dispatch
from indus.cli.slash.port import HostPort
from indus.core.subagents.child import build_child
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.learning import host as learning_host
from indus.learning.host import review_runner
from indus.protocol.methods import SessionCreateParams
from indus.state import registry
from indus.tools.builtin.code import kernel, remote
from indus.tools.builtin.delegate import DelegateTask, DelegationLimits
from tests.fakes.agent import any_window
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.fakes.subagents import RoutedModel
from tests.unit.cli.conftest import fresh_terminal_runtime
from tests.unit.cli.slash._fakes import make_ctx
from tests.unit.learning._helpers_a import with_config

WAIT = 10.0
NOTE = "The user drinks green tea while reviewing code."
SKILL = """---
name: tea-break
description: Plan a short break between long review sessions
license: MIT
---

# Tea break

## When to use
Long review sessions.

## Procedure
Stand up, make tea, come back.
"""
GOAL = "Count the files"
# CLI runs end like a process does; each test gets a fresh terminal runtime (autouse).
__all__ = ["fresh_terminal_runtime"]


@pytest.fixture(autouse=True)
def in_tmp(tmp_path: Path) -> Iterator[None]:
    here = os.getcwd()
    os.chdir(tmp_path)
    yield
    os.chdir(here)


@pytest.fixture(autouse=True)
def empty_pools() -> Iterator[None]:
    yield
    asyncio.run(kernel.POOL.shutdown())
    asyncio.run(remote.POOL.shutdown())


@pytest.fixture
def home(scope: Scope) -> Scope:
    memories = scope.home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text(NOTE + "\n", encoding="utf-8")
    skill = scope.home / "skills" / "leisure" / "tea-break"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL, encoding="utf-8")
    return scope


def overrides(client: Any, route_of: FakeModelServer) -> HostOverrides:
    return HostOverrides(
        route=fake_route(route_of),
        client_factory=lambda route: client.client(),
        list_models=any_window,
    )


def system_prompt(server: FakeModelServer, index: int = 0) -> str:
    messages = server.model_requests[index].body["messages"]
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


# -- learning and subagents -----------------------------------------------------------------------


def test_subagent_prompt_has_no_memory_skills_or_preloads(home: Scope) -> None:
    model = RoutedModel(
        FakeClock(datetime(2026, 10, 19, tzinfo=UTC)),
        parent=[
            tool_batch(("delegate_task", {"goal": GOAL})),
            text_turn("The subagent counted them."),
            text_turn("Counting files"),  # a title, should one be asked for
        ],
        children={GOAL: [text_turn("There are three files.")]},
    )
    state = CliState(scope=home, overrides=overrides(model, model.parent))
    state.globals = GlobalArgs(oneshot="count the files", skills=("tea-break",))
    assert oneshot.run_from_cli(state) == 0
    parent = system_prompt(model.parent)
    assert NOTE in parent and "<skills_index>" in parent and "## Loaded skills" in parent
    child_server = model.child(GOAL)
    assert len(child_server.model_requests) == 1
    child = system_prompt(child_server)
    assert NOTE not in child and "<saved-notes" not in child
    assert "<skills_index>" not in child and "tea-break" not in child
    assert "## Loaded skills" not in child
    child_tools = {t["function"]["name"] for t in child_server.model_requests[0].body["tools"]}
    assert child_tools.isdisjoint({"memory", "session_search", "skill_manage", "delegate_task"})


def test_subagents_never_review_and_keep_out_of_the_learning_hooks(
    home: Scope, tmp_path: Path
) -> None:
    server = FakeModelServer(script_of(text_turn("unused")))
    store = registry.acquire(home)
    spec = ServiceSpec(
        scope=home,
        route=fake_route(server),
        policy=LaunchPolicy(cwd=str(tmp_path)),
        store=store,
        overrides=overrides(server, server),
    )
    try:
        with make_host(spec, call_timeout=WAIT) as host:
            service = host.service
            sid = host.create(SessionCreateParams(cwd=str(tmp_path))).session_id
            parent = host.run(lambda: service.agent(sid))
            assert review_runner(parent) is not None  # the interactive parent reviews

            async def child_of_parent() -> Any:
                limits = DelegationLimits.from_scope(home)
                return await build_child(
                    parent, DelegateTask(GOAL), index=0, depth=1, limits=limits
                )

            child = host.call(child_of_parent())
            agent = child.agent
            try:
                assert agent.options.delegation_depth == 1
                assert agent.options.builder is parent.options.subagent_builder
                assert agent.options.builder is not parent.options.builder
                assert review_runner(agent) is None
                assert agent.runtime.before_turn == [] and agent.runtime.after_turn == []
                assert agent.runtime.note_sources == ()
                assert agent.session_id not in service.attached()  # no service session
                assert agent.runtime.code_kernel_owner == kernel.child_owner(sid, agent.session_id)
                # Even a host that asks gets no review for a child.
                wiring = learning_host.wire_session(home, (), session_id=agent.session_id)
                runner = host.run(lambda: wiring.attach_review(agent))
                assert runner.trigger is not None and runner.trigger.no_review
            finally:
                host.call(agent.close())
    finally:
        registry.release(store)


# -- code kernels and the host lifecycle ----------------------------------------------------------


class StubKernel:
    def __init__(self) -> None:
        self.last_used = 0.0
        self.busy = False
        self.pinned = False
        self.closed = False

    async def alive(self) -> bool:
        return not self.closed

    async def close(self) -> None:
        self.closed = True

    def force_close(self) -> None:
        self.closed = True


def stub_kernel(owner: str, pool: Any = kernel.POOL) -> StubKernel:
    async def take() -> StubKernel:
        key = kernel.KernelKey(owner, "local", "project", "py", "/w", ())

        async def make() -> StubKernel:
            return StubKernel()

        taken = await pool.take(
            key, clock=FakeClock(datetime(2026, 10, 19, tzinfo=UTC)), reset=False,
            idle_s=1800, cap=4, make=make,
        )  # fmt: skip
        taken.kernel.busy = False
        return cast(StubKernel, taken.kernel)

    return asyncio.run(take())


def interactive_spec(scope: Scope, tmp_path: Path, run_context: str = "interactive") -> Any:
    server = FakeModelServer(script_of(text_turn("unused")))
    store = registry.acquire(scope)
    spec = ServiceSpec(
        scope=scope,
        route=fake_route(server),
        policy=LaunchPolicy(cwd=str(tmp_path)),
        store=store,
        run_context=run_context,
        overrides=overrides(server, server),
    )
    return spec, store


def test_a_subagents_kernel_is_its_own_and_ends_with_it(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    configured = with_config(
        scope,
        {
            "approvals": {"mode": "off"},
            "delegation": {"child_toolsets": ["code_execution"]},
        },
    )
    # A real clock: the kernel's reply polling sleeps on the scope's clock.
    configured = replace(
        configured.with_env(configured.env.with_overlay({"TMPDIR": str(staging)})),
        clock=SystemClock(),
    )
    owners: list[str] = []
    take = kernel.POOL.take

    async def recording(key: kernel.KernelKey, **kw: Any) -> Any:
        owners.append(key.owner)
        return await take(key, **kw)

    monkeypatch.setattr(kernel.POOL, "take", recording)
    model = RoutedModel(
        FakeClock(datetime(2026, 10, 19, tzinfo=UTC)),
        parent=[
            tool_batch(("delegate_task", {"goal": GOAL})),
            text_turn("The subagent worked it out."),
            text_turn("Sums"),
        ],
        children={
            GOAL: [
                tool_batch(("execute_code", {"code": "print(6 * 7)"})),
                text_turn("42"),
            ]
        },
    )
    state = CliState(scope=configured, overrides=overrides(model, model.parent))
    state.globals = GlobalArgs(oneshot="work it out", toolsets="code_execution,delegation,file")
    assert oneshot.run_from_cli(state) == 0
    child_rows = model.child(GOAL).model_requests[1].body["messages"]
    tool_rows = [str(m["content"]) for m in child_rows if m["role"] == "tool"]
    assert any("42" in row for row in tool_rows), tool_rows
    (owner,) = owners
    assert kernel.is_child_owner(owner)  # <parent>::child::<child>: pinned, outside the LRU
    assert len(kernel.POOL) == 0  # disposed at the child's teardown
    assert not list(staging.iterdir())


def test_new_closes_the_old_sessions_kernels_and_exit_closes_the_pools(
    scope: Scope, tmp_path: Path
) -> None:
    spec, store = interactive_spec(scope, tmp_path)
    try:
        with make_host(spec, call_timeout=WAIT) as host:
            service = host.service
            sid = host.create(SessionCreateParams(cwd=str(tmp_path))).session_id
            assert service.kernels is not None
            assert host.run(lambda: service.kernels is not None and service.kernels.reaping)
            mine = stub_kernel(sid)
            other = stub_kernel("another-session")
            far = stub_kernel(sid + "-remote", remote.POOL)
            port = HostPort(host, scope)
            ctx = make_ctx(scope, port=port, session_id=sid)
            result = dispatch("/new now", ctx)
            assert result.switch_to and result.switch_to != sid
            assert mine.closed  # /new ended the old session's kernel
            assert not other.closed and not far.closed  # nobody else's
        # host exit: both pools are empty, the reaper is gone
        assert other.closed and far.closed
        assert len(kernel.POOL) == 0 and len(remote.POOL) == 0
        assert service.kernels is not None and not service.kernels.reaping
    finally:
        registry.release(store)


def test_one_shot_host_closes_kernels_but_runs_no_reaper(scope: Scope, tmp_path: Path) -> None:
    spec, store = interactive_spec(scope, tmp_path, run_context="single_query")
    try:
        with make_host(spec, call_timeout=WAIT) as host:
            service = host.service
            host.create(SessionCreateParams(cwd=str(tmp_path)))
            assert service.kernels is not None and not service.kernels.reaping
            left = stub_kernel("left-over")
        assert left.closed and len(kernel.POOL) == 0
    finally:
        registry.release(store)
