"""Idle exit for a daemon that a client's ``ensure`` started (P17-23).

A daemon started by ``ensure`` (``INDUS_GATEWAY_AUTOSTART=1`` in its environment, set only by
``service.gateway_ensure``'s detached start) and not supervised is nobody's to stop. ``arm`` gives
such a daemon an ``IdleWatch``: every poll it asks ``DaemonProbe`` why the daemon must stay, and
once nothing has kept it busy for ``gateway.idle_exit_seconds`` (600 s; 0 disables; re-read every
poll) it stops with the reason ``idle_exit`` (exit 0). The status file says ``draining`` with that
exit reason at once and ends ``stopped`` / ``idle_exit``; that is not a planned stop, so the next
client's ``ensure`` starts a new daemon without a word.

Busy means any of (``DaemonProbe``; every probe fails closed: an error counts as busy):

- ``quick`` (loop-only facts, synchronous): the daemon stopping; a messaging platform enabled or
  connected natively (a platform served only through a relay connector does not count: the
  connector buffers its events while the gateway is gone, P24-08); an HTTP surface part; a part's
  own ``busy_reason`` (a group-room turn queued or running, P24-06); a served profile that is
  busy (a multiplexing host, P24-01); an outstanding attach ticket; a WebSocket connection; a
  viewer that is not the messaging intake's; a running generation on the host; a chat-service
  session busy or with input waiting; an intake chat claim; a running background process of the
  terminal tool in a held session;
- ``slow`` (store and file reads, off the loop): an admission queued in the runtime ledger (not one
  stuck behind an unknown turn); a cron run in flight or a cron job due within the next tick
  (``CRON_HORIZON_S``) unless cron is paused globally; a loop or heartbeat due within the next tick
  on a held session;
- ``fingerprint``: the connections, held sessions and their event counters changed since the last
  poll (a client that came and went between two polls).

Idle time runs from the last poll that saw the daemon busy (or from arming). When it reaches the
window, ``quick`` and ``fingerprint`` are checked once more with nothing awaited before the stop.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Hashable, Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from indus.cron.models import CronJob
from indus.cron.ticker import TICK_INTERVAL_S, claim_is_live, global_pause, in_flight
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope
from indus.service.gateway_ensure import AUTOSTART_ENV
from indus.state import runtime_ledger as ledger

if TYPE_CHECKING:
    from indus.gateway.daemon.process import GatewayDaemon

__all__ = [
    "CONFIG_KEY",
    "CRON_HORIZON_S",
    "DEFAULT_IDLE_EXIT_S",
    "IDLE_EXIT_REASON",
    "MESSAGING_SURFACE",
    "DaemonProbe",
    "IdleProbe",
    "IdleWatch",
    "arm",
    "autostarted",
    "cron_busy",
    "idle_exit_seconds",
    "poll_interval",
]

log = logging.getLogger(__name__)

IDLE_EXIT_REASON: Final = "idle_exit"
"""The stop reason and the status file's ``exit_reason`` (never a ``planned_stop:`` label)."""
CONFIG_KEY: Final = "gateway.idle_exit_seconds"
DEFAULT_IDLE_EXIT_S: Final = 600.0
CRON_HORIZON_S: Final = TICK_INTERVAL_S
"""A cron job due within this many seconds keeps the daemon (the next tick would run it)."""
MESSAGING_SURFACE: Final = "messaging"
"""Viewers of this surface are the intake's own and never count as clients."""
_MAX_POLL_S: Final = 30.0
_MIN_POLL_S: Final = 0.1


def idle_exit_seconds(scope: Scope) -> float:
    """``gateway.idle_exit_seconds``: 0 or less disables; a value that is not a number keeps the
    default."""
    try:
        view: Any = scope.config if scope.config is not None else config_loader.load(scope)
        value = view.get(CONFIG_KEY)
    except Exception:
        return DEFAULT_IDLE_EXIT_S
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return DEFAULT_IDLE_EXIT_S
    return max(0.0, float(value))


def poll_interval(window: float) -> float:
    """How often the watch looks: a quarter of the window, between 0.1 s and 30 s."""
    return min(_MAX_POLL_S, max(_MIN_POLL_S, window / 4))


def autostarted(env: Mapping[str, str] | Any) -> bool:
    """Did ``ensure`` start this process (its marker in the environment)?"""
    try:
        return env.get(AUTOSTART_ENV) == "1"
    except Exception:
        return False


def cron_busy(
    jobs: Iterable[CronJob],
    *,
    home: Path,
    now: datetime,
    horizon: float = CRON_HORIZON_S,
    paused: bool = False,
) -> str | None:
    """Why cron keeps the daemon: a run in flight, or (unless ``paused`` globally) a job the
    scheduler would fire within ``horizon`` seconds. None when nothing is due."""
    listed = list(jobs)
    for job in listed:
        if in_flight(home, job.id) or (
            job.fire_claim is not None and claim_is_live(job.fire_claim, now)
        ):
            return f"cron job {job.id} is running"
    if paused:
        return None
    edge = now + timedelta(seconds=max(0.0, horizon))
    for job in listed:
        if not job.enabled or job.has_pause_marker or job.state == "completed":
            continue
        if job.state == "error":
            if job.recurring:
                return f"cron job {job.id} is due for repair"
            continue
        if job.quota_hold_until is not None and job.quota_hold_until > edge:
            continue
        if job.next_run_at is None or job.next_run_at <= edge:
            return f"cron job {job.id} is due"
    return None


class IdleProbe(Protocol):
    """What the watch asks every poll (see the module notes)."""

    def quick(self) -> str | None:
        """Why the daemon is busy from loop-only facts, or None. Must not await."""
        ...

    async def slow(self) -> str | None:
        """Why the daemon is busy from stored state (may read files), or None."""
        ...

    def fingerprint(self) -> Hashable:
        """A value that changes whenever a client or a session shows activity."""
        ...


class IdleWatch:
    """Polls ``probe`` and calls ``on_idle(seconds_idle)`` once the daemon stayed idle for the
    window (see the module notes). ``run`` returns after ``on_idle`` or when ``finished()``."""

    def __init__(
        self,
        probe: IdleProbe,
        *,
        clock: Clock,
        window: Callable[[], float],
        on_idle: Callable[[float], object],
        finished: Callable[[], bool] = lambda: False,
        interval: float | None = None,
    ) -> None:
        self.probe = probe
        self.clock = clock
        self._window = window
        self._on_idle = on_idle
        self._finished = finished
        self._interval = interval
        self.busy: str | None = None
        """The last busy reason seen (None after an idle poll)."""
        self.idle_since = clock.monotonic()
        self.fired = False

    def window(self) -> float:
        try:
            return max(0.0, float(self._window()))
        except Exception:
            return DEFAULT_IDLE_EXIT_S

    def _quick(self) -> str | None:
        try:
            return self.probe.quick()
        except Exception as exc:
            log.debug("idle probe failed; staying up", exc_info=True)
            return f"idle probe failed ({type(exc).__name__})"

    async def _slow(self) -> str | None:
        try:
            return await self.probe.slow()
        except Exception as exc:
            log.debug("idle probe failed; staying up", exc_info=True)
            return f"idle probe failed ({type(exc).__name__})"

    def _print(self) -> Hashable:
        try:
            return self.probe.fingerprint()
        except Exception:
            return object()  # never equal: counts as activity

    async def run(self) -> None:
        self.idle_since = self.clock.monotonic()
        seen = self._print()
        while not self._finished():
            window = self.window()
            await self.clock.sleep(self._interval or poll_interval(window or DEFAULT_IDLE_EXIT_S))
            if self._finished():
                return
            window = self.window()
            reason = self._quick() or await self._slow()
            now_print = self._print()
            if now_print != seen:
                seen = now_print
                reason = reason or "recent client activity"
            now = self.clock.monotonic()
            if window <= 0:
                self.busy, self.idle_since = "idle exit disabled", now
                continue
            if reason is not None:
                if reason != self.busy:
                    log.debug("idle exit: staying up (%s)", reason)
                self.busy, self.idle_since = reason, now
                continue
            self.busy = None
            if now - self.idle_since < window:
                continue
            # Last look at what can change on the loop; nothing is awaited from here to the stop.
            final = self._quick()
            if final is not None or self._print() != seen or self._finished():
                self.busy, self.idle_since = final or "recent client activity", now
                continue
            self.fired = True
            self._on_idle(now - self.idle_since)
            return


class DaemonProbe:
    """The busy checks over a running ``GatewayDaemon`` (see the module notes)."""

    def __init__(self, daemon: GatewayDaemon, *, horizon: float = CRON_HORIZON_S) -> None:
        self.daemon = daemon
        self.horizon = horizon

    # -- loop-only facts ---------------------------------------------------------------------------

    def quick(self) -> str | None:
        daemon = self.daemon
        if daemon.lifecycle.stopping:
            return "already stopping"
        relayed = set(getattr(daemon, "relayed", ()))
        platforms = sorted(set(daemon.settings.enabled()) | (set(daemon.adapters) - relayed))
        if platforms:
            return "messaging platform enabled: " + ", ".join(platforms)
        if any(part.name == "http" for part in daemon.parts):
            return "HTTP surfaces enabled"
        for part in daemon.parts:
            reason = getattr(part, "busy_reason", None)
            why = reason() if callable(reason) else None
            if why is not None:
                return str(why)
        mux = getattr(daemon, "mux", None)
        if mux is not None and getattr(daemon, "parent", None) is None:
            why = mux.busy_reason()
            if why is not None:
                return why
        if daemon.tickets.outstanding():
            return "a client is attaching"
        ws = daemon.ws
        if ws is not None and ws.connections:
            return f"{len(ws.connections)} client connection(s)"
        host = daemon.host
        if host is not None:
            for key in host.viewers.sessions():
                if any(v.surface != MESSAGING_SURFACE for v in host.viewers.viewers(key)):
                    return f"a viewer is attached to {key}"
            for key in host.hosted():
                if host.fences.running(key) is not None or host.admissions.running(key):
                    return f"a turn runs in {key}"
        service = getattr(daemon, "service", None)
        held: list[str] = list(service.attached()) if service is not None else []
        for session_id in held:
            if not service.is_idle(session_id):  # type: ignore[union-attr]
                return f"session {session_id} is busy"
        pipeline = getattr(daemon, "pipeline", None)
        if pipeline is not None and len(pipeline.claims):
            return "a chat turn runs"
        processes = daemon.terminal_processes
        if processes is not None:
            home = str(daemon.scope.home)
            for session_id in [None, *held]:
                if any(r.status == "running" for r in processes.list(home, session_id)):
                    return "a background process runs"
        return None

    def fingerprint(self) -> Hashable:
        daemon = self.daemon
        ws = daemon.ws
        connections = tuple(sorted(ws.connections)) if ws is not None else ()
        host = daemon.host
        sessions: tuple[tuple[str, int], ...] = ()
        if host is not None:
            sessions = tuple(sorted((k, host.event_log.last_seq(k)) for k in host.hosted()))
        return (connections, sessions)

    # -- stored state ------------------------------------------------------------------------------

    async def slow(self) -> str | None:
        daemon = self.daemon
        store = getattr(daemon, "store", None)
        if store is not None:
            queued = await run_blocking(lambda: _queued_session(store), label="idle probe")
            if queued is not None:
                return f"input is queued for {queued}"
        reason = await run_blocking(self._cron, label="idle probe")
        if reason is not None:
            return reason
        return await self._automation()

    def _cron(self) -> str | None:
        from indus.cron.store import CronStore

        scope = self.daemon.scope
        jobs = CronStore(scope).load()
        now = self.daemon.clock.now()
        paused = global_pause(scope.home) is not None
        return cron_busy(jobs, home=scope.home, now=now, horizon=self.horizon, paused=paused)

    async def _automation(self) -> str | None:
        automation = self.daemon.automation
        service = getattr(self.daemon, "service", None)
        if automation is None or service is None:
            return None
        held = set(service.attached())
        if not held:
            return None
        before = self.daemon.clock.now().timestamp() + self.horizon
        for source in list(automation.scheduler.sources):
            for entry in await automation.meta.due(source.namespace, before):
                if entry.session_id in held:
                    return f"{source.namespace} due in {entry.session_id}"
        return None


def _queued_session(store: Any) -> str | None:
    """A session holding queued input the daemon can run (not paused behind an unknown turn)."""
    for session_id in ledger.sessions_with_queued(store):
        rows = ledger.pending(store, session_id)
        if not any(row.state == "unknown" for row in rows):
            return session_id
    return None


def _begin_exit(daemon: GatewayDaemon, window: float, idle_for: float) -> None:
    """Make the exit visible, then stop (synchronous: no await since the last idle check)."""
    daemon.status.set_state("draining", exit_reason=IDLE_EXIT_REASON)
    log.info(
        "no client and no work for %.0f s (%s = %g): exiting; the next client starts a new gateway",
        idle_for,
        CONFIG_KEY,
        window,
    )
    daemon.request_stop(IDLE_EXIT_REASON)


def arm(
    daemon: GatewayDaemon,
    *,
    window: Callable[[], float] | None = None,
    probe: IdleProbe | None = None,
    spawn: Callable[[Awaitable[None]], object] | None = None,
) -> IdleWatch | None:
    """Start the watch for an auto-started, unsupervised daemon; None when it does not apply."""
    if not daemon.autostarted:
        return None
    if daemon.is_supervised:
        log.info("idle exit not armed: the gateway is supervised")
        return None
    chosen = window if window is not None else (lambda: idle_exit_seconds(daemon.scope))
    holder: dict[str, IdleWatch] = {}

    def fire(idle_for: float) -> None:
        _begin_exit(daemon, holder["watch"].window(), idle_for)

    watch = IdleWatch(
        probe if probe is not None else DaemonProbe(daemon),
        clock=daemon.clock,
        window=chosen,
        on_idle=fire,
        finished=lambda: daemon.lifecycle.stopping,
    )
    holder["watch"] = watch
    seconds = watch.window()
    if seconds <= 0:
        log.info("idle exit is off (%s = 0)", CONFIG_KEY)
    else:
        log.info(
            "started by a client: exits after %.0f s with nothing to do (%s)", seconds, CONFIG_KEY
        )
    coro = watch.run()
    if spawn is not None:
        spawn(coro)
    else:
        daemon.spawn(coro, name="gateway idle exit")
    return watch
