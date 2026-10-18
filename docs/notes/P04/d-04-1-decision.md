# Task: D-04.1 criteria against the real P04 loop, and the decision (P04-23, second half) Phase: 04 Owner: wave-6b builder T

Read: none from the reference — own design (D-04.1 is our stack decision; the reference runs its
loop on threads, see `docs/notes/P00/async-trial.md` for what was read there). Ours:
`docs/plan/04-architecture.md` §3 row 3 and the D-04.1 row; `docs/plan/15-roadmap.md` risk 1;
`docs/plan/phases/P04-turn-loop-tool-contract.md` row P04-23; `docs/notes/P00/async-trial.md`
(P00-13 and P03-19 numbers, the baseline); `docs/notes/P04/{route-and-d04-1,blocking-helper,
turn-loop,interrupts,parallel-exec,persist-order}.md`; the landed code in `src/indus/core/`,
`src/indus/foundation/blocking.py`, `src/indus/tools/{dispatch,hooks}.py`, `tests/fakes/turn.py`,
`tests/fakes/model/`.
Evidence: none.

What it does (own words): the P00/P03 trial proved the asyncio-first shape on a throwaway
harness. This task runs the same criteria through the code that will ship: `run_turn` with its
prologue, iteration loop, tool round, parallel executor and exit funnel; P03's `RouteCaller`
(`retry_call(stream_call)`) over the fake model server; `TurnControl.interrupt`; `dispatch` with
the shared blocking helper; and, where it matters, the real P02 store (SQLite through its writer
thread). The decision is then recorded: keep the asyncio core, or take the threaded fallback
(`core/` + `providers/` threaded behind the same interfaces).

Inputs → outputs: `uv run pytest tests/integration/core/test_d04_1_criteria.py -s` → one
`D041 …` line per measurement (p50 / p99 / max in ms, counts) and a pass/fail per criterion.

Files: `tests/integration/core/test_d04_1_criteria.py`, `docs/notes/P04/d-04-1-decision.md`;
shared: `pyproject.toml` (the `perf` marker), `docs/plan/04-architecture.md` and
`docs/plan/15-roadmap.md` (D-04.1 marked decided); added once the runs found helper bugs:
`src/indus/foundation/blocking.py`, `tests/unit/foundation/test_blocking.py`.

Method (per criterion):
- (a) interrupt mid-stream: 20 fresh sessions per variant, one turn each, real P02 store. The
  sink sees the Nth `MessageDelta` and schedules `state.control.interrupt(generation)` on the
  loop (as the in-process host would). Measured: interrupt → the fake server's response body
  closed (`StreamRecord.closed_at`), and interrupt → `run_turn` returned (exit funnel and store
  writes done). Variants: Chat Completions text, Anthropic Messages text, a stream idle between
  frames, a stream cut after a finished tool-call fragment (never run), and an interrupt in the
  second model call after a completed tool round. Each run: exit `interrupted_by_user`, the live
  rows and the stored rows pass `check_alternation`. Negative control: a caller that holds on to
  cancellation for 300 ms must be flagged.
- (b) blocking tool with a timeout: a sync tool (`timeout_s` 1 s) that blocks its worker thread
  on an event; meanwhile a heartbeat task ticks every 10 ms and 4 other sessions stream turn after
  turn. Measured: tool started → tool result (abandoned), heartbeat gaps, frame → sink latency of
  the other sessions. Checked: abandoned within timeout + 2 s, the worker thread is still stuck
  afterwards (abandoned, not killed) and frees its slot once released, the turn goes on to the
  model's answer, rows alternate. Negative control: an async handler that blocks the loop for
  300 ms must show as a heartbeat gap.
- (c) 50 concurrent sessions × 200 deltas, one loop, one shared real P02 store handle, half Chat
  Completions and half Anthropic, server gives way to the loop before every frame; history loaded
  from the store (the Agent's path). Latency: the fake server's `on_frame` stamp → the session's
  event sink receiving the `MessageDelta` carrying the same tag. Checked: p99 < 50 ms, every
  session completed with the full text, stored rows alternate, and no `PoolFull` anywhere.
- (d) hanging sync hook: a `ToolHooks` seam that runs a synchronous plugin-style hook on the
  `hooks` pool with a 1 s deadline (what P13-17 will plug in). In one session a parallel batch of
  two read-only calls where the hook hangs for one of them; a second session runs a hooked tool
  call at the same time. Checked: the hung call is blocked (fail closed, R07.18) after its
  deadline; its sibling call and the other session finish long before; the heartbeat keeps
  ticking; then the pool-bound shape: with both `hooks` workers stuck, the next hooked call is
  refused at once (`PoolFull` → blocked), not queued.
- (e) sync caller through `InProcessHost`: written in parallel by another builder in
  `tests/integration/core/test_d04_1_sync_caller.py`; referenced here, not duplicated.

Runs: each timing test 5 times alone and once while the rest of the suite runs; machine load
(`uptime`) recorded with the numbers.

Edge cases / failure modes:
- the global `tools`/`hooks` pools are shared by the whole test process: every stuck worker is
  released at the end of its test and the test waits until the pool is idle again.
- a full pool refuses (`PoolFull`) rather than queues: watched in (c); if it bites, it is
  recorded and the fix proposed, the criterion is not loosened.
- no banned sleeps in tests: real-time waits go through `SystemClock.sleep`, blocking waits are
  `threading.Event.wait`.

Our design differences / decisions:
- measured at the event sink (the loop's real output seam), not at a surface queue;
- the interrupt is called on the loop thread, as `InProcessHost` marshals it (criterion e covers
  the cross-thread hop).

Tests we will write: `tests/integration/core/test_d04_1_criteria.py` — one test per criterion
(variants parametrized), plus the two negative controls; all `perf` + `slow`.

## Results

Machine: shared 4-core build container (Intel Xeon @ 2.10 GHz), Python 3.13, SQLite 3.45.1 (P02
store in DELETE journal mode on this host), other agents active. "Alone": each test id in its own
`pytest` process, 5 times (1-minute load 1.01–1.48). "Under suite load": the same 5 × per-id runs
while the rest of the suite (`pytest --ignore=<this file>`, 3570 passed, 3 skipped) ran in
parallel (load 1.56–2.71). Figures are the median over the 5 runs with the [min..max] range; each
(a) run is 20 interrupts, (c) is 10,000 deltas, (b) and (d) one long measurement each.

| Criterion | Measure | Threshold | Alone: median [range] | Under suite load |
|---|---|---|---|---|
| (a) Chat Completions text | interrupt → body closed, max of 20 | ≤ 200 ms, 20/20 | 0.18 ms [0.12..0.36] | 0.21 ms [0.15..0.32] |
| (a) Anthropic Messages text | same | same | 0.13 ms [0.12..0.17] | 0.19 ms [0.15..0.21] |
| (a) stream idle between frames | same | same | 0.65 ms [0.58..0.91] | 0.72 ms [0.24..0.86] |
| (a) cut after a finished tool-call fragment | same | same | 0.15 ms [0.14..0.72] | 0.17 ms [0.16..0.40] |
| (a) second model call, after a tool round | same | same | 0.14 ms [0.11..0.27] | 0.24 ms [0.18..2.96] |
| (a) all variants | interrupt → `run_turn` returned (exit funnel + SQLite writes), max of 20 | ≤ 200 ms | p50 2.8–3.4 ms; max 4.8–7.5 ms, worst 13.2 ms | worst 40.1 ms |
| (a) alternation | live rows and stored rows | valid | 500/500 valid; cut tool call never run; interrupted after a tool round: `user, assistant(calls), tool, assistant` | 500/500 |
| (a) control: caller holds cancellation 300 ms | close | must be flagged | 300.8 ms, flagged 10/10 | flagged 10/10 |
| (b) sync tool blocking its thread, `timeout_s` 1 s | tool started → tool result | ≤ timeout + 2 s | 1.008 s [1.002..1.013] | 1.003 s [1.002..1.008] |
| (b) heartbeat (10 ms ticks) meanwhile | longest gap | < 200 ms | 19.3 ms [16.2..28.4] | 22.1 ms [17.6..28.3] |
| (b) 4 streaming sessions meanwhile (≈150 turns, 15K deltas) | frame → sink p99 / max | p99 < 50 ms | 2.1 ms [1.8..2.3] / 6.0 ms [4.6..15.7] | 2.5 ms [2.2..2.8] / 7.8 ms [6.2..20.7] |
| (b) abandoned, not killed | worker after the turn | still stuck, freed on release | 5/5 | 5/5 |
| (b) control: async handler blocking the loop 300 ms | heartbeat gap | must be flagged | 306.6 ms, flagged 5/5 | flagged |
| (c) 50 sessions × 200 deltas, all 50 streams open at once, real store | frame → sink p50 / p99 / max | p99 < 50 ms | 1.79 ms / 5.6 ms [4.2..6.3] / 10.3 ms [6.5..14.6] | 1.82 ms / 5.3 ms [4.4..7.1] / 7.2 ms [6.2..12.4] |
| (c) same | completed / `PoolFull` / wall | 50/50, none | 50/50, 0, 1.0 s [0.94..1.1] | 50/50, 0, 1.1 s |
| (d) hook hangs, deadline 1 s | call → its call blocked (fail closed) | ≈ deadline | 1.008 s [1.008..1.013] | 1.008 s |
| (d) sibling call in the same parallel batch | ran after | well under the deadline | 8 ms | 8 ms |
| (d) another session's hooked call | turn done after | well under the deadline | 13 ms [13..18] | 15 ms |
| (d) both hook workers stuck | next hooked call refused (`PoolFull`, blocked) | at once, pool not grown | 4 ms, 2 threads | 5 ms, 2 threads |
| (d) heartbeat | longest gap | < 200 ms | 12.5 ms [11.0..20.2] | 12.6 ms |
| (e) sync caller through `InProcessHost` | — | ≤ 200 ms | `tests/integration/core/test_d04_1_sync_caller.py` (another wave-6b builder); P03-19 trial baseline: p50 0.13–0.27 ms, max ≤ 4.7 ms | — |

Also run: the whole file in one process, 8 times plus once under suite load: 9/9 green. In the 8
whole-file runs (b)'s heartbeat showed one gap of 50–105 ms per run (never alone: max 28 ms; an
alone run with GC callbacks saw no collection pause over 5 ms); still under the 200 ms bound, cause
not pinned (state left by the earlier tests in the same process is the likely source).

What broke first, and the fixes (`src/indus/foundation/blocking.py`, shared):
1. **(c) `PoolFull` bit.** First run, helper as landed in 6a, no start barrier: **40 of 50
   sessions ended `persistence_failed` before their first model call**: the prologue loads the
   transcript with `StateStore.load` → `run_sync` → the 8-worker `tools` pool, which refused the
   9th concurrent load ("all 8 workers of the tools pool are busy"). The 10 that ran had p99
   0.27 ms, so latency was never the problem; admission was. Fix (the lead's 6a suggestion): the
   `io` and `tools` pools let up to 256 calls wait for a worker in arrival order; the wait counts
   against the caller's deadline; a call whose caller gave up (deadline or cancellation) while
   waiting never runs; past 256 waiting, `PoolFull` as before. `hooks` still refuses at once.
   Pools still never grow. Unit tests:
   `tests/unit/foundation/test_blocking.py::test_waiting_pool_queues_in_order_then_refuses_past_its_limit`,
   `::test_waiting_call_past_its_deadline_or_cancelled_never_runs`, `::test_named_pools_wait_except_hooks`.
2. **(d) a finished worker still counted as busy.** In 1 of 5 alone runs another session's hook
   was refused with `PoolFull` while only one of the two hook workers was stuck: the worker
   handed the result back to the loop before releasing its slot, so a caller that resumed at
   once and called again could find the pool "full". Fix: the worker releases its slot before
   the hand-back. Unit test `::test_worker_is_free_again_by_the_time_its_caller_resumes` (2,000
   back-to-back calls on a one-worker pool; fails 3/3 with the old order).
3. Start/abandon hand-off made atomic under the pool lock, so a call is either run or reported
   "will not run", never both.

Not fixed (recorded, proposal below): with one hook stuck, the `hooks` pool has one free worker; a
hooked call that arrives while that worker is briefly in use (here: session B started the moment
A's hook hung, racing A's sibling call's hook) is refused, and because the non-empty seam fails
closed the tool is **blocked**, not delayed. That happened in 4 of 5 runs of the first version of
(d); the final test starts B once A's sibling has run, so (d) measures the hang alone. The same
shape bites without any stuck hook: a parallel batch of three or more hooked calls already
exceeds two workers.

## Verdict

**Asyncio core kept (D-04.1 stands); the threaded fallback is not taken.** Builder's verdict,
for the lead to confirm. On the real P04 loop (a)–(d) pass in 50/50 alone runs and 50/50 under
suite load, with one to two orders of magnitude of headroom: interrupts close the provider stream
in well under 1 ms (200 ms budget) and the turn is sealed and stored within ~3 ms; a blocking tool
is abandoned at its timeout while the loop keeps a 10 ms heartbeat and four other streams
running; 50 simultaneous streams through the real P02 store keep p99 at ~5 ms (50 ms budget); a
hung sync hook costs one hook worker and its own call, nothing else. (e) is covered by
`tests/integration/core/test_d04_1_sync_caller.py`; if it fails, this verdict is reopened. The
failures found were admission bugs in the blocking helper (fixed above), not in the asyncio model;
a threaded core would need the same bounded pool for the same work.

What P05 must add:
- **The terminal timeout backstop for (b).** This suite's (b) uses a fake blocking tool on a
  worker thread: it is abandoned at its timeout, never killed. A child process must be: P05's
  terminal runner sends SIGTERM to the process group at the timeout and SIGKILL 2 s later, and
  P05 adds its own (b) test against the real `terminal` tool and local backend, with the
  heartbeat and streaming neighbours used here (trial baseline, P03-19: a SIGTERM-ignoring child
  with a 2 s timeout gone at 4.003–4.009 s).
- Tools that can hang are written as async (subprocess or async I/O), not as sync handlers: a sync
  handler past its deadline keeps a `tools` worker until it returns.

## Decisions for the lead
- Helper changes in `src/indus/foundation/blocking.py` (shared): bounded waiting for `io`/`tools`
  (`POOL_QUEUE_LIMITS`, 256 each, one constant table, easy to tune or move to config), slot
  released before hand-back, atomic start/abandon. `hooks` keeps refusing (6a's decision).
- **Proposal for P13-17 (hooks):** give `hooks` a wait queue too (`POOL_QUEUE_LIMITS["hooks"]`):
  the hook deadline bounds every wait, so a stuck hook cannot build an unbounded backlog, and
  ordinary concurrency (a parallel batch of 3+ hooked calls, two sessions while one hook is stuck)
  is delayed instead of blocked; consider sizing `hooks` to `ExecLimits.max_workers` (8) and
  disabling a hook after repeated timeouts. Not changed here.
- **Proposal (persist, not changed):** `StateStore.load` runs on the `tools` pool, shared with
  untrusted sync tool handlers, with no deadline; move it to `io` with a deadline (e.g. 30 s) so
  eight stuck tool handlers can never stall a session's transcript load.
- Latency is measured at the turn's event sink (the loop's output seam), not at a surface queue;
  (c) holds every stream on a 1 s fake-clock stall until all 50 are open, otherwise the store's
  writer thread staggers the prologues and the streams barely overlap (first version: 1.36 s
  wall, p99 0.19 ms — not a 50-way test).
- `perf` marker added to `pyproject.toml`; the file takes ~9 s and runs in the default lane. A
  separate perf lane would be `-m "not perf"` plus a `-m perf` job.
- `docs/plan/16-what-we-change.md` has no D-04.1 row, so it is unchanged; the decision is marked
  in `docs/plan/04-architecture.md` (D-04.1 row) and `docs/plan/15-roadmap.md` (risk 1).

Compare checklist (own design: compared against the P00/P03 trial it replaces):
- [x] (a) on the real loop (test: `tests/integration/core/test_d04_1_criteria.py::test_a_interrupt_mid_stream_closes_within_200ms`)
- [x] (b) on the real loop (test: `::test_b_blocking_tool_abandoned_while_loop_stays_responsive`)
- [x] (c) on the real loop (test: `::test_c_fifty_sessions_keep_p99_event_latency_under_50ms`)
- [x] (d) on the real loop (test: `::test_d_hanging_sync_hook_abandoned_without_blocking_others`)
- [ ] (e) on the real host (test: `tests/integration/core/test_d04_1_sync_caller.py`, other builder;
  not run here)
- [x] negative controls kept from the trial and moved onto the real loop: (a) a caller holding
  cancellation (`::test_a_control_a_caller_holding_cancellation_is_flagged`), (b) a handler
  blocking the loop (`::test_b_control_a_handler_blocking_the_loop_is_flagged`)
- [x] gap found: concurrent transcript loads refused by a full pool — fixed by bounded waiting
  for `io`/`tools`, pinned by `tests/unit/foundation/test_blocking.py::test_waiting_pool_queues_in_order_then_refuses_past_its_limit`
- [x] gap found: a worker counted busy after its result was handed back — fixed by releasing
  first, pinned by `tests/unit/foundation/test_blocking.py::test_worker_is_free_again_by_the_time_its_caller_resumes`
- Deliberate differences from the trial: the trial's (d) pool used a joining executor and a
  semaphore; the product helper uses its own daemon workers; (b) measures a thread abandoned by
  `dispatch`, not a child process (P05).
- Not taken: the trial's (b) child-process kill (P05's terminal runner) and (e) (other builder).

## Status

Done for (a)–(d) and the verdict (2026-10-18), pending the lead's confirmation and (e) from the
parallel builder. Changed outside the task's files: `src/indus/foundation/blocking.py` and
`tests/unit/foundation/test_blocking.py` (helper fixes and their tests), `pyproject.toml` (`perf`
marker), `docs/plan/04-architecture.md` and `docs/plan/15-roadmap.md` (D-04.1 marked decided),
`docs/notes/P04/route-and-d04-1.md` and `docs/notes/P04/blocking-helper.md` (one status line each).
