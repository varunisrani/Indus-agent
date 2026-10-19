# pyright: strict
"""Persistent session kernels for ``execute_code`` on this machine (P10-10).

A kernel is one Python child (``kernel_runner.SOURCE``) per ``KernelKey`` — owner, mode,
interpreter, cwd and tool set — that keeps its namespace between calls. ``KernelPool`` holds the
live kernels of the process (local here, remote ones in ``remote``) and applies the same rules to
both:

- one cell at a time per kernel (a second caller waits on the clock);
- ``reset`` closes the old kernel first; a kernel found dead is replaced and the call says the
  state was lost; after a timeout or an interrupt the kernel is closed and the *next* call for
  that key also says so;
- idle kernels (``kernel_idle_timeout`` on the scope's clock, not running a cell) are closed on
  every take and by ``reap_idle`` / ``run_reaper``;
- at most ``max_session_kernels`` unpinned kernels; the least recently used idle one goes first,
  a running one never. Kernels of delegated children (owner ``<parent>::child::<child>``) are
  pinned: outside the count, never evicted or reaped, closed by ``dispose_child`` (R02.32).

Every kernel is closed on every way out: ``shutdown`` at host exit, and an ``atexit`` hook that
force-kills whatever is still alive; the runner also exits by itself when its request pipe closes.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import os
import queue
import secrets
import shutil
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from indus.backends.contract import CancelSignal
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.tools.builtin.code import launch
from indus.tools.builtin.code import output as out
from indus.tools.builtin.code.kernel_runner import SOURCE as RUNNER_SOURCE
from indus.tools.builtin.code.rpc import RpcPipeline, SocketRpcServer
from indus.tools.builtin.code.runner import RunOutcome, timeout_message
from indus.tools.builtin.code.settings import (
    CELL_CAPTURE_BYTES,
    MODULE_NAME,
    STDERR_BYTES,
    TOOL_ERRORS_SHOWN,
    Mode,
)
from indus.tools.builtin.code.stub_module import (
    ENV_PERSISTENT,
    ENV_SOCKET,
    ENV_TOKEN,
    module_source,
)

log = logging.getLogger(__name__)

CHILD_MARK = "::child::"
WAIT_TURN_S = 0.05
POLL_START_S = 0.01
POLL_MAX_S = 0.1
REAPER_FLOOR_S = 30.0
REAPER_CEIL_S = 300.0


def child_owner(parent: str, child: str) -> str:
    """The kernel owner of a delegated child (P10-06 passes it as ``code_kernel_owner``)."""
    return f"{parent}{CHILD_MARK}{child}"


def is_child_owner(owner: str) -> bool:
    return CHILD_MARK in owner


@dataclass(frozen=True)
class KernelKey:
    owner: str
    place: str  # "local", or the remote backend and environment
    mode: Mode
    interpreter: str
    cwd: str
    tools: tuple[str, ...]


class PooledKernel(Protocol):
    last_used: float
    busy: bool
    pinned: bool

    async def alive(self) -> bool: ...

    async def close(self) -> None: ...

    def force_close(self) -> None: ...


@dataclass
class Taken[K: PooledKernel]:
    kernel: K
    reused: bool
    state_reset: bool
    state_lost: bool

    def info(self, mode: str) -> dict[str, Any]:
        info: dict[str, Any] = {"mode": mode, "reused": self.reused}
        if self.state_reset:
            info["state_reset"] = True
        if self.state_lost:
            info["state_lost"] = True
            info["note"] = (
                "The earlier session kernel was gone (stopped after a timeout or interrupt, "
                "ended, or lost); variables from earlier calls are not here. This call started "
                "a fresh one."
            )
        return info


class KernelPool[K: PooledKernel]:
    """See the module notes."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._kernels: dict[KernelKey, K] = {}
        self._lost: set[KernelKey] = set()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._kernels)

    def live_keys(self) -> list[KernelKey]:
        with self._lock:
            return list(self._kernels)

    def get(self, key: KernelKey) -> K | None:
        with self._lock:
            return self._kernels.get(key)

    def counted(self) -> int:
        """Kernels that count against the cap (unpinned)."""
        with self._lock:
            return sum(1 for k in self._kernels.values() if not k.pinned)

    async def take(
        self,
        key: KernelKey,
        *,
        clock: Clock,
        reset: bool,
        idle_s: float,
        cap: int,
        make: Callable[[], Awaitable[K]],
    ) -> Taken[K]:
        """The kernel for ``key``, marked busy (``give_back`` it). Starts one when needed."""
        doomed: list[K] = []
        state_reset = state_lost = False
        while True:
            with self._lock:
                doomed += self._pop_idle(clock.monotonic(), idle_s)
                current = self._kernels.get(key)
                if current is None or not current.busy:
                    if current is not None:
                        current.busy = True
                    break
            await clock.sleep(WAIT_TURN_S)
        try:
            await self._close_all(doomed)
            doomed = []
            if current is not None and reset:
                self._forget(key, current)
                await current.close()
                current, state_reset = None, True
            if current is not None and not await current.alive():
                self._forget(key, current)
                await current.close()
                current, state_lost = None, True
            reused = current is not None
            if current is None:
                fresh = await make()
                fresh.busy = True
                fresh.pinned = is_child_owner(key.owner)
                with self._lock:
                    state_lost = state_lost or (key in self._lost and not state_reset)
                    self._lost.discard(key)
                    self._kernels[key] = fresh
                current = fresh
            current.last_used = clock.monotonic()
            with self._lock:
                doomed = self._over_cap(key, cap)
        except BaseException:
            if current is not None:
                current.busy = False
            raise
        await self._close_all(doomed)
        return Taken(current, reused, state_reset, state_lost)

    def give_back(self, kernel: K, clock: Clock) -> None:
        kernel.last_used = clock.monotonic()
        kernel.busy = False

    async def discard(self, key: KernelKey, kernel: K, *, lost: bool = True) -> None:
        """Close ``kernel`` and drop it; ``lost`` makes the next call for ``key`` say so."""
        self._forget(key, kernel)
        if lost:
            with self._lock:
                self._lost.add(key)
        kernel.busy = False
        await kernel.close()

    def _forget(self, key: KernelKey, kernel: K) -> None:
        with self._lock:
            if self._kernels.get(key) is kernel:
                del self._kernels[key]

    def _pop_idle(self, now: float, idle_s: float) -> list[K]:
        stale = [
            k
            for k, kernel in self._kernels.items()
            if not kernel.busy and not kernel.pinned and now - kernel.last_used > idle_s
        ]
        return [self._kernels.pop(k) for k in stale]

    def _over_cap(self, keep: KernelKey, cap: int) -> list[K]:
        unpinned = [k for k, kernel in self._kernels.items() if not kernel.pinned]
        excess = len(unpinned) - cap
        if excess <= 0:
            return []
        idle = sorted(
            (k for k in unpinned if k != keep and not self._kernels[k].busy),
            key=lambda k: self._kernels[k].last_used,
        )
        return [self._kernels.pop(k) for k in idle[:excess]]

    async def _close_all(self, kernels: list[K]) -> None:
        for kernel in kernels:
            try:
                await kernel.close()
            except Exception:
                log.warning("closing a %s kernel failed", self.label, exc_info=True)

    async def reap_idle(self, clock: Clock, idle_s: float) -> int:
        with self._lock:
            doomed = self._pop_idle(clock.monotonic(), idle_s)
        await self._close_all(doomed)
        return len(doomed)

    async def dispose_where(self, owner_matches: Callable[[str], bool]) -> int:
        with self._lock:
            keys = [k for k in self._kernels if owner_matches(k.owner)]
            doomed = [self._kernels.pop(k) for k in keys]
            self._lost.difference_update(keys)
        await self._close_all(doomed)
        return len(doomed)

    async def dispose_owner(self, owner: str) -> int:
        return await self.dispose_where(lambda o: o == owner)

    async def shutdown(self) -> None:
        with self._lock:
            doomed = list(self._kernels.values())
            self._kernels.clear()
            self._lost.clear()
        await self._close_all(doomed)

    def force_shutdown(self) -> None:
        """Kill everything now, without waiting (process exit)."""
        with self._lock:
            doomed = list(self._kernels.values())
            self._kernels.clear()
        for kernel in doomed:
            with contextlib.suppress(Exception):
                kernel.force_close()


# -- the local kernel ------------------------------------------------------------------------------


class LocalKernel:
    """One kernel child on this machine plus its RPC server and pipes."""

    def __init__(
        self,
        folder: Path,
        server: SocketRpcServer,
        token: str,
        child: launch.Child,
        request_fd: int,
        replies: queue.Queue[dict[str, Any]],
        clock: Clock,
    ) -> None:
        self.folder = folder
        self.server = server
        self.token = token
        self.child = child
        self._request_fd: int | None = request_fd
        self.replies = replies
        self.clock = clock
        self.last_used = clock.monotonic()
        self.busy = False
        self.pinned = False
        self.count = 0
        self._closed = False

    async def alive(self) -> bool:
        return not self._closed and self.child.alive()

    def send(self, payload: dict[str, Any]) -> None:
        if self._request_fd is None:
            raise BrokenPipeError("the kernel is closed")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(self._request_fd, view)
            view = view[written:]

    def _close_pipe(self) -> None:
        if self._request_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._request_fd)
            self._request_fd = None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_pipe()  # the runner exits on its own when the pipe closes
        try:
            await self.child.stop(self.clock)
        finally:
            self._cleanup()

    def force_close(self) -> None:
        self._closed = True
        self._close_pipe()
        with contextlib.suppress(Exception):
            self.child.force_stop()
        self._cleanup()

    def _cleanup(self) -> None:
        self.server.close()
        shutil.rmtree(self.folder, ignore_errors=True)


def _read_replies(fd: int, sink: queue.Queue[dict[str, Any]]) -> None:
    try:
        with os.fdopen(fd, "rb") as source:
            for line in source:
                try:
                    reply: object = json.loads(line)
                except ValueError:
                    reply = None
                if isinstance(reply, dict):
                    sink.put(cast(dict[str, Any], reply))
                else:
                    sink.put({"state": "broken"})
    except OSError:
        pass
    finally:
        sink.put({"state": "gone"})


async def start_local_kernel(
    scope: Scope,
    *,
    session_id: str | None,
    interpreter: str,
    cwd: str | None,
    tools: tuple[str, ...],
    idle_s: float,
    clock: Clock,
) -> LocalKernel:
    folder = launch.private_dir(launch.staging_root(scope), prefix="indus-kernel-")
    server: SocketRpcServer | None = None
    fds: list[int] = []
    try:
        launch.write_private(folder / f"{MODULE_NAME}.py", module_source(tools, "socket"))
        launch.write_private(folder / "kernel.py", RUNNER_SOURCE)
        token = secrets.token_urlsafe(32)
        server = SocketRpcServer(folder)
        req_r, req_w = os.pipe()
        rep_r, rep_w = os.pipe()
        fds = [req_r, req_w, rep_r, rep_w]
        for fd in (req_w, rep_r):
            os.set_inheritable(fd, False)
        env = launch.child_environment(
            scope,
            session_id=session_id,
            staging=folder,
            extra={
                ENV_SOCKET: server.endpoint,
                ENV_TOKEN: token,
                ENV_PERSISTENT: "1",
                "INDUS_KERNEL_IN_FD": str(req_r),
                "INDUS_KERNEL_OUT_FD": str(rep_w),
                "INDUS_KERNEL_IDLE_S": str(idle_s),
                "INDUS_KERNEL_CAPTURE": str(CELL_CAPTURE_BYTES),
            },
        )
        child = launch.Child(
            [interpreter, str(folder / "kernel.py")],
            cwd=cwd or str(folder),
            env=env,
            stdout=out.ByteCapture(CELL_CAPTURE_BYTES),
            stderr=out.ByteCapture(STDERR_BYTES * 4, tail=STDERR_BYTES),
            pass_fds=(req_r, rep_w),
        )
        os.close(req_r)
        os.close(rep_w)
        fds = []
        replies: queue.Queue[dict[str, Any]] = queue.Queue()
        threading.Thread(
            target=_read_replies, args=(rep_r, replies), name="code-kernel-replies", daemon=True
        ).start()
        return LocalKernel(folder, server, token, child, req_w, replies, clock)
    except BaseException:
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        if server is not None:
            server.close()
        shutil.rmtree(folder, ignore_errors=True)
        raise


# -- running one cell ------------------------------------------------------------------------------


@dataclass(frozen=True)
class CellJob:
    code: str
    scope: Scope
    session_id: str | None
    key: KernelKey
    reset: bool
    timeout_s: float
    idle_s: float
    cap: int
    pipeline: RpcPipeline
    clock: Clock
    cancel: CancelSignal | None = None


async def _await_reply(kernel: LocalKernel, job: CellJob) -> tuple[str, dict[str, Any]]:
    deadline = job.clock.monotonic() + job.timeout_s
    delay = POLL_START_S
    while True:
        try:
            reply = kernel.replies.get_nowait()
        except queue.Empty:
            pass
        else:
            state = str(reply.get("state", ""))
            if state in ("gone", "broken"):
                return "died", reply
            return "done", reply
        if job.cancel is not None and job.cancel.is_set():
            return "interrupted", {}
        if job.clock.monotonic() >= deadline:
            return "timeout", {}
        await job.clock.sleep(delay)
        delay = min(delay * 1.5, POLL_MAX_S)


async def run_cell(
    pool: KernelPool[LocalKernel], job: CellJob, make: Callable[[], Awaitable[LocalKernel]]
) -> RunOutcome:
    """Run ``job.code`` in the kernel for ``job.key`` (see the module notes)."""
    started = job.clock.monotonic()
    taken = await pool.take(
        job.key, clock=job.clock, reset=job.reset, idle_s=job.idle_s, cap=job.cap, make=make
    )
    kernel = taken.kernel
    info = taken.info("session")
    released = False
    try:
        while True:  # drop replies and output left over from an earlier cell
            try:
                kernel.replies.get_nowait()
            except queue.Empty:
                break
        kernel.child.stdout.drain()
        kernel.child.stderr.drain()
        job.pipeline.token = kernel.token
        kernel.server.attach(job.pipeline, asyncio.get_running_loop())
        try:
            kernel.send({"id": uuid.uuid4().hex, "code": job.code})
        except OSError:
            state, reply = "died", {}
        else:
            state, reply = await _await_reply(kernel, job)
        kernel.server.detach()
        outcome = RunOutcome(
            status="success",
            tool_calls_made=job.pipeline.calls_made,
            tool_errors=job.pipeline.tool_errors(TOOL_ERRORS_SHOWN),
            exit_code=0,
            kernel=info,
            show_stderr=True,
        )
        if state in ("timeout", "interrupted"):
            await pool.discard(job.key, kernel)
            released = True
            await kernel.child.finish_reading(job.clock, within=0.5)
            info["ended"] = True
            outcome.exit_code = -1
            if state == "timeout":
                outcome.status = "timeout"
                outcome.error = timeout_message(job.timeout_s, kernel=True)
            else:
                outcome.status = "interrupted"
                outcome.error = "Interrupted: the session kernel was stopped and its state is gone."
            raw_out, _ = kernel.child.stdout.drain()
            outcome.stdout = out.decode(raw_out)
            return _timed(outcome, job, started)
        if state == "died":
            raw_err, _ = kernel.child.stderr.drain()
            await pool.discard(job.key, kernel)
            released = True
            info["ended"] = True
            why = out.decode(raw_err).strip()
            outcome.status, outcome.exit_code = "error", -1
            outcome.error = "The session kernel stopped while running the call" + (
                f": {why[-2000:]}" if why else "."
            )
            outcome.hint_text = why
            return _timed(outcome, job, started)
        await kernel.child.finish_reading(job.clock, within=0.0)
        raw_out, _ = kernel.child.stdout.drain()
        raw_err, _ = kernel.child.stderr.drain()
        kernel.count = int(reply.get("count", kernel.count + 1) or 0)
        info["execution_count"] = kernel.count
        outcome.stdout = str(reply.get("stdout", "")) + out.decode(raw_out)
        stderr = str(reply.get("stderr", "")) + out.decode(raw_err)
        trace = str(reply.get("traceback", ""))
        cell_state = str(reply.get("state", "ok"))
        if reply.get("stdout_cut"):
            info["capture_cut"] = (
                f"The call printed more than {CELL_CAPTURE_BYTES:,} characters; only the first "
                "part was kept."
            )
        if cell_state == "error":
            outcome.status, outcome.exit_code = "error", 1
            outcome.error = trace.strip().splitlines()[-1] if trace.strip() else "The cell raised."
            outcome.stderr = stderr + trace
            outcome.hint_text = trace
        elif cell_state == "exit":
            info["ended"] = True
            outcome.stderr = stderr
            await pool.discard(job.key, kernel, lost=False)
            released = True
        else:
            outcome.stderr = stderr
        return _timed(outcome, job, started)
    finally:
        kernel.server.detach()
        if not released:
            pool.give_back(kernel, job.clock)


def _timed(outcome: RunOutcome, job: CellJob, started: float) -> RunOutcome:
    outcome.seconds = max(0.0, job.clock.monotonic() - started)
    return outcome


# -- the process-wide pool -------------------------------------------------------------------------

POOL: KernelPool[LocalKernel] = KernelPool("local")


async def dispose_child(child_session_id: str) -> int:
    """Close every kernel (local and remote) a finished delegated child owned (P10-06)."""
    from indus.tools.builtin.code import remote

    suffix = f"{CHILD_MARK}{child_session_id}"
    closed = await POOL.dispose_where(lambda owner: owner.endswith(suffix))
    return closed + await remote.POOL.dispose_where(lambda owner: owner.endswith(suffix))


async def dispose_session(owner: str) -> int:
    """Close the kernels of a session that ended (``/new``, session close)."""
    from indus.tools.builtin.code import remote

    return await POOL.dispose_owner(owner) + await remote.POOL.dispose_owner(owner)


async def run_reaper(clock: Clock, idle_s: Callable[[], float]) -> None:
    """Background task for long-lived hosts: close idle kernels on a schedule."""
    from indus.tools.builtin.code import remote

    while True:
        window = idle_s()
        await clock.sleep(min(REAPER_CEIL_S, max(REAPER_FLOOR_S, window / 6.0)))
        try:
            await POOL.reap_idle(clock, window)
            await remote.POOL.reap_idle(clock, window)
        except Exception:
            log.exception("kernel reaper pass failed; trying again next time")


atexit.register(POOL.force_shutdown)


__all__ = [
    "CHILD_MARK",
    "POOL",
    "CellJob",
    "KernelKey",
    "KernelPool",
    "LocalKernel",
    "PooledKernel",
    "Taken",
    "child_owner",
    "dispose_child",
    "dispose_session",
    "is_child_owner",
    "run_cell",
    "run_reaper",
    "start_local_kernel",
]
