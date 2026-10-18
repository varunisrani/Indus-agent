# Task: journal mode and pragmas (P02-02) Phase: 02 Owner: P02-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_wal.py`
(`apply_wal_with_fallback`, `_enable_wal`, `_set_journal_mode_no_wait`, the WAL-reset gate, the
cross-VM mountinfo detection, `_apply_wal_companions`, the synchronous and integer pragma
helpers). Closed before writing.
Evidence: none. SQLite facts checked against sqlite.org: database header bytes 18/19 (file format
write/read version, 2 = WAL), `journal_size_limit`, `checkpoint_fullfsync`, `synchronous`
levels 0–3, the WAL-reset bug note (fixed 3.51.3, backports 3.50.7 and 3.44.6).

SQLite shipped with the Python uv uses on this machine: **3.45.1** (`/usr/bin/python3.13`,
Ubuntu's libsqlite3). FTS5 and the `trigram` tokenizer are available. **3.45.1 is inside the
WAL-reset bug range**, so on this host a fresh store opens in DELETE mode (see Decisions).

What it does (own words): `state.journal.apply(conn, cfg, path)` decides the journal mode of a
freshly opened writer connection and returns `"wal"` or `"delete"`. The one rule that matters: a
file that is WAL on disk (or whose header cannot be read) stays WAL, whatever the config says,
because switching a live WAL file to DELETE throws away other processes' uncheckpointed commits.
WAL is not *enabled* where it is known to be unsafe (virtiofs/9p shared mounts, SQLite builds
with the WAL-reset bug); where enabling WAL fails with an incompatibility error or SQLite quietly
refuses, the store falls back to DELETE and says so once. `apply_pragmas(conn, cfg)` sets the
configured performance/durability pragmas on every connection kind, and on macOS always forces
`checkpoint_fullfsync` and `synchronous=FULL`.

Inputs → outputs: `cfg` is the `database` config section as a mapping (`journal_mode`, the pragma
names at the top level and/or under `pragmas:`); `database_config(scope)` reads it from the
scope's config. `apply(conn, cfg, path, *, version=None, mountinfo=None, platform=None) -> str`;
`apply_pragmas(conn, cfg, *, platform=None) -> None`; `wal_reset_vulnerable(version) -> bool`;
`set_mode_no_wait(conn, mode) -> str` (busy_timeout 0 around the switch); `fs_type(path,
mountinfo)`; `reset_once_logs()` for tests.

Files: `src/indus/state/journal.py`, `tests/unit/state/test_journal.py`.

Edge cases / failure modes:
- configured mode invalid or missing → `wal`.
- header WAL + configured `delete` → stays WAL, one ERROR per process per path.
- header unreadable (exists, non-empty, pread fails) → treated as WAL, nothing switched.
- bug-range SQLite or virtiofs/9p: fresh/rollback file → DELETE (warning once); existing WAL file
  → kept with a warning once.
- `PRAGMA journal_mode=WAL` raising "locking protocol" / "not authorized" / "disk i/o error" →
  if the header now reads WAL (or unreadable) re-raise, else DELETE with one ERROR; any other
  OperationalError propagates; returning a mode other than `wal` → DELETE path with one ERROR.
- every non-WAL switch runs with `busy_timeout=0` so a concurrent opener makes it fail instead
  of racing; a failed DELETE switch returns the mode read back.
- pragmas: `cache_size`, `mmap_size`, `temp_store`, `wal_autocheckpoint`, `journal_size_limit`
  as ints (bad value → warning, skipped); `synchronous` as `OFF/NORMAL/FULL/EXTRA` or 0–3
  (unknown → warning, untouched; macOS below FULL → warning, FULL kept).
- WAL companions: `journal_size_limit=67108864`, macOS barriers.

Our design differences / decisions:
- the on-disk mode is read from header bytes 18/19 through the cached descriptor
  (`connection.read_header`) instead of `PRAGMA journal_mode` (which can block on a busy file).
- no "strict" variant raising instead of falling back (no second store needs it yet).
- the reference retries an EIO from the WAL pragma twice with real sleeps; we do not retry
  (no real sleeps in product code paths without a clock; a transient EIO lands in the guarded
  DELETE fallback only when the header is provably not WAL).

Tests we will write:
- existing WAL + `delete` → WAL, exactly one ERROR across two opens.
- unreadable header (patched) → WAL kept, `journal_mode` pragma never set to delete.
- injected mountinfo with virtiofs (and 9p) → DELETE on a fresh store; existing WAL kept.
- version gate table: 3.51.2 refused, 3.51.3 ok, 3.50.7 ok, 3.50.6 refused, 3.44.6 ok,
  3.44.5 refused, 3.6.x ok (pre-WAL), 3.45.1 refused.
- fake connection raising "locking protocol" → DELETE with one ERROR; unrelated error propagates;
  silent refusal → DELETE.
- `cache_size: lots` skipped with a warning; `synchronous: normal` applied on Linux; macOS
  (patched platform) keeps FULL and sets `checkpoint_fullfsync`.

## Decisions for the lead
- **This host's SQLite (3.45.1) is in the WAL-reset range**: by the task's own rule every fresh
  store here is DELETE, so read pooling, the M0 "store: ok (wal …)" line and the D-04.6 stress run
  behave as DELETE on this machine. Unit tests pin the version (fixture) so they are host
  independent. Options: ship a Python whose SQLite is ≥ 3.51.3 (uv-managed CPython), or a
  config/env override to allow WAL on affected builds (not built; would need a decision).
- Pragmas are read from the `database` section top level **and** from `database.pragmas`
  (top level wins); `config/schema.py` was left unchanged (`DatabaseSection` already allows extra
  keys). Change: drop one spelling.

Compare checklist (filled after the rewrite; reopened `hermes_state_wal.py`):
- [x] on-disk WAL or unreadable → keep WAL; configured `delete` ignored with one ERROR per
  process and path (tests: `tests/unit/state/test_journal.py::test_existing_wal_with_delete_config_stays_wal_and_logs_once`,
  `::test_unreadable_header_keeps_wal_and_switches_nothing`)
- [x] WAL-reset gate and cross-VM gate refuse to *enable* WAL, keep an existing WAL file with a
  warning (tests: `::test_wal_reset_bug_gate`, `::test_vulnerable_sqlite_uses_delete_for_new_and_keeps_existing_wal`,
  `::test_cross_vm_mount_gets_delete_for_a_fresh_store`, `::test_cross_vm_mount_keeps_an_existing_wal_store`)
- [x] incompatibility markers → DELETE once; other errors propagate; silent refusal → DELETE;
  never downgrade when the file now reads WAL (tests: `::test_locking_protocol_error_falls_back_to_delete_once`,
  `::test_unrelated_error_from_the_wal_switch_propagates`, `::test_silent_refusal_falls_back_to_delete`)
- [x] non-WAL switches with `busy_timeout=0`; a blocked switch leaves the mode (tests:
  `::test_non_wal_switch_uses_zero_busy_timeout`, `::test_delete_switch_blocked_by_another_opener_leaves_mode`)
- [x] WAL companions + macOS barriers on every activation; int pragmas skipped with a warning on
  bad values; synchronous names/levels, macOS floor (tests: `::test_fresh_store_gets_wal_with_companions`,
  `::test_macos_barriers_on_every_wal_activation`, `::test_bad_pragma_value_is_skipped_with_a_warning`,
  `::test_macos_forces_full_and_fullfsync`)
- Deliberate differences: the on-disk mode comes from header bytes 18/19 via the cached descriptor
  (no `PRAGMA journal_mode` probe); a configured `delete` on a fresh store is applied without the
  reference's "verify or raise" (an unreadable header already keeps WAL); no config-lock dance
  (our config is read once into the scope).
- Not taken: EIO retries with real sleeps around the WAL switch; the "journal mode upgraded"
  info log when a rollback-mode store becomes WAL; the strict `require_wal` variant.

## Status

Done (2026-10-18). `journal.py` with 44 unit tests. SQLite on this host: 3.45.1 (inside the
WAL-reset range — see Decisions). `config/schema.py` unchanged.

## Integration (lead)

- Open decision recorded at merge: this host's system SQLite is **3.45.1**, inside the WAL-reset
  range (3.7.0 – 3.51.2), so every fresh store here opens in **DELETE** mode (read pooling, the M0
  `store: ok (wal …)` line and the D-04.6 stress run all behave as DELETE on this machine). Lead to
  decide: ship/use a uv-managed CPython whose SQLite is ≥ 3.51.3, and/or an explicit override
  (config or `INDUS_` env) that allows WAL on affected builds. Nothing changed in `journal.py`.
