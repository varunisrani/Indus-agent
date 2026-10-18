# Task: `config check` + `config migrate` (P07-13) Phase: 07 Owner: P07 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/config.py`
(`_cmd_config_check`, `_cmd_config_migrate`, `validate_config_structure` head),
`hermes_cli/config_migrations.py` (skim: step runner shape and function list only),
`hermes_cli/doctor_config.py` (skim: function list, structure check),
`hermes_cli/config_check_diagnostics.py` (skim). Research `04-cli-config.md` §3.11.6, §3.13.5.
Closed before writing.
Evidence: none.

What it does (own words): `config check` reads the user's file as it is on disk (no defaults) and
reports, without changing anything:
- the version stamp (current, older → "run `indus config migrate`", newer than this build);
- values the schema rejects (wrong type, a word outside the allowed set) → errors;
- unknown keys: inside a known section (the schema keeps them but nothing reads them) and at the
  top level (with a did-you-mean when one is close) → warnings;
- structure: `fallback_providers` must be a list of mappings each naming `provider` and `model`;
  every `providers.<name>` must be a mapping; a list or mapping slot holding a quoted string that
  looks like a list or mapping (`"[a, b]"`) → errors / warnings;
- deprecated env names present in `.env` or the environment (the table is empty today; the hook
  exists so later phases add rows).
It exits 1 when any error was reported, 0 otherwise; a missing file is fine (built-in values).

`config migrate` runs P01's migration runner from the file's stamp: malformed YAML is refused
before any side effect (no backup, nothing written); our own steps run in order (the registry is
empty at stamp 1, D-16.3); new default keys are listed, never written; it prints
`Config version A → B`. It is a dry run unless `--apply`; with `--apply` the runner backs the file
up with reason `pre-migrate` first and saves through the P01 writer.

Inputs → outputs: `config check` → report lines, exit 0/1; `config migrate [--apply]` → report
lines, exit 0, or 1 with a stderr reason (unreadable, malformed, managed).

Files: `src/indus/cli/commands/config_check.py`, `tests/unit/cli/commands/test_config_check.py`.

Edge cases / failure modes:
- File missing → check says built-in settings are in use, exit 0; migrate says nothing to migrate.
- Stamp newer than the build → reported, never rewritten.
- A top-level value that is not a mapping (`- a`) → malformed, refused.
- Managed install with `--apply` → the writer refuses; exit 1.

Our design differences / decisions:
- Stamp starts at 1, no reference migrations (D-16.3).
- `migrate` is a dry run by default (`--apply` writes), matching the pattern the reference uses for
  its retired-setting migrations; the reference's main migrate writes immediately and prompts.
- No prompting for missing env vars (none are universally required; the provider is picked by
  `indus setup` / `indus model`).

Tests we will write:
- each seeded problem reported (type error, unknown section key, unknown top-level with
  did-you-mean, bad fallback entry, non-mapping provider, quoted list, deprecated env name via a
  test-only row, old stamp); exit codes; malformed YAML → refused with no backup; test-only step
  migrates and stamps with `--apply` and leaves the file alone without it; new defaults reported,
  not written; newer stamp left unchanged.

## Decisions for the lead
- `DEPRECATED_ENV` in `config_check.py` is the table later phases extend (name → replacement).

Compare checklist (filled after the rewrite; reopened `hermes_cli/config.py` check/migrate parts):
- [x] version line and migrate hint (test: `test_config_check.py::test_old_stamp_reported`)
- [x] structural problems reported (test: `::test_seeded_problems_each_reported`)
- [x] malformed YAML refused before side effects (test: `::test_migrate_malformed_refused_without_backup`)
- [x] new defaults reported, never written (test: `::test_migrate_reports_new_defaults_not_written`)
- [x] steps run from the stamp and stamp the file (test: `::test_test_only_step_migrates_and_stamps`)
- Deliberate differences: dry run by default; exit 1 on errors in `check` (the reference always
  exits 0 from check).
- Not taken: required/optional API key tables (provider-specific, belongs to `status`/`doctor`),
  plugin and MCP diagnostics (P13), `.env` reformatting during migrate.

## Status

Done (2026-10-18).

## Integration (lead)

- Findings carry a `topic` (`read`, `version`, `value`, `unknown`, `env`) so `indus doctor` files
  them under its own ids; `indus doctor`'s config check is now `check_settings` plus its own
  missing-file / failed-read handling.
- A file stamped newer than this build is an error (was a warning), so `config check` exits 1 for
  it, as doctor already did (D-16.95).
