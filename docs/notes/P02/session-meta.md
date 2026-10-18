# Task: per-session metadata API (P02-23) Phase: 02 Owner: wave-4 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — our own API (lead ruling);
which features need it comes from research `01` (goals, loops, heartbeat sections).
Evidence: none.

What it does (own words): features that keep small state per session (goals P10-24, loops
P10-25, heartbeats P10-26, later surfaces) store it as namespaced key/value rows in
`session_meta(session_id, namespace, key, value_json, updated_at, due_at)` instead of adding
columns. Values are JSON (finite numbers only) and at most 64 KiB encoded. A patch is one atomic
JSON merge-patch inside a write transaction, so two writers changing different fields of the same
value both land. `due_at` is an optional next-run time with its own index so a driver that polls
("which heartbeats are due before now?") reads an index range in time order. Rows follow the
session: exported/imported with it (P02-17), removed with it (P02-18, FK cascade plus an explicit
delete), and copied to a branch (P02-11) only for namespaces registered as branch-safe.

Inputs → outputs:
- `get(store, session_id, namespace, key, default=None) -> Any`
- `put(store, session_id, namespace, key, value, *, due_at=None) -> None`
- `patch(store, session_id, namespace, key, merge, *, due_at=KEEP) -> Any` (returns the new value)
- `delete(store, session_id, namespace, key) -> bool`
- `list_entries(store, session_id, namespace) -> dict[str, Any]` (key order)
- `due(store, namespace, before, *, limit=100) -> list[DueEntry]` (due time, then session, key)
- `register_branch_safe(namespace)`, `copy_branch_safe_in(conn, from_id, to_id, *, now) -> int`
- `export_rows_in(conn, session_id)`, `import_rows_in(conn, session_id, rows, *, now) -> int`
- in-transaction forms `put_in`, `patch_in`, `delete_in`.
- `MetaValueTooLarge` (`store.meta_too_large`) for a value over 64 KiB.

Files: `src/indus/state/session_meta.py`, `tests/unit/state/test_session_meta.py`,
`src/indus/foundation/errors.py` (shared: one code row).

Edge cases / failure modes:
- NaN / Infinity or a non-JSON object as a value → `ValueError` (programming error).
- empty or overlong (> 200 chars) namespace or key → `ValueError`.
- unknown session on a write → the foreign-key error (classified `session_row_missing`).
- patch on a missing row creates it; on a non-object value replaces it with the patch applied to
  `{}`; nested objects merge recursively; `None` deletes a field; the result is size-checked.
- `due_at` must be finite; `patch` keeps the stored `due_at` unless given.
- import validates every row before writing any.

Our design differences / decisions:
- `list_entries` instead of `list` (a module function named `list` would shadow the builtin).
- writes use the `ROUTINE` budget; reads the read pool.

Tests we will write:
- put/get round trip (nested JSON, unicode); default for missing; 65 KiB refused, 63 KiB fine.
- two threads patching different fields of one value → both fields present; `None` deletes.
- deleting the session (P02-18) removes its rows; export → import keeps rows (P02-17).
- branch copy only for registered namespaces.
- `due()` returns rows in time order and respects `before`, `limit` and namespace.

## Decisions for the lead
- New code `store.meta_too_large` (class `MetaValueTooLarge` in `session_meta.py`).
- Branch copy keeps `due_at` (a copied heartbeat schedule keeps its next-run time).

Compare checklist (no reference; checked against the P02-23 row):
- [x] put/get/patch/delete/list/due (tests in `tests/unit/state/test_session_meta.py`)
- [x] 64 KiB limit with a typed error (test: `::test_value_over_64_kib_is_refused`)
- [x] concurrent patches both survive (test: `::test_two_writers_patching_different_fields`)
- [x] delete / export-import / branch-safe copy (tests: `::test_rows_go_with_the_session`,
  `::test_export_import_keeps_rows`, `::test_branch_copies_only_branch_safe_namespaces`)
- [x] `due()` in time order (test: `::test_due_returns_rows_in_time_order`)

## Status

Done (2026-10-18). `session_meta.py` with 19 unit tests. Shared: `foundation/errors.py` (code row).
