# pyright: strict
"""Plugin system-prompt sections, rendered once per conversation and frozen (P13-18).

A plugin offers a section with ``ctx.register_prompt_section(id, text_or_callable)`` (P13-16 keeps
the entry). When a conversation's prompt is first built, ``render_sections`` turns the entries
into text, in id order, each callable getting a read-only mapping ``{session_id, model, provider,
platform, profile_name, cwd}``. A section is skipped with a warning when its callable raises,
misses the hook deadline, returns something other than text, returns blank text, is longer than
``SECTION_MAX_CHARS``, or carries our delimiters; at most ``MAX_SECTIONS`` are kept, and one that
would push the whole block past ``TOTAL_MAX_CHARS`` is skipped (a later, shorter one may still fit).

``format_block`` frames the kept sections between ``START`` and ``END``; each frame names the
section and the length of its text, so ``restore_block`` can cut the exact bytes back out of a
stored prompt and check them by framing them again. The block rides the prompt the lifecycle
stores, so:

- a rebuild after compaction reuses the block the session's ``FrozenSections`` already holds;
- a resumed conversation takes the block from its stored prompt and never runs plugin code;
- a stored prompt without a block means the conversation started with no sections: it keeps none.

``PluginSectionsBuilder`` wraps the P04 builder and fills ``BuildContext.plugin_sections``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from indus.core.prompt.builder import BuildContext, PromptParts
from indus.core.prompt.lifecycle import Builder, PromptStore
from indus.foundation.blocking import BlockingError, BlockingPool, DeadlineMissed, run_blocking
from indus.foundation.clock import Clock
from indus.plugins.context import PromptSectionEntry

__all__ = [
    "END",
    "MAX_SECTIONS",
    "SECTION_MAX_CHARS",
    "START",
    "TOTAL_MAX_CHARS",
    "FrozenSections",
    "PluginSectionsBuilder",
    "RenderedSection",
    "format_block",
    "render_sections",
    "restore_block",
    "session_info",
]

log = logging.getLogger(__name__)

SECTION_MAX_CHARS = 4_000
TOTAL_MAX_CHARS = 8_000
MAX_SECTIONS = 32
START = "<!-- indus-plugin-sections:start -->"
END = "<!-- indus-plugin-sections:end -->"
_FRAME = re.compile(
    r"<!-- indus-plugin-section id=([a-z0-9][a-z0-9._-]{0,127}) chars=(\d{1,5}) -->\n"
)
_SECTION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class RenderedSection:
    id: str
    text: str
    plugin: str = ""


def _frame(section: RenderedSection) -> str:
    return (
        f"<!-- indus-plugin-section id={section.id} chars={len(section.text)} -->\n{section.text}"
    )


def format_block(sections: Sequence[RenderedSection]) -> str:
    """The delimited block for ``sections`` ("" when there are none)."""
    if not sections:
        return ""
    return f"{START}\n" + "\n\n".join(_frame(s) for s in sections) + f"\n{END}"


def session_info(ctx: BuildContext, profile_name: str = "") -> Mapping[str, Any]:
    """The read-only mapping a section callable receives."""
    return types.MappingProxyType(
        {
            "session_id": ctx.session_id,
            "model": ctx.model,
            "provider": ctx.provider,
            "platform": ctx.platform,
            "profile_name": profile_name or ctx.scope.profile_id,
            "cwd": str(ctx.working_dir()),
        }
    )


# -- rendering ----------------------------------------------------------------------------------


def _call(render: Callable[[Mapping[str, Any]], Any], info: Mapping[str, Any]) -> Any:
    value = render(info)
    if inspect.isawaitable(value):
        awaited = value

        async def finish() -> Any:
            return await awaited

        return asyncio.run(finish())
    return value


async def _text_of(
    entry: PromptSectionEntry,
    info: Mapping[str, Any],
    deadline_s: float | None,
    clock: Clock | None,
    pool: str | BlockingPool,
) -> str | None:
    """The section's stripped text, or None (with a warning) when it must be skipped."""

    def skip(why: str) -> None:
        log.warning("plugin prompt section %s (plugin %s) skipped: %s", entry.id, entry.plugin, why)

    if not _SECTION_ID.match(entry.id):
        skip("its id is not a valid section id")
        return None
    if entry.anchor != "after_memory":
        skip(f"unknown anchor {entry.anchor!r}")
        return None
    value: Any = entry.render
    if callable(entry.render):
        try:
            value = await run_blocking(
                _call,
                entry.render,
                info,
                pool=pool,
                deadline=deadline_s if deadline_s and deadline_s > 0 else None,
                clock=clock,
                label=f"prompt section {entry.id}",
            )
        except DeadlineMissed:
            skip(f"it did not answer within {deadline_s:g} s")
            return None
        except BlockingError as exc:
            skip(f"no worker was free ({exc})")
            return None
        except Exception as exc:
            skip(f"it raised {type(exc).__name__}: {exc}")
            return None
    if not isinstance(value, str):
        skip(f"it returned {type(value).__name__}, not text")
        return None
    text = value.strip()
    if not text:
        return None
    if START in text or END in text or _FRAME.search(text):
        skip("it contains the section delimiters")
        return None
    if len(text) > SECTION_MAX_CHARS:
        skip(f"{len(text)} chars is over the {SECTION_MAX_CHARS:,}-char limit")
        return None
    return text


async def render_sections(
    entries: Sequence[PromptSectionEntry],
    info: Mapping[str, Any],
    *,
    deadline_s: float | None = None,
    clock: Clock | None = None,
    pool: str | BlockingPool = "hooks",
) -> list[RenderedSection]:
    """Render ``entries`` in id order within the limits (see the module notes)."""
    kept: list[RenderedSection] = []
    for entry in sorted(entries, key=lambda e: e.id):
        if len(kept) >= MAX_SECTIONS:
            log.warning(
                "plugin prompt section %s (plugin %s) skipped: %d sections is the limit",
                entry.id,
                entry.plugin,
                MAX_SECTIONS,
            )
            continue
        text = await _text_of(entry, info, deadline_s, clock, pool)
        if text is None:
            continue
        candidate = RenderedSection(entry.id, text, entry.plugin)
        if len(format_block([*kept, candidate])) > TOTAL_MAX_CHARS:
            log.warning(
                "plugin prompt section %s (plugin %s) skipped: the sections together would pass "
                "%s chars",
                entry.id,
                entry.plugin,
                f"{TOTAL_MAX_CHARS:,}",
            )
            continue
        kept.append(candidate)
        log.info(
            "plugin prompt section %s from plugin %s added (%d chars)",
            entry.id,
            entry.plugin,
            len(text),
        )
    return kept


# -- restoring ----------------------------------------------------------------------------------


def _parse(block: str) -> list[RenderedSection] | None:
    """The sections inside an exact block, or None when it is not one of ours."""
    head = START + "\n"
    tail = "\n" + END
    if not block.startswith(head) or not block.endswith(tail):
        return None
    pos = len(head)
    stop = len(block) - len(tail)
    found: list[RenderedSection] = []
    while True:
        match = _FRAME.match(block, pos)
        if match is None:
            return None
        size = int(match.group(2))
        begin = match.end()
        if size > SECTION_MAX_CHARS or begin + size > stop:
            return None
        found.append(RenderedSection(match.group(1), block[begin : begin + size], "stored"))
        pos = begin + size
        if pos == stop:
            break
        if not block.startswith("\n\n", pos):
            return None
        pos += 2
    return found if format_block(found) == block else None


def restore_block(prompt: str) -> str | None:
    """The plugin block of a stored prompt; "" when it has none; None when one is there but does
    not frame exactly (never half-trusted)."""
    start = prompt.rfind(START)
    if start < 0:
        return ""
    end = prompt.find(END, start)
    if end < 0:
        return None
    block = prompt[start : end + len(END)]
    at_edge = start == 0 or prompt.endswith("\n\n", 0, start)
    after = prompt[end + len(END) :]
    if not at_edge or (after and not after.startswith("\n\n")):
        return None
    return block if _parse(block) is not None else None


# -- freezing -----------------------------------------------------------------------------------


@dataclass
class FrozenSections:
    """One conversation's plugin block, filled once.

    ``entries`` returns the host's registered sections (``PluginHost.render_prompt_inputs``).
    ``session_ids`` are further ids whose stored prompt may carry the block (the session the agent
    was opened for, when a rotating compaction moved it to a child id).
    """

    entries: Callable[[], Sequence[PromptSectionEntry]]
    prompts: PromptStore | None = None
    session_ids: Sequence[str] = ()
    profile_name: str = ""
    deadline_s: float | None = None
    clock: Clock | None = None
    pool: str | BlockingPool = "hooks"
    block: str | None = None
    source: str = ""

    async def resolve(self, ctx: BuildContext) -> str:
        if self.block is not None:
            return self.block
        for sid in dict.fromkeys([ctx.session_id, *self.session_ids]):
            if not sid or self.prompts is None:
                continue
            try:
                stored = await self.prompts.load(sid)
            except Exception as exc:
                log.warning("could not read the stored prompt of %s (%s)", sid, exc)
                continue
            if stored.state != "present":
                continue
            restored = restore_block(stored.text)
            if restored is None:
                log.warning(
                    "the plugin sections in the stored prompt of %s are damaged; the conversation "
                    "goes on without them",
                    sid,
                )
                restored = ""
            self.block, self.source = restored, f"stored:{sid}"
            return self.block
        rendered = await render_sections(
            self.entries(),
            session_info(ctx, self.profile_name),
            deadline_s=self.deadline_s,
            clock=self.clock,
            pool=self.pool,
        )
        self.block, self.source = format_block(rendered), "plugins"
        return self.block


@dataclass
class PluginSectionsBuilder:
    """A prompt ``Builder`` that adds the frozen plugin block after the memory blocks."""

    inner: Builder
    frozen: FrozenSections

    async def build(self, ctx: BuildContext) -> PromptParts:
        block = await self.frozen.resolve(ctx)
        return await self.inner.build(replace(ctx, plugin_sections=block))
