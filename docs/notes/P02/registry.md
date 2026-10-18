# Task: handle registry (P02-05) Phase: 02 Owner: P02-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_registry.py` (all),
test names in `tests/hermes_state/test_shared_session_db_registry.py`. Closed before writing.
Evidence: none.

What it does (own words): a store handle owns a writer connection, a read pool and a close-time
checkpoint; two handles on one file in one process race each other's checkpoints. Long-lived
code therefore shares one `StoreHandle` per resolved path: `acquire(scope, path=None)` returns
the current generation and bumps its refcount, `handle.close()` gives one reference back, and the
last one tears the handle down (outside the registry lock, so a slow close never stalls other
paths). When the file at the path is a different inode than the generation opened (backup
restore, recovery swap), that generation is retired — its holders keep it (its writes are fenced
by P02-07) — and a fresh generation opens. Teardowns of a path are counted behind a barrier so an
acquire for that path waits until the physical close is done.

Inputs → outputs: `acquire(scope, path=None) -> StoreHandle`; `open_unshared(scope, path=None)`
(one-shot callers and tests; `close()` tears it down directly); `close_all() -> int`;
`close_all_under(directory) -> int`; `other_generations(path, *, exclude=None) -> list[str]`;
`live_handle_count(path)`. `StoreHandle`: `path`, `scope`, `journal_mode`, `wal`, `writer`,
`reads` (pool), `guard`, `write(fn, budget=)`, `awrite`, `read()`, `close()`.
Open sequence of a handle: `connection.open_store` → (under the quarantine lock when needed)
`journal.apply` + `journal.apply_pragmas` + `schema.init` → guard records identity and
`application_id` → writer + read pool → `schema.run_migrations` for registered steps.
Close sequence: PASSIVE checkpoint (skipped when quarantined or replaced) → close readers →
close writer → release the cached header descriptor when no other handle in the process uses
the path.

Files: `src/indus/state/registry.py`, `tests/unit/state/test_registry.py`.

Edge cases / failure modes:
- two threads acquiring a cold path open one handle (the second waits on an "opening" event);
  if the open fails, the waiter retries the open itself.
- inode swap → new generation; old holder's reads keep working; its release tears down only it.
- `close_all_under(dir)` closes only handles whose file is under that directory (profile delete
  or rename) and waits for already-admitted teardowns there.
- more than 4 live handles on one path → one WARNING per path (diagnostic).
- a close that raises still settles the barrier.

Our design differences / decisions:
- `StoreHandle` lives in `registry.py` (no extra module) and is the object later P02 modules take.
- no `borrow_live` yet (P02-19 can add it if VACUUM needs pinned references).

Tests we will write:
- two acquires share one handle; three releases close once.
- inode swap via `os.replace` → new generation, old holder still reads, writes on it fenced.
- `close_all_under(<root>/profiles/work)` closes only that profile's handles.
- concurrent acquire during a blocked teardown waits (threads + events, no sleeps).
- `other_generations` lists live generations only; diagnostic warning past 4 handles.

## Decisions for the lead
- none.

Compare checklist (filled after the rewrite; reopened `hermes_state_registry.py`):
- [x] refcounted sharing per resolved path; last release tears down outside the lock (test:
  `tests/unit/state/test_registry.py::test_acquires_share_one_handle_and_the_last_release_closes`)
- [x] inode change retires the generation, holders keep it, release is object-keyed (test:
  `::test_inode_swap_opens_a_new_generation_and_fences_the_old_one`)
- [x] counted teardown barrier; acquire waits behind it (test: `::test_acquire_waits_for_an_admitted_teardown`)
- [x] cold concurrent acquires open once; a failed open leaves no entry (tests:
  `::test_cold_concurrent_acquires_open_one_handle`, `::test_failed_open_leaves_no_entry_and_the_next_acquire_retries`)
- [x] `close_all_under` closes only that directory's handles (test: `::test_close_all_under_closes_only_that_profile`)
- [x] gap found: a handle opened while a close of the same path was admitted was published anyway —
  fixed: it is torn down and the acquire waits and retries (same rule as the reference).
- [x] close sequence: PASSIVE, readers, writer, header descriptor (test:
  `::test_close_sequence_checkpoints_and_releases_the_header_descriptor`)
- Deliberate differences: `StoreHandle` is the shared object itself (no separate "registry-owned"
  flag on a store class); `open_unshared` for one-shot callers.
- Not taken: `borrow_live` / `release_or_close` helpers (add with P02-19 if VACUUM needs them).

## Status

Done (2026-10-18). `registry.py` (with `StoreHandle`) and 11 unit tests, including a DELETE-mode
end-to-end handle. Extension points for wave 4: `registry.EXTENSIONS` (schema extensions run in
the init transaction) and `registry.MIGRATIONS` (data steps).
