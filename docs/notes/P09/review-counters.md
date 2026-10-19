# Task: Review counters and trigger (P09-14) Phase: 09 Owner: P09 slice E builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/turn_finalizer.py` (the
review spawn at the end of a turn), `agent/turn_iteration_prep.py` (skill counter tick),
`agent/tool_executor.py` (counter resets on `memory` / `skill_manage`), `agent/turn_context.py`
(memory counter tick and resume hydration), `tests/agent/test_skip_background_review.py` (edge
list only). Closed before writing.
Evidence: none.

What it does (own words): two counters per session decide when the background review (P09-15)
runs after a turn.

- Memory counter: one more per user turn while the `memory` tool is offered and a store is on.
  Due when it reaches `memory.review_interval` (default 10), then back to 0. A turn in which the
  agent itself called `memory` sets it to 0 (the agent just saved what it wanted) and is not due.
  A resumed session starts at (prior user turns mod interval).
- Skill counter: one more per tool-calling iteration (an assistant row with tool calls) while
  `skill_manage` is offered; an iteration that calls `skill_manage` sets it to 0. At turn end it is
  due when it is at or over `skills.review_interval` (default 10), then back to 0.
- Interval 0 or less: that counter is off.
- Trigger, checked after the final answer is stored: the turn gave a final answer, was not
  interrupted and did not fail; the session is not marked no-review (`run_context` `cron` or
  `single_query`, or the host's `no_review` flag for delegated children and safe workers); and
  `learning.review.enabled` (default true; an unreadable value counts as true and logs a warning).
  The result is the set of due kinds: `memory`, `skills` or both. Counters move on every turn of a
  reviewable session, also when the trigger then decides not to start (a due count is used up, as in
  the reference).

Hook point: a guarded step `after_turn` in `core/finalize.py`, after the durable persist and the
withdraw step, running `SessionRuntime.after_turn` hooks in order. A hook only starts work (the
review runs as its own task); a failing hook is recorded in `cleanup_errors` like every other step.

Inputs → outputs: `TurnState` at the end of the funnel → due kinds and, when the trigger holds, one
`ReviewRequest(kinds, rows snapshot, session_id)` handed to the runner's `start`.

Files: `src/indus/learning/review/__init__.py`, `src/indus/learning/review/counters.py`,
`tests/unit/learning/review/__init__.py`, `tests/unit/learning/review/test_counters.py`.
`learning.review.fork.install_review(agent)` attaches the trigger (and the runner's hooks). Shared:
`src/indus/core/turn_state.py` (`SessionRuntime.after_turn` field), `src/indus/core/finalize.py`
(the guarded step), `src/indus/foundation/config/schema.py` (`memory.review_interval`,
`skills.review_interval`, `learning.review.*`) and its test.

Edge cases / failure modes:
- the snapshot is a deep copy of the stored rows (scaffold rows left out), so nothing the review
  does reaches the live transcript;
- a turn with no user row (should not happen) counts as no turn;
- unreadable intervals → the defaults;
- the hook never raises into the funnel (guarded) and never awaits the review.

Our design differences / decisions:
- counters are worked out from the finished turn's rows in one place instead of ticking in three
  modules; the skill counter counts tool-calling iterations only (the phase row), the reference
  counts every model call;
- a turn that already called `memory` is not memory-due (reference: the due check happens before
  the call, so it may still review right after the agent saved something);
- counters live on a small per-session object the host attaches (`ReviewTrigger`), not on the agent.

Tests we will write:
- 10 user turns → one memory review due; 9 → none; memory call at turn 5 resets (due at turn 15);
- 10 tool iterations without `skill_manage` → skill review due; a `skill_manage` call resets;
- interrupted turn → no request; `-z` (`single_query`) and `cron` sessions → no request;
  `learning.review.enabled: false` → no request; unreadable config fails open;
- resumed session with 23 prior user turns hydrates to 3;
- the finalize step runs hooks after persist, and a raising hook lands in `cleanup_errors` while the
  turn result is unchanged.

## Wiring for the lead
- Per session, after the agent opens: `runner = install_review(agent, no_review=<delegated child /
  safe worker>)` (`learning.review.fork`); it builds the runner and the trigger and appends their
  hooks to `agent.runtime.after_turn`.
- Before every live turn (`SessionService` submit, before `agent.start_turn`):
  `await runner.cancel_for_live_turn()`.

## Decisions for the lead
- `SessionRuntime.after_turn` is a generic post-turn hook list (core cannot import learning);
  later phases (P14 heartbeats, P13 plugin hooks) may reuse it.
- `learning.review.enabled` lives in a new `learning` section (`learning.review.*`), the intervals in
  `memory` and `skills`, as the phase row names them.

## Compare checklist (filled after the rewrite; reopened the reference files)
- [x] memory interval 10, reset on due, hydrate from prior user turns mod interval (test:
  `test_counters.py::test_ten_user_turns_make_one_memory_review`, `::test_resume_hydrates`)
- [x] skill interval 10, counted per iteration while `skill_manage` is offered, reset by a
  `skill_manage` call (test: `::test_ten_tool_iterations_make_a_skill_review`, `::test_skill_manage_call_resets`)
- [x] foreground `memory` call resets the memory counter (test: `::test_memory_call_resets`)
- [x] no review after an interrupt, without a final answer, for cron and one-shot runs (test:
  `::test_interrupted_turn_starts_nothing`, `::test_no_review_contexts`)
- [x] enabled flag fails open (test: `::test_unreadable_enabled_fails_open`)
- Deliberate differences: see above (iteration counting; memory call in the due turn).
- Not taken: the reference's external-memory-provider sync in the same place (P13).

## Status

Done (2026-10-19).

## Integration (lead, P09 wave 2 landing)
Installed per agent by `learning.host.SessionLearning.attach_review` from `cli/runtime.agent_factory` (`no_review` for one-shot runs). `ReviewTrigger.note_write` resets a counter when the agent's own write succeeds (`MemoryTool.on_success`, `manage_tool.SUCCESS_LISTENERS`, foreground, this session only; the listener leaves with the agent). D-16.152.
