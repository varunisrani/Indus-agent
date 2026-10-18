# Task: `indus import` (P07-23) Phase: 07 Owner: P07 slice D builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/backup_restore.py`
(validation, prefix detection, page-wise DB restore), `hermes_cli/backup.py` (`run_import` and the
member import loop). Research `04-cli-config.md` §3.23; trap R04.22. Closed before writing.
Evidence: none.

What it does (own words): restores an `indus backup` zip into the **current** home (`-p work`
restores into that profile; never mapped back to the root, R04.22). Order: the file must exist and
be a zip; it must look like one of ours (a `config.yaml`, `.env` or `state.db` somewhere); a single
wrapping folder (`.indus/…`) is stripped; any member that would land outside the home (`..`,
absolute paths, drive letters) refuses the whole import; existing `config.yaml`/`.env` need a yes
(or `-f`; non-TTY without `-f` exits 1); every member is decompressed and CRC-checked before the
first byte is written (damaged → exit 1, home untouched). Then members are written through a temp
file and a rename, owner-only; runtime files of the source machine (pid, lock, process and gateway
state files) and SQLite sidecars are never written; `.db` members are written page by page into the
existing database through the online backup API, so open connections see the new data — when
another process holds the file it is skipped with a warning. A database that ends up with fewer
sessions than before is reported. Exit 1 when anything was skipped.

Files: `src/indus/cli/commands/import_cmd.py`, `tests/unit/cli/commands/test_import.py`.

Edge cases / failure modes:
- Empty zip / not ours / not a zip / missing file → exit 1.
- A damaged archive DB (fails `quick_check`) → that file skipped, existing DB untouched, exit 1.
- Restoring a root backup into a profile writes `profiles/…` under that profile (we never retarget).

Our design differences / decisions:
- Traversal refuses the whole archive before anything is written (the reference skips the member).
- Every restored file is 0600 and every created dir 0700 (our home rules).

Tests we will write:
- Corrupt member → home byte-identical; zip-slip refused; import into a named profile lands there;
  DB import while a reader connection is open (reader sees the new rows); fewer-sessions warning;
  runtime files kept; confirm on non-TTY without `-f` exits 1; prefix stripped.

Compare checklist (filled after the rewrite; reopened `hermes_cli/backup_restore.py`, `hermes_cli/backup.py`):
- [x] target is the current home (test: `tests/unit/cli/commands/test_import.py::test_restores_into_the_current_profile`)
- [x] CRC of every restored member before the first write (test: `tests/unit/cli/commands/test_import.py::test_corrupt_member_leaves_the_home_untouched`)
- [x] page-wise DB write keeps the inode, readers converge (test: `tests/unit/cli/commands/test_import.py::test_db_written_page_by_page_while_a_reader_is_open`)
- [x] runtime files and sidecars never written (test: `tests/unit/cli/commands/test_import.py::test_runtime_files_of_the_source_machine_are_kept`)
- [x] shrink report (sessions or messages) (test: `tests/unit/cli/commands/test_import.py::test_fewer_sessions_are_reported`)
- Deliberate differences: a traversal member refuses the whole archive; all restored files are
  0600; a wrapping folder is stripped when it is one of our names or holds a marker file.
- Not taken: re-creating profile alias scripts and gateway service hints (profiles' aliases are
  P07-16, gateway P15); the "deleted but still open" holder check for a missing target DB.

## Status

Done (2026-10-18). See the slice report for shared files (app.py registry lines, commands/__init__.py,
test_app.py help-order parsing).

## Integration (lead)

- The whole-archive refusal on a member outside the home is recorded as D-16.93.
- Slice D's `test_app.py` help-order parsing change was dropped at integration: with the
  registry one-liners always shown (`lazy.py`, slices A/C) no row wraps.
