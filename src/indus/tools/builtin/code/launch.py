# pyright: strict
"""Starting a local Python child for ``execute_code``: where, with which interpreter and env.

- ``private_dir``: a fresh 0700 directory (``indus-code-*``) under the scope's ``TMPDIR`` when
  that is a usable directory, else the system temp dir; ``write_private`` writes 0600 files.
- ``choose_interpreter``: ``strict`` → our interpreter; ``project`` → the active virtualenv or
  conda interpreter from the scope's environment when it is executable and answers as Python 3.8
  or newer (checked once per path, successes cached), else ours.
- ``child_environment``: ``safety.child_env`` variant ``code`` (R02.22), then what the run needs:
  RPC variables, ``TZ`` from the profile's zone, UTF-8 forced, no bytecode files, and a
  ``PYTHONPATH`` that starts with the staging dir and holds nothing of Indus's own install.
- ``spawn``: a new session and process group, stdin closed unless asked, stdout and stderr on
  pipes pumped into ``ByteCapture`` objects by daemon threads. ``stop`` is the local backend's
  ordered tree kill (SIGTERM, then SIGKILL after ``KILL_GRACE_S``); ``force_stop`` kills at once.
- Native Windows (RL26): the child starts suspended in its own job object (the Git Bash backend's
  ``start_windows_process``) and both stops terminate the job, the whole tree at once (no grace,
  as in that backend). Passing descriptors (the session kernel's pipes) has no Windows form: such a
  start is refused with a "use WSL2" message, and ``execute_code`` runs per-call scripts there.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO

from indus.backends.contract import BackendUnavailable
from indus.backends.local import LocalProcess, force_kill_tree, stop_tree, strip_app_pythonpath
from indus.backends.local_windows import WindowsProcess, start_windows_process, wsl2_refusal
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock, resolve_zone
from indus.foundation.scope import Scope
from indus.platform.facts import os_family
from indus.safety.child_env import child_env
from indus.tools.builtin.code.output import ByteCapture
from indus.tools.builtin.code.settings import KILL_GRACE_S, Mode

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 5.0
_PROBE_CACHE_MAX = 32
_usable: dict[str, bool] = {}
_usable_lock = threading.Lock()
_PROBE = "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"


# -- staging -------------------------------------------------------------------------------------


def staging_root(scope: Scope) -> str:
    configured = scope.env.get("TMPDIR", "").strip()
    if configured and os.path.isdir(configured) and os.access(configured, os.W_OK | os.X_OK):
        return configured
    return tempfile.gettempdir()


def private_dir(root: str, prefix: str = "indus-code-") -> Path:
    """A new owner-only directory under ``root``."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


# -- the interpreter -----------------------------------------------------------------------------


def _probe(path: str) -> bool:
    with _usable_lock:
        if path in _usable:
            return True
    try:
        done = subprocess.run(  # noqa: S603 - a fixed check of a known interpreter path
            [path, "-c", _PROBE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_S,
            env={"PATH": os.defpath},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if done.returncode != 0:
        return False
    with _usable_lock:
        if len(_usable) >= _PROBE_CACHE_MAX:
            _usable.pop(next(iter(_usable)))
        _usable[path] = True
    return True


def _candidates(scope: Scope) -> list[str]:
    found: list[str] = []
    folder, names = (
        ("Scripts", ("python.exe",)) if os.name == "nt" else ("bin", ("python3", "python"))
    )
    for variable in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        root = scope.env.get(variable, "").strip()
        if not root:
            continue
        for name in names:
            candidate = os.path.join(root, folder, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found.append(candidate)
                break
    return found


async def choose_interpreter(scope: Scope, mode: Mode) -> str:
    """See the module notes."""
    if mode == "strict":
        return sys.executable
    for candidate in _candidates(scope):
        if candidate == sys.executable:
            return candidate
        if await run_blocking(_probe, candidate, pool="io", label="python probe"):
            return candidate
        log.info("execute_code: %s is not a usable Python 3.8+; using Indus's own", candidate)
    return sys.executable


def forget_probes() -> None:
    with _usable_lock:
        _usable.clear()


# -- the environment -----------------------------------------------------------------------------


def child_environment(
    scope: Scope,
    *,
    session_id: str | None,
    staging: Path,
    extra: Mapping[str, str],
) -> dict[str, str]:
    env = child_env(scope, variant="code", session_id=session_id)
    zone = resolve_zone(scope)
    if zone is not None and os.name != "nt":
        env["TZ"] = zone.key
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    kept = strip_app_pythonpath(env.get("PYTHONPATH", ""))
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(staging), kept) if p)
    env.update(extra)
    return env


# -- the process ---------------------------------------------------------------------------------


def _pump(stream: IO[bytes], capture: ByteCapture) -> None:
    try:
        while True:
            chunk = stream.read1(65536)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
            if not chunk:
                return
            capture.append(bytes(chunk))  # pyright: ignore[reportUnknownArgumentType]
    except (OSError, ValueError):
        return
    finally:
        with contextlib.suppress(OSError):
            stream.close()


class Child:
    """One running Python child and the threads reading its output."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdout: ByteCapture,
        stderr: ByteCapture,
        pass_fds: Sequence[int] = (),
    ) -> None:
        self.process: LocalProcess | None = None
        self.job: WindowsProcess | None = None
        if os_family() == "win32":
            if pass_fds:
                raise BackendUnavailable(wsl2_refusal("The persistent Python kernel"))
            self.job = start_windows_process(
                list(argv), cwd=cwd, env=env, stdin=None, separate_stderr=True
            )
            popen = self.job.popen
        else:
            popen = subprocess.Popen(  # noqa: S603 - the interpreter and our staged file
                list(argv),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
                pass_fds=tuple(pass_fds),
            )
            self.process = LocalProcess(popen)
        self.popen = popen
        self.stdout, self.stderr = stdout, stderr
        assert popen.stdout is not None and popen.stderr is not None
        self._readers = [
            threading.Thread(target=_pump, args=(popen.stdout, stdout), daemon=True),
            threading.Thread(target=_pump, args=(popen.stderr, stderr), daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    @property
    def pid(self) -> int:
        return self.popen.pid

    def poll(self) -> int | None:
        return self.popen.poll()

    def alive(self) -> bool:
        return self.popen.poll() is None

    async def stop(self, clock: Clock, grace: float = KILL_GRACE_S) -> None:
        if self.process is None:
            self.force_stop()
        elif self.alive():
            await stop_tree(self.process, clock=clock, grace=grace)
        else:
            force_kill_tree(self.process)  # leftovers of an exited leader's group

    def force_stop(self) -> None:
        if self.job is not None:
            self.job.terminate_tree()  # the whole job, background grandchildren included
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                self.popen.wait(timeout=KILL_GRACE_S)
            self.job.release()
        elif self.process is not None:
            force_kill_tree(self.process)

    async def finish_reading(self, clock: Clock, within: float = 2.0) -> None:
        """Give the reader threads a moment to take what is left in the pipes."""
        deadline = clock.monotonic() + within
        while any(r.is_alive() for r in self._readers) and clock.monotonic() < deadline:
            await clock.sleep(0.02)
        if self.job is not None and not self.alive():
            self.job.release()  # the job handle; its leftovers were stopped or live on, as on POSIX


__all__ = [
    "Child",
    "child_environment",
    "choose_interpreter",
    "forget_probes",
    "private_dir",
    "staging_root",
    "write_private",
]
