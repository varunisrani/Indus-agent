"""The owner's own shell command on this machine: ``shell.exec`` and quick ``exec`` commands.

Written once for both hosts that serve them (W50-b): the in-process bridge (``--local``,
``tui_bridge.methods_shell``) and the daemon's WebSocket (``gateway.ws.methods``, for an attached
client). Neither host gets a way around the approval rules (R04.27): the command goes through the
same dangerous-command detection as the agent's terminal tool, and a hardline or dangerous command
is refused with ``SHELL_REFUSED`` (no card is offered: the agent is the place for such commands);
if the detector cannot be loaded nothing runs. The child gets its environment from the one
builder, ``safety.child_env`` (provider keys removed, R02.22, the session's pins added), reads the
null device, runs in its own process group and is killed after the host's limit on the host's
clock (``SHELL_TIMEOUT``). Output is redacted first and then cut to the last ``STDOUT_KEEP`` /
``STDERR_KEEP`` characters, so a secret that crosses the cut cannot survive as two halves.

The working folder (``command_folder``): the session's folder when it is live on the host and
still exists, else the profile's ``terminal.cwd`` when that names a folder, else the host
process's own folder.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.home import expand_path_text
from indus.foundation.scope import Scope
from indus.protocol.bridge import ShellExecResult
from indus.protocol.errors import SHELL_REFUSED, SHELL_TIMEOUT, TEXT_EMPTY
from indus.protocol.methods import ProtocolError

if TYPE_CHECKING:
    from indus.service.session_service import SessionService

__all__ = [
    "KEEP_BYTES",
    "POLL_S",
    "STDERR_KEEP",
    "STDOUT_KEEP",
    "TIMEOUT_S",
    "Captured",
    "clean",
    "command_folder",
    "refusal",
    "run_captured",
    "run_host_command",
]

log = logging.getLogger(__name__)

TIMEOUT_S = 30.0
"""How long one command may run (both hosts)."""
STDOUT_KEEP = 4_000
STDERR_KEEP = 2_000
POLL_S = 0.05
KEEP_BYTES = 256 * 1024  # per stream, before redaction (only the tail is ever shown)


@dataclass(frozen=True)
class Captured:
    stdout: str
    stderr: str
    code: int
    timed_out: bool = False


def refusal(scope: Scope, command: str) -> str | None:
    """Why ``command`` must not run from a client, or None when it may."""
    try:
        from indus.safety.approvals import detect
    except Exception:
        log.error("the dangerous-command detector could not be loaded", exc_info=True)
        return "the safety check could not be loaded, so nothing ran"
    try:
        found = detect(command, user_home=scope.env.get("HOME") or None, indus_home=str(scope.home))
    except Exception:
        log.error("the dangerous-command check failed", exc_info=True)
        return "the safety check failed, so nothing ran"
    if found.hardline:
        return f"blocked, never allowed: {found.description or 'a protected command'}"
    if found.dangerous:
        return f"blocked: {found.description or 'a risky command'}; ask the agent to run it instead"
    return None


def _pump(stream: IO[bytes], into: bytearray) -> None:
    try:
        for chunk in iter(lambda: stream.read(8192), b""):
            into.extend(chunk)
            if len(into) > KEEP_BYTES * 2:
                del into[: len(into) - KEEP_BYTES]
    except (OSError, ValueError):
        log.debug("shell output stream closed early", exc_info=True)


def _kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=2.0)


def run_captured(
    command: str,
    *,
    env: Mapping[str, str],
    cwd: str | None,
    timeout: float,
    clock: Clock,
    poll: float = POLL_S,
) -> Captured:
    """Run ``command`` through the shell, capturing both streams; the deadline is on ``clock``."""
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=dict(env),
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError) as exc:
        return Captured("", f"could not start the command: {exc}", 127)
    out, err = bytearray(), bytearray()
    readers = [
        threading.Thread(target=_pump, args=(cast(IO[bytes], proc.stdout), out), daemon=True),
        threading.Thread(target=_pump, args=(cast(IO[bytes], proc.stderr), err), daemon=True),
    ]
    for reader in readers:
        reader.start()
    started = clock.monotonic()
    timed_out = False
    while proc.poll() is None:
        if clock.monotonic() - started >= timeout:
            _kill(proc)
            timed_out = True
            break
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=poll)
    for reader in readers:
        reader.join(timeout=2.0)
    return Captured(
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
        int(proc.returncode if proc.returncode is not None else -1),
        timed_out,
    )


def command_folder(
    scope: Scope, service: SessionService | None, session_id: str | None
) -> str | None:
    """Where a command for ``session_id`` runs (see the module notes); None: the host's folder."""
    if service is not None and session_id:
        try:
            agent = service.agent(session_id)
        except ProtocolError:
            agent = None
        if agent is not None:
            folder = agent.options.cwd or agent.options.session_fields.get("cwd")
            if folder and Path(str(folder)).is_dir():
                return str(folder)
    from indus.service.client_settings import read_setting

    configured = read_setting(scope, "terminal.cwd")
    if isinstance(configured, str) and configured.strip() not in ("", "."):
        expanded = expand_path_text(configured.strip(), scope.env)
        if expanded.is_dir():
            return str(expanded)
    return None


def clean(scope: Scope, text: str, command: str, keep: int) -> str:
    """``text`` redacted, then its last ``keep`` characters (withheld when redaction fails)."""
    from indus.safety.redaction import redact_output

    try:
        redacted = redact_output(text, command=command, force=True, scope=scope)
    except Exception:
        log.warning("shell output could not be redacted; withheld", exc_info=True)
        return "[output withheld]"
    return redacted[-keep:]


async def run_host_command(
    scope: Scope,
    command: str,
    *,
    session_id: str | None,
    service: SessionService | None,
    timeout: float,
    clock: Clock,
) -> ShellExecResult:
    """Check, run and clean one command for ``session_id`` (its live id on ``service``)."""
    if not command.strip():
        raise ProtocolError(TEXT_EMPTY, "type a command after !", path="command")
    why = refusal(scope, command)
    if why is not None:
        raise ProtocolError(SHELL_REFUSED, why, path="command")
    from indus.safety.child_env import child_env

    try:
        env = child_env(scope, session_id=session_id)
    except Exception as exc:
        log.error("no safe environment for a shell command", exc_info=True)
        raise ProtocolError(
            SHELL_REFUSED, f"a safe environment could not be built ({type(exc).__name__})"
        ) from exc
    captured = await run_blocking(
        lambda: run_captured(
            command,
            env=env,
            cwd=command_folder(scope, service, session_id),
            timeout=timeout,
            clock=clock,
        ),
        pool="tools",
        label="shell.exec",
    )
    if captured.timed_out:
        raise ProtocolError(SHELL_TIMEOUT, f"stopped after {timeout:g} s")
    return ShellExecResult(
        stdout=clean(scope, captured.stdout, command, STDOUT_KEEP),
        stderr=clean(scope, captured.stderr, command, STDERR_KEEP),
        code=captured.code,
    )
