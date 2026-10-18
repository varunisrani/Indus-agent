"""P04-12: result thresholds, context scaling, and the shared head/tail truncation."""

from __future__ import annotations

import math
import random
import re

import pytest

from indus.tools.budget import (
    DEFAULT_BUDGET,
    MCP_PREFIX,
    UNLIMITED,
    Budget,
    budget_for_window,
)
from indus.tools.truncate import HEAD_SHARE, cut_marker, head_tail, split


@pytest.mark.parametrize(
    ("window", "per_result", "per_turn"),
    [
        (None, 100_000, 200_000),
        (0, 100_000, 200_000),
        (-5, 100_000, 200_000),
        (8_000, 8_000, 16_000),  # 4.8k / 9.6k floored
        (32_000, 19_200, 38_400),
        (64_000, 38_400, 76_800),
        (200_000, 100_000, 200_000),  # 120k / 240k capped
        (1_000_000, 100_000, 200_000),
    ],
)
def test_threshold_table_by_window(window: int | None, per_result: int, per_turn: int) -> None:
    budget = budget_for_window(window)
    assert (budget.per_result, budget.per_turn) == (per_result, per_turn)
    assert budget.preview == 1_500


def test_defaults() -> None:
    assert DEFAULT_BUDGET.threshold("web_search") == 100_000
    assert DEFAULT_BUDGET.per_turn == 200_000


def test_read_file_unlimited_even_when_overridden() -> None:
    budget = Budget(overrides={"read_file": 10})
    assert math.isinf(budget.threshold("read_file"))
    assert math.isinf(budget_for_window(8_000).threshold("read_file"))


def test_mcp_prefix_tighter_and_capped_by_scaled_default() -> None:
    assert DEFAULT_BUDGET.threshold(f"{MCP_PREFIX}files__read") == 50_000
    assert budget_for_window(32_000).threshold(f"{MCP_PREFIX}files__read") == 19_200
    assert DEFAULT_BUDGET.threshold("mcp_lookalike") == 100_000


def test_override_wins_over_prefix_and_default() -> None:
    budget = Budget(overrides={f"{MCP_PREFIX}x__y": 70_000, "terminal": 30_000})
    assert budget.threshold(f"{MCP_PREFIX}x__y") == 70_000
    assert budget.threshold("terminal") == 30_000


def test_registered_cap_clamped_to_per_result() -> None:
    small = budget_for_window(32_000)
    assert small.threshold("web_extract", registered_cap=100_000) == 19_200
    assert small.threshold("web_extract", registered_cap=5_000) == 5_000
    assert small.threshold("web_extract", registered_cap=UNLIMITED) == UNLIMITED


def test_budget_is_frozen() -> None:
    with pytest.raises(AttributeError):
        DEFAULT_BUDGET.per_result = 1  # type: ignore[misc]


# -- head/tail ------------------------------------------------------------------------------------


def test_text_within_limit_untouched() -> None:
    assert head_tail("short", 10) == "short"
    assert head_tail("exact", 5) == "exact"


def test_marker_names_omitted_and_total() -> None:
    text = "".join(str(i % 10) for i in range(1_000))
    out = head_tail(text, 100)
    assert out.startswith(text[:40]) and out.endswith(text[-60:])
    assert "900 of 1,000 chars" in out
    assert out.count("shortened") == 1


def test_label_and_custom_head_share() -> None:
    out = head_tail("a" * 50 + "b" * 50, 10, head=0.0, label="terminal output")
    assert out.endswith("b" * 10) and "terminal output shortened" in out
    out = head_tail("a" * 50 + "b" * 50, 10, head=1.0)
    assert out.startswith("a" * 10) and not out.endswith("b")


def test_split_validates() -> None:
    assert split(100) == (40, 60)
    with pytest.raises(ValueError):
        split(-1)
    with pytest.raises(ValueError):
        split(10, head=1.5)


def test_seeded_random_kept_length_is_the_limit() -> None:
    # Property check as a seeded table (hypothesis is not installed): the kept characters are
    # exactly ``limit`` and come from the two ends of the text.
    rng = random.Random(1204)
    for _ in range(300):
        text = "".join(rng.choice("ab\nc ") for _ in range(rng.randint(0, 3_000)))
        limit = rng.randint(0, 2_000)
        out = head_tail(text, limit)
        if len(text) <= limit:
            assert out == text
            continue
        first = int(limit * HEAD_SHARE)
        last = limit - first
        marker = cut_marker(len(text) - limit, len(text))
        assert out == text[:first] + marker + (text[-last:] if last else "")
        assert re.search(r"\d", marker)
