# Task: integrity guards and error buckets (P02-07) Phase: 02 Owner: P02-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state.py` (file identity,
replaced/corrupt guard methods, `_halt_db_corrupt`, `close` 1218–1600), `hermes_state_errors.py`
(`classify_persistence_error` and its predicates), `hermes_state_health.py` (all),
`hermes_state_user_copy.py` (which buckets exist — not the wording), test names in
`tests/hermes_state/test_state_db_file_identity.py` and `test_state_db_corrupt_quarantine.py`.
Closed before writing.
Evidence: none.

What it does (own words): two failure modes must stop writes at once instead of making things
worse. (1) The file at the store path is no longer the file this process opened — a backup
restore or a `cp` over a live store. Writing on would push old WAL frames into a new file. Before
every write (inside the writer's mutex) the guard compares the path's `(st_dev, st_ino)` and the
header `application_id` with the values recorded at open; on a mismatch the handle becomes
`replaced` for good: every write raises `StoreReplaced`, the close skips its checkpoint, no repair
is tried, and transcript writers divert pending messages to `<home>/sessions/<id>.jsonl` (R03.4).
(2) SQLite reports structural corruption (corrupt / not-a-database / "malformed") that is not
scoped to an FTS index. The handle is quarantined for the life of the process: the failing write
raises `StoreCorrupt`, later writes fail before touching SQLite, there is no reopen, no
close-time checkpoint (SQLite's own one is switched off too), and a process-wide health latch per
path says `corrupt` so doctor and listings do not show an empty store as healthy (R03.5).
FTS-scoped corruption is left to P02-14's fail-open hook.
`state.errors.classify(exc_or_text)` maps any persistence error or its text to one bucket, and
`copy_for(bucket)` gives a plain what-happened / what-to-do line, an action code for a "run
doctor" button and a transient flag (503 vs 500 later).

Inputs → outputs: guards — `FileGuard(path, identity, application_id)` with
`before_write()`, `on_write_error(exc) -> "fts" | "raise"` (raises itself for replaced/corrupt),
`replaced`, `quarantined`, `skip_close_checkpoint`; `mark_corrupt(path, reason)`,
`health_state(path) -> "ok" | "corrupt"`, `reset_health(path=None)`;
`is_structural_corruption(exc)`, `is_fts_scoped_corruption(exc)`;
`divert_path(home, session_id)`, `divert_messages(home, session_id, rows) -> Path`.
errors — `Bucket` (`locked, disk, compression, compression_closed, turn_lease, corrupt,
fts_index, replaced, session_row_missing, unknown`), `classify(exc_or_text) -> Bucket`,
`StoreCopy(bucket, what, todo, action, transient)`, `copy_for(bucket)`, `describe(exc)`,
`COPY_KEYS` (locale keys `errors.store.<bucket>.what|todo`), lock/IO predicates shared by the
writer (`is_lock_error`, `is_disk_io_error`, `is_no_more_rows`).
`foundation/errors.py` gains `StoreIsolationRefused`, `StoreUnopenable`, `StoreNotWritable`,
`StoreLocked`, `StoreReplaced`, `StoreCorrupt`, `StoreUnavailable(kind)`, `SessionTombstoned`,
`ResumeTooLarge(count, numeric)` with codes in `ERROR_CODES`.

Files: `src/indus/state/guards.py`, `src/indus/state/errors.py`, `src/indus/foundation/errors.py`
(shared), `locales/en.yaml` (shared: `errors.store.*`), `tests/unit/state/test_guards.py`,
`tests/unit/state/test_errors.py`.

Edge cases / failure modes:
- header `application_id` 0 on disk while the stamp still sits in the WAL is not a replacement;
  only a non-zero, different value is.
- the identity probe never opens/closes the store file (stat + cached-descriptor pread).
- a replaced file takes precedence over a corruption error seen on it (no quarantine of the new
  file, `StoreReplaced` raised).
- another handle in the process already latched the path corrupt → this handle quarantines
  itself before touching SQLite.
- classifier order: our typed errors (by code), FTS provenance (result code
  `SQLITE_CORRUPT_VTAB`, or text naming a `messages_fts*` object with a corruption word), SQLite
  result codes (BUSY/LOCKED → locked, CORRUPT/NOTADB → corrupt, FULL/IOERR/READONLY/CANTOPEN/PERM
  → disk), FK constraint → session_row_missing, phrases, disk-full; anything else unknown.
- divert file name: the session id must be a safe file name, else `StoreError`.

Our design differences / decisions:
- copy text is fresh English in code and mirrored in `locales/en.yaml` (a test keeps them equal);
  there is no Python locale loader yet.
- no `deleted_wal` bucket (lost-WAL capture is P17); add with P17.
- buckets are a `StrEnum`; typed mapping by `IndusError.code` so wave-4 errors join by adding a
  code row (`TYPED_BUCKETS`).

Tests we will write:
- `os.replace` a different store over the path → next write `StoreReplaced`, the next one too,
  divert JSONL written, close does not checkpoint.
- patched header `application_id` change alone → replaced; header 0 → not replaced.
- injected `sqlite3.DatabaseError("database disk image is malformed")` from a write → quarantined,
  later writes fail fast without touching the connection, latch `corrupt`, close skips the
  checkpoint; a second handle on the path quarantines itself; FTS-scoped text does not trip it.
- classifier table ≥ 25 cases; `StoreLocked` text → locked, never disk/corrupt; copy table has
  every bucket and matches `locales/en.yaml`.

## Decisions for the lead
- `errors.store.locked` / `errors.store.corrupt` in `locales/en.yaml` (P00-14 samples, unused)
  became mappings (`.what`/`.todo`); `errors.store.too_new` kept.

Compare checklist (filled after the rewrite; reopened `hermes_state.py` guard methods and
`close`, `hermes_state_errors.py`, `hermes_state_health.py`):
- [x] inode + `application_id` compared before every write inside the writer mutex; header 0 is not
  a replacement; sticky; no repair; close skips the checkpoint (tests:
  `tests/unit/state/test_guards.py::test_replaced_file_fences_every_later_write`,
  `::test_application_id_change_alone_is_detected`, `::test_header_zero_is_not_a_replacement`)
- [x] replaced takes precedence over corruption (test: `::test_corruption_on_a_replaced_file_is_not_quarantined`)
- [x] quarantine: raise, fail fast without touching SQLite, no reopen, no checkpoint, SQLite's
  close-time checkpoint switched off, process latch shared by other handles (tests:
  `::test_corruption_quarantines_the_handle`, `::test_quarantined_handle_never_reopens`)
- [x] FTS-scoped corruption goes to the fail-open hook, never the quarantine (tests:
  `::test_fts_scoped_corruption_is_not_quarantined`, `::test_fts_fail_open_hook_retries_the_write`)
- [x] classifier order typed → FTS → codes → FK → phrases → disk (test: `tests/unit/state/test_errors.py::test_classifier_table`, 37 cases)
- Deliberate differences: our typed errors are `IndusError`s (not `sqlite3.DatabaseError`
  subclasses) and are classified by code; malformed-*schema* errors also quarantine (there is no
  repair path before P07); divert files are ASCII JSON lines so lone surrogates survive.
- Not taken: `deleted_wal` bucket and lost-WAL capture (P17); latching corruption seen by readers
  (P02-20 health can add `mark_corrupt` on its read path).

## Status

Done (2026-10-18). `guards.py`, `errors.py` with 58 unit tests (15 + 43); shared files:
`src/indus/foundation/errors.py` (store error classes and codes), `locales/en.yaml`
(`errors.store.<bucket>.what|todo`).

## Integration (lead)

- `foundation/errors.py` merged with the P03 provider classes (union). Added the three codes
  `state.errors.TYPED_BUCKETS` already maps but `ERROR_CODES` lacked: `store.compression_busy`,
  `store.compression_closed`, `store.turn_lease_lost` (codes only; their error classes arrive with
  the compression and turn-lease tasks).
