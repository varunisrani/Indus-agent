"""Summary failure cooldowns and the durable anti-thrash counters (P08-05, R01.9).

A failed summary call arms a cooldown before automatic compression may try again:

- ``stall`` (the call outlived ``compression.timeout_s``): 60 → 300 → 900 s, one rung per
  consecutive stall, never shorter than ``timeout_s``; the second consecutive stall commits the
  deterministic summary instead of giving up again;
- ``truncated`` (cut by the output cap): the same ladder on its own counter;
- ``decode`` / ``empty`` (closed stream, bad JSON, no text): a flat 30 s;
- ``overload``, ``access``, ``error``: 60 s; the third consecutive overload commits the
  deterministic summary; a generic ``error`` commits it at once (only access, decode, empty,
  truncation, overload and a first stall are reasons to keep the transcript as it is);
- ``unavailable`` (no summariser at all): the deterministic summary, then 600 s.

``abort_on_summary_failure: true`` turns every "commit the deterministic summary" into an abort.
A success resets the rungs and the overload streak; a manual ``/compress`` clears the cooldown.

The deadline and error text, the fallback streak, the ineffective count and the overload streak
live in the P02 session columns (``state.compression``); the two ladder rungs and the breaker's
probe time live in ``model_config["compression_state"]`` so a restart resumes the same ladder.
``MemoryCounters`` is the in-process variant for unsaved sessions and tests.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

from indus.core.compression.anchor import ConfigBlobStore
from indus.core.compression.summarize import FailureKind

if TYPE_CHECKING:
    from indus.foundation.clock import Clock
    from indus.state.registry import StoreHandle

log = logging.getLogger(__name__)

__all__ = [
    "FLAT_COOLDOWN",
    "LADDER",
    "OTHER_COOLDOWN",
    "OVERLOAD_ESCALATION",
    "UNAVAILABLE_COOLDOWN",
    "CooldownBook",
    "CounterStore",
    "Counters",
    "FailureDecision",
    "MemoryCounters",
    "StoreCounters",
]

LADDER = (60.0, 300.0, 900.0)
FLAT_COOLDOWN = 30.0
OTHER_COOLDOWN = 60.0
UNAVAILABLE_COOLDOWN = 600.0
OVERLOAD_ESCALATION = 3
STALLS_BEFORE_FALLBACK = 2
STATE_KEY = "compression_state"


@dataclass
class Counters:
    cooldown_until: float | None = None
    cooldown_error: str | None = None
    fallback_streak: int = 0
    ineffective_count: int = 0
    overload_streak: int = 0
    stall_rung: int = 0
    truncated_rung: int = 0
    probe_at: float | None = None


class CounterStore(Protocol):
    async def read(self) -> Counters: ...

    async def write(self, counters: Counters) -> None: ...


@dataclass
class MemoryCounters:
    counters: Counters = field(default_factory=Counters)

    async def read(self) -> Counters:
        return Counters(**vars(self.counters))

    async def write(self, counters: Counters) -> None:
        self.counters = Counters(**vars(counters))


class StoreCounters:
    """Counters of one session in the P02 store (columns + one ``model_config`` blob)."""

    def __init__(self, handle: StoreHandle, session_id: str, blobs: ConfigBlobStore) -> None:
        self.handle = handle
        self.session_id = session_id
        self.blobs = blobs

    async def read(self) -> Counters:
        from indus.foundation.blocking import run_blocking
        from indus.state import compression

        stored = await run_blocking(
            lambda: compression.counters(self.handle, self.session_id),
            pool="io",
            deadline=30.0,
            label="read compression counters",
        )
        raw = await self.blobs.load(self.session_id, STATE_KEY)
        extra: Mapping[str, Any] = cast(Mapping[str, Any], raw) if isinstance(raw, Mapping) else {}
        found = Counters(
            stall_rung=_count(extra.get("stall")),
            truncated_rung=_count(extra.get("truncated")),
            probe_at=_number(extra.get("probe_at")),
        )
        if stored is not None:
            found.cooldown_until = stored.cooldown_until
            found.cooldown_error = stored.cooldown_error
            found.fallback_streak = stored.fallback_streak
            found.ineffective_count = stored.ineffective_count
            found.overload_streak = stored.overload_streak
        return found

    async def write(self, counters: Counters) -> None:
        from indus.state import sessions

        sid = self.session_id
        blob = {
            "stall": counters.stall_rung,
            "truncated": counters.truncated_rung,
            "probe_at": counters.probe_at,
        }

        def put(conn: Any) -> None:
            conn.execute(
                "UPDATE sessions SET compression_cooldown_until = ?, "
                "compression_cooldown_error = ?, compression_fallback_streak = ?, "
                "compression_ineffective_count = ?, "
                "compression_overload_streak = ? WHERE id = ?",
                (
                    counters.cooldown_until,
                    counters.cooldown_error,
                    max(0, counters.fallback_streak),
                    max(0, counters.ineffective_count),
                    max(0, counters.overload_streak),
                    sid,
                ),
            )
            sessions.patch_model_config_in(conn, sid, {STATE_KEY: blob})

        await self.handle.awrite(put)


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True)
class FailureDecision:
    """What a failed summary means for this attempt."""

    kind: FailureKind
    cooldown_s: float
    commit_fallback: bool


def _rung(step: int, floor: float) -> float:
    return max(LADDER[min(max(step, 1), len(LADDER)) - 1], floor)


class CooldownBook:
    """The durable counters of one session plus the decisions built on them."""

    def __init__(
        self,
        store: CounterStore,
        clock: Clock,
        *,
        timeout_s: float = 120.0,
        abort_on_failure: bool = False,
    ) -> None:
        self.store = store
        self.clock = clock
        self.timeout_s = timeout_s
        self.abort_on_failure = abort_on_failure

    def _now(self) -> float:
        return self.clock.now().timestamp()

    async def read(self) -> Counters:
        try:
            return await self.store.read()
        except Exception:
            log.warning("could not read the compression counters; assuming none", exc_info=True)
            return Counters()

    async def _write(self, counters: Counters) -> None:
        try:
            await self.store.write(counters)
        except Exception:
            log.warning("could not store the compression counters", exc_info=True)

    async def remaining(self) -> float:
        """Seconds left on the cooldown (0 when none is armed)."""
        until = (await self.read()).cooldown_until
        return max(0.0, until - self._now()) if until is not None else 0.0

    async def clear(self) -> None:
        """Manual compress: drop the cooldown (the ladder rungs stay)."""
        counters = await self.read()
        counters.cooldown_until = None
        counters.cooldown_error = None
        await self._write(counters)

    async def failure(self, kind: FailureKind, detail: str = "") -> FailureDecision:
        """Arm the cooldown for ``kind`` and say whether to commit the deterministic summary."""
        counters = await self.read()
        commit = False
        if kind == "stall":
            counters.stall_rung += 1
            seconds = _rung(counters.stall_rung, self.timeout_s)
            commit = counters.stall_rung >= STALLS_BEFORE_FALLBACK
        elif kind == "truncated":
            counters.truncated_rung += 1
            seconds = _rung(counters.truncated_rung, self.timeout_s)
        elif kind in ("decode", "empty"):
            seconds = FLAT_COOLDOWN
        elif kind == "overload":
            counters.overload_streak += 1
            seconds = OTHER_COOLDOWN
            commit = counters.overload_streak >= OVERLOAD_ESCALATION
        elif kind == "unavailable":
            seconds = UNAVAILABLE_COOLDOWN
            commit = True
        elif kind == "access":
            seconds = OTHER_COOLDOWN
        else:
            seconds = OTHER_COOLDOWN
            commit = True
        if self.abort_on_failure:
            commit = False
        until = self._now() + seconds
        if counters.cooldown_until is None or counters.cooldown_until < until:
            counters.cooldown_until = until
        counters.cooldown_error = (detail or kind)[:300]
        await self._write(counters)
        log.warning(
            "compression summary failed (%s); automatic compression paused for %.0f s%s",
            kind,
            seconds,
            ", committing the deterministic summary" if commit else "",
        )
        return FailureDecision(kind, seconds, commit)

    async def success(self, *, fallback: bool) -> None:
        """A compaction completed (with a model summary, or with the deterministic one)."""
        counters = await self.read()
        counters.overload_streak = 0
        if fallback:
            counters.fallback_streak += 1
        else:
            counters.fallback_streak = 0
            counters.stall_rung = 0
            counters.truncated_rung = 0
        await self._write(counters)

    async def set_ineffective(self, count: int) -> None:
        counters = await self.read()
        if counters.ineffective_count == count:
            return
        counters.ineffective_count = max(0, count)
        if count < 2:
            counters.probe_at = None
        await self._write(counters)

    async def set_probe(self, at: float | None) -> None:
        counters = await self.read()
        counters.probe_at = at
        await self._write(counters)
