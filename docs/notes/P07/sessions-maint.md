# Task: `indus sessions` maintenance side (P07-15) Phase: 07 Owner: P07 slice D builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/subcommands/sessions.py`
(prune / archive / optimize / repair flags). Research `03-state.md` §3.22–3.24; traps R03.25,
R03.16. Closed before writing.
Evidence: none.

What it does (own words): housekeeping of the session store. `prune` deletes ended conversations
that went quiet (bare = 90 days), always showing what it would delete first; `archive` soft-hides
matching conversations; `optimize` gives free pages back (VACUUM) and skips with a holder list when
another process or handle has the store open; `repair` checks the file and rebuilds the search
index (backup first); `recover` salvages every readable row into a new file; `import` loads a JSONL
export. All of it goes through the landed P02 APIs: `maintenance.prune` / `vacuum` / `holders_of`,
`deletion.delete_many`, `sessions.set_archived`, `browse.integrity`, `fts.rebuild`,
`portability.import_jsonl`.

Inputs → outputs:
- `prune [filters] [--include-pinned] [--never-active] [--force] [--dry-run] [-y]` → preview list;
  `--dry-run` stops there; otherwise confirm (non-TTY needs `-y`, else exit 1); a bulk delete
  refuses while another process holds the store unless `--force` (exit 1, holders listed).
- `archive [filters] [--include-open] [--dry-run] [-y]` → needs at least one filter.
- `optimize [--force]` → sizes before/after, or "Skipped: …" with the holders (exit 0).
- `repair [--check-only] [--no-backup]` → report; check-only exits 1 when a problem was found and
  writes nothing; structural damage points to `recover` (exit 1).
- `recover --output FILE` → per-table copied counts; the output must not exist.
- `import FILE` → imported/skipped counts or every validation problem (exit 1).

Files: `src/indus/cli/commands/sessions_maint.py`, `tests/unit/cli/commands/test_sessions_maint.py`.

Edge cases / failure modes:
- Empty plan → "Nothing to prune." exit 0.
- Pinned rows are spared unless `--include-pinned`; guarded rows (live turn, compression lock) are
  skipped and reported.
- `optimize` with a held handle in this process or another process → skip, never an error (R03.25).
- `repair` on a missing store → "nothing to repair" exit 0; on a damaged file never opens it for
  writing (that would quarantine it); `recover` reads it read-only.
- `recover` leaves out the runtime tables (compression locks, turn leases): a partial lease could
  make a later run believe work is in flight.

Our design differences / decisions:
- Plain age prune (optionally one source) is P02's own `prune` (sweep tombstones, compression-chain
  rules). A prune with any other filter, `--include-pinned` or `--never-active` deletes exactly the
  previewed ids through `delete_many` (user tombstones).
- `--never-active` selects untitled rows with no messages older than AGE (default 30 days).
- Repair here = integrity check + FTS rebuild; a full schema rescue on a scratch copy is left to a
  later phase (the salvage path covers structural damage).

Tests we will write:
- Prune dry run lists then deletes with `-y`; pinned kept; non-TTY without `-y` exits 1; repair
  check-only reports without writing; optimise under a held handle skips with a message;
  recover copies rows; import JSONL round trip and a bad payload exits 1; archive needs a filter.

## Decisions for the lead
- `optimize` skip exits 0 (skip, not refuse); `--force` runs anyway.
- `archive` with no filter is refused (exit 2) instead of archiving everything.

Compare checklist (filled after the rewrite; reopened `hermes_cli/subcommands/sessions.py`):
- [x] bare prune = 90 days; any other filter alone matches every age (test: `tests/unit/cli/commands/test_sessions_maint.py::test_filtered_prune_and_include_pinned`)
- [x] `--include-pinned`, pinned kept otherwise (test: `tests/unit/cli/commands/test_sessions_maint.py::test_prune_dry_run_lists_then_yes_deletes`)
- [x] `--never-active` default 30 days (test: `tests/unit/cli/commands/test_sessions_maint.py::test_never_active`)
- [x] `--force` against holders (test: `tests/unit/cli/commands/test_sessions_maint.py::test_prune_refuses_while_the_store_is_held`)
- [x] optimize under a live holder skips (test: `tests/unit/cli/commands/test_sessions_maint.py::test_optimize_under_a_held_handle_skips_with_a_message`)
- [x] repair `--check-only` / `--no-backup`, backup first (test: `tests/unit/cli/commands/test_sessions_maint.py::test_repair_rebuilds_the_index_after_a_backup`)
- Deliberate differences: prune does not route through a running gateway (none until P15);
  repair is integrity check + search-index rebuild, and structural damage goes to `recover`
  instead of an in-place schema rescue; `import FILE` reads our own JSONL export (no foreign
  agent histories).
- Not taken: `--include-archived`, `optimize-storage`, `set-journal-mode`, `clean-markers`,
  `repair-routing`, `repair-profiles`, `repair-prompts`, `retitle-skills` (not in P07-15's row).

## Status

Done (2026-10-18). See the slice report for shared files (app.py registry lines, commands/__init__.py,
test_app.py help-order parsing).

## Integration (lead)

- Prune / archive confirmations use `cli.prompts.confirm` (moved from `cmdkit`).
- Slice D's `test_app.py` help-order parsing change was dropped at integration: with the
  registry one-liners always shown (`lazy.py`, slices A/C) no row wraps.
