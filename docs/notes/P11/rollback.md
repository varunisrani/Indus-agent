# Task: rollback (P11-17) Phase: 11 Owner: P11 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/checkpoint_manager.py`
(list, diff and restore functions), `hermes_cli/checkpoints.py` (whole). Closed before writing.
Evidence: none.

What it does (own words): the session's checkpoints, newest first, numbered from 1, each with
time, reason, session/turn and how many files it changed against the one before. `diff(n)` is the
unified diff from checkpoint n to the files on disk now (excluded and oversize files left out).
`restore(n)` first snapshots the current state ("before rollback to <short>", so the restore can
itself be undone), then writes every captured file of checkpoint n back and deletes files that
exist now but were not in checkpoint n — only files the store would capture: excluded paths
(`node_modules`, `.env`, oversize files, …) are never deleted. `restore_file(n, path)` does the
same for one relative path (refused when the checkpoint does not hold it).

Slash: `/rollback` lists; `/rollback N` restores (asks first when
`approvals.destructive_slash_confirm` is on); `/rollback diff N`; `/rollback N <file>`;
`/rollback --all` lists every project of the profile. `/checkpoints on|off|status` switches the
trigger for this session.

Inputs → outputs: (store, workdir, n[, path]) → `RestoreResult{restored_to, safety, removed,
written, not_captured}` / diff text / list of `Checkpoint`.

Files: `src/indus/backends/checkpoints/rollback.py`, `src/indus/cli/slash/checkpoint_cmds.py`
(new), `src/indus/cli/slash/dispatch.py` (handler table), `src/indus/service/commands/registry.py`
(argument hints), `tests/unit/backends/checkpoints/test_rollback.py`.

Edge cases / failure modes:
- N out of range / not a number → a clear message, nothing changes.
- Safety snapshot fails (not "unchanged") → the restore is refused.
- Nested repositories (gitlinks) in the checkpoint → their files are not touched; listed in the
  result.
- Directories left empty by removed files are removed up to the working folder.
- Work runs on throwaway index files, so the project's own index is never disturbed.

Our design differences / decisions:
- Restore deletes files created since the checkpoint (the reference's full restore only checks
  out); excluded and oversize paths are never deleted.
- `restore_file` of a path the checkpoint does not hold is refused rather than deleting the file.
- No "safe" restore mode (agent-write ledger) in this phase.

Tests we will write:
- edit → snapshot → edit → `restore(1)` brings the bytes back; created file removed; excluded
  file kept; safety snapshot listed after restore; `restore_file` touches one file only; diff
  equals `git diff` of the checkpoint against a snapshot of the current tree; bad N / path refused.

## Decisions for the lead
- `/rollback --all` lists all projects (no cross-project restore).
- `approvals.destructive_slash_confirm` is read with a false fallback and has no schema row yet
  (adding one changes the decided-defaults test of `approvals`); the gateway owner (P15) adds the
  row and its default.

Compare checklist (filled after the rewrite):
- [x] list newest first with file counts (test: `tests/unit/backends/checkpoints/test_rollback.py::test_list_newest_first_with_counts`)
- [x] restore brings bytes back, removes created files, safety snapshot (test: `tests/unit/backends/checkpoints/test_rollback.py::test_restore_round_trip`)
- [x] single file (test: `tests/unit/backends/checkpoints/test_rollback.py::test_restore_file_touches_one_file`)
- [x] diff (test: `tests/unit/backends/checkpoints/test_rollback.py::test_diff_matches_git_diff`)
- [x] hash/path validation before git (test: `tests/unit/backends/checkpoints/test_rollback.py::test_bad_numbers_and_paths`)
- Deliberate differences: see above.
- Not taken: safe restore, session-wide diff.

## Status

Done (2026-10-18). Shared: `src/indus/cli/slash/dispatch.py` (HANDLERS), command registry hints.

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- Recorded: `approvals.destructive_slash_confirm` has no schema row yet; it is read with a false fallback and P15 (gateway) adds the row and its default.
- Recorded: `/rollback` and `/checkpoints` handlers exist for the CLI only; TUI, desktop and gateway handlers come with P12, P20 and P15.
