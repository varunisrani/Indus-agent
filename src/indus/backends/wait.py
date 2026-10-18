# pyright: strict
"""Waiting for a spawned command: poll loop, hard backstop and the exit fence (P05-02).

``wait_process`` polls the process on an injected clock (5 ms, growing ×1.5 up to 200 ms) while a
worker of the ``terminal`` blocking pool drains its output into a capture. Each round checks, in
order: has it exited; is the turn's cancel signal set (kill, rc 130, ``[Command interrupted]``);
is the deadline past (kill, rc 124, ``[Command timed out after Ns]``, ``timed_out=True`` — a
command that itself exits 124 is not flagged); is an activity callback due (every 10 s).

``run_bounded`` is the hard backstop (R02.3): the spawn and the wait run as their own task, raced
against ``timeout + 2 s`` on the clock. If the inner wait never returns, the process tree is
force-killed and the result is rc 124 with whatever output was captured.

``FOREGROUND`` tracks every running foreground command in the process. ``fence_and_kill`` (hard
exit) stops new spawns and force-kills every live tree within half a second.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from indus.backends.capture import OutputCapture
from indus.backends.contract import ActivityCallback, BackendError, CancelSignal, ExecResult
from indus.foundation.blocking import BlockingPool, PoolFull, run_blocking
from indus.foundation.clock import Clock

__all__ = [
    "BACKSTOP_GRACE_S",
    "FOREGROUND",
    "TERMINAL_POOL",
    "ForegroundTable",
    "ProcessHandle",
    "interrupted_note",
    "run_bounded",
    "timed_out_note",
    "wait_process",
]

log = logging.getLogger(__name__)

POLL_START_S = 0.005
POLL_GROWTH = 1.5
POLL_MAX_S = 0.2
ACTIVITY_EVERY_S = 10.0
BACKSTOP_GRACE_S = 2.0
# On our deadline the ordered kill must be over before the backstop (timeout + 2 s) fires.
DEADLINE_KILL_BUDGET_S = 1.5
DRAIN_GRACE_S = 2.0  # how long the output reader may lag behind the process exit
STDIN_GRACE_S = 5.0
HARD_KILL_BUDGET_S = 0.5
INTERRUPTED_RC = 130
TIMED_OUT_RC = 124

# A foreground command holds one drain worker for its whole life, so it gets its own pool
# instead of sharing the small ``io``/``tools`` pools. A full pool refuses the command.
TERMINAL_POOL = BlockingPool("terminal", 32)

EXITING_NOTE = "[host is exiting: command not started]"


def interrupted_note() -> str:
    return "\n[Command interrupted]"


def timed_out_note(timeout: float) -> str:
    return f"\n[Command timed out after {timeout:g}s]"


class ProcessHandle(Protocol):
    """A running command as the wait loop sees it."""

    @property
    def pid(self) -> int | None: ...

    def poll(self) -> int | None:
        """Exit status once the process has ended, else None. Never blocks."""
        ...

    def drain(self, sink: Callable[[str], None], stop: threading.Event) -> None:
        """Blocking: feed decoded output to ``sink`` until the stream ends, the process has been
        gone a moment, or ``stop`` is set. Runs on a worker thread."""
        ...

    @property
    def stdin_task(self) -> asyncio.Future[str | None] | None:
        """The stdin writer (its result is an error text, or None), when there is stdin."""
        ...


type KillTree = Callable[[ProcessHandle, float | None], Awaitable[None]]
"""Ordered kill of a process tree; the second argument caps how long it may take (None: the
backend's full grace)."""
type ForceKill = Callable[[ProcessHandle], None]

# Kills started for a cancelled wait: kept here so they are not garbage-collected mid-way.
_DETACHED: set[asyncio.Future[Any]] = set()


def _detach(work: Awaitable[None]) -> None:
    task = asyncio.ensure_future(work)
    _DETACHED.add(task)

    def done(t: asyncio.Future[Any]) -> None:
        _DETACHED.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.warning("killing a cancelled command failed", exc_info=t.exception())

    task.add_done_callback(done)


async def _settle(future: asyncio.Future[Any], seconds: float, clock: Clock) -> bool:
    """Wait up to ``seconds`` on ``clock`` for ``future``; True when it finished."""
    if future.done():
        return True
    timer = asyncio.ensure_future(clock.sleep(seconds))
    try:
        await asyncio.wait({future, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    return future.done()


def _finish(
    capture: OutputCapture,
    text: str,
    returncode: int,
    handle: ProcessHandle,
    *,
    timed_out: bool = False,
    interrupted: bool = False,
    stdin_error: str | None = None,
) -> ExecResult:
    spill = capture.close_spill()
    return ExecResult(
        output=text,
        returncode=returncode,
        timed_out=timed_out,
        interrupted=interrupted,
        full_output_path=spill,
        output_total_chars=capture.total_chars if spill else None,
        stdin_error=stdin_error,
        pid=handle.pid,
    )


async def wait_process(
    handle: ProcessHandle,
    capture: OutputCapture,
    *,
    timeout: float,
    clock: Clock,
    kill: KillTree,
    cancel: CancelSignal | None = None,
    on_activity: ActivityCallback | None = None,
    activity_every: float = ACTIVITY_EVERY_S,
) -> ExecResult:
    """Wait for ``handle`` (see the module notes). Cancelling the caller kills the tree."""
    stop = threading.Event()
    reader: asyncio.Future[None] = asyncio.ensure_future(
        run_blocking(handle.drain, capture.append, stop, pool=TERMINAL_POOL, label="drain")
    )
    reader.add_done_callback(_retrieve)
    started = clock.monotonic()
    deadline = started + timeout
    last_touch = started
    delay = POLL_START_S
    try:
        while True:
            if handle.poll() is not None:
                break
            if _no_reader(reader):
                await kill(handle, None)
                raise _too_many()
            if cancel is not None and cancel.is_set():
                await kill(handle, None)
                await _stop_reader(reader, stop, clock)
                text = capture.render(suffix=interrupted_note())
                return _finish(capture, text, INTERRUPTED_RC, handle, interrupted=True)
            now = clock.monotonic()
            if now >= deadline:
                await kill(handle, DEADLINE_KILL_BUDGET_S)
                await _stop_reader(reader, stop, clock)
                text = capture.render(suffix=timed_out_note(timeout))
                if capture.total_chars == 0:
                    text = text.lstrip()
                return _finish(capture, text, TIMED_OUT_RC, handle, timed_out=True)
            if on_activity is not None and now - last_touch >= activity_every:
                last_touch = now
                try:
                    on_activity(f"command running ({int(now - started)}s)")
                except Exception:
                    log.debug("activity callback failed", exc_info=True)
            await clock.sleep(delay)
            delay = min(delay * POLL_GROWTH, POLL_MAX_S)
    except asyncio.CancelledError:
        stop.set()
        _detach(kill(handle, None))
        raise
    code = handle.poll()
    assert code is not None
    if not await _settle(reader, DRAIN_GRACE_S, clock):
        stop.set()
    elif _no_reader(reader):
        raise _too_many()
    stdin_error = await _stdin_outcome(handle, clock)
    text = capture.render()
    if stdin_error:
        text += f"\n[stdin write failed: {stdin_error}]"
    return _finish(capture, text, code, handle, stdin_error=stdin_error)


def _retrieve(future: asyncio.Future[Any]) -> None:
    # Fetch a reader failure so asyncio never reports it as "never retrieved"; it is handled
    # (or irrelevant, after a kill) where the wait looks at the reader.
    if not future.cancelled():
        future.exception()


def _no_reader(reader: asyncio.Future[None]) -> bool:
    """True when the output reader was refused a worker (the pool was full)."""
    return reader.done() and not reader.cancelled() and isinstance(reader.exception(), PoolFull)


def _too_many() -> BackendError:
    return BackendError(
        "too many commands are running at once; this one was stopped",
        hint="wait for running commands to finish, then run it again",
    )


async def _stop_reader(reader: asyncio.Future[None], stop: threading.Event, clock: Clock) -> None:
    if not await _settle(reader, DRAIN_GRACE_S, clock):
        stop.set()


async def _stdin_outcome(handle: ProcessHandle, clock: Clock) -> str | None:
    task = handle.stdin_task
    if task is None:
        return None
    if not await _settle(task, STDIN_GRACE_S, clock):
        return None
    if task.cancelled():
        return None
    error = task.exception()
    if error is not None:
        return str(error)
    return task.result()


async def run_bounded(
    work: Callable[[], Awaitable[ExecResult]],
    *,
    timeout: float,
    clock: Clock,
    on_expiry: Callable[[], None],
    capture: OutputCapture,
) -> ExecResult:
    """Run ``work`` (spawn + wait) under the hard bound ``timeout + 2 s`` (R02.3).

    ``on_expiry`` force-kills whatever ``work`` spawned; the result is then rc 124 with
    ``timed_out=True`` and the output ``capture`` holds so far.
    """
    inner = asyncio.ensure_future(work())
    timer = asyncio.ensure_future(clock.sleep(timeout + BACKSTOP_GRACE_S))
    try:
        await asyncio.wait({inner, timer}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        inner.cancel()
        raise
    finally:
        timer.cancel()
    if inner.done():
        return inner.result()
    log.warning("command wait did not return within %gs; killing its process tree", timeout)
    inner.cancel()
    try:
        on_expiry()
    except Exception:
        log.warning("backstop kill failed", exc_info=True)
    text = capture.render(suffix=timed_out_note(timeout))
    if capture.total_chars == 0:
        text = text.lstrip()
    spill = capture.close_spill()
    return ExecResult(
        output=text,
        returncode=TIMED_OUT_RC,
        timed_out=True,
        full_output_path=spill,
        output_total_chars=capture.total_chars if spill else None,
    )


class ForegroundTable:
    """Every foreground command running in this process, and the one-way exit fence."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.RLock())
        self._live: dict[int, tuple[ProcessHandle, ForceKill]] = {}
        self._spawning = 0
        self._fenced = False

    @property
    def fenced(self) -> bool:
        return self._fenced

    def __len__(self) -> int:
        with self._cond:
            return len(self._live)

    def admit(self) -> bool:
        """Before a spawn: False once the fence is up (the command must not start)."""
        with self._cond:
            if self._fenced:
                return False
            self._spawning += 1
            return True

    def publish(self, handle: ProcessHandle | None, force_kill: ForceKill) -> bool:
        """After a spawn (``handle`` None when it failed). True when the fence went up meanwhile:
        the caller must force-kill what it just started."""
        with self._cond:
            self._spawning -= 1
            if handle is not None:
                self._live[id(handle)] = (handle, force_kill)
            self._cond.notify_all()
            return self._fenced

    def remove(self, handle: ProcessHandle) -> None:
        with self._cond:
            self._live.pop(id(handle), None)

    def fence_and_kill(self, budget: float = HARD_KILL_BUDGET_S) -> int:
        """Raise the fence, wait (≤ ``budget``) for spawns already past it, force-kill every live
        tree. Never blocks longer than ``budget``; returns how many trees were signalled."""
        self._fenced = True
        live: list[tuple[ProcessHandle, ForceKill]]
        if self._cond.acquire(timeout=budget):
            try:
                self._cond.wait_for(lambda: self._spawning == 0, budget)
                live = list(self._live.values())
            finally:
                self._cond.release()
        else:  # held by a stuck thread: a copy without the lock beats hanging the exit
            live = list(self._live.values())
        for handle, force_kill in live:
            with contextlib.suppress(Exception):
                force_kill(handle)
        return len(live)

    def reset_for_tests(self) -> None:
        with self._cond:
            self._fenced = False
            self._live.clear()
            self._spawning = 0


FOREGROUND = ForegroundTable()
