# Task: read-only browsing and integrity (P02-20) Phase: 02 Owner: P02 wave-4 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state.py` (the
read-only branch of `__init__`, `_open_read_only`, `_connect_read_only`),
`hermes_state_reconcile.py` (`reconcile_drift_for_browse` and the clean-before-write check
above it — for what browsing must not do), `website/docs/developer-guide/state-db-recovery.md`.
Closed before writing.
Evidence: none.

What it does (own words): listings, `doctor` and other profiles' sidebars must be able to look
at a store without ever changing it (R03.16). `open_readonly(scope, path=None)` opens the store
file with a `mode=ro` URI (percent-encoded path), a 5 s busy timeout, and nothing else: no
schema init, no journal switch, no quarantine of a bad file, no migration, no checkpoint. A
missing file is reported, never created. FTS presence is probed with SELECTs. SQLite can return
a transient `disk I/O error` to a read-only reader while a writer resets its WAL; that is retried
3 times 50 ms apart (clock-driven); lock errors are not retried (the busy timeout already waited).
A file that is not initialised, or whose schema is newer than ours, raises
`StoreUnavailable(kind=uninitialised|incompatible)`. `integrity(scope)` runs `PRAGMA
quick_check` (`integrity_check` with `full=True`) and the FTS check on that read-only connection
and reports the process health latch. `health(scope)` sums it up as `ok / busy / corrupt /
missing / unavailable` with the error bucket and its copy line, which doctor prints.

Inputs → outputs: `open_readonly(scope, path=None, *, sleep=None) -> ReadonlyStore`
(`path`, `conn`, `schema_version`, `journal_mode`, `fts`, `trigram`, `fts_stale`, `read()`,
`close()`, context manager); `integrity(scope, path=None, *, full=False) -> IntegrityReport`
(`ok`, `structure` lines, `fts` True/False/None, `latched`); `health(scope, path=None) ->
HealthReport` (`state`, `bucket`, `copy`, `line`, `detail`, `path`, `journal_mode`,
`schema_version`, `fts`, `trigram`, `fts_stale`).

Files: `src/indus/state/browse.py`, `tests/unit/state/test_browse.py`.

Edge cases / failure modes:
- missing file → `missing`, nothing created (checked before connecting; `mode=ro` cannot create).
- garbage file → `corrupt` (not a database), never moved aside; a zero-byte file reads as an
  empty database → `unavailable` (uninitialised), left as is.
- no `schema_version` table / row → `unavailable` (uninitialised); version > 1 → `unavailable`
  (incompatible).
- WAL: reading while a writer holds a long transaction works; SQLite may create the `-wal` /
  `-shm` sidecars for the reader, the main file's size and mtime stay unchanged.
- I/O error twice then fine → ok; four times → the bucket's state (`unavailable`, disk).
- lock error → `busy` with the locked copy line.
- structural damage found by `quick_check`, or SQLite raising corrupt / not-a-database →
  `corrupt` with the corrupt copy line; the process latch is set so this process's writers stop
  too (a reader's proof is as good as a writer's).
- an earlier latch in this process → `corrupt` without opening the file.
- FTS damage alone (`SQLITE_CORRUPT_VTAB`, or the FTS check failing) or a stale marker →
  state stays `ok` (the transcript is fine) with bucket `fts_index` and its copy line.
- the strict FTS `integrity-check` is an INSERT and fails on a read-only connection, so browse
  uses `PRAGMA integrity_check(<fts table>)` there (structure only).

Our design differences / decisions:
- no schema-drift catch-up while browsing (the reference adds missing columns under an owner
  lock); a store that lacks columns is still read, and only uninitialised / newer stores refuse.
- health runs `quick_check` (bounded by the 5 s busy timeout per statement, not a separate
  budget).

Tests we will write:
- missing → `missing`, file not created; uninitialised file → `unavailable`; newer version →
  `unavailable` (incompatible); open never runs schema init (no meta rows added).
- WAL: read while another connection holds `BEGIN IMMEDIATE` with a pending insert; mtime and size
  unchanged after browse + integrity + health.
- injected I/O error twice then success → ok, sleeps 50 ms × 2 on the fake clock; four times →
  raised; lock error → `busy`, not retried.
- corrupt copy (a garbage page written over a table page) → `corrupt` with the copy line and the
  latch set; garbage file → `corrupt`, not moved; FTS shadow damage → `ok` + `fts_index`.

## Decisions for the lead
- Health latches structural corruption found by a read (`guards.mark_corrupt`); change: drop
  that call if readers must stay side-effect free in-process.

Compare checklist (filled after the rewrite; reopened the read-only branch of `hermes_state.py`,
`reconcile_drift_for_browse`, the recovery doc):
- [x] read-only open: `mode=ro`, busy timeout, FTS probed by SELECT, connection closed on any
  probe failure (tests: `tests/unit/state/test_browse.py::test_open_reads_without_setting_anything_up`,
  `::test_path_with_url_characters_opens_the_right_file`)
- [x] I/O-error retry bound and backoff; locks not retried (tests:
  `::test_io_error_twice_then_success_is_ok`, `::test_io_error_four_times_gives_up`,
  `::test_lock_errors_are_busy_and_not_retried`)
- [x] never creates / migrates / checkpoints / quarantines (tests:
  `::test_missing_store_is_reported_and_never_created`,
  `::test_uninitialised_and_zero_byte_files_are_unavailable`,
  `::test_browse_never_checkpoints_a_live_wal`, `::test_garbage_file_is_corrupt_and_left_in_place`)
- [x] corruption reported with the latch and the copy line (tests:
  `::test_damaged_page_is_corrupt_with_the_copy_line`, `::test_integrity_full_check_and_latch`)
- [x] gap found while testing: a damaged FTS table makes `PRAGMA quick_check` itself raise
  `SQLITE_CORRUPT_VTAB` — the canonical tables are then checked one by one, so index damage
  reports `ok` + `fts_index` instead of `corrupt` (test: `::test_search_index_damage_keeps_the_store_ok`).
- Deliberate differences: no schema-drift catch-up while browsing; the reference also applies
  the configured pragmas to its read-only connection — ours applies none (reads only).
- Not taken: holder / gateway-ownership probes of the drift path (P15/P17).

## Status

Done (2026-10-18). `browse.py` with 17 unit tests (`test_browse.py`). Shared:
`locales/en.yaml` (`errors.store.uninitialised|incompatible`), `docs/internals/traps-ledger.md`
(R03.16).
