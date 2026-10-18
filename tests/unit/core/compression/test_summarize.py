"""The summary call and the deterministic fallback (P08-04)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from indus.core.compression import texts
from indus.core.compression.fallback_summary import (
    USER_BUDGET,
    USER_MESSAGE_MAX,
    anchor_index,
    fallback_summary,
    user_messages,
)
from indus.core.compression.summarize import (
    INPUT_MAX_CHARS,
    SUMMARY_CEILING,
    AuxSummarizer,
    SummaryFailure,
    SummaryReply,
    build_prompt,
    call_summarizer,
    handoff_text,
    previous_notes,
    records,
    sample,
    summary_budget,
)
from indus.core.transcript import TranscriptRow
from indus.foundation.scope import Scope
from indus.providers import context_length
from indus.providers.auxiliary import AuxClient
from indus.providers.context_length import ContextWindow
from tests.fakes.agent import run_driven
from tests.fakes.aux_model import (
    FakeAuxModel,
    Stall,
    closed_stream,
    empty,
    overloaded,
    truncated,
    unauthorised,
)
from tests.fakes.clock import FakeClock
from tests.unit.core.compression._transcripts import call
from tests.unit.providers._support import make_route


def rows_of(tokens: int) -> list[TranscriptRow]:
    chars = tokens * 4
    return [
        TranscriptRow(role="user", content="u" * (chars // 2)),
        TranscriptRow(role="assistant", content="a" * (chars // 2)),
    ]


@pytest.mark.parametrize(
    ("tokens", "window", "expected"),
    [
        (1_000, 1_000_000, 2_000),  # floor
        (30_000, 1_000_000, 6_000),  # 20 % of the content
        (200_000, 1_000_000, SUMMARY_CEILING),  # the code constant (D-16.24)
        (200_000, 64_000, 3_200),  # 5 % of a small window
        (200_000, 20_000, 2_000),  # the floor beats 5 % of a tiny window
    ],
)
def test_budget_clamp_table(tokens: int, window: int, expected: int) -> None:
    assert summary_budget(rows_of(tokens), window) == pytest.approx(expected, abs=10)


def test_sampled_input_is_bounded() -> None:
    items = [f"record {n} " + "z" * 2_000 for n in range(400)]
    text, sampled = sample(items)
    assert sampled
    assert len(text) <= INPUT_MAX_CHARS
    assert "record 0 " in text and "record 399 " in text
    assert "left out" in text
    small, was_sampled = sample(items[:3])
    assert not was_sampled and small.count("record") == 3
    huge, _ = sample(["q" * 500_000, "newest record"])
    assert len(huge) <= INPUT_MAX_CHARS and "newest record" in huge


def test_records_cut_and_redact() -> None:
    fake_key = "sk-" + "Q7x" * 16
    rows = [
        TranscriptRow(role="system", content="never summarised"),
        TranscriptRow(role="user", content=f"use key {fake_key} please"),
        TranscriptRow(role="assistant", content="", tool_calls=[call("c1", path="a" * 5_000)]),
        TranscriptRow(role="tool", name="read_file", tool_call_id="c1", content="t" * 20_000),
    ]
    out = records(rows)
    assert len(out) == 3
    joined = "\n".join(out)
    assert fake_key not in joined
    assert "never summarised" not in joined
    assert len(out[1]) < 1_700
    assert len(out[2]) < 6_200 and "left out" in out[2]


def test_second_pass_updates_the_previous_summary() -> None:
    notes = "## Goal\nShip the parser.\n\n## Decisions\nUse a table."
    handoff = handoff_text(notes, [], extras=False)
    rows = [
        TranscriptRow(role="user", content=handoff),
        TranscriptRow(role="assistant", content="ok"),
    ]
    assert previous_notes(rows) == notes
    fresh = build_prompt("RECORDS-TEXT", budget=3_000)
    assert texts.FRESH_TASK in fresh and texts.PREVIOUS_OPEN not in fresh
    update = build_prompt("RECORDS-TEXT", budget=3_000, previous=notes, focus="the parser")
    assert texts.UPDATE_TASK in update
    assert notes in update
    assert update.index(texts.PREVIOUS_OPEN) < update.index("RECORDS-TEXT")
    assert update.rstrip().endswith("Still\nnever copy credentials.")
    assert "about 3000 tokens" in update
    assert "## Session log" in update
    assert "## Session log" not in build_prompt("R", budget=3_000, mode="legacy")


def _middle() -> list[TranscriptRow]:
    return [
        TranscriptRow(role="user", content="Fix issue #1234 in src/indus/core/loop.py please"),
        TranscriptRow(
            role="assistant", content="", tool_calls=[call("c1", path="src/indus/core/loop.py")]
        ),
        TranscriptRow(
            role="tool",
            name="read_file",
            tool_call_id="c1",
            content="ValueError: bad thing happened at line 3\ncommit 3f9a2c1b7d",
        ),
        TranscriptRow(role="assistant", content="Found it; see https://example.test/issues/1234"),
        TranscriptRow(role="user", content="Now also update docs/notes/P08/summary.md"),
        TranscriptRow(role="assistant", content="Will do."),
    ]


def test_fallback_keeps_user_messages_and_anchors() -> None:
    middle = _middle()
    body = fallback_summary(middle, previous="## Goal\nolder notes", reason="stall")
    text = handoff_text(body, middle, extras=True)
    assert text.startswith(texts.SUMMARY_OPEN) and text.endswith(texts.SUMMARY_CLOSE)
    for row in middle:
        if row.role == "user":
            assert str(row.content) in text
    for needle in ("3f9a2c1b7d", "src/indus/core/loop.py", "#1234", "docs/notes/P08/summary.md"):
        assert needle in text
    assert "ValueError: bad thing happened" in text
    assert "older notes" in text
    assert "The summary call failed: stall." in text
    users = text.index(texts.USERS_HEADING)
    assert text.index("Now also update", users) < text.index("Fix issue #1234", users)


def test_anchor_index_ranks_and_bounds() -> None:
    rows = [TranscriptRow(role="user", content="sha abc1234def and abc1234def and deadbeef99")]
    index = anchor_index(rows)
    assert "abc1234def (×2)" in index
    assert index.index("abc1234def") < index.index("deadbeef99")
    assert anchor_index([TranscriptRow(role="user", content="1234567 is a number")]) == ""
    many = [TranscriptRow(role="user", content=" ".join(f"#{n:05d}" for n in range(3_000)))]
    assert len(anchor_index(many, budget=500)) <= 500


def test_user_messages_budget() -> None:
    rows = [TranscriptRow(role="user", content=f"{n}:" + "m" * 6_000) for n in range(10)]
    said = user_messages(rows)
    assert said[0].startswith("9:")
    assert all(len(s) <= USER_MESSAGE_MAX for s in said)
    assert sum(len(s) for s in said) <= USER_BUDGET


def _call(model: FakeAuxModel, clock: FakeClock | None = None) -> str | SummaryFailure:
    async def go() -> str | SummaryFailure:
        try:
            return await call_summarizer(model, "prompt", timeout_s=120.0, clock=clock)
        except SummaryFailure as failed:
            return failed

    if clock is None:
        return asyncio.run(go())
    return asyncio.run(run_driven(go(), clock, step=30.0))


@pytest.mark.parametrize(
    ("step", "kind"),
    [
        (overloaded(), "overload"),
        (unauthorised(), "access"),
        (closed_stream(), "decode"),
        (truncated(), "truncated"),
        (empty(), "empty"),
        (SummaryReply("I can't help with that."), "empty"),
        (RuntimeError("something odd"), "error"),
        (TimeoutError("slow"), "stall"),
    ],
)
def test_failure_kinds(step: Any, kind: str) -> None:
    result = _call(FakeAuxModel(step))
    assert isinstance(result, SummaryFailure) and result.kind == kind


def test_stall_hits_the_deadline_on_the_clock(fake_clock: FakeClock) -> None:
    model = FakeAuxModel(Stall(10_000), clock=fake_clock)
    result = _call(model, fake_clock)
    assert isinstance(result, SummaryFailure) and result.kind == "stall"


def test_a_good_reply_is_returned_redacted() -> None:
    fake_key = "sk-" + "Zp9" * 16
    result = _call(FakeAuxModel(f"## Goal\nkey was {fake_key}"))
    assert isinstance(result, str) and fake_key not in result and "## Goal" in result


def test_no_summariser_is_unavailable() -> None:
    async def go() -> str:
        return await call_summarizer(None, "p", timeout_s=1.0)

    with pytest.raises(SummaryFailure) as caught:
        asyncio.run(go())
    assert caught.value.kind == "unavailable"


def test_aux_summariser_falls_back_to_the_main_route() -> None:
    main = FakeAuxModel("from the main route")
    reply = asyncio.run(AuxSummarizer(None, fallback=main).summarize("p"))
    assert reply.text == "from the main route"
    with pytest.raises(SummaryFailure):
        asyncio.run(AuxSummarizer(None).summarize("p"))


def test_aux_summariser_drives_the_real_aux_client(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration with P08-17: the task, the reply text, the finish reason and the window."""
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        choice = {
            "index": 0,
            "message": {"role": "assistant", "content": "## Goal\nfrom the aux client"},
            "finish_reason": "length",
        }
        return httpx.Response(200, json={"id": "c1", "object": "chat.completion",
                                         "choices": [choice]})  # fmt: skip

    looked: list[str] = []

    async def window(route: Any, *, scope: Scope, list_models: Any = None) -> ContextWindow:
        looked.append(route.model)
        return ContextWindow(32_000, "test")

    monkeypatch.setattr(context_length, "find_window", window)
    route = make_route(base_url="https://aux.example/v1", model="main-model", provider="custom")
    aux = AuxClient(
        scope,
        route,
        client_factory=lambda _r: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    summarizer = AuxSummarizer(aux)
    reply = asyncio.run(summarizer.summarize("the prompt"))
    assert reply == SummaryReply("## Goal\nfrom the aux client", "length")
    assert seen[0]["messages"] == [{"role": "user", "content": "the prompt"}]
    # ``auto`` is the main route: its window is the main one, nothing is looked up
    assert asyncio.run(summarizer.window(200_000)) == 200_000 and looked == []
    assert asyncio.run(AuxSummarizer(None).window(200_000)) is None
