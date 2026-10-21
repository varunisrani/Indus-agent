# Task: Update — apply, verify, rollback (P23-18) Phase: 23 Owner: wave-36 builder U1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/update_cmd.py`
(`_validate_critical_files_syntax`, `_validate_python_files_syntax`,
`_rollback_if_pulled_syntax_error`, `_roll_back_broken_update`, `_move_checkout_to`,
`_reconcile_diverged_checkout`, `_pull_updates`, `_complete_source_update`),
`website/docs/getting-started/updating.md` (phases 1–4). Research 04 §3.25, trap R04.24. Closed
before writing. Evidence: none.

What it does (own words): an update is a transaction with a fixed order — plan, snapshot, apply,
verify, hand-off. (1) A quick state snapshot of this profile through the P07 backup code
(`updates.pre_update_backup`: quick/full/off; `--backup` forces full, `--no-backup` skips); a failed
snapshot only warns and lands in the receipt. (2) Local edits are stashed under the label
`indus-update-autostash-<stamp>`; the target commit is fetched and the checkout moved to exactly
that commit (a backup ref keeps any commit that would leave the history). (3) The current
interpreter *compiles* (never imports) the critical startup files of the new tree into a temp dir;
any failure hard-resets to the pre-update commit and exits 1 with the file and the error. (4) A new
environment is built beside the old one (`.venv-next`, relocatable, `uv sync --frozen` from the new
lock) and proven by running `.venv-next/bin/python -m indus --version` in a child; failure keeps the
old venv, resets the code and exits 1. (5) The venvs are swapped by rename (`.venv` → `.venv-prev`,
`.venv-next` → `.venv`; the previous `.venv-prev` is removed first). (6) The old process never runs
the new modules: it writes a request file and execs `<new venv python> -m indus update --finish
<request>`. Ctrl-C anywhere before the exec puts the old commit, the old venv and the local edits
back and exits 130.

Inputs → outputs: `ApplyRequest` (checkout, channel, target, flags) + git + uv → moved checkout and
swapped venv + request JSON + exec, or a rolled-back checkout and exit 1/130; each phase recorded in
the update receipt (P23-19 writes it).

Files: `src/indus/cli/update/apply.py`, `tests/integration/update/test_update_apply.py`,
`tests/fixtures/update/make_repo.py` (good, syntax-broken, lock-broken releases).

Edge cases / failure modes:
- the tree is dirty → stash with untracked files; the stash is re-applied after the swap (code and
  venv agree first); a stash that cannot be re-applied is left in the stash list, the tree is
  cleaned back to the target, and the run ends with exit 1 naming the label after `--finish`;
- the move itself fails (lock file, conflict) → reset to the pre-update commit, stash back, exit 1;
- a critical file missing in the new tree counts as broken;
- `uv` missing → the build fails like a broken lock (old venv kept, code reset);
- a rename failure during the swap → the first rename is undone (Windows holds a venv in use; the
  CLI there is WSL2, D-16.55);
- Ctrl-C during fetch, move, build or swap → restore in reverse order, exit 130;
- one update at a time: `<root>/.update.lock`; a second run exits 1.

Our design differences / decisions:
- exec'ing the new code replaces the reference's frozen import surface (D-16.3 §2.3, R04.24);
- the venv is built next to the old one and swapped by rename instead of syncing in place, so a
  failed resolve can never leave a half-synced environment; `.venv-next` is created
  `--relocatable` so its scripts survive the rename;
- installed extras come from `updates.installs.<id>.extras`; the dev group is kept only when the
  current venv has it (pytest present);
- no ZIP fallback, no submodules, no parked-branch merge strategies, no gateway pause before the
  move (the gateway keeps running on the old venv until the finish step restarts it, D-16.57).

Tests we will write: temp origin repo with releases v0.1.0 (good), v0.2.0 (good), v0.3.0 (syntax
error in a critical file), v0.4.0 (lock that cannot resolve, faked `uv`): syntax error rolls back;
unresolvable lock keeps the old venv; success swaps venvs and execs `--finish` (spy); stash conflict
exits 1 with the label; interrupt during the build leaves the old commit and venv.

## Decisions for the lead
- Stash re-apply happens after the venv swap (not right after the move), so the finish step always
  runs on a consistent tree; a conflicting stash is reported at the end with exit 1.

Compare checklist (filled after the rewrite; reopened `hermes_cli/update_cmd.py` apply/rollback
functions):
- [x] compile critical files into a temp dir, never into `__pycache__`
  (test: `tests/integration/update/test_update_apply.py::test_syntax_error_in_the_new_release_rolls_back`)
- [x] rollback is a hard reset to the pre-update commit; added files of the broken release are gone
  (`::test_syntax_error_in_the_new_release_rolls_back`)
- [x] diverged commits saved to a backup ref before the move
  (`::test_local_commits_are_kept_on_a_backup_ref`)
- [x] stash kept and named when it cannot be re-applied (`::test_stash_conflict_exits_1_with_its_label`)
- [x] Ctrl-C → 130 with the old tree (`::test_interrupt_during_the_build_restores_everything`)
- [x] a rollback on the main channel puts the branch itself back
  (`::test_main_channel_rollback_puts_the_branch_back`) — gap found at compare: the first draft
  reset `main` before leaving it; now the checkout moves first and the branch ref after
- [x] an exec that cannot start leaves the update in place and owes the finish with its exact
  command (`::test_a_finish_that_cannot_start_is_owed_with_its_command`)
- Deliberate differences: the reference's interrupted-pull marker and next-launch recovery are not
  built; the transaction is undone in-process (only a hard kill can leave a moved tree, and the
  next `indus update` simply moves it again).
- Not taken: upstream-fork sync, custom-branch merges, plugin disabling during dependency
  preparation (plugins install lazily in our design, P13).

## Status

Done (2026-10-21). Nothing changed outside the task's files.

## Integration (wave 36)

Integration: the run claims `<root>/.indus-update-in-progress` (the desktop's P20-37 contract) under the update lock before the checkout moves and releases it when apply returns; after the hand-off the same pid releases it in `--finish` (`src/indus/cli/update/marker.py`, D-16.761; tests in `tests/unit/cli/update/test_marker.py` and `tests/integration/update/test_update_apply.py`).
