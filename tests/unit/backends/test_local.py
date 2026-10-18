"""The local backend with real bash child processes (P05-03).

No timed sleeps: every wait ends with a real process exit or a kill, and every test leaves no
child behind.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from indus.backends import local as local_mod
from indus.backends.contract import ExecResult
from indus.backends.local import (
    LocalEnvironment,
    LocalProcess,
    choose_temp_dir,
    complete_path,
    force_kill_tree,
    nearest_usable_dir,
    open_local,
    prune_temp_dir,
    stop_tree,
    strip_app_pythonpath,
)
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not Path("/proc/self/stat").exists(),
    reason="needs bash and /proc",
)


def _env(scope: Scope, tmp_path: Path, **kwargs: Any) -> LocalEnvironment:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    temp = tmp_path / "t"
    temp.mkdir(exist_ok=True)
    options: dict[str, Any] = {"cwd": str(work), "timeout": 20, "temp_dir": str(temp)}
    options.update(kwargs)
    return LocalEnvironment(scope, **options)


async def _started(env: LocalEnvironment) -> LocalEnvironment:
    await env.start()
    return env


def _gone(pid: int) -> bool:
    """The process no longer runs (gone, or a zombie nobody reaped yet)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state in ("Z", "X")


def _read_pid(path: Path) -> int:
    return int(path.read_text().strip())


async def _tick(seconds: float = 0.01) -> None:
    """Let real time pass on the loop (child processes run on real time)."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    handle = loop.call_later(seconds, lambda: done.done() or done.set_result(None))
    try:
        await done
    finally:
        handle.cancel()


def test_cd_persists_and_cwd_is_reported(scope: Scope, tmp_path: Path) -> None:
    async def go() -> tuple[ExecResult, ExecResult, ExecResult, str]:
        env = await _started(_env(scope, tmp_path))
        try:
            first = await env.execute("mkdir -p 'a b' && cd 'a b' && echo moved")
            second = await env.execute("pwd")
            third = await env.execute("exit 3")
            return first, second, third, env.cwd
        finally:
            await env.cleanup()

    first, second, third, cwd = asyncio.run(go())
    real = os.path.realpath(tmp_path / "work" / "a b")
    assert first.output == "moved\n" and first.cwd_observed == real
    assert second.output == real + "\n"
    assert third.returncode == 3 and third.cwd_observed is None and cwd == real


@pytest.mark.parametrize(("persistent", "expected"), [(True, "bar"), (False, "unset")])
def test_exports_follow_the_persistent_shell_switch(
    scope: Scope, tmp_path: Path, persistent: bool, expected: str
) -> None:
    async def go() -> tuple[ExecResult, str]:
        env = await _started(_env(scope, tmp_path, persist_env=persistent))
        try:
            await env.execute("export FOO=bar; mkdir -p sub; cd sub")
            result = await env.execute('echo "${FOO-unset}"')
            return result, env.cwd
        finally:
            await env.cleanup()

    result, cwd = asyncio.run(go())
    assert result.output == expected + "\n"
    assert cwd.endswith("/sub")  # the cwd persists either way


def test_session_id_reaches_commands_but_not_the_snapshot(scope: Scope, tmp_path: Path) -> None:
    async def go() -> tuple[str, str]:
        env = await _started(_env(scope, tmp_path, session_id="sess-1"))
        try:
            result = await env.execute('echo "$INDUS_SESSION_ID $AI_AGENT"')
            return result.output, Path(env.snapshot_path).read_text()
        finally:
            await env.cleanup()

    output, snapshot = asyncio.run(go())
    assert output == "sess-1 indus-agent\n"
    assert "INDUS_SESSION_ID" not in snapshot and "AI_AGENT" not in snapshot


def test_removed_cwd_falls_back_to_the_parent(scope: Scope, tmp_path: Path) -> None:
    async def go() -> tuple[ExecResult, ExecResult]:
        env = await _started(_env(scope, tmp_path))
        try:
            gone = await env.execute("mkdir -p doomed && cd doomed && rm -rf ../doomed")
            after = await env.execute("pwd")
            return gone, after
        finally:
            await env.cleanup()

    _, after = asyncio.run(go())
    assert after.returncode == 0
    assert after.output == os.path.realpath(tmp_path / "work") + "\n"


def test_stdin_pipe_and_encode_failure(scope: Scope, tmp_path: Path) -> None:
    async def go() -> tuple[ExecResult, ExecResult]:
        env = await _started(_env(scope, tmp_path))
        try:
            ok = await env.execute("cat", stdin="one\ntwo")
            bad = await env.execute("cat; echo eof", stdin="bad \ud800 text")
            return ok, bad
        finally:
            await env.cleanup()

    ok, bad = asyncio.run(go())
    assert ok.output == "one\ntwo" and ok.stdin_error is None
    assert bad.stdin_error is not None and "eof" in bad.output
    assert "[stdin write failed:" in bad.output


def test_snapshot_failure_falls_back_to_plain_bash(scope: Scope, tmp_path: Path) -> None:
    home = tmp_path / "userhome"
    home.mkdir()
    (home / ".bash_profile").write_text("exit 7\n")
    routed = scope.with_env(scope.env.with_overlay({"HOME": str(home)}))

    async def go() -> tuple[bool, bool, ExecResult]:
        env = await _started(_env(routed, tmp_path))
        try:
            return env.snapshot_ready, env.prefer_nonlogin, await env.execute("echo works")
        finally:
            await env.cleanup()

    ready, nonlogin, result = asyncio.run(go())
    assert not ready and nonlogin
    assert result.output == "works\n" and result.returncode == 0


def test_grandchild_ignoring_sigterm_is_sigkilled(scope: Scope, tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    command = (
        f"bash -c 'trap \"\" TERM; echo $$ > {pid_file}; while :; do sleep 1; done' & "
        f"while [ ! -s {pid_file} ]; do :; done; wait"
    )

    async def go() -> ExecResult:
        env = await _started(_env(scope, tmp_path, term_grace_s=0.3))
        try:
            return await env.execute(command, timeout=1)
        finally:
            await env.cleanup()

    result = asyncio.run(go())
    assert result.timed_out and result.returncode == 124
    assert _gone(_read_pid(pid_file))


def test_setsid_escapee_is_reached_through_the_snapshot(scope: Scope, tmp_path: Path) -> None:
    if shutil.which("setsid") is None:
        pytest.skip("setsid is not installed")
    pid_file = tmp_path / "escapee.pid"
    command = f"setsid sleep 30 & echo $! > {pid_file}; wait"

    async def go() -> ExecResult:
        env = await _started(_env(scope, tmp_path, term_grace_s=0.3))
        try:
            return await env.execute(command, timeout=0.5)
        finally:
            await env.cleanup()

    result = asyncio.run(go())
    assert result.timed_out
    assert _gone(_read_pid(pid_file))


def test_cancelled_call_kills_its_command(scope: Scope, tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"

    async def go() -> None:
        env = await _started(_env(scope, tmp_path, term_grace_s=0.3))
        try:
            task = asyncio.ensure_future(env.execute(f"sleep 30 & echo $! > {pid_file}; wait"))
            while not pid_file.exists() or not pid_file.read_text().strip():
                await _tick()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            pid = _read_pid(pid_file)
            for _ in range(300):  # the detached kill finishes on its own
                if _gone(pid):
                    break
                await _tick()
        finally:
            await env.cleanup()

    asyncio.run(go())
    assert _gone(_read_pid(pid_file))


def _stranger() -> subprocess.Popen[bytes]:
    return subprocess.Popen(["sleep", "30"], start_new_session=True)


def test_recycled_pgid_is_never_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Our leader exited and was reaped; its PGID now names a stranger's group whose leader has
    another start time. Neither the ordered kill nor the force kill may touch it."""
    stranger = _stranger()
    done = subprocess.Popen(["true"], start_new_session=True)
    done.wait()
    try:
        proc = LocalProcess(done)
        real_start = local_mod.process_start_time(stranger.pid)
        assert real_start is not None
        proc.pgid = stranger.pid
        proc.leader_start = real_start + 1  # what we recorded for "our" leader
        asyncio.run(stop_tree(proc, clock=SystemClock(), grace=0.1))
        force_kill_tree(proc)
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


def test_matching_start_time_is_signalled() -> None:
    stranger = _stranger()
    try:
        proc = LocalProcess(stranger)
        asyncio.run(stop_tree(proc, clock=SystemClock(), grace=1.0))
        assert stranger.poll() == -signal.SIGTERM
    finally:
        if stranger.poll() is None:
            stranger.kill()
        stranger.wait()


def test_never_signals_our_own_group(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(local_mod.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    done = subprocess.Popen(["true"])
    done.wait()
    proc = LocalProcess(done)
    proc.pgid = os.getpgrp()
    force_kill_tree(proc, [])
    assert sent == []


def test_temp_dir_precedence(scope: Scope, tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    assert choose_temp_dir(str(configured), {"TMPDIR": "/x"}, scope.home) == str(configured)
    missing = str(tmp_path / "missing")
    assert choose_temp_dir(missing, {"TMPDIR": "/from/env/"}, scope.home) == "/from/env"
    assert choose_temp_dir("", {"TMP": "relative", "TEMP": "/t"}, scope.home) == "/t"
    assert choose_temp_dir("", {}, scope.home) == str(scope.home / "cache" / "terminal")


def test_terminal_cache_pruned_once_per_process(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = scope.home / "cache" / "terminal"
    cache.mkdir(parents=True)
    old_file = cache / "indus-snap-old.sh"
    old_dir = cache / "job"
    fresh = cache / "indus-snap-new.sh"
    old_file.write_text("x")
    old_dir.mkdir()
    (old_dir / "inner").write_text("y")
    fresh.write_text("z")
    long_ago = 1_000_000.0
    for path in (old_file, old_dir / "inner", old_dir):
        os.utime(path, (long_ago, long_ago))
    monkeypatch.setattr(local_mod, "_pruned", set())
    choose_temp_dir("", {}, scope.home)
    assert not old_file.exists() and not old_dir.exists() and fresh.exists()
    old_file.write_text("again")
    os.utime(old_file, (long_ago, long_ago))
    choose_temp_dir("", {}, scope.home)  # second call: no second prune
    assert old_file.exists()


def test_prune_keeps_a_dir_with_a_recent_write(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "fresh").write_text("x")
    os.utime(job, (1.0, 1.0))
    assert prune_temp_dir(tmp_path, now=os.path.getmtime(job / "fresh") + 10) == 0
    assert job.exists()


def test_complete_path_appends_sane_dirs_and_prepends_own_bin(tmp_path: Path) -> None:
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    path = complete_path(
        "/opt/tools::/usr/bin:/opt/tools", user_home=str(tmp_path), own_bin="/indus/bin"
    )
    parts = path.split(":")
    assert parts[0] == "/indus/bin" and parts[1] == "/opt/tools"
    assert parts.count("/opt/tools") == 1 and "" not in parts
    assert parts.index("/usr/bin") == 2  # the user's order wins
    assert "/bin" in parts and str(local_bin) in parts
    # An own bin dir already listed keeps its place.
    kept = complete_path("/usr/bin:/indus/bin", user_home=None, own_bin="/indus/bin").split(":")
    assert kept[:2] == ["/usr/bin", "/indus/bin"]


def test_app_owned_pythonpath_entries_are_stripped(tmp_path: Path) -> None:
    owned = tmp_path / "src"
    owned.mkdir()
    roots = frozenset({str(owned.resolve())})
    value = os.pathsep.join([str(owned), "/user/lib", "", str(owned) + "/"])
    assert strip_app_pythonpath(value, roots) == "/user/lib"
    assert strip_app_pythonpath(str(owned), roots) == ""


def test_run_env_drops_our_own_pythonpath(scope: Scope, tmp_path: Path) -> None:
    import indus

    ours = str(Path(indus.__file__).resolve().parent.parent)
    env_scope = scope.with_env(
        scope.env.with_overlay({"PYTHONPATH": os.pathsep.join([ours, "/user/lib"])})
    )
    env = _env(env_scope, tmp_path)
    built = env.run_env()
    assert built["PYTHONPATH"] == "/user/lib"
    assert built["INDUS_HOME"] == str(scope.home)
    only_ours = _env(scope.with_env(scope.env.with_overlay({"PYTHONPATH": ours})), tmp_path)
    assert "PYTHONPATH" not in only_ours.run_env()
    if sys.prefix != sys.base_prefix:
        venv_scope = scope.with_env(scope.env.with_overlay({"VIRTUAL_ENV": sys.prefix}))
        assert "VIRTUAL_ENV" not in _env(venv_scope, tmp_path).run_env()


def test_nearest_usable_dir(tmp_path: Path) -> None:
    assert nearest_usable_dir(str(tmp_path)) == str(tmp_path)
    assert nearest_usable_dir(str(tmp_path / "a" / "b")) == str(tmp_path)


def test_open_local_reads_the_scope_config(scope: Scope, tmp_path: Path) -> None:
    class Config:
        def __init__(self, values: dict[str, object]) -> None:
            self.values = values

        def get(self, path: str, /) -> object:
            return self.values.get(path)

    temp = tmp_path / "chosen-tmp"
    temp.mkdir()
    configured = scope.with_config(
        Config(
            {
                "terminal.cwd": str(tmp_path),
                "terminal.timeout": 42,
                "terminal.persistent_shell": False,
                "terminal.temp_dir": str(temp),
                "terminal.daemon_term_grace_s": 0.5,
                "tool_output.max_chars": 1234,
            }
        )
    )

    async def go() -> LocalEnvironment:
        env = await open_local(configured)
        await env.cleanup()
        return env

    env = asyncio.run(go())
    assert env.cwd == os.path.realpath(tmp_path) and env.timeout == 42
    assert not env.persist_env and env.term_grace_s == 0.5 and env.max_chars == 1234
    assert env.snapshot_path.startswith(str(temp) + "/indus-snap-")
    assert not Path(env.snapshot_path).exists()  # cleanup removed it
    assert env.describe().startswith("local bash")


def test_own_group_children_get_sigterm_one_by_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child sharing our process group is never reached with killpg; its descendants are
    signalled by pid instead."""
    sent: list[tuple[int, int]] = []
    child = subprocess.Popen(["bash", "-c", "sleep 30 & echo $!; wait"], stdout=subprocess.PIPE)
    assert child.stdout is not None
    grandchild = int(child.stdout.readline())  # printed after the fork: the tree exists now
    try:
        proc = LocalProcess(child)
        assert proc.pgid == os.getpgrp()
        real_kill = os.kill

        def spy(pid: int, sig: int) -> None:
            sent.append((pid, sig))
            real_kill(pid, sig)

        monkeypatch.setattr(local_mod.os, "kill", spy)
        asyncio.run(stop_tree(proc, clock=SystemClock(), grace=0.2))
        assert (grandchild, signal.SIGTERM) in sent
        assert _gone(grandchild)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdout.close()
        if not _gone(grandchild):
            os.kill(grandchild, signal.SIGKILL)
