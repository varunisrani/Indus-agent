"""P09 wave 2 host wiring through the real CLI path (``cli.runtime.build_service`` →
``agent_factory`` → ``learning.host``): every agent gets its background review, a live turn cancels
it first, ``/refine`` reaches it through ``HostPort``, the interactive service runs the curator's
maintenance timer, and ``skill_manage`` is a chat tool whose writes go through the approval gate.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.cli import oneshot
from indus.cli.args import CliState, GlobalArgs, HostOverrides
from indus.cli.runtime import ServiceSpec, learning_startup, make_host
from indus.cli.session_select import LaunchPolicy
from indus.cli.slash.dispatch import dispatch
from indus.cli.slash.port import HostPort
from indus.foundation.scope import Scope
from indus.learning import pending
from indus.learning.curator.state import CuratorState
from indus.learning.host import review_runner
from indus.learning.skills import manage_tool
from indus.learning.skills.bundled_sync import OPT_OUT_MARKER, manifest_path
from indus.learning.skills.usage import SkillUsage
from indus.protocol.methods import SessionCreateParams
from indus.state import registry
from tests.fakes.agent import any_window
from tests.fakes.model import FakeModelServer, fake_route, script_of, text_turn, tool_batch
from tests.unit.cli.slash._fakes import make_ctx
from tests.unit.learning._helpers_a import with_config

WAIT = 10.0
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


@pytest.fixture(autouse=True)
def in_tmp(tmp_path: Path) -> Iterator[None]:
    here = os.getcwd()
    os.chdir(tmp_path)
    yield
    os.chdir(here)


def overrides(server: FakeModelServer) -> HostOverrides:
    return HostOverrides(
        route=fake_route(server),
        client_factory=lambda route: server.client(),
        list_models=any_window,
    )


def run_z(scope: Scope, server: FakeModelServer, prompt: str = "hi") -> int:
    state = CliState(scope=scope, overrides=overrides(server))
    state.globals = GlobalArgs(oneshot=prompt)
    return oneshot.run_from_cli(state)


def until(check: Any, timeout: float = WAIT) -> None:
    """Poll ``check`` from this (non-loop) thread in short real-time slices."""
    pause = threading.Event()
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        pause.wait(0.01)


def test_interactive_session_gets_review_refine_and_maintenance(
    scope: Scope, tmp_path: Path
) -> None:
    # the turn, the session title (auxiliary client) and the review, in whatever order the last
    # two arrive: every reply is plain text
    server = FakeModelServer(
        script_of(text_turn("hello there"), *[text_turn("Nothing worth saving.")] * 2)
    )
    listeners = len(manage_tool.SUCCESS_LISTENERS)
    store = registry.acquire(scope)
    spec = ServiceSpec(
        scope=scope,
        route=fake_route(server),
        policy=LaunchPolicy(cwd=str(tmp_path)),
        store=store,
        overrides=overrides(server),
    )
    host = make_host(spec, call_timeout=WAIT)
    try:
        with host:
            service = host.service
            sid = host.create(SessionCreateParams(cwd=str(tmp_path))).session_id
            agent = host.run(lambda: service.agent(sid))
            runner = review_runner(agent)
            assert runner is not None and runner.trigger is not None
            assert runner.cancel_for_live_turn in agent.runtime.before_turn
            assert "skill_manage" in agent.tool_names
            assert len(manage_tool.SUCCESS_LISTENERS) == listeners + 1
            assert service.maintenance is not None and service.maintenance.names == ["curator"]

            port = HostPort(host, scope)
            assert port.refine(sid, None).startswith("Nothing to review yet")
            host.submit(sid, "hi")
            host.wait(sid, timeout=WAIT)
            ctx = make_ctx(scope, port=port, session_id=sid)
            dispatch("/refine the tea habit", ctx)
            assert ctx.lines[0].startswith("Reviewing this conversation in the background")
            until(lambda: not host.run(lambda: runner.busy))
            outcome = runner.last
            assert outcome is not None and outcome.status == "done"
            assert outcome.requests == 1
        # closing the agent removes its counter listener
        assert len(manage_tool.SUCCESS_LISTENERS) == listeners
    finally:
        registry.release(store)


def test_one_shot_sessions_never_review(scope: Scope, tmp_path: Path) -> None:
    server = FakeModelServer(script_of(text_turn("ok")))
    store = registry.acquire(scope)
    spec = ServiceSpec(
        scope=scope,
        route=fake_route(server),
        policy=LaunchPolicy(cwd=str(tmp_path)),
        store=store,
        run_context="single_query",
        overrides=overrides(server),
    )
    try:
        with make_host(spec, call_timeout=WAIT) as host:
            sid = host.create(SessionCreateParams(cwd=str(tmp_path))).session_id
            agent = host.run(lambda: host.service.agent(sid))
            runner = review_runner(agent)
            assert runner is not None and runner.trigger is not None
            assert runner.trigger.no_review
            assert host.service.maintenance is None
    finally:
        registry.release(store)


def test_chat_skill_manage_creates_a_skill(scope: Scope) -> None:
    server = FakeModelServer(
        script_of(
            tool_batch(
                ("skill_manage", {"action": "create", "name": "tea-break", "content": SKILL})
            ),
            text_turn("saved"),
        )
    )
    assert run_z(scope, server) == 0
    tools = {t["function"]["name"] for t in server.model_requests[0].body["tools"]}
    assert "skill_manage" in tools
    assert (scope.home / "skills" / "tea-break" / "SKILL.md").read_text(encoding="utf-8") == SKILL
    usage = SkillUsage(scope.home / "skills", clock=scope.clock)
    assert usage.get("tea-break")["origin"] == "learn"  # a foreground create: not the curator's


def test_chat_skill_write_is_held_when_approval_is_on(scope: Scope) -> None:
    gated = with_config(scope, {"skills": {"write_approval": True}})
    server = FakeModelServer(
        script_of(
            tool_batch(
                ("skill_manage", {"action": "create", "name": "tea-break", "content": SKILL})
            ),
            text_turn("held"),
        )
    )
    assert run_z(gated, server) == 0
    assert not (scope.home / "skills" / "tea-break").exists()
    [held] = pending.PendingStore(scope.home, scope.clock).list("skills")
    assert held.summary == "create tea-break"


# -- first run ------------------------------------------------------------------------------------


def test_first_chat_seeds_the_starter_skills_once(scope: Scope) -> None:
    server = FakeModelServer(script_of(text_turn("ok"), text_turn("again")))
    assert not manifest_path(scope.home).exists()
    assert run_z(scope, server) == 0
    seeded = sorted(p.parent.name for p in (scope.home / "skills").rglob("SKILL.md"))
    assert len(seeded) == 15 and "indus-guide" in seeded
    assert manifest_path(scope.home).is_file()
    prompt = server.model_requests[0].body["messages"][0]["content"]
    assert "- indus-guide: " in prompt  # the first session's index already lists them
    # a deleted starter skill stays deleted on the next start (the manifest says it was seeded)
    guide = next((scope.home / "skills").rglob("indus-guide"))
    for path in sorted(guide.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    guide.rmdir()
    assert run_z(scope, server) == 0
    assert not any((scope.home / "skills").rglob("indus-guide"))


def test_opt_out_marker_stops_first_run_seeding(scope: Scope) -> None:
    scope.home.mkdir(parents=True, exist_ok=True)
    (scope.home / OPT_OUT_MARKER).write_text("", encoding="utf-8")
    assert run_z(scope, FakeModelServer(script_of(text_turn("ok")))) == 0
    assert not manifest_path(scope.home).exists()
    assert not list((scope.home / "skills").rglob("SKILL.md"))


def test_interactive_start_runs_the_curator_check_off_thread(scope: Scope) -> None:
    store = registry.acquire(scope)
    try:
        learning_startup(scope, store, interactive=True)
        # first look on a fresh profile: the curator only records when it started counting
        until(lambda: CuratorState(scope).load().get("last_run_at") is not None)
    finally:
        registry.release(store)
