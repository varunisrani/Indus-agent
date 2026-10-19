# pyright: strict
"""Script mode: one fresh Python child per ``execute_code`` call on this machine (P10-08).

``run_script`` stages ``indus_tools.py`` and ``script.py`` in a private directory, opens the RPC
socket with a new token, starts the child, waits on the scope's clock (the turn's stop signal and
``timeout`` checked each round), and on every way out — finished, timed out, interrupted, an
error of ours — stops the child's whole process tree, closes the socket and removes the
directory. ``finish_result`` turns what happened into the tool's JSON result; the kernel and
remote paths use it too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from indus.backends.contract import CancelSignal
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.tools.builtin.code import launch
from indus.tools.builtin.code import output as out
from indus.tools.builtin.code.rpc import RpcPipeline, SocketRpcServer, ToolInvoker
from indus.tools.builtin.code.schema import failure_hint
from indus.tools.builtin.code.settings import (
    MODULE_NAME,
    SPILL_MAX_BYTES,
    STDERR_BYTES,
    TOOL_ERRORS_SHOWN,
    CodeSettings,
    Mode,
)
from indus.tools.builtin.code.stub_module import ENV_SOCKET, ENV_TOKEN, module_source

log = logging.getLogger(__name__)

type Status = Literal["success", "error", "timeout", "interrupted"]

POLL_START_S = 0.01
POLL_MAX_S = 0.1
INTERRUPTED_NOTE = "[execution interrupted]"


@dataclass
class RunOutcome:
    """What one run (script, kernel cell or remote call) produced, before presentation."""

    status: Status
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    error: str | None = None
    tool_calls_made: int = 0
    tool_errors: list[dict[str, str]] = field(default_factory=lambda: [])
    seconds: float = 0.0
    kernel: dict[str, Any] | None = None
    show_stderr: bool | None = None  # None: only for a non-zero exit
    hint_text: str = ""  # where failure hints look (stderr or the traceback)


def timeout_message(timeout: float, *, kernel: bool = False) -> str:
    if kernel:
        return (
            f"The call ran past its {timeout:g}s limit. The session kernel was stopped and its "
            "state is gone; the next call starts a fresh one."
        )
    return f"The script ran past its {timeout:g}s limit and was stopped."


def finish_result(outcome: RunOutcome, scope: Scope | None, tools: Iterable[str]) -> str:
    """The tool's JSON result (see the package notes for the fields)."""
    stdout = out.clean(outcome.stdout, scope)
    shown = out.present_stdout(stdout, scope)
    text = shown.text
    show_stderr = outcome.show_stderr
    if show_stderr is None:
        show_stderr = outcome.exit_code not in (0, None)
    if show_stderr and outcome.stderr:
        text = out.with_stderr(text, out.present_stderr(out.clean(outcome.stderr, scope)))
    if outcome.status == "timeout" and outcome.error:
        text = f"{text}\n\n[{outcome.error}]" if text else f"[{outcome.error}]"
    elif outcome.status == "interrupted":
        text = f"{text}\n{INTERRUPTED_NOTE}" if text else INTERRUPTED_NOTE
    result: dict[str, Any] = {
        "status": outcome.status,
        "output": text,
        "tool_calls_made": outcome.tool_calls_made,
        "duration_seconds": round(outcome.seconds, 2),
    }
    if outcome.exit_code is not None:
        result["exit_code"] = outcome.exit_code
    if outcome.error and outcome.status != "success":
        result["error"] = out.clean(outcome.error, scope)
    if outcome.tool_errors:
        result["tool_errors"] = outcome.tool_errors[:TOOL_ERRORS_SHOWN]
    if outcome.kernel is not None:
        result["kernel"] = outcome.kernel
    result.update(shown.fields)
    if outcome.status == "error":
        hint = failure_hint(outcome.hint_text or outcome.stderr, tools)
        if hint:
            result["hint"] = hint
    return json.dumps(result, ensure_ascii=False)


def error_result(message: str, *, user_summary: str | None = None, calls: int = 0) -> str:
    body: dict[str, Any] = {
        "status": "error",
        "error": message,
        "tool_calls_made": calls,
        "duration_seconds": 0.0,
    }
    if user_summary:
        body["user_summary"] = user_summary
    return json.dumps(body, ensure_ascii=False)


def stdout_text(capture: out.ByteCapture) -> str:
    """The captured stdout; past the kept head, a note and the stream's last bytes."""
    text = out.decode(capture.kept())
    if capture.overflowed:
        missing = capture.total - len(capture.kept())
        tail = out.decode(capture.last(30_000))
        text += f"\n[... {missing:,} more bytes were printed; only the end is kept ...]\n{tail}"
    return text


@dataclass(frozen=True)
class ScriptJob:
    code: str
    scope: Scope
    session_id: str | None
    tools: tuple[str, ...]
    settings: CodeSettings
    interpreter: str
    cwd: str | None  # None: the staging directory (strict mode)
    invoke: ToolInvoker
    clock: Clock
    cancel: CancelSignal | None = None
    mode: Mode = "project"


async def run_script(job: ScriptJob) -> RunOutcome:
    """Run ``job`` as a fresh process (see the module notes)."""
    started = job.clock.monotonic()
    folder = launch.private_dir(launch.staging_root(job.scope))
    server: SocketRpcServer | None = None
    child: launch.Child | None = None
    pipeline: RpcPipeline | None = None
    try:
        launch.write_private(folder / f"{MODULE_NAME}.py", module_source(job.tools, "socket"))
        launch.write_private(folder / "script.py", job.code)
        token = secrets.token_urlsafe(32)
        server = SocketRpcServer(folder)
        pipeline = RpcPipeline(
            token=token,
            allowed=frozenset(job.tools),
            max_calls=job.settings.max_tool_calls,
            invoke=job.invoke,
            clock=job.clock,
        )
        server.attach(pipeline, asyncio.get_running_loop())
        env = launch.child_environment(
            job.scope,
            session_id=job.session_id,
            staging=folder,
            extra={ENV_SOCKET: server.endpoint, ENV_TOKEN: token},
        )
        stdout = out.ByteCapture(SPILL_MAX_BYTES)
        stderr = out.ByteCapture(STDERR_BYTES * 4, tail=STDERR_BYTES)
        child = launch.Child(
            [job.interpreter, str(folder / "script.py")],
            cwd=job.cwd or str(folder),
            env=env,
            stdout=stdout,
            stderr=stderr,
        )
        status, code = await _wait(child, job)
        if status != "success":
            await child.stop(job.clock)
        await child.finish_reading(job.clock)
        outcome = RunOutcome(
            status=status,
            stdout=stdout_text(stdout),
            stderr=out.decode(stderr.last(STDERR_BYTES * 4)),
            exit_code=code if status == "success" else -1,
            tool_calls_made=pipeline.calls_made,
            tool_errors=pipeline.tool_errors(TOOL_ERRORS_SHOWN),
        )
        if status == "success" and code != 0:
            outcome.status = "error"
            outcome.error = f"The script exited with code {code}."
        elif status == "timeout":
            outcome.error = timeout_message(job.settings.timeout_s)
        elif status == "interrupted":
            outcome.error = "Interrupted: the script was stopped."
        outcome.hint_text = outcome.stderr
        outcome.seconds = max(0.0, job.clock.monotonic() - started)
        return outcome
    finally:
        if child is not None and child.alive():
            child.force_stop()
        if server is not None:
            server.close()
        shutil.rmtree(folder, ignore_errors=True)


async def _wait(child: launch.Child, job: ScriptJob) -> tuple[Status, int | None]:
    deadline = job.clock.monotonic() + job.settings.timeout_s
    delay = POLL_START_S
    while True:
        code = child.poll()
        if code is not None:
            return "success", code
        if job.cancel is not None and job.cancel.is_set():
            return "interrupted", None
        if job.clock.monotonic() >= deadline:
            return "timeout", None
        await job.clock.sleep(delay)
        delay = min(delay * 1.5, POLL_MAX_S)


__all__ = [
    "RunOutcome",
    "ScriptJob",
    "error_result",
    "finish_result",
    "run_script",
    "stdout_text",
    "timeout_message",
]
