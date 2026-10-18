# Task: export and import (JSONL) (P02-17) Phase: 02 Owner: wave-4 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_portability.py`
(`export_session`, `export_session_lineage`, `export_all`, the import validation helpers,
`_import_session_row`, `_attach_import_parents`, `import_sessions`; the cron/listing/foreign-import
parts skimmed only to skip them), `tests/hermes_state/test_session_import_value_validation.py`.
Closed before writing.
Evidence: none.

What it does (own words): a session can leave the store as JSON lines and come back. Export
writes one header object per session (all its columns, the system prompt text instead of its
hash, its `session_meta` rows) followed by one object per message in id order, with message
content decoded back to text / parts so the file is plain JSON. Lineage mode walks the
compression chain root → tip and writes one logical document: a small lineage line naming the
segments, then every segment in order. `export_all(path)` streams every session into a file
(written beside the target and moved into place, owner-only). Import is the dangerous direction:
the file may be hand-edited or hostile. The whole payload is parsed and checked before anything
is written, and every problem is reported with its line number (not only the first). Only then
does one write transaction insert the sessions, messages and metadata. Without the checks a NaN
or a 2^70 integer reaches SQLite binding or later JSON serialisers and breaks the session APIs;
without the tombstone check a deleted conversation could come back.

Inputs → outputs:
- `export_session(store, session_id, *, lineage=False, include_inactive=False, guard=False)
  -> Iterator[dict]` — `{"kind": "session", ...}` then `{"kind": "message", ...}` objects;
  lineage mode starts with `{"kind": "lineage", "segments": [...]}`. Unknown id → nothing.
  `guard=True` (callers that hold the whole document in memory) counts the live messages first
  and raises `ResumeTooLarge(numeric=4131, count=…)` above `sessions.resume_message_limit`
  (20,000; 0 disables).
- `export_all(store, path) -> ExportReport(sessions, messages, path)`.
- `import_jsonl(store, source) -> ImportReport` where `source` is a path or an iterable of text
  lines. Report: `ok`, `imported`, `skipped` (ids already present), `detached` (parents not
  found or cycle edges cut), `errors: [ImportIssue(line, session_id, message)]`.

Files: `src/indus/state/portability.py`, `tests/unit/state/test_portability.py`,
`tests/fixtures/state/export_small.jsonl`.

Edge cases / failure modes:
- limits: more than 500 sessions → one error and nothing else checked further for count;
  10,000 messages per session, 50,000 in total, 5 MiB per session (UTF-8 bytes of its lines),
  25 MiB in total — each one error at the line where it was crossed.
- NaN / Infinity / -Infinity tokens and overflowing floats (`1e309`) anywhere in a line, nested
  included → error naming the line and the JSON path; detected while parsing (hooks), so no
  non-finite value survives into the normalised payload.
- integers in integer columns (token counters, `token_count`) clamped to SQLite's signed 64-bit
  range; `true`/`false` in an integer column become 1/0; text where a number belongs is refused.
- not JSON, not an object, unknown `kind`, a message whose session has no header, duplicate
  session ids, empty role, content that is not text / list / object / null, timestamps that are
  not finite numbers → one error each.
- a literal string content that starts with our internal parts prefix stays a string (content is
  always re-encoded through `messages.encode_content`, never written raw).
- ids already in the store → skipped and reported; ids tombstoned here → the whole import is
  refused (one error per id), nothing written.
- parents: attached only when the parent exists in the store or arrives in the same payload;
  walking up from the parent must not reach the child (cycle) — the edge that would close the
  loop is the one cut; otherwise detached. Self-parent is a cycle.
- activity (`last_activity_*`), `rewind_count`, compression cool-down/streak counters, and
  `last_read_at` are reset; `message_count` / `tool_call_count` are recomputed from the live
  rows; `auto_archived` only survives together with `archived`; uids are kept.
- rows exported with `include_inactive` come back with their `active` / `compacted` flags.

Our design differences / decisions:
- our own line format (`kind` per line), not the reference's one-dict-per-session export; no
  import of the reference's store (D-16.41); no timing block in the export.
- system prompt travels as text; import re-derives the sha256 key.
- the message row id is not exported (order is the line order; fresh ids on import).
- `session_meta` rows ride on the session header (`meta`) and go through P02-23's
  `import_rows_in`.

Tests we will write:
- export → wipe (new store) → import: sessions, message content (text, parts, tool calls), uids,
  titles, flags and metadata equal; counters recomputed; activity reset.
- the checked-in fixture imports and its re-export matches it.
- lineage export: lineage line then root and child in order; inactive rows only when asked.
- 501 sessions → exactly one error; NaN in a nested field → error names the line; `1e309`; huge
  int clamped; per-session message limit; every error reported (two bad lines → two errors).
- existing id skipped; tombstoned id refused with nothing written; cycle A→B→A → one edge cut;
  missing parent detached; a failure late in the payload writes nothing.
- the 4131 guard; a prefixed literal string stays a string.

## Decisions for the lead
- A tombstoned id refuses the whole import (not just that session) so "partial failure writes
  nothing" holds; switch to per-session skip by filtering instead of erroring if preferred.
- `content` must be text, a list, an object or null; numbers are refused (the reference let finite
  numbers through).

Compare checklist (filled after the rewrite; reopened `hermes_state_portability.py` export and
import parts, `test_session_import_value_validation.py`):
- [x] limits 500 / 10,000 / 50,000 / 5 MiB / 25 MiB, the session count as one error (tests:
  `tests/unit/state/test_portability.py::test_too_many_sessions_is_one_error`,
  `::test_per_session_message_limit`, `::test_total_limits`)
- [x] validate everything first, every error reported with its line, nothing written on failure,
  a failure inside the write rolls back (tests: `::test_every_error_is_reported_with_its_line`,
  `::test_late_failure_writes_nothing`, `::test_bad_metadata_rows_are_reported`)
- [x] non-finite numbers anywhere refused, nested and inside JSON-text columns (tests:
  `::test_nan_in_a_nested_field_names_the_line`, `::test_overflowing_float_is_refused`)
- [x] ints clamped to the 64-bit range (test: `::test_huge_int_is_clamped`)
- [x] existing ids skipped; tombstoned ids refused (tests: `::test_existing_id_is_skipped`,
  `::test_tombstoned_id_is_refused`)
- [x] parents attached only when present, cycle edge cut (tests: `::test_cycle_is_cut_once`,
  `::test_parents_attach_only_when_present`)
- [x] runtime fields reset, counters from live rows, archived rows stay archived, uids kept
  (tests: `::test_round_trip_through_a_fresh_store`, `::test_inactive_rows_only_when_asked`,
  `::test_uids_and_counters`)
- [x] lineage export, 4131 guard, fixture (tests: `::test_lineage_export`,
  `::test_export_guard_4131`, `::test_fixture_imports_and_reexports`)
- [x] prefixed literal content stays text; lone surrogates scrubbed (tests:
  `::test_prefixed_literal_stays_a_string`, `::test_lone_surrogate_is_scrubbed`)
- [x] gap found: `auto_archived` must not survive without `archived` — reset unless archived
  (test: `::test_round_trip_through_a_fresh_store`).
- [x] gap found: NULL in a NOT NULL DEFAULT column broke the insert — only non-NULL values are
  bound so declared defaults apply (test: `::test_defaults_for_missing_fields`).
- Deliberate differences: per-line `kind` format; lineage as a lineage line + segments;
  out-of-range ints in integer columns are clamped (the reference refuses some and defaults
  others); numbers as content are refused; a title already used here gets ` #N` instead of
  failing the whole import on the unique index; malformed JSON text in JSON-ish message columns
  is kept verbatim (only parsed values are checked), as in the reference.
- Not taken: foreign-history import, donor adoption, timing evidence, in-place compaction
  projection (P08), a source filter on `export_all`, cron/listing helpers.

## Status

Done (2026-10-18). `portability.py` with 27 unit tests and the checked-in fixture `tests/fixtures/state/export_small.jsonl` (written by `export_all` from the test seed). Runs against stand-ins for `sessions`, `lineage`, `messages` until the wave-4 merge.
