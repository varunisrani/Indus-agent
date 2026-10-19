# Task: Mixture-of-agents turns (P10-21) Phase: 10 Owner: P10 slice K builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/moa_loop.py` (reference
gathering, trimming, interrupt settle, privacy patterns, advisory view, guidance block),
`agent/moa_alternation.py` (all). Closed before writing.
Evidence: none.

What it does (own words): a turn can be marked "mixture of agents" (`/moa [preset] <prompt>` marks
the next turn; `/moa on [preset]` marks every turn until `/moa off`). Before each model request of
such a turn, the preset's enabled reference models are asked, all at once (at most 8 at a time),
what they make of the conversation so far. Their answers become one labelled advice block that is
added to the turn's user message in the request copy only (never the system prompt, never the
store). The session's own model still acts: it calls the tools and decides when the turn ends.

Inputs → outputs: armed plan (preset, reference caller) + the request the loop built → the same
request with the advice block on the last user message; `Status` / `Notice(key="moa")` events;
usage of every reference call in the session ledger under task `moa`.

Files: `src/indus/core/moa.py`, `tests/unit/core/test_moa.py`. Shared: `src/indus/core/loop.py`
(pre-request hook), `src/indus/core/turn_state.py` (`TurnState.moa`, `LoopOptions.grace`),
`src/indus/providers/auxiliary.py` (`AuxClient.call_route`: one call on a given route).

Edge cases / failure modes:
- A reference fails or times out (`moa.reference_timeout` / the preset's, none by default): its
  slot in the block says so ("no answer: timed out after N s"); the turn goes on. All failing →
  the block says no advice arrived.
- Interrupt while references run: the wait wakes on the turn's stop event, gives finishing calls
  50 ms, cancels the rest (their slots say "skipped: interrupted"); no block is attached to a
  request that is not sent.
- A destination that rejects two user messages in a row (classifier `role_alternation`): retried
  once with neighbours merged and remembered, so later calls to it are merged before sending.
- The reference's window is smaller than the conversation: oldest messages after the first are
  dropped until the estimate fits `window × 0.9 − 8192`; the last user message and the one before it
  are always kept and the list never starts on an assistant message.
- Tool results longer than 4,000 characters reach references as head + tail with an omission note;
  tool calls appear as one-line text; no tool roles or tool-call arrays are sent to references.
- Privacy filter `moa.privacy_filter`: `display` masks emails and delimited phone numbers (after the
  main secret redactor) in what is shown; `full` also masks them in the block the model gets.
- A preset slot whose provider is `moa` is never called (also refused at validation, P10-22).

Our design differences / decisions:
- The block is appended to the text of the last user message of the request (the turn's message,
  or a steer after tool results). The reference instead adds a separate trailing user message; we
  never create two user messages in a row on the main route.
- No separate aggregator call: the session's model is the aggregator (the spec's wording). A
  preset's `aggregator` is shown by `indus moa list` only.
- asyncio tasks instead of a thread pool; the wait wakes on the stop event (the 5 s poll interval
  is kept as the longest wait between checks, the 50 ms settle as is).
- Fan-out cadence `fanout`: `per_iteration` (default, as the row says), `user_turn`, `every_n:N`;
  skipped iterations reuse the last block unchanged.
- MoA turns are the documented exception to byte-stable prompts (D-16.72): the user message bytes
  change per iteration.

Tests we will write:
- three references asked in parallel; block on the user message; system prompt bytes unchanged;
- one reference times out on the fake clock → block notes it, turn completes;
- tool results clipped to 4,000 in the reference view;
- alternation merge after a `role_alternation` rejection, remembered for the next call;
- interrupt cancels pending references; privacy filter modes; trimming to a small window;
- loop hook: an armed turn calls references before each request, an unarmed one never does.

## Decisions for the lead
- Block placement on the last user message (see above); easy to switch to a trailing message.
- `AuxClient.call_route` added (public) so a reference can name its own route; usage recorded with
  the bound recorder like every aux call.
- Intended differences (D-ids to number): advice appended to the last user message instead of a
  separate trailing message; the session's model is always the aggregator; references see our own
  advisory instructions; fan-out default per iteration.
- Interrupt wait wakes on the stop event (the 5 s poll is only the longest gap between checks).
- A `/moa on` arm and a goal/loop/heartbeat follow a rotating compaction (`SessionAutomation.move`).

Compare checklist (filled after the rewrite; reopened `agent/moa_loop.py`, `agent/moa_alternation.py`):
- [x] References asked in parallel, at most 8 at a time (test: `tests/unit/core/test_moa.py::test_three_references_in_parallel_and_the_block_on_the_user_message`)
- [x] Advice never in the system prompt; prompt and tool bytes equal to a plain turn (`test_system_prompt_bytes_match_a_plain_turn`)
- [x] Failed / timed-out reference named in the block, turn goes on (`test_a_timed_out_reference_is_noted_and_the_turn_goes_on`, `test_failed_references_give_a_no_advice_block`)
- [x] Tool results clipped head + tail at 4,000, calls rendered as text, view ends on a user message (`test_reference_view_folds_tools_and_clips_results`, `test_every_iteration_asks_again_and_tool_rounds_stay_paired`)
- [x] Window trim with the reference instructions counted, last user + one before kept, user first (`test_trim_keeps_the_newest_and_starts_on_a_user_message`, `test_trim_uses_each_reference_window`)
- [x] Reactive alternation merge remembered per destination (`test_alternation_rejection_merges_and_is_remembered`)
- [x] Interrupt: settle, cancel the rest, never attach a partial block (`test_interrupt_cancels_pending_references`, `test_interrupt_during_references_ends_the_turn_before_the_request`)
- [x] Privacy filter: display masks shown text only, full masks the block too (`test_privacy_modes_on_the_block`, `test_privacy_masks_emails_and_formatted_phones`)
- [x] Gap found: reused advice on a later iteration reads as fresh after tool results — fixed by the `EARLIER_ADVICE` note, pinned by `test_user_turn_fanout_reuses_the_block`
- [x] Usage under task `moa` through the real client (`test_aux_references_over_the_real_client`)
- Deliberate differences: the reference adds the advice as its own trailing user message and has a separate aggregator call path for one-shot `/moa`; we append to the last user message and the session's model is always the aggregator. No trace files, no per-reference cost columns (usage rows carry the task tag), no `degraded_reference_policy` (failures are always named).
- Not taken: reference temperatures, per-reference output caps, prompt-cache markers on reference requests, the copilot initiator header.

## Status

Done (2026-10-19). Shared files changed: `src/indus/core/loop.py` (claim after the prologue, pre-request hook, `grace` switch), `src/indus/core/turn_state.py` (`TurnState.moa`, `LoopOptions.grace`), `src/indus/providers/auxiliary.py` (`AuxClient.call_route`; `_once` takes `timeout=None` for no deadline).
