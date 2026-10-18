# Task: `indus checkpoints` CLI (P07-24) Phase: 07 Owner: P07 slice D builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/checkpoints.py`
(whole), `website/docs/user-guide/checkpoints-and-rollback.md` (CLI table). Research
`04-cli-config.md` §3.22. Closed before writing.
Evidence: none.

What it does (own words): inspects and trims the checkpoint store (file snapshots taken before
destructive edits; the store itself arrives in P11). `status`/`list [--limit 20]` prints the store
path, total size, project count and one row per project (workdir, commits, size, age `Ns/Nm/Nh/Nd
ago`, live/orphan), newest first. `prune [--retention-days 7] [--max-size-mb 500] [--keep-orphans]
[-f]` deletes projects whose workdir is gone (listed and confirmed unless `-f`), then projects
untouched for the retention window, then the oldest remaining until the store fits the cap; a
manual prune never waits for the automatic once-a-day marker. `clear [-f]` wipes the store after a
confirmation. Without a terminal the destructive actions need `-f` (exit 1 otherwise).

The CLI is built against `indus.backends.checkpoint_api.CheckpointStore` (`list_projects`, `size`,
`prune`, `clear`); the pure `plan_prune` decides the order so the P11 store and the fake agree.
Until a store is registered (`set_store_factory`) the commands say no checkpoints exist.

Files: `src/indus/cli/commands/checkpoints.py`, `src/indus/backends/checkpoint_api.py`,
`tests/unit/cli/commands/test_checkpoints.py`.

Edge cases / failure modes:
- Orphan deletion is bound to what was shown: a project that turns orphan after the preview is
  not deleted in that run.
- Missing last-touch → age "—"; a future stamp → "now".
- Store errors during prune are counted, never raised; `clear` that fails → exit 2.

Our design differences / decisions:
- No legacy-archive subcommand (we never had a v1 layout).
- The size cap drops whole oldest projects in the plan; a store may trim finer (commits) in P11.

Tests we will write:
- Fake store: age formatting table; prune order (orphans, stale, oldest to the cap);
  `--keep-orphans`; `clear` without `-f` on non-TTY exits 1; status rows and limit.

## Decisions for the lead
- Cross-phase: P11's shadow store implements `CheckpointStore` and calls `set_store_factory`.

Compare checklist (filled after the rewrite; reopened `hermes_cli/checkpoints.py`,
`website/docs/user-guide/checkpoints-and-rollback.md`):
- [x] status rows newest first with `Ns/Nm/Nh/Nd ago`, `--limit 20`, `list` alias, bare = status (test: `tests/unit/cli/commands/test_checkpoints.py::test_status_rows`)
- [x] prune confirms orphans unless `-f`, binds to the shown set, declined = exit 1 (test: `tests/unit/cli/commands/test_checkpoints.py::test_prune_orphans_need_a_yes`)
- [x] clear: nothing → 0, declined → 1, failure → 2 (test: `tests/unit/cli/commands/test_checkpoints.py::test_clear_with_force_and_failure`)
- Deliberate differences: the plan (orphans, stale, oldest to the cap) is computed by the CLI's
  shared `plan_prune` and handed to the store.
- Not taken: `clear-legacy` (no v1 layout to migrate from).

## Status

Done (2026-10-18). See the slice report for shared files (app.py registry lines, commands/__init__.py,
test_app.py help-order parsing).

## Integration (lead)

- Prune / clear confirmations use `cli.prompts.confirm` (moved from `cmdkit`).
- Slice D's `test_app.py` help-order parsing change was dropped at integration: with the
  registry one-liners always shown (`lazy.py`, slices A/C) no row wraps.
