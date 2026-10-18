# Task: Response checks: length and empty recovery (P04-17) Phase: 04 Owner: wave-5 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/turn_truncation.py` (all:
`recover_from_truncation`, `_abort_reason`, `_continue_text`, `_retry_truncated_tool_call`,
`boosted_output_cap`, `_prompt_filled_window`, `collapse_continuation_trail`, the content-filter
and Codex parts skimmed), `agent/turn_empty_response.py` (all), `agent/empty_response_guard.py`
(all), `agent/turn_response_check.py` (`check_api_response`, `_derive_finish_reason`, start of
`retry_invalid_response`), `agent/turn_response_intake.py` (all), `agent/think_scrubber.py` (all),
`agent/repetition_guard.py` (all), `agent/conversation_loop.py` (`_join_truncated_parts`,
`_continuation_overlap_length`, `_get_continuation_prompt`), `agent/retry_utils.py`
(`jittered_backoff`), `agent/agent_runtime_helpers.py` (`intent_ack_continuation_mode`),
`agent/turn_final_response.py` (stall/ack continuation block ~160–230). Test names only of
`test_truncated_tool_call_boost.py`, `test_think_scrubber.py`, `test_repetition_guard.py`,
`test_length_continuation_window_headroom.py`. Spec: `01-agent-core.md` §3.8 (partial stub,
scrubber), §3.14, traps R01.6, R01.17. Closed before writing.
Evidence: none.

What it does (own words): after each model call the loop asks this module what to do with an
answer that is cut off (`finish_reason == "length"`), empty, or an announcement with no action.
The module never calls the model or the store: it returns a `Verdict` (accept / continue /
retry / end) with the rows to append (nudges flagged `scaffold`), a boosted output cap, a wait,
the user-facing text and a failure reason. Order matters:

Length ladder: (0) a P03 stub that is terminal (context overflow after partial delivery) ends the
turn, never continues (R01.6). (1) visible text dominated by a repetition loop, or a reply whose
whole output went to reasoning, ends the turn with a clear message. (2) a text cut-off (or a P03
partial-stream stub) appends the fragment and a continuation nudge, both scaffold, up to 4 times;
if usage shows the prompt left under 512 tokens of the window the turn ends at once; at the
ceiling the fragments fold into one partial answer. Stub nudges say the stream broke (and name up
to 3 dropped tools); the next fragment's overlap with what was delivered is cut so text is
continued, not repeated. (3) a cut-off tool call is re-requested up to 4 times with output cap
`base·2ⁿ`, never below the failed cap, at most `max(32768, 2·cap)`, never above the model's known
limit; after that the turn ends and the truncated call is never executed.

Empty ladder: partial-stream text already shown → it is the answer; one nudge after a tool round;
a thinking-only reply is fed back as a prefill (twice); then empty retries with jittered backoff
(base 5 s, max 60 s, budget 3, or 1 when one attempt is estimated over USD 0.25; two identical
proven-empty attempts stop retrying); finally an `(empty)` sentinel row and a plain explanation.
Intent-ack: a short reply that ends by announcing its next step, with tools available, gets a
continuation nudge, at most 2 per turn.

Think-tag scrubber (A18): a small state machine over text deltas that moves inline reasoning tags
(`<think>…</think>` and the profile's extra names) out of visible text into reasoning, even when a
tag is split across deltas; an unclosed block at stream end is flushed as reasoning.

Inputs → outputs: `check_length(result, state, limits)`, `check_empty(result, state, ctx)`,
`check_intent_ack(text, state, tools_available)`, `boosted_cap(n, base, requested, limit)`,
`join_fragments(parts)`, `is_repetition_dominated(text)`, `ThinkScrubber.feed/flush` →
`Verdict` / ints / text. `TurnChecks` holds the per-turn counters.

Files: `src/indus/core/response_checks.py`, `tests/unit/core/test_response_checks.py`,
`tests/fixtures/model_scripts/{truncation,empty_ladder,mid_stream_overflow}.yaml`, shared
`locales/en.yaml` (section `turn.*`, user-facing copy).

Edge cases / failure modes:
- a stub whose text was shown is never retried as a fresh call (no duplicated text).
- a length reply with no text and no reasoning: nudged like a text cut-off (nothing appended).
- requested cap already at or above the model limit → the cap is not raised.
- unknown window or usage → never stops early.
- think tags in any letter case; prose that merely mentions `<think>` mid-line stays visible
  unless a full pair follows in the same buffer; an orphan closing tag is removed.
- backoff random source injected (tests are deterministic).

Our design differences / decisions:
- Pure functions + a small state object instead of loop-local rebinding; the loop (P04-15) owns
  applying a verdict.
- Fragments and nudges are both scaffold; the loop replaces the trail with the stitched answer
  (`join_fragments`) on success or at the ceiling.
- 4 continuations exactly as the phase row says (theirs nudges 3 times, folding on the 4th cut).
- Thinking budget exhausted: the first reasoning-only cut-off is continued with reasoning off
  (as theirs does for a structured-reasoning reply); a second one in the same turn ends it.
  Theirs detects exhaustion by an inline tag left in content, which our scrubber already removed.
- Fragments are joined directly (no inserted newline); overlap removed only at stub seams,
  minimum 8 characters.
- The scrubber flushes an unclosed block as reasoning (theirs discards it), per the phase row.
- Repetition check is our own: most frequent 48-char window → its period → expand the exact run;
  plus a repeated-line check. Dominated = a run of ≥5 repeats covering ≥50 % of ≥400 chars.
- Cost estimate is an input (`attempt_cost_usd`), `None` until P08 pricing exists.
- Model-facing nudge texts are constants here; user-facing copy is in `locales/en.yaml`.
- Not taken: content-filter fallback, provider fallback chain (P08), prior-turn content reuse
  for housekeeping tools (P04-13 has no tool kinds yet), Codex incomplete/scratchpad ladders,
  progress-callback relays, the streaming runaway watch.

Tests we will write: fake model scripts for each rung (driven through P03 `stream_call`); cap
arithmetic table; mid-stream overflow stub ends the turn; nudges scaffold; no rung executes a
truncated tool call; window-full early stop; ceiling fold; stub overlap; empty ladder order incl.
cost budget and deterministic short-circuit; intent-ack cap; scrubber split tags, unclosed flush,
orphan close, prose mention, case; repetition positive/negative table.

## Decisions for the lead
- Model-facing nudge prose lives in `response_checks.py` constants; slice C's
  `core/prompt/texts.py` is "the only place prose lives" — move them there at merge if wanted.
- Extra think-tag names: `ProviderProfile` has no field; `scrubber_for(profile)` reads an
  optional `think_tags` attribute (none today). P08 can add the field.
- `intent_ack` mode: no config key added; the loop passes `enabled`. A key
  (`agent.intent_ack_continuation`) can come with P04-15.

Cross-slice names used: none (P03 `CallResult`, `NormalizedResponse`, `Usage` are landed).

Compare checklist (filled after the rewrite; reopened `agent/turn_truncation.py`,
`agent/turn_empty_response.py`, `agent/empty_response_guard.py`, `agent/think_scrubber.py`,
`agent/repetition_guard.py` (outline only)):
- [x] length order: overflow-terminal stub → repetition / thinking abort → text continuation →
  tool-call retry (tests: `tests/unit/core/test_response_checks.py::test_mid_stream_overflow_stub_ends_the_turn`,
  `test_repetition_dominated_text_aborts`, `test_text_cut_off_is_continued_and_stitched`,
  `test_cut_off_tool_call_is_requested_again_with_a_bigger_cap`)
- [x] headroom 512 stops on the first cut-off; unknown numbers never stop (tests:
  `test_prompt_filling_the_window_stops_at_once`, `test_headroom_of_512_or_more_keeps_continuing`,
  `test_unknown_window_never_stops_early`)
- [x] ceiling folds the trail into one partial answer, no-text explanation (tests:
  `test_ceiling_folds_fragments_into_one_partial_answer`, `test_ceiling_without_any_text_explains`)
- [x] reasoning off for a text-less, non-stub continuation (test:
  `test_reasoning_that_ate_the_budget_retries_without_reasoning_then_aborts`)
- [x] stub nudge names up to 3 dropped tools; overlap at stub seams cut (tests:
  `test_broken_stream_with_dropped_tool_is_continued_not_repeated`,
  `test_stub_nudge_names_at_most_three_tools`, `test_fragments_join_directly_and_trim_overlap_only_after_a_stub`)
- [x] cap arithmetic incl. model limit and "already at the limit" (test: `test_boosted_cap_arithmetic`)
- [x] tool-call retry never appends the cut call; refusal after 4 (test:
  `test_cut_off_tool_call_is_refused_after_four_retries`)
- [x] empty ladder order: shown text → post-tool nudge once → prefill ×2 → retries → sentinel
  (tests: `test_empty_ladder_against_the_fake_model`, `test_post_tool_nudge_happens_once`,
  `test_prefill_is_tried_twice`)
- [x] budgets 3 / 1 at ≥ USD 0.25, deterministic rule (same signature; usage proves zero output, or
  no usage and nothing generated), backoff 5·2^(n−1) capped 60 + up to 50 % (tests:
  `test_expensive_attempts_get_one_retry`, `test_different_finish_reasons_are_not_deterministic`,
  `test_empty_without_usage_or_output_twice_is_deterministic`, `test_backoff_numbers`)
- [x] terminal sentinel keeps reasoning on the row, never in the answer text (test:
  `test_terminal_empty_row_keeps_the_reasoning_out_of_the_answer`)
- [x] scrubber: boundary rule, closed pair anywhere, split tags held, orphan close (+ following
  whitespace) removed, case-insensitive, flush resets the boundary (tests: the `scrub` group)
- [x] gap found: "Let me know …" sign-offs matched the announce pattern — excluded, pinned by
  `test_intent_ack_shapes`
- Deliberate differences: 4 nudges before the fold (theirs 3); fragments joined without an inserted
  newline; an unclosed think block flushes as reasoning (theirs discards it); default tag names
  are four generic ones (theirs adds a scratchpad tag and CJK names; profiles can add tags);
  repetition detector is ours (most-frequent window → period → exact run, plus repeated lines),
  judged on the last 64 KiB.
- Not taken: content-filter fallback and provider fallback chain (P08), housekeeping-tool content
  reuse, Codex incomplete and scratchpad ladders, runaway stream watch, interim rollback for
  non-continuable API modes, "suspicious stop treated as length" heuristic.

## Status

Done (2026-10-18). Files: `src/indus/core/response_checks.py`,
`tests/unit/core/test_response_checks.py` (73 tests), fixtures
`tests/fixtures/model_scripts/{truncation,empty_ladder,mid_stream_overflow}.yaml`; shared:
`locales/en.yaml` (new `turn:` section, 9 keys). The loop (P04-15) applies verdicts; P04-16 strips
scaffold rows and closes a tool-result tail on an `end` verdict.
