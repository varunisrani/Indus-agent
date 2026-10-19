"""Session heartbeats: ``/heartbeat`` (alias ``/hb``) (P10-26).

``/heartbeat every <duration> <prompt>`` sends a recurring instruction into the session while it
is idle; ``/heartbeat list``, ``pause [id]``, ``resume [id]``, ``stop [id]`` (all when no id).
Duration grammar ``[every] <n> <unit>`` with units ``s``/``m``/``h``/``d`` (and their spelled-out
forms); anything under ``MIN_INTERVAL_S`` (60 s) is refused.

Each heartbeat (ids ``h1``, ``h2``, …) is one row in ``session_meta`` namespace ``heartbeats``
whose ``due_at`` is its next fire: the last fire (or the creation) plus the interval, so missed
ticks collapse into one and a restart picks the schedule up from the last fire. The shared
scheduler (``core.session_scheduler``, polling every 5 s) fires a due heartbeat only into an idle
session with nothing in its input queue; the message is an ordinary user message (no system prompt
change) that frames the instruction as recurring and asks for a one-line answer when nothing
changed. Each fire sends ``heartbeat.fired`` so surfaces can show the turn as a "wake" row.
Session-scoped and in-process only: durable schedules are cron's (P14).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Final, Literal, cast

from indus.core.goals import CommandReply
from indus.core.loops import format_seconds
from indus.core.session_scheduler import DueEntry, MetaStore, SessionHost
from indus.foundation.clock import Clock
from indus.protocol.events import HeartbeatFired

__all__ = [
    "MIN_INTERVAL_S",
    "NAMESPACE",
    "HeartbeatDriver",
    "HeartbeatState",
    "parse_duration",
    "parse_heartbeat_args",
]

log = logging.getLogger(__name__)

NAMESPACE: Final = "heartbeats"
MIN_INTERVAL_S: Final = 60

# One named group per unit; each accepts its short and spelled-out forms (s/sec/secs/second/
# seconds, m/min/mins/minute/minutes, h/hr/hrs/hour/hours, d/day/days).
_DURATION = re.compile(
    r"^(?:every\s+)?(?P<count>\d+)\s*(?:"
    r"(?P<s>s(?:ec(?:ond)?s?)?)|(?P<m>m(?:in(?:ute)?s?)?)|(?P<h>h(?:(?:ou)?rs?)?)|(?P<d>d(?:ays?)?)"
    r")(?=\s|$)",
    re.IGNORECASE,
)
_UNIT_SECONDS: Final = (("s", 1), ("m", 60), ("h", 3600), ("d", 86400))

FIRE_TEXT: Final = (
    "[Heartbeat {hb_id} — a recurring instruction, every {every}]\n{prompt}\n\n"
    "This comes back on a timer. If nothing has changed or there is nothing worth doing right "
    "now, answer in one short line saying so; do not make up work."
)
USAGE: Final = (
    "/heartbeat every <duration> <prompt> · /heartbeat list · pause [id] · resume [id] · "
    "stop [id]   (duration like 90s, 10m, 2h, 1d; at least 60 s)"
)

type Status = Literal["active", "paused", "stopped"]


def parse_duration(text: str) -> tuple[int, str] | None:
    """``(seconds, rest of the text)`` for ``[every] <n> <unit> …``; None when it does not start
    with a duration."""
    stripped = text.strip()
    found = _DURATION.match(stripped)
    if found is None:
        return None
    unit = next(size for name, size in _UNIT_SECONDS if found.group(name))
    return int(found.group("count")) * unit, stripped[found.end() :].strip()


def parse_heartbeat_args(text: str) -> tuple[int, str] | str:
    """``(interval seconds, prompt)`` for a new heartbeat, or a problem text."""
    parsed = parse_duration(text)
    if parsed is None:
        return "Start with how often, e.g. every 10m. " + USAGE
    seconds, prompt = parsed
    if seconds < MIN_INTERVAL_S:
        return (
            f"A heartbeat runs at most once a minute; {format_seconds(seconds)} is too often "
            f"(at least {MIN_INTERVAL_S} s)."
        )
    if not prompt:
        return "Say what the heartbeat should do. " + USAGE
    return seconds, prompt


@dataclass
class HeartbeatState:
    hb_id: str
    prompt: str
    interval_s: int
    status: Status = "active"
    fire_count: int = 0
    last_fired_at: float = 0.0
    created_at: float = 0.0

    @property
    def anchor(self) -> float:
        return self.last_fired_at or self.created_at

    @property
    def next_due_at(self) -> float:
        return self.anchor + self.interval_s

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: object) -> HeartbeatState | None:
        if not isinstance(raw, Mapping):
            return None
        data = cast(Mapping[str, Any], raw)
        try:
            state = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except TypeError:
            return None
        if not state.hb_id or not state.prompt or state.interval_s < MIN_INTERVAL_S:
            return None
        return state

    def line(self, now: float) -> str:
        every = format_seconds(self.interval_s)
        fired = f", fired {self.fire_count}×" if self.fire_count else ""
        if self.status == "active":
            left = self.next_due_at - now
            when = "due now" if left <= 0 else f"next in {format_seconds(left)}"
            return f"{self.hb_id} [active] every {every}, {when}{fired}: {self.prompt}"
        return f"{self.hb_id} [{self.status}] every {every}{fired}: {self.prompt}"


class HeartbeatDriver:
    """Heartbeats of one host's sessions: commands and firing (a ``TickSource``)."""

    namespace = NAMESPACE

    def __init__(self, host: SessionHost, meta: MetaStore, clock: Clock) -> None:
        self.host = host
        self.meta = meta
        self.clock = clock

    def _now(self) -> float:
        return self.clock.now().timestamp()

    async def _all(self, session_id: str) -> list[HeartbeatState]:
        rows = await self.meta.entries(session_id, NAMESPACE)
        found = [HeartbeatState.from_json(v) for v in rows.values()]
        return sorted((s for s in found if s is not None), key=lambda s: _order(s.hb_id))

    async def _save(self, session_id: str, state: HeartbeatState) -> None:
        due = state.next_due_at if state.status == "active" else None
        await self.meta.put(session_id, NAMESPACE, state.hb_id, state.to_json(), due_at=due)

    async def restore(self, session_id: str) -> int:
        """Recompute every active heartbeat's due time from its last fire (after a restart or a
        resume); returns how many were rescheduled."""
        count = 0
        for state in await self._all(session_id):
            if state.status == "active":
                await self._save(session_id, state)
                count += 1
        return count

    async def move(self, old: str, new: str) -> int:
        """Carry the heartbeats to the session a rotating compaction continued in (P08-05)."""
        moved = 0
        for state in await self._all(old):
            await self._save(new, state)
            await self.meta.delete(old, NAMESPACE, state.hb_id)
            moved += 1
        return moved

    # -- commands --------------------------------------------------------------------------------

    async def command(self, session_id: str, args: str) -> CommandReply:
        word, _, rest = args.strip().partition(" ")
        word, rest = word.lower(), rest.strip()
        if word in ("", "list", "status"):
            beats = [s for s in await self._all(session_id) if s.status != "stopped"]
            if not beats:
                return CommandReply(("No heartbeats in this session.", USAGE))
            now = self._now()
            return CommandReply(tuple(s.line(now) for s in beats))
        if word in ("pause", "resume", "stop", "clear") and " " not in rest:
            return await self._control(session_id, "stop" if word == "clear" else word, rest)
        if word in ("help", "-h", "--help"):
            return CommandReply((USAGE,))
        parsed = parse_heartbeat_args(args)
        if isinstance(parsed, str):
            return CommandReply((parsed,))
        seconds, prompt = parsed
        existing = await self._all(session_id)
        number = 1 + max((_order(s.hb_id) for s in existing), default=0)
        state = HeartbeatState(f"h{number}", prompt, seconds, created_at=self._now())
        await self._save(session_id, state)
        return CommandReply(
            (
                f"Heartbeat {state.hb_id} set: every {format_seconds(seconds)}, while the "
                f"session is idle: {prompt}",
                f"First beat in {format_seconds(seconds)}. /heartbeat stop {state.hb_id} ends it.",
            )
        )

    async def _control(self, session_id: str, verb: str, which: str) -> CommandReply:
        beats = [s for s in await self._all(session_id) if s.status != "stopped"]
        if which:
            beats = [s for s in beats if s.hb_id == which]
            if not beats:
                return CommandReply((f"No heartbeat {which} here. /heartbeat list shows them.",))
        if not beats:
            return CommandReply(("No heartbeats in this session.",))
        lines: list[str] = []
        for state in beats:
            if verb == "stop":
                state.status = "stopped"
                lines.append(f"Heartbeat {state.hb_id} stopped.")
            elif verb == "pause":
                state.status = "paused"
                lines.append(f"Heartbeat {state.hb_id} paused.")
            else:
                if state.status == "paused":
                    # Counted from now, so a stale beat never fires the moment it resumes.
                    state.status, state.last_fired_at = "active", self._now()
                lines.append(
                    f"Heartbeat {state.hb_id} active, next in {format_seconds(state.interval_s)}."
                )
            await self._save(session_id, state)
        return CommandReply(tuple(lines))

    # -- firing ----------------------------------------------------------------------------------

    async def fire(self, entry: DueEntry, now: float) -> bool:
        """``TickSource``: send a due heartbeat into an idle session."""
        state = HeartbeatState.from_json(entry.value)
        if state is None or state.status != "active":
            return False
        session_id = entry.session_id
        before = (state.fire_count, state.last_fired_at)
        state.fire_count += 1
        state.last_fired_at = now
        await self._save(session_id, state)
        text = FIRE_TEXT.format(
            hb_id=state.hb_id, every=format_seconds(state.interval_s), prompt=state.prompt
        )
        if not await self.host.inject(session_id, text, origin="heartbeat"):
            state.fire_count, state.last_fired_at = before
            await self._save(session_id, state)
            return False
        self.host.notify(
            session_id,
            HeartbeatFired,
            heartbeat_id=state.hb_id,
            prompt=state.prompt,
            fire_count=state.fire_count,
        )
        return True


def _order(hb_id: str) -> int:
    digits = hb_id[1:]
    return int(digits) if digits.isdigit() else 0
