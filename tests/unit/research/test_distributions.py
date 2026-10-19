"""P22-02: toolset distributions and sampling."""

from __future__ import annotations

import random

import pytest

from indus.research import distributions
from indus.research.distributions import Distribution, Sampler, UnknownDistribution
from indus.tools import toolsets
from indus.tools.builtin import load_builtins
from indus.tools.registry import ToolRegistry

RESEARCH_TABLE = {
    "default": {"web": 100, "vision": 100, "image_gen": 100, "terminal": 100, "file": 100,
                "browser": 100},
    "image_gen": {"image_gen": 90, "vision": 90, "web": 55, "terminal": 45},
    "research": {"web": 90, "browser": 70, "vision": 50, "terminal": 10},
    "science": {"web": 94, "terminal": 94, "file": 94, "vision": 65, "browser": 50,
                "image_gen": 15},
    "development": {"terminal": 80, "file": 80, "web": 30, "vision": 10},
    "safe": {"web": 80, "browser": 70, "vision": 60, "image_gen": 60},
    "balanced": {"web": 50, "vision": 50, "image_gen": 50, "terminal": 50, "file": 50,
                 "browser": 50},
    "minimal": {"web": 100},
    "terminal_only": {"terminal": 100, "file": 100},
    "terminal_web": {"terminal": 100, "file": 100, "web": 100},
    "creative": {"image_gen": 90, "vision": 90, "web": 30},
    "reasoning": {"web": 90, "file": 60, "terminal": 20},
    "browser_use": {"browser": 100, "web": 80, "vision": 70},
    "browser_only": {"browser": 100},
    "browser_tasks": {"browser+search": 97, "vision": 12, "terminal": 15},
    "terminal_tasks": {"terminal": 97, "file": 97, "web": 97, "browser": 75, "vision": 50,
                       "image_gen": 10},
    "mixed_tasks": {"browser": 92, "terminal": 92, "file": 92, "web": 35, "vision": 15,
                    "image_gen": 15},
}  # fmt: skip


def valid_names() -> set[str]:
    registry = ToolRegistry()
    load_builtins(registry)
    return set(toolsets.toolset_names(registry))


def test_table_matches_the_research_table() -> None:
    assert len(distributions.DISTRIBUTIONS) == 17
    got = {d.name: dict(d.entries) for d in distributions.DISTRIBUTIONS.values()}
    assert got == RESEARCH_TABLE
    assert all(d.description for d in distributions.DISTRIBUTIONS.values())


def test_every_name_resolves_in_the_toolset_registry() -> None:
    """Fails when a toolset the table uses is renamed without updating the alias table."""
    known = valid_names()
    for dist in distributions.DISTRIBUTIONS.values():
        for entry, _ in dist.entries:
            for name in distributions.our_names(entry):
                assert name in known, (dist.name, entry, name)


def test_grouped_entry_co_occurs_exactly() -> None:
    sampler = Sampler(valid_names())
    dist = distributions.get("browser_tasks")
    rng = random.Random(1234)
    both = browser_only = search_only = 0
    for _ in range(10_000):
        picked = set(sampler.sample(dist, rng))
        both += {"browser", "search"} <= picked
        browser_only += "browser" in picked and "search" not in picked
        search_only += "search" in picked and "browser" not in picked
    assert browser_only == 0 and search_only == 0
    assert 9_500 < both <= 10_000  # 97 %, plus the fallback when every roll misses


def test_all_rolls_missing_falls_back_to_the_top_entry() -> None:
    class Never(random.Random):
        def random(self) -> float:
            return 0.9999

    sampler = Sampler(valid_names())
    assert sampler.sample("research", Never()) == ["web"]
    assert sampler.sample("browser_tasks", Never()) == ["browser", "search"]
    assert sampler.sample("terminal_tasks", Never()) == ["terminal"]  # first of the 97s


def test_unknown_toolset_is_warned_once_and_skipped() -> None:
    warnings: list[str] = []
    sampler = Sampler({"web"}, warn=warnings.append)
    dist = Distribution("odd", "test", (("web", 100), ("teleport", 100), ("web+warp", 100)))
    for seed in range(5):
        assert sampler.sample(dist, random.Random(seed)) == ["web"]
    assert len(warnings) == 2
    assert "teleport" in warnings[0] and "warp" in warnings[1]


def test_unknown_distribution_lists_the_names() -> None:
    with pytest.raises(UnknownDistribution) as caught:
        distributions.get("nope")
    message = str(caught.value)
    assert "'nope'" in message and "terminal_only" in message and "mixed_tasks" in message


def test_per_prompt_seeding_is_reproducible() -> None:
    sampler = Sampler(valid_names())
    first = [sampler.sample("balanced", distributions.prompt_rng(7, i)) for i in range(50)]
    again = [
        sampler.sample("balanced", distributions.prompt_rng(7, i)) for i in reversed(range(50))
    ]
    assert first == list(reversed(again))
    other = [sampler.sample("balanced", distributions.prompt_rng(8, i)) for i in range(50)]
    assert first != other


def test_listing_is_sorted_high_to_low() -> None:
    listed = distributions.listing(distributions.get("browser_tasks"))
    assert listed == [("browser+search", 97), ("terminal", 15), ("vision", 12)]
    ties = distributions.listing(distributions.get("terminal_web"))
    assert ties == [("file", 100), ("terminal", 100), ("web", 100)]
