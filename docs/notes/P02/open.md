# Task: opening a store (P02-01) Phase: 02 Owner: P02-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state.py` (`__init__`,
`_open_writer`, `_open_writer_conn`, `_connect_and_init*`, lines 580–860), `hermes_state_dbfile.py`
(the cached-descriptor header probe, the pre-open header checks, the quarantine lock and the
quarantine mover), `hermes_state_guard.py` (all). Closed before writing.
Evidence: none.

What it does (own words): `state.connection.open_store(scope, path=None)` turns a profile scope
into one open, writable SQLite connection on `<home>/state.db`, or refuses loudly. A test run can
never open the user's real store (fixture rows once leaked into real stores); a deleted profile is
never re-created by a stale caller; a store file that is not SQLite (0 bytes after a crash, NUL
page, clobbered first page) is moved aside with its sidecars instead of being opened or silently
overwritten; the file is owner-only before SQLite ever touches it; a file this user cannot write
gives a message naming the path instead of a late "readonly database".

Inputs → outputs: `open_store(scope, path=None, *, on_connected=None, timeout=1.0, environ=None)`
→ `StoreFile(path, conn, identity=(st_dev, st_ino), application_id, quarantined_to)`.
`on_connected(conn)` runs while the quarantine lock is still held when the file was absent or
invalid, so the journal/schema setup of the opener commits before a sibling probes the header.
Helpers used by later tasks: `read_header(path)` (cached, never-closed descriptor, `os.pread`),
`header_application_id(path)`, `header_says_wal(header)`, `file_identity(path)`,
`release_header_fd(path)`, `real_default_root()`, `looks_like_test_run(environ)`.

Files: `src/indus/state/connection.py`, `tests/unit/state/conftest.py`,
`tests/unit/state/test_connection.py` (the process race is a `slow` test there).

Edge cases / failure modes:
- guard first: `PYTEST_CURRENT_TEST` / `INDUS_TEST_ISOLATION` in the process env, or a pytest /
  unittest process among up to 8 ancestors (Linux `/proc/<pid>/stat` + `cmdline`, macOS `ps`),
  refuses `<real root>/state.db` and `<real root>/profiles/<id>/state.db`; the real root comes
  from the passwd entry (`USERPROFILE`/`LOCALAPPDATA` on Windows), never the patched home helpers;
  deeper scratch paths pass; `INDUS_STATE_DB_GUARD_BYPASS=1` bypasses → `StoreIsolationRefused`.
- parent dir: a named profile that is tombstoned or gone → `ProfileTombstoned`/`ProfileNotFound`
  (via `profiles.mkdir_in_profile`), nothing created.
- header probe: 0 bytes / NUL header → `zeroed`, other non-magic → `notadb`; moved to
  `<name>.<kind>-<ts>-<pid>.bak` (+`-wal`/`-shm` beside it), `-1`, `-2`… on a name clash; done
  under `<dir>/.quarantine.lock` (5 s; on timeout a warning and we go on), re-checked under the
  lock so two racing processes make one backup; move failed and still invalid → `StoreUnopenable`.
- the probe never opens and closes the store file per call: one descriptor per path is cached and
  kept open (closing any descriptor on the file drops SQLite's POSIX locks, R03.2); a new inode at
  the path retires the old descriptor without closing it.
- mode: a missing file is created `O_EXCL` at 0600 (umask cannot widen it); an existing file is
  chmod-ed to 0600 unless the install is managed — both before the first connect.
- preflight: the dir must be writable; the file and existing sidecars must be writable — we also
  treat "no write bit at all" as read-only, so the check holds when running as root →
  `StoreNotWritable` with the path and a chown/chmod hint.
- connect `timeout=1.0, isolation_level=None, check_same_thread=False`, row factory
  `sqlite3.Row`, `PRAGMA foreign_keys=ON`; any failure after connect closes the connection.

Our design differences / decisions:
- our names: `INDUS_STATE_DB_GUARD_BYPASS`, `INDUS_TEST_ISOLATION`, `StoreFile`, `open_store`.
- no psutil: the ancestor walk reads `/proc` on Linux and `ps` on macOS (memoised per process).
- the deleted-sidecar holder refusal and the OFD lock guard are P17 (R03.2); recorded, not built.
- no automatic malformed-schema repair on open (P07 repair verbs).

Tests we will write:
- guard: patched passwd root refused for root and named-profile stores, deeper scratch allowed,
  bypass env, `looks_like_test_run` from env and from a fake ancestor table; the computed real
  root is refused without creating anything.
- tombstoned profile refused, nothing created.
- zeroed and garbage files quarantined with sidecars, numbered on clash, fresh store opens.
- move failure (patched rename) on an invalid file → `StoreUnopenable`.
- mode 0600 with umask 0 (fresh and tightened existing).
- read-only file and read-only sidecar → `StoreNotWritable` naming the path.
- header descriptor: cached and reused; rebinds after `os.replace`.
- `slow`: two processes racing on one zeroed file → exactly one backup, both open.

## Decisions for the lead
- `src/indus/state/__init__.py` keeps P00-10's one-line docstring because
  `tests/unit/test_layers.py::test_layer_packages_exist_with_docstring` enforces it. Public names
  live in `indus.state.registry` (`acquire`, `open_unshared`, `StoreHandle`, `shared`) and
  `indus.state.writer` (`ROUTINE`, `TRANSCRIPT`, `OBSERVATION`). Change: relax that test for
  `state` and add re-exports.
- Real-root guard covers `~/.indus` exactly (no `INDUS_DATA_DIR_SUFFIX` variant), like the
  reference. Change: add suffix variants in `real_default_root()`.
- The quarantine lock is `<dir>/.quarantine.lock` (one per directory, as the research layout
  lists it), taken with `fsio.FileLock`.
- "Read-only" in the preflight also means "no write bit for anyone" so root sees it too.

Compare checklist (filled after the rewrite; reopened `hermes_state.py` 580–860,
`hermes_state_dbfile.py` header/quarantine functions, `hermes_state_guard.py`):
- [x] order guard → mkdir → probe/quarantine → perms → preflight → connect → setup, and the setup
  (journal, schema, generation stamp) runs under the quarantine lock when the file was missing or
  invalid (tests: `tests/unit/state/test_connection.py::test_processes_racing_on_a_zeroed_file_make_one_backup`,
  `::test_guard_runs_before_anything_touches_disk`)
- [x] zeroed vs notadb naming, sidecars moved with the main file, numbered on clash, re-check under
  the lock, refuse when the move failed (tests: `::test_zeroed_file_is_quarantined_with_sidecars`,
  `::test_garbage_file_is_quarantined_as_notadb`, `::test_backup_names_are_numbered_on_clash`,
  `::test_failed_move_of_an_invalid_file_refuses`)
- [x] gap found: we moved an invalid file aside even when the quarantine lock timed out — fixed:
  without the lock an invalid file is left alone and the open refuses; a missing file is still
  created (tests: `::test_busy_quarantine_lock_never_moves_an_invalid_file`,
  `::test_busy_quarantine_lock_still_creates_a_missing_store`)
- [x] header reads through one cached, never-closed descriptor, rebound on a new inode
  (test: `::test_header_reads_reuse_one_descriptor_and_follow_a_new_inode`)
- [x] isolation guard: env markers or runner ancestry; live root and `profiles/<id>` only; bypass
  env (tests: `::test_guard_refuses_real_root_and_named_profiles`, `::test_bypass_env_lets_the_real_root_open`,
  `::test_ancestry_finds_pytest_from_a_child`)
- Deliberate differences: no psutil (procfs / `ps`); the read-only preflight also treats "no write
  bit" as read-only (root); a created file is tightened by dropping group/other bits only, so a
  deliberately read-only file is still reported; sidecars are not re-chmod-ed after WAL setup
  (SQLite creates `-wal`/`-shm` with the main file's mode, which is already 0600).
- Not taken: deleted-sidecar holder refusal and the OFD lock guard (P17, R03.2); automatic
  malformed-schema repair on open (P07); the per-process "last init error" record (P06 resume
  pickers can add it next to their banner).

## Status

Done (2026-10-18). `connection.py` with 27 unit tests (one `slow`: four processes racing on a
zeroed file). Shared files touched: none beyond the P02-07 ones. `src/indus/state/__init__.py`
was left as the one-line docstring: `tests/unit/test_layers.py` (P00-10) requires every layer
package `__init__` to be exactly one docstring line, so the "public exports" row of the phase file
was not done — callers import from `indus.state.registry` / `indus.state.writer` (see Decisions).
