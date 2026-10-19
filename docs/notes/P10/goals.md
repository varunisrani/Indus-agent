# Task: Persistent goals /goal, /subgoal (P10-24) Phase: 10 Owner: P10 slice K builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/goals.py` (state shape,
judge flow, gates, failure counters; kanban parts skipped), `hermes_cli/goal_command.py` (verbs).
Closed before writing.
Evidence: none.

What it does (own words): a session can hold one standing goal. `/goal <objective> [--max-turns N]
[--gate "<cmd>"]...` sets it and sends the objective as the first turn; `/goal status|pause|resume|
clear`; `/subgoal add <text>|done <n>|list` keeps extra criteria. After each turn that ends
normally while the goal is active: the turn counts against the budget (20 by default); the quality
gates run first (shell commands through the terminal tool with approvals, 300 s each, last 3,000
chars of output kept); a failing gate (up to 3 tries) becomes the next message. Otherwise a judge
(auxiliary task `goal_judge`, 30 s, 4,096 tokens, last reply clipped to 4,000 chars) answers a
strict JSON verdict `{done, reason, next?}`. Done → the goal completes; not done → an ordinary user
message continues the work (with `next` as a hint when given). Judge errors fail open (continue);
3 unparseable verdicts in a row or 5 transport failures in a row pause the goal with a config hint;
the budget running out pauses it with a notice. A message the person sends always wins over a
continuation that has not started yet. State lives in `session_meta` namespace `goals` and comes
back on resume. Event `goal.update` carries the state for the TUI goal bar.

Inputs → outputs: slash text → reply lines and an optional first message; turn end (session id,
final text, exit reason) → gate runs / judge call → continuation injected through the session
host, or pause / done, plus `goal.update`.

Files: `src/indus/core/goals.py`, `tests/unit/core/test_goals.py`. Shared:
`src/indus/service/session_service.py` (idle check, input probe, `inject`, turn-end listeners,
`notify`), `src/indus/protocol/events.py` (`goal.update`, `loop.update`, `heartbeat.fired`) with
the generated protocol files and samples, `src/indus/foundation/config/schema.py` (`goals`,
`auxiliary.goal_judge`, `auxiliary.side_question`), `src/indus/service/commands/registry.py`.

Edge cases / failure modes:
- Turn interrupted or failed: no judge, no continuation (the person is in charge).
- Verdict wrapped in a code fence or prose: the first JSON object is taken; still no object →
  parse failure. A `done` that is not a boolean counts as unparseable.
- A gate that keeps failing: after 3 failed tries in a row the goal pauses (gate output shown).
- Goal cleared or paused while the judge runs: the verdict is dropped.
- A session with no row yet (fresh): the row is created before the goal is stored.

Our design differences / decisions:
- Verdict shape `{done: bool, reason: str, next?: str}` (the row's), no wait/blocked verdicts and
  no completion contracts.
- Gates are given with `/goal ... --gate "<cmd>"` (repeatable) when the goal is set; no separate
  gate verbs.
- `/subgoal done <n>` removes a met criterion (the reference has remove/clear).
- Continuation text written fresh; it is a plain user message, never a system-prompt change.

Tests we will write:
- fake judge: done on turn 3 → completed, no 4th continuation;
- judge garbage ×3 → paused; transport error ×5 → paused;
- failing gate → its output is the next message; budget 20 reached → paused with a notice;
- user message while a continuation is queued wins; resume after restart restores the goal;
- argument parser table; subgoals in judge and continuation text.

## Decisions for the lead
- Gate approvals: the session's broker is offered when one is attached; with none the terminal
  tool treats the call as unattended (`approvals.unattended_mode`).
- Host seams added to `SessionService` (lead to keep when merging slice G's injection work):
  `is_idle`, `generation`, `set_input_probe(probe(session_id))`, `inject(..., origin,
  if_generation)`, `notify`, `on_turn_end`, `on_session_moved`. Slice G's background-result
  injection can use `inject` too.
- CLI wiring: `cli/launch.start_automation` starts `service.automation.SessionAutomation` on the
  host loop, sets the input probe to the REPL's queued messages and binds `ctx.automation`. Other
  hosts (TUI bridge, gateway) should do the same and route `/goal` & co. to
  `SessionAutomation.command`.
- Intended differences: strict boolean `done`; no blocked/wait verdicts; an active goal holds
  loop runs; the judge uses the host's aux client (usage not in the session ledger).

Compare checklist (filled after the rewrite; reopened `hermes_cli/goals.py`, `hermes_cli/goal_command.py`):
- [x] Defaults: 20 turns, judge 30 s / 4,096 tokens / reply clipped to 4,000, gates 300 s, 3 retries, 3,000-char tail (`tests/unit/core/test_goals.py::test_aux_judge_asks_the_goal_judge_task`, `test_a_failing_gate_is_the_next_message_and_the_judge_waits`)
- [x] Gate retry rule: pause after more failures than retries (`test_a_gate_that_keeps_failing_pauses`), same as the reference
- [x] 3 parse / 5 transport failures pause; a readable verdict resets (`test_three_unreadable_verdicts_pause`, `test_five_transport_failures_pause`, `test_a_readable_verdict_resets_the_counters`)
- [x] Budget pause with notice (`test_the_turn_budget_pauses_with_a_notice`); resume resets the budget like the reference (`test_pause_resume_clear_and_status`)
- [x] The person's message wins (`test_a_message_from_the_person_wins_over_a_queued_continuation`); a changed goal drops a late verdict (`test_a_verdict_is_dropped_when_the_goal_changed_meanwhile`)
- [x] Restart restores (`test_the_goal_survives_a_restart`, `test_store_meta_creates_the_session_row_and_restores`)
- [x] Gap found: the reference skips the judge for an empty reply — added (`test_a_reply_without_text_is_not_judged`)
- [x] Gap found: the reference keeps an active goal and a loop from taking turns in one session — loops are held while a goal is active (`tests/unit/core/test_loops.py::test_an_active_goal_holds_the_loop`)
- [x] Gap found: goals, loops and heartbeats follow a rotating compaction to the child session (`tests/unit/service/test_automation.py::test_a_rotating_compaction_carries_the_features`)
- Deliberate differences: verdict `{done, reason, next?}` only (no blocked/wait verdicts, no wait barriers, no completion contracts, no `/goal draft`); `done` must be a JSON boolean; gates given as `--gate` flags at set time; `/subgoal done N` replaces remove/clear.
- Not taken: background-process and delegation blocks in the judge prompt (P10-05 delegation is not on this base), kanban worker goals.

## Status

Done (2026-10-19). Shared: `src/indus/service/session_service.py` (`is_idle`, `generation`, `set_input_probe`, `inject`, `notify`, `on_turn_end`, `on_session_moved`), `src/indus/protocol/events.py` (+ regenerated `packages/protocol/src/generated/*`, samples in `tests/unit/protocol/_samples.py`), config rows, registry entry, `locales/en.yaml` (two slash descriptions), ledgers.
