# Task: `indus backup` (P07-22) Phase: 07 Owner: P07 slice D builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/backup.py` (exclusion
constants, the locked full-backup run, quick snapshot functions). Research `04-cli-config.md`
§3.23; trap R04.23. Closed before writing.
Evidence: none.

What it does (own words): zips the whole root home (every profile) so it can be restored with
`indus import`. Left out: the install tree, caches that rebuild themselves, prior backups and
snapshots, checkpoint stores, browser profile folders (they hold cookies and saved logins, R04.23),
virtual envs and tool caches, SQLite sidecars, lock and pid files, sockets, devices and symlinks.
Every `.db` file is copied with SQLite's online backup API, so a running agent never yields a torn
copy. Secrets held in the OS keychain are not exported (D-16.79): the run lists the names the user
must re-enter; file-held secrets (`.env`, `secrets.json`) are included. The zip is written to a
hidden temp beside the output and renamed; `--keep N` (3; 0 = all) removes older
`indus-backup-*.zip` in the output dir only; one backup at a time (lock wait 0.25 s → exit 2); a
file that could not be read → the zip is kept as `.incomplete.zip`, exit 1. `--quick [--label]`
copies this profile's critical files into `<home>/state-snapshots/<stamp>[-label]/` (keep 20,
files over 1 GiB skipped).

Files: `src/indus/cli/commands/backup.py`, `src/indus/cli/backup_rules.py`,
`tests/unit/cli/commands/test_backup.py`.

Edge cases / failure modes:
- `-o` names a directory → default file name inside it; a name without `.zip` gets it.
- The output inside the home is never archived into itself.
- A non-SQLite file named `.db` is copied as a plain file.
- Nothing to back up → "No files to back up." exit 0.

Our design differences / decisions:
- Browser profiles are also caught by content: a folder holding `Cookies` / `Login Data` files is
  skipped whatever it is called.
- Keychain secrets stay out (D-16.79) with a named warning.
- No external memory-provider paths yet (no such providers until P09).

Tests we will write:
- A temp root with every excluded kind → none in the zip; DB copied while another connection holds
  an open write transaction; keep-N prunes only matching names; a second backup while the lock is
  held exits 2; quick snapshot keeps 20 and skips large files; keychain names warned.

Compare checklist (filled after the rewrite; reopened `hermes_cli/backup.py`):
- [x] exclusions incl. browser profiles, venvs, caches (kept media subfolders), sidecars, pid/lock
  files, links and non-regular files (test: `tests/unit/cli/commands/test_backup.py::test_every_excluded_kind_stays_out`)
- [x] databases through the online backup API, staged beside the output (test: `tests/unit/cli/commands/test_backup.py::test_database_copied_while_a_writer_holds_a_transaction`)
- [x] keep-N only after a complete run, only `indus-backup-*` (test: `tests/unit/cli/commands/test_backup.py::test_keep_prunes_only_matching_names`)
- [x] lock wait 0.25 s then exit 2 (test: `tests/unit/cli/commands/test_backup.py::test_a_second_backup_exits_2`)
- [x] quick snapshot keep 20, size cap (test: `tests/unit/cli/commands/test_backup.py::test_quick_snapshot`)
- Deliberate differences: an incomplete archive is published as `<name>.incomplete.zip` (the
  reference keeps the normal name); every `*.pid` / `*.lock` is left out, not a fixed list;
  browser profiles are also found by their cookie/login files.
- Not taken: memory-provider paths outside the home (`_external/`), pre-update/pre-migration
  backup flavours (P23).

## Status

Done (2026-10-18). See the slice report for shared files (app.py registry lines, commands/__init__.py,
test_app.py help-order parsing).

## Integration (lead)

- The default output folder is the scope's `HOME` (`tools.builtin.paths.user_home`), not the
  process's: an in-process test run wrote a zip into the real home folder before this fix.
- Slice D's `test_app.py` help-order parsing change was dropped at integration: with the
  registry one-liners always shown (`lazy.py`, slices A/C) no row wraps.
