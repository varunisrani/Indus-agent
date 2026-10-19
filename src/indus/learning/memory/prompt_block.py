# pyright: strict
"""The frozen memory block of the system prompt (P09-04).

The prompt shows both stores as they were when the conversation's prompt was first built. Writes
during the session reach the files and the tool results, never the current prompt (R07.1): the
provider's prompt cache keeps hitting, and the model is told in each write result that the change
is saved.

``FrozenMemory`` holds the two rendered blocks for one conversation and fills them once:

1. from the conversation's stored prompt when there is one (a resumed session, or a rebuild after
   compaction): the blocks are cut out of that text verbatim (``extract``), so a resumed session
   gets back exactly the bytes it had even when the files changed in between;
2. otherwise from the files, rendered now (``render``).

``MemoryPromptBuilder`` wraps the P04 prompt builder: it puts the held blocks and this profile's
guidance text into the ``BuildContext`` slots the builder reserves (``memory_snapshot``,
``profile_snapshot``, ``memory_guidance``). The prompt lifecycle persists the whole prompt, which
persists the blocks with it.

An entry that matches the strict threat scan when the block is rendered appears in the block only as
a ``[blocked: …]`` line asking the agent to remove it; the file keeps the original, so the user can
still see and delete it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from indus.core.prompt.builder import BuildContext, PromptParts
from indus.core.prompt.lifecycle import Builder, PromptStore
from indus.foundation.blocking import run_blocking
from indus.learning.memory.store import (
    BLOCK_TAGS,
    FILE_NAMES,
    MemoryStore,
    Snapshot,
    Target,
)
from indus.safety import threat_scan

__all__ = [
    "GUIDANCE",
    "FrozenMemory",
    "MemoryPromptBuilder",
    "extract",
    "guidance_for",
    "placeholder",
    "render",
]

log = logging.getLogger(__name__)

_GUIDANCE_HEAD = "# Memory\n"
_GUIDANCE_TAIL = (
    " Keep task progress, one-off details and anything the project files already say out of "
    "memory; steps for doing a kind of task belong in a skill. The saved entries shown in this "
    "prompt are a copy taken when the conversation started: a change you save now is on disk "
    "at once but shows here only from the next session."
)
GUIDANCE: dict[tuple[Target, ...], str] = {
    ("memory", "user"): _GUIDANCE_HEAD
    + "The memory tool keeps short facts that matter in every future session. Save to 'user' "
    "what you learn about the user (who they are, preferences, how they like to work) and to "
    "'memory' stable facts about their environment, conventions and corrections they gave you."
    + _GUIDANCE_TAIL,
    ("memory",): _GUIDANCE_HEAD
    + "The memory tool keeps your own short notes for every future session: stable facts about "
    "the environment, conventions and corrections. Only target 'memory' is available; facts "
    "about the user are not kept here." + _GUIDANCE_TAIL,
    ("user",): _GUIDANCE_HEAD
    + "The memory tool keeps a short profile of the user for every future session: who they "
    "are, their preferences and how they like to work. Only target 'user' is available; do not "
    "store environment notes there." + _GUIDANCE_TAIL,
}


def guidance_for(live: Sequence[Target]) -> str:
    """The guidance text for the stores that are on ("" when none is)."""
    return GUIDANCE.get(tuple(live), "")


def placeholder(target: Target, findings: Sequence[str]) -> str:
    kinds = ", ".join(sorted({f.split(":", 1)[0] for f in findings}))
    return (
        f"[blocked: this saved entry in {FILE_NAMES[target]} matched a blocked pattern ({kinds}) "
        f"and is hidden here. Remove it with the memory tool (action remove, target "
        f"'{target}'), or ask the user to delete it.]"
    )


def _shown(target: Target, entry: str) -> str:
    if entry.startswith("[blocked:"):
        return entry
    findings = threat_scan.scan(entry, scope="memory")
    if not findings:
        return entry
    log.warning(
        "a saved entry in %s matched %s; it is hidden in the prompt",
        FILE_NAMES[target],
        ", ".join(findings),
    )
    return placeholder(target, findings)


def render(store: MemoryStore) -> Snapshot:
    """Both blocks from the files now, threat hits swapped for placeholders."""
    return store.render_snapshot(_shown)


def _cut(prompt: str, target: Target) -> str:
    tag = BLOCK_TAGS[target]
    opening = f"<{tag} usage="
    start = prompt.rfind("\n" + opening)
    start = start + 1 if start >= 0 else (0 if prompt.startswith(opening) else -1)
    if start < 0:
        return ""
    closing = f"\n</{tag}>"
    end = prompt.find(closing, start)
    if end < 0:
        return ""
    return prompt[start : end + len(closing)]


def extract(prompt: str) -> Snapshot:
    """The memory blocks inside a stored prompt ("" for a block it does not carry)."""
    return Snapshot(memory=_cut(prompt, "memory"), user=_cut(prompt, "user"))


@dataclass
class FrozenMemory:
    """One conversation's memory blocks, filled once (see the module notes).

    ``session_ids`` are further ids whose stored prompt may carry the blocks (the session the
    agent was opened for, when a rotating compaction has since moved it to a child id).
    """

    store: MemoryStore
    prompts: PromptStore | None = None
    session_ids: Sequence[str] = ()
    snapshot: Snapshot | None = None
    source: str = ""

    async def resolve(self, session_id: str) -> Snapshot:
        if self.snapshot is not None:
            return self.snapshot
        for sid in dict.fromkeys([session_id, *self.session_ids]):
            if not sid or self.prompts is None:
                continue
            try:
                stored = await self.prompts.load(sid)
            except Exception as exc:
                log.warning("could not read the stored prompt of %s (%s)", sid, exc)
                continue
            if stored.state == "present":
                self.snapshot, self.source = extract(stored.text), f"stored:{sid}"
                return self.snapshot
        self.snapshot = await run_blocking(render, self.store, label="memory snapshot")
        self.source = "files"
        return self.snapshot


@dataclass
class MemoryPromptBuilder:
    """A prompt ``Builder`` that adds the frozen memory blocks and the memory guidance."""

    inner: Builder
    frozen: FrozenMemory

    async def build(self, ctx: BuildContext) -> PromptParts:
        snapshot = await self.frozen.resolve(ctx.session_id)
        filled = replace(
            ctx,
            memory_snapshot=snapshot.memory,
            profile_snapshot=snapshot.user,
            memory_guidance=guidance_for(self.frozen.store.live_targets),
        )
        return await self.inner.build(filled)
