# Task: Prompt lifecycle and caching invariant (P04-07) Phase: 04 Owner: wave-5 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/conversation_loop.py`
(the restore-or-build helper, the runtime-match check, the persist helper), `agent/turn_context.py`
(the one-shot note pop helper, the consume helpers for gateway / surface-switch / skills-index
notes, the merge into the user message and the multimodal append), `agent/system_prompt.py` (the
invalidate function), `agent/surface_switch.py` (runtime boundary split, identity/host line
readers, the surface-switch staging rule). Reference tests skimmed for edge cases:
`tests/agent/test_system_prompt_restore.py` (present row reused verbatim, stale identity rebuilds,
no history skips the store, no store skips persistence, write failure warns, switch stages a note
once, returning to the prompt's own surface is announced). Closed before writing.
Research: `docs/plan/research/01-agent-core.md` §3.3–3.4, trap R01.1. Evidence: none.

What it does (own words): the prompt is built once per conversation and then reused byte for byte.
`PromptLifecycle.restore_or_build(ctx, history=...)`:
1. a prompt already held by this lifecycle is returned as is (no store read, no build);
2. with history, the stored prompt is read from the session row; the row state is classified
   `missing | null | empty | present` and logged; a store error logs WARNING and counts as missing;
3. a present prompt is reused when it still matches the runtime: the `Model:`/`Provider:` lines
   of the trailer paragraph just before the runtime block equal the live values (a stored empty
   line is not checked), a `Session ID:` line (only when the session id is shown) names this
   session, and the `Working directory:` line in the first paragraph of the runtime block equals
   the live cwd. `Platform:` is not an identity field;
4. otherwise the prompt is built (P04-05) and persisted; a persist failure logs WARNING (the next
   process would otherwise miss the cache silently); null/empty rows with history also warn.
5. after a restore or rebuild, if the live platform differs from what the model was last told
   (the newest surface-switch note in the recent history, else the prompt's `Platform:` line), a
   surface-switch note is staged on the note channel — with the new surface's hint when the prompt
   itself is out of date.

`invalidate(reason="compaction")` is the only full rebuild trigger (P08 calls it); any other
reason raises. `rebind_identity(model, provider)` is the route-binding path (P04-23): it rewrites
only the `Model:`/`Provider:` lines of the held prompt and persists it.

**Note channel** (`core/notes.py`): `NoteChannel` holds one-shot notes — must-deliver notes
(several, in arrival order), the surface-switch note and the skills-index delta (one slot each, a
newer one replaces the older). `pop()` returns them joined by blank lines in that fixed order and
clears them. `attach_notes(message, notes)` returns a copy of the user message whose `api_content`
is the text sent (existing sidecar or content, then the notes); multimodal list content gets a text
part instead. Notes never touch the prompt.

Inputs → outputs: `BuildContext`, recent history rows, `PromptStore` (async `load`/`save`) →
prompt text; side effects: store write, staged notes, log lines.

Files: `src/indus/core/prompt/lifecycle.py`, `src/indus/core/notes.py`,
`tests/unit/core/prompt/test_lifecycle.py`, `tests/unit/core/test_notes.py`; shared:
`src/indus/state/sessions.py` (`set_system_prompt_in` / `set_system_prompt`: `create` only fills
gaps, so a rebuilt prompt had no write path).

Edge cases / failure modes:
- no history → no store read, build fresh, persist.
- no store (ephemeral sessions) → build, nothing persisted, no warning.
- stored prompt without a runtime block (older layout) → cwd not checked; identity lines read from
  the last paragraph.
- `Model:` text inside project files or memory does not count: only the trailer paragraph is read.
- a host description that quotes our runtime heading is defused by the builder.
- note staged twice before a turn: one surface note (newest), must-deliver notes all kept.
- a surface note already in history for the live platform → nothing restaged.

Our design differences / decisions:
- `restore_or_build` takes the build context plus the history instead of an agent object.
- Store access goes through a small async `PromptStore` protocol; `SessionPrompts` adapts the P02
  store (reads off the loop, writes through `awrite`).
- No pinned tool array restore (P04-10/P04-22 own the tool array), no bot-chat capability epoch,
  no static-prefix reconstruction for cache markers (P08 prompt caching), no plugin-section
  restore, no session-start hook.

Tests we will write: resume with the same runtime reuses stored bytes without a build; cwd change,
model change, provider change rebuild and persist; session id mismatch rebuilds only when shown;
platform change keeps bytes and stages a note once; store read error → WARNING + rebuild; write
error → WARNING; null row with history → WARNING; no history → no read; invalidate only for
compaction; rebind rewrites only identity lines; notes appended to `api_content` only; prompt bytes
identical across 50 turns with notes; real P02 store round trip.

## Decisions for the lead
- `sessions.set_system_prompt[_in]` added to P02's sessions module (shared): it stores the text by
  hash like `create` and points the row at it; it does not create a missing row (the prologue,
  P04-16, creates the row).

Compare checklist (filled after the rewrite; reopened the restore helper, runtime match, note
helpers, surface switch staging, invalidate):
- [x] reuse when runtime matches, verbatim (test:
  `tests/unit/core/prompt/test_lifecycle.py::test_resume_reuses_stored_bytes_without_building`)
- [x] stale identity or cwd → rebuild + persist (test: `::test_runtime_mismatch_rebuilds`)
- [x] row states logged; store errors WARNING (tests: `::test_store_read_error_warns_and_rebuilds`,
  `::test_null_row_with_history_warns`, `::test_persist_failure_warns`)
- [x] no history → no read (test: `::test_no_history_skips_store_read`)
- [x] surface switch keeps bytes, note once (test: `::test_surface_switch_keeps_bytes_and_notes_once`)
- [x] notes ride the user message only (test: `tests/unit/core/test_notes.py`)
- [x] invalidate refuses every reason but compaction; route binding rewrites identity lines only
  (tests: `::test_invalidate_only_for_compaction`, `::test_rebind_identity_rewrites_only_identity_lines`)
- [x] project text quoting `Model:` / `Working directory:` is never read as identity (test:
  `::test_decoys_in_project_text_are_not_identity`)
- [x] prompt bytes identical across 50 turns with notes and in a new process (test:
  `::test_prompt_bytes_unchanged_after_fifty_turns_with_notes`)
- Deliberate differences: older-layout prompts (no runtime block) are not parsed for a host
  paragraph — we have no such stored prompts; the conversation date on a rebuild comes from the
  row's `started_at`; a missing session row is never created by the lifecycle.
- Not taken: tool-array pinning, bot-chat capability epoch, static-prefix reconstruction for
  cache markers (P08), plugin-section restore, session-start hook, credits seed, memory reload on
  invalidate (P09 adds it to the compaction path).

## Status

Done (2026-10-18). Shared edit: `src/indus/state/sessions.py` (`set_system_prompt[_in]`).
Cross-slice names: none. Notes are plain message mappings, so P04-03's `TranscriptRow` can adopt
`attach_notes` without a stand-in.
