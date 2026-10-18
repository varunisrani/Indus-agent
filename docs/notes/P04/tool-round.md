# Task: Tool round: validation and ordering (P04-13) Phase: 04 Owner: wave-6a builder X

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/turn_tool_round.py` (whole
flow), `agent/turn_tool_validation.py` (whole), `agent/tool_dispatch_helpers.py` (result message
constructor, untrusted wrapping, delimiter neutralising), `run_agent.py` (the delegate cap, the
duplicate filter, `_execute_tool_calls` dispatch shape), `agent/agent_runtime_helpers.py`
(`repair_tool_call`), `agent/conversation_loop.py` (the invalid-name error text helper, only its
two cases). Spec: `docs/plan/research/01-agent-core.md` §3.7, traps R01.2, R01.3, R01.14. Closed
before writing.
Evidence: none.

What it does (own words): when an answer carries tool calls, the round makes the batch safe to run
and then runs it without ever breaking the transcript.

1. Call ids are made unique against every id already used in this session (deterministic
   `<id>_d<n>` suffix, never random) so the sanitizer and the UI can pair them.
2. Unknown names are repaired when an obvious variant exists (letter case, `-`/space vs `_`,
   CamelCase, a trailing `_tool`/`tool` suffix, characters after a stray quote or angle bracket);
   otherwise the call gets a tool-role error naming the valid tools (a blank name gets a short
   error instead, so a model echoing tool syntax from data is not fed the catalogue).
3. A batch whose every call is unknown is a strike: the assistant row plus one error result per
   call are stored and the model is asked again; three strikes in a row end the turn
   (`invalid_tool_strikes`). A mixed batch errors only the unknown calls and runs the rest (no
   strike, the counter resets).
4. Arguments must be a JSON object (empty text means `{}`). A batch with malformed JSON is asked
   for again without storing anything, up to 3 times; the 4th malformed batch is stored with error
   results for the bad calls and "skipped" results for the good ones, so the model can recover.
5. `delegate_task` calls beyond the configured child limit are dropped (others kept); repeated
   `(name, canonical args)` pairs are dropped, first wins. Dropped calls never reach the stored row,
   so they need no result.
6. The assistant tool-call row is persisted and committed **before** any tool runs; a failed write
   ends the turn `persistence_failed` and no tool runs.
7. The calls run through the planner/executor (P04-14); exactly one result per call is appended in
   the original call order (each result is stored as it is committed), the turn budget (P04-12) is
   applied to results not yet stored, guardrail guidance (P04-18) is added at construction,
   steer text (P04-19) is drained into its own row after the batch, and a guardrail halt ends the
   turn.

Result rows are `{role: tool, name, content, tool_call_id, timestamp}`. Results of `web_search`,
`web_extract`, `browser_*` and `mcp__*` tools that are at least 32 characters long are fenced as
untrusted data; every occurrence of the fence word inside the content is neutralised
case-insensitively first, and there is no "already wrapped" shortcut (a forged fence would make it
exploitable).

Inputs → outputs: the assistant `NormalizedResponse` + the `TurnState` → rows appended and stored,
events (`tool.started`, `tool.completed`), and a round verdict (`continue` / `retry` / `end`).

Files: `src/indus/core/tool_round.py`, `src/indus/core/untrusted.py`, `src/indus/core/loop_texts.py`
(all loop prose in one place), `tests/unit/core/test_tool_round.py`, `tests/fakes/tools.py`;
shared: `tests/fakes/model/__init__.py` (script helpers).

Edge cases / failure modes:
- an id reused by a provider every turn (`call_0`) → renamed `call_0_d1`, `_d2`, stable.
- a repaired name that is still unknown, or a repair that maps to an inline tool: inline names are
  valid names too.
- arguments given as a dict (some providers) → dumped to JSON text first.
- every call dropped by the dedupe? impossible: the first of each pair stays.
- the store raising on the assistant row → no side effect, turn ends; the live rows keep the call
  with closing error results so alternation still holds in what is returned.
- interrupt during the batch → executor synthesizes skipped/cancelled results; still one per call.
- multimodal results: only text parts are fenced.

Our design differences / decisions:
- A round returns a small verdict object; the loop owns iteration flow.
- The turn budget runs on results that are not stored yet (stored rows are never rewritten, so the
  wire always equals what is on disk); the reference trims after appending.
- Prose (error texts, fence preamble) is ours and lives in `core/loop_texts.py`.
- Not taken: tool-search bridge unwrapping, local batch expansion, housekeeping-round muting,
  stale bracket-marker stripping, execute_code refund, post-tool compression (P08), truncated-args
  refusal (P04-17's tool-call retry already covers cut-off calls; malformed args here are re-asked).

Tests we will write: name repair table; unknown ×3 → `invalid_tool_strikes`; mixed batch runs the
valid call; malformed JSON ×4 → error results on the 4th and nothing stored before; duplicates
dropped; delegate cap; id uniquifying across turns; commit order (assistant row committed before
the tool's side-effect marker); results in call order when finished out of order; fence wraps,
neutralises a forged closing delimiter in any letter case, leaves short text alone; store failure
on the assistant row → no side effect.

## Decisions for the lead
- Child cap for `delegate_task` per batch: `LoopOptions.max_child_calls` (default 3) until the
  delegation phase owns a config key.
- Loop prose lives in `core/loop_texts.py` (wave 5 kept nudges in `response_checks.NUDGES`); both
  could move into `core/prompt/texts.py` at merge.

Compare checklist (filled after the rewrite; reopened `agent/turn_tool_round.py`,
`agent/turn_tool_validation.py`, `agent/tool_dispatch_helpers.py` (result message, fence),
`run_agent.py` (cap, dedupe), `agent/agent_runtime_helpers.py` (`repair_tool_call`)):
- [x] uniquify before anything else, against ids already in the session (test:
  `tests/unit/core/test_tool_round.py::test_ids_made_unique_deterministically`,
  `::test_ids_repeated_across_turns_are_renamed`)
- [x] name repair variants; blank-name short error (tests: `::test_name_repair_table`,
  `::test_misspelled_name_is_repaired_and_run`, `::test_blank_name_gets_the_short_error`)
- [x] strikes only for all-invalid batches, the 3rd strike ends without storing the batch; mixed
  batch runs valid calls (tests: `::test_three_unknown_batches_end_the_turn`,
  `::test_mixed_batch_runs_the_valid_call`)
- [x] JSON retry and recovery results; a mixed batch's unknown call never triggers the JSON retry
  (test: `::test_bad_json_is_asked_again_three_times_then_answered`)
- [x] cap then dedupe on canonical args, first wins (test: `::test_duplicates_and_extra_children_are_dropped`)
- [x] persist before execute; a failed write runs nothing (tests:
  `::test_call_row_is_committed_before_the_side_effect`, `::test_store_failure_on_the_call_row_runs_nothing`)
- [x] results in call order, one per call (test: `::test_results_follow_call_order_when_finished_out_of_order`)
- [x] untrusted fence: tool set, 32-char floor, case-insensitive neutralising, no shortcut, parts
  fenced one by one (tests: `::test_fence_rules`, `::test_web_results_are_fenced`)
- [x] gap found: the reference re-arms the post-tool empty nudge after each round — added
  (`checks.post_tool_nudged = False`), pinned by `tests/unit/core/test_loop.py::test_empty_after_tools_gets_one_nudge`
- Deliberate differences: the JSON retry asks again 3 times and recovers on the 4th (phase row;
  the reference recovers on the 3rd); no fuzzy closest-name match (it can pick the wrong tool);
  on a failed call-row write the live list gets "not run" results so what is returned still
  alternates; the turn budget only spills results not stored yet; MCP prefix is ours (`mcp__`).
- Not taken: tool-search bridge unwrapping, local batch expansion, housekeeping muting, bare
  bracket-marker stripping, execute_code refund, post-tool compression (P08), elision notice for
  MCP pages, output risk metadata, truncated-args refusal (P04-17's tool-call retry covers cut-off
  calls).

## Status

Done (2026-10-18). Files: `src/indus/core/tool_round.py`, `src/indus/core/untrusted.py`,
`src/indus/core/loop_texts.py`, `tests/unit/core/test_tool_round.py`, `tests/fakes/tools.py`;
shared: `tests/fakes/model/__init__.py` (script helpers `text_turn`, `tool_batch`,
`malformed_args`, `mid_stream_cut`, `thinking_only`, `script_of`).
