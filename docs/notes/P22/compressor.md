# Task: Compression algorithm (P22-12) Phase: 22 Owner: wave-21 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `trajectory_compressor.py`
(`_find_protected_indices`, `_snap_boundary`, `_extract_turn_content_for_summary`,
`_summary_prompt`, the planning / summary / assembly methods). Research 10 §3.5 (algorithm).
Closed before writing.

What it does (own words): squeezes one trajectory (list of `{from, value}` turns) under a token
budget by replacing as few middle turns as needed with one summary turn, never cutting a tool call
apart from its response.

Steps:
1. count tokens per turn; total ≤ target → unchanged (`skipped_under_target`);
2. protected = first index of each role whose flag is on + the last N turns; window starts after
   the highest protected index below `n // 2` and ends at the lowest protected index at or above
   it; snap the start forward off `tool` turns; start ≥ end → unchanged, still over the limit;
3. need = total − target + summary_target; take turns from the start until their tokens reach the
   need, else the whole window; snap the end forward past `tool` turns (backward only when nothing
   clean lies ahead inside the window);
4. region empty or its tokens ≤ summary_target → unchanged, still over the limit;
5. summary: turns rendered as labelled blocks (`[turn i · ROLE]`), values over 3000 chars cut to
   the first 1500 and last 500 with a marker naming the dropped count; our own prompt asks for a
   neutral factual record at ≈ summary_target tokens; `max_tokens = 2 × summary_target`; a reply
   with `finish_reason == "length"` (or empty) is a failure; `max_retries` attempts with
   jittered backoff (base `retry_delay`, doubling, cap 30 s, injected sleeper); after the last
   failure a fixed fallback summary. The summary prefix marker appears exactly once;
6. assemble head (system turn gets the notice text when enabled) + one `human` turn with the
   summary + untouched tail; recount; `still_over_limit` when above target.

Inputs → outputs: `Compressor(settings, counter, summarise, *, sleep, rng)`;
`plan(turns) -> Plan` (pure; region or None and the token facts); `await compress(turns) ->
(turns, TrajectoryMetrics)`; `TrajectoryMetrics.as_dict()`; `Summariser` protocol `(prompt,
max_tokens, temperature) -> SummaryReply(text, finish_reason)`; `aux_summariser(scope,
settings)` (provider layer: explicit model/base_url through `call_route`, else the auxiliary
client's `compression` task).

Files: `src/indus/research/compressor.py`, `tests/unit/research/test_compressor.py`.

Our design differences / decisions: our prompt, marker (`[SUMMARY OF EARLIER TURNS]:`), fallback
text and elision marker; the summariser goes through our provider layer (auxiliary client),
never a raw HTTP client or a host table; a blank reply is also a failure; the sleeper and RNG are
injected so tests never wait.

Tests we will write: Hypothesis — a compressed output never begins the summary region on a `tool`
turn nor separates a `gpt` tool-call turn from its `tool` turn; protected turns byte-identical;
prefix exactly once; under-target untouched (no summariser call); `length` retried then fallback;
backoff capped at 30 s; elision 3000/1500/500; need arithmetic on a hand-built case.

## Decisions for the lead
- none beyond the texts above.

Compare checklist (filled after the rewrite; reopened `_find_protected_indices`, `_snap_boundary`,
`_extract_turn_content_for_summary`, `_summary_prompt`, planning, summary and assembly):
- [x] protected set and window (`n // 2` split) (test: `tests/unit/research/test_compressor.py::test_window_after_head_before_tail`)
- [x] start snapped forward off tool turns; end snapped forward first, backward only when nothing
  clean lies ahead (tests: `test_snap_prefers_forward_then_backward`,
  `test_property_calls_and_responses_are_never_separated`)
- [x] need = total − target + summary target, whole window when never met
  (test: `test_need_arithmetic_takes_the_fewest_turns`)
- [x] region ≤ summary target → left alone (test: `test_a_region_no_bigger_than_the_summary_is_left_alone`)
- [x] elision over 3000 → 1500 head + 500 tail (test: `test_elision_3000_1500_500`)
- [x] `max_tokens = 2 × summary target`; `length` is a failure; retries with jittered backoff capped
  at 30 s; fallback summary after the last failure (tests:
  `test_length_finish_is_retried_then_the_fallback_is_used`, `test_backoff_is_jittered_doubling_and_capped`)
- [x] marker exactly once (test: `test_summary_marker_appears_exactly_once`)
- [x] head + one `human` summary turn + untouched tail, notice on the system turn, recount,
  `still_over_limit` (tests: `test_system_turn_gets_the_notice_and_protected_turns_stay_byte_identical`,
  `test_still_over_limit_is_flagged`)
- Deliberate differences: our prompt, marker, fallback text and elision marker; a blank reply is a
  failure too (the reference accepts an empty summary); a start that cannot be moved off a `tool`
  turn aborts instead of starting the region on it; inner repeats of the marker are removed; the
  summariser is our provider layer (auxiliary client), never a raw client; per-trajectory metrics
  are nested (`tokens {before, after, saved, ratio}`, `turns`, `region`, `summarisation`).
- Property tests are seeded random generators (600 + 300 trajectories): the suite has no Hypothesis
  dependency (none in the lock; adding one is a lead call).
- Not taken: the synchronous twin of the compress call (one async path only).

## Status

Done (2026-10-19).
