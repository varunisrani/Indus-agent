"""``!command``: run a shell command from the prompt without a model turn (P06-11).

Only a leading ``!`` counts (after trimming): ``fix it!`` is a prompt. ``!!`` hands a literal
``!`` to the shell; a bare ``!`` prints a usage line. The command:

- is allowed only in an interactive local session (never for a gateway, cron or other platform
  session: nobody sits at that keyboard);
- runs on the host where the CLI runs, never in the configured sandbox backend, in the session's
  working directory, else ``terminal.cwd`` from the config, else the process's own;
- passes the same approval pipeline as the agent's terminal tool, as a local, non-isolated command
  (hardline floor, deny globs, the dangerous-command question). If the pipeline cannot be loaded
  the command is refused — fail closed (D-16.77, R04.27);
- gets its environment only from ``safety.child_env`` (provider keys removed); if that fails,
  nothing starts;
- streams merged stdout and stderr line by line; after 120 s on the injected clock the process
  group is killed (code 124); Ctrl+C kills it (130) and the session goes on; a launch failure is
  127; a command that was not run (refused or blocked) is 126.

Nothing enters the conversation: no turn, no stored row, no tokens.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import queue
import secrets
import signal
import subprocess
import threading
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import IO, Any, cast

from indus.cli.slash.context import SlashContext
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.protocol.events import CancelReason
from indus.protocol.requests_port import AnswerModel, RequestWithdrawn
from indus.protocol.server_requests import ApprovalAnswer, ApprovalRequest

__all__ = [
    "BLOCKED_RC",
    "DEFAULT_TIMEOUT_S",
    "INTERRUPTED_RC",
    "LAUNCH_FAILED_RC",
    "TIMED_OUT_RC",
    "USAGE",
    "ShellRun",
    "bang_allowed",
    "is_bang",
    "parse_bang",
    "run_bang",
    "run_shell",
]

log = logging.getLogger(__name__)

USAGE = (
    "Usage: !COMMAND runs a shell command here without asking the model (for example !git status)."
)
DEFAULT_TIMEOUT_S = 120.0
POLL_S = 0.05
TIMED_OUT_RC = 124
INTERRUPTED_RC = 130
LAUNCH_FAILED_RC = 127
BLOCKED_RC = 126
GUARD_MODULE = "indus.safety.approvals"
# A session that belongs to another surface has no person at this keyboard.
NOT_INTERACTIVE_MARKERS = ("INDUS_GATEWAY_SESSION", "INDUS_CRON_SESSION")
PLATFORM_MARKER = "INDUS_SESSION_PLATFORM"
_TRUE = frozenset({"1", "true", "yes", "on"})


def is_bang(text: str | None) -> bool:
    """Is this prompt input a bang command (a ``!`` first, after trimming)?"""
    return isinstance(text, str) and text.strip().startswith("!")


def parse_bang(text: str) -> str:
    """The shell command of a bang input: ``!  ls`` → ``ls``, ``!!`` → ``!``, ``!`` → ``""``."""
    return text.strip()[1:].strip() if is_bang(text) else ""


def bang_allowed(scope: Scope, *, interactive: bool = True) -> bool:
    """Only interactive local sessions may run bang commands."""
    if not interactive:
        return False
    env = scope.env
    if any(env.get(name, "").strip().lower() in _TRUE for name in NOT_INTERACTIVE_MARKERS):
        return False
    return not env.get(PLATFORM_MARKER, "").strip()


# -- running a shell command ----------------------------------------------------------------------


@dataclass(frozen=True)
class ShellRun:
    """How a shell command ended."""

    code: int
    timed_out: bool = False
    interrupted: bool = False
    launch_error: str | None = None


def _pump(stream: IO[bytes], lines: queue.Queue[str | None]) -> None:
    try:
        for raw in iter(stream.readline, b""):
            lines.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
    except (OSError, ValueError):
        log.debug("shell output stream closed early", exc_info=True)
    finally:
        lines.put(None)


def _drain(lines: queue.Queue[str | None], on_line: Callable[[str], None]) -> bool:
    """Hand every waiting line to ``on_line``; True once the reader has finished."""
    while True:
        try:
            line = lines.get_nowait()
        except queue.Empty:
            return False
        if line is None:
            return True
        on_line(line)


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill the command's whole process group (the shell and what it started)."""
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


def run_shell(
    command: str,
    *,
    env: Mapping[str, str],
    cwd: str | None,
    timeout: float,
    clock: Clock,
    on_line: Callable[[str], None],
    cancel: Callable[[], bool] | None = None,
    poll: float = POLL_S,
) -> ShellRun:
    """Run ``command`` through the shell, handing each output line to ``on_line``.

    The deadline is measured on ``clock``; ``cancel`` returning True, or a ``KeyboardInterrupt``
    (Ctrl+C, also raised from ``on_line``), kills the process group with code 130.
    """
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=dict(env),
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError) as exc:
        return ShellRun(LAUNCH_FAILED_RC, launch_error=str(exc))
    lines: queue.Queue[str | None] = queue.Queue()
    stdout = cast(IO[bytes], proc.stdout)
    reader = threading.Thread(target=_pump, args=(stdout, lines), name="bang-output", daemon=True)
    reader.start()
    started = clock.monotonic()
    finished = False
    try:
        while True:
            finished = _drain(lines, on_line) or finished
            if proc.poll() is not None:
                break
            if cancel is not None and cancel():
                _kill(proc)
                return ShellRun(INTERRUPTED_RC, interrupted=True)
            if clock.monotonic() - started >= timeout:
                _kill(proc)
                _drain(lines, on_line)
                return ShellRun(TIMED_OUT_RC, timed_out=True)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=poll)
        reader.join(timeout=2.0)
        if not finished:
            _drain(lines, on_line)
        return ShellRun(int(proc.returncode or 0))
    except KeyboardInterrupt:
        _kill(proc)
        return ShellRun(INTERRUPTED_RC, interrupted=True)
    finally:
        with contextlib.suppress(OSError):
            stdout.close()


# -- the approval guard ---------------------------------------------------------------------------


def load_guard() -> ModuleType | None:
    """The approval pipeline module, or None when it cannot be imported (refuse, D-16.77)."""
    try:
        return importlib.import_module(GUARD_MODULE)
    except Exception:
        log.error(
            "the approval guard could not be loaded; bang commands are refused", exc_info=True
        )
        return None


class LocalAsk:
    """A one-session request port that asks the person at this terminal (``ctx.ask_approval``)."""

    def __init__(
        self,
        session_id: str,
        ask: Callable[[ApprovalRequest], ApprovalAnswer | None] | None,
        clock: Clock,
    ) -> None:
        self.session_id = session_id
        self._ask = ask
        self._clock = clock
        self._waited = 0.0

    async def ask(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        turn_id: str | None = None,
    ) -> AnswerModel:
        request_id = "bang_" + secrets.token_hex(6)
        if kind != "approval" or self._ask is None:
            raise RequestWithdrawn(request_id, CancelReason.SESSION_CLOSED)
        request = ApprovalRequest.model_validate(
            {**payload, "request_id": request_id, "session_id": self.session_id}
        )
        started = self._clock.monotonic()
        try:
            answer = self._ask(request)
        finally:
            self._waited += max(0.0, self._clock.monotonic() - started)
        if answer is None:
            raise RequestWithdrawn(request_id, CancelReason.INTERRUPTED)
        return answer

    def cancel_all(self, reason: CancelReason) -> int:
        return 0

    def human_wait_seconds(self) -> float:
        return self._waited


def _run_coro[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` to its end from synchronous code (in a helper thread if a loop runs here)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:
            box["error"] = exc

    worker = threading.Thread(target=run, name="bang-guard", daemon=True)
    worker.start()
    worker.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def check_command(command: str, cwd: str | None, ctx: SlashContext) -> tuple[bool, str | None]:
    """``(may run, why not)`` from the approval pipeline; refused when it cannot be used."""
    guard = load_guard()
    if guard is None:
        return False, "the safety check could not be loaded, so nothing ran"
    try:
        context = guard.ApprovalContext(
            scope=ctx.scope,
            session_id=ctx.session_id,
            run_context="interactive",
            broker=LocalAsk(ctx.session_id, ctx.ask_approval, ctx.time),
            isolated=False,
            cwd=cwd,
        )
        decision = _run_coro(guard.decide(command, context))
    except Exception as exc:
        log.error("the approval check failed: %s", exc, exc_info=True)
        return False, "the safety check failed, so nothing ran"
    if decision.approved:
        return True, None
    why = decision.user_summary or decision.description or "the safety check blocked it"
    return False, why


# -- the bang command -----------------------------------------------------------------------------


def _usable_dir(path: str | None) -> str | None:
    if not path:
        return None
    expanded = os.path.expanduser(path)
    return expanded if Path(expanded).is_dir() else None


def resolve_cwd(ctx: SlashContext) -> str:
    """Session cwd, else ``terminal.cwd`` from the config, else the process cwd."""
    candidates: list[str | None] = []
    if ctx.session_cwd is not None:
        candidates.append(ctx.session_cwd())
    config = ctx.scope.config
    configured = config.get("terminal.cwd") if config is not None else None
    if isinstance(configured, str) and configured.strip() not in ("", "."):
        candidates.append(configured.strip())
    for candidate in candidates:
        found = _usable_dir(candidate)
        if found is not None:
            return found
    return os.getcwd()


def run_bang(
    text: str,
    ctx: SlashContext,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    cancel: Callable[[], bool] | None = None,
) -> int:
    """Run a bang input (see the module notes); returns the exit code."""
    command = parse_bang(text)
    if not command:
        ctx.say(USAGE)
        return 0
    if not bang_allowed(ctx.scope, interactive=ctx.interactive):
        ctx.say("Shell commands with ! only work in an interactive session on this machine.")
        return BLOCKED_RC
    cwd = resolve_cwd(ctx)
    allowed, why = check_command(command, cwd, ctx)
    if not allowed:
        ctx.say(f"!: not run: {why}")
        return BLOCKED_RC
    try:
        from indus.safety.child_env import child_env

        env = child_env(ctx.scope, session_id=ctx.session_id)
    except Exception as exc:
        log.error("no safe environment for a bang command", exc_info=True)
        ctx.say(f"!: not run: a safe environment could not be built ({type(exc).__name__})")
        return LAUNCH_FAILED_RC
    outcome = run_shell(
        command,
        env=env,
        cwd=cwd,
        timeout=timeout,
        clock=ctx.time,
        on_line=ctx.out,
        cancel=cancel,
    )
    if outcome.launch_error is not None:
        ctx.say(f"!: could not start the command: {outcome.launch_error}")
    elif outcome.timed_out:
        ctx.say(f"!: stopped after {timeout:g}s (timed out)")
    elif outcome.interrupted:
        ctx.say(f"!: interrupted (exit {outcome.code})")
    elif outcome.code != 0:
        ctx.say(f"!: exit {outcome.code}")
    return outcome.code
