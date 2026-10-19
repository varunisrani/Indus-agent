# Task: Side questions /btw (P10-23) Phase: 10 Owner: P10 slice K builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/side_question.py` (all).
Closed before writing.
Evidence: none.

What it does (own words): `/btw <question>` answers a question about the conversation without
changing it. With a live agent, a detached copy of its runtime replays the stored system prompt,
the stored transcript (cut back to the last finished assistant answer) and the same tool array, so
the provider's prefix cache is warm; the question rides as a new user message. Every tool call is
refused at dispatch, nothing is written to the session, at most 3 model calls are made (no grace
call after that), and usage goes to the session ledger under `side_question`. When
`auxiliary.side_question` names its own provider or model, the question goes there in one call
with a compact transcript instead. Without a live agent (process restarted) the stored transcript
is rendered as text (each message ≤ 2,000 chars, ≤ 24,000 in all, newest kept) for one auxiliary
call. It runs beside a running turn and never interrupts it.

Inputs → outputs: question + agent (or store + session id + aux client) → answer text.

Files: `src/indus/core/side_question.py`, `tests/unit/core/test_side_question.py`. Shared:
`LoopOptions.grace` (turn_state/loop) so the fork never makes a 4th call.

Edge cases / failure modes:
- Empty question → refused before any call.
- The fork's model calls a tool every time: three refused calls, then the answer says no answer
  came (no fourth request).
- A transcript tail that ends mid tool batch (turn running): cut back to the last complete
  assistant answer so alternation holds.
- Aux failure in the fallback path: raised to the caller, which shows one line.

Our design differences / decisions:
- The fork is a second `SessionRuntime` sharing the parent's caller (route, pool, fast mode),
  prompt text and tool array, with no store, no context engine, no fallback chain, no sink and an
  inline table that refuses every tool by name.
- The routed path and the fallback use the same text renderer; P09-15's digest helper (not on this
  base) can replace it at integration.
- Instructions to the model are our own short texts.

Tests we will write:
- fake model: answer returned, session row count unchanged, request prefix (system + history)
  byte-equal to the parent's;
- tool call refused and the 4th request never made;
- no live agent → fallback, 24,000-char budget, newest kept;
- runs while a turn is in flight without interrupting it.

## Decisions for the lead
- An empty fork answer returns a fixed "no answer" line instead of a second (one-shot) call.
- Intended difference: the fork is a copy of the runtime that refuses tools by name (no thread
  whitelist); fork failure falls back to the transcript path when an aux client exists.

Compare checklist (filled after the rewrite; reopened `agent/side_question.py`):
- [x] Snapshot cut back to the last finished assistant answer (`tests/unit/core/test_side_question.py::test_runs_beside_a_turn_in_flight_without_interrupting_it`, `test_render_transcript_rules`)
- [x] Same prompt, history and tools as the parent: warm prefix (`test_fork_answers_from_the_warm_prefix_and_writes_nothing`)
- [x] Every tool refused at dispatch, at most 3 calls, no grace call (`test_tools_are_refused_and_no_fourth_call_is_made`)
- [x] Usage under `side_question` in the parent's ledger (same test)
- [x] Fallback: 2,000 per message, 24,000 total, newest kept (`test_no_live_agent_falls_back_to_the_transcript`)
- [x] Gap found: the reference falls back to the one-shot path when the fork fails — added (`test_a_failing_fork_falls_back_to_the_transcript`)
- [x] Routed path via `auxiliary.side_question` (`test_routed_side_question_uses_the_aux_task`)
- Deliberate differences: an empty fork answer returns a fixed line instead of a second call; the routed path uses the transcript renderer (P09-15's digest helper is not on this base); no thread-scoped tool whitelist — the fork's inline table refuses every tool name.
- Not taken: separate max_tokens / temperature for the one-shot path (the aux task's settings apply).

## Status

Done (2026-10-19). Shared: `LoopOptions.grace` + `loop._no_grace` (with P10-21's loop edits). Surfaces: `/btw` through `service/automation.py` and `cli/slash/automation_cmds.py`; TUI and gateway call the same `SessionAutomation.command`.
