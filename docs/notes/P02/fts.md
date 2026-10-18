# Task: FTS5 setup and fail-open (P02-14) Phase: 02 Owner: P02 wave-4 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_fts.py` (all),
`hermes_state_common.py` (the projection view block and the FTS trigger DDL around it, the
trigram source view, the stale / deferral key names, and the cross-process rebuild admission
lock), `tests/hermes_state/test_fts_index_fail_open.py`,
`tests/hermes_state/test_fts_projection_integrity.py`. Closed before writing.
Evidence: none.

What it does (own words): two derived search indexes sit beside the transcript. They must never
cost a transcript write. `messages_fts` (unicode61 words) reads its external content through a
view that is a pure function of the message row: the text of the row (list/dict content stored
as `PARTS_PREFIX + JSON` contributes only its text parts), the tool name and the tool-call JSON;
tool-role rows contribute only their first 8,192 characters (R03.7). Because the view, the
triggers and FTS5's own `rebuild` / `integrity-check` all compute the same expression, the
index can never drift from what the checker re-reads. `messages_trigram` (trigram tokenizer,
SQLite ≥ 3.34) does substring search; it leaves out tool rows, tool-call JSON and sessions whose
source is `cron` or `subagent`. When SQLite reports corruption inside an index (result code
`SQLITE_CORRUPT_VTAB`, or a message naming one of our FTS objects) during a write or a search,
we fail open (R03.6): one transaction writes `meta.fts_stale = {reason, at}` and drops every FTS
trigger, then the canonical write is retried and lands; searches use LIKE while the marker
stands. Nothing on a live path ever runs a full `rebuild`. A later open rebuilds: only under a
cross-process lock file and only when no other in-process generation of the store is open;
otherwise it defers with a growing backoff and finally gives up (recorded in `meta`).

Inputs → outputs:
- `EXTENSION` (a `schema.SchemaExtension`, run inside the schema-init transaction):
  `needs_work(conn)` is read-only; `apply(conn)` drops orphan shadow tables by exact name, creates
  views / virtual tables / triggers, fills a newly created index with `rebuild`, detaches triggers
  this SQLite cannot run (no FTS5, or no trigram tokenizer) and records why.
- `attach(store)`: installs the writer hooks (`set_fts_fail_open`, `add_periodic(1000, merge)`) and
  runs stale recovery once.
- `fail_open(store, exc) -> bool`, `detach_in(conn, reason, *, now)`; `is_stale(conn)`,
  `stale_info(conn)`; `present(conn) -> FtsPresence(base, trigram)` (SELECT probes only).
- `recover(store) -> str` (`not_stale` / `rebuilt` / `deferred` / `waiting` / `gave_up` /
  `unsupported`); `rebuild(store) -> bool`; `deferral(conn)`.
- `merge(store)` / `merge_in(conn)`: bounded `'merge'` (≤ 4 commands × 500 pages, stop when a
  command did no work).
- `integrity_ok(conn) -> bool | None` (None = no FTS tables); strict check where the connection
  can write, `PRAGMA integrity_check(<table>)` on a read-only connection.
- `fts5_available()`, `trigram_supported()`, `text_sql(alias)` (the parts-aware text
  expression, shared with search's LIKE route), table / view / trigger names.

Files: `src/indus/state/fts.py`, `tests/unit/state/test_fts.py`; wiring in
`src/indus/state/registry.py` (shared).

Edge cases / failure modes:
- FTS5 missing from the linked library → no FTS objects, search on LIKE, logged once; a store
  moved from a host with FTS5 has triggers we cannot run → they are dropped and `fts_stale` set
  (otherwise every message insert would fail).
- trigram missing (SQLite < 3.34) → base index only; existing trigram triggers dropped and
  `fts_trigram_stale` set; when trigram returns the table is dropped, recreated and rebuilt.
- cascade delete of a session: the session row is already gone when the message delete
  trigger runs, so the trigram delete trigger asks the index itself (`<table>_docsize`) whether
  the row was indexed instead of re-testing the session's source.
- content with the parts prefix but invalid JSON → indexed as stored; CASE branches keep
  `json_*` from ever running on invalid text (a JSON error inside a trigger would fail writes).
- the NUL in the prefix: SQLite's text `substr` is unreliable around it, so the prefix is compared
  and cut as bytes (`CAST(… AS BLOB)`, hex literal) and the rest cast back to text.
- `json_each()` cannot be used in the projection: FTS5 reads its content view as `main.<view>`
  and SQLite 3.45 then looks for a table `main.json_each`; the array is walked with a recursive
  counter instead (works in views and triggers).
- a setup statement this SQLite rejects (other than a lock) is rolled back to a savepoint: the
  store opens without FTS (LIKE search) and the setup is not retried on every open.
- fail-open recursion: a detach that itself fails raises; a second fail-open on the same thread
  with nothing left to drop is allowed once (a sibling detached first), then refused so a
  non-trigger FTS error cannot loop the writer.
- quarantine / replaced while detaching: the detach is a normal guarded write, so it raises the
  sticky error and commits nothing.
- rebuild admission: lock file `<dir>/.fts_rebuild.lock`, 120 s wait polled every 0.1 s, holder
  record `{pid, start, pidns, host, at}`; after 5 s of waiting a holder that is provably dead
  (same host and PID namespace, PID gone or start time changed) is broken by unlinking the file
  and retrying on a new inode; anything uncertain waits / defers.
- deferral: attempt n waits `min(60 · 2^(n−1), 3600)` s; give up after 10 attempts or once
  1,800 s passed since the first deferral; gave-up stays until an explicit rebuild.
- periodic merge runs in the writer's housekeeping slot; failures are logged, never raised.

Our design differences / decisions:
- names `messages_fts`, `messages_trigram`, views `messages_fts_rows` / `messages_trigram_rows`,
  triggers `messages_fts_ai|ad|au`, `messages_trigram_ai|ad|au`; meta keys `fts_stale`,
  `fts_trigram_stale`, `fts_rebuild_deferral`.
- no deferred chunked backfill, no high-water gating in triggers, no CJK bigram index (P2), no
  storage-layout versioning (schema 1 is born with the aligned projection).
- recovery drops and recreates the indexes (works even when a shadow table is missing; `rebuild`
  alone fails then), in one write transaction at open.
- the trigram exclusion is by source only (`cron`, `subagent`) as the phase row says; the
  reference also excludes rows of sessions carrying a delegate marker — not taken.

Tests we will write:
- projection: 20,000-char tool row matches a word in its first 8,192 chars, not in the tail;
  strict integrity-check passes after insert / update / delete churn and a session cascade;
  parts content indexes only its text.
- fail-open: dropping a shadow table makes the next append fail inside FTS → the append lands,
  `fts_stale` set, triggers gone, `search` uses LIKE; quarantine during the detach commits nothing.
- recovery: deferred while another generation is open, rebuilt after it closes (fake clock);
  backoff table 60/120/…/3600 and give-up after 10 attempts or 1,800 s; lock held by a live
  process → deferred; dead holder broken.
- capabilities: patched old SQLite → only base index; FTS5 missing → no FTS objects and triggers
  of a moved store dropped; orphan shadow tables dropped.
- merge runs after 1,000 writes and stops when idle; `integrity_ok` None / True / False.

## Decisions for the lead
- Wiring: `registry.EXTENSIONS` holds `fts.EXTENSION` and `StoreHandle.__init__` calls
  `fts.attach(self)` after the read pool exists (writer hooks + one recovery attempt). Change:
  move both lines if the lead prefers a hook list.
- The fail-open path drops triggers but leaves the damaged virtual tables in place (searches
  avoid them while stale); recovery replaces them.
- `fts.py` reads `PARTS_PREFIX` from `indus.state.messages` lazily (inside the SQL builders), so
  no import cycle with slice A; a stand-in `messages.py` exists only in this worktree.

Compare checklist (filled after the rewrite; reopened `hermes_state_fts.py`, the FTS block and
the admission lock of `hermes_state_common.py`, both test files):
- [x] projection is a pure function of the row; view, triggers and rebuild share one expression
  (tests: `tests/unit/state/test_fts.py::test_long_tool_row_is_indexed_as_its_prefix_only`,
  `::test_strict_integrity_survives_updates_deletes_and_cascades`)
- [x] update trigger narrowed to indexed columns with an IS NOT guard (same test: updates of
  `active` and of content both keep the strict check green)
- [x] trigram leaves out tool rows, tool-call JSON, `cron` / `subagent` sources (test:
  `::test_trigram_leaves_out_tool_rows_tool_calls_and_internal_sources`)
- [x] marker + trigger drop in one transaction; canonical write retried; no rebuild on live paths
  (tests: `::test_index_damage_during_append_fails_open`,
  `tests/unit/state/test_search.py::test_damaged_index_during_search_fails_open`)
- [x] detach waits out a sibling's lock on the write budget; quarantine during the wait commits
  nothing (tests: `::test_detach_waits_out_a_sibling_holding_the_write_lock`,
  `::test_quarantine_while_detaching_commits_nothing`)
- [x] orphan shadow tables dropped by exact name only, never while the virtual table exists
  (test: `::test_orphan_shadow_tables_are_dropped_and_the_index_rebuilt`)
- [x] rebuild admission: lock file, bounded wait, holder record, dead-holder break, fail closed
  (tests: `::test_dead_holder_is_broken_after_five_seconds`,
  `::test_live_holder_is_waited_for_until_the_timeout`,
  `::test_holder_in_another_namespace_or_host_is_never_probed`,
  `::test_rebuild_lock_held_by_a_live_process_defers`)
- [x] missing tokenizer disables only the trigram index; missing FTS5 disables FTS with one log
  (tests: `::test_old_sqlite_builds_only_the_word_index`,
  `::test_trigram_detached_on_an_old_build_and_restored_on_a_new_one`,
  `::test_without_fts5_no_index_is_made_and_moved_triggers_are_dropped`)
- [x] gap found while writing (not in the reference's shape): the trigram delete trigger keyed on
  the session's source misses rows deleted by a session cascade (the session row is gone when
  the message trigger runs) — fixed by asking `messages_trigram_docsize`; pinned by the cascade
  step of `::test_strict_integrity_survives_updates_deletes_and_cascades`.
- [x] gap found at compare: none further on the write path; the reference stops a refused detach
  by returning "not detached" so the original error surfaces — ours raises the detach's own
  error (a quarantine raises `StoreCorrupt`, as the reference's test expects).
- Deliberate differences: recovery drops and recreates the indexes (no chunked backfill or
  high-water gating); deferral backoff and give-up recorded in `meta.fts_rebuild_deferral`; the
  holder record also names the host; after breaking a dead holder the wait keeps the remaining
  120 s budget (the reference restarts a 5 s budget); merge returns the number of commands that
  did work (contract says `None`; callers may ignore it).
- Not taken: CJK bigram index and its loadable tokenizer (P2), storage-layout versions and the
  legacy inline index upgrade, the trash-table teardown, the delegate-marker trigram exclusion.

## Status

Done (2026-10-18). `fts.py` with 33 unit tests (`test_fts.py`). Shared: `registry.py` (two lines:
`EXTENSIONS = [fts.EXTENSION]`, `fts.attach(self)` in `StoreHandle.__init__`),
`docs/internals/traps-ledger.md` (R03.6, R03.7). Stand-in: `src/indus/state/messages.py`
(`PARTS_PREFIX` only).
