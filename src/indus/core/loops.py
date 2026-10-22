"""In-session loops: ``/loop`` (P10-25).

``/loop <interval|auto> <prompt> [--times N] [--until "<condition>"]`` repeats a prompt as ordinary
user turns of this session; ``/loop list``, ``/loop stop [id]`` (all when no id) and
``/loop resume [id]`` (a loop the tick budget paused). A session may hold several loops (ids
``l1``, ``l2``, …), each one row in ``session_meta`` namespace ``loops`` whose ``due_at`` is the
next run, so the shared scheduler (``core.session_scheduler``) finds due loops with one indexed
query. A run in flight is tracked in this process (a run that was in flight when the process
stopped is not fired again; its next run comes at its stored due time).

Cadence: an interval token (``30s``, ``5m``, ``2h``, ``1h30m``; a bare number is prompt text) is
raised to the floor ``loops.min_interval_seconds`` (30 by default, never below 5). ``auto`` paces
itself: 60 s after a run whose final text changed, double the last wait (up to 15 min) after a run
whose text hashed the same as the one before.

A loop ends when the run's reply has ``LOOP_COMPLETE`` alone on its last line (any case, an
optional ``.`` or ``!``), after ``--times`` runs, or when the ``--until`` judge (the goal judge,
``core.goals``) rules the condition met. The backstop ``loops.max_ticks`` (100; 0 = no limit)
pauses it. The first run is due at once; every run waits for the session to be idle with nothing
in its input queue, and for an active goal (``held``) to finish or pause, so the two never take
turns in one session. Changes send ``loop.update`` (the TUI loop bar).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast

from indus.core.goals import CommandReply, GoalJudge, parse_verdict
from indus.core.session_scheduler import DueEntry, MetaStore, SessionHost, TurnEnd
from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits
from indus.protocol.events import LoopUpdate, Notice, NoticeLevel

__all__ = [
    "AUTO_CAP_S",
    "AUTO_START_S",
    "DEFAULT_MAX_TICKS",
    "DEFAULT_MIN_INTERVAL_S",
    "MARKER",
    "NAMESPACE",
    "LoopDriver",
    "LoopSpec",
    "LoopState",
    "ends_with_marker",
    "format_seconds",
    "parse_interval",
    "parse_loop_args",
    "reply_digest",
]

log = logging.getLogger(__name__)

NAMESPACE: Final = "loops"
DEFAULT_MIN_INTERVAL_S: Final = 30
LOWEST_FLOOR_S: Final = 5
DEFAULT_MAX_TICKS: Final = 100
AUTO_START_S: Final = 60
AUTO_CAP_S: Final = 15 * 60
MARKER: Final = "LOOP_COMPLETE"

_TOKEN = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.IGNORECASE)
_MARKER_LINE = re.compile(rf"^\s*{MARKER}\s*[.!]?\s*$", re.IGNORECASE)
_TIMES = re.compile(r"(?:^|\s)--times(?:\s+|=)(\S+)")
_UNTIL = re.compile(r"""(?:^|\s)--until(?:\s+|=)(?:"([^"]*)"|'([^']*)'|(.+)$)""")

type Status = Literal["active", "paused", "done", "stopped"]

RUN_TEXT: Final = (
    "[Loop {loop_id}, run {tick}{of} — {cadence}]\n{prompt}\n\n"
    "This is a repeat run of a task I set to recur in this conversation. Look at the current "
    "state afresh (earlier runs may be out of date) and report briefly what you found or did."
    "{until}\n"
    "If the task is finished or no longer makes sense, say so and end your reply with "
    f"{MARKER} alone on the last line; that stops the loop."
)
UNTIL_LINE: Final = "\nThe loop should stop once this holds: {until}. Show what you see of it."
USAGE: Final = (
    '/loop <interval|auto> <prompt> [--times N] [--until "<condition>"] · /loop list · '
    "/loop stop [id] · /loop resume [id]   (interval like 30s, 5m, 2h, 1h30m)"
)


def parse_interval(token: str) -> int | None:
    """Seconds for ``30s`` / ``5m`` / ``2h`` / ``1h30m``; None for anything else."""
    found = _TOKEN.match(token.strip()) if token and token.strip()[:1].isdigit() else None
    if found is None:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in found.groups())
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def format_seconds(seconds: float) -> str:
    """``90`` → ``1m30s``; ``3600`` → ``1h``."""
    whole = max(0, round(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{hours}h"] if hours else []
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return "".join(parts)


@dataclass(frozen=True)
class LoopSpec:
    prompt: str
    interval_s: int | None  # None: auto
    times: int = 0
    until: str = ""
    asked_s: int | None = None  # the interval typed, when the floor raised it


def parse_loop_args(text: str, *, floor_s: int = DEFAULT_MIN_INTERVAL_S) -> LoopSpec | str:
    """A new loop's spec, or a problem text."""
    rest = text.strip()
    times = 0
    found = _TIMES.search(rest)
    if found:
        raw = found.group(1)
        if not is_digits(raw) or int(raw) < 1:
            return f"--times needs a whole number above 0, not {raw!r}"
        times = int(raw)
        rest = (rest[: found.start()] + rest[found.end() :]).strip()
    until = ""
    found = _UNTIL.search(rest)
    if found:
        until = next(g for g in found.groups() if g is not None).strip()
        if not until:
            return '--until needs a condition, e.g. --until "the build is green"'
        rest = rest[: found.start()].strip()
    head, _, tail = rest.partition(" ")
    if head.lower() == "every" and tail:
        head, _, tail = tail.strip().partition(" ")
    if head.lower() == "auto":
        interval: int | None = None
    else:
        interval = parse_interval(head)
        if interval is None:
            return "Start with an interval (30s, 5m, 2h, 1h30m) or auto. " + USAGE
    prompt = tail.strip()
    if not prompt:
        return "Say what to repeat. " + USAGE
    floor = max(LOWEST_FLOOR_S, floor_s)
    if interval is not None and interval < floor:
        return LoopSpec(prompt, floor, times, until, asked_s=interval)
    return LoopSpec(prompt, interval, times, until)


def ends_with_marker(reply: str) -> bool:
    """True when the reply's last non-empty line is the stop marker."""
    lines = [line for line in (reply or "").splitlines() if line.strip()]
    return bool(lines) and _MARKER_LINE.match(lines[-1]) is not None


def reply_digest(reply: str) -> str:
    """A hash of the reply's final text, ignoring case and spacing."""
    normal = " ".join((reply or "").lower().split())
    return hashlib.sha256(normal.encode("utf-8", "replace")).hexdigest()


# -- state ---------------------------------------------------------------------------------------


@dataclass
class LoopState:
    loop_id: str
    prompt: str
    interval_s: int | None = None
    delay_s: float = AUTO_START_S
    times: int = 0
    until: str = ""
    max_ticks: int = DEFAULT_MAX_TICKS
    ticks: int = 0
    status: Status = "active"
    in_flight: bool = False
    last_digest: str = ""
    next_due_at: float | None = None
    reason: str = ""
    created_at: float = 0.0

    @property
    def cadence(self) -> str:
        if self.interval_s is None:
            return f"auto, now every {format_seconds(self.delay_s)}"
        return f"every {format_seconds(self.interval_s)}"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: object) -> LoopState | None:
        if not isinstance(raw, Mapping):
            return None
        data = cast(Mapping[str, Any], raw)
        try:
            state = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except TypeError:
            return None
        if not state.loop_id or not state.prompt:
            return None
        return state

    def line(self, now: float) -> str:
        caps = [f"{self.ticks} run(s)"]
        if self.times:
            caps = [f"{self.ticks}/{self.times} runs"]
        elif self.max_ticks:
            caps.append(f"budget {self.max_ticks}")
        if self.until:
            caps.append(f"until: {self.until}")
        when = ""
        if self.status == "active":
            if self.in_flight:
                when = ", running now"
            elif self.next_due_at is not None:
                left = self.next_due_at - now
                when = ", due now" if left <= 0 else f", next in {format_seconds(left)}"
        why = f" — {self.reason}" if self.reason and self.status != "active" else ""
        return (
            f"{self.loop_id} [{self.status}{why}] {self.cadence}, {', '.join(caps)}{when}: "
            f"{self.prompt}"
        )


# -- the driver ------------------------------------------------------------------------------------


class LoopDriver:
    """Loops of one host's sessions: commands, firing (a ``TickSource``) and run completion."""

    namespace = NAMESPACE

    def __init__(
        self,
        host: SessionHost,
        meta: MetaStore,
        clock: Clock,
        *,
        judge: GoalJudge | None = None,
        min_interval_s: int = DEFAULT_MIN_INTERVAL_S,
        max_ticks: int = DEFAULT_MAX_TICKS,
        held: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self.host = host
        self.held = held
        self.meta = meta
        self.clock = clock
        self.judge = judge
        self.min_interval_s = max(LOWEST_FLOOR_S, min_interval_s)
        self.max_ticks = max(0, max_ticks)
        self._undo: Any = None
        self._flying: set[tuple[str, str]] = set()

    def attach(self) -> None:
        if self._undo is None:
            self._undo = self.host.on_turn_end(self.on_turn_end)

    def detach(self) -> None:
        if self._undo is not None:
            self._undo()
            self._undo = None

    def _now(self) -> float:
        return self.clock.now().timestamp()

    async def _loops(self, session_id: str) -> list[LoopState]:
        rows = await self.meta.entries(session_id, NAMESPACE)
        found = [LoopState.from_json(v) for v in rows.values()]
        return sorted((s for s in found if s is not None), key=lambda s: _order(s.loop_id))

    async def _save(self, session_id: str, state: LoopState) -> None:
        due = state.next_due_at if state.status == "active" else None
        await self.meta.put(session_id, NAMESPACE, state.loop_id, state.to_json(), due_at=due)

    def _update(self, session_id: str, state: LoopState) -> None:
        due = state.next_due_at if state.status == "active" else None
        self.host.notify(
            session_id,
            LoopUpdate,
            loop_id=state.loop_id,
            status=state.status,
            prompt=state.prompt,
            ticks=state.ticks,
            next_due_at=datetime.fromtimestamp(due, UTC) if due is not None else None,
            reason=state.reason,
        )

    async def move(self, old: str, new: str) -> int:
        """Carry the loops to the session a rotating compaction continued in (P08-05)."""
        moved = 0
        for state in await self._loops(old):
            if (old, state.loop_id) in self._flying:
                self._flying.discard((old, state.loop_id))
                self._flying.add((new, state.loop_id))
            await self._save(new, state)
            await self.meta.delete(old, NAMESPACE, state.loop_id)
            moved += 1
        return moved

    # -- commands --------------------------------------------------------------------------------

    async def command(self, session_id: str, args: str) -> CommandReply:
        word, _, rest = args.strip().partition(" ")
        word, rest = word.lower(), rest.strip()
        if word in ("", "list", "status"):
            loops = [s for s in await self._loops(session_id) if s.status in ("active", "paused")]
            if not loops:
                return CommandReply(("No loops in this session.", USAGE))
            now = self._now()
            return CommandReply(tuple(s.line(now) for s in loops))
        if word in ("help", "-h", "--help"):
            return CommandReply((USAGE,))
        if word in ("stop", "resume") and " " not in rest:
            return await self._control(session_id, word, rest)
        spec = parse_loop_args(args, floor_s=self.min_interval_s)
        if isinstance(spec, str):
            return CommandReply((spec,))
        existing = await self._loops(session_id)
        number = 1 + max((_order(s.loop_id) for s in existing), default=0)
        now = self._now()
        state = LoopState(
            loop_id=f"l{number}",
            prompt=spec.prompt,
            interval_s=spec.interval_s,
            delay_s=float(spec.interval_s or AUTO_START_S),
            times=spec.times,
            until=spec.until,
            max_ticks=self.max_ticks,
            next_due_at=now,
            created_at=now,
        )
        await self._save(session_id, state)
        self._update(session_id, state)
        lines = [f"Loop {state.loop_id} set ({state.cadence}): {state.prompt}"]
        if spec.asked_s is not None:
            lines.append(
                f"(raised from {format_seconds(spec.asked_s)} to the "
                f"{format_seconds(self.min_interval_s)} minimum, loops.min_interval_seconds)"
            )
        if state.times:
            lines.append(f"Runs {state.times} time(s), then stops.")
        if state.until:
            lines.append(f"Stops once this holds: {state.until}")
        if not state.times and state.max_ticks:
            lines.append(f"Pauses after {state.max_ticks} runs (loops.max_ticks).")
        lines.append(
            f"The first run starts as soon as the session is idle; a reply ending in {MARKER} "
            f"stops it. /loop stop {state.loop_id} ends it."
        )
        return CommandReply(tuple(lines))

    async def _control(self, session_id: str, verb: str, which: str) -> CommandReply:
        loops = [s for s in await self._loops(session_id) if s.status in ("active", "paused")]
        if which:
            loops = [s for s in loops if s.loop_id == which]
            if not loops:
                return CommandReply((f"No loop {which} here. /loop list shows them.",))
        if not loops:
            return CommandReply(("No loops in this session.",))
        lines: list[str] = []
        now = self._now()
        for state in loops:
            self._flying.discard((session_id, state.loop_id))
            if verb == "stop":
                state.status, state.reason, state.in_flight = "stopped", "stopped by you", False
                lines.append(f"Loop {state.loop_id} stopped.")
            elif state.status == "paused":
                state.status, state.reason, state.in_flight = "active", "", False
                state.ticks = 0 if not state.times else state.ticks
                state.next_due_at = now + min(state.delay_s, 5.0)
                lines.append(f"Loop {state.loop_id} resumed ({state.cadence}).")
            else:
                lines.append(f"Loop {state.loop_id} is already running.")
            await self._save(session_id, state)
            self._update(session_id, state)
        return CommandReply(tuple(lines))

    # -- firing ----------------------------------------------------------------------------------

    def run_text(self, state: LoopState) -> str:
        return RUN_TEXT.format(
            loop_id=state.loop_id,
            tick=state.ticks,
            of=f" of {state.times}" if state.times else "",
            cadence=state.cadence,
            prompt=state.prompt,
            until=UNTIL_LINE.format(until=state.until) if state.until else "",
        )

    async def fire(self, entry: DueEntry, now: float) -> bool:
        """``TickSource``: start a due run of an idle session."""
        state = LoopState.from_json(entry.value)
        session_id = entry.session_id
        key = (session_id, entry.key)
        if state is None or state.status != "active" or key in self._flying:
            return False
        if self.held is not None and await self.held(session_id):
            return False  # an active goal drives the session; the run waits for it
        due_before = state.next_due_at
        state.ticks += 1
        state.in_flight = True
        # Provisional: if this process stops mid-run, the loop is due again one cadence later.
        state.next_due_at = now + state.delay_s
        self._flying.add(key)
        await self._save(session_id, state)
        started = await self.host.inject(session_id, self.run_text(state), origin="loop")
        if not started:
            self._flying.discard(key)
            state.ticks -= 1
            state.in_flight = False
            state.next_due_at = due_before
            await self._save(session_id, state)
            return False
        self._update(session_id, state)
        return True

    async def on_turn_end(self, ended: TurnEnd) -> None:
        for state in await self._loops(ended.session_id):
            key = (ended.session_id, state.loop_id)
            if key in self._flying:
                self._flying.discard(key)
                if state.status == "active":
                    await self._complete(ended, state)

    async def _complete(self, ended: TurnEnd, state: LoopState) -> None:
        sid = ended.session_id
        state.in_flight = False
        reply = ended.final_text if ended.completed else ""
        stop = await self._stop_reason(state, reply)
        if stop is not None:
            state.status, state.reason = stop
            level = NoticeLevel.INFO if state.status == "done" else NoticeLevel.WARNING
            self.host.notify(
                sid,
                Notice,
                level=level,
                text=f"Loop {state.loop_id} {state.status}: {state.reason}",
                key=f"loop_{state.loop_id}",
            )
        else:
            if state.interval_s is None:
                digest = reply_digest(reply)
                if state.last_digest and digest == state.last_digest:
                    state.delay_s = min(max(state.delay_s, AUTO_START_S) * 2, AUTO_CAP_S)
                else:
                    state.delay_s = AUTO_START_S
                state.last_digest = digest
            else:
                state.delay_s = state.interval_s
            state.next_due_at = self._now() + state.delay_s
        await self._save(sid, state)
        self._update(sid, state)

    async def _stop_reason(self, state: LoopState, reply: str) -> tuple[Status, str] | None:
        if ends_with_marker(reply):
            return "done", f"finished after {state.ticks} run(s): the task said it is complete"
        if state.until and reply.strip() and self.judge is not None:
            try:
                verdict = parse_verdict(await self.judge(state.until, (), reply))
            except Exception as exc:
                log.info("loop judge failed (%s); the loop goes on", exc)
                verdict = None
            if verdict is not None and verdict.done:
                return "done", f"the stop condition holds: {verdict.reason or state.until}"
        if state.times and state.ticks >= state.times:
            return "done", f"ran {state.ticks}/{state.times} times"
        if state.max_ticks and state.ticks >= state.max_ticks:
            return (
                "paused",
                f"{state.ticks}/{state.max_ticks} runs used (loops.max_ticks); "
                f"/loop resume {state.loop_id} goes on",
            )
        return None


def _order(loop_id: str) -> int:
    digits = loop_id[1:]
    return int(digits) if digits.isdigit() else 0
