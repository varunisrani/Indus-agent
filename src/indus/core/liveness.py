"""Liveness watchdog and wall-clock run budget for running turns (P08-16, A63, A34).

**Watchdog.** A turn that shows no activity for ``agent.turn_liveness_s`` seconds (default 600,
polled every 15 s; ``<= 0`` turns it off) is stopped by the system, so a wedged turn cannot hold
its session for ever. Activity is ``TurnState.touch`` on the loop (every model call, tool round and
stream event the loop sees) and ``Watchdog.heartbeat`` from worker threads.

The abort is decided on an *observation* — the turn's generation and the time of its last activity
— and committed only when both still hold when they are read again under the activity lock. A
heartbeat that lands between the two reads, a turn that ended, or a newer turn (another
generation) vetoes it, so a turn that resumed is never stopped. The commit runs on the loop with no
``await`` between the re-read and the interrupt; loop-side touches cannot slip in between.

**Run budget.** ``agent.run_budget_s`` (off by default; the CLI's ``--run-budget`` sets the same
value) bounds a whole run on the wall clock. At 80 % one wrap-up notice is appended to the newest
tool result that is not stored yet — never a new system message, so the cached prompt prefix is
untouched; it is latched only once it really landed. When the budget is spent the turn is stopped
and ends ``budget_exhausted``. ``cap_timeout`` limits implicit provider timeouts to
``max(60 s, 50 % of what is left)``; explicitly configured timeouts are never capped.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from indus.core.exit_reasons import ExitReason, Failure
from indus.protocol.events import Notice, NoticeLevel

if TYPE_CHECKING:
    from indus.core.transcript import TranscriptRow
    from indus.core.turn_state import TurnState
    from indus.foundation.clock import Clock
    from indus.foundation.scope import Scope

log = logging.getLogger(__name__)

__all__ = [
    "BUDGET_EXHAUSTED_NOTICE",
    "DEFAULT_LIVENESS_S",
    "LIVENESS_NOTICE",
    "POLL_S",
    "WRAP_UP_NOTICE",
    "LivenessSettings",
    "Observation",
    "RunBudget",
    "Watchdog",
    "cap_timeout",
    "register",
    "run_budget_from",
]

DEFAULT_LIVENESS_S = 600.0
POLL_S = 15.0
WRAP_UP_AT = 0.8
MIN_IMPLICIT_TIMEOUT_S = 60.0

LIVENESS_NOTICE = (
    "This reply was stopped: it showed no activity for {seconds} s. The conversation is free "
    "again; send your message once more to retry."
)
BUDGET_EXHAUSTED_NOTICE = "The run's time budget ({seconds} s) is used up; the reply was stopped."
WRAP_UP_NOTICE = (
    "[Time check: most of this run's time budget is used. Finish the current step, then give "
    "your final answer with what you have; do not start new long work.]"
)


# -- settings --------------------------------------------------------------------------------------


def _seconds(raw: object, *, key: str, default: float) -> float:
    """A finite number of seconds from config; anything else warns and gives ``default``."""
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        log.warning("%s: %r is not a number of seconds; using %s", key, raw, default)
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s: %r is not a number of seconds; using %s", key, raw, default)
        return default
    if not math.isfinite(value):
        log.warning("%s: %r is not a finite number; using %s", key, raw, default)
        return default
    return value


def _config_value(scope: Scope | None, key: str) -> object:
    if scope is None or scope.config is None:
        return None
    try:
        return scope.config.get(key)
    except Exception:
        log.debug("%s unreadable", key, exc_info=True)
        return None


@dataclass(frozen=True)
class LivenessSettings:
    """``timeout_s`` is ``None`` when the watchdog is off."""

    timeout_s: float | None = DEFAULT_LIVENESS_S
    poll_s: float = POLL_S

    @classmethod
    def from_value(cls, raw: object, *, poll_s: float = POLL_S) -> LivenessSettings:
        value = _seconds(raw, key="agent.turn_liveness_s", default=DEFAULT_LIVENESS_S)
        return cls(timeout_s=value if value > 0 else None, poll_s=max(0.01, poll_s))

    @classmethod
    def from_scope(cls, scope: Scope | None) -> LivenessSettings:
        return cls.from_value(_config_value(scope, "agent.turn_liveness_s"))


# -- run budget ------------------------------------------------------------------------------------


class RunBudget:
    """A wall-clock budget for a run, started when it is made (see the module notes)."""

    def __init__(self, seconds: float, clock: Clock) -> None:
        self.seconds = float(seconds)
        self.clock = clock
        self.started = clock.monotonic()
        self.wrapped_up = False

    @property
    def elapsed(self) -> float:
        return max(0.0, self.clock.monotonic() - self.started)

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed)

    @property
    def fraction(self) -> float:
        return self.elapsed / self.seconds if self.seconds > 0 else 0.0

    @property
    def exhausted(self) -> bool:
        return self.elapsed >= self.seconds

    def wrap_up(self, rows: list[TranscriptRow]) -> bool:
        """Append the wrap-up notice once, past 80 %, to the newest tool row not stored yet.

        False (nothing latched) when it is not time yet, it already happened, or the newest tool
        row is already stored (an older row may sit in a provider's cache; it is never edited).
        """
        if self.wrapped_up or self.fraction < WRAP_UP_AT:
            return False
        for row in reversed(rows):
            if row.role != "tool" or row.scaffold:
                continue
            if row.row_id is not None:
                return False
            content: Any = row.content
            if isinstance(content, list):
                parts = cast(list[Any], content)
                row.content = [*parts, {"type": "text", "text": WRAP_UP_NOTICE}]
            else:
                text = content if isinstance(content, str) else ""
                row.content = f"{text}\n\n{WRAP_UP_NOTICE}" if text else WRAP_UP_NOTICE
            self.wrapped_up = True
            log.info(
                "run budget wrap-up notice added (budget %.0f s, %.0f s used)",
                self.seconds,
                self.elapsed,
            )
            return True
        return False


def run_budget_from(raw: object, clock: Clock) -> RunBudget | None:
    """A budget for ``agent.run_budget_s`` / ``--run-budget``; ``None`` when off (``<= 0``)."""
    value = _seconds(raw, key="agent.run_budget_s", default=0.0)
    return RunBudget(value, clock) if value > 0 else None


def cap_timeout(seconds: float, budget: RunBudget | None, *, explicit: bool) -> float:
    """``seconds`` limited to ``max(60, 50 % of the budget left)`` unless set explicitly."""
    if budget is None or explicit:
        return seconds
    return min(seconds, max(MIN_IMPLICIT_TIMEOUT_S, budget.remaining * 0.5))


# -- the watchdog ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    generation: int
    stamp: float
    idle_s: float


class Watchdog:
    """Watches one turn (see the module notes). ``start`` runs it as a task on the loop."""

    def __init__(
        self,
        state: TurnState,
        settings: LivenessSettings,
        *,
        budget: RunBudget | None = None,
    ) -> None:
        self.state = state
        self.settings = settings
        self.budget = budget
        self.generation = state.generation
        self.lock = threading.Lock()
        self.fired: str | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def clock(self) -> Clock:
        return self.state.clock

    # -- activity ------------------------------------------------------------------------------

    def heartbeat(self, note: str = "") -> None:
        """Mark activity from any thread (a tool or stream that is still working)."""
        with self.lock:
            self.state.last_activity = self.clock.monotonic()
            if note:
                self.state.activity_note = note

    def _active(self) -> bool:
        state = self.state
        return (
            not state.ended
            and state.control.phase != "done"
            and state.control.generation == self.generation
        )

    def observe(self) -> Observation | None:
        """The current (generation, last activity) pair, or ``None`` when the turn is over."""
        with self.lock:
            if not self._active():
                return None
            stamp = self.state.last_activity
        return Observation(self.generation, stamp, max(0.0, self.clock.monotonic() - stamp))

    def commit(self, seen: Observation, reason: str) -> bool:
        """Stop the turn if ``seen`` still describes it (re-read under the lock)."""
        state = self.state
        with self.lock:
            if not self._active() or state.last_activity != seen.stamp:
                return False
            if seen.generation != state.control.generation:
                return False
            if reason == "budget":
                total = self.budget.seconds if self.budget is not None else 0.0
                text = BUDGET_EXHAUSTED_NOTICE.format(seconds=round(total))
                state.end(ExitReason.BUDGET_EXHAUSTED, final_text=text)
            else:
                text = LIVENESS_NOTICE.format(seconds=round(seen.idle_s))
                failure = Failure("liveness", True, detail=f"no activity for {seen.idle_s:.0f} s")
                state.end(ExitReason.INTERRUPTED_BY_SYSTEM, final_text=text, failure=failure)
            stopped = state.control.interrupt(seen.generation, by_system=True)
        self.fired = reason
        state.emit(Notice, level=NoticeLevel.WARNING, text=text, key=f"turn_{reason}")
        if reason == "liveness":
            log.error(
                "turn %s of session %s stopped: no activity for %.0f s (last: %s)",
                state.turn_id,
                state.session_id,
                seen.idle_s,
                state.activity_note or "unknown",
            )
        else:
            log.warning("turn %s stopped: the run budget is used up", state.turn_id)
        return stopped

    # -- polling -------------------------------------------------------------------------------

    def tick(self) -> bool:
        """One poll; False when the watchdog is done (the turn ended or was stopped)."""
        seen = self.observe()
        if seen is None:
            return False
        if self.budget is not None and self.budget.exhausted:
            return not self.commit(seen, "budget")
        timeout = self.settings.timeout_s
        if timeout is not None and seen.idle_s >= timeout:
            return not self.commit(seen, "liveness")
        return True

    def _wait_s(self) -> float:
        wait = self.settings.poll_s
        if self.budget is not None:
            wait = min(wait, max(0.01, self.budget.remaining))
        return wait

    async def run(self) -> None:
        while True:
            await self.clock.sleep(self._wait_s())
            if not self.tick():
                return

    def start(self) -> asyncio.Task[None]:
        self._task = asyncio.get_running_loop().create_task(self.run(), name="turn watchdog")
        return self._task

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()


def register(state: TurnState) -> Watchdog | None:
    """Start watching ``state`` (called by the prologue). ``None`` when nothing is to watch.

    The run budget comes from ``SessionRuntime.run_budget``; when the runtime has none and the
    config sets ``agent.run_budget_s``, one is started now and kept on the runtime for the run.
    The watchdog stops itself when the turn ends and is cancelled with the turn's task.
    """
    runtime = state.runtime
    settings = LivenessSettings.from_scope(runtime.scope)
    budget = runtime.run_budget
    if budget is None:
        budget = run_budget_from(_config_value(runtime.scope, "agent.run_budget_s"), state.clock)
        runtime.run_budget = budget
    if settings.timeout_s is None and budget is None:
        return None
    watchdog = Watchdog(state, settings, budget=budget)
    watchdog.start()
    current = asyncio.current_task()
    if current is not None:
        current.add_done_callback(lambda _: watchdog.stop())
    return watchdog
