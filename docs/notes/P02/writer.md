# Task: single writer (P02-03) Phase: 02 Owner: P02-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state.py` (`_execute_write`
1006–1200, `_sleep_before_write_retry`, `_try_wal_checkpoint`, `_reopen_after_close_locked`),
test names in `tests/hermes_state/test_write_lock_patience.py` and
`test_wal_checkpoint_strategy.py`. Closed before writing.
Evidence: none.

What it does (own words): every mutation of the store goes through one `Writer` per database path
per process. `write(fn, budget=...)` takes an in-process mutex, opens `BEGIN IMMEDIATE`, runs
`fn(conn)`, commits, and rolls back on any exception. A sibling process holding the write lock is
normal (VACUUM, checkpoint, a long migration): the writer waits it out with jittered retries until
a *time* budget runs out, never after a fixed number of attempts, so a healthy store does not
lose a turn. The whole callback is re-run on retry, so callbacks must be idempotent and must not
commit. Only when the budget is spent does it raise `StoreLocked`, whose text says plainly that
this is contention, not damage. Every 50 successful writes it asks SQLite for a PASSIVE
checkpoint (never TRUNCATE on this path: TRUNCATE under load tore pages on big files, R03.3).
Async code calls `await writer.awrite(fn)`, which runs the same write on a single-thread
executor owned by the writer so the event loop never blocks.

Inputs → outputs: `Writer(path, conn, *, reopen, clock, sleep=None, rng=None, guard=None)`;
`write(fn, *, budget=ROUTINE) -> T`; `awrite(fn, *, budget=ROUTINE) -> T`;
`borrow()` (context manager: the writer connection under the mutex, for read fallbacks);
`add_periodic(every, fn)` (hooks after N successful writes: P02-14's FTS merge);
`set_fts_fail_open(fn)` (P02-14); `checkpoint_passive()`; `close(checkpoint=True)`.
Budgets: `ROUTINE` 20 s, `TRANSCRIPT` 60 s, `OBSERVATION` 0.5 s. Jitter: uniform 20–150 ms while
less than 2 s of the budget has passed, then 250–1000 ms, never past the deadline.

Files: `src/indus/state/writer.py`, `tests/unit/state/test_writer.py`.

Edge cases / failure modes:
- busy/locked (result code BUSY/LOCKED, or the text when no code) and "no more rows available"
  → retry until the deadline; then `StoreLocked` (`store.locked`).
- `disk I/O error` from `BEGIN IMMEDIATE` (callback not run) → one retry on the same connection;
  after the callback ran it propagates (its effect is unknown).
- callback raises → rollback, nothing written, the exception propagates unchanged.
- guard hooks (P02-07): checked inside the mutex before every attempt; a database error after
  the callback is handed to the guard (replaced → `StoreReplaced`, FTS-scoped → fail-open hook,
  structural → quarantine + `StoreCorrupt`).
- connection closed by a teardown while a write was queued → reopen once with a WARNING, unless
  the guard says quarantined or replaced.
- a write started from inside a write callback on the same thread → `RuntimeError` (would
  deadlock otherwise); `borrow()` inside a write callback yields the same connection.
- checkpoint failure is logged and never raises.

Our design differences / decisions:
- sleeping uses the injected clock's `sleep_sync` when it has one (`SystemClock`, tests' fake),
  else a `threading.Event` wait; the deadline uses `clock.monotonic()`.
- lock-holder naming (`/proc/locks`) is P17; on exhaustion we log the path and budget only.
- the compression-lock collision budget (5 s) lives with P02-12, not here.

Tests we will write:
- locked for 3 s of fake time then free → ROUTINE succeeds; every sleep in its band.
- locked forever → `StoreLocked` at 20.0 s fake time; 0.5 s for OBSERVATION; 60 s TRANSCRIPT;
  the message never classifies as disk/corrupt.
- callback raising → rollback, no partial row.
- BEGIN I/O error once → retried; twice → raised; I/O error after the callback → raised.
- "no more rows available" retried.
- 50 writes → exactly one PASSIVE checkpoint, no TRUNCATE anywhere.
- `awrite` from 20 tasks serialises, loop heartbeat keeps ticking.
- closed connection → reopened with a WARNING; nested write refused.

## Decisions for the lead
- Hooks `add_periodic` / `set_fts_fail_open` are the seams P02-14 plugs into; no FTS code here.

Compare checklist (filled after the rewrite; reopened `hermes_state.py` `_execute_write`,
`_sleep_before_write_retry`, `_try_wal_checkpoint`, `_reopen_after_close_locked`):
- [x] deadline budgets 20/60/0.5 s, jitter 20–150 ms for the first 2 s then 250–1000 ms, never past
  the deadline (tests: `tests/unit/state/test_writer.py::test_lock_held_for_three_seconds_is_waited_out`,
  `::test_lock_held_forever_raises_store_locked_at_the_deadline`)
- [x] BEGIN I/O error retried once on the same connection, never after the callback ran (tests:
  `::test_begin_io_error_once_is_retried`, `::test_begin_io_error_twice_is_raised`,
  `::test_io_error_after_the_callback_ran_is_not_retried`)
- [x] "no more rows" retried like busy (test: `::test_no_more_rows_is_retried_like_busy`)
- [x] PASSIVE every 50 writes, never TRUNCATE (test: `::test_fifty_writes_make_one_passive_checkpoint`)
- [x] the exhaustion error says contention and classifies as `locked` even when the path contains
  damage words (tests: `tests/unit/state/test_errors.py::test_store_locked_text_never_reads_as_damage`)
- [x] teardown race reopens with a WARNING, never for quarantined/replaced handles (tests:
  `::test_write_after_teardown_reopens_with_a_warning`, `tests/unit/state/test_guards.py::test_quarantined_handle_never_reopens`)
- Deliberate differences: exhaustion raises our typed `StoreLocked` instead of a re-worded SQLite
  error; nested writes raise instead of deadlocking; `awrite` runs on a writer-owned single
  thread executor.
- Not taken: lock-holder names in the exhaustion log (P17 holder scans); the compression-busy
  5 s budget (P02-12 owns the lock and its error).

## Status

Done (2026-10-18). `writer.py` with 17 unit tests. Seams for wave 4: `add_periodic` (FTS merge
every 1,000 writes) and `set_fts_fail_open` (P02-14).
