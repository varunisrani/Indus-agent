"""Contract cases for backends that are not this machine (P11-21): ACC-D3-1, ACC-D13-1/2.

The shared cases (output, marker, cwd, deadline, interrupt, stdin, capture) run for every row in
``test_execution_contract.py``. These add what Docker and SSH must also keep: per-session values
never frozen into the snapshot (R02.1, ``local`` too), values never on a client command line
(R02.7, checked on the fake clients' recorded argv; the ``/proc/<pid>/cmdline`` check is the live
lane's), the ``environment_recreated`` warning, background jobs started, polled and killed inside
the backend, and a degraded result when the backend cannot be reached.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from indus.backends.contract import ExecResult
from indus.backends.docker.environment import DockerEnvironment
from indus.backends.processes import ProcessRecord, ProcessRegistry, SpawnRequest
from indus.backends.registry import BackendStatus
from indus.backends.remote_processes import RemoteJob
from indus.backends.spawned import SpawnedEnvironment
from indus.foundation.clock import SystemClock
from tests.contract.backends.conftest import Backend
from tests.fakes.terminal import forge_token

NAME = "CONTRACT_TOKEN"
REAL = SystemClock()


def _calls_argv(backend: Backend) -> list[list[str]]:
    if backend.rig.docker is not None:
        return [call["argv"] for call in backend.rig.docker.calls()]
    if backend.rig.ssh is not None:
        return [call["argv"] for call in backend.rig.ssh.calls()]
    return []


def test_session_values_are_never_frozen_into_the_snapshot(shell_backend: Backend) -> None:
    value = forge_token()
    shell_backend.configure(env_passthrough=[NAME], secrets={NAME: value})

    async def go() -> tuple[ExecResult, ExecResult, str]:
        async with shell_backend.open() as env:
            await env.execute("export KEPT=yes")
            seen = await env.execute(f'printf %s "${NAME}"')
            kept = await env.execute('printf %s "$KEPT"')
            assert isinstance(env, SpawnedEnvironment)
            snapshot = Path(env.snapshot_path).read_text(encoding="utf-8")
            return seen, kept, snapshot

    seen, kept, snapshot = asyncio.run(go())
    assert seen.output == value  # the command sees the value...
    assert kept.output == "yes" and "KEPT" in snapshot  # ...the snapshot carries exports...
    assert value not in snapshot and NAME not in snapshot  # ...but never the session value


def test_values_never_ride_a_client_command_line(remote_backend: Backend) -> None:
    value = forge_token()
    remote_backend.configure(env_passthrough=[NAME], secrets={NAME: value})

    async def go() -> ExecResult:
        async with remote_backend.open() as env:
            return await env.execute(f'printf %s "${NAME}"')

    result = asyncio.run(go())
    assert result.output == value
    argvs = _calls_argv(remote_backend)
    assert argvs, "the fake client recorded no call"
    assert not any(value in word for argv in argvs for word in argv)  # R02.7
    assert any(NAME in " ".join(argv) for argv in argvs)  # the name travels, the value does not


def test_replaced_container_carries_the_warning_once(remote_backend: Backend) -> None:
    async def go() -> tuple[ExecResult, ExecResult]:
        async with remote_backend.open() as env:
            await env.execute("true")
            if remote_backend.rig.docker is not None:
                assert isinstance(env, DockerEnvironment) and env.container_id is not None
                remote_backend.rig.docker.remove_container(env.container_id)
            first = await env.execute("printf %s ok")
            second = await env.execute("printf %s ok")
            return first, second

    first, second = asyncio.run(go())
    assert first.output == "ok" and second.output == "ok"
    if remote_backend.name == "docker":  # R02.19: replaced once, said once
        assert first.environment_recreated and not second.environment_recreated
    else:  # a remote host is never silently replaced
        assert not first.environment_recreated and not second.environment_recreated


async def _until(record: ProcessRecord, done: object, seconds: float = 10.0) -> None:
    handle = record.handle
    assert isinstance(handle, RemoteJob) and handle.poller is not None
    deadline = REAL.monotonic() + seconds
    while REAL.monotonic() < deadline:
        await handle.poller.round()
        if callable(done) and done():
            return
        await REAL.sleep(0.1)  # the fake clients are real processes: real time


def test_background_job_starts_polls_and_is_killed_inside(remote_backend: Backend) -> None:
    async def go() -> tuple[ProcessRecord, ProcessRecord]:
        processes = ProcessRegistry()
        async with remote_backend.open() as env:

            def request(command: str) -> SpawnRequest:
                return SpawnRequest(
                    command,
                    remote_backend.scope,
                    "s1",
                    f"contract@{remote_backend.name}",
                    remote_backend.workdir,
                    {},
                    environment=env,
                )

            short = await processes.spawn(request("printf 'from inside\\n'; exit 3"))
            long = await processes.spawn(request("printf 'ready\\n'; sleep 30"))
            assert isinstance(short.handle, RemoteJob) and isinstance(long.handle, RemoteJob)
            await _until(short, lambda: short.exited)
            await _until(long, lambda: "ready" in long.buffer)
            await processes.kill(long)
            await processes.shutdown()
            return short, long

    short, long = asyncio.run(go())
    assert short.exited and short.exit_code == 3 and short.buffer == "from inside\n"
    assert long.exited and long.reason == "killed" and "ready" in long.buffer


def test_lost_connection_answers_degraded(remote_backend: Backend) -> None:
    rig = remote_backend.rig

    def cut() -> None:
        if rig.docker is not None:
            rig.docker.set_daemon(False)
        if rig.ssh is not None:
            rig.ssh.set_mode("refused", home=str(rig.ssh.remote_home))

    async def go() -> tuple[ExecResult | BackendStatus, ExecResult | BackendStatus]:
        registry = remote_backend.registry()
        options = {"scope": remote_backend.scope, "overrides": remote_backend.overrides()}
        try:
            first = await registry.run("echo up", **options)  # type: ignore[arg-type]
            cut()
            second = await registry.run("echo again", **options)  # type: ignore[arg-type]
            return first, second
        finally:
            await registry.shutdown(fence=False)

    first, second = asyncio.run(go())
    assert isinstance(first, ExecResult) and first.output == "up\n"
    assert isinstance(second, BackendStatus) and second.status == "degraded"
    assert second.reason and second.retry_hint
