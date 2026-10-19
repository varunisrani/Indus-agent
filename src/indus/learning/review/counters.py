"""Review counters and the post-turn trigger (P09-14).

Two counters per session say when the background review (``review.fork``) is due:

- memory: one more per user turn while the ``memory`` tool is offered (and the host says a store
  is on); due at ``memory.review_interval`` (default 10), then back to 0. A turn in which the agent
  itself called ``memory`` sets it to 0. A resumed session starts at (prior user turns mod
  interval);
- skills: one more per tool-calling iteration while ``skill_manage`` is offered; an iteration that
  calls ``skill_manage`` sets it to 0. At turn end it is due at or over ``skills.review_interval``
  (default 10), then back to 0.

The host also resets a counter the moment the agent's own write succeeds (``note_write``, fed by
``MemoryTool.on_success`` and ``skill_manage``'s success listeners). An interval of 0 or less turns
its counter off. ``ReviewTrigger.after_turn`` is the
``SessionRuntime.after_turn`` hook the exit funnel runs once the turn is stored: it moves the
counters and, when something is due and the turn qualifies (a final answer, not interrupted, not
failed; the session is not a no-review one; ``learning.review.enabled``), hands a deep-copied
snapshot to the runner's ``start``. The hook never waits for the review.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from indus.core.exit_reasons import INTERRUPTED
from indus.core.transcript import TranscriptRow, call_name

if TYPE_CHECKING:
    from indus.core.turn_state import TurnState
    from indus.foundation.scope import Scope

__all__ = [
    "DEFAULT_INTERVAL",
    "KINDS",
    "MEMORY_TOOL",
    "NO_REVIEW_CONTEXTS",
    "SKILL_TOOL",
    "Kind",
    "ReviewCounters",
    "ReviewRequest",
    "ReviewTrigger",
    "TurnFacts",
    "interval_from",
    "review_enabled",
]

log = logging.getLogger(__name__)

type Kind = Literal["memory", "skills"]

KINDS: tuple[Kind, ...] = ("memory", "skills")
DEFAULT_INTERVAL = 10
MEMORY_TOOL = "memory"
SKILL_TOOL = "skill_manage"
NO_REVIEW_CONTEXTS = frozenset({"cron", "single_query"})
"""Run contexts that never review: scheduled jobs and one-shot runs (``-z``, finite ``chat -q``)."""

_FALSE_WORDS = frozenset({"false", "no", "off", "0"})


def interval_from(scope: Scope | None, key: str) -> int:
    """``key`` as a whole number of steps; missing or unreadable → the default (10)."""
    config = scope.config if scope is not None else None
    if config is None:
        return DEFAULT_INTERVAL
    try:
        value = config.get(key)
    except Exception:
        log.debug("%s unreadable; using %d", key, DEFAULT_INTERVAL, exc_info=True)
        return DEFAULT_INTERVAL
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return DEFAULT_INTERVAL
    try:
        return int(value)
    except ValueError:
        log.warning("%s is not a whole number (%r); using %d", key, value, DEFAULT_INTERVAL)
        return DEFAULT_INTERVAL


def review_enabled(scope: Scope | None) -> bool:
    """``learning.review.enabled``; an unreadable value counts as on (fail open, with a warning)."""
    config = scope.config if scope is not None else None
    if config is None:
        return True
    try:
        value = config.get("learning.review.enabled")
    except Exception:
        log.warning("learning.review.enabled is unreadable; reviews stay on", exc_info=True)
        return True
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    log.warning("learning.review.enabled has an odd value (%r); reviews stay on", value)
    return True


# -- the facts of one finished turn ---------------------------------------------------------------


@dataclass(frozen=True)
class TurnFacts:
    """What the counters need from a finished turn.

    ``prior_user_turns``: user rows stored before this turn's; ``rounds``: the tool names of each
    tool-calling iteration of this turn, in order.
    """

    prior_user_turns: int = 0
    memory_available: bool = False
    skills_available: bool = False
    rounds: tuple[frozenset[str], ...] = ()
    user_turn: bool = True

    @property
    def called_memory(self) -> bool:
        return any(MEMORY_TOOL in names for names in self.rounds)

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[TranscriptRow],
        user_index: int,
        tool_names: Iterable[str],
        *,
        memory_ready: bool = True,
    ) -> TurnFacts:
        names = frozenset(tool_names)
        before = rows[:user_index] if user_index >= 0 else rows
        prior = sum(1 for r in before if r.role == "user" and not r.scaffold and not r.is_steer)
        rounds: list[frozenset[str]] = []
        if user_index >= 0:
            for row in rows[user_index + 1 :]:
                if row.role == "assistant" and row.tool_calls and not row.scaffold:
                    rounds.append(frozenset(call_name(c) or "" for c in row.tool_calls))
        return cls(
            prior_user_turns=prior,
            memory_available=MEMORY_TOOL in names and memory_ready,
            skills_available=SKILL_TOOL in names,
            rounds=tuple(rounds),
            user_turn=user_index >= 0,
        )


@dataclass
class ReviewCounters:
    """The two counters of one session (see the module notes)."""

    memory_interval: int = DEFAULT_INTERVAL
    skills_interval: int = DEFAULT_INTERVAL
    memory_turns: int = 0
    skill_steps: int = 0
    hydrated: bool = False

    def hydrate(self, prior_user_turns: int) -> None:
        """Start a resumed session's memory counter at (prior turns mod interval), once."""
        if self.hydrated:
            return
        self.hydrated = True
        if prior_user_turns > 0 and self.memory_interval > 0 and self.memory_turns == 0:
            self.memory_turns = prior_user_turns % self.memory_interval

    def reset(self, kind: Kind) -> None:
        """The agent itself just saved to ``kind`` (a successful foreground write)."""
        if kind == "memory":
            self.memory_turns = 0
        else:
            self.skill_steps = 0

    def observe(self, facts: TurnFacts) -> frozenset[Kind]:
        """Count one finished turn; the kinds that are due now (their counters go back to 0)."""
        self.hydrate(facts.prior_user_turns)
        due: set[Kind] = set()
        if self.memory_interval > 0 and facts.memory_available and facts.user_turn:
            if facts.called_memory:
                self.memory_turns = 0
            else:
                self.memory_turns += 1
                if self.memory_turns >= self.memory_interval:
                    self.memory_turns = 0
                    due.add("memory")
        if self.skills_interval > 0 and facts.skills_available:
            for names in facts.rounds:
                self.skill_steps = 0 if SKILL_TOOL in names else self.skill_steps + 1
            if self.skill_steps >= self.skills_interval:
                self.skill_steps = 0
                due.add("skills")
        return frozenset(due)


# -- the trigger ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewRequest:
    """One review to run: what it covers and the conversation it reads (deep copies)."""

    kinds: frozenset[Kind]
    rows: tuple[TranscriptRow, ...]
    session_id: str
    focus: str | None = None
    attended: bool = False
    reason: Literal["auto", "refine"] = "auto"


def snapshot(rows: Iterable[TranscriptRow]) -> tuple[TranscriptRow, ...]:
    """A structural deep copy of the durable rows; the review never touches the live list."""
    return tuple(copy.deepcopy(r) for r in rows if not r.scaffold)


def _qualifies(state: TurnState) -> str | None:
    """Why this turn must not start a review, or ``None`` when it may."""
    if state.control.interrupted or state.exit in INTERRUPTED:
        return "interrupted"
    if state.failure is not None:
        return "failed"
    if not (state.final_text or "").strip():
        return "no final answer"
    return None


@dataclass
class ReviewTrigger:
    """One session's counters and trigger; ``after_turn`` is its post-turn hook."""

    scope: Scope | None
    start: Callable[[ReviewRequest], object]
    no_review: bool = False
    memory_ready: Callable[[], bool] = lambda: True
    counters: ReviewCounters = field(default_factory=ReviewCounters)

    def __post_init__(self) -> None:
        self.counters.memory_interval = interval_from(self.scope, "memory.review_interval")
        self.counters.skills_interval = interval_from(self.scope, "skills.review_interval")

    def note_write(self, kind: Kind) -> None:
        """A successful foreground write the host heard about (``MemoryTool.on_success``,
        ``manage_tool.SUCCESS_LISTENERS``): that counter starts again."""
        self.counters.reset(kind)

    def skipped(self, state: TurnState) -> bool:
        """A session that never reviews (and so never counts)."""
        return self.no_review or state.runtime.run_context in NO_REVIEW_CONTEXTS

    def after_turn(self, state: TurnState) -> None:
        if self.skipped(state):
            return
        try:
            ready = bool(self.memory_ready())
        except Exception:
            log.debug("memory readiness check failed; counting as not ready", exc_info=True)
            ready = False
        facts = TurnFacts.from_rows(
            state.rows, state.user_index, state.runtime.tool_names(), memory_ready=ready
        )
        due = self.counters.observe(facts)
        if not due:
            return
        why = _qualifies(state)
        if why is None and not review_enabled(self.scope):
            why = "learning.review.enabled is off"
        if why is not None:
            log.debug(
                "review of session %s due (%s) but not started: %s",
                state.session_id,
                ",".join(sorted(due)),
                why,
            )
            return
        request = ReviewRequest(kinds=due, rows=snapshot(state.rows), session_id=state.session_id)
        self.start(request)
