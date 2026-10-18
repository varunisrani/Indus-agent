# Task: Transcript model + alternation repair (P04-03) Phase: 04 Owner: wave-5 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/agent_runtime_helpers.py`
(`repair_message_sequence`, `repair_message_sequence_with_cursor`, the four passes
`_merge_consecutive_assistants`, `_drop_stray_tool_results`, `_prune_unanswered_tool_calls`,
`_merge_consecutive_users`, the sentinel decode step and `_merge_assistant_into`, ~380–810),
`agent/AGENTS.md` (alternation and test guidance near the end), `agent/message_sanitization.py`
(`tool_call_id_variants`, `coalesce_tool_call_id`, 509–540), test names only of
`tests/agent/test_message_sequence_repair.py`. Spec: `docs/plan/research/01-agent-core.md` §3.6,
§3.7, trap R01.2. Closed before writing.
Evidence: none.

What it does (own words): the live history of a session is a list of transcript rows. Before a
model call the loop runs `repair_transcript` on it so the history alternates the way every
provider accepts: no two assistant rows in a row, no tool result that does not answer a call of
the assistant row right before its run of results, no tool call left without a result in the run
directly after it, and no two plain user rows in a row. Rows typed `display_kind="steer"` are the
one deliberate mid-loop user row: nothing is ever folded into them and they are never folded into
another row. Content stored with the state codec (`PARTS_PREFIX`) is decoded first so a
multimodal row is merged as parts, never as a prefixed string. Repair returns the new list plus a
report that says how many rows each pass changed and where the persistence cursor ("rows already
on disk") moved, so the flush after the turn neither skips nor re-appends rows.

Inputs → outputs: `repair_transcript(rows, *, flushed=None)` → `(rows, RepairReport)`.
`RepairReport`: per-pass counts, `total`, `flushed` (new cursor, or `None` when none was given),
`rewritten` (output indices inside the flushed prefix whose durable row changed), `sources`
(for each output row the input indices it stands for), `dropped` (input indices removed).

Files: `src/indus/core/transcript.py`, `src/indus/core/repair.py`,
`tests/unit/core/test_repair.py`.

Edge cases / failure modes:
- empty list → no work, empty report.
- assistant + assistant: tool calls united in order, text joined with one newline (strings), part
  lists concatenated; the earlier row's reasoning wins, the later one's is used only when the
  earlier has none; `reasoning_details` appended in arrival order, never merged into each other;
  a changed content drops the stale `api_content` sidecar; `tool_call_uids` united.
- tool rows at the very start, after a user row, with an unknown id, a second result for an id
  already answered in the same run, or no id at all → dropped.
- assistant with calls whose results are missing from the run right after it → those calls are
  pruned; a row left with no text, reasoning or calls is dropped.
- user + user: strings joined with a blank line, one empty side keeps the other; string + parts →
  parts with a text part; a damaged prefixed payload (decode failed, still a string with the
  prefix) is never merged; steer on either side → never merged.
- a pass can create a new neighbour pair for an earlier pass (an orphan result between two
  assistant rows is dropped, leaving the two adjacent) → the passes run again until nothing
  changes (bounded by the row count).
- the input list and its rows are never mutated; changed rows are copies.

Our design differences / decisions:
- Our names: `TranscriptRow`, `repair_transcript`, `RepairReport`, `DISPLAY_STEER`.
- Rows are a dataclass, not dicts; `from_message` / `to_message` / `from_store` / `to_store`
  convert at the edges. The store column is `tool_name`; the row field is `name`.
- Fixpoint loop over the passes (theirs runs each pass once, which leaves the hole above).
- Tool results with no id are dropped here too (theirs leaves them for the send-time sanitizer).
- Assistant part lists are concatenated rather than leaving the later text behind; user string +
  parts merge into parts (theirs leaves multimodal user rows unmerged). No user text is lost.
- Cursor: an output row counts as flushed when its first source row was flushed; flushed output
  rows whose bytes changed are listed in `rewritten` for the persistence layer to replace in
  place (theirs pops a marker and forces a full rescan).
- Not taken: Responses-API interim rows and verification-candidate supersede (no such rows yet;
  P08 adds Responses), alias-group id variants (only `id` / `call_id` exist in our shape),
  the absorbed-row-id bookkeeping for id-less reloads (our rows carry `row_id` / uids, and
  `sources` gives the full mapping).

Tests we will write: one table test per pass; steer protection both sides; codec decode before
merge; damaged payload not merged; fixpoint case; cursor and `rewritten` cases; input not mutated;
seeded-random property test (hypothesis is not installed, so a fixed-seed generator makes 400
random sequences): output alternates (no same-role neighbours except tool/tool and a steer
neighbour), every remaining call has exactly one result in the run after it, every result answers
a call, and every non-empty user text survives as a substring of some user row.

## Decisions for the lead
- The state store has no `display_kind` or scaffold column: `to_store` does not write them, so a
  reloaded steer row is a plain user row unless P04-19 recognises its marker text. Steer typing on
  reload is left to P04-19 (smallest change: a store column later, or the marker prefix).

Cross-slice names used: none (only `indus.state.messages` and P03 types, both landed).

Compare checklist (filled after the rewrite; reopened `agent/agent_runtime_helpers.py` 380–810):
- [x] pass order: decode → merge assistants → drop strays → prune unanswered → merge users
  (test: `tests/unit/core/test_repair.py::test_passes_repeat_until_nothing_changes`)
- [x] merge assistants unions calls in order, joins text with one newline, drops a stale empty
  `tool_calls`, keeps the earlier reasoning and carries a later one only when missing, drops the
  sidecar only when content changed (tests: `test_consecutive_assistants_merge_calls_and_text`,
  `test_assistant_merge_keeps_sidecar_when_content_unchanged`,
  `test_assistant_merge_takes_later_reasoning_only_when_missing`)
- [x] stray results consumed once per run; assistant opens a run, any other row closes it
  (test: `test_stray_results_are_dropped`, `test_two_calls_sharing_an_id_keep_two_results`)
- [x] prune keeps answered calls, drops a payload-less row (tests: `test_unanswered_calls_are_pruned`,
  `test_payload_less_row_left_without_calls_is_dropped`, `test_result_after_a_user_row_does_not_answer`)
- [x] user merge: blank-line join, one empty side keeps the other, steer never folded into,
  undecodable payload never merged (tests: `test_user_rows_merge_without_losing_text`,
  `test_nothing_is_folded_into_a_steer_row`, `test_damaged_parts_payload_is_never_merged`)
- [x] decode first so parts merge as parts (test: `test_stored_parts_are_decoded_before_merging`)
- [x] cursor keeps flushed survivors (tests: `test_cursor_counts_flushed_survivors`,
  `test_cursor_after_drops_ahead_of_it`)
- [x] gap found: their single run of the passes leaves two assistant rows adjacent when an orphan
  result between them is dropped — fixed by the fixpoint loop, pinned by
  `test_passes_repeat_until_nothing_changes` and the property test
- [x] gap found: their pass 2 matches by id set, so two calls sharing an id with one result keep
  both calls — fixed with per-id counts, pinned by `test_two_calls_sharing_an_id_keep_two_results`
- Deliberate differences: their user merge also drops a model-switch marker's display kind and
  adopts the plain row's id; ours clears a differing display kind and keeps the first row's
  identity (`sources` names both). Theirs refuses to merge multimodal user rows; ours merges parts.
  Theirs pops a persisted marker and rescans; ours reports `rewritten` indices. Scaffold and
  durable user rows are never merged (keeps nudges out of the durable record, R01.3).
- Not taken: Responses interim and verification-candidate exemptions, compression summary-carrier
  exemption (no such rows before P05/P08), alias id variants, absorbed-row-id bookkeeping.

## Status

Done (2026-10-18). Files: `src/indus/core/transcript.py`, `src/indus/core/repair.py`,
`tests/unit/core/test_repair.py` (29 tests incl. a 400-case seeded-random property test;
hypothesis is not installed). Handed on: steer typing on reload (P04-19), applying `rewritten`
and `flushed` in the persistence flush (P04-16).
