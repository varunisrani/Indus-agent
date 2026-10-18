# Task: D-04.1 async trial, harness and criteria (a) and (c) (P00-13) Phase: 00 Owner: P00 fakes/trial builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/chat_completion_helpers.py`
lines 2703–2760 (the streaming call object: a request worker thread plus a polling monitor sharing
mutable state, a per-request cancel flag, attempt counters and a holder for the live stream so a
foreign thread can close it), `agent/interrupt_control.py` (public method names only: interrupt,
hard interrupt, clear, steer, redirect, withdraw steer). Trap text R01.4 from
`docs/plan/research/01-agent-core.md` §6. Closed before writing.
Evidence: none.

What it does (own words): decides whether D-04.1 (asyncio-first core) holds before any product code
depends on it. The reference runs each model request on a worker thread and, on interrupt, abandons
the worker and shuts the socket down from another thread (R01.4). The async answer is: the turn is
one task, interrupt cancels it, the stream is an async generator that is closed on the way out, and
one exit funnel always seals history. The trial is a throwaway prototype under `tests/trials/`;
nothing under `src/` imports it.

Criteria (architecture §8, D-04.1) — all five must pass for D-04.1 to stand:
- (a) interrupt mid-stream closes the stream within 200 ms and leaves alternation valid. **P00-13.**
- (b) a tool command with a timeout is killed at timeout + 2 s while the loop stays responsive.
  **Open, P03-19.**
- (c) 50 concurrent fake sessions in one loop keep p99 event latency < 50 ms. **P00-13.**
- (d) a synchronous plugin hook that hangs is abandoned after its timeout without blocking other
  sessions. **Open, P03-19.**
- (e) a sync caller (classic REPL key handler) drives and interrupts a turn through a loop thread.
  **Open, P03-19.**
P03-19 finishes (b), (d), (e) and writes the decision input (keep D-04.1 or take the fallback).

Method:
- `harness.py`: `Session` owns history, a generation number, the current turn task and an event
  queue. `start_turn(user_text, source)` bumps the generation and starts a task. The turn reads an
  async-generator source of deltas (text fragments, tool-call fragments) and posts one event per
  delta. `interrupt(generation)` only acts if the generation is still current (a late interrupt
  must not hit the next turn), records the request time and cancels the task. Every way out —
  completed, interrupted, failed — goes through one funnel that closes the source, seals the
  assistant message (marked partial when interrupted), drops tool calls whose arguments never
  finished streaming, appends a synthetic result for every finished-but-unrun tool call, posts a
  `turn_end` event and records the exit time.
- Fake source: an async generator that gives way to the loop N times between deltas (via
  `tests.fakes.clock.checkpoint`, never a timed sleep), stamps each delta with
  `time.perf_counter_ns()` when produced and records when its `finally` ran (that is `aclose`).
- Surface consumer: one task per session dequeues events and records `dequeued − produced`.
- (a): wait (on an event, no sleep) until the consumer has seen 20 deltas, interrupt, await the
  turn. Measure interrupt → source closed and interrupt → funnel done; check alternation. 20 runs,
  every run must be ≤ 200 ms. Variants: plain text, and a finished tool call plus a half-streamed
  one (the finished one gets a synthetic result, the half one is dropped).
- (c): 50 sessions × 200 deltas, N = 2, all in one loop; p50/p99/max over the 10 000 latencies,
  p99 < 50 ms.
- Negative control for (a): a source that holds on to cancellation for 1 s before unwinding. To
  keep the no-sleep rule, the control runs on `FakeClock` time (the harness takes its time source
  as a parameter): the source waits on `clock.sleep(1.0)`, the test advances the clock by 1 s, and
  the harness must report a 1000 ms close and flag it as over budget.

Numbers to record: (a) max/p50 close and exit latency over 20 runs; (c) p50, p99, max event
latency and wall time for the whole fan-out. Measured on the shared 4-core build machine with other
agents running, so these are an upper bound.

Files: `docs/notes/P00/async-trial.md`, `tests/trials/__init__.py`,
`tests/trials/async_core/__init__.py`, `tests/trials/async_core/harness.py`,
`tests/trials/async_core/test_interrupt_latency.py`, `tests/trials/async_core/test_fanout_latency.py`,
`tests/trials/async_core/test_harness_rules.py` (generation binding, alternation checker, no `src/`
import).

Edge cases / failure modes:
- cancel lands while the turn is awaiting the source (normal) or between deltas; both close it.
- the task is cancelled by someone else (shutdown): the funnel still seals history, then the
  cancellation propagates instead of being swallowed.
- interrupt for an old generation: ignored, returns False.
- tool call cut mid-arguments: never kept, never "run" (R01.6 dropped mid-stream calls).

Our design differences / decisions:
- one task per turn and `Task.cancel()` instead of abandoned worker threads and socket shutdown.
- the interrupt bound to a generation is part of the API from day one.
- measurement uses `perf_counter_ns` only to measure; nothing waits on wall time.

Tests we will write: see Method; all trial timing tests carry `@pytest.mark.slow` and print their
numbers.

## Results

Machine: shared 4-core build container, Python 3.13.16, other agents active. The trial files were
run 6 times in a row (`uv run pytest tests/trials/async_core -q -s`): 6/6 green, 10 passed each.
Ranges below are over those 6 runs; each (a) run is itself 20 interrupts.

| Criterion | Measure | Threshold | Result (6 runs) |
|---|---|---|---|
| (a) cancel lands in the source | interrupt → source closed, max of 20 | ≤ 200 ms, 20/20 | p50 0.005–0.010 ms, max 0.010–0.036 ms; 120/120 within |
| (a) same | interrupt → exit funnel done, max of 20 | ≤ 200 ms | p50 0.013–0.025 ms, max 0.025–0.084 ms |
| (a) cancel lands in the turn, source closed via `aclose` | close / exit max | ≤ 200 ms | close max 0.013–0.061 ms, exit max 0.028–0.075 ms |
| (a) pending delta after a finished + a cut tool call | close / exit max | ≤ 200 ms | close max 0.011–0.021 ms, exit max 0.027–0.062 ms; alternation valid, cut call dropped, finished call answered by a synthetic result |
| (a) negative control (source holds cancellation 1 s, virtual time) | close | must be flagged | 1000.0 ms measured, flagged by `budget_violations` |
| (c) 50 sessions × 200 deltas, one loop | produced → dequeued latency over 10 000 events | p99 < 50 ms | p50 0.16–0.26 ms, p99 0.37–0.99 ms, max 0.75–4.3 ms; wall 75–122 ms for the whole fan-out |

Reading: both criteria pass with two to three orders of magnitude of headroom. Caveat for P03-19:
the fake source does no real I/O and no per-event work (no SSE parsing, no rendering, no store
writes), so (c) measures scheduling overhead only. P03-19 should repeat (c) with the real SSE
parser over `httpx.MockTransport` and a store write per event before making the final call.

Open for P03-19: (b) tool command killed at timeout + 2 s with the loop responsive; (d) hanging
sync plugin hook abandoned after its timeout; (e) a sync caller driving and interrupting a turn
through a loop thread; and the decision input (keep D-04.1 or take the thread-per-turn fallback).

## Decisions for the lead
- The negative control runs on virtual time (see Method) so the suite keeps the no-sleep rule.
- The event queue is unbounded in the trial; back-pressure (bounded queue, drop/coalesce policy)
  is a product decision for the service layer, not measured here.

Compare checklist (filled after the rewrite; reopened `agent/chat_completion_helpers.py`
2703–2760 and the method list of `agent/interrupt_control.py`):
- [x] The reference keeps a per-request cancel flag and a holder for the live stream so another
  thread can force it closed; here the stream is an async generator owned by the turn task, and
  cancelling the task closes it (both paths: cancel inside the source, and `aclose` from the turn).
  (tests: `test_interrupt_latency.py::test_interrupt_mid_text_stream`)
- [x] Interrupts are bound to a generation so a late interrupt cannot cancel the next turn (R01.4).
  (test: `test_harness_rules.py::test_stale_interrupt_does_not_cancel_the_next_turn`)
- [x] Partial delivery: the streamed text is kept as a sealed partial assistant message, a
  half-streamed tool call is dropped and never answered or run, a finished one gets a synthetic
  result (R01.6). (test: `test_interrupt_latency.py::test_interrupt_with_pending_delta_after_tool_calls`)
- [x] Shutdown cancellation (not our interrupt) still seals history and then propagates.
  (test: `test_harness_rules.py::test_foreign_cancel_still_seals_history_then_propagates`)
- Deliberate differences: no worker thread, no monitor polling loop, no socket shutdown from a
  foreign thread; one task and `Task.cancel()`.
- Not taken (later phases): steer / redirect / withdraw-steer and hard interrupt (P03 turn loop);
  "a redirect must not put raw reasoning into replayable content" (R01.4, P03); stale-stream
  detection (R01.5, P03); attempt counters and route-switch checks (P03).

## Status

Done for P00-13 (2026-10-18): harness, criteria (a) and (c) measured and passing, negative control
in place. P03-19 appends (b), (d), (e) and the decision input. No product code touched; nothing
under `src/` imports the trial (test: `test_harness_rules.py::test_product_code_does_not_import_the_trial`).
