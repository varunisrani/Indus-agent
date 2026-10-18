# Task: `indus doctor` (P07-19) Phase: 07 Owner: P07 slice D builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/doctor.py` (check order,
summary, ack), `hermes_cli/doctor_config.py`, `hermes_cli/doctor_state.py`,
`hermes_cli/doctor_tools.py` (skimmed: env-file, directory, git/rg checks). Research
`04-cli-config.md` §3.24.1. Closed before writing.
Evidence: none.

What it does (own words): a health check of one profile. Ordered checks each return findings
`Finding{id, severity, message, fix}` (severity ok / info / warn / error); `--fix` repairs what is
safe (create missing dirs, tighten permissions, seed `config.yaml` from the commented example,
quarantine bad `.env` lines) and the summary says "Fixed N issue(s)"; remaining warnings and errors
are numbered with a `--fix` tip; exit 1 while any remain, "All checks passed" and 0 otherwise.
Checks live in a registry (`indus.cli.doctor`) so later phases add their own. `--live` adds one
bounded `GET /models` probe per provider with a credential (timeout
`doctor.live_probe_timeout_s`, default 10). `--ack ID` records an advisory acknowledgement in
`security.acked_advisories` and exits. `--minimal` prints only the M0 report of
`state.doctor_report.minimal_report` (P02-22); `--json` passes through.

Order: python (version, FTS5) → packages → home (dirs 0700, secret files 0600) → config (readable,
schema warnings, version stamp) → env (`.env` denylisted names, non-ASCII lines) → keychain →
auth store → providers (+ live probes) → store (P02 health) → terminal backend → git and rg →
tools.

Files: `src/indus/cli/commands/doctor.py`, `src/indus/cli/doctor/__init__.py`,
`src/indus/cli/doctor/checks.py`, `tests/unit/cli/commands/test_doctor.py`.

Edge cases / failure modes:
- A check that raises becomes one error finding naming the check; the run continues.
- A managed install keeps its own modes (no permission findings, no permission fixes).
- `.env` quarantine moves the bad lines to `.env.quarantine` (0600) and keeps the rest.
- `--live` with no credentialed provider → one info finding; timeouts and HTTP errors are warnings.
- `--minimal` never touches packages, network or backends.

Our design differences / decisions:
- Config validity uses P01's schema validation and the loader's failed-read state; P07-13's
  `config check` may replace the check body once both land.
- Advisory ids are free text until an advisory catalogue exists (no unknown-id refusal).
- Auth store check reads `auth.json` as plain JSON (slice B owns the store API).

Tests we will write:
- Healthy temp home → "All checks passed", exit 0; each seeded fault (missing dir, loose mode,
  broken YAML, denylisted `.env` name, non-ASCII `.env` line, corrupt store) appears; `--fix`
  repairs dirs/perms/.env; exit codes; `--live` with respx and the timeout; `--ack` writes the
  config; `--minimal` equals the P02-22 report text and JSON; a crashing check is contained.

## Decisions for the lead
- "Selected by" for `--minimal` is derived from the scope (sticky file / default / flag-or-env)
  because bootstrap does not expose the selection source; a `CliState` field would make it exact.
- Optional tools (git, rg) and the keychain fallback are `info`, so a healthy home passes without
  them.

Compare checklist (filled after the rewrite; reopened `hermes_cli/doctor.py`, `doctor_config.py`,
`doctor_state.py`, `doctor_tools.py`):
- [x] ordered checks, summary wording ("Fixed N", numbered issues, tip, "All checks passed"),
  exit 1 while issues remain (test: `tests/unit/cli/commands/test_doctor.py::test_each_seeded_fault_is_named`)
- [x] `--ack` saves and stops (test: `tests/unit/cli/commands/test_doctor.py::test_ack_records_the_advisory`)
- [x] `--live` runs after the static checks, bounded (test: `tests/unit/cli/commands/test_doctor.py::test_live_probe_uses_the_configured_timeout`)
- [x] `--fix` scope: dirs, modes, config seed, `.env` (test: `tests/unit/cli/commands/test_doctor.py::test_fix_repairs_dirs_modes_and_env`)
- Deliberate differences: a missing `.env` is information, not a failure (keys may live in the
  keychain); an unknown advisory id is accepted (no catalogue yet) where the reference exits 2;
  the store check only reports and points at `indus sessions repair` instead of repairing in place.
- Not taken: security advisories feed, MCP security, certificates, dashboard import, node/browser
  tooling, npm audit, skills hub, memory provider, cron/checkpoint/gateway checks (their phases
  register checks through `indus.cli.doctor.register`).

## Status

Done (2026-10-18). See the slice report for shared files (app.py registry lines, commands/__init__.py,
test_app.py help-order parsing).

## Integration (lead)

- The config check runs P07-13's `check_settings` (one rule set for both commands); `--fix` seeds a
  missing file with P07-11's `seed_config` (version stamp + commented example).
- `--minimal` "selected by" is exact now: `bootstrap.run_selected` returns the profile
  `Selection` and `entry` stores its source in `CliState.selected_by`; the derived guess is only
  the fallback (tests, embedded callers).
- Slice D's `test_app.py` help-order parsing change was dropped at integration: with the
  registry one-liners always shown (`lazy.py`, slices A/C) no row wraps.
