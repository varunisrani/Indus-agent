"""The Docker environment's lifecycle over the fake client (P11-05).

``exec`` runs real bash on this machine through the fake client, so the shared shell flow
(snapshot, wrapper, cwd marker, kill) is exercised for real; every wait ends with a process exit
or a kill on a fake clock.
"""

from __future__ import annotations

import asyncio
import shutil
from functools import partial
from pathlib import Path

import pytest

from indus.backends.contract import BackendError, Environment
from indus.backends.docker import DOCKER_FLAGS, persistent_per_profile
from indus.backends.docker.environment import (
    LABEL_AGENT,
    LABEL_ENVIRONMENT,
    LABEL_PROFILE,
    LABEL_TASK,
    DockerEnvironment,
    open_docker,
)
from indus.backends.registry import (
    BackendEntry,
    BackendStatus,
    CreateRequest,
    EnvironmentRegistry,
)
from indus.backends.wait import INTERRUPTED_RC, TIMED_OUT_RC
from indus.foundation.scope import Scope
from tests.fakes.backend.docker_cli import FakeDocker
from tests.fakes.clock import FakeClock
from tests.unit.backends.conftest import drive
from tests.unit.backends.docker.conftest import IMAGE, docker_scope, open_env, track

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    (path / "sub").mkdir(parents=True)
    return path


def _runs(fake: FakeDocker) -> list[list[str]]:
    return [c for c in fake.commands() if c[:2] == ["run", "-d"]]


def test_runs_commands_in_a_labelled_container(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> tuple[DockerEnvironment, list[str]]:
        env = await open_env(scope, fake_docker, work)
        outputs: list[str] = [env.container_id or ""]
        first = await env.execute("printf hello")
        outputs.append(f"{first.returncode}:{first.output}")
        moved = await env.execute("cd sub && pwd")
        outputs.append(moved.output.strip())
        await env.cleanup()
        return env, outputs

    env, (cid, *outputs) = asyncio.run(go())
    assert isinstance(env, Environment)
    assert outputs == ["0:hello", str(work / "sub")]
    assert env.cwd == str(work / "sub")
    (run,) = _runs(fake_docker)
    assert run[run.index("--name") + 1].startswith("indus-")
    assert run[-3:] == [IMAGE, "sleep", "infinity"]
    assert "--init" in run and "--cap-drop" in run and "no-new-privileges" in run
    (box,) = fake_docker.containers().values()
    assert box["labels"][LABEL_AGENT] == "1"
    assert box["labels"][LABEL_TASK] == "shared"
    assert box["labels"][LABEL_PROFILE] == "work"
    assert len(box["labels"][LABEL_ENVIRONMENT]) == 24
    execs = [c for c in fake_docker.commands() if c[0] == "exec" and "bash" in c]
    assert execs and all(c[c.index("bash") - 1] == cid for c in execs)
    assert all("-w" not in c[: c.index("bash")] for c in execs)


def test_second_process_attaches(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> tuple[str | None, str | None]:
        first = await open_env(scope, fake_docker, work)
        one = first.container_id
        await first.cleanup()
        second = await open_env(scope, fake_docker, work)
        two = second.container_id
        assert (await second.execute("printf again")).output == "again"
        await second.cleanup()
        return one, two

    one, two = asyncio.run(go())
    assert one is not None and two is not None and one.startswith(two)
    assert len(_runs(fake_docker)) == 1


def test_stopped_container_is_started(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> None:
        first = await open_env(scope, fake_docker, work)
        await first.cleanup()
        with fake_docker.edit() as state:
            for box in state["containers"].values():
                box["state"] = "exited"
        second = await open_env(scope, fake_docker, work)
        await second.cleanup()

    asyncio.run(go())
    assert "start" in fake_docker.subcommands()
    assert len(_runs(fake_docker)) == 1
    (box,) = fake_docker.containers().values()
    assert box["state"] == "running"


def test_different_image_makes_a_new_container(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    fake_docker.add_image("other:2")
    first_scope = docker_scope(tmp_path, fake_docker, fake_clock)
    second_scope = docker_scope(tmp_path, fake_docker, fake_clock, terminal__docker_image="other:2")

    async def go() -> None:
        for scope in (first_scope, second_scope):
            env = await open_env(scope, fake_docker, work)
            await env.cleanup()

    asyncio.run(go())
    assert len(_runs(fake_docker)) == 2
    prints = {box["labels"][LABEL_ENVIRONMENT] for box in fake_docker.containers().values()}
    assert len(prints) == 2


def test_shared_key_has_no_fingerprint(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock, terminal__docker_shared_key="team a")

    async def go() -> None:
        env = await open_env(scope, fake_docker, work)
        await env.cleanup()

    asyncio.run(go())
    (box,) = fake_docker.containers().values()
    assert LABEL_ENVIRONMENT not in box["labels"]
    assert box["labels"][LABEL_PROFILE].startswith("team_a-")


def test_persistent_cleanup_leaves_it_running(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> None:
        env = await open_env(scope, fake_docker, work)
        await env.cleanup()
        await env.cleanup()  # safe twice

    asyncio.run(go())
    (box,) = fake_docker.containers().values()
    assert box["state"] == "running"
    assert "stop" not in fake_docker.subcommands() and "rm" not in fake_docker.subcommands()


def test_non_persistent_cleanup_stops_then_removes(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock, terminal__container_persistent=False)
    assert not persistent_per_profile(scope)

    async def go() -> None:
        env = await open_env(scope, fake_docker, work, kind="session", ident="s1")
        await env.cleanup()

    asyncio.run(go())
    ends = [c for c in fake_docker.commands() if c[0] in ("stop", "rm")]
    assert [c[:3] for c in ends] == [["stop", "-t", "10"], ["rm", "-f", ends[1][2]]]
    assert fake_docker.containers() == {}
    (run,) = _runs(fake_docker)
    assert "/workspace:rw,exec,size=10g" in run  # tmpfs, nothing bound from the host


def test_networked_container_replaced_under_lockdown(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    open_scope = docker_scope(tmp_path, fake_docker, fake_clock)
    locked = docker_scope(tmp_path, fake_docker, fake_clock, terminal__docker_network=False)

    async def go() -> None:
        for scope in (open_scope, locked):
            env = await open_env(scope, fake_docker, work)
            await env.cleanup()

    asyncio.run(go())
    (box,) = fake_docker.containers().values()
    assert box["network"] == "none"
    assert len(_runs(fake_docker)) == 2


def test_failed_run_removes_the_container_and_hides_values(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    value = "tok-" + "9" * 12
    scope = docker_scope(
        tmp_path,
        fake_docker,
        fake_clock,
        env={"MY_TOKEN": value},
        terminal__docker_forward_env=["MY_TOKEN"],
    )
    with fake_docker.edit() as state:
        state["fail"]["run"] = f"invalid env value {value}"

    with pytest.raises(BackendError) as caught:
        asyncio.run(open_env(scope, fake_docker, work))
    assert value not in str(caught.value) and "[redacted]" in str(caught.value)
    name = _runs(fake_docker)[0][_runs(fake_docker)[0].index("--name") + 1]
    assert ["rm", "-f", name] in fake_docker.commands()


def test_deadline_kills_inside_the_container_too(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> tuple[int, bool]:
        env = await drive(fake_clock, open_env(scope, fake_docker, work, clock=fake_clock))
        result = await drive(fake_clock, env.execute("sleep 60", timeout=3))
        await drive(fake_clock, env.cleanup())
        return result.returncode, result.timed_out

    code, timed_out = asyncio.run(go())
    assert (code, timed_out) == (TIMED_OUT_RC, True)
    inside = [c for c in fake_docker.commands() if c[0] == "exec" and "sh" in c]
    assert any("kill -TERM" in c[-1] for c in inside)


def test_interrupt_gives_130(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> int:
        env = await drive(fake_clock, open_env(scope, fake_docker, work, clock=fake_clock))
        stop = asyncio.Event()

        async def later() -> None:
            await fake_clock.sleep(1)
            stop.set()

        task = asyncio.ensure_future(later())
        result = await drive(fake_clock, env.execute("sleep 60", cancel=stop))
        await task
        await drive(fake_clock, env.cleanup())
        return result.returncode

    assert asyncio.run(go()) == INTERRUPTED_RC


def test_stdin_reaches_the_command(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> str:
        env = await open_env(scope, fake_docker, work)
        result = await env.execute("cat", stdin="from stdin\n")
        await env.cleanup()
        return result.output

    assert asyncio.run(go()) == "from stdin\n"
    assert any(c[:2] == ["exec", "-i"] for c in fake_docker.commands())


def _registry(fake: FakeDocker) -> EnvironmentRegistry:
    async def factory(request: CreateRequest) -> Environment:
        return track(
            await open_docker(request, runner=fake.runner, os_name="linux", reaper=lambda *_: None)
        )

    entry = BackendEntry(
        "docker", factory, DOCKER_FLAGS, profile_scoped=partial(persistent_per_profile)
    )
    return EnvironmentRegistry([entry])


def test_daemon_gone_mid_call_degrades_and_next_call_recreates(
    tmp_path: Path, fake_clock: FakeClock, fake_docker: FakeDocker, work: Path
) -> None:
    scope: Scope = docker_scope(tmp_path, fake_docker, fake_clock)

    async def go() -> list[object]:
        registry = _registry(fake_docker)
        seen: list[object] = []
        seen.append(await registry.run("printf one", scope=scope, session_id="s", cwd=str(work)))
        key = registry.key_for(scope, session_id="s")
        fake_docker.set_daemon(False)
        seen.append(await registry.run("printf two", scope=scope, session_id="s", cwd=str(work)))
        seen.append(key in registry.live())
        fake_docker.set_daemon(True)
        seen.append(await registry.run("printf three", scope=scope, session_id="s", cwd=str(work)))
        seen.append(await registry.release(scope, session_id="s"))  # profile key: kept
        await registry.shutdown(fence=False)
        return seen

    one, two, still_live, three, released = asyncio.run(go())
    assert getattr(one, "output", None) == "one"
    assert isinstance(two, BackendStatus) and two.status == "degraded"
    assert "daemon" in two.reason
    assert still_live is False
    assert getattr(three, "output", None) == "three"
    assert released is False
    assert len(_runs(fake_docker)) == 1  # the new environment attached to the same container
