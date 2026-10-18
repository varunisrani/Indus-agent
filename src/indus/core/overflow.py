"""Overflow recovery: what to do when the provider proves a request does not fit (P08-06, R01.8).

The P03 classifier names the reason; ``plan_overflow`` decides, ``recover_overflow`` carries the
decision out on a ``TurnState`` with the context engine:

- **payload too large (413)**: compress and retry; progress is scored in serialised bytes (the
  token estimate prices images flat, so only bytes show what a 413 needs);
- **output cap too large** (the input fits, input + output does not): clamp the next request's
  output cap to the stated budget (never above ``window − estimated input``, minus a 64-token
  margin), compress too, retry; an output-cap error that states no number fails at once —
  compression cannot fix it;
- **prompt too long**: adopt the window the error states when it is lower than ours (and remember
  it as provider-confirmed); for a model whose window was only the fallback guess, with no stated
  limit, step down the probe tiers 256K → 128K → 64K → 32K → 16K → 8K; then compress and retry;
  without progress and without a smaller window the turn ends.

Every recovery compression is one bounded attempt that bypasses the summary cooldown and the
ineffective breaker without clearing them, and counts against the per-turn attempt budget
(``compression.max_attempts``). Out of attempts or out of progress → the turn ends
``compression_exhausted`` with a typed failure (reason, retryable flag) and a plain reply.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from indus.core import loop_texts
from indus.core.compression.anchor import estimate_messages, estimate_text
from indus.core.compression.engine import CompressRequest, compress_now, durable
from indus.core.exit_reasons import ExitReason, Failure
from indus.providers.context_length import parse_overflow_limit

if TYPE_CHECKING:
    from indus.core.turn_state import TurnState
    from indus.providers.classify import ClassifiedError

log = logging.getLogger(__name__)

__all__ = [
    "OUTPUT_MARGIN",
    "PROBE_TIERS",
    "OverflowPlan",
    "is_output_cap_message",
    "is_overflow",
    "next_tier",
    "parse_output_budget",
    "plan_overflow",
    "recover_overflow",
    "serialized_bytes",
]

PROBE_TIERS = (256_000, 128_000, 64_000, 32_000, 16_000, 8_000)
OUTPUT_MARGIN = 64
SHRINK = 0.95
FALLBACK_SOURCE = "fallback"

Kind = Literal["payload_too_large", "output_cap", "prompt_too_long"]

TEXT_TOO_LARGE = (
    "The request is too large for {model} even after condensing the conversation. Nothing was "
    "dropped; start a new session (/new) or remove large attachments, then try again."
)
TEXT_TOO_LONG = (
    "The conversation no longer fits {model}'s context window and could not be condensed any "
    "further. Nothing was dropped; start a new session (/new) to continue."
)
TEXT_OUTPUT_CAP = (
    "The provider refused the requested reply length for {model} and did not say what it allows. "
    "This is an output limit, not a full conversation: lower the model's output cap for this "
    "endpoint and retry."
)


# -- parsing ------------------------------------------------------------------------------------

_OUTPUT_WORDS = (
    "max_tokens",
    "max_output_tokens",
    "max_completion_tokens",
    "output token",
    "in the completion",
    "for the completion",
)
_INPUT_WORDS = (
    "prompt is too long",
    "prompt too long",
    "input is too long",
    "input token",
    "prompt contains",
    "too many input",
)
_LIMIT_WORDS = (
    "too large",
    "exceed",
    "limited to",
    "at most",
    "must be",
    "should be",
    "less than or equal",
    "range of",
    "maximum allowed",
)
_BUDGET_PATTERNS = tuple(
    re.compile(p)
    for p in (
        # Anthropic: "max_tokens: 100000 > 64000, which is the maximum allowed ..."
        r"max_tokens\s*:\s*\d+\s*>\s*(\d+)",
        # "available_tokens: 10000", "available tokens = 10000"
        r"available[_ ]tokens\s*[:=]?\s*(\d+)",
        # "This model supports at most 32768 completion tokens"
        r"supports at most\s+(\d+)\s+(?:completion\s+|output\s+)?tokens",
        # "max_completion_tokens is limited to 16384"
        r"(?:max_tokens|max_completion_tokens|max_output_tokens)\s+is\s+limited\s+to\s+(\d+)",
        # "Range of max_tokens should be [1, 65536]"
        r"range of max_tokens should be\s*\[\s*\d+\s*,\s*(\d+)\s*\]",
    )
)
_CONTEXT_IS = re.compile(r"maximum context length is\s+(\d+)")
_SPLIT = re.compile(
    r"\((\d+)\s+(?:tokens\s+)?in the messages[,;]\s*(\d+)\s+(?:tokens\s+)?in the completion\)"
)


def parse_output_budget(message: str) -> int | None:
    """The output tokens an "output cap too large" error says are available, or None."""
    lowered = message.lower()
    if not any(w in lowered for w in _OUTPUT_WORDS):
        return None
    for pattern in _BUDGET_PATTERNS:
        found = pattern.search(lowered)
        if found is not None and int(found.group(1)) >= 1:
            return int(found.group(1))
    window = _CONTEXT_IS.search(lowered)
    split = _SPLIT.search(lowered)
    if window is not None and split is not None:
        available = int(window.group(1)) - int(split.group(1))
        if available >= 1:
            return available  # the prompt fits; the completion request does not
    return None


def is_output_cap_message(message: str) -> bool:
    """Does the error complain about the output cap (and not about an oversized input)?"""
    lowered = message.lower()
    return (
        any(w in lowered for w in _OUTPUT_WORDS)
        and any(w in lowered for w in _LIMIT_WORDS)
        and not any(w in lowered for w in _INPUT_WORDS)
    )


def is_overflow(error: ClassifiedError) -> bool:
    """A provider-proven overflow: the classifier says so, or the message states an output budget
    (some vendors word "output cap too large" as a plain validation error)."""
    return error.should_compress or parse_output_budget(error.message or "") is not None


def next_tier(window: int) -> int | None:
    return next((tier for tier in PROBE_TIERS if tier < window), None)


def serialized_bytes(messages: Sequence[Mapping[str, Any]]) -> int:
    try:
        return len(json.dumps(list(messages), ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return sum(len(repr(m)) for m in messages)


# -- the plan -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class OverflowPlan:
    action: Literal["compress", "fail", "not_overflow"]
    kind: Kind | None = None
    output_cap: int | None = None
    new_window: int | None = None
    window_confirmed: bool = False
    failure: Failure | None = None
    final_text: str | None = None


def _fail(kind: Kind, reason: str, model: str, *, retryable: bool = False) -> OverflowPlan:
    text = {
        "payload_too_large": TEXT_TOO_LARGE,
        "output_cap": TEXT_OUTPUT_CAP,
        "prompt_too_long": TEXT_TOO_LONG,
    }[kind]
    return OverflowPlan(
        "fail",
        kind=kind,
        failure=Failure(reason, retryable, detail=kind),
        final_text=text.format(model=model or "this model"),
    )


def plan_overflow(
    error: ClassifiedError,
    *,
    window: int,
    window_known: bool,
    input_tokens: int,
    attempts: int,
    max_attempts: int,
    model: str = "",
) -> OverflowPlan:
    """What to do about ``error`` (see the module notes)."""
    out_of_attempts = attempts >= max_attempts
    if error.reason == "payload_too_large":
        if out_of_attempts:
            return _fail("payload_too_large", "payload_too_large", model)
        return OverflowPlan("compress", kind="payload_too_large")
    message = error.message or ""
    budget = parse_output_budget(message)
    if error.reason != "context_overflow" and budget is None:
        return OverflowPlan("not_overflow")
    if budget is not None:
        if out_of_attempts:
            return _fail("output_cap", "context_overflow", model)
        room = window - input_tokens
        cap = min(budget, room) - OUTPUT_MARGIN if room > 0 else budget - OUTPUT_MARGIN
        return OverflowPlan("compress", kind="output_cap", output_cap=max(1, cap))
    if is_output_cap_message(message):
        return _fail("output_cap", "output_cap", model)
    stated = parse_overflow_limit(message)
    new_window: int | None = None
    confirmed = False
    if stated is not None and stated < window:
        new_window, confirmed = stated, True
    elif stated is None and not window_known:
        new_window = next_tier(window)
    if out_of_attempts:
        return _fail("prompt_too_long", "context_overflow", model)
    return OverflowPlan(
        "compress", kind="prompt_too_long", new_window=new_window, window_confirmed=confirmed
    )


# -- carrying it out ----------------------------------------------------------------------------


def _request_tokens(state: TurnState) -> int:
    rows = [r.to_message() for r in state.rows]
    tools = json.dumps(list(state.runtime.tools), default=str) if state.runtime.tools else ""
    return estimate_messages(rows) + estimate_text(state.system_prompt) + estimate_text(tools)


def _remember_window(state: TurnState, tokens: int) -> None:
    runtime = state.runtime
    scope = runtime.scope
    route = runtime.route
    if scope is None or route is None:
        return
    try:
        from indus.providers import context_length

        context_length.remember(
            scope, str(getattr(route, "base_url", "")), str(getattr(route, "model", "")), tokens
        )
    except Exception:
        log.debug("could not remember the provider-stated window", exc_info=True)


def _end(state: TurnState, plan: OverflowPlan) -> None:
    state.end(
        ExitReason.COMPRESSION_EXHAUSTED,
        final_text=plan.final_text or loop_texts.PROVIDER_FAILED_REPLY.format(detail="overflow"),
        failure=plan.failure,
    )


async def recover_overflow(state: TurnState, error: ClassifiedError) -> bool | None:
    """``None``: not an overflow, or no engine to recover with (the caller ends the turn as
    before); ``True``: retry the request; ``False``: the turn ended here."""
    engine = state.runtime.context
    if engine is None or state.grace or not is_overflow(error):
        return None
    model = state.runtime.caller.model
    before_rows = durable(state)
    before_messages = [r.to_message() for r in before_rows]
    plan = plan_overflow(
        error,
        window=engine.window,
        window_known=engine.window_source != FALLBACK_SOURCE,
        input_tokens=_request_tokens(state),
        attempts=engine.attempts,
        max_attempts=engine.settings.max_attempts,
        model=model,
    )
    if plan.action == "not_overflow":
        return None
    if plan.action == "fail":
        log.warning("overflow recovery gave up (%s)", plan.kind)
        _end(state, plan)
        return False
    if plan.new_window is not None and plan.new_window < engine.window:
        log.warning(
            "context window of %s set to %d tokens (%s)",
            model,
            plan.new_window,
            "stated by the provider" if plan.window_confirmed else "probing down",
        )
        engine.set_route(
            window=plan.new_window,
            max_output=engine.max_output,
            model=engine.model,
            provider=engine.provider,
            source="provider-error" if plan.window_confirmed else "probe",
        )
        state.runtime.options = replace(state.runtime.options, context_length=plan.new_window)
        if plan.window_confirmed:
            _remember_window(state, plan.new_window)
    if plan.output_cap is not None:
        state.output_cap = plan.output_cap
    outcome = await compress_now(state, CompressRequest(gate="overflow"))
    if state.ended:
        return False
    after_messages = [r.to_message() for r in durable(state)]
    compressed = outcome.status == "compressed"
    if plan.kind == "payload_too_large":
        before, after = serialized_bytes(before_messages), serialized_bytes(after_messages)
        progress = compressed and 0 < after < before * SHRINK
    else:
        before, after = estimate_messages(before_messages), estimate_messages(after_messages)
        progress = compressed and (
            len(after_messages) < len(before_messages) or 0 < after < before * SHRINK
        )
    if plan.kind == "output_cap" or progress or plan.new_window is not None:
        return True
    kind = plan.kind or "prompt_too_long"
    reason = "payload_too_large" if kind == "payload_too_large" else "context_overflow"
    _end(state, _fail(kind, reason, model))
    return False
