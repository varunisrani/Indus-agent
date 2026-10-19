"""execute_code on backends that are not this machine (P10-11).

Two stand-ins for a remote backend: the fake ``docker`` client of ``tests/fakes/backend`` (its
"container" commands really run on this machine with the container's variables) and
``RemoteShell``, a bash-backed environment that records every call. Both keep their temp dir
inside the test's ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import os
import shutil
import signal
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import ActivityCallback, BackendFlags, CancelSignal, ExecResult
from indus.backends.registry import (
    CreateRequest,
    EnvironmentRegistry,
    default_entries,
)
from indus.tools.builtin.code import ExecuteCodeTool, remote
from indus.tools.builtin.code.settings import REMOTE_POLL_S
from tests.fakes.backend.docker_cli import FakeDocker
from tests.unit.tools.builtin._file_env import FakeRegistry
from tests.unit.tools.builtin.code._rig import call, code_scope, fake_tools, run

IMAGE = "registry.test/indus-sandbox:code"


class RemoteShell:
    """A backend that is "elsewhere": real bash, own temp dir, every call recorded."""

    name = "fakeremote"

    def __init__(self, cwd: Path, temp_dir: Path, *, path: str | None = None) -> None:
        self._cwd = str(cwd)
        self.temp_dir = str(temp_dir)
        self.path = path if path is not None else os.environ.get("PATH", os.defpath)
        self.calls: list[tuple[str, bool]] = []

    @property
    def flags(self) -> BackendFlags:
        return BackendFlags(isolated=True, container_paths=True)

    @property
    def cwd(self) -> str:
        return self._cwd

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        bounded: bool = True,
        on_activity: ActivityCallback | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        self.calls.append((command, bounded))
        proc = await asyncio.create_subprocess_exec(
            shutil.which("bash") or "/bin/bash",
            "-c",
            command,
            cwd=cwd or self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={"PATH": self.path, "HOME": self._cwd, "LANG": "C.UTF-8"},
            start_new_session=True,
        )
        talk = asyncio.ensure_future(proc.communicate(stdin.encode() if stdin is not None else b""))
        loop = asyncio.get_running_loop()
        end = loop.time() + (timeout or 60)
        rc, timed_out, interrupted = 0, False, False
        while not talk.done():
            await asyncio.wait({talk}, timeout=0.02)
            if talk.done():
                break
            if cancel is not None and cancel.is_set():
                interrupted, rc = True, 130
            elif loop.time() > end:
                timed_out, rc = True, 124
            else:
                continue
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            break
        out, _ = await talk
        text = out.decode("utf-8", errors="replace")
        return ExecResult(
            text,
            rc if (timed_out or interrupted) else (proc.returncode or 0),
            timed_out=timed_out,
            interrupted=interrupted,
        )

    async def cleanup(self) -> None:
        return None

    def describe(self) -> str:
        return "bash standing in for a remote host"


def _strays(marker: str) -> list[int]:
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            raw = Path(f"/proc/{entry}/cmdline").read_bytes()
            state = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if marker.encode() in raw and state != "Z":
            found.append(int(entry))
    return found


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setattr(remote, "POLL_S", 0.05)
    yield
    asyncio.run(remote.POOL.shutdown())
    for _ in range(50):  # a stopped runner may need a moment to go
        if not _strays(str(tmp_path)):
            break
        threading.Event().wait(0.05)
    leftover = _strays(str(tmp_path))
    for pid in leftover:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    assert not leftover, f"remote processes left running: {leftover}"


def shell_env(tmp_path: Path, **kw: Any) -> tuple[RemoteShell, FakeRegistry]:
    work = tmp_path / "work"
    temp = tmp_path / "rtmp"
    work.mkdir(exist_ok=True)
    temp.mkdir(exist_ok=True)
    env = RemoteShell(work, temp, **kw)
    return env, FakeRegistry(env, backend="fakeremote")


def leftovers(tmp_path: Path) -> list[str]:
    return sorted(os.listdir(tmp_path / "rtmp"))


KERNEL = {"code_execution": {"kernel": "auto"}}


def test_default_poll_interval_is_half_a_second() -> None:
    assert REMOTE_POLL_S == 0.5


def test_kernel_round_trips_cells_and_answers_request_files(tmp_path: Path) -> None:
    env, environments = shell_env(tmp_path)
    scope = code_scope(tmp_path, KERNEL)
    registry, seen = fake_tools(tmp_path)
    target = tmp_path / "data.txt"
    target.write_text("remote hello")
    tool = ExecuteCodeTool()
    first = run(
        call(
            tool,
            scope,
            "counter = 41\nprint('stored')",
            registry=registry,
            cwd=tmp_path,
            environments=environments,
        )
    )
    code = (
        "from indus_tools import read_file\n"
        f"print(counter + 1, read_file({str(target)!r})['content'])\n"
        "import subprocess; subprocess.run(['echo', 'child-out'])\n"
    )
    second = run(
        call(tool, scope, code, registry=registry, cwd=tmp_path, environments=environments)
    )
    assert first["status"] == "success" and first["output"] == "stored\n"
    assert first["kernel"] == {"mode": "remote", "reused": False, "execution_count": 1}
    assert second["output"] == "42 remote hello\nchild-out\n"
    assert second["kernel"]["reused"] is True and second["tool_calls_made"] == 1
    assert seen == [("read_file", {"path": str(target), "offset": 1})]
    assert all(bounded is False for _, bounded in env.calls)  # R02.2: nothing internal is cut
    (folder,) = leftovers(tmp_path)
    assert folder.startswith("indus-rkernel-")
    assert oct(os.stat(tmp_path / "rtmp" / folder).st_mode & 0o777) == "0o700"
    assert "INDUS_RPC_TOKEN" not in " ".join(c for c, _ in env.calls)  # never on a command line
    asyncio.run(remote.POOL.shutdown())
    assert leftovers(tmp_path) == []


def test_two_megabyte_tool_result_arrives_whole(tmp_path: Path) -> None:
    env, environments = shell_env(tmp_path)
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    blob = "".join(chr(97 + (i * 7) % 26) for i in range(2_000_000))
    target = tmp_path / "big.txt"
    target.write_text(blob)
    code = (
        "import hashlib\nfrom indus_tools import read_file\n"
        f"text = read_file({str(target)!r})['content']\n"
        "print(len(text), hashlib.sha256(text.encode()).hexdigest())\n"
    )
    result = run(
        call(
            ExecuteCodeTool(),
            scope,
            code,
            registry=registry,
            cwd=tmp_path,
            environments=environments,
        )
    )
    assert result["output"] == f"2000000 {hashlib.sha256(blob.encode()).hexdigest()}\n"
    assert all(bounded is False for _, bounded in env.calls)


def test_kernel_start_failure_falls_back_to_a_shipped_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote, "RUNNER_SOURCE", "import sys\nsys.exit(3)\n")
    env, environments = shell_env(tmp_path)
    scope = code_scope(tmp_path, KERNEL)
    registry, seen = fake_tools(tmp_path)
    code = "from indus_tools import terminal\nprint(terminal('uname')['output'])"
    result = run(
        call(
            ExecuteCodeTool(),
            scope,
            code,
            registry=registry,
            cwd=tmp_path,
            environments=environments,
        )
    )
    assert result["status"] == "success" and result["output"] == "ran uname\n"
    assert result["kernel"] == {"mode": "script", "remote": True}
    assert seen == [("terminal", {"command": "uname"})]
    assert leftovers(tmp_path) == [] and len(remote.POOL) == 0


def test_missing_python3_ships_a_script_with_python(tmp_path: Path) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("cat", "rm", "mkdir", "chmod"):
        found = shutil.which(name)
        assert found is not None
        (tools / name).symlink_to(found)
    (tools / "python").symlink_to(sys.executable)
    env, environments = shell_env(tmp_path, path=str(tools))
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    result = run(
        call(
            ExecuteCodeTool(),
            scope,
            "import sys\nprint(sys.version_info >= (3, 8))",
            registry=registry,
            cwd=tmp_path,
            environments=environments,
        )
    )
    assert result["output"] == "True\n" and result["kernel"]["mode"] == "script"
    assert not any("python3 " in c and "runner.py" in c for c, _ in env.calls)


def test_no_python_at_all_is_a_clear_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _, environments = shell_env(tmp_path, path=str(empty))
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    result = run(
        call(
            ExecuteCodeTool(),
            scope,
            "print(1)",
            registry=registry,
            cwd=tmp_path,
            environments=environments,
        )
    )
    assert result["status"] == "error"
    assert "needs Python 3.8 or newer (python3) on the fakeremote backend" in result["error"]


def test_kernel_off_ships_one_script_per_call(tmp_path: Path) -> None:
    env, environments = shell_env(tmp_path)
    scope = code_scope(tmp_path)  # kernel off
    registry, _ = fake_tools(tmp_path)
    tool = ExecuteCodeTool()
    run(call(tool, scope, "x = 1", registry=registry, cwd=tmp_path, environments=environments))
    second = run(
        call(
            tool,
            scope,
            "print('x' in globals())\nimport os\nprint(os.getcwd())",
            registry=registry,
            cwd=tmp_path,
            environments=environments,
        )
    )
    assert second["output"] == f"False\n{tmp_path / 'work'}\n"
    assert second["kernel"] == {"mode": "script", "remote": True}
    assert leftovers(tmp_path) == []


def test_shipped_script_errors_and_timeouts(tmp_path: Path) -> None:
    _, environments = shell_env(tmp_path)
    scope = code_scope(tmp_path, {"code_execution": {"timeout": 2}})
    registry, _ = fake_tools(tmp_path)
    tool = ExecuteCodeTool()
    failed = run(
        call(
            tool,
            scope,
            "import json\njson.loads({'a': 1})",
            registry=registry,
            cwd=tmp_path,
            environments=environments,
        )
    )
    assert failed["status"] == "error" and "already return parsed dicts" in failed["hint"]
    slow = run(
        call(
            tool,
            scope,
            "import time\ntime.sleep(60)",
            registry=registry,
            cwd=tmp_path,
            environments=environments,
        )
    )
    assert slow["status"] == "timeout" and "2s limit" in slow["error"]
    assert leftovers(tmp_path) == []


def test_remote_kernel_timeout_stops_the_runner(tmp_path: Path) -> None:
    _, environments = shell_env(tmp_path)
    scope = code_scope(tmp_path, {"code_execution": {"kernel": "auto", "timeout": 2}})
    registry, _ = fake_tools(tmp_path)
    tool = ExecuteCodeTool()
    run(call(tool, scope, "kept = 1", registry=registry, cwd=tmp_path, environments=environments))
    (key,) = remote.POOL.live_keys()
    live = remote.POOL.get(key)
    assert live is not None
    pid = int(live.pid)
    slow = run(
        call(
            tool,
            scope,
            "import time\ntime.sleep(60)",
            registry=registry,
            cwd=tmp_path,
            environments=environments,
        )
    )
    assert slow["status"] == "timeout" and slow["kernel"]["ended"] is True
    assert not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text()
    assert leftovers(tmp_path) == []
    fresh = run(
        call(
            tool,
            scope,
            "print('kept' in globals())",
            registry=registry,
            cwd=tmp_path,
            environments=environments,
        )
    )
    assert fresh["output"] == "False\n" and fresh["kernel"]["state_lost"] is True


def test_parse_tick() -> None:
    output = (
        '@@INDUS-REQ rpc/req_000001.json\n{"tool": "read_file", "seq": 1}\n'
        "@@INDUS-REQ rpc/req_000002.json\nnot json\n"
        '@@INDUS-CELL\n{"state": "ok", "stdout": "a\\u2028b"}\n'
        "@@INDUS-DEAD\n"
    )
    tick = remote.parse_tick(output)
    assert tick.requests == [{"tool": "read_file", "seq": 1}, None]
    assert tick.cell == {"state": "ok", "stdout": "a b"} and tick.dead and not tick.gone
    assert remote.parse_tick("@@INDUS-GONE\n").gone
    assert remote.parse_tick("@@INDUS-CELL\n{broken\n").cell_broken


# -- over the fake docker client -------------------------------------------------------------------


def _no_reaper(*_: object) -> None:
    return None


@pytest.fixture
def docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Any, EnvironmentRegistry]]:
    from indus.backends.docker import environment as docker_environment
    from indus.backends.docker import probe, reaper

    if shutil.which("bash") is None:
        pytest.skip("the fake docker client runs commands with bash")
    probe.clear_caches()
    reaper.reset_for_tests()
    sandbox_tmp = tmp_path / "rtmp"
    sandbox_tmp.mkdir()
    monkeypatch.setattr(docker_environment, "SANDBOX_TEMP", str(sandbox_tmp))
    fake = FakeDocker(tmp_path / "fake-docker")
    fake.install()
    fake.add_image(IMAGE)

    async def create(request: CreateRequest) -> Any:
        return await docker_environment.open_docker(
            request, runner=fake.runner, os_name="linux", reaper=_no_reaper
        )

    (row,) = [e for e in default_entries() if e.name == "docker"]
    registry = EnvironmentRegistry([dataclasses.replace(row, factory=create)])
    work = tmp_path / "work"
    work.mkdir()
    scope = code_scope(
        tmp_path,
        {**KERNEL, "terminal": {"backend": "docker", "docker_image": IMAGE, "cwd": str(work)}},
    )
    scope = scope.with_env(
        scope.env.with_overlay(
            {"PATH": f"{fake.bin_dir}{os.pathsep}{scope.env.get('PATH', os.defpath)}"}
        )
    )
    try:
        yield scope, registry
    finally:
        asyncio.run(remote.POOL.shutdown())
        asyncio.run(registry.shutdown(fence=False))
        probe.clear_caches()
        reaper.reset_for_tests()


def test_docker_kernel_round_trip(tmp_path: Path, docker: tuple[Any, EnvironmentRegistry]) -> None:
    scope, environments = docker
    registry, seen = fake_tools(tmp_path)
    tool = ExecuteCodeTool()
    first = run(
        call(tool, scope, "acc = [1]", registry=registry, cwd=tmp_path, environments=environments)
    )
    second = run(
        call(
            tool,
            scope,
            "from indus_tools import terminal\nacc.append(2)\nprint(acc, terminal('id')['output'])",
            registry=registry,
            cwd=tmp_path,
            environments=environments,
        )
    )
    assert first["status"] == "success", first
    assert second["output"] == "[1, 2] ran id\n" and second["kernel"]["mode"] == "remote"
    assert seen == [("terminal", {"command": "id"})]
