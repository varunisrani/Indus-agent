# Task: schema 1 (P02-06) Phase: 02 Owner: P02-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_schema.py`
(`_init_schema` 924–1009, `_run_data_migrations` 1009–1100, `_reconcile_columns`, the message-uid
backfill for the chunking shape), `hermes_state_common.py` (the declared DDL block and index
list only — for which tables/indexes exist, not their spelling), `hermes_state_reconcile.py`
(function list: the column-diff idea), test names in `tests/hermes_state/test_runtime_schema.py`
and `test_schema_read_probe.py`. Closed before writing.
Evidence: none.

What it does (own words): `state.schema` declares schema 1 as Python data (tables, columns,
indexes, triggers) in our own names (D-16.2: not compatible with the reference's database; no
legacy migrations). `init(conn, *, clock)` makes any store match the declaration in one
`BEGIN IMMEDIATE` transaction: create what is missing, then add every declared column a table
lacks (a diff of `PRAGMA table_info` against the declaration — new columns and tables never
need a version bump), then create indexes and triggers (after the reconcile, so an index on a
column that was just added works), stamp a fresh store (version 1, `store_id`, `created_at`),
and mint the once-per-file generation token and `application_id`. Opening must wait out a
sibling's write lock instead of failing the whole open (R03.9): lock errors during init are
retried with 250–1000 ms jitter for up to 20 s, then logged and raised. `run_migrations` is the
runner for future *data* migrations: each step works through id ranges of 2,000 in its own short
write transaction, spends at most ~1 s per open after its first chunk, keeps a resume cursor in
`meta` and a done marker, so a big store converges over several opens without holding the write
lock for minutes (R03.15).

Inputs → outputs: `init(conn, *, clock, sleep=None, rng=None, extensions=()) -> InitReport`
(`fresh`, `version`, `added_columns`, `generation`, `application_id`);
`read_version(conn) -> int | None`; `SCHEMA_VERSION = 1`; `TABLES`, `INDEXES`, `TRIGGERS`;
`MigrationStep(name, high_water, run_chunk, chunk=2000)`;
`run_migrations(write, steps, *, clock, time_box=1.0) -> dict[str, bool]`.
A store whose version is newer than 1 → `StoreUnavailable(kind="incompatible")`.
`extensions` are callables run inside the init transaction after the core DDL (P02-14 FTS setup
plugs in here).

Files: `src/indus/state/schema.py`, `tests/unit/state/test_schema.py`.

## Tables (schema 1)

Times are REAL epoch seconds (UTC); flags are INTEGER 0/1; `json` = JSON text.

- `schema_version(version)` — one row, `1`.
- `meta(key PK, value)` — store-wide key/value. Keys used: `store_id`, `created_at`,
  `db_file_generation`; `migration:<step>:cursor` / `migration:<step>:done` (runner);
  reserved for wave 4: `tombstone:<session id>` (P02-18), `fts_stale`, `fts_rebuild*` (P02-14),
  `last_auto_prune`, `last_vacuum` (P02-19).
- `system_prompts(hash PK = sha256 hex, text)`.
- `sessions` — `id` PK, `source` NOT NULL, `created_source`, `user_id`, `model`,
  `model_config` json, `system_prompt_hash` → `system_prompts(hash)`, `parent_session_id` →
  `sessions(id)` ON DELETE SET NULL (branch/compression children are orphaned, P02-18 deletes
  delegate children itself), `started_at` NOT NULL, `ended_at`, `end_reason`, counters
  (`message_count`, `tool_call_count`, `api_call_count`, `input_tokens`, `output_tokens`,
  `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`; all NOT NULL DEFAULT 0),
  workspace (`cwd`, `git_root`, `git_branch`), route/cost (`billing_provider`,
  `billing_base_url`, `estimated_cost_usd`, `actual_cost_usd`, `cost_status`), titles (`title`,
  `title_source` = user|llm|derived), activity (`last_activity_at`, `last_activity_note`,
  `last_activity_provenance`), compression anti-thrash (`compression_cooldown_until`,
  `compression_cooldown_error`, `compression_fallback_streak`, `compression_ineffective_count`,
  `compression_overload_streak`), `profile_id`, `rewind_count`, flags (`archived`,
  `auto_archived`, `pinned`, `hidden`), `last_read_at` (NULL read, 0 unread).
- `messages` — `id` INTEGER PK AUTOINCREMENT (ids never reused; transcript order), `session_id`
  → `sessions(id)` ON DELETE CASCADE, `role`, `content`, `tool_call_id`, `tool_calls` json,
  `tool_name`, `timestamp` NOT NULL, `token_count`, `finish_reason`, `reasoning`,
  `reasoning_details` json, `platform_message_id`, `active` DEFAULT 1, `compacted` DEFAULT 0,
  `api_content`, `message_uid` (32 hex; trigger mints one when inserted NULL),
  `tool_call_uids` json.
- `model_usage` — `session_id` → `sessions(id)` ON DELETE CASCADE, `model`,
  `billing_provider`, `billing_base_url`, `task` ('' = main loop; all four NOT NULL DEFAULT ''),
  `api_call_count`, the five token counters, `estimated_cost_usd`, `actual_cost_usd`,
  `cost_status`, `first_seen`, `last_seen`; PK (session_id, model, billing_provider,
  billing_base_url, task).
- `compression_locks(session_id PK → sessions ON DELETE CASCADE, holder, acquired_at,
  expires_at)`.
- `turn_leases(conversation_id PK, holder, acquired_at, expires_at)` — used from P17.
- `session_meta(session_id → sessions ON DELETE CASCADE, namespace, key, value_json,
  updated_at, due_at)`, PK (session_id, namespace, key) — P02-23; `due_at` (nullable REAL) is an
  indexed next-run time so `due(namespace, before)` is an index range, not a JSON scan.

Indexes: sessions by `source`, `parent_session_id`, `started_at DESC`, `last_activity_at`,
`system_prompt_hash`; unique `title` WHERE NOT NULL; messages by (session_id, id),
(session_id, timestamp), (session_id, active), (session_id, message_uid), partial
(session_id, platform_message_id) WHERE NOT NULL; locks/leases by `expires_at`; model_usage by
`model`; session_meta by (namespace, due_at) WHERE due_at NOT NULL.
Trigger: `messages_mint_uid` (AFTER INSERT WHEN `message_uid` IS NULL).
FTS tables, views and triggers are not part of this declaration (P02-14 adds them through
`extensions`).

Edge cases / failure modes:
- second init on a current store: no DDL changes, no rows written (checked by `total_changes`).
- a hand-made legacy `sessions` lacking columns gains them, rows intact; a column that cannot
  be added (NOT NULL without default) is logged and skipped, lock errors re-raise for the retry.
- non-lock init errors propagate at once; the transaction is rolled back.
- concurrent first openers: `INSERT OR IGNORE` + read back for every stamp; first one wins.
- newer schema version → `StoreUnavailable(kind="incompatible")`, nothing written.
- migration runner: the first chunk always runs; later chunks stop once ~1 s (clock) passed
  since the first ended; cursor saved after every chunk; done marker stops further work.

Our design differences / decisions:
- declaration as data (no SQL parsing for the reconcile).
- no primary-key heal paths (nothing legacy to heal), no NULL-`active` repair, no FTS layout
  versioning, no startup-watchdog lease (P15).
- the uid backfill shape is kept only as the generic runner; schema 1 has no data steps.
- holder logging at init lock exhaustion names the path only (holder scans are P17).

Tests we will write:
- fresh init → every table/index/trigger, version 1, non-zero `application_id`, token,
  `store_id`, `created_at`; second init writes nothing.
- hand-made `sessions` missing two columns gains them with data intact.
- row inserted without uid gets 32 hex; duplicate non-NULL title rejected, two NULLs fine.
- init on a connection whose file is locked by a second connection for 3 s of fake time succeeds;
  locked for longer than 20 s → raises; non-lock error → raised at once.
- newer version → incompatible.
- dummy 10,000-row migration: 5 chunks over two opens, cursor advancing, each chunk its own
  short transaction, done marker.

## Decisions for the lead
- Writable open of a store with a version newer than 1 refuses (`StoreUnavailable`
  `incompatible`) rather than writing into an unknown layout.
- `session_meta.due_at` added beyond the P02-23 column list (indexed next-run time).
- `parent_session_id` uses ON DELETE SET NULL, messages/usage/locks/session_meta ON DELETE CASCADE.

Compare checklist (filled after the rewrite; reopened `hermes_state_schema.py` 756–1100,
the DDL list in `hermes_state_common.py`):
- [x] every MVP column for P02-08..23 is declared (sessions, messages, usage, locks, leases,
  session_meta); new columns arrive by reconcile without a version bump (test:
  `tests/unit/state/test_schema.py::test_reconcile_adds_missing_columns_and_keeps_rows`); version starts at 1;
  our own names (D-16.2, D-16.3)
- [x] indexes created after the reconcile; uid trigger; unique non-NULL title (tests:
  `::test_message_without_uid_gets_one`, `::test_titles_unique_when_not_null`)
- [x] lock patience on init with 250–1000 ms jitter for 20 s, non-lock errors at once (tests:
  `::test_init_waits_out_a_three_second_lock`, `::test_init_gives_up_after_the_patience`,
  `::test_non_lock_init_error_is_raised_at_once`)
- [x] generation token first-opener-wins and derived 31-bit `application_id` (test:
  `::test_fresh_init_creates_and_stamps_everything`)
- [x] chunked, time-boxed migration runner with cursor + done marker (test:
  `::test_chunked_migration_is_time_boxed_and_resumes`)
- [x] improvement kept: a store that already matches is checked read-only, so a second open takes
  no write lock at all (test: `::test_second_init_writes_nothing`) — the reference avoids only the
  no-op UPDATE.
- Deliberate differences: declaration as data; a newer schema version is refused; each migration
  chunk is its own `Writer.write` transaction.
- Not taken: PK heal paths, NULL-`active` repair, FTS layout versioning, startup-watchdog lease
  (no legacy stores; P15 owns the watchdog).

## Status

Done (2026-10-18). `schema.py` with 14 unit tests. Tables documented above for wave 4.
