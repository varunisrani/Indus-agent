# pyright: strict
"""The local backend: commands run on this machine with bash (P05-03).

Every command is a fresh ``bash -c`` (``bash -l -c`` while the snapshot is missing) in a new
session and process group, stdout and stderr merged. Its environment comes from the one child
environment builder (``indus.safety.child_env``), then gets a usable PATH (missing system and
user-local dirs appended, Indus's own bin dir prepended when missing) and loses the PYTHONPATH
entries that point into Indus's own install, which would break a child Python of another version.

Stopping a command (R02.12): the descendants are listed first (they move to init once their
parent dies), the leader gets SIGTERM and up to ``terminal.daemon_term_grace_s`` to finish, then
the survivors — its group and listed descendants that left it — get SIGTERM, and whatever is
still alive at the end of the grace gets SIGKILL. A group is only signalled while its leader is
still the process we started (same start time) or gone: a recycled PID/PGID is never hit.
Process facts come from ``/proc``; without it (macOS) the group signals are best effort.

Windows is out of scope (WSL2, D-16.55).
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import functools
import logging
import os
import select
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

from indus.backends.capture import DEFAULT_MAX_CHARS
from indus.backends.contract import BackendFlags, BackendUnavailable
from indus.backends.spawned import SNAPSHOT_TIMEOUT_S, SpawnedEnvironment
from indus.backends.sudo import SudoSupport
from indus.backends.wait import TERMINAL_POOL, ProcessHandle
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.platform import known_dirs, locate
from indus.safety.child_env import child_env, passthrough_names

__all__ = [
    "LocalEnvironment",
    "LocalProcess",
    "choose_temp_dir",
    "complete_path",
    "descendants",
    "force_kill_tree",
    "open_local",
    "process_start_time",
    "stop_tree",
    "strip_app_pythonpath",
]

log = logging.getLogger(__name__)

# Usual command dirs (bin before sbin, local before system, Homebrew last), appended to a PATH
# that lacks them: a service manager or cron often starts us with a bare PATH.
SANE_PATH: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *(f"{root}/{leaf}" for leaf in ("bin", "sbin") for root in ("/usr/local", "/usr", "")),
            *known_dirs.homebrew_bins("darwin"),
        )
    )
)
TEMP_IDLE_S = 24 * 3600
KILL_POLL_S = 0.05
SURVIVOR_WINDOW_S = 0.2
REAP_WAIT_S = 1.0
DRAIN_SELECT_S = 0.1
DRAIN_IDLE_ROUNDS = 3  # stop ~0.3 s after the leader exits, even if a grandchild holds the pipe

LOCAL_FLAGS = BackendFlags(
    isolated=False, container_paths=False, stdin_mode="pipe", host_local=True
)

_pruned_lock = threading.Lock()
_pruned: set[str] = set()


# -- process facts (/proc) ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProcInfo:
    pid: int
    ppid: int
    pgrp: int
    start: int  # clock ticks since boot: with the pid, the process's identity
    state: str = "R"

    @property
    def zombie(self) -> bool:
        return self.state in ("Z", "X")


def _read_stat(pid: int) -> ProcInfo | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    # The command name sits in parentheses and may contain spaces or ')'.
    fields = raw.rsplit(")", 1)[-1].split()
    try:
        return ProcInfo(pid, int(fields[1]), int(fields[2]), int(fields[19]), fields[0])
    except (IndexError, ValueError):
        return None


def process_start_time(pid: int) -> int | None:
    """Start time of ``pid`` (None when it does not exist or cannot be read)."""
    info = _read_stat(pid)
    return None if info is None else info.start


def _all_processes() -> list[ProcInfo] | None:
    """Every process ``/proc`` lists (None without ``/proc``)."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    found: list[ProcInfo] = []
    for name in names:
        if name.isdigit():
            info = _read_stat(int(name))
            if info is not None:
                found.append(info)
    return found


def descendants(pid: int) -> list[ProcInfo]:
    """Every descendant of ``pid`` right now (empty without ``/proc``)."""
    children: dict[int, list[ProcInfo]] = {}
    for info in _all_processes() or []:
        children.setdefault(info.ppid, []).append(info)
    found: list[ProcInfo] = []
    todo = [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child.pid != pid and all(f.pid != child.pid for f in found):
                found.append(child)
                todo.append(child.pid)
    return found


def _still_same(info: ProcInfo) -> bool:
    """True while ``info.pid`` is the very process the snapshot saw (a zombie included)."""
    return process_start_time(info.pid) == info.start


def _running(info: ProcInfo) -> bool:
    """True while the snapshot's process is still running (not gone, recycled or a zombie)."""
    now = _read_stat(info.pid)
    return now is not None and now.start == info.start and not now.zombie


def _group_ours(pgid: int | None, leader_start: int | None) -> bool:
    """May group ``pgid`` be signalled? Never our own group or init's; never a group whose
    leader slot holds a different process (recycled). A leader that is gone is fine: the kernel
    does not reuse a PGID while any member lives."""
    if pgid is None or pgid <= 1 or pgid == os.getpgrp():
        return False
    current = process_start_time(pgid)
    if current is None or leader_start is None:
        return True
    return current == leader_start


def _signal_group(pgid: int | None, leader_start: int | None, sig: int) -> None:
    if pgid is None or not _group_ours(pgid, leader_start):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _group_alive(pgid: int | None, leader_start: int | None) -> bool:
    """Does group ``pgid`` still have a running member? Zombies do not count: where PID 1 does
    not reap orphans (some containers) they would keep a dead group "alive"."""
    if pgid is None or not _group_ours(pgid, leader_start):
        return False
    table = _all_processes()
    if table is not None:
        return any(info.pgrp == pgid and not info.zombie for info in table)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_known(info: ProcInfo, sig: int) -> None:
    if _still_same(info):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(info.pid, sig)


# -- the process handle ---------------------------------------------------------------------------


class LocalProcess:
    """A bash child started by the local backend (meets ``wait.ProcessHandle``)."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self.popen = popen
        self.pgid: int | None
        try:
            self.pgid = os.getpgid(popen.pid)
        except ProcessLookupError:
            self.pgid = popen.pid  # start_new_session made it the leader of its own group
        self.leader_start = process_start_time(popen.pid)
        self._stdin_task: asyncio.Future[str | None] | None = None

    @property
    def pid(self) -> int | None:
        return self.popen.pid

    def poll(self) -> int | None:
        return self.popen.poll()

    @property
    def stdin_task(self) -> asyncio.Future[str | None] | None:
        return self._stdin_task

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
                    pipe.close()  # the child must still see EOF

        task.add_done_callback(closed_on_failure)
        self._stdin_task = task

    def drain(self, sink: Callable[[str], None], stop: threading.Event) -> None:
        stream = self.popen.stdout
        if stream is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        idle_rounds = 0
        try:
            fd = stream.fileno()
            while not stop.is_set():
                try:
                    ready, _, _ = select.select([fd], [], [], DRAIN_SELECT_S)
                except (OSError, ValueError):
                    break
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break  # every writer closed the pipe
                    sink(decoder.decode(chunk))
                    idle_rounds = 0
                elif self.popen.poll() is not None:
                    idle_rounds += 1
                    if idle_rounds >= DRAIN_IDLE_ROUNDS:
                        break  # a background grandchild may keep the pipe open for ever
        finally:
            tail = decoder.decode(b"", final=True)
            if tail:
                sink(tail)
            with contextlib.suppress(OSError):
                stream.close()


def _write_stdin(pipe: IO[bytes], data: str) -> str | None:
    """Write ``data`` and close the pipe; an encode failure is returned as text (stdin is still
    closed, so the child sees EOF instead of waiting for ever)."""
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


# -- stopping trees -------------------------------------------------------------------------------


async def stop_tree(proc: LocalProcess, *, clock: Clock, grace: float) -> None:
    """The ordered kill described in the module notes; returns once everything is gone or
    SIGKILLed. Never raises for processes that already exited."""
    tree = descendants(proc.popen.pid)
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.popen.send_signal(signal.SIGTERM)
    deadline = clock.monotonic() + max(0.0, grace)
    while proc.poll() is None and clock.monotonic() < deadline:
        await clock.sleep(KILL_POLL_S)

    _signal_group(proc.pgid, proc.leader_start, signal.SIGTERM)
    group_reached = _group_ours(proc.pgid, proc.leader_start)
    for info in tree:  # those the group signal did not reach: escapees, or every one of them
        if not group_reached or info.pgrp != proc.pgid:
            _signal_known(info, signal.SIGTERM)

    def anything_left() -> bool:
        return (
            proc.poll() is None
            or _group_alive(proc.pgid, proc.leader_start)
            or any(_running(info) for info in tree)
        )

    until = max(deadline, clock.monotonic() + SURVIVOR_WINDOW_S)
    while anything_left() and clock.monotonic() < until:
        await clock.sleep(KILL_POLL_S)
    if anything_left():
        force_kill_tree(proc, tree)
        until = clock.monotonic() + REAP_WAIT_S
        while proc.poll() is None and clock.monotonic() < until:
            await clock.sleep(KILL_POLL_S)


def force_kill_tree(proc: LocalProcess, tree: list[ProcInfo] | None = None) -> None:
    """SIGKILL the group (same recycled-PID guard), the listed descendants and the leader; no
    waiting (hard exit and the backstop use this)."""
    listed = descendants(proc.popen.pid) if tree is None else tree
    _signal_group(proc.pgid, proc.leader_start, signal.SIGKILL)
    for info in listed:
        _signal_known(info, signal.SIGKILL)
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.popen.kill()


# -- environment assembly -------------------------------------------------------------------------


def choose_temp_dir(configured: str, env: Mapping[str, str], home: Path) -> str:
    """``terminal.temp_dir`` (when it is an existing dir) → ``TMPDIR``/``TMP``/``TEMP`` (absolute)
    → ``<home>/cache/terminal`` (old entries pruned once per process) → the system temp dir."""
    if configured and os.path.isabs(configured) and os.path.isdir(configured):
        return configured.rstrip("/") or "/"
    for name in ("TMPDIR", "TMP", "TEMP"):
        value = env.get(name, "")
        if value.startswith("/"):
            return value.rstrip("/") or "/"
    cache = home / "cache" / "terminal"
    try:
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.access(cache, os.W_OK | os.X_OK):
            _prune_temp_once(cache)
            return str(cache)
    except OSError:
        pass
    return tempfile.gettempdir().rstrip("/") or "/"


def _newest_mtime(path: Path) -> float:
    newest = path.lstat().st_mtime
    if path.is_dir() and not path.is_symlink():
        for root, dirs, files in os.walk(path):
            for name in [*dirs, *files]:
                with contextlib.suppress(OSError):
                    newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
    return newest


def prune_temp_dir(directory: Path, *, now: float, idle_s: float = TEMP_IDLE_S) -> int:
    """Remove entries of ``directory`` not written to for ``idle_s``; returns how many went."""
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if now - _newest_mtime(entry) <= idle_s:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _prune_temp_once(directory: Path) -> None:
    key = str(directory)
    with _pruned_lock:
        if key in _pruned:
            return
        _pruned.add(key)
    try:
        prune_temp_dir(directory, now=time.time())
    except OSError:
        log.debug("pruning %s failed", directory, exc_info=True)


@functools.cache
def own_bin_dir() -> str | None:
    """The directory holding the ``indus`` command, when there is one."""
    beside = Path(sys.executable).parent
    if (beside / "indus").is_file():
        return str(beside)
    found = locate.which("indus")
    return os.path.dirname(found) if found else None


def complete_path(path: str, *, user_home: str | None, own_bin: str | None) -> str:
    """``path`` without empty or repeated entries, missing system dirs and the user's local bin dir
    appended (the user's order wins), Indus's bin dir prepended when it is not listed."""
    entries = list(dict.fromkeys(p for p in path.split(":") if p))
    extra = list(SANE_PATH)
    if user_home:
        local_bin = os.path.join(user_home, ".local", "bin")
        with contextlib.suppress(OSError):
            if os.path.isdir(local_bin):
                extra.append(local_bin)
    entries += [p for p in extra if p not in entries]
    if own_bin and own_bin not in entries:
        entries.insert(0, own_bin)
    return ":".join(entries)


@functools.cache
def _app_owned_roots() -> frozenset[str]:
    import indus

    roots = {str(Path(indus.__file__).resolve().parent.parent)}
    if sys.prefix != sys.base_prefix:  # running from a virtual environment
        paths = sysconfig.get_paths()
        for key in ("purelib", "platlib"):
            with contextlib.suppress(KeyError, OSError):
                roots.add(str(Path(paths[key]).resolve()))
    return frozenset(roots)


def strip_app_pythonpath(value: str, roots: frozenset[str] | None = None) -> str:
    """``value`` without the entries that point into Indus's own install."""
    owned = _app_owned_roots() if roots is None else roots
    kept: list[str] = []
    for entry in value.split(os.pathsep):
        if not entry:
            continue
        try:
            resolved = str(Path(entry).resolve())
        except OSError:
            resolved = entry
        if resolved not in owned:
            kept.append(entry)
    return os.pathsep.join(kept)


def _usable_dir(path: str) -> bool:
    return os.path.isdir(path) and os.access(path, os.X_OK)


def nearest_usable_dir(path: str) -> str:
    """``path`` when it can be entered, else its nearest enterable parent, else the temp dir."""
    if path and _usable_dir(path):
        return path
    current: str = path
    while current:
        parent: str = os.path.dirname(current)
        if parent == current:
            break
        current = parent
        if _usable_dir(current):
            return current
    return tempfile.gettempdir()


def _config(scope: Scope, key: str) -> Any:
    value = scope.config.get(key) if scope.config is not None else None
    return default_for(key) if value is None else value


def _find_bash(path: str) -> str:
    found = locate.which("bash", path=path)
    if found:
        return found
    if os.access("/bin/bash", os.X_OK):
        return "/bin/bash"
    raise BackendUnavailable("bash was not found; the local backend needs it")


# -- the backend ----------------------------------------------------------------------------------


class LocalEnvironment(SpawnedEnvironment):
    """Commands on this machine (see the module notes)."""

    backend_name = "local"

    def __init__(
        self,
        scope: Scope,
        *,
        cwd: str,
        timeout: float,
        temp_dir: str,
        persist_env: bool = True,
        term_grace_s: float = 2.0,
        session_id: str | None = None,
        env_overrides: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        snapshot_timeout: float = SNAPSHOT_TIMEOUT_S,
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
        self.session_id = session_id
        self.term_grace_s = term_grace_s
        self.env_overrides = dict(env_overrides or {})

    def describe(self) -> str:
        return f"local bash on this machine, cwd {self._cwd}"

    def _excluded_names(self) -> tuple[str, ...]:
        # Passthrough values are injected fresh for every command (``run_env``); frozen into the
        # snapshot they would outlive a changed secret or reach another session (R02.1, P11-21).
        accepted, _ = passthrough_names(self.scope)
        return tuple(accepted)

    def run_env(self) -> dict[str, str]:
        """The environment one command starts with."""
        env = child_env(self.scope, session_id=self.session_id)
        env.update(self.env_overrides)
        env["PATH"] = complete_path(
            env.get("PATH", ""), user_home=env.get("HOME"), own_bin=own_bin_dir()
        )
        if "PYTHONPATH" in env:
            stripped = strip_app_pythonpath(env["PYTHONPATH"])
            if stripped:
                env["PYTHONPATH"] = stripped
            else:
                del env["PYTHONPATH"]
        venv = env.get("VIRTUAL_ENV")
        if venv and sys.prefix != sys.base_prefix:
            with contextlib.suppress(OSError):
                if Path(venv).resolve() == Path(sys.prefix).resolve():
                    del env["VIRTUAL_ENV"]  # Indus's own venv is not the user's
        return env

    def _refresh_cwd(self) -> None:
        usable = nearest_usable_dir(self._cwd)
        if usable != self._cwd:
            log.warning("cwd %s is gone or not enterable; using %s", self._cwd, usable)
            self._cwd = usable

    def _accept_cwd(self, path: str) -> bool:
        return os.path.isdir(path)

    async def _spawn(self, script: str, *, login: bool, stdin: str | None) -> ProcessHandle:
        env = self.run_env()
        bash = _find_bash(env["PATH"])
        argv = [bash, "-l", "-c", script] if login else [bash, "-c", script]
        popen = subprocess.Popen(  # noqa: S603 - the command is the user's, by design
            argv,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=nearest_usable_dir(self._cwd),
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        proc = LocalProcess(popen)
        if stdin is not None:
            proc.start_stdin(stdin)
        return proc

    async def _kill(self, handle: ProcessHandle, within: float | None = None) -> None:
        grace = self.term_grace_s
        if within is not None:
            grace = min(grace, max(0.0, within - SURVIVOR_WINDOW_S))
        await stop_tree(cast(LocalProcess, handle), clock=self.clock, grace=grace)

    def _force_kill(self, handle: ProcessHandle) -> None:
        force_kill_tree(cast(LocalProcess, handle))

    def _remove_files(self) -> None:
        snapshot = Path(self.snapshot_path)
        for path in [snapshot, *snapshot.parent.glob(snapshot.name + ".tmp.*")]:
            with contextlib.suppress(OSError):
                path.unlink()


async def open_local(
    scope: Scope,
    *,
    session_id: str | None = None,
    cwd: str | None = None,
    timeout: float | None = None,
    clock: Clock | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> LocalEnvironment:
    """A started local environment configured from ``scope`` (``terminal.*``,
    ``tool_output.max_chars``); arguments override the config."""
    raw_cwd = str(cwd if cwd is not None else _config(scope, "terminal.cwd") or ".")
    home_dir = scope.env.get("HOME") or str(Path.home())
    if raw_cwd == "~" or raw_cwd.startswith("~/"):
        raw_cwd = home_dir + raw_cwd[1:]
    start_dir = os.path.abspath(raw_cwd)
    env = LocalEnvironment(
        scope,
        cwd=nearest_usable_dir(start_dir),
        timeout=float(timeout if timeout is not None else _config(scope, "terminal.timeout")),
        temp_dir=choose_temp_dir(
            str(_config(scope, "terminal.temp_dir") or ""), scope.env, scope.home
        ),
        persist_env=bool(_config(scope, "terminal.persistent_shell")),
        term_grace_s=float(_config(scope, "terminal.daemon_term_grace_s")),
        session_id=session_id,
        env_overrides=env_overrides,
        clock=clock,
        max_chars=int(_config(scope, "tool_output.max_chars")),
    )
    env.sudo = SudoSupport(scope, session_id=session_id)
    await env.start()
    return env
