# Task: Prologue, persist order, finalize (P04-16) Phase: 04 Owner: wave-6a builder X

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/turn_context.py` (the turn
setup steps, per-turn reset, user-row staging, history hydration, session-row creation, sidecar
stamping, the turn-start persist, the boundary export), `agent/turn_finalizer.py` (whole:
budget fallback, scaffold drop, stream recovery, tail close, guarded cleanup, exit log, leftover
steer), `agent/turn_final_response.py` (scaffold pop before the durable final flush). Spec:
research `01-agent-core.md` §3.3, §3.9, traps R01.3, R03.19. Closed before writing.
Evidence: none.

What it does (own words):

Prologue (`run_prologue`), in this order:
1. reset per-turn state (checks, guardrail counters, budget, scrubber, streamed text);
2. scrub lone UTF-16 surrogates from the user text and from `persist_text` (R03.19);
3. repair the loaded history (P04-03); when a stored row changed or a stored row was dropped, the
   live transcript in the store is replaced softly (best effort, WARNING on failure);
4. hydrate process-local state on the first turn of this process that has history, through a
   hydrator table (name → callable over the rows; the todo list registers there in P05);
5. restore or build the system prompt (P04-07);
6. make sure the session row exists **with** the prompt before anything else touches the store;
7. stage the user row: `content` is what the user typed (`persist_text` when given), `api_content`
   is the exact text sent (the message incl. any API-only prefix, plus the one-shot notes);
8. **persist the user row before the first provider call**. A failed write ends the turn
   `persistence_failed` without calling the model.

Persist order during the turn: rows are stored in list order as they are appended; scaffold rows
(continuation and ack nudges, retry sentinels, prefills, the grace nudge) are never stored. The
assistant tool-call row is stored before any tool runs; each tool row is stored when committed.

Finalize (`finalize_turn`, the single exit funnel), every step guarded on its own so a failing step
never loses the answer:
1. drop scaffold rows from the live list;
2. recover the final text from the streamed text when the terminal answer came back empty;
3. interrupted: an open tool-call row gets synthesized results, sealed partial text becomes an
   assistant row; delivered final text always has an assistant row;
4. persist what is not stored yet;
5. leftover steer becomes `pending_steer` (the next turn's message), unless the stop was hard;
6. withdraw open server requests (turn ended);
7. stamp `{turn_id, current_turn_user_index}`, set exit reason / failure, log one line, emit
   `turn.done`; reasoning is taken from this turn's rows only.

Inputs → outputs: `TurnState` → the prologue's side effects (rows stored) / a `TurnResult`.

Files: `src/indus/core/prologue.py`, `src/indus/core/finalize.py`, `src/indus/core/persist.py`
(the `TranscriptStore` seam, the P02 adapter, an in-memory store), `tests/integration/core/test_persist_order.py`,
`tests/unit/core/test_prologue_finalize.py`, `tests/fakes/store.py` (commit recorder with named
failure points).

Edge cases / failure modes:
- the store raises at the user row → no provider call at all.
- a crash (process death, simulated with a `BaseException`) at each write point, then a restart
  that reloads and repairs → never a lost user row, never a duplicated side effect, valid
  alternation, one result per remaining call.
- `persist_text` with notes: stored text is clean, the wire saw prefix + notes.
- multimodal user content: notes become a text part (no string sidecar).
- the steer row's `display_kind` is not a store column: on reload a row whose text starts with the
  steer marker is marked `steer` again.

Our design differences / decisions:
- Store access through an async `TranscriptStore` protocol; `StateStore` adapts the P02 store
  (writes through `awrite`, reads on a worker thread).
- Repair rewrites are applied with `messages.replace_in(soft=True)` guarded by the expected live
  ids.
- Not taken: preflight compression, plugin pre-call context, memory prefetch, titles, MoA, voice,
  output-transform hooks, file-mutation footer, abnormal-exit explainer, trajectory save.

Tests we will write: crash-injection at user row / assistant tool-call row / tool row / final row
with a reload; raise-injection at each point → `persistence_failed` and a valid live list; scaffold
rows absent from the store; surrogate scrubbed; `persist_text` stored while the wire saw the prefix;
notes in `api_content` only; hydrator runs once; leftover steer; stream recovery; repair rewrite
applied to the store.

## Decisions for the lead
- P02 `messages` has no `display_kind`/`scaffold` columns; steer rows are recognised by their
  marker on reload (`persist.rows_from_store`). A `display_kind` column would be cleaner (P02).

Compare checklist (filled after the rewrite; reopened `agent/turn_context.py` (build steps,
staging, hydration, row creation, sidecar, turn-start persist, boundary export) and
`agent/turn_finalizer.py`):
- [x] prologue order: prompt before session row; row before first call (tests:
  `tests/unit/core/test_prologue_finalize.py::test_user_row_is_stored_before_the_first_call`,
  `::test_a_failed_start_write_means_no_provider_call[*]`)
- [x] sidecar = exact sent bytes; clean text stored; notes one-shot (tests:
  `::test_persist_text_is_stored_while_the_wire_sees_the_prefix`, `::test_notes_ride_the_user_message_only`)
- [x] surrogates scrubbed (test: `::test_surrogates_are_scrubbed`)
- [x] hydration only on the first resumed turn (test: `::test_hydrators_run_once_on_the_first_resumed_turn`)
- [x] finalize: scaffold drop → stream recovery → reason → tail close → persist → leftover steer
  (tests: `::test_scaffold_rows_never_reach_the_store`, `::test_stream_text_recovers_an_empty_final`)
- [x] each cleanup step guarded (test: `::test_a_failing_finalize_step_keeps_the_answer`)
- [x] boundary stamp; reasoning from this turn only (tests: `::test_reasoning_is_taken_from_this_turn_only`,
  `tests/unit/core/test_loop.py::test_every_exit_passes_the_funnel[*]`)
- [x] repair write-back to the store (test: `::test_repaired_history_is_written_back`)
- [x] crash and raise injection at every write point on the real P02 store (tests:
  `tests/integration/core/test_persist_order.py::test_crash_at_each_write_point_then_restart[*]`,
  `::test_store_error_at_each_write_point[*]`, `::test_interrupted_tool_batch_survives_a_restart`)
- [x] gap found: after a crash between a stored call row and its results the repair silently
  dropped the call, so the model could run the side effect again — the prologue now answers such
  tail calls with an "outcome unknown" result (stored), pinned by `test_crash_at_each_write_point_then_restart[tool]`.
- Deliberate differences: no in-place row update protocol for late sidecars (we stage the sidecar
  before the first write); the reply-less turn's user row stays its own stored row and the wire
  sanitizer joins it with the next one.
- Not taken: compaction, plugin context, memory prefetch, titles, MoA/voice, output-transform
  hooks, file-mutation footer, abnormal-exit explainer, trajectory save, kanban bookkeeping.

## Status

Done (2026-10-18). Files: `src/indus/core/prologue.py`, `src/indus/core/finalize.py`,
`src/indus/core/persist.py`, `tests/unit/core/test_prologue_finalize.py`,
`tests/integration/core/test_persist_order.py`, `tests/fakes/store.py`.
