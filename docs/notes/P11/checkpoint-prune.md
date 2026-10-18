# Task: checkpoint pruning (P11-18) Phase: 11 Owner: P11 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/checkpoint_pruning.py`
(whole), `tools/checkpoint_maintenance.py` (prune order, marker, status, clear),
`hermes_cli/checkpoints.py` (whole). Closed before writing.
Evidence: none.

What it does (own words): keeps the shared store bounded. A full prune deletes projects whose
folder is gone (unless orphans are kept) and projects untouched for `retention_days` (7), runs
`git gc --prune=now`, then drops the oldest checkpoints (never a project's newest one) until the
store is at most `max_total_size_mb` (500). The automatic prune runs at most once per
`min_interval_hours` (24): it writes `<home>/checkpoints/.last_prune` before it starts (so a prune
that dies costs one skipped interval, not a gc on every start); an explicit prune ignores the
marker. The snapshot path trims each project to `max_snapshots` (20) by rewriting its history
(same trees, messages and dates) and leaves the gc to the next prune (`.gc-pending`).

The `CheckpointStore` protocol methods carry the `indus checkpoints` CLI unchanged:
`list_projects` (commits, exclusive size, last touch, folder exists), `size` (bytes of
`<home>/checkpoints`), `prune(plan)` (deletes the planned projects in order, then gc; failures
counted), `clear()` (removes `<home>/checkpoints`).

Files: `src/indus/backends/checkpoints/prune.py`, `src/indus/backends/checkpoints/store.py`
(protocol methods), `tests/unit/backends/checkpoints/test_prune.py`,
`tests/integration/backends/test_checkpoints_cli_real_store.py`.

Edge cases / failure modes:
- A ref moved by another writer between read and rewrite → `update-ref` with the old value
  fails, the rewrite is abandoned, counted as an error.
- Corrupt marker → treated as "never pruned".
- A project's last checkpoint is never dropped by the size pass; when only newest checkpoints
  remain the pass stops (reported as still over the cap).
- Clear on a missing store → nothing to clear.

Our design differences / decisions:
- The automatic prune keeps orphans (a missing folder may be an unmounted drive); only the
  explicit, confirmed CLI prune deletes them. Retention still applies to existing folders.
- Size pass: one checkpoint at a time, globally oldest first, gc and re-measure after each, at
  most 200 drops per run.
- No legacy-store commands (D-16.3).

Tests we will write:
- orphan project removed; stale (8 days, fake clock) removed; size cap drops oldest checkpoints;
  marker prevents a second auto-prune within 24 h and allows it after; explicit prune ignores
  the marker; `max_snapshots` trims history keeping trees; P07's CLI cases against the real store.

## Decisions for the lead
- The CLI's `prune(plan)` deletes whole planned projects (the plan's `oversize` keys included);
  the finer per-checkpoint size pass belongs to `prune_store` (automatic prune). Per-project size
  is what deleting that project would free.

Compare checklist (filled after the rewrite; reopened `tools/checkpoint_pruning.py`,
`tools/checkpoint_maintenance.py`):
- [x] order orphans → stale → gc → size (test: `tests/unit/backends/checkpoints/test_prune.py::test_prune_store_order`)
- [x] marker claim before work, 24 h (test: `tests/unit/backends/checkpoints/test_prune.py::test_marker_throttles_auto_prune`)
- [x] history rewrite keeps trees/dates; ref moved concurrently → error (test: `tests/unit/backends/checkpoints/test_prune.py::test_trim_keeps_newest`)
- [x] CLI against the real store (test: `tests/integration/backends/test_checkpoints_cli_real_store.py`)
- Deliberate differences: unattended prune never deletes orphans (reference: same, via config);
  no per-call store-busy refusal (we wait on the file lock up to 15 s); the snapshot path trims
  only the per-project count (the reference also drops one round of oldest checkpoints there when
  the store is over the size cap; ours leaves size to the daily prune); `checkpoints.auto_prune`
  defaults to true (reference: off).
- Not taken: legacy archives, footprint notice.

## Status

Done (2026-10-18).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- Decisions accepted: `checkpoints.auto_prune` defaults to true (D-16.135); orphans are never deleted automatically; the size cap applies only in the daily prune.
