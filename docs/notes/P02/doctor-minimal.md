# Task: M0 minimal doctor report (P02-22) Phase: 02 Owner: P02 wave-4 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/doctor.py` (the
ordered check list only). Closed before writing.
Evidence: none.

What it does (own words): the milestone-0 demo. `python -m indus.state.doctor_report [-p NAME]
[--json]` boots the process the normal way (`foundation.bootstrap.run`, so profile selection,
`.env`, config and secrets are exactly what the agent would see) and prints five sections: home
(path, profile id and how it was selected: flag / env / sticky / default), config (file path,
`_config_version`, every key the user set with its source `user` or `env-expanded`, the number
of keys left at their defaults), secrets (the configured `security.secret_store`, the backend
in use and the keychain probe result), store (path, journal mode, SQLite version, FTS5 and
trigram availability, schema version, health from P02-20 with its copy line) and logs (the logs
folder). It only reads: the store is opened through `browse`, so a fresh home stays without a
`state.db`. Exit 0 when every section is fine, 1 when any section is `corrupt` or `unavailable`,
never anything else. `minimal_report(scope, *, selection=None) -> Report` builds the same data
for P07-19's `indus doctor --minimal`.

Inputs → outputs: argv `[-p|--profile NAME] [--json]`, the process env → stdout text (one
`label: value` line per fact, labels from `cli.doctor.*` in `locales/en.yaml`) or one JSON
object `{"home", "config", "secrets", "store", "logs", "status", "exit_code"}`; exit code 0 / 1.
Store line: `store: ok (wal, schema 1, fts5 yes, trigram yes)` / `store: missing`.

Files: `src/indus/state/doctor_report.py`, `tests/e2e/__init__.py`,
`tests/e2e/test_doctor_minimal.py`; `locales/en.yaml` (shared: `cli.doctor.*`).

Edge cases / failure modes:
- bootstrap refuses (unknown / deleted profile, keychain demanded but missing) → one line with
  the error's message on stderr, exit 1.
- a broken `config.yaml` → config section `warn` (the agent runs on the last good settings or the
  defaults), exit 0.
- store `busy` → `warn`, exit 0; `missing` → fine on a fresh home, exit 0.
- `--json` output carries no secrets (names of user-set keys only, never values).
- `-p work` reports `<root>/profiles/work` and its store (R03.23).

Our design differences / decisions:
- labels live in code (`LABELS`) and are mirrored in `locales/en.yaml` (a test keeps them equal);
  there is no Python locale loader yet.
- how the profile was selected is read by running `preparse.select_profile` on a copy of the
  environment before bootstrap (bootstrap does not return its selection).

Tests we will write (subprocess, `INDUS_HOME=<tmp>`):
- fresh home → exit 0, `store: missing`, no `state.db` created, the five labels present.
- a store created by a test helper (WAL pinned in-process) → `store: ok (wal, schema 1, fts5 yes,
  trigram yes)`.
- corrupt copy → exit 1 with the corrupt copy line.
- `--json` parses with the five sections; config keys set by the user listed with sources.
- `-p work` targets the work profile.
- labels equal `locales/en.yaml` `cli.doctor.*`; `minimal_report` in-process exit-code table.

## Decisions for the lead
- Bootstrap failures exit 1 (not 2) to keep the "never 2+" rule.

Compare checklist (filled after the rewrite; reopened `hermes_cli/doctor.py` check list):
- [x] section order: environment/home → config → secrets → store → logs (the reference walks
  environment, configuration files, auth, directory structure, then the state database) (test:
  `tests/e2e/test_doctor_minimal.py::test_minimal_report_in_process`)
- [x] M0 lines: fresh home `store: missing`, created store `store: ok (wal, schema 1, fts5 yes,
  trigram yes)`, corrupt copy exit 1 with the copy line, `--json` five sections, `-p work`
  (tests: `::test_fresh_home_reports_a_missing_store_and_creates_none`,
  `::test_a_created_store_reports_ok_with_wal`, `::test_a_corrupt_store_exits_one_with_the_copy_line`,
  `::test_json_has_the_five_sections_and_config_sources`,
  `::test_profile_flag_targets_the_work_profile`)
- Deliberate differences: five sections only (M0); no auth, tools, network or fix mode (P07).
- Not taken: everything beyond the minimal report (P07-19 wires `indus doctor --minimal`).

## Status

Done (2026-10-18). `doctor_report.py` with 14 tests (`tests/e2e/test_doctor_minimal.py`, 6 of
them subprocess runs). Shared: `locales/en.yaml` (`cli.doctor.*`), `docs/internals/traps-ledger.md`
(R03.16, R03.23). Note: on this host (SQLite 3.45.1, in the WAL-reset range) a store created by
the agent itself is DELETE; the demo's "wal" line comes from a store made with the pinned
version, as in the unit tests.
