# Task: read pool (P02-04) Phase: 02 Owner: P02-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_readpool.py` (all),
`hermes_state.py` (`_get_read_conn`, `_checkout_read_conn`, `_read_ctx`, 860–1006), test names in
`tests/hermes_state/test_session_db_read_conn_pool.py`. Closed before writing.
Evidence: none.

What it does (own words): in WAL mode readers do not need the write lock, so reads borrow a
read-only connection from a small LIFO pool and never queue behind the writer's mutex. Read
connections are descriptors, and unbounded per-thread connections once pushed a process into
EMFILE where the error surfaced in unrelated tools (R03.17). So: at most 8 live read connections
per database file (shared by every handle on that file in the process), at most 24 per process,
and none at all when fewer than 64 descriptors remain under the soft `RLIMIT_NOFILE`. When a
reader cannot get a permit it first closes an idle pooled connection anywhere in the process; if
there is none it reads on the writer connection (slower, never blocked forever, never EMFILE).

Inputs → outputs: `ReadPool(path, writer, *, wal, clock, pragmas=None, opener=None)`;
`with pool.read() as conn:`; `pool.close()`; module functions `open_fd_count()`,
`fd_soft_limit()` (patched by tests), `process_stats()` for diagnostics.

Files: `src/indus/state/readpool.py`, `tests/unit/state/test_readpool.py`.

Edge cases / failure modes:
- DELETE mode (or `wal=False`): every read uses the writer connection under its mutex with
  `busy_timeout` raised to 5 s for the read, restored after.
- read connections open with `file:<percent-encoded absolute path>?mode=ro` so `?`/`#` in a home
  path cannot select another file; row factory `sqlite3.Row`; configured pragmas applied.
- a failed open returns its permits and blocks new opens for 60 s of clock time (a timestamp,
  not a sticky flag); reads fall back meanwhile.
- `close()` marks the pool closed and closes idle connections; a reader in flight closes its own
  connection instead of returning it; permits are always released, also when close fails.
- fd count is cached 0.25 s; an unmeasurable count or limit → allow; a count probe that itself
  hits EMFILE → refuse.

Our design differences / decisions:
- the budget object per path is held by a module table keyed by the resolved path and kept
  alive by the pools that use it (weak values), like the reference.
- the "more than 4 handles on one path" diagnostic lives in the registry (P02-05), not here.

Tests we will write:
- 9 concurrent readers on one path → 8 pooled + 1 on the writer.
- 3 paths × 8 held + 1 on a 4th path → falls back (process cap 24).
- idle connection on another path is reclaimed for a new reader when the process cap is hit.
- fake rlimit 128 with 70 open fds → refused, falls back.
- failed open → no retry for 60 s fake time, then retried.
- close during an in-flight read → that connection closed, not returned; permits all back.
- path containing `#` (and `?`) opens the right file.
- DELETE mode → writer connection with busy_timeout 5000 during the read, restored after.

## Decisions for the lead
- none.

Compare checklist (filled after the rewrite; reopened `hermes_state_readpool.py`,
`hermes_state.py` `_get_read_conn`/`_read_ctx`):
- [x] 8 per path shared by every handle, 24 per process, 64-fd headroom with a 0.25 s cached count,
  unmeasurable → allow (tests: `tests/unit/state/test_readpool.py::test_nine_concurrent_readers_use_eight_pooled_and_the_writer`,
  `::test_process_cap_falls_back_to_the_writer`, `::test_low_descriptor_headroom_refuses_new_connections`,
  `::test_unmeasurable_descriptors_allow_reads`)
- [x] a permit miss reclaims an idle connection anywhere, else the writer
  (test: `::test_idle_connections_elsewhere_are_reclaimed_at_the_process_cap`)
- [x] failed opens back off 60 s by timestamp, permits never stranded
  (test: `::test_failed_open_backs_off_for_sixty_seconds`)
- [x] close marks the pool closed; in-flight readers close their own connection
  (test: `::test_close_during_a_read_closes_that_connection`)
- [x] DELETE mode reads on the writer with 5 s busy timeout (test:
  `::test_delete_mode_reads_on_the_writer_with_a_longer_busy_timeout`)
- [x] gap found while testing the registry: a handle whose file was replaced would open *new* read
  connections on the replacement — fixed: the pool asks the guard before opening and reads stay on
  the writer connection (test: `tests/unit/state/test_registry.py::test_inode_swap_opens_a_new_generation_and_fences_the_old_one`)
- Deliberate differences: the duplicate-handle warning lives in the registry.
- Not taken: the transient disk-I/O retry of pooled reads (P02-20 browse owns read-only retries).

## Status

Done (2026-10-18). `readpool.py` with 15 unit tests.
