# Task: `indus status` + `indus dump` (P07-21) Phase: 07 Owner: P07 slice D builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/status.py` (sections,
provider label), `hermes_cli/dump.py` (layout, key display). Research `04-cli-config.md`
§3.24.2, §3.24.4. Closed before writing.
Evidence: none.

What it does (own words): `status` is one screen about this profile: model, active provider and
where it came from, the providers that have a usable credential (the same set `/model` offers),
terminal backend, session count and store size, gateway "not running" and scheduled jobs 0 until
those phases land. `--full` adds paths, the key names that are set (never values) and the config
keys you set; `--deep` (implies full) adds the store integrity check and the backend probe.
`dump` prints a compact plain-text block for support requests: versions, OS, install method,
profile, model/provider/backend, config keys set, and each provider key as `set`/`not set` (with
`--show-keys`: first and last four characters).

Files: `src/indus/cli/commands/status.py`, `src/indus/cli/commands/dump.py`,
`tests/unit/cli/commands/test_status.py`.

Edge cases / failure modes:
- No provider usable → "none (run `indus setup`)"; unknown configured provider → its name with the
  resolution problem.
- Missing store → 0 sessions, no store created (read-only open).
- A key shorter than 12 characters is never partly shown, even with `--show-keys`.

Our design differences / decisions:
- Keys are only names or masks; no value reaches the screen without `--show-keys`.
- Gateway and scheduler rows are placeholders until P14/P15.

Tests we will write:
- Goldens with a fake environment (fixed clock, temp home, fake keys); no secret value appears in
  any output (scan for the fake secrets), including `--full`/`--deep`; `--show-keys` masks.

Compare checklist (filled after the rewrite; reopened `hermes_cli/status.py`, `hermes_cli/dump.py`):
- [x] one screen by default, `--full`/`--all`, `--deep` implies full (test: `tests/unit/cli/commands/test_status.py::test_status_full_and_deep_show_names_only`)
- [x] dump keys as set / not set, `--show-keys` first and last four (test: `tests/unit/cli/commands/test_status.py::test_dump_show_keys_masks`)
- Deliberate differences: credentials are found in the secret store and env view (not only
  `.env`), so dump has no "shell only" remark; gateway and jobs are fixed placeholders.
- Not taken: platform rows, emergency-stop banner, portal sections, SDK versions.

## Status

Done (2026-10-18). See the slice report for shared files (app.py registry lines, commands/__init__.py,
test_app.py help-order parsing).

## Integration (lead)

- `status` has a "Signed in" row and `dump` a "signed in" line (from `AuthStore.signed_in()`); a
  signed-in provider also counts as credentialed.
- Slice D's `test_app.py` help-order parsing change was dropped at integration: with the
  registry one-liners always shown (`lazy.py`, slices A/C) no row wraps.
