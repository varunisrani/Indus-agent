# pyright: strict
"""The shared flow of every spawn-per-command shell backend (P05-01).

A subclass only knows how to start ``bash`` with a script (``_spawn``) and how to stop a process
tree (``_kill`` with grace, ``_force_kill`` at once). This base does the rest the same way for
every backend: the snapshot at start (with its fallbacks), the compound-background rewrite,
heredoc stdin, the command wrapper, bounded or full capture, the wait with the hard backstop, the
exit fence, and reading the cwd marker back.

Snapshot fallbacks: when the login-shell bootstrap fails, commands run under ``bash -l`` so the
user's profile still loads; when a short ``bash -c true`` probe shows plain bash works while the
login shell is broken, commands run under plain ``bash -c`` instead.
"""

from __future__ import annotations

import logging
import secrets
import shlex
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from indus.backends.capture import (
    DEFAULT_MAX_CHARS,
    OutputCapture,
    bounded_capture,
    unbounded_capture,
)
from indus.backends.contract import (
    ActivityCallback,
    BackendError,
    BackendFlags,
    CancelSignal,
    EnvironmentConnectionError,
    ExecResult,
)
from indus.backends.wait import (
    EXITING_NOTE,
    FOREGROUND,
    INTERRUPTED_RC,
    TERMINAL_POOL,
    ProcessHandle,
    run_bounded,
    wait_process,
)
from indus.backends.wrapper import (
    ScriptPaths,
    bootstrap_script,
    cwd_marker,
    embed_stdin_heredoc,
    rewrite_compound_background,
    split_cwd_marker,
    wrap_command,
)
from indus.foundation.clock import Clock, SystemClock

__all__ = ["SNAPSHOT_TIMEOUT_S", "SpawnedEnvironment"]

log = logging.getLogger(__name__)

SNAPSHOT_TIMEOUT_S = 30.0
PROBE_TIMEOUT_S = 15.0


class SpawnedEnvironment(ABC):
    """Base of the shell backends; meets ``indus.backends.contract.Environment``."""

    backend_name: str = "shell"

    def __init__(
        self,
        *,
        cwd: str,
        timeout: float,
        temp_dir: str,
        flags: BackendFlags,
        persist_env: bool = True,
        clock: Clock | None = None,
        capture_home: Path | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        snapshot_timeout: float = SNAPSHOT_TIMEOUT_S,
    ) -> None:
        self._cwd = cwd
        self.timeout = float(timeout)
        self._flags = flags
        self.persist_env = persist_env
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.capture_home = capture_home
        self.max_chars = max_chars
        self.snapshot_timeout = snapshot_timeout
        self.env_id = secrets.token_hex(6)
        base = temp_dir.rstrip("/") or "/"
        self.snapshot_path = f"{base}/indus-snap-{self.env_id}.sh"
        self.paths = ScriptPaths(
            snapshot=shlex.quote(self.snapshot_path),
            snapshot_tmp_template=shlex.quote(self.snapshot_path + ".tmp.XXXXXXXXXX"),
            marker=cwd_marker(self.env_id),
        )
        self.snapshot_ready = False
        self.prefer_nonlogin = False
        self._recreated = False
        self._closed = False

    # -- contract surface ------------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.backend_name

    @property
    def flags(self) -> BackendFlags:
        return self._flags

    @property
    def cwd(self) -> str:
        return self._cwd

    def describe(self) -> str:
        return f"{self.backend_name} shell in {self._cwd}"

    # -- subclass hooks ----------------------------------------------------------------------------

    @abstractmethod
    async def _spawn(self, script: str, *, login: bool, stdin: str | None) -> ProcessHandle:
        """Start bash running ``script`` (``bash -l -c`` when ``login``)."""

    @abstractmethod
    async def _kill(self, handle: ProcessHandle, within: float | None = None) -> None:
        """Stop the process tree in order, with grace; ``within`` caps the time it may take."""

    @abstractmethod
    def _force_kill(self, handle: ProcessHandle) -> None:
        """Stop the process tree now, without waiting (hard exit, backstop)."""

    def _excluded_names(self) -> Iterable[str]:
        """Extra variable names that must never be frozen into the snapshot."""
        return ()

    def _prepare(self, command: str) -> str:
        """Command text rewrites before wrapping (the sudo rewrite of P05-10 hooks in here)."""
        return rewrite_compound_background(command)

    def _wrap(self, command: str, cwd: str) -> str:
        return wrap_command(
            command,
            self.paths,
            cwd=cwd,
            snapshot_ready=self.snapshot_ready,
            persist_env=self.persist_env,
            excluded_names=self._excluded_names(),
        )

    def _refresh_cwd(self) -> None:  # noqa: B027 - optional hook
        """Before each command: a backend may move a vanished cwd somewhere usable."""

    def _accept_cwd(self, path: str) -> bool:
        """Whether a reported cwd may become the environment's cwd."""
        return bool(path)

    def mark_recreated(self) -> None:
        """The sandbox behind this environment was replaced while serving the current command;
        the next result says so once (background processes and unsaved files are gone)."""
        self._recreated = True

    # -- lifecycle ---------------------------------------------------------------------------------

    async def start(self) -> None:
        """Write the snapshot (once, after construction); fall back as the module notes say."""
        script = bootstrap_script(self.paths, cwd=self._cwd, excluded_names=self._excluded_names())
        try:
            result = await self._run_script(
                script, login=True, timeout=self.snapshot_timeout, stdin=None, bounded=False
            )
            if result.returncode != 0:
                raise BackendError(f"snapshot script exited with status {result.returncode}")
            self.snapshot_ready = True
            self._take_cwd(result)
            log.info("%s shell snapshot ready (cwd %s)", self.backend_name, self._cwd)
        except EnvironmentConnectionError:
            raise
        except Exception as exc:
            self.snapshot_ready = False
            self.prefer_nonlogin = await self._plain_bash_works()
            log.warning(
                "%s shell snapshot failed (%s); commands run under %s",
                self.backend_name,
                exc,
                "plain bash -c" if self.prefer_nonlogin else "bash -l",
            )

    async def _plain_bash_works(self) -> bool:
        try:
            probe = await self._run_script(
                "true",
                login=False,
                timeout=min(PROBE_TIMEOUT_S, self.snapshot_timeout),
                stdin=None,
                bounded=False,
            )
        except EnvironmentConnectionError:
            raise
        except Exception:
            return False
        return probe.returncode == 0

    # -- running -----------------------------------------------------------------------------------

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
        if self._closed:
            raise BackendError(f"the {self.backend_name} environment was cleaned up")
        text = self._prepare(command)
        if stdin is not None and self._flags.stdin_mode == "heredoc":
            text = embed_stdin_heredoc(text, stdin)
            stdin = None
        if not cwd:
            self._refresh_cwd()
        script = self._wrap(text, cwd or self._cwd)
        result = await self._run_script(
            script,
            login=not self.snapshot_ready and not self.prefer_nonlogin,
            timeout=self.timeout if timeout is None or timeout <= 0 else float(timeout),
            stdin=stdin,
            bounded=bounded,
            on_activity=on_activity,
            cancel=cancel,
        )
        result = self._take_cwd(result, adopt=not cwd)
        if self._recreated:
            self._recreated = False
            result = replace(result, environment_recreated=True)
        return result

    async def _run_script(
        self,
        script: str,
        *,
        login: bool,
        timeout: float,
        stdin: str | None,
        bounded: bool,
        on_activity: ActivityCallback | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        capture: OutputCapture = (
            bounded_capture(self.capture_home, self.max_chars) if bounded else unbounded_capture()
        )
        spawned: list[ProcessHandle] = []

        async def spawn_and_wait() -> ExecResult:
            if TERMINAL_POOL.busy >= TERMINAL_POOL.workers:
                raise BackendError(
                    "too many commands are running at once",
                    hint="wait for running commands to finish, then run it again",
                )
            if not FOREGROUND.admit():
                return ExecResult(output=EXITING_NOTE, returncode=INTERRUPTED_RC, interrupted=True)
            started: ProcessHandle | None = None
            try:
                started = await self._spawn(script, login=login, stdin=stdin)
            finally:
                fenced = FOREGROUND.publish(started, self._force_kill)
            assert started is not None  # _spawn returned (it raises otherwise)
            handle = started
            spawned.append(handle)
            if fenced:
                self._force_kill(handle)
            try:
                return await wait_process(
                    handle,
                    capture,
                    timeout=timeout,
                    clock=self.clock,
                    kill=self._kill,
                    cancel=cancel,
                    on_activity=on_activity,
                )
            finally:
                FOREGROUND.remove(handle)

        def expire() -> None:
            for handle in spawned:
                self._force_kill(handle)

        return await run_bounded(
            spawn_and_wait, timeout=timeout, clock=self.clock, on_expiry=expire, capture=capture
        )

    def _take_cwd(self, result: ExecResult, *, adopt: bool = True) -> ExecResult:
        """Strip this environment's marker line and report where the command ended. The
        environment's own cwd moves only when this command printed the marker (a killed command
        printed none) and ran in the environment's cwd, not a per-call workdir (R02.10)."""
        found = split_cwd_marker(result.output, self.paths.marker)
        if found is None:
            return result
        path, cleaned = found
        if path and self._accept_cwd(path):
            if adopt:
                self._cwd = path
            return replace(result, output=cleaned, cwd_observed=path)
        return replace(result, output=cleaned)

    async def cleanup(self) -> None:
        self._closed = True
        self._remove_files()

    def _remove_files(self) -> None:  # noqa: B027 - optional hook
        """Remove the snapshot and stray temp copies (local file system backends)."""
