"""Backstops that work when the event loop does not (P15-25).

- ``LoopWatchdog``: a thread asks the event loop to run a tiny callback every ``interval`` seconds
  and waits ``timeout`` for it; ``strikes`` misses in a row mean the loop is wedged, and
  ``on_trip`` runs (the daemon dumps stacks, writes ``degraded`` and hard-exits 75). One answered
  probe resets the count. The probe never touches files on the loop.
- ``Deadline``: a one-shot thread timer (startup watchdog: not ``running`` within 300 s; shutdown
  watchdog: drain budget + 60 s after a stop began). ``cancel`` disarms it.
- ``dump_stacks``: every thread's stack (``faulthandler``) and every asyncio task's stack, appended
  to ``logs/gateway_faulthandler.log`` under a header line (SIGUSR2, and before any watchdog exit).
- ``RestartLoopGuard``: boots that find restart-interrupted sessions are recorded; boots chain while
  each gap stays within ``max(window, max_gap)``; when the chain reaches ``max_restarts`` the guard
  trips and that boot skips auto-resume. At most 50 boots are kept. Any read or write failure fails
  open (not tripped), so the guard can never wedge the gateway.
- ``SystemdNotifier``: ``sd_notify`` datagrams when ``NOTIFY_SOCKET`` is set: ``READY=1`` once
  running, ``STOPPING=1`` at stop, and with a watchdog interval ``WATCHDOG=1`` every half interval
  while the loop keeps time. Never raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import io
import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from indus.foundation import fsio
from indus.foundation.clock import Clock

__all__ = [
    "Deadline",
    "LoopWatchdog",
    "RestartLoopGuard",
    "SystemdNotifier",
    "dump_stacks",
    "notify",
]

log = logging.getLogger(__name__)


# -- stack dumps -----------------------------------------------------------------------------------


def dump_stacks(path: Path, reason: str, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Append every thread's and every task's stack to ``path`` (best effort, never raises)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            stamp = datetime.now(UTC).isoformat(timespec="seconds")
            fh.write(f"\n=== {stamp} stack dump: {reason} ===\n")
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)
            fh.flush()
            if loop is not None:
                _dump_tasks(fh, loop)
    except Exception:
        log.warning("could not write the stack dump to %s", path, exc_info=True)


def _dump_tasks(fh: io.TextIOBase, loop: asyncio.AbstractEventLoop) -> None:
    try:
        tasks = list(asyncio.all_tasks(loop))
    except RuntimeError:  # the set changed while we read it (another thread)
        return
    fh.write(f"--- {len(tasks)} asyncio task(s) ---\n")
    for task in tasks:
        buffer = io.StringIO()
        with contextlib.suppress(Exception):
            task.print_stack(file=buffer)
        fh.write(buffer.getvalue())


# -- the loop watchdog -----------------------------------------------------------------------------


def loop_probe(loop: asyncio.AbstractEventLoop, timeout: float) -> bool:
    """True when ``loop`` runs a callback within ``timeout`` seconds (call from another thread)."""
    answered = threading.Event()
    try:
        loop.call_soon_threadsafe(answered.set)
    except RuntimeError:  # the loop is closed
        return True
    return answered.wait(timeout)


class LoopWatchdog:
    """Trips after ``strikes`` unanswered probes in a row (see the module notes)."""

    def __init__(
        self,
        probe: Callable[[], bool],
        on_trip: Callable[[], None],
        *,
        interval: float = 30.0,
        strikes: int = 3,
        name: str = "gateway loop watchdog",
    ) -> None:
        self.probe = probe
        self.on_trip = on_trip
        self.interval = interval
        self.strikes = max(1, strikes)
        self.misses = 0
        self.tripped = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout)

    def check(self) -> bool:
        """One probe; True when it tripped the watchdog."""
        try:
            answered = self.probe()
        except Exception:
            log.warning("the loop probe failed", exc_info=True)
            answered = False
        if answered:
            self.misses = 0
            return False
        self.misses += 1
        log.warning("the event loop did not answer (%d/%d)", self.misses, self.strikes)
        if self.misses >= self.strikes and not self.tripped:
            self.tripped = True
            self.on_trip()
            return True
        return False

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if self.check():
                return


class Deadline:
    """A one-shot thread timer: ``on_fire`` runs unless ``cancel`` comes first."""

    def __init__(self, seconds: float, on_fire: Callable[[], None], *, name: str) -> None:
        self.seconds = seconds
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.name = name
        self._timer.daemon = True
        self._on_fire = on_fire
        self.fired = False

    def _fire(self) -> None:
        self.fired = True
        self._on_fire()

    def start(self) -> Deadline:
        self._timer.start()
        return self

    def cancel(self) -> None:
        self._timer.cancel()


# -- the restart-loop guard ------------------------------------------------------------------------


class RestartLoopGuard:
    """Chained restart-interrupted boots (see the module notes)."""

    def __init__(
        self,
        path: Path,
        *,
        max_restarts: int = 3,
        window: float = 60.0,
        max_gap: float = 300.0,
        keep: int = 50,
    ) -> None:
        self.path = path
        self.max_restarts = max_restarts
        self.gap = float(max(1.0, window, max_gap))
        self.keep = keep

    def _load(self) -> list[float]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        if not isinstance(data, dict):
            return []
        boots = cast(dict[str, Any], data).get("boots")
        if not isinstance(boots, list):
            return []
        return [
            float(t)
            for t in cast(list[object], boots)
            if isinstance(t, int | float) and not isinstance(t, bool)
        ]

    def chain(self, boots: list[float], now: float) -> list[float]:
        """The unbroken run of boots leading up to ``now`` (oldest first)."""
        kept: list[float] = []
        previous = now
        for when in sorted(boots, reverse=True):
            if when > now:  # clock stepped back: a "future" boot still belongs to the run
                kept.append(when)
                continue
            if previous - when > self.gap:
                break
            kept.append(when)
            previous = when
        return sorted(kept)

    def check_and_record(self, now: float | None = None) -> bool:
        """Record this boot; True when auto-resume must be skipped. Fails open."""
        when = time.time() if now is None else now
        try:
            boots = [*self.chain(self._load(), when), when]
            fsio.atomic_write_json(self.path, {"boots": boots[-self.keep :]}, mode=0o600)
        except Exception:
            log.warning("restart-loop guard unavailable; auto-resume allowed", exc_info=True)
            return False
        tripped = self.max_restarts > 0 and len(boots) >= self.max_restarts
        if tripped:
            log.warning(
                "%d gateway restarts in a row interrupted work (gaps of %.0f s or less): skipping "
                "auto-resume this time; delete %s if this is wrong",
                len(boots),
                self.gap,
                self.path,
            )
        return tripped

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


# -- systemd ---------------------------------------------------------------------------------------


def notify(message: str, env: Mapping[str, str]) -> bool:
    """Send one ``sd_notify`` datagram; False when not under systemd or on any failure."""
    address = str(env.get("NOTIFY_SOCKET", "")).strip()
    if not address or not message or not hasattr(socket, "AF_UNIX"):
        return False
    target = "\0" + address[1:] if address.startswith("@") else address
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)
            sender.connect(target)
            sender.send(message.encode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return False
    return True


class SystemdNotifier:
    """READY / STOPPING / WATCHDOG pings (see the module notes)."""

    def __init__(
        self, env: Mapping[str, str], *, clock: Clock, watchdog_seconds: float = 0.0
    ) -> None:
        self.env = dict(env)
        self.clock = clock
        self.watchdog_seconds = max(0.0, watchdog_seconds)
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def active(self) -> bool:
        return bool(str(self.env.get("NOTIFY_SOCKET", "")).strip())

    def ready(self, status: str = "running") -> bool:
        if not self.active:
            return False
        safe = status.replace("\n", " ")
        sent = notify(f"READY=1\nSTATUS={safe}", self.env)
        if self.watchdog_seconds > 0 and self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._pings(), name="systemd watchdog pings"
            )
        return sent

    async def _pings(self) -> None:
        cadence = max(0.05, self.watchdog_seconds / 2.0)
        clock = self.clock
        due = clock.monotonic() + cadence
        while not self._stopped:
            await clock.sleep(max(0.0, due - clock.monotonic()))
            late = clock.monotonic() - due
            if late > max(0.1, cadence / 2.0):
                notify("STATUS=event loop late; watchdog pings stopped", self.env)
                return
            notify("WATCHDOG=1", self.env)
            due += cadence

    async def stopping(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.active:
            notify("STOPPING=1", self.env)
