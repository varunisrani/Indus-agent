# pyright: strict
"""Telling the agent about background jobs: completions, watched lines, heartbeats (P05-13).

A background job can announce three things: that it ended (notify-on-exit), that a line matched
one of its watch patterns, or — every N seconds while it runs — what it printed since the last
time (a heartbeat). Each announcement is a ``ProcessEvent`` whose ``text`` is the message the
agent reads in a new turn.

Where an event can go is decided by ``Deliveries``: a surface that can start a turn for a session
later (an interactive CLI/TUI/desktop session) registers a sink for that (profile home, session).
Nothing registered means nothing can be delivered: one-shot runs, cron jobs and delegated children
never register, so their notify flags are cleared and the model is told to poll instead (R02.17).
A sink should start the turn only when the job's completion has not been used up meanwhile
(``ProcessRegistry.consumed``): a model that already read the result with ``wait`` or ``log``
does not need it twice.

Watch patterns are rate-limited so a chatty process cannot force a turn per line (R02.18):

- per process, at most one notice per ``WATCH_SPACING_S`` (15 s); a 15 s window in which matches
  had to be dropped is a strike; ``WATCH_STRIKE_LIMIT`` (3) strike windows in a row, or
  ``WATCH_LIFETIME_MAX`` (8) delivered notices, switch watching off for that process — it falls
  back to one notice on exit, and one message says so;
- across all processes, at most ``GLOBAL_MAX_PER_WINDOW`` (15) notices per ``GLOBAL_WINDOW_S``
  (10 s); one more trips a breaker that drops every match for ``GLOBAL_COOLDOWN_S`` (30 s), with
  one message when it trips and one (with the count) when it lets matches through again.

The limiters are plain state machines over a time value the caller passes, so they run on the
injected clock.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "COMPLETION_TAIL_CHARS",
    "GLOBAL_COOLDOWN_S",
    "GLOBAL_MAX_PER_WINDOW",
    "GLOBAL_WINDOW_S",
    "HEARTBEAT_CHARS",
    "HEARTBEAT_MIN_S",
    "HEARTBEAT_TICK_S",
    "WATCH_LIFETIME_MAX",
    "WATCH_SPACING_S",
    "WATCH_STRIKE_LIMIT",
    "BreakerVerdict",
    "CompletionSink",
    "Deliveries",
    "EventKind",
    "GlobalBreaker",
    "HeartbeatState",
    "ProcessEvent",
    "WatchState",
    "WatchVerdict",
    "completion_text",
    "heartbeat_text",
    "watch_text",
]

log = logging.getLogger(__name__)

COMPLETION_TAIL_CHARS = 2_000
WATCH_SPACING_S = 15.0
WATCH_STRIKE_LIMIT = 3
WATCH_LIFETIME_MAX = 8
WATCH_MAX_LINES = 20
WATCH_MAX_CHARS = 2_000
GLOBAL_MAX_PER_WINDOW = 15
GLOBAL_WINDOW_S = 10.0
GLOBAL_COOLDOWN_S = 30.0
HEARTBEAT_MIN_S = 60
HEARTBEAT_TICK_S = 5.0
HEARTBEAT_CHARS = 2_000

type EventKind = Literal[
    "completion", "watch_match", "watch_off", "watch_paused", "watch_resumed", "heartbeat"
]


@dataclass(frozen=True, slots=True)
class ProcessEvent:
    """One announcement about a background job; ``text`` is what the agent reads."""

    kind: EventKind
    process_id: str
    home: str
    session_id: str | None
    command: str
    text: str
    output: str = ""
    exit_code: int | None = None
    reason: str = ""
    pattern: str = ""
    suppressed: int = 0
    seq: int = 0


type CompletionSink = Callable[[ProcessEvent], None]
"""Takes an event and arranges a turn for it (now, or when the running turn ends)."""


class Deliveries:
    """Which (profile home, session) pairs can be told about background jobs later."""

    def __init__(self) -> None:
        self._sinks: dict[tuple[str, str], CompletionSink] = {}
        self._lock = threading.Lock()

    def register(self, home: str, session_id: str, sink: CompletionSink) -> Callable[[], None]:
        """Route this session's events to ``sink``; returns the function that undoes it."""
        key = (str(home), session_id)
        with self._lock:
            self._sinks[key] = sink

        def unregister() -> None:
            with self._lock:
                if self._sinks.get(key) is sink:
                    del self._sinks[key]

        return unregister

    def deliverable(self, home: str, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._lock:
            return (str(home), session_id) in self._sinks

    def deliver(self, event: ProcessEvent) -> bool:
        """Hand ``event`` to its session's sink; False when nobody can receive it."""
        if not event.session_id:
            return False
        with self._lock:
            sink = self._sinks.get((event.home, event.session_id))
        if sink is None:
            log.info("no receiver for %s of %s; dropped", event.kind, event.process_id)
            return False
        try:
            sink(event)
        except Exception:
            log.warning("delivering %s of %s failed", event.kind, event.process_id, exc_info=True)
            return False
        return True


# -- watch patterns -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WatchVerdict:
    """What one batch of output lines means for a watched process."""

    deliver: bool = False
    pattern: str = ""
    lines: tuple[str, ...] = ()
    suppressed: int = 0  # matches dropped since the last delivered notice
    switched_off: str = ""  # why watching just stopped ("" = it did not)


@dataclass
class WatchState:
    """Per-process watch-pattern state (see the module notes)."""

    patterns: tuple[str, ...]
    off: bool = False
    window_ends: float = 0.0
    window_dropped: bool = False
    strikes: int = 0
    delivered: int = 0
    dropped: int = 0

    def matches(self, lines: list[str]) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for line in lines:
            for pattern in self.patterns:
                if pattern and pattern in line:
                    found.append((pattern, line.rstrip()))
                    break
        return found

    def offer(self, lines: list[str], now: float) -> WatchVerdict:
        """Feed complete output lines seen at ``now``."""
        if self.off or not self.patterns:
            return WatchVerdict()
        hits = self.matches(lines)
        if not hits:
            return WatchVerdict()
        if now < self.window_ends:
            self.dropped += len(hits)
            if self.window_dropped:
                return WatchVerdict()
            self.window_dropped = True
            self.strikes += 1
            if self.strikes < WATCH_STRIKE_LIMIT:
                return WatchVerdict()
            self.off = True
            return WatchVerdict(
                suppressed=self.dropped,
                switched_off=(
                    f"matches kept arriving faster than one per {WATCH_SPACING_S:g} s in "
                    f"{WATCH_STRIKE_LIMIT} windows in a row"
                ),
            )
        if self.window_ends and not self.window_dropped:
            self.strikes = 0  # the last window was calm: the run of strikes is over
        self.window_dropped = False
        self.window_ends = now + WATCH_SPACING_S
        self.delivered += 1
        suppressed, self.dropped = self.dropped, 0
        reason = ""
        if self.delivered >= WATCH_LIFETIME_MAX:
            self.off = True
            reason = f"{WATCH_LIFETIME_MAX} notices were sent for this process"
        return WatchVerdict(
            deliver=True,
            pattern=hits[0][0],
            lines=tuple(line for _, line in hits),
            suppressed=suppressed,
            switched_off=reason,
        )


@dataclass(frozen=True, slots=True)
class BreakerVerdict:
    admit: bool
    tripped: bool = False  # this call tripped the breaker
    resumed: int = -1  # >= 0: the breaker just let matches through again after dropping this many


@dataclass
class GlobalBreaker:
    """The cross-process limit on watch notices (see the module notes)."""

    window_start: float = 0.0
    hits: int = 0
    paused_until: float = 0.0
    dropped: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def admit(self, now: float) -> BreakerVerdict:
        with self._lock:
            resumed = -1
            if self.paused_until and now >= self.paused_until:
                resumed, self.dropped = self.dropped, 0
                self.paused_until = 0.0
                self.window_start, self.hits = now, 0
            if self.paused_until:
                self.dropped += 1
                return BreakerVerdict(False)
            if now - self.window_start >= GLOBAL_WINDOW_S:
                self.window_start, self.hits = now, 0
            if self.hits < GLOBAL_MAX_PER_WINDOW:
                self.hits += 1
                return BreakerVerdict(True, resumed=resumed)
            self.paused_until = now + GLOBAL_COOLDOWN_S
            self.dropped += 1
            return BreakerVerdict(False, tripped=True, resumed=resumed)


# -- heartbeats ---------------------------------------------------------------------------------


@dataclass
class HeartbeatState:
    """Per-process heartbeat: every ``every_s`` seconds, the output printed since the last one."""

    every_s: int
    last_at: float
    seen_total: int = 0
    seq: int = 0

    def due(self, now: float) -> bool:
        return now - self.last_at >= self.every_s

    def take(self, now: float, buffer: str, total: int) -> tuple[str, int] | None:
        """The new output (newest ``HEARTBEAT_CHARS``) and how many characters were left out
        before it; None for a quiet tick (nothing new: no message at all)."""
        self.last_at = now
        new = total - self.seen_total
        self.seen_total = total
        if new <= 0:
            return None
        fresh = buffer[-new:] if new <= len(buffer) else buffer
        omitted = new - len(fresh)
        if len(fresh) > HEARTBEAT_CHARS:
            omitted += len(fresh) - HEARTBEAT_CHARS
            fresh = fresh[-HEARTBEAT_CHARS:]
        self.seq += 1
        return fresh, omitted


# -- what the agent reads -----------------------------------------------------------------------


def _ended_how(reason: str, exit_code: int | None, source: str) -> str:
    if reason == "killed":
        return f"was stopped ({source or 'kill'})"
    if reason == "failed_start":
        return "could not be started"
    if reason == "lost":
        return "was lost (its backend went away)"
    if exit_code == 0:
        return "finished successfully"
    return f"finished with exit code {exit_code}"


def completion_text(
    process_id: str,
    command: str,
    output: str,
    *,
    exit_code: int | None,
    reason: str,
    source: str = "",
    cut: int = 0,
) -> str:
    lines = [
        f"[Background process {process_id} {_ended_how(reason, exit_code, source)}.",
        f"Command: {command}",
    ]
    if cut > 0:
        lines.append(
            f"Last output ({cut:,} earlier characters not shown; process_manage log with "
            f'session_id "{process_id}" has more):'
        )
    else:
        lines.append("Output:")
    lines.append((output or "(no output)") + "]")
    return "\n".join(lines)


def watch_text(process_id: str, command: str, pattern: str, output: str, suppressed: int) -> str:
    text = (
        f'[Background process {process_id} printed a line matching "{pattern}".\n'
        f"Command: {command}\nMatching lines:\n{output}"
    )
    if suppressed:
        text += f"\n({suppressed} more matches were held back by the rate limit)"
    return text + "]"


def watch_off_text(process_id: str, why: str, suppressed: int) -> str:
    held = f" {suppressed} matches were held back." if suppressed else ""
    return (
        f"[Watching the output of background process {process_id} has stopped: {why}.{held} "
        "You will get one message when it exits instead.]"
    )


def breaker_text(*, tripped: bool, dropped: int = 0) -> str:
    if tripped:
        return (
            f"[Too many watch-pattern matches: more than {GLOBAL_MAX_PER_WINDOW} in "
            f"{GLOBAL_WINDOW_S:g} s across all background processes. Matches are not reported "
            f"for the next {GLOBAL_COOLDOWN_S:g} s.]"
        )
    return f"[Watch-pattern matches are reported again; {dropped} were dropped while paused.]"


def heartbeat_text(
    process_id: str,
    command: str,
    output: str,
    *,
    seq: int,
    elapsed_s: float,
    every_s: int,
    omitted: int,
) -> str:
    shown = f"({omitted:,} earlier characters left out)\n{output}" if omitted else output
    return (
        f"[Background process {process_id} is still running ({_age(elapsed_s)}, update {seq}; "
        f"the next comes in {every_s} s if it prints more, and you will be told when it exits).\n"
        f"Command: {command}\nOutput since the last update:\n{shown}]"
    )


def _age(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
