"""A vanished container, and isolation decided by mounts (P11-06, R02.19, R02.6, R02.4)."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import EnvironmentConnectionError
from indus.backends.docker import recovery
from indus.backends.docker.recovery import (
    container_gone,
    daemon_gone,
    mark_lost,
    register_lost_handler,
    unregister_lost_handler,
)
from indus.backends.processes import ProcessRegistry, SpawnRequest
from indus.safety.approvals import ApprovalContext, ApprovalState, decide
from indus.safety.approvals.decision import ApprovalDecision
from tests.fakes.backend.docker_cli import FakeDocker
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import FakeJob, Inbox
from tests.unit.backends.docker.conftest import Secrets, docker_scope, open_env

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def lost() -> Iterator[list[str]]:
    seen: list[str] = []
    register_lost_handler(seen.append)
    yield seen
    unregister_lost_handler(seen.append)


def _runs(fake: FakeDocker) -> int:
    return sum(1 for c in fake.commands() if c[:2] == ["run", "-d"])


def test_patterns_open_the_output() -> None:
    assert container_gone("Error response from daemon: No such container: abc\n", 1)
    assert container_gone("Error response from daemon: container abc is not running", 1)
    assert container_gone('Error: no container with name or ID "abc" found', 125)
    assert not container_gone("No such container\n", 0)
    assert not container_gone("build log\nError response from daemon: No such container", 1)
    assert not container_gone("grep: is not running: No such file", 2)
    assert daemon_gone("Cannot connect to the Docker daemon at unix:///x. Is it running?", 1)
    assert not daemon_gone("all good", 0)


def test_removed_container_is_recreated_once(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path, lost: list[str]
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> tuple[Any, Any, str | None, str | None, str]:
        env = await open_env(scope, fake_docker, work)
        before = env.container_id
        assert before is not None
        await env.execute("export KEPT=1")
        fake_docker.remove_container(before)
        result = await env.execute("printf ok")
        after = env.container_id
        later = await env.execute("printf again")
        assert env.recreations == 1
        assert env.sudo is None  # no host password is ever fed into a container
        key = env.env_key
        await env.cleanup()
        return result, later, before, after, key

    result, later, before, after, key = asyncio.run(go())
    assert (result.output, result.returncode, result.environment_recreated) == ("ok", 0, True)
    assert not later.environment_recreated  # said once
    assert after is not None and before is not None and not before.startswith(after)
    assert _runs(fake_docker) == 2
    assert lost == [key]
    assert len([c for c in fake_docker.commands() if c[0] == "exec" and "-l" in c]) >= 2


def test_recovery_prefers_a_labelled_container(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path, lost: list[str]
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> tuple[str, str | None]:
        env = await open_env(scope, fake_docker, work)
        old = env.container_id
        assert old is not None
        other = fake_docker.add_container(labels=env.lifecycle.labels)
        fake_docker.remove_container(old)
        result = await env.execute("printf ok")
        assert result.environment_recreated
        now = env.container_id
        await env.cleanup()
        return other, now

    other, now = asyncio.run(go())
    assert now is not None and other.startswith(now)
    assert _runs(fake_docker) == 1  # nothing new was created


def test_second_failure_surfaces_the_error(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path, lost: list[str]
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> Any:
        env = await open_env(scope, fake_docker, work)
        old = env.container_id
        assert old is not None
        fake_docker.remove_container(old)
        with fake_docker.edit() as state:
            state["fail"]["run"] = "no space left on device"  # recovery cannot create one
        result = await env.execute("printf ok")
        await env.cleanup()
        return result

    result = asyncio.run(go())
    assert result.returncode != 0 and "No such container" in result.output
    assert not result.environment_recreated
    assert lost == []


def test_still_gone_after_recovery_is_returned_as_is(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path, lost: list[str]
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> Any:
        env = await open_env(scope, fake_docker, work)
        old = env.container_id
        assert old is not None
        fake_docker.remove_container(old)
        with fake_docker.edit() as state:
            state["run_state"] = "exited"  # the replacement dies at once
        result = await env.execute("printf ok")
        await env.cleanup()
        return result

    result = asyncio.run(go())
    assert result.returncode != 0 and "is not running" in result.output
    assert _runs(fake_docker) == 2  # one recovery, no second one


def test_without_persist_it_degrades(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(
        tmp_path, fake_docker, fake_clock, terminal__docker_persist_across_processes=False
    )

    async def go() -> None:
        env = await open_env(scope, fake_docker, work)
        old = env.container_id
        assert old is not None
        fake_docker.remove_container(old)
        try:
            with pytest.raises(EnvironmentConnectionError, match="container is gone"):
                await env.execute("printf ok")
        finally:
            await env.cleanup()

    asyncio.run(go())
    assert _runs(fake_docker) == 1


def test_background_job_marked_lost(tmp_path: Path, fake_clock: FakeClock) -> None:
    scope = docker_scope(tmp_path, None, fake_clock)
    jobs: list[FakeJob] = []

    def launch(spec: object) -> FakeJob:
        jobs.append(FakeJob())
        return jobs[-1]

    async def go() -> tuple[list[str], Any, Any, Inbox]:
        registry = ProcessRegistry(clock=fake_clock, launcher=launch, watch_exits=False)
        inbox = Inbox()
        registry.deliveries.register(str(scope.home), "s1", inbox)

        def request(key: str) -> SpawnRequest:
            return SpawnRequest(
                "make", scope, "s1", key, "/", {"PATH": "/usr/bin"}, notify_on_exit=True
            )

        mine = await registry.spawn(request("docker-key"))
        other = await registry.spawn(request("local-key"))
        ended = mark_lost(registry, "docker-key")
        return ended, mine, other, inbox

    ended, mine, other, inbox = asyncio.run(go())
    assert ended == [mine.id]
    assert mine.exited and mine.reason == "lost" and mine.source == recovery.LOST_SOURCE
    assert jobs[0].forced == 1
    assert not other.exited
    assert inbox.kinds() == ["completion"]


# -- isolation ------------------------------------------------------------------------------------


def _decide(scope: Any, isolated: bool) -> ApprovalDecision:
    context = ApprovalContext(
        scope=scope, session_id="s1", isolated=isolated, state=ApprovalState()
    )
    return asyncio.run(decide("rm -rf /workspace/*", context))


def test_mounted_cwd_goes_through_approval(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(
        tmp_path, fake_docker, fake_clock, terminal__docker_mount_cwd_to_workspace=True
    )

    async def go() -> tuple[bool, str]:
        env = await open_env(scope, fake_docker, work)
        flags, target = env.flags, env.mounts.cwd_target
        await env.cleanup()
        return flags.isolated, target or ""

    isolated, target = asyncio.run(go())
    assert not isolated and target == "/workspace"
    decision = _decide(scope, isolated)
    assert decision.step != "isolated"
    assert not decision.approved  # nobody could be asked here, so the flow refused it


def test_unmounted_skips_prompt_but_deny_glob_blocks(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> bool:
        env = await open_env(scope, fake_docker, work)
        isolated = env.flags.isolated
        await env.cleanup()
        return isolated

    isolated = asyncio.run(go())
    assert isolated
    allowed = _decide(scope, isolated)
    assert allowed.approved and allowed.step == "isolated"
    denying = docker_scope(tmp_path, fake_docker, fake_clock, approvals__deny=["rm -rf *"])
    blocked = _decide(denying, isolated)
    assert not blocked.approved and blocked.outcome == "user_deny_rule"


def test_host_sudo_password_never_enters_the_container(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    local_pw = "test-local-" + "pw44"
    only_local = docker_scope(
        tmp_path, fake_docker, fake_clock, secrets=Secrets({"sudo_password": local_pw})
    )
    both = docker_scope(
        tmp_path,
        fake_docker,
        fake_clock,
        profile="other",
        secrets=Secrets({"sudo_password": local_pw, "docker_sudo_password": "x-" + "pw55"}),
    )

    async def go() -> tuple[Any, Any]:
        plain = await open_env(only_local, fake_docker, work)
        await plain.execute("printf ok")
        first = plain.sudo
        await plain.cleanup()
        own = await open_env(both, fake_docker, work)
        second = own.sudo
        await own.cleanup()
        return first, second

    first, second = asyncio.run(go())
    assert first is None
    assert second is not None and second.target.startswith("docker:") and not second.can_prompt
    assert second.secret_names[0] == "docker_sudo_password"
    for call in fake_docker.calls():
        assert local_pw not in " ".join(call["argv"])
