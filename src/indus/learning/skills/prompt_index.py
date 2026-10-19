"""The skills index in the system prompt, and the mid-session change note (R07.29).

The index is a compact block in the prompt's volatile tier::

    ## Skills
    <guidance>

    <skills_index>
    ### <category>[ — <category label>]
    - <name>: [project] <description clipped to 60 characters, "…" when clipped>
    [names only] <category>: a, b, c
    </skills_index>

``[project]`` marks a skill from the trusted repo's own folders (P13-28); other rows carry no
tag. It is built once per prompt build (``SkillsIndexBuilder`` wraps the P04 builder) and frozen
with the prompt. Each turn ``stage_index_delta`` compares the rows the prompt carries with the
rows the session would build now and stages one note on the turn's note channel when that
difference changed; the prompt itself only changes at the next compaction rebuild.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from indus.core.prompt.builder import BuildContext, PromptParts
from indus.foundation.scope import Scope
from indus.learning.skills.format import VisibilityContext
from indus.learning.skills.resolver import TIER_PROJECT, SkillCatalog, SkillRecord, load_catalog

__all__ = [
    "BLOCK_END",
    "BLOCK_START",
    "DESCRIPTION_CLIP",
    "IndexDeltaTracker",
    "IndexOptions",
    "SkillsIndexBuilder",
    "build_index",
    "catalog_index_source",
    "clip_description",
    "index_rows",
    "render_delta_note",
    "stage_index_delta",
]

DESCRIPTION_CLIP = 60
CLIP_MARK = "…"
BLOCK_START = "<skills_index>"
BLOCK_END = "</skills_index>"
GENERAL = "general"
NAMES_ONLY = "[names only]"
VIEW_TOOL = "skill_view"
PROJECT_TAG = "[project] "

HEADING = "## Skills"
GUIDANCE = (
    "Skills are saved, tested procedures for recurring kinds of work. When the request matches a "
    "skill below, open it with skill_view(name) first and follow it rather than improvising. "
    f"Descriptions are cut at {DESCRIPTION_CLIP} characters; a row ending in {CLIP_MARK} has "
    "more to say once viewed. Rows marked [names only] list skills by name; view one to see it."
)

NOTE_HEAD = (
    "[Note: the skills available in this conversation changed after the system prompt was written."
)
NOTE_ADDED = "Added (open with skill_view(name) when relevant):"
NOTE_REMOVED = "No longer available:"
NOTE_CURRENT = (
    " The skills list in the system prompt is accurate again; ignore earlier skill notes."
)

_SPACES = re.compile(r"\s+")


class _Notes(Protocol):
    def stage_skills_delta(self, text: str) -> None: ...


class _Builder(Protocol):
    async def build(self, ctx: BuildContext) -> PromptParts: ...


def clip_description(text: str, limit: int = DESCRIPTION_CLIP) -> str:
    """One line, at most ``limit`` characters; a clipped text ends in ``…``."""
    flat = _SPACES.sub(" ", text).strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + CLIP_MARK


@dataclass(frozen=True)
class IndexOptions:
    """What shapes one index: visibility inputs and the categories shown as names only."""

    tools: frozenset[str] = frozenset()
    toolsets: frozenset[str] = frozenset()
    platform: str | None = None
    compact_categories: frozenset[str] = frozenset()

    def visibility(self) -> VisibilityContext:
        return VisibilityContext(
            toolsets=self.toolsets, tools=self.tools, session_platform=self.platform or None
        )


def _row(rec: SkillRecord) -> str:
    tag = PROJECT_TAG if rec.tier == TIER_PROJECT else ""
    return f"- {rec.name}: {tag}{clip_description(rec.description)}"


def build_index(catalog: SkillCatalog, options: IndexOptions) -> str:
    """The whole block, or "" when nothing is visible."""
    shown = catalog.visible(options.visibility())
    ambiguous = catalog.ambiguous_names()
    groups: dict[str, list[SkillRecord]] = {}
    for rec in shown:
        groups.setdefault(rec.category or GENERAL, []).append(rec)
    if not groups:
        return ""
    lines: list[str] = []
    for category in sorted(groups, key=lambda c: (c != GENERAL, c)):
        members = sorted(groups[category], key=lambda r: (r.name, r.relative_path))
        if category in options.compact_categories:
            names = sorted({r.name for r in members})
            lines.append(f"{NAMES_ONLY} {category}: {', '.join(names)}")
            continue
        label = catalog.category_labels.get(category, "")
        lines.append(f"### {category}" + (f" — {clip_description(label)}" if label else ""))
        noted: set[str] = set()
        for rec in members:
            if rec.name in ambiguous:
                if rec.name in noted:
                    continue
                noted.add(rec.name)
                paths = sorted(r.load_name or str(r.skill_dir) for r in ambiguous[rec.name])
                lines.append(
                    f"- {rec.name}: [{len(paths)} skills share this name; view one by path "
                    f"({', '.join(paths)}) and rename the other]"
                )
                continue
            lines.append(_row(rec))
    return "\n".join([HEADING, GUIDANCE, "", BLOCK_START, *lines, BLOCK_END])


def index_rows(text: str) -> dict[str, str]:
    """``{name: row}`` for the index block inside ``text`` (names-only names map to "")."""
    start = text.find(BLOCK_START)
    end = text.find(BLOCK_END, start + 1)
    if start < 0 or end < 0:
        return {}
    rows: dict[str, str] = {}
    for line in text[start + len(BLOCK_START) : end].splitlines():
        if line.startswith("- "):
            name = line[2:].split(": ", 1)[0].strip()
            if name:
                rows.setdefault(name, line)
        elif line.startswith(NAMES_ONLY):
            _, _, names = line[len(NAMES_ONLY) :].partition(": ")
            for name in (n.strip() for n in names.split(",")):
                if name:
                    rows.setdefault(name, "")
    return rows


def render_delta_note(added: dict[str, str], removed: Iterable[str]) -> str:
    gone = sorted(removed)
    if not added and not gone:
        return NOTE_HEAD + NOTE_CURRENT + "]"
    parts = [NOTE_HEAD]
    if added:
        parts.append(NOTE_ADDED)
        parts.extend(added[name] or f"- {name}" for name in sorted(added))
    if gone:
        parts.append(f"{NOTE_REMOVED} {', '.join(gone)}")
    return "\n".join(parts) + "]"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class IndexDeltaTracker:
    """Remembers what the session was last told so each difference is announced once."""

    def __init__(self) -> None:
        self._told: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset())
        self._prompt_digest = ""

    def note(self, prompt_text: str, current_index: str) -> str | None:
        """The note to send with the next user message, or None."""
        in_prompt = index_rows(prompt_text)
        now = index_rows(current_index)
        added = {name: row for name, row in now.items() if name not in in_prompt}
        removed = {name for name in in_prompt if name not in now}
        delta = (frozenset(added), frozenset(removed))
        digest = _digest(prompt_text)
        rebuilt = bool(self._prompt_digest) and digest != self._prompt_digest
        self._prompt_digest = digest
        if delta == self._told:
            return None
        self._told = delta
        if not added and not removed and rebuilt:
            return None  # a rebuilt prompt already carries the change
        return render_delta_note(added, removed)


def stage_index_delta(
    notes: _Notes, tracker: IndexDeltaTracker, prompt_text: str | None, current_index: str
) -> str | None:
    """Stage the change note on ``notes`` (the turn's note channel); return what was staged."""
    if prompt_text is None:
        return None
    note = tracker.note(prompt_text, current_index)
    if note is not None:
        notes.stage_skills_delta(note)
    return note


IndexSource = Callable[[BuildContext], str]


def catalog_index_source(
    scope: Scope,
    *,
    toolsets: frozenset[str] = frozenset(),
    compact_categories: frozenset[str] = frozenset(),
    catalog_factory: Callable[[Scope, str | None], SkillCatalog] | None = None,
) -> IndexSource:
    """An ``IndexSource`` that rescans the scope's skills on every call."""

    def source(ctx: BuildContext) -> str:
        if VIEW_TOOL not in ctx.tool_names:
            return ""
        platform = ctx.platform or None
        catalog = (
            catalog_factory(scope, platform)
            if catalog_factory is not None
            else load_catalog(scope, platform=platform)
        )
        options = IndexOptions(
            tools=ctx.tool_names,
            toolsets=toolsets,
            platform=platform,
            compact_categories=compact_categories,
        )
        return build_index(catalog, options)

    return source


class SkillsIndexBuilder:
    """A P04 ``Builder`` that fills ``skills_index`` (plus any extra blocks) before building.

    The agent calls ``build`` only for a new prompt or a compaction rebuild, so the index is
    frozen with the prompt by construction.
    """

    def __init__(
        self,
        inner: _Builder,
        source: IndexSource,
        *,
        extra_blocks: Iterable[IndexSource] = (),
    ) -> None:
        self._inner = inner
        self._source = source
        self._extra = tuple(extra_blocks)
        self.last_index = ""

    def current_index(self, ctx: BuildContext) -> str:
        return self._source(ctx)

    async def build(self, ctx: BuildContext) -> PromptParts:
        index = self._source(ctx)
        self.last_index = index
        blocks = [index, *(block(ctx) for block in self._extra)]
        text = "\n\n".join(b.strip() for b in blocks if b and b.strip())
        return await self._inner.build(replace(ctx, skills_index=text))
