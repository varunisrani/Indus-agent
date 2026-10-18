"""Response checks: what the turn loop does with a cut-off, empty or announce-only answer.

Nothing here calls the model or the store. Each check returns a ``Verdict`` the loop (P04-15)
applies: accept the answer, continue with rows appended, retry the same call (maybe with a larger
output cap, maybe after a wait) or end the turn with a text for the user. Rows the checks add as
private nudges are ``scaffold`` (never kept in the durable snapshot, R01.3). ``TurnChecks`` holds
the per-turn counters.

Length ladder (order matters; ``check_length``):

0. a P03 partial-stream stub marked terminal (context overflow after text was shown) ends the turn
   and is never continued — continuing would only send a bigger request (R01.6);
1. visible text dominated by a repetition loop ends the turn with a clear message; so does an
   answer whose whole budget went to reasoning a second time, after one continuation was asked
   for with reasoning off;
2. a text cut-off (also a P03 stub: the stream broke after text was shown) appends the fragment and
   a continuation nudge, both scaffold, up to ``MAX_CONTINUATIONS`` times. When usage shows the
   prompt left under ``MIN_HEADROOM`` tokens of the window, or at the ceiling, the fragments fold
   into one partial answer and the turn ends. A stub's next fragment drops what it repeats of the
   delivered text, so text is continued, not repeated;
3. a cut-off tool call is asked for again, up to ``MAX_TOOL_RETRIES`` times, with a growing output
   cap (``boosted_cap``); after that the turn ends. A truncated call is never executed.

Empty ladder (``check_empty``): text already shown by a broken stream is the answer → one nudge
after a tool round → a thinking-only answer is fed back as a prefill (twice) → empty retries with
jittered backoff (budget 3, or 1 when one attempt costs over USD 0.25; two identical proven-empty
attempts stop early) → an ``(empty)`` sentinel row and a plain explanation (R01.17).

``check_intent_ack``: a short answer that only announces its next step, with tools available, gets
one nudge to act, at most ``MAX_INTENT_ACKS`` per turn.

Also here (A18): ``ThinkScrubber``, a state machine over text deltas that moves inline reasoning
tags out of visible text even when a tag is split across deltas, and ``is_repetition_dominated``,
the runaway-loop test used by rung 1.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from indus.core.transcript import TranscriptRow
from indus.providers.streaming import CallResult
from indus.providers.types import NormalizedResponse, Usage

__all__ = [
    "COPY",
    "DEFAULT_THINK_TAGS",
    "EMPTY_SENTINEL",
    "MAX_CONTINUATIONS",
    "MAX_INTENT_ACKS",
    "MAX_TOOL_RETRIES",
    "MIN_HEADROOM",
    "NUDGES",
    "EmptyContext",
    "LengthLimits",
    "ThinkScrubber",
    "TurnChecks",
    "Verdict",
    "backoff_seconds",
    "boosted_cap",
    "check_empty",
    "check_intent_ack",
    "check_length",
    "finish_continuation",
    "is_repetition_dominated",
    "join_fragments",
    "looks_like_intent_ack",
    "scrubber_for",
]

MAX_CONTINUATIONS = 4
MAX_TOOL_RETRIES = 4
MIN_HEADROOM = 512
CAP_FLOOR = 32_768
DEFAULT_BASE_CAP = 4096
MAX_PREFILLS = 2
EMPTY_RETRY_BUDGET = 3
REDUCED_EMPTY_BUDGET = 1
COST_THRESHOLD_USD = 0.25
BACKOFF_BASE = 5.0
BACKOFF_MAX = 60.0
BACKOFF_JITTER = 0.5
MAX_INTENT_ACKS = 2
MAX_NAMED_DROPPED = 3
MIN_STUB_OVERLAP = 8
EMPTY_SENTINEL = "(empty)"
DISPLAY_PREFILL = "prefill"

# User-facing copy; mirrored by ``turn.*`` in locales/en.yaml (a test keeps them equal).
COPY: dict[str, str] = {
    "overflow": (
        "The conversation no longer fits this model's context window, so the partial reply was "
        "not continued. Compress the conversation or start a new session."
    ),
    "repetition": (
        "The model got stuck repeating itself, so the reply was stopped. Send the message again, "
        "or switch to another model."
    ),
    "thinking_exhausted": (
        "The model spent its whole output budget on reasoning and wrote no answer. Lower the "
        "reasoning effort or raise the output limit."
    ),
    "ceiling_no_text": (
        "The reply hit the output limit on every attempt and no visible text came back. Lower the "
        "reasoning effort or raise the output limit."
    ),
    "window_full": (
        "The prompt used {prompt} of the model's {window}-token context window, leaving no room "
        "for an answer. Compress the conversation or start a new session."
    ),
    "tool_call_cut": (
        "The model's tool call was cut off several times in a row, so it was not run."
    ),
    "dropped_tools": (
        "The stream broke while a tool call was being written ({names}); it was not run."
    ),
    "empty": (
        "The model returned no answer after several attempts. Send the message again, or switch "
        "to another model."
    ),
    "heal_notice": (
        "Some earlier turns in this session are empty and get patched on every request. Replies "
        "still work; start a new session to clear them."
    ),
}

# Model-facing nudges (sent as scaffold user rows, never stored).
NUDGES: dict[str, str] = {
    "continue": (
        "[Your reply was cut off by the output limit. Continue exactly where it stopped, without "
        "repeating anything already written.]"
    ),
    "stream_broke": (
        "[The connection dropped while your reply was streaming. The text above was delivered; "
        "continue from where it stopped without repeating it.]"
    ),
    "dropped_tools": (
        "[The connection dropped while you were writing a call to {names}; that call was not run. "
        "Your tools are still available: split large content over several smaller calls and try "
        "again.]"
    ),
    "empty_after_tools": (
        "[You sent an empty reply after the tool results above. Read them and carry on with the "
        "task.]"
    ),
    "intent_ack": (
        "[You said what you would do next but did not do it. Make the tool call now, or give your "
        "final answer.]"
    ),
}

type Action = Literal["accept", "continue", "retry", "end"]


@dataclass(frozen=True)
class Verdict:
    """What the loop does next.

    ``append``: rows to add to the live history (in order). ``drop_scaffold``: remove this turn's
    scaffold rows first (the fold at the end of a continuation trail). ``output_cap``: max output
    tokens for the retry. ``wait``: seconds to wait before it. ``reasoning_off``: send the next
    request without reasoning. ``final_text``: the answer or explanation shown to the user.
    ``notice``: a status line for the UI (never sent to the model).
    """

    action: Action
    rung: str
    append: tuple[TranscriptRow, ...] = ()
    drop_scaffold: bool = False
    final_text: str | None = None
    failure_reason: str | None = None
    retryable: bool = True
    output_cap: int | None = None
    wait: float = 0.0
    reasoning_off: bool = False
    notice: str | None = None


@dataclass(frozen=True)
class _Attempt:
    """One empty answer in the current streak."""

    signature: tuple[str, str, str]
    usage_present: bool
    zero_output: bool
    generated: bool


@dataclass
class TurnChecks:
    """Per-turn counters for the ladders (a new turn starts a new object)."""

    continuations: int = 0
    fragments: list[tuple[str, bool]] = field(default_factory=list[tuple[str, bool]])
    tool_retries: int = 0
    post_tool_nudged: bool = False
    prefills: int = 0
    empty_retries: int = 0
    empty_streak: list[_Attempt] = field(default_factory=list[_Attempt])
    intent_acks: int = 0
    reasoning_off_tried: bool = False


@dataclass(frozen=True)
class LengthLimits:
    """Numbers the length ladder needs; ``None`` means unknown."""

    context_window: int | None = None
    requested_cap: int | None = None
    base_cap: int | None = None
    model_output_limit: int | None = None


@dataclass(frozen=True)
class EmptyContext:
    """What the empty ladder needs to know about the call that came back empty."""

    streamed_text: str = ""
    after_tool_round: bool = False
    attempt_cost_usd: float | None = None
    model: str = ""
    provider: str = ""
    rng: random.Random | None = None


@dataclass(frozen=True)
class _Reply:
    response: NormalizedResponse
    stub: bool = False
    terminal: bool = False
    dropped: tuple[str, ...] = ()


def _reply(result: CallResult | NormalizedResponse) -> _Reply:
    if isinstance(result, CallResult):
        return _Reply(result.response, result.stub, result.stub_terminal, result.dropped_tools)
    return _Reply(result)


def _has_reasoning(response: NormalizedResponse) -> bool:
    return bool((response.reasoning and response.reasoning.strip()) or response.reasoning_details)


def _nudge(key: str, **values: str) -> TranscriptRow:
    return TranscriptRow(role="user", content=NUDGES[key].format(**values), scaffold=True)


# -- arithmetic ----------------------------------------------------------------------------------


def boosted_cap(
    n: int,
    *,
    base: int | None = None,
    requested: int | None = None,
    model_limit: int | None = None,
) -> int:
    """Output cap for tool-call retry ``n`` (1-based).

    ``base·2ⁿ`` (``base`` defaults to the cap that failed, else 4096), never below the failed cap,
    at most ``max(32768, 2·cap)``, never above the model's known limit. A failed cap already at
    the limit is returned unchanged: doubling cannot help.
    """
    start = base or requested or DEFAULT_BASE_CAP
    anchor = requested or start
    if model_limit and anchor >= model_limit:
        return anchor
    grown = max(start * (2**n), requested or 0)
    capped = min(grown, max(CAP_FLOOR, 2 * anchor))
    return min(capped, model_limit) if model_limit else capped


def _window_filled(usage: Usage | None, window: int | None) -> tuple[int, int] | None:
    if usage is None or not window or usage.prompt_tokens <= 0:
        return None
    if window - usage.prompt_tokens < MIN_HEADROOM:
        return usage.prompt_tokens, window
    return None


def backoff_seconds(attempt: int, rng: random.Random) -> float:
    """Wait before empty retry ``attempt`` (1-based): min(5·2^(n−1), 60) plus up to 50 %."""
    exponent = max(0, attempt - 1)
    delay = BACKOFF_MAX if exponent >= 16 else min(BACKOFF_BASE * (2**exponent), BACKOFF_MAX)
    return delay + rng.uniform(0.0, BACKOFF_JITTER * delay)


# -- fragments -----------------------------------------------------------------------------------


def _overlap(before: str, after: str) -> int:
    """Length of the longest head of ``after`` that ``before`` ends with (at least 8 chars)."""
    limit = min(len(before), len(after), 4096)
    for size in range(limit, MIN_STUB_OVERLAP - 1, -1):
        if before.endswith(after[:size]):
            return size
    return 0


def join_fragments(parts: Sequence[tuple[str, bool]]) -> str:
    """Stitch continuation fragments ``(text, came_from_stub)`` into one text.

    After a stub fragment (the stream broke after showing it) the next fragment's head that repeats
    the delivered tail is cut. Fragments are joined directly: a cut-off continues mid-word.
    """
    out = ""
    after_stub = False
    for text, stub in parts:
        if after_stub and out and text:
            text = text[_overlap(out, text) :]
        out += text
        after_stub = stub
    return out


def finish_continuation(state: TurnChecks, final_text: str | None) -> str:
    """The whole answer once a continued reply completes; clears the fragment trail."""
    parts = [*state.fragments, (final_text or "", False)]
    state.fragments = []
    return join_fragments(parts)


# -- repetition ----------------------------------------------------------------------------------

MIN_REPETITION_TEXT = 400
_MIN_REPEATS = 5
_DOMINANT = 0.5
_WINDOW = 48
_TAIL = 65_536


def _line_dominated(text: str) -> bool:
    counts = Counter(line.strip() for line in text.splitlines() if line.strip())
    size = len(text)
    return any(
        c >= _MIN_REPEATS and c * len(line) >= size * _DOMINANT for line, c in counts.items()
    )


def _run_span(text: str, at: int, period: int) -> int:
    left = at
    while left > 0 and text[left - 1] == text[left - 1 + period]:
        left -= 1
    right = at
    while right + period < len(text) and text[right] == text[right + period]:
        right += 1
    return right + period - left


def _periodic_dominated(text: str) -> bool:
    size = len(text)
    windows = Counter(text[i : i + _WINDOW] for i in range(size - _WINDOW + 1))
    if not windows:
        return False
    window, count = windows.most_common(1)[0]
    if count < _MIN_REPEATS:
        return False
    spots: list[int] = []
    at = text.find(window)
    while at >= 0:
        spots.append(at)
        at = text.find(window, at + 1)
    gaps = Counter(b - a for a, b in zip(spots, spots[1:], strict=False))
    if not gaps:
        return False
    period = gaps.most_common(1)[0][0]
    for probe in (spots[len(spots) // 2], spots[0], spots[-1]):
        if probe + period > size:
            continue
        span = _run_span(text, probe, period)
        if span >= _MIN_REPEATS * period and span >= size * _DOMINANT:
            return True
    return False


def is_repetition_dominated(text: object) -> bool:
    """True when one exact repeat run (≥5 repeats) covers at least half of a long text.

    Short text (under 400 chars) is never judged; very long text is judged on its last 64 KiB.
    """
    if not isinstance(text, str) or len(text) < MIN_REPETITION_TEXT:
        return False
    tail = text[-_TAIL:]
    return _line_dominated(tail) or _periodic_dominated(tail)


# -- the length ladder ---------------------------------------------------------------------------


def _fold(state: TurnChecks, rung: str, final: str, reason: str) -> Verdict:
    partial = join_fragments(state.fragments).strip()
    state.fragments = []
    rows = (TranscriptRow(role="assistant", content=partial, finish_reason="length"),)
    return Verdict(
        action="end",
        rung=rung,
        append=rows if partial else (),
        drop_scaffold=True,
        final_text=final,
        failure_reason=reason,
        retryable=True,
    )


def check_length(
    result: CallResult | NormalizedResponse,
    state: TurnChecks,
    limits: LengthLimits | None = None,
) -> Verdict:
    """The verdict for a ``finish_reason == "length"`` answer (see the module notes)."""
    limits = limits or LengthLimits()
    reply = _reply(result)
    response = reply.response
    if reply.terminal:
        return Verdict(
            action="end",
            rung="overflow",
            drop_scaffold=True,
            final_text=COPY["overflow"],
            failure_reason="context_overflow",
            retryable=False,
        )
    text = response.content or ""
    has_calls = bool(response.tool_calls)
    if not has_calls and is_repetition_dominated(text):
        return Verdict(
            action="end",
            rung="repetition",
            drop_scaffold=True,
            final_text=COPY["repetition"],
            failure_reason="repetition",
        )
    thinking_only = not has_calls and not reply.stub and not text.strip()
    if thinking_only and _has_reasoning(response) and state.reasoning_off_tried:
        return Verdict(
            action="end",
            rung="thinking_exhausted",
            drop_scaffold=True,
            final_text=COPY["thinking_exhausted"],
            failure_reason="thinking_exhausted",
        )
    if has_calls and not reply.stub:
        state.tool_retries += 1
        n = state.tool_retries
        if n <= MAX_TOOL_RETRIES:
            cap = boosted_cap(
                n,
                base=limits.base_cap,
                requested=limits.requested_cap,
                model_limit=limits.model_output_limit,
            )
            return Verdict(action="retry", rung="tool_retry", output_cap=cap)
        return Verdict(
            action="end",
            rung="tool_refused",
            drop_scaffold=True,
            final_text=COPY["tool_call_cut"],
            failure_reason="truncated",
        )
    return _continue_text(reply, state, limits)


def _continue_text(reply: _Reply, state: TurnChecks, limits: LengthLimits) -> Verdict:
    response = reply.response
    text = response.content or ""
    state.continuations += 1
    rows: list[TranscriptRow] = []
    if text:
        state.fragments.append((text, reply.stub))
        rows.append(
            TranscriptRow(role="assistant", content=text, scaffold=True, finish_reason="length")
        )
    filled = _window_filled(response.usage, limits.context_window)
    if filled is not None:
        prompt, window = filled
        notice = COPY["window_full"].format(prompt=f"{prompt:,}", window=f"{window:,}")
        partial = join_fragments(state.fragments).strip()
        final = f"{partial}\n\n{notice}" if partial else notice
        return _fold(state, "window_full", final, "truncated")
    if state.continuations > MAX_CONTINUATIONS:
        partial = join_fragments(state.fragments).strip()
        return _fold(state, "ceiling", partial or COPY["ceiling_no_text"], "truncated")
    names = ", ".join(reply.dropped[:MAX_NAMED_DROPPED])
    if reply.stub and names:
        rows.append(_nudge("dropped_tools", names=names))
        notice: str | None = COPY["dropped_tools"].format(names=names)
    elif reply.stub:
        rows.append(_nudge("stream_broke"))
        notice = None
    else:
        rows.append(_nudge("continue"))
        notice = None
    reasoning_off = not text and not reply.stub
    state.reasoning_off_tried = state.reasoning_off_tried or reasoning_off
    return Verdict(
        action="continue",
        rung="continue_stub" if reply.stub else "continue_text",
        append=tuple(rows),
        reasoning_off=reasoning_off,
        notice=notice,
    )


# -- the empty ladder ----------------------------------------------------------------------------


def _record_empty(state: TurnChecks, response: NormalizedResponse, ctx: EmptyContext) -> None:
    usage = response.usage
    present = usage is not None and usage.prompt_tokens > 0
    zero = present and usage is not None and usage.completion_tokens + usage.reasoning_tokens == 0
    state.empty_streak.append(
        _Attempt(
            signature=(ctx.model, ctx.provider, response.finish_reason),
            usage_present=present,
            zero_output=zero,
            generated=_has_reasoning(response) or bool((response.content or "").strip()),
        )
    )


def _deterministic_empty(state: TurnChecks) -> bool:
    """Two or more identical empties that prove nothing was generated (fails open)."""
    streak = state.empty_streak
    if len(streak) < 2:
        return False
    if any(a.signature != streak[0].signature for a in streak):
        return False
    by_usage = all(a.usage_present and a.zero_output for a in streak)
    by_body = all(not a.usage_present and not a.generated for a in streak)
    return by_usage or by_body


def check_empty(
    result: CallResult | NormalizedResponse,
    state: TurnChecks,
    ctx: EmptyContext | None = None,
) -> Verdict:
    """The verdict for an answer with no visible text and no tool calls (see the module notes)."""
    ctx = ctx or EmptyContext()
    response = _reply(result).response
    shown = ctx.streamed_text.strip()
    if shown:
        state.empty_streak.clear()
        return Verdict(action="accept", rung="partial_stream", final_text=shown)
    if ctx.after_tool_round and not state.post_tool_nudged:
        state.post_tool_nudged = True
        rows = (
            TranscriptRow(role="assistant", content=EMPTY_SENTINEL, scaffold=True),
            _nudge("empty_after_tools"),
        )
        return Verdict(action="continue", rung="post_tool_nudge", append=rows)
    if _has_reasoning(response) and state.prefills < MAX_PREFILLS:
        state.prefills += 1
        row = TranscriptRow(
            role="assistant",
            reasoning=response.reasoning,
            reasoning_details=[dict(d) for d in response.reasoning_details] or None,
            scaffold=True,
            display_kind=DISPLAY_PREFILL,
            finish_reason="incomplete",
        )
        return Verdict(action="continue", rung="prefill", append=(row,))
    _record_empty(state, response, ctx)
    cost = ctx.attempt_cost_usd
    budget = (
        REDUCED_EMPTY_BUDGET
        if cost is not None and cost >= COST_THRESHOLD_USD
        else EMPTY_RETRY_BUDGET
    )
    if state.empty_retries < budget and not _deterministic_empty(state):
        state.empty_retries += 1
        wait = backoff_seconds(state.empty_retries, ctx.rng or random.Random())
        return Verdict(action="retry", rung="empty_retry", wait=wait)
    sentinel = TranscriptRow(
        role="assistant",
        content=EMPTY_SENTINEL,
        reasoning=response.reasoning,
        reasoning_details=[dict(d) for d in response.reasoning_details] or None,
        finish_reason=response.finish_reason,
    )
    return Verdict(
        action="end",
        rung="empty_terminal",
        append=(sentinel,),
        drop_scaffold=True,
        final_text=COPY["empty"],
        failure_reason="empty_response",
    )


# -- intent acknowledgement ----------------------------------------------------------------------

_ACK_LIMIT = 600
_SENTENCE_END = re.compile(r"(?<=[.!?:])\s+|\n+")
_ANNOUNCES = re.compile(
    r"^(?:(?:ok(?:ay)?|alright|sure|great|got it)[,.!]?\s+)?"
    r"(?:(?:now|next|first|then),?\s+)?"
    r"(?:i['’]ll|i\s+will|let\s+me(?!\s+know\b)|i['’]m\s+going\s+to|i\s+am\s+going\s+to"
    r"|i['’]m\s+about\s+to)\b",
    re.IGNORECASE,
)


def looks_like_intent_ack(text: str | None) -> bool:
    """A short reply whose last sentence announces an action instead of reporting a result."""
    if not text:
        return False
    body = text.strip()
    if not body or len(body) > _ACK_LIMIT or "```" in body or body.endswith("?"):
        return False
    sentences = [s.strip() for s in _SENTENCE_END.split(body) if s.strip()]
    return bool(sentences) and bool(_ANNOUNCES.match(sentences[-1]))


def check_intent_ack(
    text: str | None,
    state: TurnChecks,
    *,
    tools_available: bool,
    enabled: bool = True,
) -> Verdict | None:
    """A continuation nudge for an announce-only reply, or ``None`` to accept the reply."""
    if not enabled or not tools_available or state.intent_acks >= MAX_INTENT_ACKS:
        return None
    if not looks_like_intent_ack(text):
        return None
    state.intent_acks += 1
    rows = (TranscriptRow(role="assistant", content=text), _nudge("intent_ack"))
    return Verdict(action="continue", rung="intent_ack", append=rows)


# -- think-tag scrubber --------------------------------------------------------------------------

DEFAULT_THINK_TAGS: tuple[str, ...] = ("think", "thinking", "reasoning", "thought")


class ThinkScrubber:
    """Moves inline reasoning blocks out of streamed text.

    ``feed(delta)`` returns ``(visible, reasoning)`` for one delta. A tag split across deltas is
    held back until it resolves. An opening tag starts a block only at a line boundary (start of
    the stream, or nothing but whitespace before it on its line), so prose that mentions a tag is
    left alone unless the closing tag follows in the same buffer. A closing tag with no block open
    is dropped. ``flush()`` ends the stream: an unclosed block comes out as reasoning, a held
    partial tag as text. Tag names match in any letter case.
    """

    def __init__(self, tags: Iterable[str] = DEFAULT_THINK_TAGS) -> None:
        names = list(dict.fromkeys(t.strip().lower() for t in tags if t and t.strip()))
        self._opens = tuple(f"<{name}>" for name in names)
        self._closes = tuple(f"</{name}>" for name in names)
        self._all = self._opens + self._closes
        self._held: str = ""
        self._inside: bool = False
        self._line_blank: bool = True

    def reset(self) -> None:
        self._held = ""
        self._inside = False
        self._line_blank = True

    # helpers

    @staticmethod
    def _first(buf: str, tags: tuple[str, ...]) -> tuple[int, int]:
        lowered = buf.lower()
        best = (-1, 0)
        for tag in tags:
            at = lowered.find(tag)
            if at >= 0 and (best[0] < 0 or at < best[0]):
                best = (at, len(tag))
        return best

    @staticmethod
    def _partial_tail(buf: str, tags: tuple[str, ...]) -> int:
        lowered = buf.lower()
        longest = max((len(t) for t in tags), default=0)
        for size in range(min(len(lowered), longest - 1), 0, -1):
            tail = lowered[-size:]
            if any(len(t) > size and t.startswith(tail) for t in tags):
                return size
        return 0

    def _tag_at_start(self, buf: str) -> tuple[str, int]:
        """Kind and length of the tag ``buf`` starts with: open, close, partial or none."""
        lowered = buf.lower()
        for tag in self._opens:
            if lowered.startswith(tag):
                return "open", len(tag)
        for tag in self._closes:
            if lowered.startswith(tag):
                return "close", len(tag)
        if any(len(tag) > len(lowered) and tag.startswith(lowered) for tag in self._all):
            return "partial", 0
        return "none", 0

    def _emit(self, out: list[str], text: str) -> None:
        if not text:
            return
        out.append(text)
        cut = text.rfind("\n")
        if cut >= 0:
            self._line_blank = not text[cut + 1 :].strip()
        else:
            self._line_blank = self._line_blank and not text.strip()

    # public

    def feed(self, delta: str) -> tuple[str, str]:
        buf = self._held + delta
        self._held = ""
        visible: list[str] = []
        hidden: list[str] = []
        while buf:
            if self._inside:
                at, size = self._first(buf, self._closes)
                if at < 0:
                    keep = self._partial_tail(buf, self._closes)
                    hidden.append(buf[: len(buf) - keep])
                    self._held = buf[len(buf) - keep :]
                    break
                hidden.append(buf[:at])
                buf = buf[at + size :]
                self._inside = False
                continue
            lt = buf.find("<")
            if lt < 0:
                self._emit(visible, buf)
                break
            self._emit(visible, buf[:lt])
            buf = buf[lt:]
            kind, size = self._tag_at_start(buf)
            if kind == "partial":
                self._held = buf
                break
            if kind == "none":
                self._emit(visible, "<")
                buf = buf[1:]
            elif kind == "close":
                buf = buf[size:].lstrip()
            elif self._line_blank:
                self._inside = True
                buf = buf[size:]
            else:
                at, close_size = self._first(buf[size:], self._closes)
                if at >= 0:
                    hidden.append(buf[size : size + at])
                    buf = buf[size + at + close_size :]
                else:
                    self._emit(visible, buf[:size])
                    buf = buf[size:]
        return "".join(visible), "".join(hidden)

    def flush(self) -> tuple[str, str]:
        held, inside = self._held, self._inside
        self.reset()
        return ("", held) if inside else (held, "")


def scrubber_for(profile: object = None) -> ThinkScrubber:
    """A scrubber for the default tags plus any ``think_tags`` the provider profile lists."""
    extra: object = getattr(profile, "think_tags", None) or ()
    names: list[str] = []
    if isinstance(extra, (list, tuple)):
        names = [str(t) for t in cast(Sequence[object], extra)]
    return ThinkScrubber((*DEFAULT_THINK_TAGS, *names))
