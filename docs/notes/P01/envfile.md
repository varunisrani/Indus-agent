# Task: `.env` file io (P01-14) Phase: 01 Owner: P01 S-env builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/config.py` lines 78–228
(writer denylist and the name rule), 2689–2720 (value quoting, line-shape detection), 2787–2870
(save / remove). Research `docs/plan/research/04-cli-config.md` §3.14.1–3.14.2, traps R04.10, R04.12.
Closed before writing.
Evidence: none.

What it does (own words): `foundation.envfile` is the one reader and the one writer for a profile's
`.env`. The reader turns the file into a name → value dict; the writer changes a few names in place
and leaves every other byte of the file alone. The two share one line parser, so any line the reader
treats as an assignment is also a line the writer will find, replace or delete (R04.10). Without this
a hand-written `export K=v` or `K = v` line is invisible to the writer: a save appends a duplicate and
a delete leaves a line that brings the value back on the next load. The writer refuses names that
steer how child processes run (loader, interpreter, git helper, shell init, our own location and
policy switches) so no settings UI can be turned into code execution (R04.12).

Inputs → outputs:
- `parse_line(line) -> (name, value) | None`, `parse_text(text) -> dict`, `read_env_file(path) -> dict`
  (missing file → `{}`; BOM dropped; NUL bytes dropped with one warning).
- `set_values(path, {name: value | None}, *, windows=None) -> EnvWriteReport` — locked
  (`FileLock(<path>.lock, 15 s)`), atomic (`fsio.atomic_write_bytes`, new file 0600, mode kept),
  records written names in `<path>.keys` (names only; read by P01-15).
- `check_writable_name(name, *, windows=None)`, `is_denied(name, *, windows=None)`,
  `quote_value(value)`, `custom_key_name(slug)`.

Files: `src/indus/foundation/envfile.py`, `tests/unit/foundation/test_envfile.py`,
`tests/unit/foundation/_stubs.py` (test-only stand-ins for `fsio` until the config slice lands).

Edge cases / failure modes:
- Line shapes: `K=v`, `export K=v`, `export\tK=v`, `K = v`, quoted (`'…'` literal, `"…"` with `\"` and
  `\\`), inline `#` only after whitespace when unquoted, text after a closing quote ignored, blank
  and comment lines, lines without `=` or with a bad name ignored. Duplicates: the last one wins.
- Writer: first defining line is replaced in place, any further defining lines are removed (so a
  duplicate cannot shadow the new value); `None` removes every defining line; absent → appended (a
  missing final newline is added first).
- Values: `\r`/`\n` stripped; non-ASCII stripped with a warning that lists at most 5 positions;
  empty stays `K=`; values with `#`, quotes or any whitespace are double-quoted with escapes.
- Names: must match `^[A-Za-z_][A-Za-z0-9_]*$` (set and delete); denylist checked on set only
  (removing a dangerous line is never an escalation). Windows: name compare is case-insensitive for
  the denylist and for finding existing lines.
- Lock timeout → the fsio error propagates; nothing is written. Crash mid-write → fsio guarantees
  the old file is intact.

Our design differences / decisions:
- One parser for read and write instead of two matchers kept in sync by hand.
- Set also drops duplicate defining lines (the reference only replaces the first).
- No `${VAR}` interpolation in `.env` values (kept literal). Config `${VAR}` expansion is P01-10.
- Errors are `EnvNameError(ValueError)` (bad name) and `EnvNameDenied(EnvNameError)` (denylist), plain
  `ValueError` subclasses so CLI/web can turn them into messages; no `IndusError` code yet.
- Denylist is a superset of the reference: we also keep the session/ACP routing names.
- Warnings go to the module logger, and are also returned in the report for UIs to show.

Tests we will write: round trip `K=a b#c`; every line shape replaced and deleted with no
resurrection; denylist table (prefixes, names, `INDUS_*` allowed, Windows case-insensitive); name
rule; non-ASCII positions; quoting table; BOM/NUL reading; 0640 kept, new file 0600; `.keys`
record; concurrent writers in two processes (`slow`); seeded-random printable-ASCII round trip
(hypothesis is not in the dev group yet).

## Decisions for the lead
- hypothesis (pin table says P00-11) is not in `[dependency-groups].dev`, so the "hypothesis" checks
  are written as seeded random loops. Swap to `@given` when the dep lands.
- Error types are `ValueError` subclasses, not `IndusError` (the tree in P01-20 has no `.env`
  code). Easy to re-parent later.
- Names written are recorded in `<path>.keys` beside the file (P01-15 calls it `<home>/.env.keys`).

Compare checklist (filled after the rewrite; reopened `hermes_cli/config.py` 78–228, 2689–2870):
- [x] every reader line shape found by the writer (test: `tests/unit/foundation/test_envfile.py::test_set_replaces_every_line_shape`, `::test_delete_removes_every_line_shape`)
- [x] denylist covers every reference name, ours a superset (test: `::test_denylist_superset_of_reference_names`)
- [x] quoting rule (test: `::test_quote_value_table`)
- [x] lock + atomic + mode (tests: `::test_existing_mode_is_kept`, `::test_new_file_is_owner_only`, `::test_concurrent_writers_both_land`)
- [x] Windows case-insensitive compare (test: `::test_denylist_case_insensitive_on_windows`)
- [x] custom key name (test: `::test_custom_key_name`)
- Deliberate differences: duplicates removed on set; no interpolation; denylist not applied on delete
  (same as the reference); the reference normalises every line when it rewrites the file, we keep
  untouched lines byte for byte (only NUL bytes and a leading BOM are dropped); the non-ASCII
  warning goes to the logger and the report, not straight to stderr.
- Not taken: managed-install / admin-pinned write refusal (I20, P3) and publishing to the live env
  (the caller rebuilds the scope's env view through P01-15).

## Status

Done (2026-10-18). Uses `indus.foundation.fsio` (P01-07, config slice) through a test-only stand-in.

## Integration (lead)

- Tests use the real `fsio` (`FileLock(path, timeout=)`, `atomic_write_bytes(path, data)` match
  its signatures); the `_stubs.install()` calls (also in the spawned writer) are gone.
