"""Contract cases for the cloud sandboxes (P21-16): modal, daytona, vercel_sandbox.

The shared cases (output, marker, cwd, odd output, deadline 124, interrupt 130, stdin, bounded vs
unbounded capture) run for these rows in ``test_execution_contract.py``, and the snapshot rule
(R02.1) in ``test_docker_ssh_contract.py``. These add what a sandbox in someone's cloud must also
keep, each over the row's fake SDK: forwarded values only in the SDK's env parameter (R02.7), a
sandbox replaced at open flagged once (R02.19; Daytona restarts a stopped sandbox with its files
instead), background jobs started, polled and killed inside the sandbox, file write/read/patch
through the backend, and a lost vendor link answered as ``degraded``.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from indus.backends.contract import ExecResult
from indus.backends.processes import ProcessRecord, ProcessRegistry, SpawnRequest
from indus.backends.registry import BackendStatus
from indus.backends.remote_processes import RemoteJob
from indus.backends.sandbox_records import SandboxRecords
from indus.foundation.clock import SystemClock
from indus.tools.builtin.patch import patch
from indus.tools.builtin.read_file import read_file
from indus.tools.builtin.write_file import write_file
from tests.contract.backends.conftest import RECORD_KINDS, Backend
from tests.fakes.terminal import forge_token

NAME = "CONTRACT_TOKEN"
REAL = SystemClock()
TASK = "contract"  # the task id ``Backend.open`` acquires with
EXEC_METHOD = {
    "modal": "Sandbox.exec",
    "daytona": "Process.exec",
    "vercel_sandbox": "Sandbox.create_process",
}


def test_values_ride_the_sdk_env_parameter_only(sdk_backend: Backend) -> None:
    value = forge_token()
    sdk_backend.configure(env_passthrough=[NAME], secrets={NAME: value})

    async def go() -> ExecResult:
        async with sdk_backend.open() as env:
            return await env.execute(f'printf %s "${NAME}"')

    result = asyncio.run(go())
    assert result.output == value
    cloud = sdk_backend.rig.cloud
    assert cloud is not None
    calls = cloud.calls_of(EXEC_METHOD[sdk_backend.name])
    assert calls, "the fake SDK recorded no exec"
    assert not any(value in repr(call.args) for call in calls)  # never in the command (R02.7)
    assert any(value in repr(call.kwargs.get("env")) for call in calls)  # only in env=
    assert not any(value in " ".join(argv) for argv in cloud.ran)


def test_a_replaced_sandbox_says_so_once(sdk_backend: Backend) -> None:
    sdk_backend.configure(container_persistent=True)
    kind = RECORD_KINDS.get(sdk_backend.name)
    if kind is not None:  # a stored snapshot that no longer exists: started from the image
        SandboxRecords.for_scope(sdk_backend.scope, kind).put(TASK, snapshot_id="im-gone")

    async def go() -> tuple[ExecResult, ExecResult, ExecResult]:
        async with sdk_backend.open() as env:
            first = await env.execute("printf %s one > kept.txt; printf %s ok")
            if kind is None:  # Daytona: stopped behind our back, started again with its files
                sandbox: Any = getattr(env, "sandbox")  # noqa: B009 - the Daytona handle
                sandbox.stop()
            second = await env.execute("cat kept.txt")
            third = await env.execute("printf %s ok")
            return first, second, third

    first, second, third = asyncio.run(go())
    assert first.output == "ok" and second.output == "one" and third.output == "ok"
    if kind is not None:
        assert first.environment_recreated and not second.environment_recreated
        assert SandboxRecords.for_scope(sdk_backend.scope, kind).get(TASK) is not None  # saved
    else:
        assert not first.environment_recreated and not second.environment_recreated
        cloud = sdk_backend.rig.cloud
        assert cloud is not None and "Sandbox.start" in cloud.methods()
    assert not third.environment_recreated


async def _until(record: ProcessRecord, done: object, seconds: float = 10.0) -> None:
    handle = record.handle
    assert isinstance(handle, RemoteJob) and handle.poller is not None
    deadline = REAL.monotonic() + seconds
    while REAL.monotonic() < deadline:
        await handle.poller.round()
        if callable(done) and done():
            return
        await REAL.sleep(0.1)  # the fake sandboxes run real processes: real time


def test_background_job_starts_polls_and_is_killed_inside(sdk_backend: Backend) -> None:
    async def go() -> tuple[ProcessRecord, ProcessRecord]:
        processes = ProcessRegistry()
        async with sdk_backend.open() as env:

            def request(command: str) -> SpawnRequest:
                return SpawnRequest(
                    command,
                    sdk_backend.scope,
                    "s1",
                    f"contract@{sdk_backend.name}",
                    sdk_backend.workdir,
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


def test_file_write_read_and_patch_through_the_backend(sdk_backend: Backend) -> None:
    target = os.path.join(sdk_backend.workdir, "notes", "plan.txt")

    async def go() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
        registry = sdk_backend.registry()
        common: dict[str, Any] = {
            "scope": sdk_backend.scope,
            "task_id": "files",
            "session_id": "files",
            "environments": registry,
        }
        try:
            first = {"path": target, "content": "step one\n"}
            wrote = json.loads(await write_file(first, **common))
            read = json.loads(await read_file({"path": target}, **common))
            edit = {"path": target, "old_string": "step one", "new_string": "step two"}
            patched = json.loads(await patch(edit, **common))
            _key, env = await registry.acquire(sdk_backend.scope, session_id="files")
            seen = await env.execute(f"cat {target}")
            return wrote, read, patched, seen.output
        finally:
            await registry.shutdown(fence=False)

    wrote, read, patched, seen = asyncio.run(go())
    assert wrote.get("verified") is True and wrote.get("dirs_created") is True, wrote
    assert "step one" in read.get("content", ""), read
    assert patched.get("success") is True, patched
    assert seen == "step two\n"  # the sandbox itself holds the patched file


def test_lost_vendor_link_answers_degraded(sdk_backend: Backend) -> None:
    cloud = sdk_backend.rig.cloud
    assert cloud is not None

    async def go() -> tuple[ExecResult | BackendStatus, ExecResult | BackendStatus]:
        registry = sdk_backend.registry()
        options: dict[str, Any] = {
            "scope": sdk_backend.scope,
            "task_id": TASK,
            "overrides": sdk_backend.overrides(),
        }
        try:
            first = await registry.run("echo up", **options)
            cloud.fail_with(EXEC_METHOD[sdk_backend.name], ConnectionResetError("link down"), 5)
            second = await registry.run("echo again", **options)
            return first, second
        finally:
            await registry.shutdown(fence=False)

    first, second = asyncio.run(go())
    assert isinstance(first, ExecResult) and first.output == "up\n"
    assert isinstance(second, BackendStatus) and second.status == "degraded", second
    assert second.reason and second.retry_hint
