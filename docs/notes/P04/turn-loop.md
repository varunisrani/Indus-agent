# Task: Turn loop + single exit funnel (P04-15) Phase: 04 Owner: wave-6a builder X

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/conversation_loop.py` (loop
state, the turn driver, the per-turn error cap and local-module list, the run wrapper that stamps
the turn boundary), `agent/iteration_budget.py` (whole), `agent/turn_loop_errors.py` (whole),
`agent/turn_summary.py` (skimmed: display only, not taken), `agent/turn_iteration_prep.py`
(iteration start, retry-restart consumption incl. the restart cap), `agent/chat_completion_helpers.py`
(`handle_max_iterations` grace call), `agent/turn_final_response.py` (text-answer branch order),
`website/docs/developer-guide/agent-loop.md`. Spec: research `01-agent-core.md` §3.5–3.6, trap
R01.18. Closed before writing.
Evidence: none.

What it does (own words): `run_turn(state)` drives one user turn. `TurnState` is a plain object
handed to small phase functions; nothing is looked up by reflection.

- Loop condition: `(api_calls < max_iterations and budget.remaining > 0) or grace`. Default cap 150
  (`agent.max_turns`, D-16.62; `0` = unlimited). `api_calls` goes up when an iteration starts and
  one budget unit is consumed. Restarts that produced no answer (a redirect, a route change seen by
  P03) give the iteration back but bump `restart_count`; more than 8 restarts in one turn ends it.
- One iteration: interrupt check → steer drain (pre-request) → build the request (system prompt +
  sanitized wire copy + tool array) → one provider call through P03 (`stream_call` inside
  `retry_call`, behind a `ModelCaller` seam) → response handling: a `length` answer goes to the
  P04-17 length ladder, tool calls to the tool round (P04-13), text to the empty ladder /
  intent-ack check / final answer. Verdicts are applied here (`drop_scaffold`, `append`,
  `output_cap`, `wait`, `reasoning_off`).
- Exhaustion: one toolless **grace** request with a fixed "summarise what you did" user nudge
  (scaffold), retried once when empty, think blocks stripped; if that fails, a fixed limit message.
- Errors: P03 failures (`RetryFailed`) end the turn `provider_failed` with the classifier's
  retryable flag and reset time. Any other exception that escapes an iteration counts against a
  cap of 8 per turn; one raised only inside local processing modules (repair, wire) is a
  deterministic bug and ends the turn at once. Unanswered tool calls get error results first.
- **Every exit** passes `finalize_turn` (P04-16), which stamps `{turn_id, current_turn_user_index}`
  next to the final rows, sets `exit_reason` from the closed `ExitReason` enum and, for failures,
  `failure_reason` + `failure_retryable` (+ `resets_at`), and logs one INFO line (WARNING when
  abnormal).

Inputs → outputs: a `TurnState` (built by `new_turn_state`, see the 6b seam below) → `TurnResult`.

Files: `src/indus/core/loop.py`, `src/indus/core/turn_state.py`, `src/indus/core/exit_reasons.py`,
`src/indus/core/budget.py`, `src/indus/core/model_call.py` (the provider-call seam),
`tests/unit/core/test_loop.py`, `tests/unit/core/_loop_kit.py` (test harness).

Edge cases / failure modes:
- unlimited cap (`0`) never exhausts; restarts still capped.
- the grace call itself interrupted → `interrupted_by_user`.
- an exception after the assistant tool-call row was stored → closing error results, then retry.
- a provider failure after tools ran this turn → the stored rows end at tool results; the next turn
  starts with a user row (valid: tool → user).
- outer cancellation of the turn task (host shutdown) → `interrupted_by_system`, funnel still runs.

Our design differences / decisions:
- asyncio core; the provider call is a child task so an interrupt or redirect can cancel it alone.
- Mapping of P04-17 end verdicts onto the closed enum: `context_overflow` →
  `compression_exhausted`; `truncated`, `empty_response`, `repetition`, `thinking_exhausted` →
  `provider_failed` with that failure reason. Repeated outer errors and local bugs →
  `provider_failed` with reason `loop_error` / `local_error` (the enum has no internal-error value).
- Grace and max-iteration ends carry no `failure` (the turn produced an answer), but log WARNING.
- Not taken: codex app-server mode, MoA, scripted preludes, preflight compression (P08), fallback
  chains and credential pools (P08), run-budget wall-clock wrap-up (P08), voice route, review forks,
  per-turn CLI summary line.

6b seam (Agent, P04-20): the Agent keeps per-session pieces (`SessionRuntime`: prompt lifecycle,
heal ledger, args memo, model caller, store, registry/tool array, hydrated flag) and per turn calls
`new_turn_state(runtime, message=..., persist_text=..., history=..., turn_id=..., generation=...,
sink=..., broker=...)` then `await run_turn(state)`. Interrupt/steer/redirect go to
`state.control` (P04-19).

Tests we will write: plain answer (fake model); tool loop; 150 iterations → grace call; grace empty
twice → limit message; unlimited cap; 8 injected outer escapes → failed; local-module bug → ends
at once; restart cap; provider failure → retryable flag + reset time; every exit reason
(parametrised over all enum values) returns a result with the stamp and, for failures, reason +
retryable; one log line per turn, WARNING when abnormal.

## Decisions for the lead
- Suggest adding `internal_error` to `ExitReason` (protocol) so loop bugs are not reported as
  provider failures; until then `provider_failed` + `failure.reason = loop_error|local_error`.
- `LoopOptions` carries numbers without config keys except `agent.max_turns` and
  `agent.api_max_retries` (both exist).

Compare checklist (filled after the rewrite; reopened `agent/conversation_loop.py`,
`agent/iteration_budget.py`, `agent/turn_loop_errors.py`, `agent/turn_iteration_prep.py`
(iteration start, restarts), `agent/chat_completion_helpers.py` (grace), `agent/turn_final_response.py`):
- [x] loop condition incl. grace; iteration start increments; budget consume/refund (tests:
  `tests/unit/core/test_loop.py::test_cap_reached_gets_one_toolless_grace_call`,
  `::test_shared_budget_exhausted`, `::test_unlimited_cap`)
- [x] restart cap for refunding restarts; an unapplied redirect goes back as steer (test:
  `::test_restart_cap`)
- [x] grace: toolless, nudge, one retry on empty, think stripped, fixed fallback (tests:
  `::test_cap_reached_gets_one_toolless_grace_call`, `::test_empty_grace_twice_gives_the_limit_reply`)
- [x] outer error cap 8, local-module classification, unanswered calls closed (tests:
  `::test_eight_escaped_errors_fail_the_turn`, `::test_seven_escapes_then_success`,
  `::test_a_local_processing_bug_is_not_retried`, `::test_open_calls_are_closed_when_an_error_escapes`)
- [x] every exit through one funnel with the boundary stamp (test:
  `::test_every_exit_passes_the_funnel[*]`, 10 cases; `::test_random_turns_always_alternate[*]`, 40 seeds)
- [x] provider failures keep the classifier's retryable flag (tests:
  `::test_provider_failure_carries_the_classifier_verdict`, `::test_rate_limit_after_retries_is_retryable`)
- [x] text branch order: empty ladder → intent-ack → continuation stitch → repetition stop →
  final row stored (tests: `::test_length_ladder_continues_and_stitches`,
  `::test_thinking_only_is_fed_back_as_prefill`, `::test_intent_ack_gets_a_nudge`)
- Deliberate differences: the grace nudge is scaffold (never stored); outer-error cap does not
  shrink with small iteration caps; P04-17 end verdicts and loop bugs are mapped onto the closed
  enum as listed above.
- Not taken: interpreter-shutdown branch (asyncio), codex app-server, MoA, preludes, preflight
  compression, fallback chains, run-budget wrap-up, dropped-tool-call re-prompt, stop gates,
  reasoning-as-answer promotion, output transform hooks, CLI turn summary.

## Status

Done (2026-10-18). Files: `src/indus/core/loop.py`, `src/indus/core/turn_state.py`,
`src/indus/core/exit_reasons.py`, `src/indus/core/budget.py`, `src/indus/core/model_call.py`,
`tests/unit/core/test_loop.py`, `tests/fakes/turn.py` (harness). The test harness file is
`tests/fakes/turn.py` (not `tests/unit/core/_loop_kit.py`) so the integration suite shares it.
