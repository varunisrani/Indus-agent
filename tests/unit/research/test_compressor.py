"""P22-12: the compression algorithm (boundaries, need arithmetic, summary retries, assembly).

The property tests are seeded random generators (the suite has no Hypothesis dependency): many
generated trajectories, each checked against the invariants.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from indus.research.compress_config import CompressionConfig, TokenCounter
from indus.research.compressor import (
    BACKOFF_CAP_S,
    FALLBACK_SUMMARY,
    SUMMARY_MARKER,
    Compressor,
    SummaryReply,
    backoff_delay,
    elide,
    render_region,
    snap,
    window,
)
from tests.fakes.research.fake_tokenizer import FakeTokenizer, words


@dataclass
class ScriptedSummariser:
    replies: list[SummaryReply | Exception] = field(default_factory=list[SummaryReply | Exception])
    default: SummaryReply = field(default_factory=lambda: SummaryReply("short recap", "stop"))
    prompts: list[str] = field(default_factory=list[str])
    options: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    async def __call__(self, prompt: str, *, max_tokens: int, temperature: float) -> SummaryReply:
        self.prompts.append(prompt)
        self.options.append({"max_tokens": max_tokens, "temperature": temperature})
        step = self.replies.pop(0) if self.replies else self.default
        if isinstance(step, Exception):
            raise step
        return step


@dataclass
class Sleeps:
    waits: list[float] = field(default_factory=list[float])

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def config(**sections: dict[str, Any]) -> CompressionConfig:
    base: dict[str, Any] = {
        "compression": {"target_max_tokens": 100, "summary_target_tokens": 10},
        "summarization": {"max_retries": 3, "retry_delay": 2},
    }
    for key, value in sections.items():
        base.setdefault(key, {}).update(value)
    return CompressionConfig.model_validate(base)


def make(
    cfg: CompressionConfig,
    summariser: ScriptedSummariser | None = None,
    sleeps: Sleeps | None = None,
) -> Compressor:
    return Compressor(
        cfg,
        TokenCounter(FakeTokenizer()),
        summariser or ScriptedSummariser(),
        sleep=sleeps or Sleeps(),
        rng=random.Random(7),
    )


def t(role: str, n: int, tag: str = "w") -> dict[str, str]:
    return {"from": role, "value": words(n, tag)}


def run(compressor: Compressor, turns: Sequence[dict[str, str]]) -> Any:
    return asyncio.run(compressor.compress(turns))


# -- pieces ----------------------------------------------------------------------------------------


def test_window_after_head_before_tail() -> None:
    assert window(10, {0, 1, 2, 3, 6, 7, 8, 9}) == (4, 6)
    assert window(10, set()) == (0, 10)
    assert window(4, {0, 1, 2, 3}) == (2, 2)


def test_snap_prefers_forward_then_backward() -> None:
    roles = ["system", "human", "gpt", "tool", "tool", "gpt", "tool", "human"]
    turns = [{"from": r, "value": ""} for r in roles]
    assert snap(turns, 3, 2, 7) == 5  # forward off two tool turns
    assert snap(turns, 6, 2, 7) == 7
    assert snap(turns, 6, 2, 6) == 5  # nothing clean ahead inside the window → back
    assert snap(turns, 2, 2, 7) == 2


def test_elision_3000_1500_500() -> None:
    short = "a" * 3000
    assert elide(short) == short
    long = "h" * 1500 + "m" * 1500 + "t" * 500
    out = elide(long)
    assert out.startswith("h" * 1500 + "\n") and out.endswith("\n" + "t" * 500)
    assert "1500 characters left out" in out
    rendered = render_region([{"from": "tool", "value": long}], 0, 1)
    assert rendered.startswith("[turn 0 · TOOL]\n") and "m" * 1501 not in rendered


def test_backoff_is_jittered_doubling_and_capped() -> None:
    rng = random.Random(3)
    assert 2.0 <= backoff_delay(1, 2.0, rng) <= 3.0
    assert 4.0 <= backoff_delay(2, 2.0, rng) <= 6.0
    assert backoff_delay(9, 2.0, rng) == BACKOFF_CAP_S


# -- whole trajectories ----------------------------------------------------------------------------


def test_under_target_is_untouched_and_no_summary_is_asked() -> None:
    summariser = ScriptedSummariser()
    turns = [t("system", 10), t("human", 10), t("gpt", 10)]
    out, metrics = run(make(config(), summariser), turns)
    assert out == turns
    assert metrics.skipped_under_target and not metrics.was_compressed
    assert summariser.prompts == []


def test_need_arithmetic_takes_the_fewest_turns() -> None:
    # 0 sys 5, 1 human 5, 2 gpt 5, 3 tool 5 (protected head) | 4..9 gpt/human 30 each | tail 4
    turns = [t("system", 5), t("human", 5), t("gpt", 5), t("tool", 5)]
    turns += [t("gpt", 30), t("human", 30), t("gpt", 30), t("human", 30), t("gpt", 30)]
    turns += [t("human", 1), t("gpt", 1), t("human", 1), t("gpt", 1)]
    total = 20 + 150 + 4  # 174 → need = 174 - 100 + 10 = 84 → three 30-token turns
    summariser = ScriptedSummariser(default=SummaryReply("two words", "stop"))
    cfg = config(output={"add_summary_notice": False})
    out, metrics = run(make(cfg, summariser), turns)
    assert metrics.tokens_before == total
    assert (metrics.region_start, metrics.region_end, metrics.region_turns) == (4, 7, 3)
    summary = f"{SUMMARY_MARKER} two words"
    assert out[4] == {"from": "human", "value": summary}
    assert out[5:] == turns[7:]
    assert metrics.tokens_after == total - 90 + len(summary.split())
    assert metrics.tokens_saved == metrics.tokens_before - metrics.tokens_after
    assert metrics.turns_removed == 2 and metrics.was_compressed
    assert not metrics.still_over_limit
    assert summariser.options == [{"max_tokens": 20, "temperature": 0.3}]
    assert "[turn 4 · GPT]" in summariser.prompts[0] and "[turn 7" not in summariser.prompts[0]


def test_system_turn_gets_the_notice_and_protected_turns_stay_byte_identical() -> None:
    turns = [t("system", 5), t("human", 5), t("gpt", 5), t("tool", 5)]
    turns += [t("gpt", 60), t("tool", 60), t("gpt", 60)]
    turns += [t("human", 1), t("gpt", 1), t("human", 1), t("gpt", 1)]
    cfg = config()
    out, metrics = run(make(cfg), turns)
    assert metrics.was_compressed
    assert out[0]["value"] == turns[0]["value"] + cfg.output.notice_text
    assert out[1:4] == turns[1:4]
    assert out[-4:] == turns[-4:]
    off = config(output={"add_summary_notice": False})
    out, _ = run(make(off), turns)
    assert out[0] == turns[0]


def test_region_never_starts_on_a_tool_turn_whose_call_is_protected() -> None:
    # the first gpt (index 2) is protected and its tool response (3) is not the first tool turn
    turns = [t("system", 5), t("human", 5), t("tool", 5), t("gpt", 5), t("tool", 50)]
    turns += [t("gpt", 50), t("human", 50), t("gpt", 1), t("human", 1), t("gpt", 1), t("human", 1)]
    out, metrics = run(make(config(protected_turns={"first_tool": False})), turns)
    assert metrics.was_compressed
    assert turns[metrics.region_start]["from"] != "tool"


def test_length_finish_is_retried_then_the_fallback_is_used() -> None:
    summariser = ScriptedSummariser(
        replies=[
            SummaryReply("half a summ", "length"),
            RuntimeError("overloaded"),
            SummaryReply("   ", "stop"),
        ]
    )
    sleeps = Sleeps()
    turns = [t("system", 5), t("human", 5), t("gpt", 5), t("tool", 5)]
    turns += [t("gpt", 60), t("human", 60), t("gpt", 60)]
    turns += [t("human", 1), t("gpt", 1), t("human", 1), t("gpt", 1)]
    out, metrics = run(make(config(), summariser, sleeps), turns)
    assert metrics.summarisation_calls == 3 and metrics.summarisation_errors == 3
    assert len(sleeps.waits) == 2 and all(w <= BACKOFF_CAP_S for w in sleeps.waits)
    assert out[metrics.region_start]["value"] == FALLBACK_SUMMARY


def test_a_retry_that_succeeds_wins() -> None:
    summariser = ScriptedSummariser(
        replies=[SummaryReply("cut", "length"), SummaryReply(f"{SUMMARY_MARKER} fine", "stop")]
    )
    turns = [t("system", 5), t("human", 5), t("gpt", 5), t("tool", 5)]
    turns += [t("gpt", 60), t("human", 60)]
    turns += [t("human", 1), t("gpt", 1), t("human", 1), t("gpt", 1)]
    out, metrics = run(make(config(), summariser), turns)
    assert metrics.summarisation_calls == 2 and metrics.summarisation_errors == 1
    assert out[metrics.region_start]["value"] == f"{SUMMARY_MARKER} fine"


@pytest.mark.parametrize(
    "reply",
    [
        "plain text",
        f"{SUMMARY_MARKER} once",
        f"{SUMMARY_MARKER}{SUMMARY_MARKER} twice",
        f"lead {SUMMARY_MARKER} inside",
    ],
)
def test_summary_marker_appears_exactly_once(reply: str) -> None:
    summariser = ScriptedSummariser(default=SummaryReply(reply, "stop"))
    turns = [t("system", 5), t("human", 5), t("gpt", 5), t("tool", 5)]
    turns += [t("gpt", 60), t("human", 60)]
    turns += [t("human", 1), t("gpt", 1), t("human", 1), t("gpt", 1)]
    out, metrics = run(make(config(), summariser), turns)
    value = out[metrics.region_start]["value"]
    assert value.startswith(SUMMARY_MARKER) and value.count(SUMMARY_MARKER) == 1


def test_a_region_no_bigger_than_the_summary_is_left_alone() -> None:
    turns = [t("system", 60), t("human", 60), t("gpt", 5), t("tool", 5)]
    turns += [t("human", 8), t("human", 1), t("gpt", 1), t("human", 1), t("gpt", 1)]
    summariser = ScriptedSummariser()
    out, metrics = run(make(config(), summariser), turns)
    assert out == turns and metrics.still_over_limit and not metrics.was_compressed
    assert summariser.prompts == []


def test_still_over_limit_is_flagged() -> None:
    turns = [t("system", 200), t("human", 5), t("gpt", 5), t("tool", 5)]
    turns += [t("gpt", 60), t("human", 60)]
    turns += [t("human", 1), t("gpt", 1), t("human", 1), t("gpt", 1)]
    _, metrics = run(make(config()), turns)
    assert metrics.was_compressed and metrics.still_over_limit


# -- properties (seeded) ---------------------------------------------------------------------------


def random_trajectory(rng: random.Random) -> list[dict[str, str]]:
    turns = [t("system", rng.randint(1, 30)), t("human", rng.randint(1, 30))]
    for _ in range(rng.randint(0, 14)):
        kind = rng.random()
        if kind < 0.55:
            turns.append(t("gpt", rng.randint(1, 60), f"c{len(turns)}"))
            for _ in range(rng.randint(1, 2)):
                turns.append(t("tool", rng.randint(1, 80), f"r{len(turns)}"))
        elif kind < 0.8:
            turns.append(t("gpt", rng.randint(1, 60), f"g{len(turns)}"))
        else:
            turns.append(t("human", rng.randint(1, 40), f"h{len(turns)}"))
    return turns


def pairs(turns: Sequence[dict[str, str]]) -> list[tuple[str, str]]:
    """Every (call turn, response turn) adjacency: a tool turn and the turn before it."""
    return [
        (turns[i - 1]["value"], turns[i]["value"])
        for i in range(1, len(turns))
        if turns[i]["from"] == "tool"
    ]


@pytest.mark.parametrize("seed", range(4))
def test_property_calls_and_responses_are_never_separated(seed: int) -> None:
    rng = random.Random(9000 + seed)
    for _ in range(150):
        turns = random_trajectory(rng)
        cfg = config(
            compression={
                "target_max_tokens": rng.randint(20, 200),
                "summary_target_tokens": rng.randint(1, 15),
            },
            protected_turns={
                "first_gpt": rng.random() < 0.7,
                "first_tool": rng.random() < 0.7,
                "last_n": rng.randint(0, 4),
            },
            output={"add_summary_notice": False},
        )
        out, metrics = run(make(cfg), turns)
        if not metrics.was_compressed:
            assert out == turns
            continue
        start, end = metrics.region_start, metrics.region_end
        assert turns[start]["from"] != "tool"
        assert end >= len(turns) or turns[end]["from"] != "tool"
        summary_at = start
        assert out[summary_at]["from"] == "human"
        assert summary_at + 1 >= len(out) or out[summary_at + 1]["from"] != "tool"
        # every response still sits right after the very turn it followed before
        assert set(pairs(out)) <= set(pairs(turns))
        assert len(pairs(out)) == len(pairs(turns[:start])) + len(pairs(turns[end:]))
        assert out[:start] == turns[:start] and out[start + 1 :] == turns[end:]
        assert out[summary_at]["value"].count(SUMMARY_MARKER) == 1


@pytest.mark.parametrize("seed", range(2))
def test_property_protected_turns_are_byte_identical(seed: int) -> None:
    rng = random.Random(4100 + seed)
    for _ in range(150):
        turns = random_trajectory(rng)
        cfg = config(
            compression={"target_max_tokens": rng.randint(20, 120), "summary_target_tokens": 5},
            output={"add_summary_notice": False},
        )
        out, metrics = run(make(cfg), turns)
        firsts: dict[str, int] = {}
        for index, turn in enumerate(turns):
            firsts.setdefault(turn["from"], index)
        tail = list(range(max(0, len(turns) - cfg.protected_turns.last_n), len(turns)))
        shift = (metrics.region_turns - 1) if metrics.was_compressed else 0
        for index in {*firsts.values(), *tail}:
            moved = (
                index - shift if metrics.was_compressed and index >= metrics.region_end else index
            )
            assert out[moved] == turns[index]


# -- the provider-layer summariser -----------------------------------------------------------------


@dataclass
class FakeAux:
    calls: list[tuple[str, Any, dict[str, Any]]] = field(
        default_factory=list[tuple[str, Any, dict[str, Any]]]
    )

    def _answer(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(text="recap", response=SimpleNamespace(finish_reason="length"))

    async def call(self, task: str, messages: Any, **options: Any) -> Any:
        self.calls.append(("call", task, {"messages": messages, **options}))
        return self._answer()

    async def call_route(self, task: str, route: Any, messages: Any, **options: Any) -> Any:
        self.calls.append(("call_route", task, {"route": route, "messages": messages, **options}))
        return self._answer()


def test_aux_summariser_uses_the_auxiliary_model_by_default(scope: Any) -> None:
    from indus.research.compressor import aux_summariser

    aux = FakeAux()
    settings = CompressionConfig().summarization
    summarise = aux_summariser(scope, settings, client=aux)
    reply = asyncio.run(summarise("P", max_tokens=40, temperature=0.3))
    assert reply == SummaryReply("recap", "length")
    [(kind, task, options)] = aux.calls
    assert (kind, task) == ("call", "compression")
    assert options["messages"] == [{"role": "user", "content": "P"}]
    assert options["max_tokens"] == 40 and options["temperature"] == 0.3


def test_aux_summariser_with_an_explicit_endpoint_and_secret(scope: Any) -> None:
    from indus.foundation.errors import ProviderError
    from indus.research.compressor import aux_summariser

    aux = FakeAux()
    settings = CompressionConfig().summarization.model_copy(
        update={
            "model": "sum-model",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key_secret": "RESEARCH_SUMMARY_KEY",
        }
    )
    summarise = aux_summariser(scope, settings, client=aux)
    with pytest.raises(ProviderError, match="RESEARCH_SUMMARY_KEY"):
        asyncio.run(summarise("P", max_tokens=40, temperature=0.3))
    value = "summary" + "Key" * 5
    with_key = scope.with_env(scope.env.with_overlay({"RESEARCH_SUMMARY_KEY": value}))
    summarise = aux_summariser(with_key, settings, client=aux)
    asyncio.run(summarise("P", max_tokens=40, temperature=0.3))
    [(kind, task, options)] = aux.calls
    assert (kind, task) == ("call_route", "compression")
    route = options["route"]
    assert route.model == "sum-model" and route.base_url.startswith("http://127.0.0.1:9")
    assert route.api_key.reveal() == value
