# pyright: strict
"""The ``terminal`` tool's foreground run and every result shape it answers with (P05-12).

``run_foreground`` runs an approved command through the environment registry with bounded
capture. Backend trouble the registry recognises (unreachable, unusable) comes back as a
``BackendStatus``; any other exception is retried up to three times after 2, 4 and 8 s on the
injected clock, except one that says it timed out (answered at once with exit 124). The cancel
signal is checked while backing off, so an interrupt is never lost to a retry.

``finish`` turns an ``ExecResult`` into the JSON the model reads, in this order: the sudo flags
the P05-10 layer set on the result → head/tail cut to ``tool_output.max_chars`` → escape codes
removed → secrets masked (command-aware, P05-06) → what a non-zero exit code means → one hint
(or, on exit 0, a warning that a pipe or ``|| echo`` may have hidden a failure) → the overflow
log rewritten masked.

Keys keep one order: ``output, exit_code, error``, then ``cwd, environment_recreated,
full_output_path, output_total_chars, approval, exit_code_meaning, hint, sudo_auth_failed,
sudo_cache_cleared``. An optional field that does not apply is left out, never ``null``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from indus.backends.contract import CancelSignal, ExecResult
from indus.backends.processes import strip_ansi
from indus.backends.registry import BackendStatus, EnvironmentRegistry
from indus.backends.sudo import use_sudo_caller
from indus.backends.wait import INTERRUPTED_RC, TIMED_OUT_RC, interrupted_note
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.protocol.requests_port import ServerRequestPort
from indus.safety.redaction import redact_output
from indus.tools.truncate import head_tail

__all__ = [
    "ENV_RECREATED_NOTE",
    "RETRY_DELAYS",
    "backend_status",
    "background_failed",
    "background_started",
    "blocked",
    "exit_code_meaning",
    "failed",
    "failure_hint",
    "finish",
    "masked_failure_hint",
    "pending",
    "rejected",
    "run_foreground",
    "timed_out",
]

log = logging.getLogger(__name__)

RETRY_DELAYS: tuple[float, ...] = (2.0, 4.0, 8.0)
BACKOFF_SLICE_S = 0.5
HINT_SCAN_CHARS = 4_000
INTERRUPT_MARK = interrupted_note().strip()

ENV_RECREATED_NOTE = (
    "The environment had to be rebuilt while this command ran (its container or sandbox had "
    "gone away). Saved files were restored where the backend keeps them, but background "
    "processes and anything outside kept paths from earlier commands may be gone; check before "
    "relying on earlier work."
)


def _json(body: Mapping[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False)


# -- shapes ---------------------------------------------------------------------------------------


def rejected(error: str) -> str:
    """The call was refused before anything ran (bad arguments, a shape that needs rewriting)."""
    return _json({"error": error, "status": "error"})


def blocked(error: str, *, user_summary: str | None = None) -> str:
    """A guard or the approval pipeline refused the command; nothing ran."""
    body: dict[str, Any] = {"output": "", "exit_code": -1, "error": error, "status": "blocked"}
    if user_summary:
        body["user_summary"] = user_summary
    return _json(body)


def pending(
    command: str, description: str, pattern_key: str, *, allow_permanent: bool = True
) -> str:
    """The command waits for an approval that will come through another channel."""
    return _json(
        {
            "status": "pending_approval",
            "approval_pending": True,
            "command": command,
            "description": description,
            "pattern_key": pattern_key,
            "smart_denied": False,
            "allow_permanent": allow_permanent,
        }
    )


def timed_out(seconds: float) -> str:
    """The backend raised a timeout before the command produced a result."""
    return _json(
        {
            "output": "",
            "exit_code": TIMED_OUT_RC,
            "error": f"Command timed out after {seconds:g} seconds",
        }
    )


def failed(error: str) -> str:
    return _json({"output": "", "exit_code": -1, "error": error})


def background_started(process_id: str, pid: int | None, extra: Mapping[str, Any]) -> str:
    body: dict[str, Any] = {
        "output": "Background process started",
        "session_id": process_id,
        "pid": pid,
        "exit_code": 0,
        "error": None,
    }
    body.update(extra)
    return _json(body)


def background_failed(error: str) -> str:
    return _json(
        {"output": "", "exit_code": -1, "error": f"Failed to start background process: {error}"}
    )


def backend_status(status: BackendStatus) -> str:
    """Degraded, disabled or failed backend (shapes from the environment registry)."""
    return _json(status.to_dict())


# -- exit codes and hints -------------------------------------------------------------------------

_SIGNAL_NOTES: dict[int, str] = {
    3: "SIGQUIT (quit request)",
    4: "SIGILL (illegal instruction: a broken binary or the wrong CPU architecture)",
    6: "SIGABRT (the program aborted itself, e.g. a failed assertion)",
    7: "SIGBUS (bad memory access)",
    8: "SIGFPE (arithmetic fault such as division by zero)",
    9: "SIGKILL (killed outright; often the out-of-memory killer, or kill -9)",
    11: "SIGSEGV (the program crashed: segmentation fault)",
    13: "SIGPIPE (it wrote into a pipe whose reader had already gone)",
    15: "SIGTERM (asked to stop: a kill, a timeout or a shutdown)",
    24: "SIGXCPU (CPU time limit reached)",
    25: "SIGXFSZ (file size limit reached)",
}
_HEDGED_SIGNALS = frozenset({9, 11, 13, 15, 24, 25})  # 137, 139, 141, 143, 152, 153

_INFORMATIONAL: dict[str, dict[int, str]] = {
    **{
        name: {1: "no matches were found (not an error)"}
        for name in ("grep", "egrep", "fgrep", "rg", "ag", "ack")
    },
    **{
        name: {1: "the inputs differ (expected, not an error)"}
        for name in ("diff", "colordiff", "cmp")
    },
    **{name: {1: "the condition was false (not an error)"} for name in ("test", "[", "[[")},
    "find": {1: "some paths could not be read; the results shown may still be useful"},
    "curl": {
        6: "the host name could not be resolved",
        7: "the connection to the host failed",
        22: "the server answered with an HTTP error status",
        28: "the transfer timed out",
    },
    "git": {1: "git reported a difference or a soft failure (normal for git diff and similar)"},
}

_SEGMENT_SPLIT = re.compile(r"\|\||&&|\||;")


def _base_command(segment: str) -> str:
    for word in segment.split():
        if "=" in word and not word.startswith(("-", "/", "./")):
            continue  # a VAR=value prefix
        return word.rsplit("/", 1)[-1]
    return ""


def exit_code_meaning(command: str, code: int) -> str | None:
    """What a non-zero exit status tells, when it is not a plain failure."""
    if code == 0:
        return None
    if code < 0:
        number = -code
        if number == signal.SIGINT:
            return None  # interrupts carry their own marker
        note = _SIGNAL_NOTES.get(number)
        if note is None:
            try:
                note = signal.Signals(number).name
            except ValueError:
                note = f"signal {number}"
        return f"The command was terminated by signal {number}: {note}"
    if code > 128 and (code - 128) in _HEDGED_SIGNALS:
        number = code - 128
        return f"Exit code {code} usually means signal {number}: {_SIGNAL_NOTES[number]}"
    segments = [s for s in _SEGMENT_SPLIT.split(command) if s.strip()]
    last = segments[-1] if segments else command
    return _INFORMATIONAL.get(_base_command(last.strip()), {}).get(code)


_MASKING_TAIL = re.compile(
    r"(?<!\|)\|(?!\|)\s*(?:tail|head|cat|tee|less|more|wc|sort|uniq)\b[^|]*$"
)
_MASKING_FALLBACK = re.compile(r"\|\|\s*(?:echo\b|printf\b|true\b|:(?:\s|$))")
_READ_ONLY_HEADS = frozenset(
    {"grep", "rg", "ag", "find", "ls", "cat", "head", "tail", "jq", "awk", "sed", "echo", "printf"}
)
_FAILURE_TEXT = re.compile(
    r"Traceback \(most recent call last\)"
    r"|(?m:^E\s{2,}\w*Error)"
    r"|(?m:^=+ .*\d+ failed)"
    r"|(?m:^FAILED \S+)"
    r"|error\[E\d{4}\]"
    r"|error: could not compile"
    r"|npm ERR!"
    r"|(?m:^make(?:\[\d+\])?: \*\*\*)"
    r"|BUILD FAILED"
    r"|compilation terminated\."
)


def masked_failure_hint(command: str, output: str) -> str | None:
    """On exit 0: warn when a pipe tail or an ``||`` fallback may have hidden a real failure."""
    window = output[:HINT_SCAN_CHARS]
    if not command or not window or _base_command(command) in _READ_ONLY_HEADS:
        return None
    if not _FAILURE_TEXT.search(window):
        return None
    if _MASKING_TAIL.search(command):
        return (
            "Exit code 0 belongs to the last command of the pipe (tail, head, ...), not to the "
            "command before it, and the output shows a failure. Treat this run as failed: run "
            "the command again without the pipe to get its own exit code (long output is cut "
            "and saved to a file automatically)."
        )
    if _MASKING_FALLBACK.search(command):
        return (
            "Exit code 0 comes from the || fallback, not from the command before it, and the "
            "output shows a failure. Treat this run as failed: run the command on its own to get "
            "its real exit code."
        )
    return None


_NOT_FOUND = re.compile(r"(?:^|\n)(?:bash: (?:line \d+: )?)?([\w.+-]+): command not found")
_NO_MODULE = re.compile(r"(?:ModuleNotFoundError|ImportError): No module named '?([\w.]+)")


def failure_hint(command: str, code: int, output: str) -> str | None:
    """One next step for a common failure, or None."""
    window = output[:HINT_SCAN_CHARS]
    missing = _NOT_FOUND.search(window)
    if missing:
        name = missing.group(1)
        if name in ("python", "pip"):
            return (
                f"There is no plain `{name}` here: use `{name}3`, or the project's virtual "
                f"environment (for example .venv/bin/{name})."
            )
        return (
            f"`{name}` is not installed or not on PATH. Check with `command -v {name}`, then "
            "install it or call it by its full path instead of retrying."
        )
    module = _NO_MODULE.search(window)
    if module:
        return (
            f"Python cannot import '{module.group(1)}'. Usually the wrong interpreter runs: use "
            "the project's virtual environment before installing anything."
        )
    if "Permission denied" in window:
        return (
            "Permission denied: check the owner and mode of the path (ls -l) or use a location "
            "you can write to; use sudo only when the task really needs it."
        )
    if code == 126:
        return (
            "Exit 126: the file exists but cannot be executed; make it executable or run it "
            "with its interpreter."
        )
    if code == 124:
        return (
            "Exit 124: the command ran out of time. Give it a larger timeout (foreground maximum "
            "600 s) or run it with background=true and notify=true."
        )
    return None


# -- finishing a run ------------------------------------------------------------------------------


def _redact_file(path: str, command: str, scope: Scope | None) -> bool:
    """Rewrite the overflow log with secrets masked (force mode); False when that failed and the
    file was removed."""
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
        clean = redact_output(strip_ansi(raw), command=command, force=True, scope=scope)
        fd, temp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(clean)
            os.chmod(temp, 0o600)
            os.replace(temp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise
        return True
    except Exception:
        log.warning("could not mask the full-output file; it was removed", exc_info=True)
        with contextlib.suppress(OSError):
            target.unlink()
        return False


def _changed_cwd(observed: str | None, start: str | None) -> str | None:
    if not observed or not start:
        return None
    try:
        if os.path.realpath(observed) == os.path.realpath(start):
            return None
    except OSError:
        return None
    return observed


def finish(
    command: str,
    result: ExecResult,
    *,
    scope: Scope | None,
    start_cwd: str | None,
    workdir: str | None,
    approval_note: str | None = None,
    max_chars: int = 50_000,
) -> str:
    """The JSON for a command that ran (see the module notes)."""
    output = result.output
    # The sudo layer (P05-10) annotates the output itself and flags the result.
    auth_failed = result.sudo_auth_failed
    cache_cleared = result.sudo_cache_cleared
    output = head_tail(output, max_chars)
    output = strip_ansi(output)
    output = redact_output(output.strip(), command=command, scope=scope) if output else ""
    code = result.returncode
    interrupted = code == INTERRUPTED_RC and INTERRUPT_MARK in output
    meaning = None if interrupted else exit_code_meaning(command, code)
    if code == 0:
        hint = masked_failure_hint(command, output)
    elif meaning is None and not interrupted:
        hint = failure_hint(command, code, output)
    else:
        hint = None
    note = approval_note
    if note and interrupted:
        note = note.rstrip(".") + ", then interrupted."
    spill: list[tuple[str, Any]] = []
    if result.full_output_path and _redact_file(result.full_output_path, command, scope):
        spill = [
            ("full_output_path", result.full_output_path),
            ("output_total_chars", result.output_total_chars),
        ]
    body: dict[str, Any] = {"output": output, "exit_code": code, "error": None}
    optional: list[tuple[str, Any]] = [
        ("cwd", None if workdir else _changed_cwd(result.cwd_observed, start_cwd)),
        ("environment_recreated", ENV_RECREATED_NOTE if result.environment_recreated else None),
        *spill,
        ("approval", note or None),
        ("exit_code_meaning", meaning),
        ("hint", hint),
        ("sudo_auth_failed", True if auth_failed else None),
        ("sudo_cache_cleared", True if cache_cleared else None),
    ]
    for key, value in optional:
        if value is not None:
            body[key] = value
    return _json(body)


# -- running --------------------------------------------------------------------------------------


async def _backoff(clock: Clock, seconds: float, cancel: CancelSignal | None) -> bool:
    """Sleep ``seconds`` on ``clock``; True when the cancel signal was set meanwhile."""
    left = seconds
    while left > 0:
        if cancel is not None and cancel.is_set():
            return True
        step = min(BACKOFF_SLICE_S, left)
        await clock.sleep(step)
        left -= step
    return cancel is not None and cancel.is_set()


def _says_timeout(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, TimeoutError) or "timeout" in text or "timed out" in text


async def run_foreground(
    environments: EnvironmentRegistry,
    command: str,
    *,
    scope: Scope,
    session_id: str | None,
    timeout: float,
    cwd: str | None,
    clock: Clock,
    cancel: CancelSignal | None = None,
    broker: object | None = None,
    turn_id: str | None = None,
    interactive: bool = True,
    delays: tuple[float, ...] = RETRY_DELAYS,
) -> ExecResult | BackendStatus | str:
    """Run ``command`` (see the module notes). A ``str`` is a finished error result.
    ``interactive`` false (unattended runs) leaves the sudo hook headless: it never prompts."""
    for attempt in range(len(delays) + 1):
        try:
            with use_sudo_caller(
                broker=cast(ServerRequestPort | None, broker),
                turn_id=turn_id,
                interactive=interactive,
            ):
                return await environments.run(
                    command,
                    scope=scope,
                    session_id=session_id,
                    timeout=timeout,
                    cwd=cwd,
                    bounded=True,
                    cancel=cancel,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _says_timeout(exc):
                return timed_out(timeout)
            text = redact_output(f"{type(exc).__name__}: {exc}", scope=scope)
            if attempt >= len(delays):
                log.error("command failed after %d retries: %s", len(delays), text)
                return failed(f"Command execution failed: {text}")
            log.warning(
                "backend error, retry %d of %d in %gs: %s",
                attempt + 1,
                len(delays),
                delays[attempt],
                text,
            )
            if await _backoff(clock, delays[attempt], cancel):
                return ExecResult(
                    output=INTERRUPT_MARK, returncode=INTERRUPTED_RC, interrupted=True
                )
    raise AssertionError("unreachable")  # pragma: no cover
