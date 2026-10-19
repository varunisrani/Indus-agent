"""Job scripts: where they may live, how they run, and the wake gate (P14-09, R02.22).

Scripts live in ``<home>/scripts/`` only. A job's ``script`` value is resolved against that folder
(absolute and ``~`` forms too) and its real path, symlinks followed, must stay inside it.
``.sh``/``.bash`` run under bash; anything else under the job's ``interpreter`` (an absolute path
to a Python executable) or this process's Python. The child environment is
``safety.child_env``'s default variant (no provider keys or other Indus-managed credentials; the
profile's passthrough names resolved through its secret store); the working folder is the job's
workdir, else the scripts folder; the child leads its own session and process group.

Time limit: ``INDUS_CRON_SCRIPT_TIMEOUT`` → ``cron.script_timeout_seconds`` → 3,600 s. On time-out,
on cancel and on any other way out of ``run_script`` the whole process tree is stopped (the local
backend's ordered kill, then SIGKILL for survivors): never just the leader. stdout and stderr are
capped (``STREAM_CAP`` bytes kept each) and redacted before they leave.

Wake gate: when the last non-empty stdout line is a JSON object with a boolean ``wake``,
``{"wake": false}`` skips this tick and ``{"wake": true, "context": {...}}`` hands the context to
the prompt. The gate line is removed from the output.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import stat
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

from indus.backends.local import LocalProcess, force_kill_tree, stop_tree
from indus.cron import settings
from indus.cron.ports import JobSpec
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.platform.locate import which
from indus.safety.child_env import child_env
from indus.safety.redaction import redact_output

__all__ = [
    "STREAM_CAP",
    "ScriptRefused",
    "ScriptResult",
    "check_script_value",
    "parse_wake_gate",
    "resolve_interpreter",
    "resolve_script",
    "run_script",
    "script_argv",
]

log = logging.getLogger(__name__)

STREAM_CAP = 1024 * 1024
POLL_S = 0.05
KILL_GRACE_S = 1.0
READER_JOIN_S = 5.0
WAKE_KEY = "wake"
CONTEXT_KEY = "context"
SHELL_SUFFIXES = frozenset({".sh", ".bash"})
_COMMAND_CHARS = frozenset(" \t\r\n|;&<>$`'\"(){}*?!\\")
_PYTHON_NAME = re.compile(r"^(?:python(?:\d+(?:\.\d+)?)?|pypy3?)(?:\.exe)?$", re.IGNORECASE)


class ScriptRefused(ValueError):
    """The script value or interpreter cannot be run (the message says why)."""


@dataclass(frozen=True)
class ScriptResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    error: str | None = None
    wake: bool = True
    context: Mapping[str, Any] | None = None
    gated: bool = False  # the output ended with a wake-gate line

    @property
    def nothing_to_report(self) -> bool:
        """Woken by no explicit gate and no output: a pre-run script found nothing."""
        return not self.gated and not self.stdout.strip()

    @property
    def failure_text(self) -> str:
        """What an alert says about a failed script."""
        parts = [self.error or "the script failed"]
        if self.stderr:
            parts.append(f"stderr:\n{self.stderr}")
        if self.stdout:
            parts.append(f"stdout:\n{self.stdout}")
        return "\n\n".join(parts)


# -- create-time and path checks -------------------------------------------------------------------


def check_script_value(value: str) -> str | None:
    """A refusal message when ``value`` looks like a command line rather than a file name."""
    text = value.strip()
    if not text:
        return "the script name is empty"
    if text.startswith("-") or any(ch in _COMMAND_CHARS for ch in text):
        return (
            f"{value!r} looks like a command line, not a script file. Save the commands in a file "
            "in the profile's scripts folder (<home>/scripts/) and give that file's name."
        )
    return None


def resolve_script(home: Path, value: str) -> Path:
    """The real path of ``value`` inside ``<home>/scripts/``; ``ScriptRefused`` otherwise."""
    folder = settings.scripts_dir(home)
    if "\x00" in value:
        raise ScriptRefused("the script name contains a NUL byte")
    try:
        raw = Path(value.strip()).expanduser()
        root = folder.resolve()
        path = (raw if raw.is_absolute() else folder / raw).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ScriptRefused(f"the script path {value!r} cannot be resolved: {exc}") from None
    if not path.is_relative_to(root):
        raise ScriptRefused(
            f"the script {value!r} resolves outside this profile's scripts folder ({root}); "
            "scripts must live there"
        )
    if not path.exists():
        raise ScriptRefused(
            f"script not found: {path} (scripts are looked up only in this profile's folder {root})"
        )
    if not path.is_file():
        raise ScriptRefused(f"the script path is not a file: {path}")
    return path


def resolve_interpreter(value: str) -> str:
    """The checked absolute path of a pinned Python interpreter."""
    raw = Path(value.strip()).expanduser()
    if not raw.is_absolute():
        raise ScriptRefused(
            f"the interpreter must be an absolute path (got {value!r}); a bare name changes "
            "meaning with PATH"
        )
    try:
        mode = raw.stat().st_mode
        names = {raw.name, raw.resolve().name}
    except FileNotFoundError:
        raise ScriptRefused(f"interpreter not found: {raw}") from None
    except (OSError, RuntimeError) as exc:
        raise ScriptRefused(f"the interpreter {value!r} cannot be read: {exc}") from None
    if not stat.S_ISREG(mode):
        raise ScriptRefused(f"the interpreter is not a file: {raw}")
    if os.name != "nt" and not mode & 0o111:
        raise ScriptRefused(f"the interpreter is not executable: {raw}")
    if not all(_PYTHON_NAME.match(name) for name in names):
        raise ScriptRefused(f"the interpreter must be a Python executable: {raw}")
    return str(raw)


def script_argv(path: Path, interpreter: str | None) -> list[str]:
    if path.suffix.lower() in SHELL_SUFFIXES:
        bash = which("bash")
        if bash is None:
            raise ScriptRefused(f"cannot run {path.name}: bash is not on PATH")
        return [bash, str(path)]
    if interpreter:
        return [resolve_interpreter(interpreter), str(path)]
    return [sys.executable, str(path)]


# -- the wake gate --------------------------------------------------------------------------------


def parse_wake_gate(stdout: str) -> tuple[bool, Mapping[str, Any] | None, str]:
    """``(wake, context, output without the gate line)`` (see the module notes)."""
    lines = stdout.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if not line:
            continue
        try:
            gate = json.loads(line)
        except ValueError:
            return True, None, stdout
        if not isinstance(gate, dict) or not isinstance(gate.get(WAKE_KEY), bool):
            return True, None, stdout
        found = cast(dict[str, Any], gate)
        rest = "\n".join(lines[:index] + lines[index + 1 :]).strip()
        if found[WAKE_KEY] is False:
            return False, None, rest
        context = found.get(CONTEXT_KEY)
        return True, cast(dict[str, Any], context) if isinstance(context, dict) else None, rest
    return True, None, stdout


# -- running --------------------------------------------------------------------------------------


class _Reader:
    """Drains one pipe on a daemon thread, keeping the first ``STREAM_CAP`` bytes."""

    def __init__(self, pipe: IO[bytes] | None, name: str) -> None:
        self.data = bytearray()
        self.dropped = 0
        self._pipe = pipe
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        if pipe is not None:
            self._thread.start()

    def _run(self) -> None:
        pipe = self._pipe
        assert pipe is not None
        with contextlib.suppress(OSError, ValueError):
            while chunk := pipe.read(65536):
                room = STREAM_CAP - len(self.data)
                if room > 0:
                    self.data.extend(chunk[:room])
                self.dropped += max(0, len(chunk) - max(room, 0))

    def join(self, seconds: float) -> None:
        if self._pipe is None:
            return
        self._thread.join(seconds)
        if self._thread.is_alive():
            log.warning("a script's output pipe stayed open; output reading abandoned")
            with contextlib.suppress(OSError):
                self._pipe.close()

    def text(self) -> str:
        out = bytes(self.data).decode("utf-8", errors="replace")
        if self.dropped:
            out += f"\n[... {self.dropped} more bytes not kept ...]"
        return out


def _clean(text: str, scope: Scope) -> str:
    try:
        return redact_output(text.strip(), force=True, scope=scope)
    except Exception:
        log.warning("script output redaction failed; output withheld", exc_info=True)
        return "[output withheld: redaction failed]"


def _workdir(job: JobSpec, script_path: Path) -> Path:
    """The job's workdir when usable, else the folder the script sits in."""
    if job.workdir and Path(job.workdir).is_absolute() and Path(job.workdir).is_dir():
        return Path(job.workdir)
    return script_path.parent


async def run_script(
    scope: Scope,
    job: JobSpec,
    *,
    script: str | None = None,
    clock: Clock | None = None,
    timeout: float | None = None,
    cancel: asyncio.Event | None = None,
) -> ScriptResult:
    """Run the job's script (or ``script``) and return what it did; never raises for script
    problems. The process tree is gone when this returns, whichever way it returns."""
    clock = clock or scope.clock
    value = script if script is not None else job.script
    if not value:
        return ScriptResult(ok=False, error="the job has no script")
    try:
        path = resolve_script(scope.home, value)
        argv = script_argv(path, job.interpreter)
    except ScriptRefused as exc:
        return ScriptResult(ok=False, error=str(exc))
    limit = timeout if timeout is not None else settings.script_timeout(scope)
    try:
        popen = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(_workdir(job, path)),
            env=child_env(scope),
            start_new_session=True,
        )
    except OSError as exc:
        return ScriptResult(ok=False, error=f"the script could not start: {exc}")
    proc = LocalProcess(popen)
    out = _Reader(popen.stdout, f"cron script stdout {job.id}")
    err = _Reader(popen.stderr, f"cron script stderr {job.id}")
    timed_out = cancelled = False
    try:
        deadline = clock.monotonic() + limit
        while popen.poll() is None:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            if clock.monotonic() >= deadline:
                timed_out = True
                break
            await clock.sleep(POLL_S)
        if popen.poll() is None:
            await stop_tree(proc, clock=clock, grace=KILL_GRACE_S)
    finally:
        if popen.poll() is None:
            force_kill_tree(proc)
        await _join_readers(out, err)
    stdout, stderr = _clean(out.text(), scope), _clean(err.text(), scope)
    if timed_out:
        return ScriptResult(
            ok=False,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
            error=f"the script timed out after {limit:g} s: {path.name}",
        )
    if cancelled:
        return ScriptResult(
            ok=False, stdout=stdout, stderr=stderr, cancelled=True, error="the run was cancelled"
        )
    code = popen.returncode
    if code != 0:
        return ScriptResult(
            ok=False,
            stdout=stdout,
            stderr=stderr,
            exit_code=code,
            error=f"the script exited with code {code}",
        )
    wake, context, rest = parse_wake_gate(stdout)
    return ScriptResult(
        ok=True,
        stdout=rest,
        stderr=stderr,
        exit_code=0,
        wake=wake,
        context=context,
        gated=rest != stdout or not wake,
    )


async def _join_readers(*readers: _Reader) -> None:
    def join_all() -> None:
        for reader in readers:
            reader.join(READER_JOIN_S)

    try:
        await run_blocking(join_all, label="cron script output")
    except Exception:
        log.warning("could not wait for a script's output", exc_info=True)
