"""Signals, drain, restart and stop for the gateway daemon (P15-25).

``Lifecycle`` holds what the daemon was asked to do and why; signal handlers only call into it:

- SIGINT / SIGTERM → ``request_stop``: a stop (exit 0). A second stop while one is under way only
  hurries the drain. A stop always wins over a pending restart.
- SIGUSR1 → ``request_restart``: refuse new turns, let the running ones finish (up to
  ``restart_after_turn_timeout``), then stop and exit 75 for the supervisor to start a new process.
- SIGUSR2 → the daemon's stack dump (keeps running).

Windows has no SIGUSR1/SIGUSR2 and its loop cannot add signal handlers: SIGINT, SIGTERM and
SIGBREAK go through ``signal.signal`` onto the loop instead, and a restart arrives as the control
channel's ``stop`` verb with ``restart: true`` (docs/notes/P25/windows-gateway.md).

While draining (a stop, a restart wait, or an external drain marker) the intake's pause probe
(``notice``) answers new turns with a short notice; commands and messages for a conversation that
is already replying still pass (``AdmissionPipeline``'s pause gate).

``drain_turns`` is the chat-turn part of a stop: every key with a running turn is flagged
restart-interrupted **before** any wait (a kill during the drain still leaves the flag for boot
recovery), the turns get ``restart_drain_timeout`` (default 0 s) to finish — a finished one loses
its flag — and the rest are interrupted, their chats told the work continues after the restart, and
given the post-interrupt grace to unwind.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from indus.foundation.blocking import run_blocking
from indus.gateway.core.events import Source
from indus.protocol.methods import ProtocolError
from indus.state import gateway_routes as routes
from indus.state.gateway_runtime import EXIT_OK, EXIT_RESTART

if TYPE_CHECKING:
    from indus.gateway.daemon.process import GatewayDaemon

__all__ = [
    "INTERRUPTED_REASON",
    "MAINTENANCE_NOTICE",
    "MAINTENANCE_REASONS",
    "RESTARTING_NOTICE",
    "Lifecycle",
    "drain_turns",
    "wait_for_turns",
]

log = logging.getLogger(__name__)

MAINTENANCE_REASONS = frozenset({"maintenance", "operator"})
"""Drain reasons answered with the maintenance notice: the external drain marker and the control
socket's ``drain`` verb (``pause-for-update`` drains as ``update``: the gateway restarts next)."""

INTERRUPTED_REASON = "restart_interrupted"
"""``resume_reason`` of a route whose turn a stop or restart cut short (recovery resumes it)."""

MAINTENANCE_NOTICE = (
    "The gateway is in maintenance and isn't starting new work right now. "
    "Please send your message again in a few minutes."
)
RESTARTING_NOTICE = (
    "The gateway is restarting and isn't starting new work. Send your message again in a minute."
)
STOPPED_TURN_NOTICE = (
    "The gateway is restarting, so I stopped working on your message. "
    "I'll pick it up again as soon as I'm back."
)

type Mode = Literal["run", "stop", "restart"]


class Lifecycle:
    """What the daemon was asked to do (see the module notes). Use on the daemon's loop."""

    def __init__(self) -> None:
        self.mode: Mode = "run"
        self.reason = ""
        self.exit_code = EXIT_OK
        self.drain_reason: str | None = None
        self._requested: asyncio.Event | None = None
        self._hurry: asyncio.Event | None = None
        self._installed: list[int] = []
        self._previous: dict[int, Any] = {}

    def _events(self) -> tuple[asyncio.Event, asyncio.Event]:
        if self._requested is None or self._hurry is None:
            self._requested = asyncio.Event()
            self._hurry = asyncio.Event()
        return self._requested, self._hurry

    # -- requests ----------------------------------------------------------------------------------

    @property
    def stopping(self) -> bool:
        return self.mode != "run"

    def request_stop(self, reason: str, *, exit_code: int = EXIT_OK) -> None:
        requested, hurry = self._events()
        if self.mode == "stop":
            log.info("second stop request (%s): interrupting the drain", reason)
            hurry.set()
            return
        if self.mode == "restart":
            hurry.set()  # a stop cuts the after-turn wait short
        self.mode = "stop"
        self.reason = reason
        self.exit_code = exit_code
        self.drain_reason = "stopping"
        requested.set()

    def request_restart(self, reason: str) -> None:
        requested, _hurry = self._events()
        if self.mode != "run":
            return
        self.mode = "restart"
        self.reason = reason
        self.exit_code = EXIT_RESTART
        self.drain_reason = "restarting"
        requested.set()

    async def wait_requested(self) -> None:
        requested, _hurry = self._events()
        await requested.wait()

    def hurried(self) -> bool:
        _requested, hurry = self._events()
        return hurry.is_set()

    async def wait_hurry(self, timeout: float) -> bool:
        """Sleep up to ``timeout``; True when a second stop request cut it short."""
        _requested, hurry = self._events()
        if timeout <= 0:
            return hurry.is_set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(hurry.wait(), timeout)
        return hurry.is_set()

    # -- draining ----------------------------------------------------------------------------------

    @property
    def draining(self) -> bool:
        return self.drain_reason is not None

    def set_draining(self, reason: str = "maintenance") -> None:
        """Stop taking new turns (an external drain or ``pause-for-update``)."""
        if self.mode == "run":
            self.drain_reason = reason

    def clear_draining(self) -> None:
        if self.mode == "run":
            self.drain_reason = None

    def notice(self) -> str | None:
        """The intake's pause probe: what a new turn is told while draining (else None)."""
        if self.drain_reason is None:
            return None
        if self.drain_reason in MAINTENANCE_REASONS:
            return MAINTENANCE_NOTICE
        return RESTARTING_NOTICE

    # -- signals -----------------------------------------------------------------------------------

    def install_signals(
        self, loop: asyncio.AbstractEventLoop, *, on_dump: Callable[[], None]
    ) -> None:
        wanted: dict[str, Callable[[], None]] = {
            "SIGTERM": lambda: self.request_stop("signal:SIGTERM"),
            "SIGINT": lambda: self.request_stop("signal:SIGINT"),
            "SIGBREAK": lambda: self.request_stop("signal:SIGBREAK"),  # Windows' Ctrl+Break
            "SIGUSR1": lambda: self.request_restart("signal:SIGUSR1"),
            "SIGUSR2": on_dump,
        }
        for name, handler in wanted.items():
            signum = getattr(signal, name, None)
            if signum is None:
                continue  # not on this OS
            try:
                loop.add_signal_handler(signum, handler)
            except NotImplementedError:
                if not self._install_plain(loop, signum, handler):
                    continue
            except (RuntimeError, ValueError):
                continue
            self._installed.append(signum)

    def _install_plain(
        self, loop: asyncio.AbstractEventLoop, signum: int, handler: Callable[[], None]
    ) -> bool:
        """A loop without signal support (Windows): a plain handler that hands over to the loop.
        Only the main thread may do this; elsewhere the signal keeps its default."""
        try:
            previous = signal.signal(signum, lambda _n, _f: loop.call_soon_threadsafe(handler))
        except (OSError, RuntimeError, ValueError):
            return False
        self._previous[signum] = previous
        return True

    def remove_signals(self, loop: asyncio.AbstractEventLoop) -> None:
        for signum in self._installed:
            if signum in self._previous:
                with contextlib.suppress(Exception):
                    signal.signal(signum, self._previous.pop(signum))
                continue
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signum)
        self._installed.clear()


# -- chat turns ------------------------------------------------------------------------------------


async def wait_for_turns(daemon: GatewayDaemon, timeout: float, *, poll: float = 0.05) -> bool:
    """Wait until no chat turn holds a claim (up to ``timeout`` s on the daemon's clock, cut
    short by a second stop); True when none is left."""
    clock = daemon.clock
    end = clock.monotonic() + max(0.0, timeout)
    while daemon.pipeline.claims.keys():
        if clock.monotonic() >= end or daemon.lifecycle.hurried():
            return False
        await clock.sleep(poll)
    return True


async def _flag(daemon: GatewayDaemon, key: str) -> None:
    try:
        await run_blocking(
            routes.mark_resume_pending,
            daemon.store,
            key,
            INTERRUPTED_REASON,
            label="resume flag",
        )
    except Exception:
        log.warning("could not flag %s as restart-interrupted", key, exc_info=True)


async def _unflag(daemon: GatewayDaemon, key: str) -> None:
    try:
        entry = await run_blocking(routes.get, daemon.store, key, label="gateway route")
        if entry is not None and entry.resume_reason == INTERRUPTED_REASON:
            await run_blocking(routes.clear_resume_pending, daemon.store, key, label="resume flag")
    except Exception:
        log.warning("could not clear the restart flag of %s", key, exc_info=True)


async def _origin(daemon: GatewayDaemon, key: str) -> Source | None:
    try:
        entry = await run_blocking(routes.get, daemon.store, key, label="gateway route")
    except Exception:
        return None
    if entry is None:
        return None
    try:
        return Source.model_validate(entry.origin)
    except Exception:
        return None


async def drain_turns(daemon: GatewayDaemon, *, drain_timeout: float, grace: float) -> list[str]:
    """Flag, wait, interrupt and notify (see the module notes); the keys interrupted."""
    pipeline = daemon.pipeline
    running = {key: pipeline.claims.current(key) for key in pipeline.claims.keys()}  # noqa: SIM118
    if not running:
        return []
    for key in running:
        await _flag(daemon, key)
    await wait_for_turns(daemon, drain_timeout)
    interrupted: list[str] = []
    for key, claim in running.items():
        current = pipeline.claims.current(key)
        if current is None or current is not claim or current.finished:
            # finished during the drain (or only its bookkeeping is left, the reply already on
            # its way through the ledger): nothing to interrupt, notify or resume
            await _unflag(daemon, key)
            continue
        interrupted.append(key)
        if claim is not None:
            # Its own admission on the shared host (cancelled while still queued), else the
            # session's running turn.
            with contextlib.suppress(ProtocolError, Exception):
                await pipeline.interrupt_claim(claim)
    for key in interrupted:
        source = await _origin(daemon, key)
        if source is not None:
            await pipeline.reply(source, STOPPED_TURN_NOTICE, kind="notice")
    await wait_for_turns(daemon, grace)
    for key in interrupted:
        claim = running[key]
        if claim is None:
            continue
        with contextlib.suppress(Exception):
            await run_blocking(
                routes.end_turn,
                daemon.store,
                key,
                claim.token or f"turn-{claim.generation}",
                label="active-turn marker",
            )
    log.info("interrupted %d chat turn(s) for the stop", len(interrupted))
    return interrupted
