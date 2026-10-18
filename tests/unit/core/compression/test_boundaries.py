"""Pruning and boundaries (P08-03)."""

from __future__ import annotations

import copy
import random

import pytest

from indus.core.compression import texts
from indus.core.compression.boundaries import (
    MAX_SKILL_MARKERS,
    MIN_TAIL_ROWS,
    TailPolicy,
    find_tail_start,
    head_end,
    is_real_user,
    pending_round,
    prune,
    window,
)
from indus.core.transcript import TranscriptRow
from tests.unit.core.compression._transcripts import call, conversation


@pytest.mark.parametrize(
    ("window_size", "mode", "trigger", "budget", "ceiling"),
    [
        (32_000, "lean", 0, 6_400, 6_400),
        (64_000, "lean", 0, 10_000, 12_800),
        (200_000, "lean", 0, 10_000, 15_000),
        (600_000, "lean", 0, 15_000, 22_500),
        (2_000_000, "lean", 0, 25_000, 37_500),
        (200_000, "legacy", 150_000, 30_000, 40_000),
        (1_000_000, "legacy", 500_000, 100_000, 150_000),
    ],
)
def test_tail_budget_table(
    window_size: int, mode: str, trigger: int, budget: int, ceiling: int
) -> None:
    policy = TailPolicy(window=window_size, trigger=trigger, mode=mode)
    assert policy.budget() == budget
    assert policy.soft_ceiling() == ceiling


def _check(rows: list[TranscriptRow], policy: TailPolicy) -> None:
    cut = window(rows, policy)
    count = len(rows)
    assert cut.head_end <= cut.tail_start <= count
    if cut.head_end < count:
        assert rows[cut.head_end].role != "tool"
    if cut.tail_start < count:
        assert rows[cut.tail_start].role != "tool"  # never between a call and its results
    available = count - cut.head_end - 1
    floor = 0 if available <= 1 else min(MIN_TAIL_ROWS, max(3, available - 2), available)
    if cut.tail_start > cut.head_end:
        assert count - cut.tail_start >= floor
    users_after_head = sum(1 for r in rows[cut.head_end :] if is_real_user(r))
    users_in_tail = sum(1 for r in rows[cut.tail_start :] if is_real_user(r))
    assert users_in_tail >= min(policy.min_user, users_after_head)


def test_property_boundaries() -> None:
    rng = random.Random(77)
    for _ in range(300):
        rows = conversation(rng, rng.randint(1, 25), pending=rng.random() < 0.3)
        policy = TailPolicy(
            window=rng.choice([32_000, 64_000, 128_000, 200_000]),
            mode=rng.choice(["lean", "legacy"]),
            trigger=rng.randint(20_000, 150_000),
            min_user=rng.randint(0, 3),
        )
        _check(rows, policy)
        boundary = find_tail_start(rows, head_end(rows), policy)
        before = copy.deepcopy(rows)
        result = prune(rows, boundary)
        assert [r.content for r in rows] == [r.content for r in before]  # input untouched
        spared = pending_round(rows)
        for index, (old, new) in enumerate(zip(rows, result.rows, strict=True)):
            changed = old.content != new.content
            if changed:
                assert index < boundary and index not in spared
                assert new.role == "tool"
                assert isinstance(new.content, str) and new.content.startswith(texts.PRUNED_PREFIX)
            else:
                assert new is old
        _check(result.rows, policy)


def test_long_outputs_before_the_tail_become_placeholders() -> None:
    rows = conversation(random.Random(3), 12, big=20_000)
    policy = TailPolicy(window=64_000)
    boundary = find_tail_start(rows, head_end(rows), policy)
    result = prune(rows, boundary)
    assert result.pruned > 0
    for row in result.rows[:boundary]:
        if row.role == "tool":
            assert isinstance(row.content, str)
            assert len(row.content) <= 200 or row.content.startswith(texts.PRUNED_PREFIX)
    assert result.rows[boundary:] == rows[boundary:]


def test_pending_round_is_never_pruned() -> None:
    rows = conversation(random.Random(5), 4, pending=True)
    spared = pending_round(rows)
    assert spared and rows[spared.start].role == "assistant"
    result = prune(rows, len(rows))
    for index in spared:
        assert result.rows[index] is rows[index]
    assert pending_round(rows[:-1]) == range(0) or rows[-2].role == "tool"


def test_skill_markers_capped_at_twenty() -> None:
    rows = [TranscriptRow(role="system", content="s"), TranscriptRow(role="user", content="go")]
    for n in range(25):
        ident = f"k{n}"
        rows.append(
            TranscriptRow(
                role="assistant",
                content="",
                tool_calls=[call(ident, "skill_view", name=f"skill-{n}")],
            )
        )
        rows.append(
            TranscriptRow(role="tool", name="skill_view", tool_call_id=ident, content="body " * 100)
        )
    rows.append(TranscriptRow(role="assistant", content="done"))
    result = prune(rows, len(rows))
    assert len(result.skills) == MAX_SKILL_MARKERS
    assert result.skills[0] == "skill-24" and result.skills[-1] == "skill-5"
    markers = [r.content for r in result.rows if r.role == "tool"]
    assert markers[0] == texts.SKILL_MARKER.format(name="skill-0")


def test_images_in_tool_results_are_dropped_with_a_note() -> None:
    rows = [
        TranscriptRow(role="system", content="s"),
        TranscriptRow(role="user", content="shot"),
        TranscriptRow(role="assistant", content="", tool_calls=[call("i1", "screenshot")]),
        TranscriptRow(
            role="tool",
            tool_call_id="i1",
            content=[
                {"type": "text", "text": "here"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        ),
        TranscriptRow(role="assistant", content="seen"),
    ]
    result = prune(rows, 5)
    content = result.rows[3].content
    assert isinstance(content, list)
    assert all(part.get("type") == "text" for part in content)
    assert texts.IMAGES_ELIDED.format(count=1) in content[-1]["text"]


def test_keep_last_fixes_the_tail() -> None:
    rows = conversation(random.Random(9), 10)
    cut = window(rows, TailPolicy(window=200_000), keep_last=4)
    assert len(rows) - cut.tail_start >= 4
    assert rows[cut.tail_start].role != "tool"


def test_tiny_transcript_has_no_middle() -> None:
    rows = conversation(random.Random(1), 1)
    cut = window(rows, TailPolicy(window=200_000))
    assert cut.empty
