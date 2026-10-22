# pyright: strict
"""The local backend on native Windows: commands run through Git Bash (P25-24, best effort).

Same contract as the POSIX local backend (``indus.backends.local``) and the same backend name,
``local``; the registry opens this one when Indus itself runs on Windows (D-16.950). WSL2 stays
the supported way to run the agent on a Windows machine (D-16.55); this backend is best effort.

What differs from POSIX:

- **The shell** is the ``bash.exe`` of Git for Windows, found by ``find_git_bash`` (D-16.951). The
  WSL launcher (``System32\\bash.exe``, the WindowsApps alias) is never taken: it would run the
  command in Linux.
- **Paths.** Python, the file tools and ``cwd`` keep native paths (``C:\\Users\\me``); every path
  written into a script is in MSYS form (``/c/Users/me``), and the cwd bash reports is turned back
  into the native form (D-16.952). ``to_shell_path`` / ``to_native_path`` are pure and work on any
  OS. MSYS's own argument conversion is switched off unless the user set it, so native flags like
  ``/F`` reach native programs unchanged.
- **Temp dir**: ``terminal.temp_dir`` when it exists, else ``<home>/cache/terminal`` written with
  forward slashes, which Python and bash both read (D-16.953).
- **Script delivery**: each script is written to a private file in the temp dir and run as
  ``bash [-l] <file>``; a command line is capped at 32,767 characters and MSYS re-parses (and
  globs) the one it is given (D-16.954).
- **Stopping**: there are no process groups or signals for a console-less tree. Each command starts
  suspended, joins its own job object, then resumes; stopping terminates the job, which takes every
  descendant with it, at once (D-16.955). The job is not kill-on-close, so the background jobs of a
  command that ended normally live on, as on POSIX.
- **Encoding**: UTF-8 defaults for child processes and the command's own hidden console switched to
  code page 65001 (D-16.956).
- **Output**: anonymous pipes cannot be ``select``ed, so the reader polls the bytes waiting in the
  pipe (``drain_pipe``) and still stops shortly after the leader exits when a background grandchild
  keeps the pipe open.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import itertools
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any, NoReturn, Protocol, cast

from indus.backends.capture import DEFAULT_MAX_CHARS
from indus.backends.contract import BackendError, BackendUnavailable, ExecResult
from indus.backends.local import (
    LOCAL_FLAGS,
    nearest_usable_dir,
    own_bin_dir,
    prune_temp_dir,
    strip_app_pythonpath,
)
from indus.backends.spawned import SNAPSHOT_TIMEOUT_S, SpawnedEnvironment
from indus.backends.wait import TERMINAL_POOL, ProcessHandle
from indus.backends.wrapper import ScriptPaths
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.platform import known_dirs, locate
from indus.platform.facts import os_family
from indus.platform.shapes import LookupContext
from indus.safety.child_env import child_env, passthrough_names

__all__ = [
    "GIT_BASH_VAR",
    "GitBash",
    "JobApi",
    "LocalWindowsEnvironment",
    "WindowsProcess",
    "drain_pipe",
    "find_git_bash",
    "git_bash_env",
    "open_local_windows",
    "start_windows_process",
    "to_native_path",
    "to_shell_path",
    "windows_path_value",
    "windows_temp_dir",
    "wsl2_refusal",
]

log = logging.getLogger(__name__)

GIT_BASH_VAR = "INDUS_GIT_BASH"
WIN_PATH_SEP = ";"
# Folders whose ``bash.exe`` is the WSL launcher, never Git Bash (compared lower-case, per segment).
LAUNCHER_DIRS = frozenset({"system32", "syswow64", "sysnative", "windowsapps"})
# Git Bash's tool folders below the install root, in the order its login profile puts them (one
# of the first three exists, by architecture; folders that do not exist are left out).
GIT_TOOL_DIRS: tuple[tuple[str, ...], ...] = (
    ("mingw64", "bin"),
    ("clangarm64", "bin"),
    ("mingw32", "bin"),
    ("usr", "local", "bin"),
    ("usr", "bin"),
    ("bin",),
)
# Folders a ``git.exe`` sits in, below the install root.
GIT_EXE_DIRS = frozenset({"cmd", "bin"})
GIT_ARCH_DIRS = frozenset({"mingw64", "mingw32", "clangarm64", "usr"})

# Process creation flags (winbase.h): a new process group, no console window, suspended until it
# has joined its job object.
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
SPAWN_FLAGS = CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
KILLED_EXIT_CODE = 1

DRAIN_POLL_S = 0.02
DRAIN_IDLE_ROUNDS = 15  # ~0.3 s after the leader exits, as on POSIX
READ_CHUNK = 65536
KILL_POLL_S = 0.05
REAP_WAIT_S = 1.0

UTF8_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("PYTHONUTF8", "1"),
    ("PYTHONIOENCODING", "utf-8"),
)
MSYS_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("MSYS_NO_PATHCONV", "1"),
    ("MSYS2_ARG_CONV_EXCL", "*"),
)
UTF8_LOCALE = "C.UTF-8"
# First line of every command script: the command's own (hidden) console speaks UTF-8.
UTF8_CONSOLE = "command -v chcp.com >/dev/null 2>&1 && chcp.com 65001 >/dev/null 2>&1"

# Without O_BINARY the Windows C runtime would turn every LF into CRLF, which bash rejects.
_BINARY: int = getattr(os, "O_BINARY", 0)

_pruned_lock = threading.Lock()
_pruned: set[str] = set()
_warned_no_job = False


# -- paths ----------------------------------------------------------------------------------------

_NATIVE_DRIVE = re.compile(r"^([A-Za-z]):(.*)$", re.DOTALL)
_SHELL_DRIVE = re.compile(r"^/(?:cygdrive/)?([A-Za-z])(?=/|$)(.*)$", re.DOTALL)
_LONG_PREFIXES = ("\\\\?\\", "//?/")


def to_shell_path(path: str) -> str:
    """A native path in the form Git Bash reads: ``C:\\Users\\me`` → ``/c/Users/me``.

    UNC shares become ``//server/share``; the ``\\\\?\\`` long-path prefix is dropped; relative and
    already-MSYS paths only get forward slashes. A drive-relative ``C:name`` is left as it is (it
    means "relative to the cwd on drive C", which MSYS has no form for).
    """
    if not path:
        return path
    text = path
    if text.startswith(_LONG_PREFIXES):
        text = text[4:]
        if text[:4].upper() in ("UNC\\", "UNC/"):
            text = "//" + text[4:]
    text = text.replace("\\", "/")
    found = _NATIVE_DRIVE.match(text)
    if found is not None:
        rest = found.group(2)
        if rest == "" or rest.startswith("/"):
            return "/" + found.group(1).lower() + rest
    return text


def to_native_path(path: str, *, root: str | None = None, temp: str | None = None) -> str:
    """A path bash printed, in native form: ``/c/Users/me`` → ``C:\\Users\\me``.

    ``/cygdrive/c/...`` counts as a drive too; ``//server/share`` becomes a UNC path; ``/tmp`` maps
    to ``temp`` (Git for Windows mounts the user temp dir there) and any other absolute MSYS path
    into the Git install ``root``. Without the root such a path has no native form and comes back
    unchanged. Native input is normalised (upper-case drive, backslashes).
    """
    if not path:
        return path
    found = _NATIVE_DRIVE.match(path)
    if found is not None:
        return found.group(1).upper() + ":" + found.group(2).replace("/", "\\")
    if path.startswith(("//", "\\\\")):
        return path.replace("/", "\\")
    found = _SHELL_DRIVE.match(path)
    if found is not None:
        return found.group(1).upper() + ":" + (found.group(2) or "\\").replace("/", "\\")
    if path.startswith("/"):
        if temp and (path == "/tmp" or path.startswith("/tmp/")):
            base, rest = temp, path[len("/tmp") :]
        elif root:
            base, rest = root, ("" if path == "/" else path)
        else:
            return path
        return base.rstrip("\\/") + rest.replace("/", "\\")
    return path.replace("/", "\\")


def _forward_slashes(path: str) -> str:
    text = path.replace("\\", "/")
    stripped = text.rstrip("/")
    if not stripped:
        return "/"
    if re.fullmatch(r"[A-Za-z]:", stripped):
        return stripped + "/"
    return stripped


def _same_dir(left: str, right: str) -> bool:
    return _forward_slashes(left).lower() == _forward_slashes(right).lower()


# -- temp dir -------------------------------------------------------------------------------------


def windows_temp_dir(configured: str, home: Path) -> str:
    """``terminal.temp_dir`` (an existing absolute dir) → ``<home>/cache/terminal`` (old entries
    pruned once per process) → the system temp dir; always with forward slashes. ``TMP`` and
    ``TEMP`` are not used: they often hold spaces or 8.3 short names."""
    if configured and os.path.isabs(configured) and os.path.isdir(configured):
        return _forward_slashes(configured)
    cache = home / "cache" / "terminal"
    try:
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.access(cache, os.W_OK | os.X_OK):
            _prune_once(cache)
            return _forward_slashes(str(cache))
    except OSError:
        pass
    return _forward_slashes(tempfile.gettempdir())


def _prune_once(directory: Path) -> None:
    key = str(directory)
    with _pruned_lock:
        if key in _pruned:
            return
        _pruned.add(key)
    try:
        prune_temp_dir(directory, now=time.time())
    except OSError:
        log.debug("pruning %s failed", directory, exc_info=True)


# -- finding Git Bash -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitBash:
    """The shell this backend runs: ``bash`` (the ``bash.exe``), the Git install ``root`` it
    belongs to (None when unknown), its existing tool folders, and where it was found."""

    bash: str
    root: str | None
    tool_dirs: tuple[str, ...]
    source: str


def _env_get(env: Mapping[str, str], name: str) -> str:
    """``env[name]`` with Windows' case-insensitive names ("Path" and "PATH" are one)."""
    value = env.get(name)
    if value is not None:
        return value
    wanted = name.upper()
    return next((v for k, v in env.items() if k.upper() == wanted), "")


def _segments(path: str) -> list[str]:
    return [part for part in re.split(r"[\\/]+", path) if part]


def _is_launcher(path: str) -> bool:
    return any(part.lower() in LAUNCHER_DIRS for part in _segments(path)[:-1])


def _parent(path: str) -> str:
    """The parent folder of ``path`` under either separator (OS-independent)."""
    trimmed = path.rstrip("\\/")
    cut = max(trimmed.rfind("/"), trimmed.rfind("\\"))
    return trimmed[:cut] if cut > 0 else trimmed


def _name(path: str) -> str:
    trimmed = path.rstrip("\\/")
    return trimmed[max(trimmed.rfind("/"), trimmed.rfind("\\")) + 1 :].lower()


def _root_of_bash(bash: str) -> str | None:
    """``<root>\\bin\\bash.exe`` or ``<root>\\usr\\bin\\bash.exe`` → ``<root>``."""
    folder = _parent(bash)
    if _name(folder) != "bin":
        return None
    above = _parent(folder)
    return _parent(above) if _name(above) == "usr" else above


def _root_of_git(git: str) -> str | None:
    """``<root>\\cmd\\git.exe``, ``<root>\\bin\\git.exe`` or ``<root>\\mingw64\\bin\\git.exe`` →
    ``<root>``."""
    folder = _parent(git)
    if _name(folder) not in GIT_EXE_DIRS:
        return None
    above = _parent(folder)
    return _parent(above) if _name(above) in GIT_ARCH_DIRS else above


def _bash_under(root: str) -> str | None:
    for parts in (("bin", "bash.exe"), ("usr", "bin", "bash.exe")):
        candidate = os.path.join(root, *parts)
        if os.path.isfile(candidate):
            return candidate
    return None


def _tool_dirs(root: str | None) -> tuple[str, ...]:
    if root is None:
        return ()
    found = (os.path.join(root, *parts) for parts in GIT_TOOL_DIRS)
    return tuple(d for d in found if os.path.isdir(d))


def _record(bash: str, source: str, root: str | None = None) -> GitBash:
    base = root if root is not None else _root_of_bash(bash)
    return GitBash(bash=bash, root=base, tool_dirs=_tool_dirs(base), source=source)


def _version_key(folder: Path) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", folder.name))


def _portable_roots(home: Path) -> list[str]:
    """The installer's portable Git copies, ``<home>/tools/git-<version>``, newest first."""
    try:
        found = [p for p in (home / "tools").glob("git-*") if p.is_dir()]
    except OSError:
        return []
    return [str(p) for p in sorted(found, key=_version_key, reverse=True)]


def _on_path(
    name: str, env: Mapping[str, str], family: str, user_home: Callable[[], Path] | None
) -> list[str]:
    """Every ``name`` on the PATH, in PATH order (the platform lookup stops at the first, and the
    first may be the WSL launcher or a MinGit without bash)."""
    found: list[str] = []
    for folder in _env_get(env, "PATH").split(os.pathsep):
        if not folder:
            continue
        hit = locate.locate(
            name, LookupContext(path=folder), known=(), env=env, family=family, user_home=user_home
        )
        found += [c.value for c in hit.present if c.value not in found]
    return found


def find_git_bash(
    env: Mapping[str, str],
    *,
    home: Path,
    family: str | None = None,
    user_home: Callable[[], Path] | None = None,
) -> GitBash:
    """Git Bash for this machine (D-16.951), in this order:

    ``INDUS_GIT_BASH`` (an explicit file; refused when missing) → the installer's portable Git in
    ``<home>/tools`` → the Git install that ``git.exe`` on PATH belongs to → the usual Git for
    Windows folders (``known_dirs.git_for_windows_roots``) → any ``bash.exe`` on PATH that is not
    the WSL launcher. Raises ``BackendUnavailable`` when there is none.
    """
    fam = family if family is not None else os_family()
    explicit = _env_get(env, GIT_BASH_VAR).strip()
    if explicit:
        if os.path.isfile(explicit):
            return _record(explicit, "override")
        raise BackendUnavailable(
            f"{GIT_BASH_VAR} names {explicit}, which is not a file",
            hint=f"point {GIT_BASH_VAR} at the bash.exe of Git for Windows, or unset it",
        )
    for root in _portable_roots(home):
        bash = _bash_under(root)
        if bash is not None:
            return _record(bash, "portable", root)
    for git in _on_path("git.exe", env, fam, user_home):
        root = _root_of_git(git)
        bash = _bash_under(root) if root is not None else None
        if root is not None and bash is not None:
            return _record(bash, "git", root)
    home_fn = user_home if user_home is not None else Path.home
    for entry in known_dirs.git_for_windows_roots(fam):
        expanded = known_dirs.expand(entry, env, home_fn)
        bash = _bash_under(str(expanded)) if expanded is not None else None
        if expanded is not None and bash is not None:
            return _record(bash, "known_dir", str(expanded))
    for bash in _on_path("bash.exe", env, fam, user_home):
        if not _is_launcher(bash):
            return _record(bash, "PATH")
    raise BackendUnavailable(
        "Git Bash was not found; the local terminal backend on Windows needs Git for Windows",
        hint=(
            "install Git for Windows (the PowerShell installer sets up a portable copy), or set "
            f"{GIT_BASH_VAR} to its bash.exe; for a fully supported setup run Indus Agent in WSL2"
        ),
    )


# -- the child environment ------------------------------------------------------------------------


def windows_path_value(value: str, *, front: tuple[str, ...], own_bin: str | None) -> str:
    """A Windows PATH value: ``front`` (Git Bash's tool folders) first, Indus's bin folder next
    when it is not listed, then the rest in the user's order; empty and repeated entries go
    (compared without case or trailing separators)."""
    entries = [e for e in value.split(WIN_PATH_SEP) if e]
    head = list(front)
    if own_bin and not any(_same_dir(own_bin, e) for e in entries):
        head.append(own_bin)
    result: list[str] = []
    for entry in [*head, *entries]:
        if not any(_same_dir(entry, kept) for kept in result):
            result.append(entry)
    return WIN_PATH_SEP.join(result)


def _path_key(env: Mapping[str, str]) -> str:
    return next((k for k in env if k.upper() == "PATH"), "PATH")


def git_bash_env(env: Mapping[str, str], bash: GitBash) -> dict[str, str]:
    """``env`` made ready for a Git Bash child: its tool folders first on PATH (D-16.957), the
    UTF-8 and MSYS defaults and a UTF-8 ``LANG`` unless already set (D-16.956)."""
    out = dict(env)
    key = _path_key(out)
    out[key] = windows_path_value(out.get(key, ""), front=bash.tool_dirs, own_bin=own_bin_dir())
    for name, value in (*UTF8_DEFAULTS, *MSYS_DEFAULTS):
        if not _env_get(out, name):
            out[name] = value
    if not (_env_get(out, "LANG") or _env_get(out, "LC_ALL")):
        out["LANG"] = UTF8_LOCALE
    return out


def wsl2_refusal(what: str) -> str:
    """The message for a feature native Windows does not have yet (RL26, D-16.55)."""
    return (
        f"{what} is not available on native Windows yet; run Indus Agent inside WSL2, the "
        "supported way to use it on Windows"
    )


# -- the process handle ---------------------------------------------------------------------------


class JobApi(Protocol):
    """The few Win32 calls the process handle needs (a fake stands in for them in tests)."""

    def open_process(self, pid: int) -> int | None: ...

    def create_job(self) -> int | None: ...

    def assign(self, job: int, process: int) -> bool: ...

    def resume(self, process: int) -> bool: ...

    def terminate_job(self, job: int, code: int) -> bool: ...

    def active_processes(self, job: int) -> int | None: ...

    def close(self, handle: int) -> None: ...

    def pipe_available(self, fd: int) -> int | None:
        """Bytes waiting in the pipe; None once every writer closed it and it is empty."""
        ...


def drain_pipe(
    fd: int,
    *,
    available: Callable[[], int | None],
    exited: Callable[[], bool],
    sink: Callable[[str], None],
    stop: threading.Event,
    poll_s: float = DRAIN_POLL_S,
    idle_rounds: int = DRAIN_IDLE_ROUNDS,
) -> None:
    """Feed what arrives on ``fd`` to ``sink`` (UTF-8, bad bytes replaced) until the pipe is
    closed and empty, ``stop`` is set, or the process has ``exited`` and nothing came for
    ``idle_rounds`` polls (a background grandchild may hold the pipe for ever)."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    idle = 0
    try:
        while not stop.is_set():
            waiting = available()
            if waiting is None:
                break
            if waiting > 0:
                try:
                    chunk = os.read(fd, min(waiting, READ_CHUNK))
                except OSError:
                    break
                if not chunk:
                    break
                sink(decoder.decode(chunk))
                idle = 0
                continue
            if exited():
                idle += 1
                if idle >= idle_rounds:
                    break
            stop.wait(poll_s)
    finally:
        tail = decoder.decode(b"", final=True)
        if tail:
            sink(tail)


def _write_stdin(pipe: IO[bytes], data: str) -> str | None:
    """Write ``data`` and close the pipe (always, so the child sees EOF); an encode failure is
    returned as text."""
    try:
        raw = data.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError as exc:
        with contextlib.suppress(OSError):
            pipe.close()
        return f"stdin is not valid text ({exc.reason} at position {exc.start})"
    try:
        pipe.write(raw)
    except OSError:
        pass  # the child closed its stdin early: normal
    finally:
        with contextlib.suppress(OSError):
            pipe.close()
    return None


class WindowsProcess:
    """A Git Bash child in its own job object (meets ``wait.ProcessHandle``)."""

    def __init__(
        self, popen: subprocess.Popen[bytes], api: JobApi, *, script_path: str | None = None
    ) -> None:
        self.popen = popen
        self.api = api
        self.job: int | None = None
        self.script_path = script_path
        self._lock = threading.Lock()
        self._stdin_task: asyncio.Future[str | None] | None = None

    @property
    def pid(self) -> int | None:
        return self.popen.pid

    def poll(self) -> int | None:
        return self.popen.poll()

    @property
    def stdin_task(self) -> asyncio.Future[str | None] | None:
        return self._stdin_task

    def attach_and_resume(self) -> None:
        """Put the suspended child into a new job object, then let it run. Without a job (the
        host refuses nesting) only the leader can be stopped later; a child that cannot be
        resumed is killed and the spawn fails."""
        global _warned_no_job
        process = self.api.open_process(self.popen.pid)
        if process is None:
            self._abandon("the new process could not be opened")
        try:
            job = self.api.create_job()
            if job is not None and self.api.assign(job, process):
                self.job = job
            else:
                if job is not None:
                    self.api.close(job)
                if not _warned_no_job:
                    _warned_no_job = True
                    log.warning(
                        "no job object for Git Bash children: a stop reaches the leader only"
                    )
            if not self.api.resume(process):
                self._abandon("the new process could not be resumed")
        finally:
            self.api.close(process)

    def _abandon(self, reason: str) -> NoReturn:
        self.terminate_tree()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            self.popen.wait(timeout=REAP_WAIT_S)
        self.release()
        raise BackendError(f"Git Bash could not be started: {reason}")

    def start_stdin(self, data: str) -> None:
        pipe = self.popen.stdin
        if pipe is None:
            return
        task: asyncio.Future[str | None] = asyncio.ensure_future(
            run_blocking(_write_stdin, pipe, data, pool=TERMINAL_POOL, label="stdin")
        )

        def closed_on_failure(done: asyncio.Future[str | None]) -> None:
            if done.cancelled() or done.exception() is not None:
                with contextlib.suppress(OSError):
                    pipe.close()

        task.add_done_callback(closed_on_failure)
        self._stdin_task = task

    def drain(self, sink: Callable[[str], None], stop: threading.Event) -> None:
        stream = self.popen.stdout
        if stream is None:
            return
        try:
            fd = stream.fileno()
            drain_pipe(
                fd,
                available=lambda: self.api.pipe_available(fd),
                exited=lambda: self.popen.poll() is not None,
                sink=sink,
                stop=stop,
            )
        except (OSError, ValueError):
            log.debug("reading Git Bash output failed", exc_info=True)
        finally:
            with contextlib.suppress(OSError):
                stream.close()
            if self.popen.poll() is not None:
                self.release()

    def terminate_tree(self) -> None:
        """Stop the whole tree now: terminate the job, else kill the leader."""
        with self._lock:
            job = self.job
        if job is not None and self.api.terminate_job(job, KILLED_EXIT_CODE):
            return
        if self.popen.poll() is None:
            with contextlib.suppress(OSError):
                self.popen.kill()

    def tree_running(self) -> bool:
        """Whether any process of the tree still runs (the leader alone without a job)."""
        with self._lock:
            job = self.job
        if job is not None:
            count = self.api.active_processes(job)
            if count is not None:
                return count > 0
        return self.popen.poll() is None

    def release(self) -> None:
        """Close the job handle (the tree is not killed by it) and remove the script file once
        the leader has exited. Safe to call more than once."""
        with self._lock:
            job, self.job = self.job, None
        if job is not None:
            self.api.close(job)
        script = self.script_path
        if script is not None and self.popen.poll() is not None:
            self.script_path = None
            with contextlib.suppress(OSError):
                os.unlink(script)


def _platform_api() -> JobApi:
    if sys.platform != "win32":
        raise BackendUnavailable("the Git Bash backend runs only on Windows")
    return _Win32Jobs()


if sys.platform == "win32":  # the Win32 calls; pyright and the tests on other OSes skip this
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _PROCESS_ACCESS = 0x0001 | 0x0100 | 0x0800 | 0x1000  # terminate, set quota, resume, query
    _JOB_BASIC_ACCOUNTING = 1

    class _Accounting(ctypes.Structure):
        _fields_ = [  # noqa: RUF012 - the ctypes layout
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class _Win32Jobs:
        def __init__(self) -> None:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            nt = ctypes.WinDLL("ntdll")
            self._open = k32.OpenProcess
            self._open.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self._open.restype = wintypes.HANDLE
            self._create = k32.CreateJobObjectW
            self._create.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            self._create.restype = wintypes.HANDLE
            self._assign = k32.AssignProcessToJobObject
            self._assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self._assign.restype = wintypes.BOOL
            self._terminate = k32.TerminateJobObject
            self._terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self._terminate.restype = wintypes.BOOL
            self._query = k32.QueryInformationJobObject
            self._query.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ]
            self._query.restype = wintypes.BOOL
            self._close = k32.CloseHandle
            self._close.argtypes = [wintypes.HANDLE]
            self._close.restype = wintypes.BOOL
            self._peek = k32.PeekNamedPipe
            self._peek.argtypes = [
                wintypes.HANDLE,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
            ]
            self._peek.restype = wintypes.BOOL
            self._resume = nt.NtResumeProcess
            self._resume.argtypes = [wintypes.HANDLE]
            self._resume.restype = ctypes.c_long

        def open_process(self, pid: int) -> int | None:
            return self._open(_PROCESS_ACCESS, False, pid) or None

        def create_job(self) -> int | None:
            return self._create(None, None) or None

        def assign(self, job: int, process: int) -> bool:
            return bool(self._assign(job, process))

        def resume(self, process: int) -> bool:
            return self._resume(process) == 0

        def terminate_job(self, job: int, code: int) -> bool:
            return bool(self._terminate(job, code))

        def active_processes(self, job: int) -> int | None:
            info = _Accounting()
            ok = self._query(
                job, _JOB_BASIC_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None
            )
            return int(info.ActiveProcesses) if ok else None

        def close(self, handle: int) -> None:
            self._close(handle)

        def pipe_available(self, fd: int) -> int | None:
            waiting = wintypes.DWORD(0)
            handle = msvcrt.get_osfhandle(fd)
            ok = self._peek(handle, None, 0, None, ctypes.byref(waiting), None)
            return int(waiting.value) if ok else None


def start_windows_process(
    argv: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    stdin: str | None,
    script_path: str | None = None,
    api: JobApi | None = None,
    separate_stderr: bool = False,
) -> WindowsProcess:
    """Start ``argv`` suspended, in a new process group with no console window, put it into its
    own job object and let it run (see ``WindowsProcess.attach_and_resume``). stderr joins stdout
    unless ``separate_stderr`` (then the caller reads ``popen.stderr`` itself)."""
    jobs = api if api is not None else _platform_api()
    popen = subprocess.Popen(  # noqa: S603 - the command is the user's, by design
        argv,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if separate_stderr else subprocess.STDOUT,
        cwd=cwd,
        env=dict(env),
        creationflags=SPAWN_FLAGS,
        close_fds=True,
    )
    proc = WindowsProcess(popen, jobs, script_path=script_path)
    proc.attach_and_resume()
    if stdin is not None:
        proc.start_stdin(stdin)
    return proc


# -- the backend ----------------------------------------------------------------------------------


def _setting(scope: Scope, key: str) -> Any:
    value = scope.config.get(key) if scope.config is not None else None
    return default_for(key) if value is None else value


class LocalWindowsEnvironment(SpawnedEnvironment):
    """Commands on this Windows machine through Git Bash (see the module notes)."""

    backend_name = "local"

    def __init__(
        self,
        scope: Scope,
        *,
        bash: GitBash,
        cwd: str,
        timeout: float,
        temp_dir: str,
        persist_env: bool = True,
        session_id: str | None = None,
        env_overrides: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        snapshot_timeout: float = SNAPSHOT_TIMEOUT_S,
        api: JobApi | None = None,
    ) -> None:
        super().__init__(
            cwd=cwd,
            timeout=timeout,
            temp_dir=temp_dir,
            flags=LOCAL_FLAGS,
            persist_env=persist_env,
            clock=clock,
            capture_home=scope.home,
            max_chars=max_chars,
            snapshot_timeout=snapshot_timeout,
        )
        self.scope = scope
        self.bash = bash
        self.temp_dir = temp_dir.rstrip("/") or "/"
        self.session_id = session_id
        self.env_overrides = dict(env_overrides or {})
        self.api = api
        self._script_numbers = itertools.count(1)
        # Python opens the snapshot by its forward-slash native path; bash gets the MSYS form.
        shell_snapshot = to_shell_path(self.snapshot_path)
        self.paths = ScriptPaths(
            snapshot=shlex.quote(shell_snapshot),
            snapshot_tmp_template=shlex.quote(shell_snapshot + ".tmp.XXXXXXXXXX"),
            marker=self.paths.marker,
        )

    def describe(self) -> str:
        return f"local Git Bash on this Windows machine (best effort), cwd {self._cwd}"

    # -- paths for callers ------------------------------------------------------------------------

    def shell_path(self, path: str) -> str:
        """``path`` (native) as a Git Bash argument."""
        return to_shell_path(path)

    def native_path(self, path: str) -> str:
        """``path`` (as bash printed it) in native form."""
        temp = _env_get(self.scope.env, "TEMP") or tempfile.gettempdir()
        return to_native_path(path, root=self.bash.root, temp=temp)

    # -- environment ------------------------------------------------------------------------------

    def _excluded_names(self) -> tuple[str, ...]:
        accepted, _ = passthrough_names(self.scope)
        return tuple(accepted)

    def run_env(self) -> dict[str, str]:
        """The environment one command starts with (D-16.956, D-16.957)."""
        env = child_env(self.scope, session_id=self.session_id)
        env.update(self.env_overrides)
        env = git_bash_env(env, self.bash)
        python_path = _env_get(env, "PYTHONPATH")
        for name in [k for k in env if k.upper() == "PYTHONPATH"]:
            del env[name]
        stripped = strip_app_pythonpath(python_path) if python_path else ""
        if stripped:
            env["PYTHONPATH"] = stripped
        venv = env.get("VIRTUAL_ENV")
        if venv and sys.prefix != sys.base_prefix:
            with contextlib.suppress(OSError):
                if Path(venv).resolve() == Path(sys.prefix).resolve():
                    del env["VIRTUAL_ENV"]
        return env

    # -- scripts and the cwd ----------------------------------------------------------------------

    def _wrap(self, command: str, cwd: str) -> str:
        target = cwd if cwd.startswith("~") else to_shell_path(cwd)
        return UTF8_CONSOLE + "\n" + super()._wrap(command, target)

    def _refresh_cwd(self) -> None:
        usable = nearest_usable_dir(self._cwd)
        if usable != self._cwd:
            log.warning("cwd %s is gone or not enterable; using %s", self._cwd, usable)
            self._cwd = usable

    def _accept_cwd(self, path: str) -> bool:
        return os.path.isdir(self.native_path(path))

    def _take_cwd(self, result: ExecResult, *, adopt: bool = True) -> ExecResult:
        taken = super()._take_cwd(result, adopt=False)
        if taken.cwd_observed is None:
            return taken
        native = self.native_path(taken.cwd_observed)
        if adopt:
            self._cwd = native
        return replace(taken, cwd_observed=native)

    def _write_script(self, script: str) -> str:
        path = f"{self.temp_dir}/indus-cmd-{self.env_id}-{next(self._script_numbers)}.sh"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _BINARY, 0o600)
        with open(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        return path

    # -- processes --------------------------------------------------------------------------------

    async def _spawn(self, script: str, *, login: bool, stdin: str | None) -> ProcessHandle:
        env = self.run_env()
        path = self._write_script(script)
        argv = [self.bash.bash, *(["-l"] if login else []), to_shell_path(path)]
        try:
            return start_windows_process(
                argv,
                cwd=nearest_usable_dir(self._cwd),
                env=env,
                stdin=stdin,
                script_path=path,
                api=self.api,
            )
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise

    async def _kill(self, handle: ProcessHandle, within: float | None = None) -> None:
        cast(WindowsProcess, handle).terminate_tree()
        await self._await_killed(handle, REAP_WAIT_S if within is None else max(within, 0.0))

    def _force_kill(self, handle: ProcessHandle) -> None:
        cast(WindowsProcess, handle).terminate_tree()

    async def _await_killed(self, handle: ProcessHandle, within: float) -> None:
        proc = cast(WindowsProcess, handle)
        until = self.clock.monotonic() + within
        while (proc.poll() is None or proc.tree_running()) and self.clock.monotonic() < until:
            await self.clock.sleep(KILL_POLL_S)
        if proc.poll() is not None:
            proc.release()

    def _remove_files(self) -> None:
        snapshot = Path(self.snapshot_path)
        leftovers = [
            *snapshot.parent.glob(snapshot.name + ".tmp.*"),
            *snapshot.parent.glob(f"indus-cmd-{self.env_id}-*.sh"),
        ]
        for path in [snapshot, *leftovers]:
            with contextlib.suppress(OSError):
                path.unlink()


async def open_local_windows(
    scope: Scope,
    *,
    session_id: str | None = None,
    cwd: str | None = None,
    timeout: float | None = None,
    clock: Clock | None = None,
    env_overrides: Mapping[str, str] | None = None,
    bash: GitBash | None = None,
    api: JobApi | None = None,
) -> LocalWindowsEnvironment:
    """A started Git Bash environment configured from ``scope`` (``terminal.*``,
    ``tool_output.max_chars``); arguments override the config. No sudo support on Windows."""
    shell = bash if bash is not None else find_git_bash(scope.env, home=scope.home)
    raw_cwd = str(cwd if cwd is not None else _setting(scope, "terminal.cwd") or ".")
    user_home = _env_get(scope.env, "USERPROFILE") or _env_get(scope.env, "HOME")
    home_dir = user_home or str(Path.home())
    if raw_cwd == "~" or raw_cwd.startswith(("~/", "~\\")):
        raw_cwd = home_dir + raw_cwd[1:]
    elif raw_cwd.startswith("/"):
        raw_cwd = to_native_path(raw_cwd, root=shell.root)
    start_dir = os.path.abspath(raw_cwd)
    env = LocalWindowsEnvironment(
        scope,
        bash=shell,
        cwd=nearest_usable_dir(start_dir),
        timeout=float(timeout if timeout is not None else _setting(scope, "terminal.timeout")),
        temp_dir=windows_temp_dir(str(_setting(scope, "terminal.temp_dir") or ""), scope.home),
        persist_env=bool(_setting(scope, "terminal.persistent_shell")),
        session_id=session_id,
        env_overrides=env_overrides,
        clock=clock,
        max_chars=int(_setting(scope, "tool_output.max_chars")),
        api=api,
    )
    await env.start()
    return env
