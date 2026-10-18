# Task: prune and VACUUM (P02-19) Phase: 02 Owner: wave-4 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_maintenance.py`
(`prune_sessions`, the prune filter / continued-ancestor / pinned-tail SQL ideas,
`sweep_orphaned_sessions`, `_freelist_ratio`, `vacuum`, `maybe_auto_prune_and_vacuum`),
`hermes_state_common.py` (the last-active expression and the freelist threshold only),
`hermes_state_holders.py` (`foreign_state_db_holders`, Linux branch only),
`hermes_state_repair.py` (the non-blocking auto-maintenance lock only), the stale-open source
list in `hermes_state.py`, `tests/hermes_state/test_auto_vacuum_holder_gate.py`,
`test_prune_activity_recency.py`. Closed before writing.
Evidence: none.

What it does (own words): old conversations are removed in bulk and the file is shrunk, without
ever cutting into something still in use. `prune` picks ended sessions whose last activity — the
freshest of the activity stamp, the newest message and the start (and, for rows the startup
reaper closed, the close time) inside the sane time window — is older than the cutoff. A
compressed-away segment ages with its conversation: it stays while any later segment stays.
Pinned rows (and continuations of a pinned segment) and rows under a live lease or lock are
spared. A dry run only reports. Deleted rows get 7-day sweep tombstones (P02-18) and each pass
also drops tombstones that aged out. `auto_maintain(scope)` is the opt-in startup job
(`sessions.auto_prune`): at most one process at a time (non-blocking `.auto-maintenance.lock`),
at most once a day (`meta.last_auto_prune`), prune by `sessions.retention_days`, then close open
rows of our own short-lived sources (cli, cron, acp, subagent, tool) that went quiet for the
whole window, with `startup_orphan_reap` (still resumable). It never raises — every problem
becomes a skip with a log line (R03.25). It may VACUUM, but only when rows were pruned, the last
VACUUM is at least 30 days old, more than 25 % of pages are free, and no one else holds the
file. `vacuum` refuses on a quarantined or replaced handle and while another in-process
generation or (Linux) another process has the store or a sidecar open (a scan failure counts as
an unknown holder) unless forced; it merges FTS segments first, checkpoints PASSIVE, VACUUMs,
checkpoints TRUNCATE and re-records the file identity so the guard does not mistake its own
rewrite for a replacement. `stats` is a read-only snapshot where each number may fail alone.

Inputs → outputs:
- `prune(store, older_than_days=90, *, source=None, dry_run=False) -> PruneReport(ids,
  spared_pinned, spared_guarded, dry_run)` (`count` property).
- `close_stale_open_in(conn, *, older_than_days, now) -> list[str]`.
- `auto_maintain(scope, *, store=None) -> MaintenanceReport(ran, skipped, pruned, closed,
  vacuumed, freelist_ratio, holders, error)`.
- `vacuum(store, *, force=False) -> VacuumReport(size_before, size_after, holders, forced)`;
  `VacuumRefused(holders)` (`store.vacuum_refused`).
- `scan_holders(path) -> list[Holder(pid, what)]`, `holders_of(store) -> list[Holder]`.
- `stats(store) -> StoreStats(page_count, page_size, size_bytes, freelist_count, freelist_ratio,
  rows)`.

Files: `src/indus/state/maintenance.py`, `tests/unit/state/test_maintenance.py`,
`tests/integration/state/test_vacuum_holders.py` (+ `__init__.py`),
`src/indus/foundation/errors.py` (shared: one code row).

Edge cases / failure modes:
- negative `older_than_days` → `ValueError` (a future cutoff would match everything);
  negative / missing / non-integer `retention_days` → auto skip with a warning.
- timestamps outside [2020-01-01, now + 1 day] are ignored when computing last activity.
- a corrupt `last_auto_prune` / `last_vacuum` value counts as "never".
- the lock is held by another process → skip; the store file does not exist → skip (never
  create a store just to maintain it).
- holder scan: our own pid skipped; processes whose fd table we may not read are skipped;
  `/proc` unreadable → one unknown holder (pid -1).
- VACUUM cannot run inside a transaction: it runs on the writer connection under the writer's
  mutex, outside `write()`.

Our design differences / decisions:
- the holder scan is Linux-only until P17 (other systems report no foreign holders); a refused
  manual VACUUM raises `VacuumRefused` naming the holders; automatic maintenance only skips.
- the stale-open sources are our own five (no kanban / api_server / recovered sources yet).
- no gateway-heartbeat gate on closing stale rows (P15).
- `vacuum` writes `meta.last_vacuum` itself, so manual runs also reset the 30-day clock.

Tests we will write:
- prune table: 100-day-old ended session pruned, 10-day kept, open kept, pinned kept, recent
  activity stamp keeps an old row, compression root kept while its tip is recent and removed
  with an idle chain, guarded row spared, source filter, dry run writes nothing, tombstones are
  sweep ones.
- auto-maintain: off by default, twice within 24 h of fake time runs once, again after 24 h;
  negative retention skips with a warning; held lock skips; stale open cli row closed but a
  messaging-source row kept; an injected failure becomes `error`, never raises.
- auto-VACUUM gate table (pruned / interval / ratio / holder).
- vacuum: in-process second generation refuses, `force` runs; quarantined handle refuses;
  identity re-recorded so the next write works; scan failure counts as unknown holder.
- stats fields; stats while another connection holds the write lock returns at once.
- `slow` integration: a second process holding the file → refused with its pid, allowed after
  it exits.

## Decisions for the lead
- New code `store.vacuum_refused` (class `VacuumRefused` in `maintenance.py`).
- `last_activity` counts `ended_at` only for `startup_orphan_reap` rows (they age from the reap).

Compare checklist (filled after the rewrite; reopened `hermes_state_maintenance.py` 140–586,
the Linux holder scan, the two tests):
- [x] ended-only candidates by last activity, window-clamped, reap rule (tests:
  `tests/unit/state/test_maintenance.py::test_prune_table`, `::test_recent_activity_keeps_an_old_row`)
- [x] compression ancestors kept while a later segment stays; pinned tail spared (tests:
  `::test_compression_root_kept_while_its_tip_is_recent`, `::test_pinned_segment_spares_its_continuations`)
- [x] guarded spared, stale guards reaped (not in a dry run); children orphaned, not cascaded
  (tests: `::test_guarded_rows_are_spared`, `::test_prune_does_not_cascade_delegates`)
- [x] negative window refused; source filter (test: `::test_prune_source_filter_and_bad_window`)
- [x] every prune pass reaps aged sweep tombstones (test: `::test_prune_reaps_aged_tombstones`)
- [x] auto: opt-in, lock, 24 h throttle recorded even on zero, retention guard, no store → skip,
  stale-open close of own sources, never raises (tests: `::test_auto_maintain_is_off_by_default`,
  `::test_auto_maintain_runs_once_a_day`, `::test_auto_maintain_records_a_run_that_pruned_nothing`,
  `::test_auto_maintain_bad_retention_skips`, `::test_auto_maintain_lock_held_skips`,
  `::test_auto_maintain_without_a_store_file`, `::test_auto_maintain_closes_stale_own_sources`,
  `::test_auto_maintain_never_raises`)
- [x] auto-VACUUM gates: pruned, 30 days, ratio > 25 %, holders (tests: `::test_auto_vacuum_gates`,
  `::test_auto_maintain_vacuums_when_due`, `::test_auto_maintain_skips_vacuum_with_a_holder`)
- [x] vacuum: quarantine refusal, FTS merge first (failure tolerated), PASSIVE → VACUUM →
  TRUNCATE, identity re-recorded (tests: `::test_vacuum_refuses_a_quarantined_handle`,
  `::test_vacuum_shrinks_merges_first_and_rerecords_identity`, `::test_vacuum_survives_a_failing_fts_merge`)
- [x] in-process and foreign holder gates, scan failure = unknown holder (tests:
  `::test_vacuum_refuses_another_generation`, `::test_scan_failure_is_an_unknown_holder`,
  `tests/integration/state/test_vacuum_holders.py::test_second_process_blocks_vacuum`,
  `::test_auto_maintenance_only_skips_with_a_holder`)
- [x] stats independent fields, non-blocking (tests: `::test_stats_fields`,
  `::test_stats_on_a_busy_store_do_not_block`)
- [x] gap found: a guarded continuation must also keep its compression ancestors — guarded and
  pinned rows leave the chosen set before the ancestor fix-point (test:
  `::test_guarded_continuation_keeps_its_ancestors`).
- Deliberate differences: Linux-only scan (P17 widens it); uninspectable foreign processes are
  skipped rather than matched on their command line; manual refusal is a typed error; prune
  always spares guarded rows (the reference only in automatic mode); an unreadable freelist
  ratio skips the auto-VACUUM (the reference falls back to the time gate); a `last_auto_prune`
  in the future does not block a run.
- Not taken: matching holders of an unlinked earlier file generation (P17), ghost-session prune,
  auto-archive, the prune filter surface beyond `source` (P06/P07 CLI), startup-watchdog leases (P15).

## Status

Done (2026-10-18). `maintenance.py` with 41 unit tests and 2 `slow` integration tests (`tests/integration/state/test_vacuum_holders.py`). Shared: `foundation/errors.py` (code row), `docs/internals/traps-ledger.md` (R03.25).
