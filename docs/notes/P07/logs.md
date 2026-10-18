# Task: `indus logs` (P07-20) Phase: 07 Owner: P07 slice D builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/logs.py` (whole).
Research `04-cli-config.md` §3.24.5. Closed before writing.
Evidence: none.

What it does (own words): reads the profile's log files under `<home>/logs/` in P01's line format
(`YYYY-MM-DD HH:MM:SS LEVEL logger [session:turn]: message`). `logs [NAME]` prints the last `-n`
(50) lines of `agent` (default), `errors`, `gateway`, `gui` or `update`; filters AND together:
`--level` (minimum), `--session` (substring), `--since` (`30s/5m/2h/1d` before now on the scope's
clock), `--component` (`agent`, `tools`, `cli`, `gateway`, `cron` → logger-name prefixes). A line
without a timestamp (a traceback frame) belongs to the record above it and shares its verdict.
`-f` keeps printing new matching lines, polling every 0.3 s, and starts over when the file is
rotated or truncated. `logs list` shows every log file with size and age.

Files: `src/indus/cli/commands/logs.py`, `tests/unit/cli/commands/test_logs.py`,
`tests/fixtures/logs/agent.log`.

Edge cases / failure modes:
- Missing file → "No <name> log yet" with the path, exit 1; unknown name / bad `--since` /
  bad `--level` → usage error, exit 2.
- Continuation lines before the first stamp pass unless a time or level filter is on.
- Rotation: inode change or a size below the read position → reopen from the start; a file that
  vanished for a moment is waited for.
- Ctrl+C while following → "--- stopped ---", exit 0.

Our design differences / decisions:
- The follow loop takes a pause function (default: a blocking pause on the scope's clock), so
  tests drive rotation without real sleeps.
- Component prefixes are ours (`indus.core`/`indus.providers`/`indus.learning` = agent, …).

Tests we will write:
- Fixture logs: each filter and combinations; traceback continuation kept and dropped with its
  record; `--since` with a fake clock; follow across a rotation (temp files); list sizes;
  missing file exit 1.

Compare checklist (filled after the rewrite; reopened `hermes_cli/logs.py`):
- [x] filters AND; a line without a stamp takes its record's verdict; unstamped lines before the
  first record pass unless a time/level filter is on (test: `tests/unit/cli/commands/test_logs.py::test_stray_lines_before_the_first_stamp`)
- [x] `--since` units s/m/h/d (test: `tests/unit/cli/commands/test_logs.py::test_since_uses_the_scope_clock`)
- [x] `list` with sizes and ages (test: `tests/unit/cli/commands/test_logs.py::test_list_and_missing`)
- [x] follow polls every 0.3 s (test: `tests/unit/cli/commands/test_logs.py::test_follow_survives_rotation_and_truncation`)
- Deliberate differences: follow reopens the file after rotation or truncation (the reference keeps
  reading the old handle); unknown names / bad values are click usage errors (exit 2, the
  reference exits 1); the update log of a named profile is read from the root's logs folder.
- Not taken: desktop, handoff and MCP log names (no such files before P12/P13/P20).

## Status

Done (2026-10-18). See the slice report for shared files (app.py registry lines, commands/__init__.py,
test_app.py help-order parsing).

## Integration (lead)

- Follow-after-rotation behaviour recorded as D-16.100.
- Slice D's `test_app.py` help-order parsing change was dropped at integration: with the
  registry one-liners always shown (`lazy.py`, slices A/C) no row wraps.
