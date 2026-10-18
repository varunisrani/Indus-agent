"""Overflow recovery (P08-06, R01.8)."""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import pytest

from indus.core.compression.engine import BuiltinEngine, build_engine
from indus.core.compression.threshold import CompressionSettings
from indus.core.exit_reasons import ExitReason
from indus.core.overflow import (
    is_output_cap_message,
    next_tier,
    parse_output_budget,
    plan_overflow,
    serialized_bytes,
)
from indus.core.transcript import TranscriptRow
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.providers.classify import ClassifiedError
from tests.fakes.aux_model import FakeAuxModel
from tests.fakes.clock import FakeClock
from tests.fakes.model import Fail, Turn, text_turn
from tests.fakes.turn import TurnKit, check_alternation
from tests.unit.core.compression._transcripts import conversation

ANTHROPIC_CAP = (
    "max_tokens: 100000 > 64000, which is the maximum allowed number of output tokens "
    "for this model"
)
OPENAI_SPLIT = (
    "This model's maximum context length is 128000 tokens. However, you requested 140000 tokens "
    "(100000 in the messages, 40000 in the completion). Please reduce the length of the messages "
    "or completion."
)
OPENAI_INPUT = (
    "This model's maximum context length is 128000 tokens. However, you requested 134000 tokens "
    "(130000 in the messages, 4000 in the completion). Please reduce the length of the messages "
    "or completion."
)
TOO_LONG = "prompt is too long: 250000 tokens > 200000 maximum"


def overflow(message: str) -> ClassifiedError:
    return ClassifiedError(
        reason="context_overflow",
        status=400,
        message=message,
        retryable=False,
        should_compress=True,
    )


def plan(message: str, **kw: Any) -> Any:
    values: dict[str, Any] = {
        "window": 200_000,
        "window_known": True,
        "input_tokens": 150_000,
        "attempts": 0,
        "max_attempts": 3,
    }
    values.update(kw)
    return plan_overflow(overflow(message), **values)


# -- parsing and planning -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "budget"),
    [
        (ANTHROPIC_CAP, 64_000),
        ("input length and `max_tokens` exceed context limit; available_tokens: 10000", 10_000),
        (
            "max_tokens is too large: 65536. This model supports at most 32768 completion tokens",
            32_768,
        ),
        ("max_completion_tokens is limited to 16384 for this model", 16_384),
        ("Range of max_tokens should be [1, 8192]", 8_192),
        (OPENAI_SPLIT, 28_000),
        (OPENAI_INPUT, None),
        (TOO_LONG, None),
    ],
)
def test_output_budget_parsing(message: str, budget: int | None) -> None:
    assert parse_output_budget(message) == budget


def test_output_cap_clamp_arithmetic() -> None:
    near_full = plan(ANTHROPIC_CAP, input_tokens=150_000)
    assert near_full.action == "compress" and near_full.kind == "output_cap"
    assert near_full.output_cap == 50_000 - 64  # window − input beats the stated budget
    roomy = plan(ANTHROPIC_CAP, input_tokens=10_000)
    assert roomy.output_cap == 64_000 - 64
    over = plan(ANTHROPIC_CAP, input_tokens=250_000)
    assert over.output_cap == 64_000 - 64  # the estimate overshot: trust the provider's number
    tiny = plan("available_tokens: 10 max_tokens too large", input_tokens=10)
    assert tiny.output_cap == 1


def test_output_cap_without_a_number_fails_fast() -> None:
    message = "max_tokens is too large for this model; it must be lower"
    assert is_output_cap_message(message)
    verdict = plan(message)
    assert verdict.action == "fail" and verdict.failure.reason == "output_cap"
    assert not is_output_cap_message(TOO_LONG)


def test_prompt_too_long_adopts_a_stated_lower_limit() -> None:
    lower = plan(TOO_LONG, window=256_000)
    assert lower.action == "compress" and lower.kind == "prompt_too_long"
    assert lower.new_window == 200_000 and lower.window_confirmed
    same = plan(TOO_LONG, window=200_000)
    assert same.new_window is None
    unstated = plan("Input is too long for requested model.", window=200_000)
    assert unstated.new_window is None  # a known window is kept when nothing is stated


def test_tier_probe_sequence() -> None:
    window = 300_000
    seen = []
    while True:
        verdict = plan("Input is too long for requested model.", window=window, window_known=False)
        if verdict.new_window is None:
            break
        seen.append(verdict.new_window)
        window = verdict.new_window
    assert seen == [256_000, 128_000, 64_000, 32_000, 16_000, 8_000]
    assert next_tier(8_000) is None


def test_payload_too_large_plan_and_bytes() -> None:
    error = ClassifiedError(
        reason="payload_too_large", status=413, retryable=False, should_compress=True
    )
    assert (
        plan_overflow(
            error, window=1, window_known=True, input_tokens=0, attempts=0, max_attempts=3
        ).action
        == "compress"
    )
    assert serialized_bytes([{"role": "user", "content": "é"}]) == len(
        json.dumps([{"role": "user", "content": "é"}], ensure_ascii=False).encode()
    )


def test_out_of_attempts_is_terminal() -> None:
    for message in (TOO_LONG, ANTHROPIC_CAP):
        verdict = plan(message, attempts=3, max_attempts=3)
        assert verdict.action == "fail"
        assert verdict.failure.retryable is False and verdict.final_text


def test_not_an_overflow() -> None:
    error = ClassifiedError(reason="rate_limit", status=429)
    assert (
        plan_overflow(
            error, window=1, window_known=True, input_tokens=0, attempts=0, max_attempts=3
        ).action
        == "not_overflow"
    )


# -- in the loop ----------------------------------------------------------------------------------


def fail_with(message: str, status: int = 400) -> Turn:
    body = json.dumps({"error": {"message": message, "type": "invalid_request_error"}})
    return Turn((Fail(status, body),))


def kit_with_engine(
    scope: Scope, clock: FakeClock, turns: list[Turn], **settings: Any
) -> tuple[TurnKit, FakeAuxModel, list[BuiltinEngine], Any]:
    kit = TurnKit(scope, clock, turns)
    kit.step = 0.01
    aux = FakeAuxModel(clock=clock)
    engines: list[BuiltinEngine] = []

    def prepare(state: TurnState) -> None:
        if state.runtime.context is None:
            engine = build_engine(
                scope,
                session_id=state.session_id,
                clock=clock,
                store=state.runtime.store,
                summarizer=aux,
                window=256_000,
                window_source=settings.pop("source", "family"),
                model="test-model",
                settings=CompressionSettings(**settings),
            )
            assert engine is not None
            state.runtime.context = engine
            engines.append(engine)

    return kit, aux, engines, prepare


def history(exchanges: int = 20) -> list[TranscriptRow]:
    rows = conversation(random.Random(21), exchanges, big=20_000)
    return [r for r in rows if r.role != "system"]


def test_loop_recovers_from_prompt_too_long(scope: Scope, fake_clock: FakeClock) -> None:
    kit, aux, engines, prepare = kit_with_engine(
        scope, fake_clock, [fail_with(TOO_LONG), text_turn("done")]
    )
    result = asyncio.run(kit.turn("go on", history=history(), prepare=prepare))
    assert result.exit_reason is ExitReason.COMPLETED, result.final_text
    assert len(aux.prompts) == 1  # one bounded recovery compression
    assert engines[0].window == 200_000 and engines[0].trigger == 150_000  # stated limit adopted
    check_alternation(result.rows)
    assert len(kit.server.model_requests) == 2


def test_loop_output_cap_clamps_the_retry(scope: Scope, fake_clock: FakeClock) -> None:
    kit, _, _, prepare = kit_with_engine(
        scope, fake_clock, [fail_with(ANTHROPIC_CAP), text_turn("done")]
    )
    result = asyncio.run(kit.turn("go on", history=history(5), prepare=prepare))
    assert result.exit_reason is ExitReason.COMPLETED
    first, second = kit.server.model_requests
    assert (
        second.body.get("max_tokens") is not None
        or second.body.get("max_completion_tokens") is not None
    )
    cap = second.body.get("max_tokens") or second.body.get("max_completion_tokens")
    assert cap <= 64_000 - 64


def test_payload_too_large_without_progress_is_terminal(
    scope: Scope, fake_clock: FakeClock
) -> None:
    kit, _, _, prepare = kit_with_engine(
        scope, fake_clock, [fail_with("request entity too large", status=413), text_turn("never")]
    )
    result = asyncio.run(kit.turn("hi", prepare=prepare))  # nothing to compress in a fresh chat
    assert result.exit_reason is ExitReason.COMPRESSION_EXHAUSTED
    assert result.failure is not None
    assert result.failure.reason == "payload_too_large" and result.failure.retryable is False
    assert result.final_text and "Nothing was dropped" in result.final_text


def test_terminal_result_carries_reason_and_retryable(scope: Scope, fake_clock: FakeClock) -> None:
    turns = [fail_with(TOO_LONG) for _ in range(5)]
    kit, aux, _, prepare = kit_with_engine(scope, fake_clock, turns, max_attempts=1)
    result = asyncio.run(kit.turn("go on", history=history(), prepare=prepare))
    assert result.exit_reason is ExitReason.COMPRESSION_EXHAUSTED
    assert result.failure is not None and result.failure.reason == "context_overflow"
    assert result.failure.retryable is False
    assert len(aux.prompts) == 1  # the attempt cap bounds the recovery
    check_alternation(result.rows)


def test_bypass_is_one_bounded_attempt(scope: Scope, fake_clock: FakeClock) -> None:
    kit, aux, engines, prepare = kit_with_engine(
        scope, fake_clock, [fail_with(TOO_LONG), text_turn("ok")]
    )

    def armed(state: TurnState) -> None:
        prepare(state)
        engine = engines[0]
        engine.book.store.counters.cooldown_until = fake_clock.now().timestamp() + 900  # type: ignore[attr-defined]

    result = asyncio.run(kit.turn("go on", history=history(), prepare=armed))
    assert result.exit_reason is ExitReason.COMPLETED
    assert len(aux.prompts) == 1  # the cooldown did not stop the proven-overflow attempt
    remaining = asyncio.run(engines[0].book.remaining())
    assert remaining > 0  # and it was not cleared


def test_a_validation_error_stating_an_output_budget_is_an_overflow() -> None:
    error = ClassifiedError(
        reason="format_error", status=400, message=ANTHROPIC_CAP, retryable=False
    )
    verdict = plan_overflow(
        error, window=200_000, window_known=True, input_tokens=1_000, attempts=0, max_attempts=3
    )
    assert verdict.kind == "output_cap" and verdict.output_cap == 64_000 - 64
