# Task: background delegation (P10-05) Phase: 10 Owner: P10 slice G builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/async_delegation.py`
(queueing, result re-entry, list/steer/stop), `tools/async_delegation_worker.py`,
`agent/subagent_lifecycle.py` (states). Closed before writing.
Evidence: none.

What it does (own words): on an interactive surface a top-level spawn returns at once with
`{subagent_ids, status: started}`. At most `delegation.max_async_children` (3) children run per
session; more wait their turn. When every child of one call has ended, one message with their
results enters the parent conversation through `SessionService` between turns: if a turn is
running it waits for that turn to end, then starts a new turn with the results as an internal
user message. `action: list` shows `{subagent_id, goal, status, started_at, iterations,
last_activity}`; `steer` hands text to the child through the P04 steer path (delivered right
after its next tool results); `stop` interrupts one child and returns its partial result. `/stop`,
a hard interrupt and session end stop every detached child of the session. Process-local only: a
restart forgets them (the tool description says durable work belongs to cron or background
processes).

Inputs → outputs: spawn → ids; list/steer/stop → JSON; completion → internal message.

Files: `src/indus/core/subagents/background.py`, `tests/unit/core/subagents/test_background.py`;
shared: `src/indus/service/session_service.py` (internal message between turns, stop children),
`src/indus/cli/slash/port.py` (`/stop`, `/new` stop children).

Edge cases / failure modes:
- 4th async child queued (status `queued`), starts when one ends.
- Completion while a turn runs → queued behind it, never mid-turn.
- Steer/stop of an unknown or finished id → error text.
- Interrupted parent turn does not stop detached children (only stop surfaces do).

Our design differences / decisions:
- No durable ledger or replay after restart (the plan's process-local rule).
- Steer goes through the child's P04 steer row rather than being glued into the tool result text.

Tests we will write:
- spawn returns at once; completion injected after the running turn ends; 4th child queued;
  steer reaches the child's next request; stop returns partial; stop surface stops detached
  children; new process → empty list, no crash.

Compare checklist (reopened `tools/async_delegation.py`, `agent/subagent_lifecycle.py`):
- [x] immediate return, delivery between turns only (tests: `tests/unit/core/subagents/test_background.py::test_spawn_returns_at_once_and_results_arrive_as_a_new_turn`, `::test_completion_waits_for_the_running_turn`)
- [x] async cap 3, 4th queued (test: `::test_fourth_child_waits_for_a_slot`)
- [x] steer / stop / list (tests: `::test_steer_reaches_the_child_after_its_next_tool_call`, `::test_stop_returns_the_partial_result_and_skips_delivery`)
- [x] stop surfaces (tests: `::test_stop_surface_stops_detached_children`, `::test_hard_interrupt_and_session_close_stop_detached_children`); restart (`::test_a_new_process_knows_no_children`)
- Deliberate differences: at the async cap the reference rejects new background dispatches; the
  plan says queue, so we queue. No durable ledger, replay or orphan sweep (process-local).
- Not taken: per-task / per-group independent completions.

## Decisions for the lead (added)
- `/stop` and `/new` reach the children through `cli/slash/port.py` `stop_processes` (its count
  now includes stopped subagents, so `/stop` may say "N background processes" for subagents too).
- `SessionService.interrupt(hard=True)` also stops detached children (Ctrl+C twice).
- Internal messages are ordinary user turns (`[Background subagent results] ...` + JSON); a
  pending steer follow-up goes first.

## Status

Done (2026-10-19). Shared: `service/session_service.py`, `cli/slash/port.py`.
