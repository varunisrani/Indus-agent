"""Response checks: length and empty ladders, intent-ack, scrubber, repetition (P04-17)."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.core.response_checks import (
    COPY,
    EMPTY_SENTINEL,
    MAX_CONTINUATIONS,
    NUDGES,
    EmptyContext,
    LengthLimits,
    ThinkScrubber,
    TurnChecks,
    Verdict,
    backoff_seconds,
    boosted_cap,
    check_empty,
    check_intent_ack,
    check_length,
    finish_continuation,
    is_repetition_dominated,
    join_fragments,
    looks_like_intent_ack,
    scrubber_for,
)
from indus.foundation import yamlio
from indus.providers.streaming import CallResult, stream_call
from indus.providers.timeouts import TimeoutPolicy
from indus.providers.transports import get_transport
from indus.providers.types import ChatRequest, NormalizedResponse, ToolCall, Usage
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, fake_route, load_script

ROOT = Path(__file__).resolve().parents[3]
REQ = ChatRequest(model="fake-model-1", messages=[{"role": "user", "content": "hi"}])


def play(name: str, calls: int, api_mode: str = "chat_completions") -> list[CallResult]:
    """Run ``calls`` model calls of fixture script ``name`` through the P03 stream driver."""
    clock = FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    server = FakeModelServer(load_script(name), clock=clock)
    route = fake_route(server, api_mode)
    transport = get_transport(api_mode)

    async def drive(client: Any) -> Any:
        task = asyncio.ensure_future(
            stream_call(
                client,
                transport,
                route,
                REQ,
                policy=TimeoutPolicy(),
                clock=clock,
                on_event=lambda _event: None,
                route_epoch=lambda: 0,
            )
        )
        for _ in range(5_000):
            await clock.run_until_idle()
            if task.done():
                return task.result()
            clock.advance(1.0)
        raise AssertionError("call never finished")

    async def go() -> list[CallResult]:
        out: list[CallResult] = []
        async with server.client() as client:
            for _ in range(calls):
                result = await drive(client)
                assert isinstance(result, CallResult)
                out.append(result)
        return out

    return asyncio.run(go())


def text_reply(
    text: str | None,
    *,
    reasoning: str | None = None,
    usage: Usage | None = None,
    calls: tuple[ToolCall, ...] = (),
    finish: Any = "length",
) -> NormalizedResponse:
    return NormalizedResponse(
        content=text, tool_calls=calls, finish_reason=finish, reasoning=reasoning, usage=usage
    )


def never_runs_a_cut_call(verdict: Verdict) -> None:
    for row in verdict.append:
        assert not row.tool_calls, "a truncated tool call must never be appended for execution"


# -- length ladder against the fake model --------------------------------------------------------


def test_text_cut_off_is_continued_and_stitched() -> None:
    first, second, *_ = play("truncation", 2)
    state = TurnChecks()
    verdict = check_length(first, state, LengthLimits(context_window=128_000))
    assert verdict.action == "continue" and verdict.rung == "continue_text"
    fragment, nudge = verdict.append
    assert fragment.role == "assistant" and fragment.scaffold
    assert fragment.content == "The first half of the answer stops here"
    assert nudge.role == "user" and nudge.scaffold and nudge.content == NUDGES["continue"]
    assert not verdict.reasoning_off
    assert second.response.finish_reason == "stop"
    whole = finish_continuation(state, second.response.content)
    assert whole == "The first half of the answer stops here and the second half ends it."
    assert state.fragments == []


def test_cut_off_tool_call_is_requested_again_with_a_bigger_cap() -> None:
    results = play("truncation", 4)
    cut, whole = results[2], results[3]
    assert cut.response.finish_reason == "length" and cut.response.tool_calls
    state = TurnChecks()
    verdict = check_length(cut, state, LengthLimits(requested_cap=4096))
    assert verdict.action == "retry" and verdict.rung == "tool_retry"
    assert verdict.output_cap == 8192 and verdict.append == ()
    never_runs_a_cut_call(verdict)
    assert whole.response.finish_reason == "tool_calls"


def test_cut_off_tool_call_is_refused_after_four_retries() -> None:
    cut = text_reply(None, calls=(ToolCall("c1", "write_file", '{"path": "a'),))
    state = TurnChecks()
    caps: list[int | None] = []
    for _ in range(4):
        verdict = check_length(cut, state, LengthLimits(requested_cap=4096))
        assert verdict.action == "retry"
        caps.append(verdict.output_cap)
        never_runs_a_cut_call(verdict)
    assert caps == [8192, 16384, 32768, 32768]
    final = check_length(cut, state, LengthLimits(requested_cap=4096))
    assert final.action == "end" and final.rung == "tool_refused"
    assert final.final_text == COPY["tool_call_cut"] and final.failure_reason == "truncated"
    never_runs_a_cut_call(final)


def test_mid_stream_overflow_stub_ends_the_turn() -> None:
    (overflow,) = play("mid_stream_overflow", 1)
    assert overflow.stub and overflow.stub_terminal
    state = TurnChecks()
    verdict = check_length(overflow, state)
    assert verdict.action == "end" and verdict.rung == "overflow"
    assert verdict.failure_reason == "context_overflow" and not verdict.retryable
    assert verdict.append == () and verdict.final_text == COPY["overflow"]
    assert state.continuations == 0


def test_broken_stream_with_dropped_tool_is_continued_not_repeated() -> None:
    _, broken, follow = play("mid_stream_overflow", 3)
    assert broken.stub and not broken.stub_terminal and broken.dropped_tools == ("write_file",)
    state = TurnChecks()
    verdict = check_length(broken, state)
    assert verdict.action == "continue" and verdict.rung == "continue_stub"
    fragment, nudge = verdict.append
    assert fragment.scaffold and nudge.scaffold
    assert nudge.content == NUDGES["dropped_tools"].format(names="write_file")
    assert verdict.notice == COPY["dropped_tools"].format(names="write_file")
    never_runs_a_cut_call(verdict)
    whole = finish_continuation(state, follow.response.content)
    assert whole == "Writing the report now, in three parts."


def test_stub_nudge_names_at_most_three_tools() -> None:
    stub = CallResult(
        response=text_reply("partial"),
        stub=True,
        dropped_tools=("a", "b", "c"),
        dropped_tool_count=5,
    )
    verdict = check_length(stub, TurnChecks())
    assert verdict.append[-1].content == NUDGES["dropped_tools"].format(names="a, b, c")


def test_stub_without_tools_gets_the_stream_nudge() -> None:
    verdict = check_length(CallResult(response=text_reply("partial"), stub=True), TurnChecks())
    assert verdict.append[-1].content == NUDGES["stream_broke"]


# -- length ladder tables ------------------------------------------------------------------------


def test_prompt_filling_the_window_stops_at_once() -> None:
    reply = text_reply("Some text", usage=Usage(prompt_tokens=7_900, completion_tokens=200))
    verdict = check_length(reply, TurnChecks(), LengthLimits(context_window=8_192))
    assert verdict.action == "end" and verdict.rung == "window_full"
    notice = COPY["window_full"].format(prompt="7,900", window="8,192")
    assert verdict.final_text == f"Some text\n\n{notice}"
    assert verdict.drop_scaffold
    (row,) = verdict.append
    assert row.content == "Some text" and not row.scaffold


def test_headroom_of_512_or_more_keeps_continuing() -> None:
    reply = text_reply("t", usage=Usage(prompt_tokens=8_192 - 512, completion_tokens=10))
    verdict = check_length(reply, TurnChecks(), LengthLimits(context_window=8_192))
    assert verdict.action == "continue"


def test_unknown_window_never_stops_early() -> None:
    reply = text_reply("t", usage=Usage(prompt_tokens=10**9))
    assert check_length(reply, TurnChecks(), LengthLimits()).action == "continue"


def test_ceiling_folds_fragments_into_one_partial_answer() -> None:
    state = TurnChecks()
    verdicts = [check_length(text_reply(part), state) for part in "abcde"]
    assert [v.action for v in verdicts] == ["continue"] * MAX_CONTINUATIONS + ["end"]
    final = verdicts[-1]
    assert final.rung == "ceiling" and final.final_text == "abcde" and final.drop_scaffold
    (row,) = final.append
    assert row.content == "abcde" and not row.scaffold and row.finish_reason == "length"
    assert state.fragments == []


def test_ceiling_without_any_text_explains() -> None:
    state = TurnChecks()
    verdicts = [check_length(text_reply(None), state) for _ in range(5)]
    assert all(v.reasoning_off for v in verdicts[:4])
    assert all(len(v.append) == 1 and v.append[0].role == "user" for v in verdicts[:4])
    assert verdicts[-1].final_text == COPY["ceiling_no_text"] and verdicts[-1].append == ()


def test_repetition_dominated_text_aborts() -> None:
    loop = "I will now check the file again to be sure.\n" * 40
    verdict = check_length(text_reply(loop), TurnChecks())
    assert verdict.action == "end" and verdict.rung == "repetition"
    assert verdict.final_text == COPY["repetition"] and verdict.append == ()


def test_repetition_in_a_stub_aborts_too() -> None:
    stub = CallResult(response=text_reply("same words over " * 60), stub=True)
    assert check_length(stub, TurnChecks()).rung == "repetition"


def test_reasoning_that_ate_the_budget_retries_without_reasoning_then_aborts() -> None:
    state = TurnChecks()
    thinking = text_reply(None, reasoning="long thoughts")
    first = check_length(thinking, state)
    assert first.action == "continue" and first.reasoning_off
    assert [r.role for r in first.append] == ["user"]  # nothing visible to keep, only the nudge
    second = check_length(thinking, state)
    assert second.action == "end" and second.rung == "thinking_exhausted"
    assert second.final_text == COPY["thinking_exhausted"] and second.drop_scaffold


def test_terminal_empty_row_keeps_the_reasoning_out_of_the_answer() -> None:
    state = TurnChecks(prefills=2)
    ctx = EmptyContext(rng=random.Random(0))
    reply = text_reply(None, reasoning="r", finish="stop")
    verdicts = [check_empty(reply, state, ctx) for _ in range(4)]
    final = verdicts[-1]
    assert final.rung == "empty_terminal" and final.final_text == COPY["empty"]
    (row,) = final.append
    assert row.content == EMPTY_SENTINEL and row.reasoning == "r"


@pytest.mark.parametrize(
    ("n", "base", "requested", "limit", "want"),
    [
        (1, None, None, None, 8_192),
        (1, None, 4_096, None, 8_192),
        (2, None, 4_096, None, 16_384),
        (3, None, 4_096, None, 32_768),
        (4, None, 4_096, None, 32_768),
        (1, None, 50_000, None, 100_000),
        (1, None, 50_000, 64_000, 64_000),
        (2, None, 64_000, 64_000, 64_000),
        (1, 1_024, 2_048, None, 2_048),
        (2, 1_024, 2_048, None, 4_096),
        (6, 1_024, None, None, 32_768),
        (1, 1_000, 30_000, None, 30_000),
    ],
)
def test_boosted_cap_arithmetic(
    n: int, base: int | None, requested: int | None, limit: int | None, want: int
) -> None:
    assert boosted_cap(n, base=base, requested=requested, model_limit=limit) == want


def test_fragments_join_directly_and_trim_overlap_only_after_a_stub() -> None:
    assert join_fragments([("abc", False), ("def", False)]) == "abcdef"
    repeated = [("the quick brown fox", False), ("brown fox jumps", False)]
    assert join_fragments(repeated) == "the quick brown foxbrown fox jumps"
    after_stub = [("the quick brown fox", True), ("brown fox jumps", False)]
    assert join_fragments(after_stub) == "the quick brown fox jumps"
    short = [("ends with ab", True), ("ab and more", False)]
    assert join_fragments(short) == "ends with abab and more"


# -- empty ladder --------------------------------------------------------------------------------


def test_empty_ladder_against_the_fake_model() -> None:
    results = play("empty_ladder", 5)
    state = TurnChecks()
    ctx = EmptyContext(after_tool_round=True, model="m", provider="p", rng=random.Random(1))

    first = check_empty(results[0], state, ctx)
    assert first.action == "continue" and first.rung == "post_tool_nudge"
    sentinel, nudge = first.append
    assert sentinel.role == "assistant" and sentinel.content == EMPTY_SENTINEL and sentinel.scaffold
    assert nudge.content == NUDGES["empty_after_tools"] and nudge.scaffold

    second = check_empty(results[1], state, ctx)
    assert second.rung == "prefill"
    (prefill,) = second.append
    assert prefill.reasoning == "weighing the options" and prefill.scaffold
    assert prefill.content is None

    third = check_empty(results[2], state, ctx)
    assert third.action == "retry" and third.rung == "empty_retry"
    assert 5.0 <= third.wait <= 7.5

    fourth = check_empty(results[3], state, ctx)  # second identical proven-empty answer
    assert fourth.action == "end" and fourth.rung == "empty_terminal"
    (row,) = fourth.append
    assert row.content == EMPTY_SENTINEL and not row.scaffold
    assert fourth.final_text == COPY["empty"] and fourth.failure_reason == "empty_response"
    assert fourth.drop_scaffold
    assert results[4].response.content == "Here is the answer."


def test_shown_partial_stream_text_is_the_answer() -> None:
    verdict = check_empty(
        text_reply(None, finish="stop"),
        TurnChecks(),
        EmptyContext(streamed_text="  half an answer "),
    )
    assert verdict.action == "accept" and verdict.final_text == "half an answer"


def test_post_tool_nudge_happens_once() -> None:
    state = TurnChecks()
    ctx = EmptyContext(after_tool_round=True, rng=random.Random(0))
    empty = text_reply(None, finish="stop")
    assert check_empty(empty, state, ctx).rung == "post_tool_nudge"
    assert check_empty(empty, state, ctx).rung == "empty_retry"


def test_prefill_is_tried_twice() -> None:
    state = TurnChecks()
    thinking = text_reply(None, reasoning="hmm", finish="stop")
    ctx = EmptyContext(rng=random.Random(0))
    rungs = [check_empty(thinking, state, ctx).rung for _ in range(3)]
    assert rungs == ["prefill", "prefill", "empty_retry"]


def test_empty_retries_back_off_and_run_out() -> None:
    state = TurnChecks()
    ctx = EmptyContext(rng=random.Random(3))
    # Some output tokens were billed, so the streak is not provably deterministic.
    empty = text_reply("", usage=Usage(prompt_tokens=100, completion_tokens=3), finish="stop")
    verdicts = [check_empty(empty, state, ctx) for _ in range(4)]
    assert [v.action for v in verdicts] == ["retry", "retry", "retry", "end"]
    waits = [v.wait for v in verdicts[:3]]
    assert 5 <= waits[0] <= 7.5 and 10 <= waits[1] <= 15 and 20 <= waits[2] <= 30


def test_expensive_attempts_get_one_retry() -> None:
    state = TurnChecks()
    ctx = EmptyContext(attempt_cost_usd=0.30, rng=random.Random(0))
    empty = text_reply("", usage=Usage(prompt_tokens=100, completion_tokens=2), finish="stop")
    assert [check_empty(empty, state, ctx).action for _ in range(2)] == ["retry", "end"]
    cheap = TurnChecks()
    cheap_ctx = EmptyContext(attempt_cost_usd=0.10, rng=random.Random(0))
    assert [check_empty(empty, cheap, cheap_ctx).action for _ in range(4)] == [
        "retry",
        "retry",
        "retry",
        "end",
    ]


def test_different_finish_reasons_are_not_deterministic() -> None:
    state = TurnChecks()
    ctx = EmptyContext(rng=random.Random(0))
    a = text_reply(None, usage=Usage(prompt_tokens=10), finish="stop")
    b = text_reply(None, usage=Usage(prompt_tokens=10), finish="content_filter")
    assert [check_empty(r, state, ctx).action for r in (a, b, a)] == ["retry", "retry", "retry"]


def test_empty_without_usage_or_output_twice_is_deterministic() -> None:
    state = TurnChecks()
    ctx = EmptyContext(rng=random.Random(0))
    empty = text_reply(None, finish="stop")
    assert [check_empty(empty, state, ctx).action for _ in range(2)] == ["retry", "end"]


def test_backoff_numbers() -> None:
    rng = random.Random(9)
    for attempt, low in [(1, 5.0), (2, 10.0), (3, 20.0), (4, 40.0), (5, 60.0), (40, 60.0)]:
        wait = backoff_seconds(attempt, rng)
        assert low <= wait <= low * 1.5


# -- intent acknowledgement ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("I'll check the config file now.", True),
        ("Okay, let me look at the logs.", True),
        ("Found two problems. Next, I will fix the parser.", True),
        ("I am going to run the tests.", True),
        ("Done. The file has 3 lines.", False),
        ("Should I check the file?", False),
        ("I'll check it:\n```\ncode\n```", False),
        ("Let me know if you need anything else", False),
        ("I'll " + "x" * 700, False),
        ("", False),
        (None, False),
    ],
)
def test_intent_ack_shapes(text: str | None, want: bool) -> None:
    assert looks_like_intent_ack(text) is want


def test_intent_ack_continues_at_most_twice() -> None:
    state = TurnChecks()
    text = "I'll open the file next."
    first = check_intent_ack(text, state, tools_available=True)
    assert first is not None and first.action == "continue"
    reply, nudge = first.append
    assert reply.role == "assistant" and not reply.scaffold and reply.content == text
    assert nudge.scaffold and nudge.content == NUDGES["intent_ack"]
    assert check_intent_ack(text, state, tools_available=True) is not None
    assert check_intent_ack(text, state, tools_available=True) is None


def test_intent_ack_needs_tools_and_can_be_off() -> None:
    assert check_intent_ack("I'll do it.", TurnChecks(), tools_available=False) is None
    assert (
        check_intent_ack("I'll do it.", TurnChecks(), tools_available=True, enabled=False) is None
    )
    assert check_intent_ack("All done.", TurnChecks(), tools_available=True) is None


# -- think-tag scrubber --------------------------------------------------------------------------


def scrub(deltas: list[str], scrubber: ThinkScrubber | None = None) -> tuple[str, str]:
    s = scrubber or ThinkScrubber()
    seen, hidden = [], []
    for delta in deltas:
        v, r = s.feed(delta)
        seen.append(v)
        hidden.append(r)
    v, r = s.flush()
    return "".join(seen) + v, "".join(hidden) + r


def test_tags_split_across_three_deltas() -> None:
    assert scrub(["<thi", "nk>x</th", "ink>y"]) == ("y", "x")


def test_unclosed_block_is_flushed_as_reasoning() -> None:
    s = ThinkScrubber()
    assert s.feed("<think>still thinking") == ("", "still thinking")
    assert s.feed(" and more</thi") == ("", " and more")
    assert s.flush() == ("", "</thi")


def test_char_by_char_closed_pair() -> None:
    assert scrub(list("<think>abc</think>Hello")) == ("Hello", "abc")


def test_prose_mentioning_a_tag_mid_line_is_kept() -> None:
    text = "Wrap it in a <think> tag when you write the prompt."
    assert scrub([text]) == (text, "")


def test_inline_closed_pair_is_scrubbed_anywhere() -> None:
    assert scrub(["a <think>b</think> c"]) == ("a  c", "b")


def test_open_tag_after_newline_starts_a_block() -> None:
    assert scrub(["Intro\n", "  <thinking>plan", "</thinking>Answer"]) == (
        "Intro\n  Answer",
        "plan",
    )


def test_orphan_close_tag_is_removed() -> None:
    assert scrub(["</think>  answer"]) == ("answer", "")


def test_tags_match_in_any_case() -> None:
    assert scrub(["<THINK>x</Think>y"]) == ("y", "x")


def test_partial_tag_that_never_completes_is_text() -> None:
    assert scrub(["a <thi"]) == ("a <thi", "")
    assert scrub(["x < y and y > z"]) == ("x < y and y > z", "")


def test_text_without_tags_passes_through_unchanged() -> None:
    deltas = ["Hello, ", "world. ", "1 < 2"]
    assert scrub(deltas) == ("".join(deltas), "")


def test_flush_resets_for_the_next_stream() -> None:
    s = ThinkScrubber()
    s.feed("tail without newline")
    s.flush()
    assert s.feed("<think>r</think>v") == ("v", "r")


def test_profile_tags_extend_the_defaults() -> None:
    class Profile:
        think_tags = ("scratch",)

    s = scrubber_for(Profile())
    assert scrub(["<scratch>x</scratch>y"], s) == ("y", "x")
    assert scrub(["<think>x</think>y"], s) == ("y", "x")
    assert scrub(["<scratch>x</scratch>y"], scrubber_for(None)) == ("<scratch>x</scratch>y", "")


# -- repetition ----------------------------------------------------------------------------------


def _prose(seed: int, words: int) -> str:
    rng = random.Random(seed)
    vocab = [
        "alpha",
        "river",
        "stone",
        "quiet",
        "lamp",
        "seven",
        "orange",
        "window",
        "garden",
        "north",
        "paper",
        "silver",
        "cloud",
        "metal",
        "bridge",
        "candle",
        "forest",
    ]
    return " ".join(rng.choice(vocab) for _ in range(words))


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("All work and no play makes a dull day.\n" * 30, True),
        ("the loop keeps going " * 50, True),
        (_prose(1, 50) * 6, True),  # a long unit repeated: periodic, not line-based
        (_prose(2, 400), False),
        ("x" * 300, False),  # too short to judge
        (
            _prose(3, 300) + ("sixty characters of the same thing, repeated a few times!!" * 5),
            False,
        ),
        ("\n".join(f"INSERT INTO t VALUES ({i}, 'row number {i}');" for i in range(60)), False),
        (None, False),
        (42, False),
    ],
)
def test_repetition_table(text: Any, want: bool) -> None:
    assert is_repetition_dominated(text) is want


# -- copy ----------------------------------------------------------------------------------------


def test_copy_matches_the_english_catalogue() -> None:
    catalogue = yamlio.load_safe(ROOT / "locales" / "en.yaml")
    assert catalogue["turn"] == COPY
