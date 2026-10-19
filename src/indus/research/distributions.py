"""Toolset distributions for batch runs (P22-02).

A distribution names toolset entries with an inclusion chance in percent. Each prompt rolls every
entry on its own (``rng.random() * 100 < percent``), so any number of toolsets can come up. An
entry may join several toolsets with ``+``: that group is rolled once and its members always come
together. An entry naming a toolset the registry does not know is warned about once per sampler
and skipped. When every roll misses, the single most likely valid entry is used, so a prompt never
runs without tools.

The table names toolsets as the research table does; ``TOOLSET_ALIASES`` maps those names to ours
in one place (D-16.63). Today every name exists in our registry under the same name.

``prompt_rng(seed, index)`` gives each prompt its own generator from the run seed and the prompt's
index, so a resumed run (other workers, other order) samples the same toolsets for the same prompt.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

__all__ = [
    "DEFAULT_DISTRIBUTION",
    "DISTRIBUTIONS",
    "TOOLSET_ALIASES",
    "Distribution",
    "Sampler",
    "UnknownDistribution",
    "get",
    "listing",
    "names",
    "our_names",
    "prompt_rng",
]

log = logging.getLogger(__name__)

DEFAULT_DISTRIBUTION = "default"
GROUP_JOIN = "+"

TOOLSET_ALIASES: Mapping[str, str] = {
    "web": "web",
    "search": "search",
    "vision": "vision",
    "image_gen": "image_gen",
    "terminal": "terminal",
    "file": "file",
    "browser": "browser",
}
"""Toolset names used by the table → our toolset names (one place to follow a rename)."""


@dataclass(frozen=True)
class Distribution:
    name: str
    description: str
    entries: tuple[tuple[str, int], ...]


def _dist(name: str, description: str, *entries: tuple[str, int]) -> Distribution:
    return Distribution(name, description, entries)


_TABLE: tuple[Distribution, ...] = (
    _dist(
        "default",
        "Every toolset on every prompt",
        ("web", 100), ("vision", 100), ("image_gen", 100),
        ("terminal", 100), ("file", 100), ("browser", 100),
    ),
    _dist(
        "image_gen",
        "Mostly making and reading images, with some web and shell",
        ("image_gen", 90), ("vision", 90), ("web", 55), ("terminal", 45),
    ),
    _dist(
        "research",
        "Looking things up on the web and in pages, sometimes reading images",
        ("web", 90), ("browser", 70), ("vision", 50), ("terminal", 10),
    ),
    _dist(
        "science",
        "Web, shell and files nearly always; images and browsing often",
        ("web", 94), ("terminal", 94), ("file", 94),
        ("vision", 65), ("browser", 50), ("image_gen", 15),
    ),
    _dist(
        "development",
        "Shell and files for coding, now and then a web lookup",
        ("terminal", 80), ("file", 80), ("web", 30), ("vision", 10),
    ),
    _dist(
        "safe",
        "No shell: web, browsing and images only",
        ("web", 80), ("browser", 70), ("vision", 60), ("image_gen", 60),
    ),
    _dist(
        "balanced",
        "Each toolset on a coin flip",
        ("web", 50), ("vision", 50), ("image_gen", 50),
        ("terminal", 50), ("file", 50), ("browser", 50),
    ),
    _dist("minimal", "Web search and extraction only", ("web", 100)),
    _dist("terminal_only", "Shell and files only", ("terminal", 100), ("file", 100)),
    _dist(
        "terminal_web",
        "Shell and files plus the web for documentation",
        ("terminal", 100), ("file", 100), ("web", 100),
    ),
    _dist(
        "creative",
        "Making and reading images, a little web",
        ("image_gen", 90), ("vision", 90), ("web", 30),
    ),
    _dist(
        "reasoning",
        "Mostly thinking with the web, few other tools",
        ("web", 90), ("file", 60), ("terminal", 20),
    ),
    _dist(
        "browser_use",
        "Operating web pages with search and image reading",
        ("browser", 100), ("web", 80), ("vision", 70),
    ),
    _dist("browser_only", "Operating web pages and nothing else", ("browser", 100)),
    _dist(
        "browser_tasks",
        "Browsing with web search always beside it (search finds the pages to open)",
        ("browser+search", 97), ("vision", 12), ("terminal", 15),
    ),
    _dist(
        "terminal_tasks",
        "Shell, files and web nearly always, other toolsets sometimes",
        ("terminal", 97), ("file", 97), ("web", 97),
        ("browser", 75), ("vision", 50), ("image_gen", 10),
    ),
    _dist(
        "mixed_tasks",
        "Browsing, shell and files nearly always, the rest now and then",
        ("browser", 92), ("terminal", 92), ("file", 92),
        ("web", 35), ("vision", 15), ("image_gen", 15),
    ),
)  # fmt: skip

DISTRIBUTIONS: Mapping[str, Distribution] = {d.name: d for d in _TABLE}


class UnknownDistribution(ValueError):
    """The distribution name is not in the table."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"unknown distribution {name!r}; choose one of: {', '.join(names())}")


def names() -> list[str]:
    return [d.name for d in _TABLE]


def get(name: str) -> Distribution:
    found = DISTRIBUTIONS.get(name)
    if found is None:
        raise UnknownDistribution(name)
    return found


def our_names(entry: str) -> tuple[str, ...]:
    """The toolsets one entry stands for, under our names."""
    members = [part.strip() for part in entry.split(GROUP_JOIN) if part.strip()]
    return tuple(TOOLSET_ALIASES.get(member, member) for member in members)


def listing(dist: Distribution) -> list[tuple[str, int]]:
    """The entries, most likely first (ties by name)."""
    return sorted(dist.entries, key=lambda item: (-item[1], item[0]))


def prompt_rng(seed: int, index: int) -> random.Random:
    """The generator for one prompt: depends only on the run seed and the prompt index."""
    return random.Random(f"{seed}:{index}")


class Sampler:
    """Rolls distributions against the set of toolsets that exist (``valid``)."""

    def __init__(self, valid: Iterable[str], warn: Callable[[str], None] | None = None) -> None:
        self.valid = frozenset(valid)
        self._warn = warn or log.warning
        self._warned: set[tuple[str, str]] = set()

    def _usable(self, dist: Distribution, entry: str) -> tuple[str, ...] | None:
        members = our_names(entry)
        missing = [m for m in members if m not in self.valid]
        if not members or missing:
            key = (dist.name, entry)
            if key not in self._warned:
                self._warned.add(key)
                self._warn(
                    f"distribution {dist.name!r}: toolset {'+'.join(missing) or entry!r} "
                    "does not exist; entry skipped"
                )
            return None
        return members

    def sample(self, dist: Distribution | str, rng: random.Random) -> list[str]:
        """Our toolset names for one prompt (see the module notes)."""
        found = get(dist) if isinstance(dist, str) else dist
        chosen: list[str] = []
        best: tuple[int, tuple[str, ...]] | None = None
        for entry, percent in found.entries:
            members = self._usable(found, entry)
            if members is None:
                continue
            if best is None or percent > best[0]:
                best = (percent, members)
            if rng.random() * 100 < percent:
                chosen.extend(m for m in members if m not in chosen)
        if not chosen and best is not None:
            chosen.extend(best[1])
        return chosen
