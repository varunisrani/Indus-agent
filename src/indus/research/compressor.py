"""Squeezing one trajectory under a token budget (P22-12).

A trajectory is a list of ``{from, value}`` turns (``trajectory.convert``'s output). When its
tokens exceed ``compression.target_max_tokens``, the fewest middle turns that bring it under the
target are replaced by one ``human`` turn holding a summary; a tool call is never cut apart from
its response.

1. Count tokens per turn. At or under the target → unchanged (``skipped_under_target``).
2. Protected turns: the first turn of each role whose ``protected_turns.first_<role>`` flag is on,
   and the last ``protected_turns.last_n`` turns. The window that may be summarised starts after
   the highest protected index below ``n // 2`` and ends at the lowest protected index at or above
   it. The start moves forward off ``tool`` turns (a response whose call stays in the head cannot
   begin the region); a start at or past the end → unchanged, still over the limit.
3. Need = total − target + summary target (the summary costs tokens too). Turns are taken from the
   start until their tokens reach the need, else the whole window. The end then moves forward past
   ``tool`` turns (folding a response into the region that holds its call), backward only when
   nothing clean lies ahead inside the window.
4. A region that is empty, or holds no more tokens than the summary would, cannot shrink the
   trajectory → unchanged, still over the limit.
5. The region is rendered as labelled blocks (values over 3000 characters keep the first 1500 and
   the last 500) and summarised through the provider layer with our prompt and ``max_tokens = 2 ×
   summary target``. A reply cut by the length limit, or blank, is a failure (a truncated summary
   would quietly corrupt the sample); failures are retried ``max_retries`` times in all with
   jittered backoff (``retry_delay`` doubling, at most 30 s); after the last one a fixed fallback
   summary is used. The summary carries ``SUMMARY_MARKER`` exactly once, at the start.
6. Head (the system turn gets the notice text when enabled) + the summary turn + the untouched
   tail; recounted; ``still_over_limit`` when still above the target.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from indus.core.compression.summarize import SummaryReply
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.research.compress_config import (
    CompressionConfig,
    ProtectedTurns,
    SummarizationSettings,
    TokenCounter,
)

__all__ = [
    "FALLBACK_SUMMARY",
    "SUMMARY_MARKER",
    "Compressor",
    "Summariser",
    "SummaryReply",
    "TrajectoryMetrics",
    "aux_summariser",
    "backoff_delay",
    "elide",
    "render_region",
    "snap",
    "window",
]

TASK = "compression"
SUMMARY_MARKER = "[SUMMARY OF EARLIER TURNS]:"
FALLBACK_SUMMARY = (
    f"{SUMMARY_MARKER} Earlier tool calls and their results were condensed here to save "
    "context; a detailed summary of them could not be produced."
)
ELIDE_OVER = 3000
ELIDE_HEAD = 1500
ELIDE_TAIL = 500
BACKOFF_CAP_S = 30.0
ROLES = ("system", "human", "gpt", "tool")

PROMPT = """The turns below come from the middle of a conversation between a user and an AI \
assistant that uses tools. They will be removed from the conversation, and your text will \
stand in their place, so whoever reads the rest of the conversation must be able to rely on it.

Write a plain, factual account of these turns, from a neutral point of view:
- which tools the assistant called, and with which important arguments;
- what the calls returned: results, numbers, file names, paths, identifiers and errors;
- what the assistant concluded or decided along the way.

Do not add advice or anything that is not in the turns. Aim for about {target} tokens.
Start your answer with "{marker}" and write nothing before it.

=== TURNS ===
{turns}
=== END OF TURNS ==="""


class Summariser(Protocol):
    """One summary request; ``SummaryReply(text, finish_reason)``."""

    async def __call__(
        self, prompt: str, *, max_tokens: int, temperature: float
    ) -> SummaryReply: ...


@dataclass
class TrajectoryMetrics:
    """What happened to one trajectory. Token and turn counts are before → after; the region is
    ``[region_start, region_end)`` of the input (``-1`` when nothing was summarised)."""

    tokens_before: int = 0
    tokens_after: int = 0
    turns_before: int = 0
    turns_after: int = 0
    region_start: int = -1
    region_end: int = -1
    was_compressed: bool = False
    still_over_limit: bool = False
    skipped_under_target: bool = False
    summarisation_calls: int = 0
    summarisation_errors: int = 0

    @property
    def tokens_saved(self) -> int:
        return self.tokens_before - self.tokens_after

    @property
    def turns_removed(self) -> int:
        return self.turns_before - self.turns_after

    @property
    def region_turns(self) -> int:
        return max(0, self.region_end - self.region_start) if self.region_start >= 0 else 0

    @property
    def ratio(self) -> float:
        return self.tokens_after / max(self.tokens_before, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tokens": {
                "before": self.tokens_before,
                "after": self.tokens_after,
                "saved": self.tokens_saved,
                "ratio": round(self.ratio, 4),
            },
            "turns": {
                "before": self.turns_before,
                "after": self.turns_after,
                "removed": self.turns_removed,
            },
            "region": {
                "start": self.region_start,
                "end": self.region_end,
                "turns": self.region_turns,
            },
            "was_compressed": self.was_compressed,
            "still_over_limit": self.still_over_limit,
            "skipped_under_target": self.skipped_under_target,
            "summarisation": {
                "calls": self.summarisation_calls,
                "errors": self.summarisation_errors,
            },
        }


# -- boundaries (pure) -----------------------------------------------------------------------------


def _role(turn: Mapping[str, Any]) -> str:
    return str(turn.get("from", ""))


def _value(turn: Mapping[str, Any]) -> str:
    value = turn.get("value", "")
    return value if isinstance(value, str) else str(value)


def protected_indices(turns: Sequence[Mapping[str, Any]], flags: ProtectedTurns) -> set[int]:
    wanted = {
        "system": flags.first_system,
        "human": flags.first_human,
        "gpt": flags.first_gpt,
        "tool": flags.first_tool,
    }
    first: dict[str, int] = {}
    for index, turn in enumerate(turns):
        first.setdefault(_role(turn), index)
    out = {first[role] for role in ROLES if wanted[role] and role in first}
    n = len(turns)
    out.update(range(max(0, n - flags.last_n), n))
    return out


def window(n: int, protected: set[int]) -> tuple[int, int]:
    """``[start, end)``: after the protected head (indices below ``n // 2``), before the
    protected tail."""
    half = n // 2
    head = [i for i in protected if i < half]
    tail = [i for i in protected if i >= half]
    return (max(head) + 1 if head else 0), (min(tail) if tail else n)


def _clean(turns: Sequence[Mapping[str, Any]], index: int) -> bool:
    return index >= len(turns) or _role(turns[index]) != "tool"


def snap(turns: Sequence[Mapping[str, Any]], index: int, low: int, high: int) -> int:
    """The nearest boundary at or after ``index`` (up to ``high``) that does not sit on a
    ``tool`` turn; failing that, the nearest one before it (down to ``low``)."""
    ahead = index
    while ahead < high and not _clean(turns, ahead):
        ahead += 1
    if _clean(turns, ahead):
        return ahead
    back = index
    while back > low and not _clean(turns, back):
        back -= 1
    return back


# -- rendering -------------------------------------------------------------------------------------


def elide(
    text: str, *, over: int = ELIDE_OVER, head: int = ELIDE_HEAD, tail: int = ELIDE_TAIL
) -> str:
    if len(text) <= over:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n[... {dropped} characters left out ...]\n{text[-tail:]}"


def render_region(turns: Sequence[Mapping[str, Any]], start: int, end: int) -> str:
    blocks: list[str] = []
    for index in range(start, end):
        role = _role(turns[index]).upper() or "UNKNOWN"
        blocks.append(f"[turn {index} · {role}]\n{elide(_value(turns[index]))}")
    return "\n\n".join(blocks)


def with_marker(text: str) -> str:
    """``text`` with ``SUMMARY_MARKER`` exactly once, at the start."""
    body = text.strip()
    while body.startswith(SUMMARY_MARKER):
        body = body[len(SUMMARY_MARKER) :].lstrip()
    body = body.replace(SUMMARY_MARKER, "").strip()
    return f"{SUMMARY_MARKER} {body}" if body else SUMMARY_MARKER


def backoff_delay(attempt: int, base: float, rng: random.Random) -> float:
    """Wait before retry ``attempt`` (1-based): ``base · 2^(attempt−1)`` plus up to half again,
    never more than ``BACKOFF_CAP_S``."""
    raw = base * (2 ** max(0, attempt - 1))
    return min(BACKOFF_CAP_S, raw + rng.uniform(0.0, raw / 2))


class SummaryRefused(Exception):
    """The summariser answered, but not with a usable summary."""


# -- the compressor --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    counts: tuple[int, ...]
    total: int
    region: tuple[int, int] | None


class Compressor:
    """See the module notes. ``sleep`` and ``rng`` are injected so tests never wait."""

    def __init__(
        self,
        config: CompressionConfig,
        counter: TokenCounter,
        summarise: Summariser,
        *,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.counter = counter
        self.summarise = summarise
        self.sleep = sleep if sleep is not None else SystemClock().sleep
        self.rng = rng or random.Random()

    def counts(self, turns: Sequence[Mapping[str, Any]]) -> list[int]:
        return [self.counter.count(_value(turn)) for turn in turns]

    def plan(self, turns: Sequence[Mapping[str, Any]]) -> Plan:
        counts = self.counts(turns)
        total = sum(counts)
        settings = self.config.compression
        if total <= settings.target_max_tokens:
            return Plan(tuple(counts), total, None)
        protected = protected_indices(turns, self.config.protected_turns)
        start, end = window(len(turns), protected)
        start = snap(turns, start, start, end)
        if start >= end or not _clean(turns, start):
            return Plan(tuple(counts), total, None)
        need = total - settings.target_max_tokens + settings.summary_target_tokens
        taken = 0
        until = start
        for index in range(start, end):
            taken += counts[index]
            until = index + 1
            if taken >= need:
                break
        until = snap(turns, until, start, end)
        if until <= start or not _clean(turns, until):
            return Plan(tuple(counts), total, None)
        if sum(counts[start:until]) <= settings.summary_target_tokens:
            return Plan(tuple(counts), total, None)
        return Plan(tuple(counts), total, (start, until))

    async def summary(self, text: str, metrics: TrajectoryMetrics) -> str:
        settings = self.config.summarization
        target = self.config.compression.summary_target_tokens
        prompt = PROMPT.format(target=target, marker=SUMMARY_MARKER, turns=text)
        for attempt in range(1, settings.max_retries + 1):
            metrics.summarisation_calls += 1
            try:
                reply = await self.summarise(
                    prompt, max_tokens=2 * target, temperature=settings.temperature
                )
                if (reply.finish_reason or "").strip().lower() == "length":
                    raise SummaryRefused("the summary was cut off by the length limit")
                if not reply.text.strip():
                    raise SummaryRefused("the summary was empty")
                return with_marker(reply.text)
            except Exception:
                metrics.summarisation_errors += 1
                if attempt >= settings.max_retries:
                    break
                await self.sleep(backoff_delay(attempt, settings.retry_delay, self.rng))
        return FALLBACK_SUMMARY

    async def compress(
        self, turns: Sequence[Mapping[str, Any]]
    ) -> tuple[list[dict[str, Any]], TrajectoryMetrics]:
        metrics = TrajectoryMetrics()
        plan = self.plan(turns)
        metrics.turns_before = metrics.turns_after = len(turns)
        metrics.tokens_before = metrics.tokens_after = plan.total
        copied = [dict(turn) for turn in turns]
        if plan.total <= self.config.compression.target_max_tokens:
            metrics.skipped_under_target = True
            return copied, metrics
        metrics.still_over_limit = True
        if plan.region is None:
            return copied, metrics
        start, end = plan.region
        summary = await self.summary(render_region(turns, start, end), metrics)
        head = [dict(turn) for turn in turns[:start]]
        output = self.config.output
        if output.add_summary_notice:
            head = [
                {**turn, "value": _value(turn) + output.notice_text}
                if _role(turn) == "system"
                else turn
                for turn in head
            ]
        result = [*head, {"from": "human", "value": summary}, *(dict(t) for t in turns[end:])]
        metrics.region_start, metrics.region_end = start, end
        metrics.turns_after = len(result)
        metrics.tokens_after = sum(self.counts(result))
        metrics.was_compressed = True
        metrics.still_over_limit = metrics.tokens_after > self.config.compression.target_max_tokens
        return result, metrics


# -- the provider-layer summariser ----------------------------------------------------------------


def aux_summariser(
    scope: Scope, settings: SummarizationSettings, *, client: Any = None
) -> Summariser:
    """Summaries from the profile's auxiliary model (``auxiliary.compression``, else the main
    route), or from ``settings.model`` / ``settings.base_url`` when given, with the key named by
    ``settings.api_key_secret``. Build it inside the event loop that will use it."""
    from indus.foundation.errors import ProviderError
    from indus.providers import route as route_mod
    from indus.providers.auxiliary import AuxClient

    main: Callable[[], route_mod.RuntimeRoute] = lambda: route_mod.resolve(scope)  # noqa: E731
    aux = client if client is not None else AuxClient(scope, main)
    explicit: route_mod.RuntimeRoute | None = None

    def explicit_route() -> route_mod.RuntimeRoute:
        nonlocal explicit
        if explicit is None:
            picked = route_mod.resolve(scope, model=settings.model, base_url=settings.base_url)
            if settings.api_key_secret:
                found = route_mod.find_key(scope, (settings.api_key_secret,))
                if found is None:
                    raise ProviderError(
                        f"the secret {settings.api_key_secret} named by "
                        "summarization.api_key_secret is not set"
                    )
                picked = replace(picked, api_key=route_mod.Secret(found[1]), key_env=found[0])
            explicit = picked
        return explicit

    async def call(prompt: str, *, max_tokens: int, temperature: float) -> SummaryReply:
        messages = [{"role": "user", "content": prompt}]
        if settings.model or settings.base_url:
            result = await aux.call_route(
                TASK,
                explicit_route(),
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout_s=None,
            )
        else:
            result = await aux.call(TASK, messages, max_tokens=max_tokens, temperature=temperature)
        return SummaryReply(result.text, result.response.finish_reason)

    return call
