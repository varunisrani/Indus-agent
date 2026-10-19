# Task: Skills index in the prompt and mid-session delta (P09-07) Phase: 09 Owner: P09 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/prompt_builder.py`
(`build_skills_system_prompt` and the helpers it calls: grouping, clipping, names-only categories),
`agent/skills_index_delta.py` (all). Closed before writing.
Evidence: none.

What it does (own words): the system prompt carries a compact "Skills" block so the model knows
which procedures exist without loading them. Rows are grouped by category, each `- name:
description` with the description clipped to 60 characters and a visible `…` when clipped (the
model learns to view the skill for the rest). A coding posture can demote whole categories to one
names-only line; nothing is ever dropped. Same-tier duplicates that cannot load by bare name get a
one-line rename note instead of rows. The block is built once with the prompt and stays frozen
(R07.29): a skill created, deleted or changed mid-session never rebuilds the prompt. Instead, each
turn the index the prompt carries is compared with the index that would be built now; a change is
announced once as a note on the next user message (the P04 note channel); the next compaction
rebuild picks the change up in the prompt itself.

Inputs → outputs: catalog + visibility (tools, toolsets, surface) + demoted categories → index
text; `SkillsIndexBuilder(inner)` fills `BuildContext.skills_index` on every (re)build;
`IndexDeltaTracker.note(prompt_text, current_index)` → note text or None;
`stage_index_delta(notes, tracker, prompt, current)` stages it on the note channel.

Files: `src/indus/learning/skills/prompt_index.py`, `tests/unit/learning/skills/test_prompt_index.py`.
`src/indus/core/prompt/builder.py` already reserves `skills_index` in the volatile tier, so no
change is needed there.

Edge cases / failure modes:
- no visible skills or no `skill_view` tool → empty block (the builder drops it);
- description with newlines or runs of spaces → collapsed to one line before clipping;
- exactly 60 characters → no mark; 61 → 59 characters + `…`;
- plugin skills (`plugin:skill`) rows parse back correctly (split on `": "`, not `":"`);
- a prompt rebuilt by compaction already lists the change → no note (and the tracker resets);
- a skill added then removed before any compaction → a second note says the prompt list is
  accurate again.

Our design differences / decisions:
- block delimited by `<skills_index>` … `</skills_index>`, header and guidance in our own words;
- one note per change in the difference; the note states the whole current difference;
- the delta tracker lives on the session; a resumed session starts with an empty tracker, so its
  first turn may repeat a note already in the transcript (cheap, and never wrong);
- the builder wrapper is a `Builder` (P04 protocol), so the agent needs no new seam: the host passes
  `AgentOptions.builder = SkillsIndexBuilder(PromptBuilder(), …)`.

Tests we will write:
- golden index for a fixture tree (categories, clipping mark, names-only demotion, duplicate note);
- 60/61-char clipping; empty when the view tool is absent;
- builder wrapper: prompt bytes unchanged after a mid-session add; next user message has exactly
  one note, the following none; removal note; compaction rebuild lists the new skill.

## Decisions for the lead
- The per-turn call (`stage_index_delta` before each turn) and the builder wrapper are not wired into
  `SessionService` here (that file is shared with slice A's memory block); it is a two-line seam:
  pass `SkillsIndexBuilder` as `AgentOptions.builder` and call `stage_index_delta` with the
  agent's `PromptLifecycle.notes` and `.prompt` before `run_turn`.

Compare checklist (filled after the rewrite; reopened both files):
- [x] 60-char clip with a visible mark (test: `tests/unit/learning/skills/test_prompt_index.py::test_clip_edges`)
- [x] names-only demotion keeps every name (test: `::test_golden_index`)
- [x] delta note once per change, prompt bytes unchanged (test: `::test_mid_session_add_and_remove`)
- [x] rebuild includes the new skill and sends no stale note (test: `::test_compaction_rebuild_lists_new_skill`)
- Deliberate differences: the reference reads the last delta back from the transcript for agents
  rebuilt per turn; our sessions keep one agent, so the tracker is in memory only.
- Not taken: safe-worker suppression (no safe worker yet), project-skill trust rows (P13).

## Status

Done (2026-10-19). Shared: none.

## Integration (lead, P09 wave 1 landing, 2026-10-19)

Integration: wired. `learning.host.wire_session` builds `SkillsIndexBuilder` (with the auto-load /
`-s` block as an extra block) and a prologue note source; the core gained
`AgentOptions.note_sources` → `SessionRuntime.note_sources`, run after the prompt is restored or
built and before the notes are popped (`tests/unit/learning/test_host.py`). D-16.144.
