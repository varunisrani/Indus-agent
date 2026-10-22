"""The Git Bash backend for native Windows (P25-24, best effort, D-16.55).

Most of the module is plain logic and runs on every OS: the MSYS path conversion table, the temp
dir choice, the Git Bash lookup over fake install trees, the child environment, the script text,
the pipe reader and the process handle over a fake job API. The cases at the end start real Git
Bash children and run only on Windows (``platforms("windows")``): the contract subset of the
Windows CI lane (echo, cwd marker, timeout rc 124, interrupt rc 130, the tree killed).
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from indus.backends import local_windows as lw
from indus.backends import registry
from indus.backends.contract import BackendError, BackendUnavailable, ExecResult
from indus.backends.local_windows import (
    GitBash,
    LocalWindowsEnvironment,
    WindowsProcess,
    drain_pipe,
    find_git_bash,
    to_native_path,
    to_shell_path,
    windows_path_value,
    windows_temp_dir,
)
from indus.backends.registry import CreateRequest, EnvKey
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.platform import known_dirs

# -- path conversion (every OS) -------------------------------------------------------------------

# native → what Git Bash is given
TO_SHELL = [
    ("C:\\Users\\me\\proj", "/c/Users/me/proj"),
    ("c:\\Users\\me", "/c/Users/me"),
    ("D:/data/set", "/d/data/set"),
    ("C:\\", "/c/"),
    ("C:", "/c"),
    ("C:\\dir with space\\x", "/c/dir with space/x"),
    ("\\\\server\\share\\dir", "//server/share/dir"),
    ("\\\\?\\C:\\very\\long", "/c/very/long"),
    ("\\\\?\\UNC\\server\\share\\x", "//server/share/x"),
    ("relative\\file.txt", "relative/file.txt"),
    ("/c/already/msys", "/c/already/msys"),
    ("C:name", "C:name"),
    ("", ""),
]

ROOT = "C:\\Program Files\\Git"
TEMP = "C:\\Users\\me\\AppData\\Local\\Temp"

# what bash printed → native
TO_NATIVE = [
    ("/c/Users/me/proj", "C:\\Users\\me\\proj"),
    ("/d", "D:\\"),
    ("/d/", "D:\\"),
    ("/c/x/", "C:\\x\\"),
    ("/cygdrive/e/work", "E:\\work"),
    ("//server/share/dir", "\\\\server\\share\\dir"),
    ("/usr/bin", ROOT + "\\usr\\bin"),
    ("/", ROOT),
    ("/tmp", TEMP),
    ("/tmp/job/out", TEMP + "\\job\\out"),
    ("/tmpfiles/x", ROOT + "\\tmpfiles\\x"),
    ("/cygdrive", ROOT + "\\cygdrive"),
    ("/mnt/data", ROOT + "\\mnt\\data"),
    ("C:/Users/me", "C:\\Users\\me"),
    ("c:\\Users\\me", "C:\\Users\\me"),
    ("relative/file.txt", "relative\\file.txt"),
    ("", ""),
]


@pytest.mark.parametrize(("native", "shell"), TO_SHELL)
def test_native_to_shell_table(native: str, shell: str) -> None:
    assert to_shell_path(native) == shell


@pytest.mark.parametrize(("shell", "native"), TO_NATIVE)
def test_shell_to_native_table(shell: str, native: str) -> None:
    assert to_native_path(shell, root=ROOT, temp=TEMP) == native


@pytest.mark.parametrize(
    "native",
    ["C:\\Users\\me\\proj", "D:\\", "\\\\server\\share\\dir", "E:\\a b\\c.txt"],
)
def test_round_trip(native: str) -> None:
    assert to_native_path(to_shell_path(native), root=ROOT, temp=TEMP) == native


def test_msys_only_paths_stay_unchanged_without_the_git_root() -> None:
    assert to_native_path("/usr/bin") == "/usr/bin"
    assert to_native_path("/tmp/x", root=ROOT) == ROOT + "\\tmp\\x"


# -- temp dir (every OS) --------------------------------------------------------------------------


def test_temp_dir_is_the_home_cache_with_forward_slashes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    chosen = windows_temp_dir("", home)
    assert chosen == str(home / "cache" / "terminal").replace("\\", "/")
    assert "\\" not in chosen and Path(chosen).is_dir()


def test_configured_temp_dir_wins_when_it_exists(tmp_path: Path) -> None:
    mine = tmp_path / "mine"
    mine.mkdir()
    assert windows_temp_dir(str(mine) + os.sep, tmp_path / "home") == str(mine).replace("\\", "/")
    missing = str(tmp_path / "missing")
    assert windows_temp_dir(missing, tmp_path / "home").endswith("cache/terminal")
    assert windows_temp_dir("relative", tmp_path / "home").endswith("cache/terminal")


def test_drive_root_keeps_its_slash() -> None:
    assert lw._forward_slashes("C:\\") == "C:/"
    assert lw._forward_slashes("C:\\Temp\\") == "C:/Temp"


# -- finding Git Bash (every OS, fake trees) ------------------------------------------------------


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _git_install(root: Path, *, bash: bool = True) -> Path:
    _exe(root / "cmd" / "git.exe")
    (root / "mingw64" / "bin").mkdir(parents=True)
    (root / "usr" / "bin").mkdir(parents=True)
    if bash:
        _exe(root / "bin" / "bash.exe")
    return root


def _find(tmp_path: Path, env: dict[str, str], home: Path | None = None) -> GitBash:
    return find_git_bash(
        env,
        home=home if home is not None else tmp_path / "indus-home",
        family="win32",
        user_home=lambda: tmp_path / "user",
    )


def test_explicit_override_wins(tmp_path: Path) -> None:
    bash = _exe(tmp_path / "custom" / "bin" / "bash.exe")
    _git_install(tmp_path / "indus-home" / "tools" / "git-2.51.0.windows.1")
    found = _find(tmp_path, {lw.GIT_BASH_VAR: str(bash)})
    assert found.bash == str(bash) and found.source == "override"
    assert found.root == str(tmp_path / "custom")


def test_explicit_override_that_is_missing_is_refused(tmp_path: Path) -> None:
    _git_install(tmp_path / "indus-home" / "tools" / "git-2.51.0.windows.1")
    with pytest.raises(BackendUnavailable) as caught:
        _find(tmp_path, {lw.GIT_BASH_VAR: str(tmp_path / "nope.exe")})
    assert lw.GIT_BASH_VAR in str(caught.value)


def test_newest_portable_git_from_the_installer(tmp_path: Path) -> None:
    tools = tmp_path / "indus-home" / "tools"
    _git_install(tools / "git-2.9.0.windows.1")
    newest = _git_install(tools / "git-2.51.0.windows.1")
    on_path = _git_install(tmp_path / "elsewhere")
    found = _find(tmp_path, {"PATH": str(on_path / "cmd")})
    assert found.source == "portable" and found.root == str(newest)
    assert found.bash == str(newest / "bin" / "bash.exe")
    assert found.tool_dirs == (
        str(newest / "mingw64" / "bin"),
        str(newest / "usr" / "bin"),
        str(newest / "bin"),
    )


def test_git_on_path_leads_to_its_bash(tmp_path: Path) -> None:
    root = _git_install(tmp_path / "Git")
    found = _find(tmp_path, {"Path": os.pathsep.join([str(tmp_path / "x"), str(root / "cmd")])})
    assert found.source == "git" and found.bash == str(root / "bin" / "bash.exe")


def test_arm64_git_lists_its_own_tool_folder(tmp_path: Path) -> None:
    root = tmp_path / "GitArm"
    _exe(root / "cmd" / "git.exe")
    _exe(root / "usr" / "bin" / "bash.exe")
    (root / "clangarm64" / "bin").mkdir(parents=True)
    found = _find(tmp_path, {"PATH": str(root / "cmd")})
    assert found.bash == str(root / "usr" / "bin" / "bash.exe")
    assert found.tool_dirs == (str(root / "clangarm64" / "bin"), str(root / "usr" / "bin"))


def test_mingit_without_bash_does_not_count(tmp_path: Path) -> None:
    mingit = _git_install(tmp_path / "MinGit", bash=False)
    other = _exe(tmp_path / "msys" / "usr" / "bin" / "bash.exe")
    found = _find(tmp_path, {"PATH": os.pathsep.join([str(mingit / "cmd"), str(other.parent)])})
    assert found.source == "PATH" and found.bash == str(other)
    assert found.root == str(tmp_path / "msys")


def test_known_install_dirs_including_program_files_x86(tmp_path: Path) -> None:
    root = _git_install(tmp_path / "pf86" / "Git")
    found = _find(tmp_path, {"ProgramFiles(x86)": str(tmp_path / "pf86")})
    assert found.source == "known_dir" and found.root == str(root)
    assert "%ProgramFiles(x86)%/Git" in known_dirs.git_for_windows_roots("win32")
    assert known_dirs.git_for_windows_roots("linux") == ()
    assert "%ProgramFiles%/Git" in known_dirs.all_literals()


def test_the_wsl_launcher_is_never_taken(tmp_path: Path) -> None:
    launcher = _exe(tmp_path / "Windows" / "System32" / "bash.exe")
    alias = _exe(tmp_path / "Microsoft" / "WindowsApps" / "bash.exe")
    real = _exe(tmp_path / "Git" / "usr" / "bin" / "bash.exe")
    path = os.pathsep.join([str(launcher.parent), str(alias.parent), str(real.parent)])
    found = _find(tmp_path, {"PATH": path})
    assert found.bash == str(real)


def test_nothing_found_names_git_for_windows_and_wsl2(tmp_path: Path) -> None:
    launcher = _exe(tmp_path / "Windows" / "System32" / "bash.exe")
    with pytest.raises(BackendUnavailable) as caught:
        _find(tmp_path, {"PATH": str(launcher.parent)})
    assert "Git for Windows" in str(caught.value)
    assert caught.value.hint is not None and "WSL2" in caught.value.hint


# -- child environment and script text (every OS) -------------------------------------------------


def test_path_value_puts_git_tools_first_without_repeats() -> None:
    value = "C:\\Windows\\System32;C:\\Program Files\\Git\\usr\\bin\\;;C:\\tools"
    front = ("C:\\Program Files\\Git\\mingw64\\bin", "C:\\Program Files\\Git\\usr\\bin")
    result = windows_path_value(value, front=front, own_bin="C:\\indus\\bin").split(";")
    assert result == [
        "C:\\Program Files\\Git\\mingw64\\bin",
        "C:\\Program Files\\Git\\usr\\bin",
        "C:\\indus\\bin",
        "C:\\Windows\\System32",
        "C:\\tools",
    ]
    listed = windows_path_value("c:\\INDUS\\bin;C:\\x", front=(), own_bin="C:\\indus\\bin")
    assert listed == "c:\\INDUS\\bin;C:\\x"


def _bash(root: Path) -> GitBash:
    tools = (str(root / "mingw64" / "bin"), str(root / "usr" / "bin"))
    return GitBash(bash=str(root / "bin" / "bash.exe"), root=str(root), tool_dirs=tools, source="t")


def _env(scope: Scope, tmp_path: Path, **kwargs: Any) -> LocalWindowsEnvironment:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    temp = tmp_path / "t"
    temp.mkdir(exist_ok=True)
    options: dict[str, Any] = {
        "bash": _bash(tmp_path / "Git"),
        "cwd": str(work),
        "timeout": 20,
        "temp_dir": str(temp).replace("\\", "/"),
    }
    options.update(kwargs)
    return LocalWindowsEnvironment(scope, **options)


def test_the_environment_meets_the_local_row(scope: Scope, tmp_path: Path) -> None:
    env = _env(scope, tmp_path)
    assert env.name == "local" and env.flags.host_local and not env.flags.container_paths
    assert "Git Bash" in env.describe() and "best effort" in env.describe()


def test_scripts_see_msys_paths(scope: Scope, tmp_path: Path) -> None:
    env = _env(scope, tmp_path, temp_dir="C:/Users/me/.indus/cache/terminal")
    assert env.snapshot_path.startswith("C:/Users/me/.indus/cache/terminal/indus-snap-")
    assert env.paths.snapshot.startswith("/c/Users/me/.indus/cache/terminal/indus-snap-")
    assert env.paths.snapshot_tmp_template.endswith(".tmp.XXXXXXXXXX")
    script = env._wrap("ls", "C:\\Users\\me\\my proj")
    assert script.splitlines()[0] == lw.UTF8_CONSOLE
    assert "builtin cd -- '/c/Users/me/my proj' || exit 126" in script
    assert 'builtin cd -- "$HOME"/x || exit 126' in env._wrap("ls", "~/x")


def _marked(env: LocalWindowsEnvironment, path: str) -> ExecResult:
    marker = env.paths.marker
    return ExecResult(output=f"out\n{marker}{path}{marker}\n", returncode=0)


def test_reported_cwd_comes_back_native(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(scope, tmp_path)
    seen: list[str] = []

    def isdir(path: str) -> bool:
        seen.append(path)
        return path == "C:\\work\\sub"

    monkeypatch.setattr(lw.os.path, "isdir", isdir)
    result = env._take_cwd(_marked(env, "/c/work/sub"))
    assert result.output == "out" and result.cwd_observed == "C:\\work\\sub"
    assert env.cwd == "C:\\work\\sub" and seen == ["C:\\work\\sub"]

    kept = env._take_cwd(_marked(env, "/d/elsewhere"), adopt=False)
    assert kept.cwd_observed is None and kept.output == "out" and env.cwd == "C:\\work\\sub"

    other = env._take_cwd(_marked(env, "/c/work/sub"), adopt=False)
    assert other.cwd_observed == "C:\\work\\sub" and env.cwd == "C:\\work\\sub"


def test_run_env_utf8_msys_defaults_and_path(scope: Scope, tmp_path: Path) -> None:
    env = _env(scope, tmp_path, env_overrides={"MSYS_NO_PATHCONV": "0", "LC_ALL": "de_DE.UTF-8"})
    run = env.run_env()
    assert run["PYTHONUTF8"] == "1" and run["PYTHONIOENCODING"] == "utf-8"
    assert run["MSYS_NO_PATHCONV"] == "0"  # the user's value stays
    assert run["MSYS2_ARG_CONV_EXCL"] == "*"
    assert "LANG" not in run or run["LANG"] == scope.env.get("LANG")
    first = run["PATH"].split(";")[:2]
    assert first == list(env.bash.tool_dirs)

    plain = _env(scope, tmp_path, env_overrides={"LANG": "", "LC_ALL": ""}).run_env()
    assert plain["LANG"] == lw.UTF8_LOCALE


def test_script_files_are_lf_private_and_cleaned_up(scope: Scope, tmp_path: Path) -> None:
    env = _env(scope, tmp_path)
    path = env._write_script("echo one\necho two\n")
    assert Path(path).read_bytes() == b"echo one\necho two\n"
    assert Path(path).name.startswith(f"indus-cmd-{env.env_id}-")
    if os.name == "posix":
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
    asyncio.run(env.cleanup())
    assert not Path(path).exists()


# -- the pipe reader (every OS) -------------------------------------------------------------------


class FakePipe:
    """A real pipe with a byte count standing in for ``PeekNamedPipe``."""

    def __init__(self) -> None:
        self.read_fd, self.write_fd = os.pipe()
        self.pending = 0
        self.closed = False

    def put(self, data: bytes) -> None:
        os.write(self.write_fd, data)
        self.pending += len(data)

    def close_writer(self) -> None:
        os.close(self.write_fd)
        self.closed = True

    def available(self) -> int | None:
        waiting, self.pending = self.pending, 0  # the reader takes all of it
        if waiting == 0 and self.closed:
            return None
        return waiting

    def done(self) -> None:
        os.close(self.read_fd)
        if not self.closed:
            os.close(self.write_fd)


def test_drain_decodes_split_utf8_until_the_pipe_closes() -> None:
    pipe = FakePipe()
    text = "été ✓"
    raw = text.encode()
    pipe.put(raw[:2])
    pipe.put(raw[2:])
    pipe.put(b"\xff!")
    pipe.close_writer()
    out: list[str] = []
    try:
        drain_pipe(
            pipe.read_fd,
            available=pipe.available,
            exited=lambda: False,
            sink=out.append,
            stop=threading.Event(),
            poll_s=0.001,
        )
    finally:
        pipe.done()
    assert "".join(out) == text + "\ufffd!"


def test_drain_stops_after_the_leader_exits_while_the_pipe_stays_open() -> None:
    pipe = FakePipe()
    pipe.put(b"tail")
    out: list[str] = []
    rounds: list[int] = []

    def exited() -> bool:
        rounds.append(1)
        return True

    try:
        drain_pipe(
            pipe.read_fd,
            available=pipe.available,
            exited=exited,
            sink=out.append,
            stop=threading.Event(),
            poll_s=0.001,
            idle_rounds=3,
        )
    finally:
        pipe.done()
    assert "".join(out) == "tail" and len(rounds) == 3


def test_drain_honours_stop() -> None:
    pipe = FakePipe()
    stop = threading.Event()
    stop.set()
    out: list[str] = []
    try:
        drain_pipe(
            pipe.read_fd, available=pipe.available, exited=lambda: False, sink=out.append, stop=stop
        )
    finally:
        pipe.done()
    assert out == []


# -- the process handle over a fake job API (every OS) --------------------------------------------


class FakeJobs:
    """Stands in for the Win32 job calls; terminating the job kills ``victim`` for real."""

    def __init__(
        self,
        victim: subprocess.Popen[bytes],
        *,
        job: bool = True,
        assign: bool = True,
        resume: bool = True,
    ) -> None:
        self.victim = victim
        self.calls: list[tuple[str, int]] = []
        self.give_job = job
        self.assign_ok = assign
        self.resume_ok = resume
        self.active = 1
        self.terminated: list[int] = []
        self.closed: list[int] = []

    def open_process(self, pid: int) -> int | None:
        self.calls.append(("open", pid))
        return 7 if pid > 0 else None

    def create_job(self) -> int | None:
        self.calls.append(("job", 0))
        return 9 if self.give_job else None

    def assign(self, job: int, process: int) -> bool:
        self.calls.append(("assign", job))
        return self.assign_ok

    def resume(self, process: int) -> bool:
        self.calls.append(("resume", process))
        return self.resume_ok

    def terminate_job(self, job: int, code: int) -> bool:
        self.terminated.append(job)
        self.victim.kill()
        self.victim.wait(timeout=10)
        self.active = 0
        return True

    def active_processes(self, job: int) -> int | None:
        return self.active

    def close(self, handle: int) -> None:
        self.closed.append(handle)

    def pipe_available(self, fd: int) -> int | None:
        return None


def _sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _finish(popen: subprocess.Popen[bytes]) -> None:
    if popen.poll() is None:
        popen.kill()
    popen.wait(timeout=10)
    if popen.stdout is not None:
        popen.stdout.close()


def test_child_joins_its_job_before_it_runs(tmp_path: Path) -> None:
    popen = _sleeper()
    jobs = FakeJobs(popen)
    script = tmp_path / "cmd.sh"
    script.write_text("true\n")
    proc = WindowsProcess(popen, jobs, script_path=str(script))
    try:
        proc.attach_and_resume()
        assert [name for name, _ in jobs.calls] == ["open", "job", "assign", "resume"]
        assert jobs.closed == [7] and proc.job == 9
        assert proc.tree_running()
        proc.release()  # the leader still runs: the job closes, the script stays
        assert jobs.closed == [7, 9] and proc.job is None and script.exists()
        proc.job = 9
        proc.terminate_tree()
        assert jobs.terminated == [9] and not proc.tree_running() and proc.poll() is not None
    finally:
        _finish(popen)
    proc.release()
    proc.release()
    assert not script.exists() and jobs.closed == [7, 9, 9]


def test_without_a_job_the_leader_alone_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lw, "_warned_no_job", False)
    popen = _sleeper()
    jobs = FakeJobs(popen, assign=False)
    proc = WindowsProcess(popen, jobs)
    try:
        proc.attach_and_resume()
        assert proc.job is None and 9 in jobs.closed
        assert ("resume", 7) in jobs.calls and proc.tree_running()
        proc.terminate_tree()
        popen.wait(timeout=10)
        assert not proc.tree_running() and jobs.terminated == []
    finally:
        _finish(popen)


def test_a_child_that_cannot_resume_is_killed() -> None:
    popen = _sleeper()
    jobs = FakeJobs(popen, resume=False)
    proc = WindowsProcess(popen, jobs)
    try:
        with pytest.raises(BackendError, match="could not be resumed"):
            proc.attach_and_resume()
        assert jobs.terminated == [9] and proc.job is None
    finally:
        _finish(popen)


# -- the registry row (every OS) ------------------------------------------------------------------


def test_the_local_row_opens_git_bash_on_windows(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[dict[str, Any]] = []
    sentinel = object()

    async def fake_open(scope: Scope, **kwargs: Any) -> object:
        opened.append(kwargs)
        return sentinel

    monkeypatch.setattr(registry, "os_family", lambda: "win32")
    monkeypatch.setattr(lw, "open_local_windows", fake_open)
    request = CreateRequest(
        scope=scope,
        key=EnvKey(str(scope.home), "task", "t1", "local"),
        task_id="t1",
        session_id="s1",
        overrides={"cwd": "C:\\work"},
    )
    assert asyncio.run(registry._create_local(request)) is sentinel
    assert opened == [{"session_id": "s1", "cwd": "C:\\work"}]


# -- the whole flow with POSIX bash standing in (Linux and macOS) ---------------------------------
# Everything but the Win32 calls and the drive-letter paths: script files, the wrapper, the pipe
# reader, the job-shaped stop (a process group plays the job), stdin and the cwd marker.


class GroupJobs:
    """The job API played by a POSIX process group."""

    def open_process(self, pid: int) -> int | None:
        return pid

    def create_job(self) -> int | None:
        return -1

    def assign(self, job: int, process: int) -> bool:
        return True

    def resume(self, process: int) -> bool:
        return True

    def terminate_job(self, job: int, code: int) -> bool:
        return False  # set per process below

    def active_processes(self, job: int) -> int | None:
        return None

    def close(self, handle: int) -> None:
        return None

    def pipe_available(self, fd: int) -> int | None:
        import fcntl
        import select
        import termios

        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            return 0
        raw = fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0")
        waiting = int.from_bytes(raw, sys.byteorder)
        return waiting or None


class GroupJob(GroupJobs):
    def __init__(self, pgid: int) -> None:
        self.pgid = pgid

    def terminate_job(self, job: int, code: int) -> bool:
        import contextlib
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pgid, signal.SIGKILL)
        return True

    def active_processes(self, job: int) -> int | None:
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return 0
        return 1


def _posix_start(
    argv: list[str],
    *,
    cwd: str,
    env: Any,
    stdin: str | None,
    script_path: str | None = None,
    api: Any = None,
) -> WindowsProcess:
    popen = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        env=dict(env),
        start_new_session=True,
    )
    proc = WindowsProcess(popen, GroupJob(popen.pid), script_path=script_path)
    proc.attach_and_resume()
    if stdin is not None:
        proc.start_stdin(stdin)
    return proc


def _stand_in(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> LocalWindowsEnvironment:
    import shutil

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("needs bash")
    monkeypatch.setattr(lw, "start_windows_process", _posix_start)
    monkeypatch.setattr(LocalWindowsEnvironment, "native_path", lambda self, path: path)
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    temp = tmp_path / "t"
    temp.mkdir()
    return LocalWindowsEnvironment(
        scope,
        bash=GitBash(bash=bash, root=None, tool_dirs=(), source="test"),
        cwd=os.path.realpath(work),
        timeout=20,
        temp_dir=str(temp),
        clock=SystemClock(),
    )


@pytest.mark.platforms("posix")
def test_stand_in_runs_commands_from_script_files(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _stand_in(scope, tmp_path, monkeypatch)
    work = env.cwd
    long_text = "y" * 40_000
    hello, moved, here, exported, seen, piped, long, failed = _run_each(
        env,
        [
            ("printf %s hello", None),
            ("cd sub", None),
            ("printf %s here", None),
            ("export KEPT=1", None),
            ('printf %s "$KEPT"', None),
            ("cat", "first\nsecond"),
            (f"printf %s '{long_text}'", None),
            ("exit 3", None),
        ],
    )
    assert hello.output == "hello" and hello.returncode == 0 and hello.cwd_observed == work
    assert moved.cwd_observed == os.path.join(work, "sub") and here.output == "here"
    assert exported.returncode == 0 and seen.output == "1"
    assert piped.output == "first\nsecond" and long.output[:10] == "y" * 10
    assert failed.returncode == 3 and failed.cwd_observed is None
    assert not list((tmp_path / "t").iterdir())  # scripts and snapshot gone after cleanup


@pytest.mark.platforms("posix")
def test_stand_in_deadline_kills_the_tree(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _stand_in(scope, tmp_path, monkeypatch)
    (result,) = _run(env, "sleep 30 & echo $!; wait", timeout=1)
    assert result.returncode == 124 and result.timed_out
    assert _posix_gone(int(result.output.split()[0]))


def _posix_gone(pid: int) -> bool:
    """Gone, or a zombie nobody reaped yet (some containers' PID 1 never does)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    stat_file = Path(f"/proc/{pid}/stat")
    return stat_file.exists() and stat_file.read_text().rsplit(")", 1)[-1].split()[0] in "ZX"


@pytest.mark.platforms("posix")
def test_stand_in_interrupt_gives_130(
    scope: Scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _stand_in(scope, tmp_path, monkeypatch)
    stop = threading.Event()

    async def go() -> ExecResult:
        await env.start()
        try:
            asyncio.get_running_loop().call_later(0.3, stop.set)
            return await env.execute("sleep 30", timeout=20, cancel=stop)
        finally:
            await env.cleanup()

    result = asyncio.run(go())
    assert result.returncode == 130 and result.interrupted


# -- real Git Bash children (Windows only) --------------------------------------------------------


def _same_dir(left: str, right: str) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def _windows_env(scope: Scope, tmp_path: Path) -> LocalWindowsEnvironment:
    try:
        bash = find_git_bash(scope.env, home=scope.home)
    except BackendUnavailable as exc:  # a Windows host without Git for Windows
        pytest.skip(str(exc))
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    temp = tmp_path / "t"
    temp.mkdir()
    return LocalWindowsEnvironment(
        scope,
        bash=bash,
        cwd=os.path.realpath(work),
        timeout=20,
        temp_dir=str(temp).replace("\\", "/"),
        clock=SystemClock(),
    )


def _run(env: LocalWindowsEnvironment, *commands: str, **kwargs: Any) -> list[ExecResult]:
    async def go() -> list[ExecResult]:
        await env.start()
        try:
            return [await env.execute(c, **kwargs) for c in commands]
        finally:
            await env.cleanup()

    return asyncio.run(go())


def _gone(pid: int) -> bool:
    """The Windows process ``pid`` has exited (waits up to 5 s; never signals it)."""
    import ctypes

    kernel32: Any = getattr(ctypes, "windll").kernel32  # noqa: B009 - typed for Windows only
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, 5000) == 0
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.platforms("windows")
def test_windows_echo_and_cwd_marker(scope: Scope, tmp_path: Path) -> None:
    env = _windows_env(scope, tmp_path)
    work = env.cwd
    hello, two = _run(env, "printf %s hello", "echo two words")
    assert hello.output == "hello" and hello.returncode == 0
    assert hello.cwd_observed is not None and _same_dir(hello.cwd_observed, work)
    assert "__INDUS_CWD_" not in hello.output and two.output == "two words\n"


@pytest.mark.platforms("windows")
def test_windows_cd_moves_the_environment(scope: Scope, tmp_path: Path) -> None:
    env = _windows_env(scope, tmp_path)
    target = os.path.join(env.cwd, "sub")
    moved, here = _run(env, "cd sub", "printf %s here")
    assert moved.cwd_observed is not None and _same_dir(moved.cwd_observed, target)
    assert _same_dir(env.cwd, target) and here.output == "here"


@pytest.mark.platforms("windows")
def test_windows_deadline_gives_124(scope: Scope, tmp_path: Path) -> None:
    env = _windows_env(scope, tmp_path)
    (result,) = _run(env, "sleep 30", timeout=1)
    assert result.returncode == 124 and result.timed_out
    assert result.output.endswith("[Command timed out after 1s]")


@pytest.mark.platforms("windows")
def test_windows_interrupt_gives_130(scope: Scope, tmp_path: Path) -> None:
    env = _windows_env(scope, tmp_path)
    stop = threading.Event()

    async def go() -> ExecResult:
        await env.start()
        try:
            asyncio.get_running_loop().call_later(0.5, stop.set)
            return await env.execute("sleep 30", timeout=20, cancel=stop)
        finally:
            await env.cleanup()

    result = asyncio.run(go())
    assert result.returncode == 130 and result.interrupted
    assert result.output.endswith("[Command interrupted]")


@pytest.mark.platforms("windows")
def test_windows_timeout_kills_the_whole_tree(scope: Scope, tmp_path: Path) -> None:
    env = _windows_env(scope, tmp_path)
    python = to_shell_path(sys.executable)
    child = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"
    (result,) = _run(env, f"'{python}' -c '{child}' & wait", timeout=3)
    assert result.returncode == 124
    pid = int(result.output.split()[0])
    assert _gone(pid)


@pytest.mark.platforms("windows")
def test_windows_stdin_and_utf8_output(scope: Scope, tmp_path: Path) -> None:
    env = _windows_env(scope, tmp_path)
    piped, accents = _run_each(
        env, [("cat", "first\nsecond"), ("printf '\\303\\251t\\303\\251'", None)]
    )
    assert piped.output == "first\nsecond" and piped.stdin_error is None
    assert accents.output == "été"


@pytest.mark.platforms("windows")
def test_windows_script_longer_than_a_command_line(scope: Scope, tmp_path: Path) -> None:
    env = _windows_env(scope, tmp_path)
    long_text = "x" * 40_000  # a Windows command line stops at 32,767 characters
    (result,) = _run(env, f"printf %s '{long_text}'", bounded=False)
    assert result.output == long_text


def _run_each(
    env: LocalWindowsEnvironment, calls: list[tuple[str, str | None]]
) -> list[ExecResult]:
    async def go() -> list[ExecResult]:
        await env.start()
        try:
            return [await env.execute(command, stdin=data) for command, data in calls]
        finally:
            await env.cleanup()

    return asyncio.run(go())
