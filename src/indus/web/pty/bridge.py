"""One child process on one pseudo-terminal, for the dashboard's Chat tab (P18-07).

``spawn(argv, cwd=, env=, size=)`` starts the child behind a new PTY and returns a ``PtyBridge``:

- ``read()`` waits for the next chunk the child printed (at most 64 KiB) and returns None at end
  of output. On POSIX the master is non-blocking and the wait is a readiness callback on the event
  loop with a 0.2 s timeout; on each timeout the leader is asked (``WNOHANG``) whether it still
  runs, so a child whose helper keeps the slave side open still ends the stream once it is gone
  and nothing is left to read;
- ``write(data, timeout=10.0)`` writes everything without blocking the loop and answers False when
  the child left its input full for ``timeout`` seconds or the PTY is gone (the route then closes
  the socket 1013);
- ``resize(size)`` applies a clamped ``TermSize`` (``TIOCSWINSZ``);
- ``close()`` ends the child's whole process group — the child is a session leader — with
  SIGHUP, then (after the grace, 1.5 s, never more than 2.0 s) SIGTERM, then SIGKILL, 0.5 s
  apart, draining output meanwhile so an exiting child is never stuck on a full PTY; the leader is
  reaped, the group swept with a final SIGKILL, the fd closed. Idempotent.

The caller owns argv and env (the env comes from ``safety.child_env``, R02.22); the bridge only
backfills ``TERM`` when it is missing.

Windows: ConPTY through ``pywinpty`` when it imports, read on one dedicated thread per bridge with
a 0.05 s back-off when idle. When the PTY support cannot load, ``unavailable()`` names the reason
(on Windows: the chat needs WSL2, D-16.38) and ``spawn`` raises ``PtyUnavailable`` with it; the
route writes it into the terminal and closes 1011.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final, Protocol

from indus.foundation.clock import Clock, SystemClock
from indus.web.pty.resize import TermSize

__all__ = [
    "DEFAULT_SIZE",
    "GRACE_S",
    "MAX_GRACE_S",
    "READ_CHUNK",
    "READ_TIMEOUT_S",
    "WINDOWS_NOTICE",
    "WRITE_TIMEOUT_S",
    "PtyBridge",
    "PtyUnavailable",
    "spawn",
    "unavailable",
]

log = logging.getLogger(__name__)

READ_CHUNK: Final = 65536
READ_TIMEOUT_S: Final = 0.2
IDLE_BACKOFF_S: Final = 0.05
WRITE_TIMEOUT_S: Final = 10.0
GRACE_S: Final = 1.5
MAX_GRACE_S: Final = 2.0
STEP_S: Final = 0.5
_POLL_S: Final = 0.02
DEFAULT_SIZE: Final = TermSize(80, 24)

WINDOWS_NOTICE: Final = (
    "Chat needs WSL2 on this machine: the terminal support for native Windows could not be "
    "loaded. Install Indus Agent inside WSL2 to use the Chat tab; the rest of the dashboard works "
    "here."
)
POSIX_NOTICE: Final = (
    "Chat needs the terminal support package, which is not installed: reinstall Indus Agent "
    "with its dashboard extra (indus-agent[dashboard])."
)


class PtyUnavailable(RuntimeError):
    """No PTY can be made here; the message is shown to the person as is."""


class PtyBridge(Protocol):
    """What the routes and the keep-alive registry use."""

    @property
    def pid(self) -> int: ...

    def is_alive(self) -> bool: ...

    async def read(self) -> bytes | None: ...

    async def write(self, data: bytes, *, timeout: float = WRITE_TIMEOUT_S) -> bool: ...

    def resize(self, size: TermSize) -> None: ...

    async def close(self) -> None: ...


# -- loading the platform support -----------------------------------------------------------------


def _load_posix() -> Any:
    import ptyprocess  # pyright: ignore[reportMissingImports]

    return ptyprocess


def _load_windows() -> Any:
    import winpty  # pyright: ignore[reportMissingImports]

    return winpty


def _platform() -> str:
    return sys.platform


def unavailable(platform: str | None = None) -> str | None:
    """None when a PTY can be spawned here, else the reason to show."""
    name = platform if platform is not None else _platform()
    try:
        _load_windows() if name.startswith("win") else _load_posix()
    except ImportError:
        return WINDOWS_NOTICE if name.startswith("win") else POSIX_NOTICE
    return None


def spawn(
    argv: Sequence[str],
    *,
    cwd: Path | str | None,
    env: Mapping[str, str],
    size: TermSize = DEFAULT_SIZE,
    clock: Clock | None = None,
    grace: float = GRACE_S,
    platform: str | None = None,
) -> PtyBridge:
    """Start ``argv`` behind a new PTY (blocking: fork and exec; call it off the loop)."""
    name = platform if platform is not None else _platform()
    child_env = dict(env)
    if not child_env.get("TERM"):
        child_env["TERM"] = "xterm-256color"
    workdir = None if cwd is None else str(cwd)
    ticks = clock if clock is not None else SystemClock()
    if name.startswith("win"):
        try:
            winpty = _load_windows()
        except ImportError as exc:
            raise PtyUnavailable(WINDOWS_NOTICE) from exc
        return _WindowsPty.start(winpty, list(argv), workdir, child_env, size, ticks)
    try:
        ptyprocess = _load_posix()
    except ImportError as exc:
        raise PtyUnavailable(POSIX_NOTICE) from exc
    proc = ptyprocess.PtyProcess.spawn(
        list(argv), cwd=workdir, env=child_env, dimensions=(size.rows, size.cols)
    )
    return _PosixPty(proc, clock=ticks, grace=grace)


# -- POSIX ----------------------------------------------------------------------------------------


class _PosixPty:
    """``ptyprocess`` underneath; reads and writes go straight to the non-blocking master fd."""

    def __init__(self, proc: Any, *, clock: Clock, grace: float = GRACE_S) -> None:
        self._proc = proc
        self._fd: int = int(proc.fd)
        self._pid: int = int(proc.pid)
        self._clock = clock
        self._grace = min(max(grace, 0.0), MAX_GRACE_S)
        self._closed = False
        self._fd_closed = False
        self._leader_gone = False
        # Our own close does the signalling; ptyprocess must not sleep in its close.
        with contextlib.suppress(AttributeError):
            proc.delayafterclose = 0
            proc.delayafterterminate = 0
        os.set_blocking(self._fd, False)

    @property
    def pid(self) -> int:
        return self._pid

    def _leader_alive(self) -> bool:
        if self._leader_gone:
            return False
        try:
            alive = bool(self._proc.isalive())
        except Exception:  # already reaped elsewhere, or the pid is not ours any more
            alive = False
        if not alive:
            self._leader_gone = True
        return alive

    def is_alive(self) -> bool:
        return not self._closed and self._leader_alive()

    async def _ready(self, writing: bool, timeout: float) -> bool:
        """Wait until the fd can be read (or written), at most ``timeout`` seconds."""
        if self._fd_closed or timeout <= 0:
            return False
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()

        def mark() -> None:
            if not ready.done():
                ready.set_result(None)

        try:
            if writing:
                loop.add_writer(self._fd, mark)
            else:
                loop.add_reader(self._fd, mark)
        except (OSError, ValueError):
            return False
        try:
            await asyncio.wait_for(ready, timeout)
            return not self._fd_closed
        except TimeoutError:
            return False
        finally:
            if not self._fd_closed:
                with contextlib.suppress(OSError, ValueError):
                    if writing:
                        loop.remove_writer(self._fd)
                    else:
                        loop.remove_reader(self._fd)

    async def read(self) -> bytes | None:
        while True:
            if self._closed or self._fd_closed:
                return None
            try:
                data = os.read(self._fd, READ_CHUNK)
            except BlockingIOError:
                if self._leader_gone:
                    return None  # the leader is gone and nothing is left to read
                if not await self._ready(False, READ_TIMEOUT_S):
                    self._leader_alive()
                continue
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    return None  # the slave side is closed
                raise
            return data or None

    async def write(self, data: bytes, *, timeout: float = WRITE_TIMEOUT_S) -> bool:
        if self._closed or self._fd_closed:
            return False
        loop = asyncio.get_running_loop()
        end = loop.time() + max(0.0, timeout)
        view = memoryview(data)
        while view:
            if self._closed or self._fd_closed:
                return False
            try:
                written = os.write(self._fd, view)
            except BlockingIOError:
                written = 0
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF, errno.EPIPE):
                    return False
                raise
            if written > 0:
                view = view[written:]
                continue
            if not await self._ready(True, end - loop.time()):
                return False
        return True

    def resize(self, size: TermSize) -> None:
        if self._closed or self._fd_closed:
            return
        with contextlib.suppress(OSError):
            self._proc.setwinsize(size.rows, size.cols)

    def _drain_now(self) -> None:
        with contextlib.suppress(OSError):
            while os.read(self._fd, READ_CHUNK):
                pass

    def _group_alive(self) -> bool:
        self._leader_alive()  # reaps the leader once it has exited
        try:
            os.killpg(self._pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True  # exists but not ours to signal: treat as alive, the sweep will tell
        return True

    async def _wait_gone(self, seconds: float) -> bool:
        waited = 0.0
        while waited < seconds:
            if not self._group_alive():
                return True
            if not self._fd_closed:
                self._drain_now()
            await self._clock.sleep(_POLL_S)
            waited += _POLL_S
        return not self._group_alive()

    def _signal_group(self, sig: int) -> bool:
        try:
            os.killpg(self._pid, sig)
        except ProcessLookupError:
            return False
        except OSError:
            with contextlib.suppress(OSError):
                os.kill(self._pid, sig)
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sig, wait in (
            (signal.SIGHUP, self._grace),
            (signal.SIGTERM, STEP_S),
            (signal.SIGKILL, STEP_S),
        ):
            if not self._group_alive():
                break
            if not self._signal_group(sig):
                break
            if await self._wait_gone(wait):
                break
        self._signal_group(signal.SIGKILL)  # a helper that outlived everything above
        self._leader_alive()
        loop = asyncio.get_running_loop()
        with contextlib.suppress(OSError, ValueError):
            loop.remove_reader(self._fd)
        with contextlib.suppress(OSError, ValueError):
            loop.remove_writer(self._fd)
        self._fd_closed = True
        try:
            self._proc.close(force=True)
        except Exception as exc:  # the fd is ours to drop either way
            log.debug("pty close: %s", type(exc).__name__)
            with contextlib.suppress(OSError):
                os.close(self._fd)


# -- Windows --------------------------------------------------------------------------------------


class _WindowsPty:
    """ConPTY through ``pywinpty``: one reader thread per bridge (no readiness API there)."""

    def __init__(self, proc: Any, clock: Clock) -> None:
        self._proc = proc
        self._pid = int(proc.pid)
        self._clock = clock
        self._closed = False
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="indus-conpty")

    @classmethod
    def start(
        cls,
        winpty: Any,
        argv: list[str],
        cwd: str | None,
        env: dict[str, str],
        size: TermSize,
        clock: Clock,
    ) -> _WindowsPty:
        proc = winpty.PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(size.rows, size.cols))
        return cls(proc, clock)

    @property
    def pid(self) -> int:
        return self._pid

    def is_alive(self) -> bool:
        if self._closed:
            return False
        with contextlib.suppress(Exception):
            return bool(self._proc.isalive())
        return False

    def _read_once(self) -> bytes | None:
        try:
            text = self._proc.read(READ_CHUNK)
        except EOFError:
            return None
        if isinstance(text, bytes):
            return text
        return str(text).encode("utf-8", "replace")

    async def _run[T](self, fn: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn)

    async def read(self) -> bytes | None:
        while not self._closed:
            chunk = await self._run(self._read_once)
            if chunk is None:
                return None
            if chunk:
                return chunk
            await self._clock.sleep(IDLE_BACKOFF_S)
        return None

    async def write(self, data: bytes, *, timeout: float = WRITE_TIMEOUT_S) -> bool:
        if self._closed:
            return False
        text = data.decode("utf-8", "replace")
        try:
            await asyncio.wait_for(self._run(lambda: self._proc.write(text)), timeout)
        except (TimeoutError, OSError, EOFError):
            return False
        return True

    def resize(self, size: TermSize) -> None:
        with contextlib.suppress(Exception):
            self._proc.setwinsize(size.rows, size.cols)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._run(lambda: self._proc.terminate(force=True))
        self._pool.shutdown(wait=False, cancel_futures=True)
