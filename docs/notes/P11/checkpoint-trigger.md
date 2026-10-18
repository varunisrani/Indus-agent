# Task: checkpoint trigger (P11-16) Phase: 11 Owner: P11 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/checkpoint_manager.py`
(the per-turn dedup and `ensure_checkpoint` call shape), `website/docs/user-guide/checkpoints-and-rollback.md`
(skimmed: when snapshots happen, the flags). Closed before writing.
Evidence: none.

What it does (own words): when checkpoints are on, the first file-changing operation of a turn
snapshots the session's working folder before the change lands. File-changing means `write_file`,
`patch` (both forms) or a `terminal` command that deletes, moves or overwrites files (`rm`,
`rmdir`, `unlink`, `shred`, `mv`, `truncate`, `sed -i`/`perl -i`, a truncating `>` redirect to a
file, `git reset/checkout/clean/restore/stash/switch/rebase/merge/revert/apply/am/rm/mv`, `dd`,
`find -delete`) or anything the P05 approval detector already calls dangerous. One snapshot per
turn per session; a later turn takes another one. A failure never blocks the tool: the tool result
carries `checkpoint_warning` once in that turn.

On/off: `checkpoints.enabled` (default false), the `--checkpoints` flag (process default for this
run) and `/checkpoints on|off` (per session, wins over both). `checkpoints.max_snapshots` (20)
caps the history of one project (oldest dropped on the snapshot path); `min_interval_hours` (24)
throttles the automatic prune (P11-18), which the trigger starts at most once per process after
its first snapshot.

Inputs → outputs: `(scope, session_id, turn_id, workdir, host)` → maybe a snapshot; returns a
warning string or None.

Files: `src/indus/backends/checkpoints/trigger.py`, `src/indus/tools/builtin/mutation_hooks.py`
(new: the tool-side hook), `src/indus/tools/builtin/write_file.py`, `src/indus/tools/builtin/patch.py`,
`src/indus/tools/builtin/terminal.py`, `src/indus/cli/app.py` + `src/indus/cli/args.py` +
`src/indus/cli/chat.py` (`--checkpoints`), `tests/unit/backends/checkpoints/test_trigger.py`.

Edge cases / failure modes:
- Container/remote backend (files are not on this host) → no snapshot; one warning per turn.
- No turn id (direct calls) → every call is its own turn (the store skips unchanged trees).
- Snapshot raises or times out → warning, tool runs.
- Unparseable shell (unterminated quote) → treated as file-changing (fail toward a snapshot).

Our design differences / decisions:
- The trigger is a process-wide object keyed by (home, session); the tool layer asks it through
  one hook module, so backends never import tools (D-04.7).
- Snapshot runs on the `tools` blocking pool with a 60 s deadline.
- The working folder is the session's working directory (terminal cwd / file-op cwd), not the
  project root found from the edited path.

Tests we will write:
- two writes in one turn → one snapshot; next turn → another; non-destructive command → none;
  disabled → none; snapshot error → tool still runs, one warning; classifier table.

## Decisions for the lead
- Destructive classifier lives in the trigger (lexical, uses `safety.approvals.shlex_lite`) plus
  `detect(...).dangerous`; the P05 detector has no "destructive" category of its own.

Compare checklist (filled after the rewrite):
- [x] once per turn, reset per turn (test: `tests/unit/backends/checkpoints/test_trigger.py::test_once_per_turn`)
- [x] disabled → nothing (test: `tests/unit/backends/checkpoints/test_trigger.py::test_disabled_takes_nothing`)
- [x] failure never blocks, warns once (test: `tests/unit/backends/checkpoints/test_trigger.py::test_snapshot_error_warns_once`)
- [x] tools wired (test: `tests/unit/backends/checkpoints/test_trigger.py::test_write_file_takes_one_snapshot_per_turn`)
- Deliberate differences: the reference walks up from the edited file to a project marker; we use
  the session's working folder (spec).
- Not taken: per-directory dedup inside one turn (one folder per session).

## Status

Done (2026-10-18). Shared: `tools/builtin/write_file.py`, `patch.py`, `terminal.py` (one hook call
each, results gain `checkpoint_warning` / `worktree_warning`).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- No change at integration.
