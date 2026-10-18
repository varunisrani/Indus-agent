# Task: Interrupt, steer, redirect (P04-19) Phase: 04 Owner: wave-6a builder X

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/interrupt_control.py` (whole),
`agent/conversation_loop.py` (`_apply_active_turn_redirect`), `agent/agent_runtime_helpers.py`
(the post-batch steer drain), `agent/turn_iteration_prep.py` (the pre-request steer drain),
`agent/prompt_builder.py` (steer row shape), `agent/tool_executor.py` (skip/cancel wording paths).
Test names only of `tests/agent/test_steer.py` for the edge-case list. Spec: research
`01-agent-core.md` §3.10, trap R01.4. Closed before writing.
Evidence: none.

What it does (own words): every turn runs as an asyncio task with a **generation** number;
`TurnControl` is how other code reaches it.

- `interrupt(generation, *, hard=False, by_system=False)`: ignored (returns False) when the
  generation is not the running turn's. Otherwise it cancels the in-flight provider call (P03
  closes the HTTP stream as the task is cancelled), withdraws open server requests (one
  `server_request.cancelled` event each), and makes the executor give unstarted tools `skipped`
  results and running ones `cancelled` results. The funnel stores sealed partial text. A hard
  interrupt also drops buffered steer text.
- `steer(text, ticket=None)`: buffers text (several join with newlines). It is delivered as its own
  `user` row with `display_kind=steer`, wrapped in the steer markers, after the current batch: at
  the post-batch drain, at the pre-request drain (only right after tool results), and leftovers at
  finalize become the next turn's message. It is never written onto a stored tool row.
  `withdraw_steer(ticket)` takes back exactly that submission when no drain consumed it.
- `redirect(text)`: during a model call it cancels only that call, keeps every finished row, adds
  an assistant checkpoint (the visible streamed text, or a hidden placeholder whose `api_content`
  is a neutral note) and a real user row: `content` is the correction, `api_content` carries the
  interruption note plus the partial visible text (never reasoning; a repetition loop's text is
  left out) and the correction. The loop then retries (a refunded restart). During tool execution
  it degrades to `steer`. With no live turn (or a hard stop pending) it returns False. Several
  redirects before the loop applies them are kept apart with a separator line.

Interrupt effect points: iteration start, provider call (incl. P03 backoff waits, which run inside
the call task), a verdict wait, the tool batch, the grace call.

Inputs → outputs: method calls on the loop thread → flags, cancelled tasks, rows, events.

Files: `src/indus/core/interrupts.py`, `tests/unit/core/test_interrupts.py`.

Edge cases / failure modes:
- interrupt before the turn bound its call task → the flag is seen at the next effect point.
- steer during a hard interrupt → dropped; during an ordinary interrupt → kept as leftover.
- redirect after the answer completed → False (the surface queues a new turn).
- hard stop wins over a redirect arriving at the same time.
- a steer accepted just before a redirect lands before the correction.

Our design differences / decisions:
- No per-thread interrupt bits: asyncio cancellation of the call task and the tool tasks. Calls are
  made on the loop thread; P04-21's in-process host marshals from other threads.
- The server-request broker is reached through `indus.protocol.requests_port.ServerRequestPort`
  (`indus.service` sits above `indus.core` in the layer contract).
- Not taken: codex native steer, child-agent fan-out (delegation phase), liveness-generation
  claims for watchdog aborts, terminal "yield" requests.

Tests we will write: fake streaming model: interrupt mid-stream → stream closed promptly, valid
alternation; stale generation ignored; steer during tools → separate row after results; steer
during hard interrupt dropped, ordinary interrupt kept; redirect never stores reasoning text;
redirect during tools becomes steer; redirect with no live turn is False; ticket withdraw; pending
server requests cancelled with one event each.

## Decisions for the lead
- Broker: the lead moved the port to `indus.protocol.requests_port` (`ServerRequestPort`:
  `cancel_all(reason) -> int`, `human_wait_seconds() -> float`); `TurnControl` uses it, and the
  broker itself emits the `server_request.cancelled` events. The stand-in copy of that file in
  this worktree is to be dropped at merge.

Compare checklist (filled after the rewrite; reopened `agent/interrupt_control.py`,
`_apply_active_turn_redirect`, both steer drains, the steer row helper; `tests/agent/test_steer.py`
test names only):
- [x] generation guard; hard vs soft; steer dropped only on hard (tests:
  `tests/unit/core/test_interrupts.py::test_stale_generation_is_ignored`,
  `::test_hard_interrupt_drops_steer_ordinary_keeps_it`, `::test_steer_buffer_and_tickets`)
- [x] interrupt closes the stream promptly; running tools cancelled, unstarted skipped; requests
  withdrawn (tests: `::test_interrupt_mid_stream_closes_the_stream_quickly`,
  `::test_interrupt_during_tools_cancels_running_and_skips_the_rest`, `::test_interrupt_before_the_first_call`)
- [x] steer drain points and row shape; never on a tool row; stored and recognised on reload
  (tests: `::test_steer_during_tools_becomes_its_own_row_after_the_results`,
  `::test_steer_during_a_text_answer_is_left_for_the_next_turn`, `::test_concurrent_steers_keep_all_text`)
- [x] redirect: only the model call; checkpoint rules; no reasoning; repetition dropped; tools →
  steer; no live turn → False; multiple redirects kept apart; hard stop wins (tests:
  `::test_redirect_keeps_partial_text_and_never_stores_reasoning`,
  `::test_redirect_before_any_text_uses_a_hidden_placeholder`, `::test_redirect_during_tools_becomes_steer`,
  `::test_redirect_without_a_live_call_is_refused`, `::test_redirect_rows_shapes`)
- [x] gap found: a steer accepted just before a redirect must land before the correction — the
  redirect now drains it first, pinned by `::test_steer_accepted_before_a_redirect_lands_before_the_correction`
- [x] gap found: an ordinary interrupt during tools delivered buffered steer as a row the model
  never saw — now it stays as `pending_steer`, pinned by `::test_hard_interrupt_drops_steer_ordinary_keeps_it`
- Deliberate differences: cancellation of asyncio tasks instead of per-thread interrupt bits; the
  pre-request drain only runs right after tool results (otherwise the text waits for the next
  batch or the next turn).
- Not taken: codex native steer, child-agent fan-out, liveness-generation claims, tool "yield".

## Status

Done (2026-10-18). Files: `src/indus/core/interrupts.py`, `tests/unit/core/test_interrupts.py`;
stand-in: `src/indus/protocol/requests_port.py` (slice Z's file copied, first line marks it).
