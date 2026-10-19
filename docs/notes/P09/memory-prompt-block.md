# Task: Frozen memory block in the prompt (P09-04) Phase: 09 Owner: P09 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/agent_init.py` (only
`_init_memory`), `agent/prompt_builder.py` (memory block call sites and the guidance builder).
Closed before writing.
Evidence: none.

What it does (own words): the system prompt carries both stores as they were when the session's
prompt was first built — never re-read mid-session, so a memory write cannot change the current
prompt bytes (R07.1, provider prefix cache). `FrozenMemory` holds the two rendered blocks for one
conversation. It is filled once: from the session's stored prompt when one exists (resume after a
restart, or a rebuild after compaction — the block is cut out of the stored text verbatim), else
from the files on disk at that moment. A compression rebuild therefore reuses the held block, and a
resumed session gets back exactly the bytes it had even when the files changed in between; a new
session reads the files and so shows writes made earlier. An entry that matches the strict threat
scan when the block is rendered is replaced in the block (only there) by a `[blocked: …]` line
that asks the agent to remove it; `MemoryStore.entries()` still returns the original so the user can
see and delete it. Guidance text for the tool is ours: a general version, narrower versions when only
one store is on, and none when both are off (the tool is gone then too).

The builder seam: `MemoryPromptBuilder(inner, frozen)` implements the prompt `Builder` protocol and
fills `BuildContext.memory_snapshot`, `profile_snapshot` and `memory_guidance` before calling the
inner `PromptBuilder` (P04-05 already reserves those volatile slots). The lifecycle (P04-06) still
decides when to build and stores the whole prompt, which is how the block is persisted with it.

Inputs → outputs: the stores, the stored prompt text (if any), the session ids → the two blocks and
the guidance text in the prompt.

Files: `src/indus/learning/memory/prompt_block.py`,
`tests/unit/learning/memory/test_prompt_block.py`. Shared: `src/indus/core/prompt/builder.py`
(`BuildContext.memory_guidance`, used by `_tool_guidance`).

Edge cases / failure modes:
- stored prompt present but without memory blocks (memory was empty or off at the start) → the
  blocks stay empty for the whole session, even if files gained entries;
- stored prompt unreadable / store error → treated as missing (render from disk), WARNING logged;
- rotating compaction moves to a child session id: the held block is reused; on a fresh process the
  child has a stored prompt only after its first build, so the original id is tried as well;
- an entry text that contains a block tag line → defused when rendering (`> ` prefix), so cutting
  the block out of the prompt stays exact;
- both stores off → no block, no guidance.

Our design differences / decisions:
- block format is ours: `<saved-notes usage="…">…</saved-notes>` and
  `<user-profile usage="…">…</user-profile>`; the tags double as the markers `extract` cuts on;
- the frozen block is recovered from the stored prompt instead of a second stored copy (the prompt
  is already persisted per session; one source of truth);
- tier position: the builder's volatile tier keeps its P04 order (skills index, memory, user
  profile, profile line, date, runtime) — the memory block comes after the persona and the
  context tier; the phase text "before skills index" is not applied (see decisions).

Tests we will write:
- golden: write during turn 1, prompt bytes of turn 2 identical; a compaction rebuild keeps the
  same block; a new session shows the write;
- resume after a restart (new lifecycle, new frozen holder) restores identical block bytes even after
  the files changed;
- threat-hit entry rendered as a placeholder while `entries()` returns the original;
- guidance: both on, one off (narrow text), both off (no guidance, no block).

## Decisions for the lead
- Tier order: P04-05's builder puts the skills index before the memory block in the volatile tier.
  The phase row asks for memory before the skills index; the two orders differ only in which
  volatile block comes first. Kept P04's order (no golden churn, slice B owns the index slot); swap
  the two entries in `builder._render` if the phase order is wanted.

Compare checklist (filled after the rewrite; reopened `agent/agent_init.py` `_init_memory`,
`agent/prompt_builder.py` memory call sites):
- [x] snapshot taken once per session, writes never change it (test: `test_prompt_block.py::test_write_during_turn_one_keeps_prompt_bytes`)
- [x] placeholder only in the snapshot (test: `test_prompt_block.py::test_threat_entry_is_a_placeholder_in_the_block_only`)
- [x] narrower guidance when one store is off (test: `test_prompt_block.py::test_guidance_variants`)
- Deliberate differences: the block is recovered from the stored prompt on resume (the reference
  re-reads disk when an agent is rebuilt and relies on the stored prompt for the cached prefix).
- Not taken: external memory provider blocks (P13).

## Status

Done (2026-10-19). Shared: `core/prompt/builder.py` gained `BuildContext.memory_guidance`.

## Integration (lead, P09 wave 1 landing, 2026-10-19)

Compare redone at integration (lead reopened `agent/agent_init.py` `_init_memory` and the
memory call sites of the prompt assembly, then closed them): snapshot once per session, placeholder
only in the block, narrower guidance with one store, no block when both stores are off: all match.
Lead decision: the phase order is applied — the volatile tier is now memory notes, user profile,
skills index (`builder.py`, `test_builder.py::test_volatile_tier_order`; D-16.139). Builder stack in
`learning.host`: `MemoryPromptBuilder` over `SkillsIndexBuilder` over `PromptBuilder` (only fills
slots; the order in the text is the builder's), wired by `cli/runtime.with_learning`.
