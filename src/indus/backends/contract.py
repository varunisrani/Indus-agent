# pyright: strict
"""The execution contract every terminal backend meets (P05-01).

An ``Environment`` runs one shell command at a time and answers with an ``ExecResult``. The
terminal tool, the file tools and every later backend (containers, SSH, cloud sandboxes) talk to
this protocol only. Names here are the public surface of ``indus.backends``.

Cancellation: the running turn's stop signal reaches a backend as a ``CancelSignal`` (anything with
``is_set()``, e.g. the turn's ``asyncio.Event``). Backends sit below ``core`` in the layer order,
so they never import the interrupt machinery itself. Cancelling the task that awaits ``execute``
stops the command as well.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from indus.foundation.errors import IndusError

__all__ = [
    "ActivityCallback",
    "BackendError",
    "BackendFlags",
    "BackendUnavailable",
    "CancelSignal",
    "Environment",
    "EnvironmentConnectionError",
    "ExecResult",
    "StdinMode",
]

type StdinMode = Literal["pipe", "payload", "heredoc"]
"""How stdin reaches a command: the process's stdin pipe, a payload the backend ships itself, or
text embedded in the script as a heredoc."""

type ActivityCallback = Callable[[str], None]
"""Called now and then while a long command runs, so the caller can show it is alive."""

DEFAULT_RETRY_HINT = (
    "The command did not fail: the backend could not be reached. Check that it is running and "
    "reachable, then run the same command again; a new connection is made automatically."
)


class CancelSignal(Protocol):
    """The turn's stop flag as a backend sees it (``asyncio.Event`` and ``threading.Event`` fit)."""

    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ExecResult:
    """What one command produced.

    ``returncode`` is the command's exit status (124 at our deadline, 130 when interrupted).
    ``cwd_observed`` is the directory the command ended in, only when this command reported it.
    ``full_output_path`` / ``output_total_chars`` are set when bounded capture cut the output and
    the whole stream was kept in a file. ``sudo_auth_failed`` / ``sudo_cache_cleared``: sudo
    rejected the password Indus fed it, and the session's cached copy was dropped (P05-10).
    """

    output: str
    returncode: int
    cwd_observed: str | None = None
    timed_out: bool = False
    interrupted: bool = False
    full_output_path: str | None = None
    output_total_chars: int | None = None
    stdin_error: str | None = None
    environment_recreated: bool = False
    pid: int | None = None
    sudo_auth_failed: bool = False
    sudo_cache_cleared: bool = False


@dataclass(frozen=True, slots=True)
class BackendFlags:
    """Facts about where a backend runs commands.

    ``isolated``: commands cannot touch the host (a container or VM), so host-only guards may be
    relaxed while deny rules still apply. ``container_paths``: cwd and paths are the sandbox's,
    not the host's. ``stdin_mode``: see ``StdinMode``. ``host_local``: commands run on the Indus
    host itself.
    """

    isolated: bool = False
    container_paths: bool = False
    stdin_mode: StdinMode = "pipe"
    host_local: bool = False


@runtime_checkable
class Environment(Protocol):
    """One place commands run (see the module notes)."""

    @property
    def name(self) -> str:
        """The backend name (``local``, ``docker``, ...)."""
        ...

    @property
    def flags(self) -> BackendFlags: ...

    @property
    def cwd(self) -> str:
        """The working directory the next command starts in."""
        ...

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
        """Run ``command``. ``bounded`` caps the kept output (the model-facing foreground path
        only); internal readers pass ``bounded=False`` and get every character."""
        ...

    async def cleanup(self) -> None:
        """Give back everything this environment holds; safe to call twice."""
        ...

    def describe(self) -> str:
        """One line for logs and the prompt: backend and where it runs."""
        ...


class BackendError(IndusError):
    default_code = "backend.error"


class EnvironmentConnectionError(BackendError):
    """The backend itself could not be reached (daemon down, host unreachable, link lost).

    Never a command that merely exited non-zero. The registry turns it into a ``degraded`` result
    and drops the environment so the next call starts a fresh one.
    """

    default_code = "backend.connection"

    def __init__(self, reason: str, *, retry_hint: str = "") -> None:
        super().__init__(reason, hint=retry_hint or DEFAULT_RETRY_HINT)
        self.reason = reason
        self.retry_hint: str = retry_hint or DEFAULT_RETRY_HINT


class BackendUnavailable(BackendError):
    """The backend cannot be created here (an optional dependency or binary is missing)."""

    default_code = "backend.unavailable"
