# Task: Pre-import profile selection (P01-05) Phase: 01 Owner: P01 S-core builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/main.py`
(`_apply_profile_override` 595–700 and the `_scan_profile_flag` helper it calls, 477–535).
Closed before writing.
Evidence: R04.2.

What it does (own words): before any app module is imported, works out which profile this run
serves. Order: an explicit `-p NAME` / `--profile NAME` / `--profile=NAME` anywhere in argv (skipping
values of value-taking flags and everything after `--` or a nested-tail flag such as `--args`); else
an `INDUS_HOME` that already points at `<root>/profiles/<id>`; else the sticky `active_profile`
file (not for supervised gateway children); else the default profile. It returns a `Selection` and
the stripped argv; it writes nothing, the caller exports `INDUS_HOME`.

Inputs → outputs: `argv`, `env`, `value_flags`, `nested_tail_flags`, `recovery_commands` →
`Selection{home, profile_id, argv, source, warning}` or `ProfileNotFound` / `InvalidProfileId` /
`StickyProfileMissing`.

Files: `src/indus/foundation/preparse.py`, `tests/unit/foundation/test_preparse.py`.

Edge cases / failure modes:
- `-p` as the last token (no value) → left in argv for the CLI parser to report.
- `-m -p` where `-m` takes a value → `-p` is the model name.
- named profile tombstoned or missing → `ProfileNotFound` naming `indus profile list`.
- sticky profile missing → `StickyProfileMissing`, unless the command is a recovery command
  (default + warning).
- any unexpected exception → default profile + one warning; a profile bug never stops startup.
- under `sudo`, a profile missing in root's home is looked up in the invoking user's root.

Our design differences / decisions:
- Value flags and nested-tail flags are parameters (P06 derives them from the click app).
- The one-time "sticky profile used while `INDUS_HOME` was unset" note is returned in
  `Selection.warning` (first time per process) for the caller to log; preparse never prints.
- Allowed `indus.*` imports: `foundation.home`, `foundation.profiles` and the stdlib-only
  `foundation.errors` leaf (errors are needed to raise typed `ProfileError`s).

Tests we will write: every argv shape in the row; env home trusted; sticky BOM; supervised child
skips sticky; tombstoned flag; sticky missing with and without recovery; injected exception →
default + warning; fresh-interpreter import check.

## Decisions for the lead
- `foundation.errors` is allowed in the preparse import closure (stdlib-only leaf).
- Invalid `-p` value raises `InvalidProfileId` (exit 1 later), not a silent skip as the reference
  does after a subcommand; easy to relax once P06 knows which subcommands own a `-p`.

Compare checklist (filled after the rewrite; reopened `hermes_cli/main.py` 477–700):
- [x] value-flag skip, `--` and `--args` tails (tests:
  `tests/unit/foundation/test_preparse.py::test_value_flag_hides_profile`, `test_nested_tail_untouched`)
- [x] env home under `profiles/` trusted (test: `test_env_home_under_profiles_trusted`)
- [x] sticky file only when not supervised (test: `test_supervised_child_ignores_sticky`)
- [x] failure → default + warning (test: `test_unexpected_error_falls_back_to_default`)
- Deliberate differences: theirs exits the process; ours raises typed errors and returns data.
  Theirs silently skips an option-looking `-p` value after a subcommand; ours raises.
- Not taken: desktop SSH backend and s6 supervisor detection (only `INDUS_GATEWAY_SUPERVISED=1`).

## Status

Done (2026-10-18).
