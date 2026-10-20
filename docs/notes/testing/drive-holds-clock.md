# Task: the test driver holds the fake clock while pool work runs (harness fix) Phase: testing Owner: builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none (test-harness fix in our own
fakes and one observable on our own blocking pool; the reference has no fake-clock driver).
Evidence: none. Background: commits 077ef96 (test_batch stale heartbeat count) and 0e9a538
(learning review tests).

What it does (own words): `tests/fakes/turn.py::drive` (and `tests/fakes/agent.py::run_driven`,
`TurnKit.turn`, which use it) lets a task run and moves the `FakeClock` by `step` every time the
task has not finished after 0.5 ms of real time. "Not finished after 0.5 ms" was taken to mean
"the loop is idle, waiting on the clock". That is false when real work runs off the loop: a
synchronous tool on a `tools` pool worker, any other `run_blocking` call, a subprocess wait on the
`terminal` pool. The loop looks idle, the clock races ahead (about 2000 fake seconds per real
second), and any deadline on the injected clock raced against that work (the executor's 420 s call
deadline, the 600 s liveness watchdog, the batch heartbeats) can expire within a fraction of a real
second on a loaded machine. Two tests failed this way today and were fixed locally (077ef96,
0e9a538); every other `drive` user has the same latent race.

The fix: while a `BlockingPool` call is in flight and its caller still awaits it, `drive` does not
move the clock; it keeps yielding in short real-time waits until the call is back. Fake time then
moves only while the loop really is waiting on it.

Inputs → outputs: `drive(task, clock, step)` → same contract (returns when `task` is done, raises
`AssertionError` when it never finishes), plus: raises `AssertionError` naming the hold when one
stretch of held time passes `HOLD_LIMIT_S` real seconds (a stuck or deadlocked pool call fails the
test instead of hanging it).

Files: `src/indus/foundation/blocking.py` (shared: `awaited_calls()`, `awaiting_off_loop()` /
`OffLoopWait`), `src/indus/state/writer.py`, `src/indus/core/prompt/lifecycle.py`,
`src/indus/cron/output.py` (shared: one `with awaiting_off_loop()` each), `tests/fakes/turn.py`
(`drive`), `tests/fakes/clock.py` (`sync_sleepers`), `tests/unit/cron/test_output.py` (one test's
wait), new `tests/unit/fakes/test_drive.py`, `tests/unit/foundation/test_awaited_calls.py`, this
note.

Design:
- Product side, `indus.foundation.blocking`: one process-wide int, the number of off-loop calls
  whose caller is still awaiting them. `BlockingPool.run` counts a call from the moment the pool
  accepts it until its caller stops waiting (it returned or raised, missed its deadline, or the
  caller was cancelled). Abandoned work (its thread runs on) is not counted: nobody waits on it,
  and counting it would freeze fake time for ever. Read with `awaited_calls()`. No behaviour
  change; cost is a lock and an add per call; nothing in the product reads it.
- `awaiting_off_loop()` (an `OffLoopWait`) counts the same way for off-loop work that does not
  use a pool: `with awaiting_off_loop(): await ...`. Its `release()` is idempotent and may run on
  the worker thread, so a thread can take itself off the count the moment it is done (needed
  where the caller then polls on the clock to notice, as cron's `bounded` does).
- Test side, `drive`: before each `advance`, if `awaited_calls()` is above the number of threads
  parked in `FakeClock.sleep_sync` (a pool job waiting on the fake clock must still see it move),
  skip the advance and wait another 0.5 ms. The 200 000-round cap now counts advances only; holds
  are bounded separately by `HOLD_LIMIT_S` = 30 s of real time per uninterrupted hold, which
  raises `AssertionError` naming the count.
- `FakeClock.sync_sleepers`: count of parked `sleep_sync` callers (thread sleepers).

Coverage (a probe plugin wrapped `drive` and logged every executor submit, thread start and
`Popen` made while it ran, over all 43 `drive` user files):
- `BlockingPool` (`tools`, `io`, `terminal`, `hooks` and private pools): counted. This includes
  terminal commands (the `drain` reader on the `terminal` pool) and git in worktrees (`io`).
- State writer `awrite` (its own one-thread executor; 747 submits in that run): counted via
  `awaiting_off_loop`. A sqlite write that blocks under load no longer moves fake time; its lock
  retries pause with `sleep_sync` on a fake clock, which does not hold.
- `SessionPrompts.load` (`asyncio.to_thread`, 5 calls): counted via `awaiting_off_loop`.
- cron `output.bounded` (a daemon thread polled on the clock with a 10 s default budget; 48
  starts): counted, the thread releases itself before it signals done. Before, 5 ms of real
  pruning time under drive used the whole 10 s budget and the step was "left running".
- Not counted: asyncio subprocesses (shell hooks: the one drive user,
  `test_pre_llm_call_turn.py::test_shell_hook_context_through_a_turn`, already gives its hook bus
  a real-time deadline (`clock=None`); gateway `!` commands, WhatsApp sidecar and computer-use
  driver have no drive user), other `to_thread` / `run_in_executor` sites (dashboard auth, PTY
  bridge, BlueBubbles/SimpleX file reads: no drive user). `Popen` on the loop thread
  (`backends/local.py` spawn) is a short blocking call on the loop itself, so it cannot race.

Edge cases / failure modes:
- A pool job that itself waits on the fake clock through `sleep_sync`: not a hold (see above).
- A pool job that waits on the loop, which waits on the fake clock (a background reader such as
  the terminal `drain` while the poll loop sleeps on the clock; a sync handler that calls back into
  the loop): the hold never ends; the test fails after `HOLD_LIMIT_S` with a message naming the
  count. Such a test must wait on something real instead (listed in Status if any).
- Pools in other threads or other loops (a gateway test running a second loop) count too: the
  counter is process-wide on purpose, the fake clock is shared by whatever the test wires.
- Off-loop work outside a pool is counted only where wrapped (see Coverage).
- A test that simulates a step stuck in real time and expects a fake deadline to pass while it
  is stuck cannot use `drive`: it moves the clock itself (`test_output.py`, see Status).

Risks:
- A test that relied on fake time moving while a pool call was in flight now hangs for up to 30 s
  and then fails; found by running every `drive` user (results in Status).
- Residual: a long chain of steps on the loop itself, on a starved machine, still lets the
  clock move between steps (each 0.5 ms wake advances it). Holding while `loop._ready` is
  non-empty would close that, but was tried and rejected: some drive users keep a callback ready
  at all times (`test_idle_queue.py::test_runner_defers_reviews_on_a_local_server` held for 30 s
  and failed), so it needs its own pass.
- Speed: the hold waits only as long as the real work takes; the 43 drive files ran in 55 s idle,
  70 s under three CPU spinners.

Tests we will write:
- `tests/unit/fakes/test_drive.py`: a slow pool job (real 0.3 s) under a 420 s clock deadline
  sees no fake time pass and finishes; the clock still moves with nothing off the loop and
  between pool calls; a pool job parked in `sleep_sync` is released; an abandoned call no longer
  holds; a stuck call fails after the hold limit (patched to 0.2 s).
- Probe (same file): `run_driven` + a real `Agent` turn calling a sync tool that spends 1 s of
  real time on its `tools` worker. With the hold disabled it fails every time with the original
  message ("grind did not finish within 420 s and was abandoned"); with it, the result comes back.
- `tests/unit/foundation/test_awaited_calls.py`: counted while awaited; not after a return, a
  raise, a missed deadline, a cancel or a `PoolFull` refusal; `OffLoopWait.release` from another
  thread, once.

## Decisions for the lead
- `HOLD_LIMIT_S` = 30 s real per hold: long enough for a loaded machine's real pool work (git,
  sqlite, file I/O), short enough that a stuck job fails well inside a CI timeout. Change in
  `tests/fakes/turn.py`.
- Only `tests/fakes/turn.py::drive` changes. The ~30 per-file `drive`/`_drive` helpers elsewhere
  in tests (backends conftest, gateway adapters, providers) keep their own loops; they can adopt
  `awaited_calls()` the same way if they show the race (follow-up).

Compare checklist: n/a (no reference counterpart).

## Status

Done (2026-10-20). One drive user relied on time moving while real work ran:
`tests/unit/cron/test_output.py::test_hung_cleanup_is_abandoned_at_ten_seconds` (a cleanup step
stuck on a real event, 10 s fake budget). It now moves the clock itself in 0.5 s steps instead of
`run_driven`; assertions unchanged. No other test changed. The landed local fixes (077ef96,
0e9a538) stay. Follow-up for the lead: the per-file `drive` / `_drive` / `pump` helpers in about
30 other test files (backends conftest, gateway adapters, providers, terminal rig) do not hold the
clock; they can read `awaited_calls()` the same way if they show the race. A hand-rolled loop of that
kind failed once in the full gate under four concurrent full suites:
`tests/unit/cron/test_ticker.py::test_daemon_fires_an_every_30m_job_twice_in_an_hour` (its own
loop advances 10 s per 0.5 ms before it calls `run_driven`; the first run ended so late that the
second slot fell past the 61:30 window; log: "cron cleanup output pruning ... took longer than
10 s"). Not changed by this task (not reproduced: 5/5 alone, 19/19 under 3 or 12 CPU spinners, 5/5
with injected pool, prune and sqlite delays).
