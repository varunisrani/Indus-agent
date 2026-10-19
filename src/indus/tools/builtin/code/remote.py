# pyright: strict
"""``execute_code`` on a backend that is not this machine (P10-11).

All we can do there is ``env.execute``. Every internal command runs from ``/`` with
``bounded=False`` (R02.2: a tool result or cell output is never cut by the backend's capture),
and files are shipped through stdin, base64-encoded and decoded by the remote ``python3`` — never
on a command line, where other users could read them.

- ``run_remote`` first checks that ``python3`` exists there. With kernels on it takes the
  owner's remote kernel from ``POOL`` (starting one when needed); when no kernel can start it
  ships one script for this call instead.
- A kernel lives in a private directory (``umask 077``, mode 700) in the backend's temp dir:
  ``runner.py``, ``indus_tools.py`` (file transport), ``cells/`` and ``rpc/``. It is started
  detached and must write ``ready`` within ``START_WAIT_S``.
- One poll round trip every ``REMOTE_POLL_S`` (0.5 s, on the scope's clock) takes pending tool
  requests (``rpc/req_<n>.json``), the cell result when it is there, and a liveness check.
  Requests go through the same ``RpcPipeline`` as local calls; answers are written back as
  ``rpc/res_<n>.json`` (temp name, then rename).
- A timeout or an interrupt stops the runner (TERM to its group, KILL after 5 s) and removes its
  directory; the next call says the state was lost.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import secrets
import shlex
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from indus.backends.contract import CancelSignal, Environment, ExecResult
from indus.foundation.clock import Clock, resolve_zone
from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.tools.builtin.code.kernel import KernelKey, KernelPool
from indus.tools.builtin.code.remote_runner import SOURCE as RUNNER_SOURCE
from indus.tools.builtin.code.rpc import RpcPipeline, decode_request
from indus.tools.builtin.code.runner import RunOutcome, timeout_message
from indus.tools.builtin.code.settings import (
    CELL_CAPTURE_BYTES,
    KILL_GRACE_S,
    MODULE_NAME,
    REMOTE_POLL_S,
    TOOL_ERRORS_SHOWN,
    CodeSettings,
    Mode,
)
from indus.tools.builtin.code.stub_module import ENV_DIR, ENV_TOKEN, module_source

log = logging.getLogger(__name__)

POLL_S = REMOTE_POLL_S  # tests may shorten it; the default is the plan's 0.5 s
START_WAIT_S = 15.0
SHORT_TIMEOUT_S = 30.0
SHIP_TIMEOUT_S = 120.0
INTERPRETERS = ("python3", "python")
PYTHON_CHECK = (
    "for p in " + " ".join(INTERPRETERS) + '; do command -v "$p" >/dev/null 2>&1 && '
    "\"$p\" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null && "
    'echo "INDUS-PY:$p"; done; true'
)
NO_PYTHON = (
    "execute_code needs Python 3.8 or newer (python3) on the {backend} backend, and there is none "
    "there. Install Python 3 in that environment, or use the tools directly."
)


class RemoteSetupError(IndusError):
    default_code = "tools.code_remote_setup"


# -- remote shell helpers --------------------------------------------------------------------------


async def sh(
    env: Environment,
    command: str,
    *,
    timeout: float = SHORT_TIMEOUT_S,
    stdin: str | None = None,
    cancel: CancelSignal | None = None,
) -> ExecResult:
    return await env.execute(
        command, timeout=timeout, cwd="/", stdin=stdin, bounded=False, cancel=cancel
    )


async def checked(
    env: Environment,
    command: str,
    what: str,
    *,
    timeout: float = SHORT_TIMEOUT_S,
    stdin: str | None = None,
) -> ExecResult:
    result = await sh(env, command, timeout=timeout, stdin=stdin)
    if result.returncode != 0:
        raise RemoteSetupError(f"{what} failed (exit {result.returncode}): {result.output[-500:]}")
    return result


_DECODE = (
    "import base64,os,sys;p=sys.argv[1];t=p+'.part';"
    "d=base64.b64decode(sys.stdin.buffer.read());"
    "f=os.open(t,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);os.write(f,d) if d else None;"
    "os.close(f);os.replace(t,p)"
)


async def ship(env: Environment, path: str, text: str, *, python: str = "python3") -> None:
    """Write ``text`` owner-only to ``path`` on the backend (temp name, then rename)."""
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    command = f"umask 077 && {python} -c {shlex.quote(_DECODE)} {shlex.quote(path)}"
    await checked(env, command, f"writing {path}", timeout=SHIP_TIMEOUT_S, stdin=payload)


async def remote_temp_dir(env: Environment) -> str:
    declared = getattr(env, "temp_dir", None)
    if isinstance(declared, str) and declared.startswith("/"):
        return declared.rstrip("/") or "/"
    snapshot = getattr(env, "snapshot_path", None)
    if isinstance(snapshot, str) and snapshot.startswith("/"):
        return os.path.dirname(snapshot) or "/"
    asked = await sh(env, 'printf %s "${TMPDIR:-/tmp}"')
    value = asked.output.strip()
    return value if value.startswith("/") else "/tmp"  # noqa: S108 - the remote's temp dir


def private_dirs_command(root: str, *inside: str) -> str:
    made = " ".join(shlex.quote(p) for p in (root, *inside))
    return f"umask 077 && mkdir -p {made} && chmod 700 {made}"


def env_file(values: Mapping[str, str]) -> str:
    return "".join(f"{name}={shlex.quote(value)}\n" for name, value in values.items())


def run_env(scope: Scope, folder: str, token: str, extra: Mapping[str, str]) -> dict[str, str]:
    values = {
        ENV_DIR: f"{folder}/rpc",
        ENV_TOKEN: token,
        "PYTHONPATH": folder,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        **extra,
    }
    zone = resolve_zone(scope)
    if zone is not None:
        values["TZ"] = zone.key
    return values


def launch_command(folder: str, cwd: str, env_name: str, program: str) -> str:
    """Source the env file inside a subshell (its names never reach the backend's session
    snapshot), delete it, then run ``program`` from ``cwd``."""
    env_path = shlex.quote(f"{folder}/{env_name}")
    return (
        f"cd {shlex.quote(cwd)} && ( set -a && . {env_path} && set +a && rm -f {env_path} && "
        f"{program} )"
    )


# -- the tool-request poll -------------------------------------------------------------------------

_MARK_REQ = "@@INDUS-REQ "
_MARK_CELL = "@@INDUS-CELL"
_MARK_DEAD = "@@INDUS-DEAD"
_MARK_GONE = "@@INDUS-GONE"


@dataclass
class Tick:
    requests: list[object]
    cell: dict[str, Any] | None = None
    cell_broken: bool = False
    dead: bool = False
    gone: bool = False


def tick_command(folder: str, *, cell_seq: int | None = None, pid: str | None = None) -> str:
    q = shlex.quote(folder)
    parts = [
        f"cd {q} 2>/dev/null || {{ echo {_MARK_GONE}; exit 0; }}",
        'for f in rpc/req_*.json; do [ -f "$f" ] || continue; '
        f'printf \'{_MARK_REQ}%s\\n\' "$f"; cat "$f"; printf \'\\n\'; rm -f "$f"; done',
    ]
    if cell_seq is not None:
        name = f"cells/res_{cell_seq:06d}.json"
        parts.append(
            f"if [ -f {name} ]; then echo {_MARK_CELL}; cat {name}; printf '\\n'; rm -f {name}; fi"
        )
    if pid is not None:
        parts.append(f"kill -0 {shlex.quote(pid)} 2>/dev/null || echo {_MARK_DEAD}")
    return "; ".join(parts)


def parse_tick(output: str) -> Tick:
    tick = Tick([])
    lines = output.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\r")
        if line.startswith(_MARK_REQ) and index + 1 < len(lines):
            tick.requests.append(decode_request(lines[index + 1].encode("utf-8")))
            index += 2
            continue
        if line == _MARK_CELL and index + 1 < len(lines):
            try:
                parsed = json.loads(lines[index + 1])
            except ValueError:
                tick.cell_broken = True
            else:
                if isinstance(parsed, dict):
                    tick.cell = {str(k): v for k, v in parsed.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
                else:
                    tick.cell_broken = True
            index += 2
            continue
        if line == _MARK_DEAD:
            tick.dead = True
        elif line == _MARK_GONE:
            tick.gone = True
        index += 1
    return tick


async def answer_requests(
    env: Environment, folder: str, tick: Tick, pipeline: RpcPipeline, *, python: str = "python3"
) -> None:
    for request in tick.requests:
        if not isinstance(request, dict):
            continue  # unreadable: removed, never answered
        body = cast(dict[str, Any], request)
        seq = body.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            continue
        answer = await pipeline.handle(body)
        await ship(
            env,
            f"{folder}/rpc/res_{seq:06d}.json",
            json.dumps(answer, ensure_ascii=False),
            python=python,
        )


# -- the remote kernel -----------------------------------------------------------------------------


class KernelStartFailed(IndusError):
    default_code = "tools.code_kernel_start"


class RemoteKernel:
    def __init__(self, env: Environment, folder: str, pid: str, token: str, clock: Clock) -> None:
        self.env = env
        self.folder = folder
        self.pid = pid
        self.token = token
        self.clock = clock
        self.last_used = clock.monotonic()
        self.busy = False
        self.pinned = False
        self.seq = 0
        self.count = 0
        self._closed = False

    async def alive(self) -> bool:
        if self._closed:
            return False
        try:
            probe = await sh(self.env, f"kill -0 {shlex.quote(self.pid)} 2>/dev/null && echo ALIVE")
        except Exception:
            return False  # a dropped link and a dead runner get the same answer: start again
        return "ALIVE" in probe.output

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await stop_remote(self.env, self.pid, self.folder)

    def force_close(self) -> None:
        self._closed = True  # needs the backend; the runner exits when its directory goes


async def stop_remote(env: Environment, pid: str, folder: str) -> None:
    """TERM the runner's group, KILL whatever is left after ``KILL_GRACE_S``, remove the dir."""
    p = shlex.quote(pid)
    rounds = int(KILL_GRACE_S * 10)
    command = (
        f"kill -TERM -- -{p} 2>/dev/null; kill -TERM {p} 2>/dev/null; "
        f"i=0; while kill -0 {p} 2>/dev/null && [ $i -lt {rounds} ]; "
        "do sleep 0.1; i=$((i+1)); done; "
        f"kill -KILL -- -{p} 2>/dev/null; kill -KILL {p} 2>/dev/null; "
        f"rm -rf {shlex.quote(folder)}; true"
    )
    try:
        await sh(env, command, timeout=KILL_GRACE_S + SHORT_TIMEOUT_S)
    except Exception:
        log.warning("could not stop the remote kernel %s in %s", pid, folder, exc_info=True)


async def start_remote_kernel(
    scope: Scope,
    env: Environment,
    *,
    cwd: str | None,
    tools: tuple[str, ...],
    idle_s: float,
    clock: Clock,
) -> RemoteKernel:
    root = await remote_temp_dir(env)
    folder = f"{root}/indus-rkernel-{uuid.uuid4().hex[:12]}"
    pid = ""
    try:
        await checked(
            env,
            private_dirs_command(folder, f"{folder}/cells", f"{folder}/rpc"),
            "making the kernel directory",
        )
        token = secrets.token_urlsafe(32)
        await ship(env, f"{folder}/runner.py", RUNNER_SOURCE)
        await ship(env, f"{folder}/{MODULE_NAME}.py", module_source(tools, "file"))
        values = run_env(
            scope,
            folder,
            token,
            {
                "INDUS_KERNEL_DIR": folder,
                "INDUS_KERNEL_IDLE_S": str(idle_s),
                "INDUS_KERNEL_CAPTURE": str(CELL_CAPTURE_BYTES),
            },
        )
        await ship(env, f"{folder}/kernel.env", env_file(values))
        log_file = shlex.quote(folder + "/runner.log")
        # Only the interpreter goes to the background (``{ ... & }``), so ``$!`` is its pid and
        # nothing left running holds this command's output pipe.
        program = (
            f": > {log_file} && "
            f"{{ python3 {shlex.quote(folder + '/runner.py')} > {log_file} 2>&1 < /dev/null & }} "
            '&& echo "INDUS-PID:$!"'
        )
        started = await sh(env, launch_command(folder, cwd or folder, "kernel.env", program))
        pid = next(
            (
                line.strip().removeprefix("INDUS-PID:")
                for line in started.output.splitlines()
                if line.strip().startswith("INDUS-PID:")
            ),
            "",
        )
        if not pid.isdigit():
            raise KernelStartFailed(f"the remote kernel did not start: {started.output[-300:]!r}")
        deadline = clock.monotonic() + START_WAIT_S
        ready = f"{folder}/ready"
        while True:
            check = await sh(
                env,
                f"[ -f {shlex.quote(ready)} ] && echo READY; "
                f"kill -0 {shlex.quote(pid)} 2>/dev/null || echo DEAD",
            )
            if "READY" in check.output:
                return RemoteKernel(env, folder, pid, token, clock)
            if "DEAD" in check.output or clock.monotonic() >= deadline:
                raise KernelStartFailed("the remote kernel stopped before it was ready")
            await clock.sleep(POLL_S)
    except BaseException:
        if pid.isdigit():
            await stop_remote(env, pid, folder)
        else:
            try:
                await sh(env, f"rm -rf {shlex.quote(folder)}")
            except Exception:
                log.debug("could not remove %s", folder, exc_info=True)
        raise


POOL: KernelPool[RemoteKernel] = KernelPool("remote")


# -- one call --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteJob:
    code: str
    scope: Scope
    env: Environment
    env_key: str
    owner: str
    mode: Mode
    tools: tuple[str, ...]
    settings: CodeSettings
    reset: bool
    pipeline: RpcPipeline
    clock: Clock
    cancel: CancelSignal | None = None


def _cwd(job: RemoteJob) -> str | None:
    return job.env.cwd if job.mode == "project" else None


async def run_remote(job: RemoteJob) -> RunOutcome:
    """See the module notes. Raises ``RemoteSetupError`` when python3 is missing."""
    started = job.clock.monotonic()
    probe = await sh(job.env, PYTHON_CHECK)
    found = [
        line.strip().removeprefix("INDUS-PY:")
        for line in probe.output.splitlines()
        if line.strip().startswith("INDUS-PY:")
    ]
    usable = [name for name in INTERPRETERS if name in found]
    if not usable:
        raise RemoteSetupError(NO_PYTHON.format(backend=job.env.name))
    outcome: RunOutcome | None = None
    if job.settings.kernel == "auto" and "python3" in usable:
        try:
            outcome = await _run_in_kernel(job)
        except KernelStartFailed as exc:
            log.warning(
                "remote kernel unavailable on %s (%s); shipping a script", job.env.name, exc
            )
        except RemoteSetupError as exc:
            log.warning(
                "remote kernel setup failed on %s (%s); shipping a script", job.env.name, exc
            )
    if outcome is None:
        outcome = await _run_shipped(job, usable[0])
    outcome.seconds = max(0.0, job.clock.monotonic() - started)
    return outcome


async def _run_in_kernel(job: RemoteJob) -> RunOutcome:
    key = KernelKey(
        job.owner, f"{job.env.name}:{job.env_key}", job.mode, "python3", _cwd(job) or "", job.tools
    )

    async def make() -> RemoteKernel:
        return await start_remote_kernel(
            job.scope,
            job.env,
            cwd=_cwd(job),
            tools=job.tools,
            idle_s=job.settings.kernel_idle_s,
            clock=job.clock,
        )

    taken = await POOL.take(
        key,
        clock=job.clock,
        reset=job.reset,
        idle_s=job.settings.kernel_idle_s,
        cap=job.settings.max_kernels,
        make=make,
    )
    kernel = taken.kernel
    info = taken.info("remote")
    released = False
    job.pipeline.token = kernel.token
    try:
        q = shlex.quote(kernel.folder)
        await sh(job.env, f"rm -f {q}/rpc/req_* {q}/rpc/res_*")  # nothing left from a past call
        kernel.seq += 1
        seq = kernel.seq
        try:
            await ship(
                job.env,
                f"{kernel.folder}/cells/req_{seq:06d}.json",
                json.dumps({"id": uuid.uuid4().hex, "code": job.code}, ensure_ascii=False),
            )
        except RemoteSetupError:
            await POOL.discard(key, kernel)
            released = True
            raise KernelStartFailed("the call could not be handed to the remote kernel") from None
        state, reply = await _poll_cell(job, kernel, seq)
        outcome = RunOutcome(
            status="success",
            exit_code=0,
            tool_calls_made=job.pipeline.calls_made,
            tool_errors=job.pipeline.tool_errors(TOOL_ERRORS_SHOWN),
            kernel=info,
            show_stderr=True,
        )
        if state in ("timeout", "interrupted", "died"):
            await POOL.discard(key, kernel)
            released = True
            info["ended"] = True
            outcome.exit_code = -1
            if state == "timeout":
                outcome.status = "timeout"
                outcome.error = timeout_message(job.settings.timeout_s, kernel=True)
            elif state == "interrupted":
                outcome.status = "interrupted"
                outcome.error = (
                    "Interrupted: the remote session kernel was stopped; its state is gone."
                )
            else:
                outcome.status = "error"
                outcome.error = "The remote session kernel stopped while running the call."
            return outcome
        kernel.count = int(reply.get("count", kernel.count + 1) or 0)
        info["execution_count"] = kernel.count
        outcome.stdout = str(reply.get("stdout", ""))
        outcome.stderr = str(reply.get("stderr", ""))
        trace = str(reply.get("traceback", ""))
        cell_state = str(reply.get("state", "ok"))
        if reply.get("stdout_cut"):
            info["capture_cut"] = (
                f"The call printed more than {CELL_CAPTURE_BYTES:,} bytes; only the first part "
                "was kept."
            )
        if cell_state == "error":
            outcome.status, outcome.exit_code = "error", 1
            outcome.error = trace.strip().splitlines()[-1] if trace.strip() else "The cell raised."
            outcome.stderr += trace
            outcome.hint_text = trace
        elif cell_state == "exit":
            info["ended"] = True
            await POOL.discard(key, kernel, lost=False)
            released = True
        return outcome
    finally:
        if not released:
            POOL.give_back(kernel, job.clock)


async def _poll_cell(job: RemoteJob, kernel: RemoteKernel, seq: int) -> tuple[str, dict[str, Any]]:
    deadline = job.clock.monotonic() + job.settings.timeout_s
    command = tick_command(kernel.folder, cell_seq=seq, pid=kernel.pid)
    while True:
        try:
            result = await sh(job.env, command)
        except Exception:
            log.debug("remote kernel poll failed once", exc_info=True)
            result = None
        if result is not None:
            tick = parse_tick(result.output)
            await answer_requests(job.env, kernel.folder, tick, job.pipeline)
            if tick.cell is not None:
                return "done", tick.cell
            if tick.cell_broken or tick.dead or tick.gone:
                return "died", {}
        if job.cancel is not None and job.cancel.is_set():
            return "interrupted", {}
        if job.clock.monotonic() >= deadline:
            return "timeout", {}
        await job.clock.sleep(POLL_S)


# -- shipping one script ---------------------------------------------------------------------------


async def _run_shipped(job: RemoteJob, python: str) -> RunOutcome:
    root = await remote_temp_dir(job.env)
    folder = f"{root}/indus-rscript-{uuid.uuid4().hex[:12]}"
    stop_polling = False

    async def serve() -> None:
        command = tick_command(folder)
        while not stop_polling:
            try:
                result = await sh(job.env, command)
            except Exception:
                log.debug("remote request poll failed once", exc_info=True)
            else:
                await answer_requests(
                    job.env, folder, parse_tick(result.output), job.pipeline, python=python
                )
            await job.clock.sleep(POLL_S)

    poller: asyncio.Task[None] | None = None
    try:
        await checked(
            job.env, private_dirs_command(folder, f"{folder}/rpc"), "making the script directory"
        )
        token = secrets.token_urlsafe(32)
        job.pipeline.token = token
        await ship(
            job.env, f"{folder}/{MODULE_NAME}.py", module_source(job.tools, "file"), python=python
        )
        await ship(job.env, f"{folder}/script.py", job.code, python=python)
        await ship(
            job.env,
            f"{folder}/run.env",
            env_file(run_env(job.scope, folder, token, {})),
            python=python,
        )
        poller = asyncio.ensure_future(serve())
        program = f"exec {python} {shlex.quote(folder + '/script.py')}"
        result = await job.env.execute(
            launch_command(folder, _cwd(job) or folder, "run.env", program),
            timeout=job.settings.timeout_s,
            cwd="/",
            bounded=False,
            cancel=job.cancel,
        )
    finally:
        stop_polling = True
        if poller is not None:
            poller.cancel()
            with contextlib.suppress(BaseException):  # its own cancellation, or a poll error
                await poller
        try:
            await sh(job.env, f"rm -rf {shlex.quote(folder)}")
        except Exception:
            log.debug("could not remove %s", folder, exc_info=True)
    outcome = RunOutcome(
        status="success",
        stdout=result.output,
        exit_code=result.returncode,
        tool_calls_made=job.pipeline.calls_made,
        tool_errors=job.pipeline.tool_errors(TOOL_ERRORS_SHOWN),
        kernel={"mode": "script", "remote": True},
    )
    if result.timed_out:
        outcome.status, outcome.exit_code = "timeout", -1
        outcome.error = timeout_message(job.settings.timeout_s)
    elif result.interrupted:
        outcome.status, outcome.exit_code = "interrupted", -1
        outcome.error = "Interrupted: the script was stopped."
    elif result.returncode != 0:
        outcome.status = "error"
        outcome.error = f"The script exited with code {result.returncode}."
        outcome.hint_text = result.output
    return outcome


__all__ = [
    "POLL_S",
    "POOL",
    "KernelStartFailed",
    "RemoteJob",
    "RemoteKernel",
    "RemoteSetupError",
    "answer_requests",
    "parse_tick",
    "remote_temp_dir",
    "run_remote",
    "ship",
    "start_remote_kernel",
    "stop_remote",
    "tick_command",
]
