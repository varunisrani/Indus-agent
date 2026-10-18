# Task: todo_list + clarify (P05-24) Phase: 05 Owner: wave-1 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/todo_tool.py` (whole: store,
merge, validation, parents, ordering, render, schema), `tools/clarify_tool.py` (whole: question
normalising, recommended label, answer cleaning, statuses, schema), `run_agent.py`
(`_hydrate_todo_store`, `_latest_todo_response`: pairing and size rule). Test names of
`tests/tools/test_todo_tool.py`, `test_todo_nested.py`, `test_todo_tool_type_coercion.py`,
`test_clarify_tool.py`. Closed before writing.
Evidence: none.

What it does (own words): `todo_list` keeps one task list per agent session. With no `todos` it
reads; with `todos` it replaces the list (`merge` false) or merges by id (`merge` true: fields
given update the item, unknown ids are appended). Each item is `{id, content, status, parent?}`;
status is pending, in_progress, completed or cancelled. Every call returns the whole list, a
revision number that grows only when the list really changed, and counts per status. The list is
process state, so on resume a hydrator rebuilds it from the newest todo result in the stored
transcript — only a result that answers an earlier assistant `todo_list` call counts, so a forged
tool row cannot seed it. `render()` gives P08 the text to re-inject after compression (active
items, with their parents kept for context).

`clarify` asks the person 1–5 questions in one server request (P04 broker port) and returns one
response per question in order — answered, skipped or unanswered — and how the wait ended:
submitted, cancelled, timed_out or undelivered. The first of two or more choices is shown with a
"(recommended)" marker that never leaks back into the answers.

Inputs → outputs: `todo_list {todos?: [{id, content, status, parent?}], merge?: bool}` → JSON
`{todos, revision, summary: {total, pending, in_progress, completed, cancelled}}`;
`TodoList.write/read/restore/render/snapshot`; `todo_wiring()` → (inline executor, hydrator) for
the Agent. `clarify {questions: [{question, choices?, multi_select?}]}` → JSON `{responses:
[{question, choices_offered, status, user_response}], outcome, notice?}`. Timeout from
`agent.clarify_timeout` (3,600 s; existing key).

Files: `src/indus/tools/builtin/todo.py`, `src/indus/tools/builtin/clarify.py`,
`tests/unit/tools/builtin/test_todo.py`, `tests/unit/tools/builtin/test_clarify.py`; shared:
`src/indus/tools/builtin/__init__.py` (module list), `src/indus/core/tool_round.py` (the call
context offers the session broker as `broker`), `src/indus/core/agent.py` (installs the todo
executor and hydrator when `todo_list` is selected and the caller gave none).

Edge cases / failure modes:
- todo: `todos` sent as JSON text → parsed; not a list → error; non-object items → placeholder
  item; empty/blank content → error and the list is unchanged (no partial write); content over
  4,000 chars → cut with a marker; more than 256 items → the first 256 kept; duplicate ids → the
  last one wins in its position; unknown status → pending; a parent equal to the item, missing,
  or forming a cycle is dropped; an in_progress item is moved ahead of earlier pending ones in a
  flat list (nested lists keep their order); a merge entry without id is ignored; merge with
  `parent: ""` clears it; `merge` as text "true" accepted.
- hydration: newest paired result wins; results over 512,000 chars skipped; unparsable skipped;
  an empty list is an authoritative clear; revision restored, not bumped.
- clarify: questions not a list / empty / over 5 → error; blank question → error; choices not
  strings → error; a choice over 8,000 chars → error (handler, not schema); blank choices dropped,
  all blank → error; more than 4 → first 4; multi_select without choices → single; one choice is
  not marked; answers in any case of the marker are stripped; multi-select answers are lists.
- broker outcomes: answer → submitted (empty answer list = skipped); withdrawn timed_out →
  timed_out; withdrawn for interrupt/turn end/session close/elsewhere → cancelled; client cannot
  answer clarify → undelivered; no broker in this context → undelivered with a notice.

Our design differences / decisions:
- Todo is an inline tool: its executor gets the session's list through a per-agent wiring object
  (the registry entry only carries the schema; dispatched without the loop it errors).
- Clarify questions get ids `q0..q4` on the wire; the answer keys follow them.
- Partial answers on timeout: the P04 port has no channel for them (an ask ends with the full
  answer or a withdrawal), so a timeout reports every question `unanswered`.
- Marker text is ours: `(recommended)`.

Tests we will write: replace vs merge; limits; validation; parents; ordering; revision; render;
hydration from a stored transcript (paired, forged, oversized, newest wins); executor end-to-end
with coercion. Clarify: normalising table; fake broker answered / skipped / timed out / withdrawn
/ unsupported / missing; marker added and stripped; multi-select lists; timeout read from config.

## Decisions for the lead
- `tool_round` now offers the broker to handlers as the `broker` keyword (P05-09 approvals need
  the same).
- The Agent installs the todo wiring itself when `todo_list` is in the selected tools and
  `AgentOptions.inline` has no executor for it.
- Spec says `agent.clarify_timeout_s`; the schema already has `agent.clarify_timeout` (3,600) — used as is.

Compare checklist (filled after the rewrite; reopened `tools/todo_tool.py`, `tools/clarify_tool.py`, `run_agent.py`):
- [x] replace vs merge, read mode, summary (tests: `tests/unit/tools/builtin/test_todo.py::test_replace_and_read`, `::test_merge_updates_given_fields_and_appends`)
- [x] revision only on change (test: `::test_revision_moves_only_on_change`)
- [x] duplicates, active-first ordering, nested lists untouched (tests: `::test_duplicate_ids_keep_the_last_entry_in_its_place`, `::test_active_item_moves_ahead_of_earlier_pending_ones`)
- [x] empty content refuses the whole write, list unchanged (test: `::test_items_without_content_refuse_the_whole_write`)
- [x] limits and invalid items (tests: `::test_limits`, `::test_unreadable_items_and_missing_ids_are_normalised`)
- [x] parents: self, dangling, cycles, merge set/clear (test: `::test_parents`)
- [x] render keeps open subtasks' parents, drops finished subtrees (test: `::test_render_shows_open_work_with_context`)
- [x] hydration: paired only, newest wins, oversized/unparsable skipped, revision rule (tests: `::test_hydrate_*`, `::test_executor_result_round_trips_through_hydration`)
- [x] hydration from a stored session through the Agent and the prologue (test: `::test_the_list_survives_a_resume_through_the_store`)
- [x] clarify normalising and limits, handler-enforced choice length (tests: `tests/unit/tools/builtin/test_clarify.py::test_bad_questions_are_refused`, `::test_question_shapes_are_normalised`, `::test_schema_leaves_choice_length_to_the_handler`)
- [x] recommended marker added once, stripped from answers (test: `::test_recommended_marker_rules`)
- [x] statuses and outcomes over the port (tests: `::test_answered_and_skipped_in_question_order`, `::test_withdrawn_questions_are_unanswered`, `::test_undelivered_when_nobody_can_answer`)
- [x] timeout from config (test: `::test_timeout_comes_from_config`)
- Deliberate differences: placeholder ids `item-N` instead of a shared `?`; a self-parent is also
  dropped on merge; clarify with no broker returns an `undelivered` result instead of an error;
  a single question object (or JSON text) is accepted; marker text `(recommended)`.
- Not taken: legacy tool-name aliases and the tool-search bridge in the pairing check; observer
  hooks around a human wait (P13); partial answers on timeout (the port has no channel for them).

## Status

Done (2026-10-18). Files: `src/indus/tools/builtin/{todo,clarify}.py`,
`tests/unit/tools/builtin/{test_todo,test_clarify}.py`. Shared: `src/indus/tools/builtin/__init__.py`,
`src/indus/core/tool_round.py` (broker offered as `broker`), `src/indus/core/agent.py` (todo
wiring + `Agent.todo`), `tests/unit/tools/builtin/__init__.py` (new package marker).
