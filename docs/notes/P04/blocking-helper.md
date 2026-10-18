# Task: the one D-04.1 blocking helper (P04-23, wave 6a) Phase: 04 Owner: wave-6a builder W

Read: none — own design (D-04.1). The reference runs its loop on threads and has no single helper
of this kind to compare against; the shape comes from our plan (architecture §3 row 3, D-04.1),
the P00/P03 trial (`tests/trials/async_core/driver_parts.py` `BoundedHookPool`,
`test_sync_hook_abandon.py`; `docs/notes/P00/async-trial.md`), the lead-approved proposal
("Proposal: the one D-04.1 blocking helper" in the route-binding task note) and the two private
helpers it replaces (`tools/dispatch.py` `run_sync`, `core/prompt/context_files.py`
`_start_read`/`read_bounded`).
Evidence: none.

What it does (own words): the core is asyncio-first, so anything that blocks — a file read, a
synchronous tool handler, a synchronous plugin hook — must run off the event loop, and a call that
never comes back must not hold up the turn, other sessions or process exit. `run_blocking` hands
the call to a small named pool of daemon worker threads, waits for it without blocking the loop,
and gives up at a deadline: the caller gets `DeadlineMissed` (also a `TimeoutError`), the worker
thread is left to finish on its own (Python cannot kill a thread), its slot stays taken until it
does, and whatever it produces later is swallowed. The profile `Scope` and every other context
variable travel into the worker (R01.16). A pool never grows: when every worker is busy (or stuck)
new work is refused at once with `PoolFull`, so a burst of stuck hooks cannot turn into hundreds
of threads.

Inputs → outputs:
- `BlockingPool(name, workers)`; `await pool.run(fn, *args, deadline=None, clock=None, label="")
  -> T`; `pool.busy`, `pool.threads`, `pool.workers`, `pool.shutdown()`.
- `POOLS`: read-only mapping `io` (8, file reads), `tools` (8, sync tool handlers), `hooks` (2, sync
  plugin hooks — the size the P00 trial measured); threads start lazily, one per concurrent call.
- `await run_blocking(fn, *args, pool="io" | BlockingPool, deadline=None, clock=None, label="")`.
- Errors (rows in `foundation/errors.py` `ERROR_CODES`, classes here): `BlockingError`
  (`blocking.error`), `PoolFull` (`blocking.pool_full`), `PoolClosed` (`blocking.pool_closed`),
  `DeadlineMissed` (`blocking.deadline_missed`, also a `TimeoutError`).

Files: `src/indus/foundation/blocking.py`, `tests/unit/foundation/test_blocking.py`; changed:
`src/indus/tools/dispatch.py` (`run_sync` → thin wrapper), `src/indus/core/prompt/context_files.py`
(private thread helper removed, `read_bounded` uses the `io` pool); shared:
`src/indus/foundation/errors.py` (four `ERROR_CODES` rows).

Edge cases / failure modes:
- deadline `None` waits for ever; a negative deadline is a `ValueError`.
- deadline on an injected `Clock` (fake in tests) or, with no clock, on loop time.
- result and deadline arrive in the same loop iteration → the result wins.
- caller cancelled (interrupt) → `CancelledError` propagates at once; the work is abandoned the
  same way as at a deadline; work that has not started yet is skipped by its worker.
- the abandoned call later returns or raises → nothing reaches the caller or the loop's
  "exception never retrieved" handler; the loop may even be closed by then (the hand-back is
  dropped quietly).
- every worker busy → `PoolFull` before any thread is touched; abandoned work keeps its slot until
  it really returns, so a pool of stuck calls refuses everything (criterion d: refuse, not grow).
- `shutdown()` never joins: idle workers are told to stop, stuck ones are left (daemon threads do
  not hold process exit); `run` after shutdown → `PoolClosed`.
- an exception from `fn` is re-raised in the caller as the same object, traceback included.
- one WARNING per abandonment (label, pool, deadline, busy/size); a late result is not logged
  again.

Our design differences / decisions:
- Own daemon threads instead of `concurrent.futures.ThreadPoolExecutor`: executor threads are
  joined at interpreter exit, so one stuck call would hang shutdown.
- Full pool refuses (`PoolFull`) instead of queueing — the proposal's choice and criterion d's
  wording; callers map it into their own result (tool error, skipped context file).
- `DeadlineMissed` is both an `IndusError` (stable code) and a `TimeoutError`, so existing
  `except TimeoutError` callers (dispatch) keep working.
- `run_blocking` takes positional `*args` for `fn` (the proposal had a zero-argument callable
  only), so callers need no lambda or `partial`.
- Pool sizes are module constants, not config keys: the proposal names fixed sizes and no keys.

Tests we will write (`tests/unit/foundation/test_blocking.py`):
- value returned from a worker thread, args passed, loop not blocked;
- scope and a plain context variable travel into the worker; worker changes do not leak back;
- exception re-raised with the worker frame in its traceback;
- deadline on a fake clock: a heartbeat task keeps ticking while waiting; `DeadlineMissed` (a
  `TimeoutError`) at the deadline; the thread is still stuck and keeps its slot; slot freed when it
  returns; logged once;
- deadline on loop time (no clock), same shape;
- pool of 2: two stuck calls accepted, the third refused with `PoolFull`, thread count stays 2;
  threads reused after release;
- criterion d shape: a hung sync hook abandoned at its deadline while a second hook call
  completes, and a later call still works;
- caller cancellation abandons and drains; late exception never reaches the loop's handler, not
  even after the loop closed;
- `shutdown()` returns at once with a stuck worker; `run` after it → `PoolClosed`;
- named pools exist with their sizes; unknown name refused; negative deadline refused.

## Decisions for the lead
- No config keys for pool sizes (the proposal names none); `POOL_SIZES` is one constant table, easy
  to move into `foundation/config/schema.py` later.
- A full pool refuses. With 8 `tools` workers, the ninth concurrent sync tool call in one process
  gets `Tool failed: PoolFull: ...`; with 8 `io` workers a ninth concurrent context-file read is
  skipped with a warning. If 50-session load (criterion c) shows refusals, raise the sizes or let
  `io` wait for a slot up to the deadline.
- `dispatch` keeps its deadline on loop time (as before), not the scope clock: existing callers
  give `ToolContext` without a scope or with a fake clock that tests never advance.
- Two WARNING lines when a context file read misses its deadline (the helper's "abandoned" line
  and the prompt's "skipping it" line): they say different things; drop one if it is noise.

Compare checklist (own design: compared against the two helpers it replaces and the trial pool):
- [x] dispatch: sync handler runs off the loop with the scope (test:
  `tests/unit/tools/test_dispatch.py::test_sync_handler_runs_off_the_loop_with_scope`)
- [x] dispatch: past its deadline → `timeout` error result (test:
  `tests/unit/tools/test_dispatch.py::test_sync_handler_past_deadline_is_an_error`)
- [x] context files: slow read abandoned at the deadline on the scope clock, never joined (test:
  `tests/unit/core/prompt/test_context_files.py::test_slow_read_hits_deadline`)
- [x] context files: missing / unreadable file → None (test: `::test_read_errors_count_as_empty`)
- [x] trial pool: stuck hooks keep their slots, extra work refused, pool never grows (test:
  `tests/unit/foundation/test_blocking.py::test_full_pool_refuses_and_never_grows`)
- [x] trial pool used a joining executor; ours never joins (test: `::test_shutdown_never_waits_on_abandoned_work`)
- Deliberate differences: the context-file helper started one thread per read with no bound; it
  now shares the bounded `io` pool.
- Not taken: child processes (P05's terminal runner keeps its SIGTERM/SIGKILL backstop).

## Status

Done (2026-10-18). Helper built, both private helpers moved onto it; `ERROR_CODES` rows added in
the shared `foundation/errors.py`.
