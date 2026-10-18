# Task: config backups and migrations (P01-12) Phase: 01 Owner: S-config builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/config_backups.py` (all),
`hermes_cli/config_version_stamp.py`, `hermes_cli/config_migrations.py` (the step-list shape only).
Closed before writing.
Evidence: none.

What it does (own words): point-in-time copies of `config.yaml` live in one place,
`<home>/backups/config/config.yaml.<reason>.<YYYYMMDD-HHMMSS>`, for four reasons: `good` (the file
as it was at the last successful parse; the loader's fallback in a fresh process), `pre-write`,
`corrupt` (a broken file the loader could not parse) and `pre-migrate`. A copy is skipped when the
source is missing, empty, or byte-identical to the newest copy for that reason; only the newest 5
per reason are kept; a planted symlink at the target path is never written through; a failed backup
is logged and never blocks the write it precedes. Migrations are an ordered registry
`{from_version: step}`; ours starts empty because our config stamp starts at 1 and we carry no
reference-format migrations (D-16.3). Running `migrate` on an unstamped file only stamps it.

Inputs → outputs:
- `backup_config(config_path, reason, *, clock, keep=5) -> Path | None`, `list_backups(config_path,
  reason=None)` (newest first), `newest_good_bytes(config_path) -> bytes | None`, `backups_dir()`.
- `migrate(scope, *, dry_run=False) -> MigrationReport{from_version, to_version, steps,
  new_defaults, wrote, message}`; `read_version(raw)`; `STEPS`; `CURRENT_VERSION`.

Files: `src/indus/foundation/config/backups.py`, `src/indus/foundation/config/migrations.py`,
`tests/unit/foundation/config/test_backups_migrations.py`.

Edge cases / failure modes:
- the timestamp comes from the injected clock (`now()`), so tests are deterministic; two backups
  within one second for the same reason collide → the second is skipped (target exists).
- backups dir created 0700; copies written 0600 through `fsio` (which refuses symlinks).
- unknown reason → `ValueError` (programming error).
- migrate: missing file → nothing to do; malformed YAML → `ConfigUnreadable` before any backup or
  write; stamp newer than this build → nothing written, message says so; bool / non-int stamp → 0.
- new default keys are listed (dotted paths absent from the file), never written.

Our design differences / decisions:
- no support floor and no legacy steps (D-16.3); no sweep of legacy sibling backup files (we never
  wrote any).
- migrate writes through the P01-11 writer (`replace=True`), so it inherits fail-closed reads,
  template preservation and default stripping.

Tests we will write: six `good` backups → five kept; identical bytes → no new file; empty/missing
source → none; symlink at the target → refused, original and link target intact; failure does not
raise; migrate unstamped → stamps 1, lists defaults, writes none; malformed → error and no backup;
dry run writes nothing; already current → no write; a registered step runs in order (temporary
registry entry in the test).

Compare checklist (filled after the rewrite; reopened the three files above):
- [x] skip identical by byte compare, bounded count, no write through a link (tests: `tests/unit/foundation/config/test_backups_migrations.py::test_identical_bytes_skipped`, `::test_keep_newest_five`, `::test_symlink_at_target_refused`)
- [x] migrate refuses malformed YAML before side effects; reports A → B; does not write defaults (tests: `::test_migrate_malformed_no_side_effects`, `::test_migrate_unstamped_stamps_and_lists_defaults`)
- Deliberate differences: no support floor, no legacy sweep, empty step registry (D-16.3).

## Status

Done (2026-10-18). Nothing outside the task's files.

## Integration (lead)

- Tests take the real `Scope` through `cfg_scope` (thin wrapper over the shared `scope` fixture);
  no behaviour change.
