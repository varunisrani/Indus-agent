"""Native Windows branches outside the terminal backend (RL26, wave 47).

Background jobs refuse with a "use WSL2" message; cron scripts, the code-execution child and
``indus verify`` (its host and its start command) go through the Git Bash backend's job objects.
Every case here runs on Linux with the OS family patched to ``win32`` and the Win32 job calls
replaced by a stand-in that starts the child in its own POSIX process group and kills that group
when "the job" is terminated; real Windows children are covered by ``test_local_windows.py``'s
``platforms("windows")`` cases.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.backends import local_windows as lw
from indus.backends import processes
from indus.backends.contract import BackendError, BackendUnavailable, ExecResult
from indus.backends.local_windows import GitBash, WindowsProcess
from indus.backends.processes import ProcessRegistry, SpawnRequest
from indus.cli.commands import verify as verify_cli
from indus.cron import script as cron_script
from indus.cron.ports import JobSpec
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.safety.child_env import child_env
from indus.service import console
from indus.tools.builtin.code import launch
from indus.tools.builtin.code.output import ByteCapture
from indus.tools.verify import runner as verify_runner
from tests.fakes.terminal import terminal_scope

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def scope(tmp_path: Path) -> Scope:
    return terminal_scope(tmp_path)


class GroupJobs:
    """The Win32 job calls over a POSIX process group (the stand-in's child leads its own)."""

    def __init__(self) -> None:
        self.pgid = 0
        self.terminated = 0
        self.closed = 0

    def open_process(self, pid: int) -> int | None:
        self.pgid = pid
        return pid

    def create_job(self) -> int | None:
        return 1

    def assign(self, job: int, process: int) -> bool:
        return True

    def resume(self, process: int) -> bool:
        return True

    def terminate_job(self, job: int, code: int) -> bool:
        self.terminated += 1
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pgid, signal.SIGKILL)
        return True

    def active_processes(self, job: int) -> int | None:
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return 0
        return 1

    def close(self, handle: int) -> None:
        self.closed += 1

    def pipe_available(self, fd: int) -> int | None:
        import fcntl
        import termios

        waiting = bytearray(4)
        try:
            fcntl.ioctl(fd, termios.FIONREAD, waiting)
        except OSError:
            return None
        count = int.from_bytes(waiting, sys.byteorder)
        if count == 0:
            import select

            readable, _, _ = select.select([fd], [], [], 0)
            if readable:
                return None  # readable with nothing waiting: every writer closed it
        return count


class StandIn:
    """Replaces ``start_windows_process``: same arguments, a POSIX child, ``GroupJobs``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.jobs = GroupJobs()

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin: str | None,
        script_path: str | None = None,
        api: Any = None,
        separate_stderr: bool = False,
    ) -> WindowsProcess:
        self.calls.append({"argv": list(argv), "env": dict(env), "separate": separate_stderr})
        popen = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if separate_stderr else subprocess.STDOUT,
            cwd=cwd,
            env=dict(env),
            start_new_session=True,
        )
        proc = WindowsProcess(popen, self.jobs, script_path=script_path)
        proc.attach_and_resume()
        return proc


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    stat_file = Path(f"/proc/{pid}/stat")
    return stat_file.exists() and stat_file.read_text().rsplit(")", 1)[-1].split()[0] in "ZX"


def _bash() -> GitBash:
    found = shutil.which("bash")
    if found is None:
        pytest.skip("needs bash")
    return GitBash(bash=found, root=None, tool_dirs=(), source="test")


# -- background processes: a clear refusal ---------------------------------------------------------


def test_background_job_on_native_windows_is_refused_with_wsl2(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[Any] = []

    def launcher(spec: Any) -> Any:
        launched.append(spec)
        raise AssertionError("nothing may start on native Windows")

    monkeypatch.setattr(processes, "os_family", lambda: "win32")
    registry = ProcessRegistry(launcher=launcher, receipts=None, checkpoints=False)
    request = SpawnRequest("sleep 5", scope, "s1", "k", str(scope.home), child_env(scope))

    record = asyncio.run(registry.spawn(request))

    assert not launched
    assert record.exited and record.reason == "failed_start" and record.exit_code == -1
    assert "WSL2" in record.tail(2000) and record.tail(2000).strip() == processes.WINDOWS_REFUSAL


# -- cron scripts ----------------------------------------------------------------------------------


def _install(scope: Scope, name: str, text: str) -> Path:
    folder = scope.home / "scripts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text)
    return path


def test_cron_shell_script_runs_under_git_bash_in_a_job(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in, bash = StandIn(), _bash()
    monkeypatch.setattr(cron_script, "os_family", lambda: "win32")
    monkeypatch.setattr(cron_script, "start_windows_process", stand_in)
    monkeypatch.setattr(cron_script, "find_git_bash", lambda env, home: bash)
    _install(scope, "hello.sh", 'printf "out:%s" "$MSYS_NO_PATHCONV"; printf oops >&2\n')

    result = asyncio.run(
        cron_script.run_script(scope, JobSpec(id="a1b2c3d4e5f6", script="hello.sh"))
    )

    assert result.ok, result.failure_text
    assert result.stdout == "out:1" and result.stderr == "oops"
    (call,) = stand_in.calls
    assert call["argv"][0] == bash.bash and call["argv"][1].endswith("/scripts/hello.sh")
    assert call["separate"] is True and call["env"]["PYTHONUTF8"] == "1"
    assert stand_in.jobs.closed >= 1  # the job handle is released


def test_cron_timeout_terminates_the_whole_job(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in, bash = StandIn(), _bash()
    monkeypatch.setattr(cron_script, "os_family", lambda: "win32")
    monkeypatch.setattr(cron_script, "start_windows_process", stand_in)
    monkeypatch.setattr(cron_script, "find_git_bash", lambda env, home: bash)
    pid_file = scope.home / "child.pid"
    _install(scope, "slow.sh", f"sleep 30 & echo $! > {pid_file}; wait\n")

    result = asyncio.run(
        cron_script.run_script(scope, JobSpec(id="a1b2c3d4e5f6", script="slow.sh"), timeout=1.0)
    )

    assert result.timed_out and not result.ok
    assert stand_in.jobs.terminated >= 1
    assert _gone(int(pid_file.read_text().strip()))  # the grandchild went with the job


def test_cron_shell_script_without_git_bash_is_refused(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(env: Mapping[str, str], home: Path) -> GitBash:
        raise BackendUnavailable("Git Bash was not found")

    monkeypatch.setattr(cron_script, "os_family", lambda: "win32")
    monkeypatch.setattr(cron_script, "find_git_bash", missing)
    _install(scope, "hello.sh", "echo hi\n")

    result = asyncio.run(
        cron_script.run_script(scope, JobSpec(id="a1b2c3d4e5f6", script="hello.sh"))
    )

    assert not result.ok and result.error is not None and "Git Bash" in result.error


# -- the code-execution child ----------------------------------------------------------------------


def test_code_child_runs_in_a_job_and_stop_terminates_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in = StandIn()
    monkeypatch.setattr(launch, "os_family", lambda: "win32")
    monkeypatch.setattr(launch, "start_windows_process", stand_in)
    code = "import os, sys, time; print('up', flush=True); sys.stderr.write('e'); time.sleep(30)"
    out, err = ByteCapture(1 << 16), ByteCapture(1 << 16)

    async def go() -> None:
        child = launch.Child(
            [sys.executable, "-c", code],
            cwd=str(tmp_path),
            env=dict(os.environ),
            stdout=out,
            stderr=err,
        )
        clock = SystemClock()
        deadline = clock.monotonic() + 10
        while b"up" not in out.last(100) and clock.monotonic() < deadline:
            await clock.sleep(0.05)
        assert child.alive() and child.job is not None and child.process is None
        await child.stop(clock)
        await child.finish_reading(clock)
        assert not child.alive() and _gone(child.pid)

    asyncio.run(go())
    assert stand_in.calls[0]["separate"] is True
    assert stand_in.jobs.terminated >= 1 and stand_in.jobs.closed >= 1
    assert err.last(10) == b"e"


def test_code_kernel_descriptors_are_refused_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in = StandIn()
    monkeypatch.setattr(launch, "os_family", lambda: "win32")
    monkeypatch.setattr(launch, "start_windows_process", stand_in)
    with pytest.raises(BackendUnavailable, match="WSL2"):
        launch.Child(
            [sys.executable, "-c", "pass"],
            cwd=str(tmp_path),
            env={},
            stdout=ByteCapture(64),
            stderr=ByteCapture(64),
            pass_fds=(5,),
        )
    assert not stand_in.calls


# -- indus verify ----------------------------------------------------------------------------------


class FakeHost:
    def __init__(self, bash: GitBash) -> None:
        self.bash = bash
        self.commands: list[str] = []
        self.cleaned = False

    async def execute(self, command: str, **kwargs: Any) -> ExecResult:
        self.commands.append(command)
        return ExecResult(output="ok\n", returncode=0)

    async def cleanup(self) -> None:
        self.cleaned = True


def test_verify_uses_the_git_bash_host_on_windows(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.backends import local

    host = FakeHost(_bash())
    opened: list[dict[str, Any]] = []

    async def open_windows(scope: Scope, **kwargs: Any) -> FakeHost:
        opened.append(kwargs)
        return host

    async def open_posix(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the POSIX local backend is not for native Windows")

    monkeypatch.setattr(verify_cli, "os_family", lambda: "win32")
    monkeypatch.setattr(lw, "open_local_windows", open_windows)
    monkeypatch.setattr(local, "open_local", open_posix)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "Makefile").write_text("test:\n\techo ok\n")

    recipe, result = asyncio.run(
        verify_cli.run_for(
            scope,
            root,
            phases=("test",),
            skip_start=True,
            port=None,
            phase_timeout=30,
            ready_timeout=1,
        )
    )

    assert recipe is not None and result is not None and result.ok
    assert opened == [{"cwd": str(root), "timeout": 30}]
    assert host.commands and host.cleaned


def test_windows_starter_boots_and_stops_the_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    stand_in = StandIn()
    monkeypatch.setattr(verify_runner, "start_windows_process", stand_in)
    starter = verify_runner.WindowsStarter(dict(os.environ), _bash(), clock=SystemClock())

    async def go() -> str:
        handle = await starter.start("echo $$; sleep 30 & wait", os.getcwd())
        await SystemClock().sleep(0.5)
        return await starter.stop(handle)

    output = asyncio.run(go())
    assert stand_in.calls[0]["argv"][1:] == ["-c", "echo $$; sleep 30 & wait"]
    assert stand_in.calls[0]["env"]["MSYS_NO_PATHCONV"] == "1"
    assert stand_in.jobs.terminated == 1 and _gone(int(output.split()[0]))


# -- the admin console's command runner (wave 48) --------------------------------------------------


def _fake_python(tmp_path: Path) -> str:
    """Stands in for ``python``: drops ``-m indus`` and acts on the console words."""
    path = tmp_path / "fake-python"
    path.write_text(
        "#!/bin/sh\n"
        "shift 2\n"
        'case "$1" in\n'
        '  status) printf "out:%s" "$NO_COLOR"; printf oops >&2 ;;\n'
        '  slow) sleep 30 & echo $! > "$2"; wait ;;\n'
        "esac\n"
    )
    path.chmod(0o755)
    return str(path)


def test_console_runs_its_command_in_a_job_on_windows(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in = StandIn()
    monkeypatch.setattr(console, "os_family", lambda: "win32", raising=False)
    monkeypatch.setattr(console, "start_windows_process", stand_in, raising=False)
    runner = console.SubprocessRunner(python=_fake_python(tmp_path))

    run = runner(scope, ["status"], console.CancelToken())

    assert (run.code, run.stdout, run.stderr, run.cancelled) == (0, "out:1", "oops", False)
    (call,) = stand_in.calls
    assert call["argv"][1:] == ["-m", "indus", "status"] and call["separate"] is True
    assert call["env"]["INDUS_HOME"] == str(scope.home)
    assert stand_in.jobs.closed >= 1  # the job handle is released


def test_console_cancel_terminates_the_job_on_windows(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in = StandIn()
    monkeypatch.setattr(console, "os_family", lambda: "win32", raising=False)
    monkeypatch.setattr(console, "start_windows_process", stand_in, raising=False)
    runner = console.SubprocessRunner(python=_fake_python(tmp_path))
    token, pid_file = console.CancelToken(), tmp_path / "child.pid"
    done: list[console.RunOutput] = []
    worker = threading.Thread(
        target=lambda: done.append(runner(scope, ["slow", str(pid_file)], token))
    )
    worker.start()
    clock = SystemClock()
    deadline = clock.monotonic() + 10
    while not (pid_file.exists() and pid_file.read_text().strip()) and clock.monotonic() < deadline:
        clock.sleep_sync(0.05)
    token.cancel()
    worker.join(10)

    assert done and done[0].cancelled
    assert stand_in.jobs.terminated >= 1 and stand_in.jobs.closed >= 1
    assert _gone(int(pid_file.read_text().strip()))  # the grandchild went with the job


def test_console_start_failure_on_windows_is_reported(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(argv: list[str], **kwargs: Any) -> WindowsProcess:
        raise BackendError("the new process could not be opened")

    monkeypatch.setattr(console, "os_family", lambda: "win32", raising=False)
    monkeypatch.setattr(console, "start_windows_process", refuse, raising=False)

    result = console.ConsoleEngine(scope).execute("status")

    assert result.status == "error" and "could not start" in result.message
    assert "could not be opened" in result.message
