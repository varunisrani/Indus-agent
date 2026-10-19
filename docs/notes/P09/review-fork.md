# Task: Review fork (P09-15) Phase: 09 Owner: P09 slice E builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/background_review.py`
(constants, the digest, the tool whitelist, the fork runner, cancellation), `agent/review_engine.py`
(skim), `agent/review_idle_queue.py` (skim). Closed before writing.
Evidence: none.

What it does (own words): after a busy turn (P09-14) or on `/refine` (P09-17) a background task
replays the conversation in a forked turn and lets the model save facts and improve skills.

- The fork is a real turn of the P04 loop over a forked `SessionRuntime`: the parent's tool
  registry, a deep copy of its tool array (advertised tools stay byte-identical), its loop options
  (reasoning effort, output cap), its wire heal ledger and argument memo (copies), and the parent's
  stored prompt bytes served by a fixed builder. Its own store is an in-memory one, so nothing is
  written to the parent's transcript; it has no event sink, no broker (approvals deny), no context
  engine, no fallback chain and no post-turn hooks (so it never schedules a review of itself: R07.5).
- History: a structural deep copy of the snapshot. Same model: replayed whole (the first request is a
  warm prefix of the parent's). Routed to another model: a digest — the last 24 messages verbatim,
  extended back until the kept run starts on an assistant row (never on a tool result, never on a
  user row that would be joined with the digest's own user message), plus one user message that lists the
  older turns (user text cut to 300 chars, assistant to 200, tool names listed).
- Route: the auxiliary client's candidates for task `review` (P08-17; `auxiliary.review`). The
  first candidate naming the main route (or the same provider/model/endpoint) means "same model":
  the parent's caller, recording usage under task `review` on the parent session, with the parent's
  reasoning effort (a configured `auxiliary.review.reasoning_effort` is ignored with one warning per
  session). Otherwise the review goes through `AuxClient.call("review", …, tools=…)` (recording to
  the parent's ledger through the bound client) with the configured effort.
- Limits: at most 16 model requests (the loop's cap plus a caller guard that refuses a 17th, the
  grace request included); an input budget = sum of input tokens over its requests — explicit
  `learning.review.max_input_tokens` (≤ 0 unlimited), else 75 % of the review model's window capped
  at 600,000, 120,000 when the window is unknown; a request is refused when the tokens spent plus
  the last request's input would cross the budget (requests only grow). A refused request ends the
  fork's turn like a provider error; the writes made so far stay.
- Dispatch whitelist (advertised ≠ permitted): `skill_manage`, `skill_view`, `skills_list`,
  `read_file`, `search_files`, `memory` only when the review covers memory, and any
  `learning.review.extra_tools` the parent also offers. Everything else is refused with one line
  naming what is allowed. Enforced on both paths: inline executors are filtered, registry calls go
  through a `before` hook that blocks.
- Write origin bound to `background_review` inside the task (`learning.pending.bind_origin`),
  attended only for `/refine`. Read evidence (P09-08) is per session id; the fork runs under its own
  id `<parent>:review:<hex>` so the parent's earlier views never count and the marks are cleared at
  the end.
- Cancellation: a live turn calls `cancel_for_live_turn()`, which interrupts the fork (hard, by
  system) and waits up to 2.0 s (injected clock) for it to finish; past that it logs and returns.
- Scope: the task is started with the parent's `spawn` (`Agent.spawn` copies `Scope` and context
  variables, R01.16), else `spawn_with_scope`.
- One review per session at a time: a due review while one runs is skipped with a debug line.
- Deferral (`learning.review.defer: auto|never`, F9): when the review would run on a local server
  (`RuntimeRoute.is_local`) and it is not `/refine`, it is queued per session (newest snapshot
  replaces the older one, the first enqueue time is kept), dispatched once the process has had no
  live turn for 60 s, re-queued when a live turn pre-empts it, and force-run after
  `learning.review.defer_max_age_s` (1,800 s). In-memory, best effort; `enabled` is re-read at
  dispatch.

Inputs → outputs: `ReviewRequest(kinds, rows, focus?, attended)` + `ReviewParent` (runtime,
same-model caller, aux client, spawn, sink) → `ReviewOutcome(kinds, summary_lines, new rows, request
count, input/output tokens, stop reason, routed)`; the summary event (P09-16) on the parent's sink.

Files: `src/indus/learning/review/fork.py`, `src/indus/learning/review/idle_queue.py`,
`tests/unit/learning/review/test_fork.py`, `tests/unit/learning/review/test_idle_queue.py`,
`tests/integration/learning/test_review_fork_e2e.py`, `tests/unit/learning/review/helpers.py`,
`tests/unit/learning/review/_stubs_e.py` (STAND-IN for slice D's `skill_manage`). Shared:
`src/indus/providers/auxiliary.py` (`AuxClient.call(..., tools=...)`),
`src/indus/foundation/config/schema.py` (`learning.review.*`, `auxiliary.review`).

Edge cases / failure modes:
- the fork raising → logged as a warning, outcome `error`, no summary lines; usage already recorded;
- a parent with no stored prompt yet (never ran a turn) → nothing to review, skipped;
- route resolution failure for a pinned review model → one warning, same-model path;
- the budget or the cap stops the fork mid-way → writes done so far stay, the summary reports them;
- cancellation before the task started → it never calls the model.

Our design differences / decisions:
- the fork is our own loop's turn (fresh `TurnState` over a forked runtime) instead of a second
  agent object; persistence isolation comes from its in-memory store and its own session id;
- the "server idle" probe of the reference (slots of a managed local server) is reduced to "no live
  turn in this process for the settle window" — we have no managed local runtime yet;
- the settle window is 60 s (phase row);
- a skill-only review never gets the `memory` tool (same as the reference's gate);
- the budget stops *before* a request would cross it (the phase row); the reference lets the
  crossing request finish and stops at the next one;
- the digest tail is extended back to an assistant row, not only past tool rows, so the digest's
  user message is never followed by another user message;
- the configured-effort warning and the route-fallback warning are log lines (once per session),
  not chat notices — P15 may surface them.

Tests we will write:
- fake model scripted to call `terminal` → refused by the whitelist, the tool never runs;
- 17th request never sent (16 requests max, grace included);
- budget stop before crossing (scripted usage numbers);
- digest keeps 24 and never starts on a tool row; clipping 300/200; tool names listed;
- same-model first request: system, tools, effort and the message prefix equal the parent's last
  request;
- a live turn cancels the fork within 2 s (fake clock); approval prompt never reaches the broker;
- usage recorded on the parent ledger with task `review`;
- deferral queue replaces the older snapshot and force-runs at 1,800 s; dispatches after 60 s quiet;
- integration: a scripted review creates a skill → a new session's skills index lists it (M2 demo).

## Wiring for the lead
- Per session: `runner = ReviewRunner(ReviewParent.from_agent(agent))` (needs `AgentOptions.aux`
  for routed reviews; without it every review runs on the main model), then the trigger of P09-14.
- Live turn: `await runner.cancel_for_live_turn()` before `agent.start_turn`; on session close:
  `await runner.close()`.
- `skill_manage` (slice D) must read the origin from `learning.pending` (it does by the brief) and
  read evidence from `view_tools.READS` keyed by `ToolContext.session_id` — the fork's id.

## Decisions for the lead
- `AuxClient.call` gained `tools=` (passed into the `ChatRequest`) so routed reviews can call tools
  through the one auxiliary client (no second client).
- Config: `learning.review.{enabled, extra_tools, max_input_tokens, defer, defer_max_age_s}` and
  `auxiliary.review` (an `AuxTaskSection`).

## Compare checklist (filled after the rewrite; reopened `agent/background_review.py`,
`agent/review_idle_queue.py`, `agent/conversation_loop.py` budget check)
- [x] 16 iterations; budget 75 % / 600,000 / 120,000; explicit ≤ 0 unlimited (test:
  `test_fork.py::test_input_budget`, `::test_seventeenth_request_is_never_sent`,
  `::test_budget_stops_before_crossing`)
- [x] whitelist: skill tools + read_file/search_files + memory only for memory reviews + extras the
  parent has; advertised array unchanged (test: `::test_terminal_is_refused_and_never_runs`,
  `::test_skill_review_has_no_memory_tool`, `::test_allowed_tools_only_what_the_parent_offers`)
- [x] digest 24 + 300/200 clips + tool names; same model replays everything (test:
  `::test_digest_keeps_the_tail_and_lists_older_turns`, `::test_digest_never_starts_on_a_tool_row`)
- [x] same-model parity: prompt bytes, tools, effort, output cap (test:
  `::test_same_model_request_prefix_matches_the_parent`); configured effort ignored once (test:
  `::test_same_model_ignores_a_configured_effort_once`); routed honours it (test:
  `::test_routed_review_replays_a_digest_through_the_aux_client`)
- [x] no recursion, no parent transcript writes, approvals denied (test:
  `::test_review_writes_nothing_to_the_parent_transcript_and_never_reviews_itself`,
  `::test_approval_prompt_never_reaches_the_surface`)
- [x] read-before-write evidence fresh per review (test: `::test_parent_views_do_not_count_as_review_reads`)
- [x] 2.0 s cancel for a live turn (test: `::test_live_turn_cancels_the_fork_within_two_seconds`)
- [x] usage on the parent under `review` (test: `::test_usage_lands_on_the_parent_ledger_as_review`)
- [x] deferral: one slot per session, newest wins, first time kept, quiet window, max age 1,800,
  enabled re-read at dispatch, `/refine` never deferred (test: `test_idle_queue.py`)
- [x] M2: review writes memory and a skill; next session's index lists it; the session's prompt is
  unchanged (test: `tests/integration/learning/test_review_fork_e2e.py`)
- Deliberate differences: listed above.
- Not taken: the reference's `/btw` sharing of the fork builder, provider pin attributes (our
  route binding carries its own request extras), prefill messages (none in our loop), the managed
  local server's slot probe, the "agent cannot emit tool calls" skip (no such providers here).

## Status

Done (2026-10-19). Shared: `core/turn_state.py`, `core/finalize.py`, `providers/auxiliary.py`,
`foundation/config/schema.py` (+ test).

## Integration (lead, P09 wave 2 landing)
`runner.cancel_for_live_turn` runs as an `Agent.start_turn` `before_turn` hook (new core seam, `SessionRuntime.before_turn`). `_stubs_e.py` deleted: the tests use the real `skill_manage` (error results carry no `success` key; one test adjusted). `AuxClient.call` keeps one `tools=`/`tool_choice=` implementation. D-16.153.
