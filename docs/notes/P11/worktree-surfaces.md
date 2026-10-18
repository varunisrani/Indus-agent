# Task: worktree surfaces (P11-20) Phase: 11 Owner: P11 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/worktree_cmd.py`
(whole), `hermes_cli/worktree_exit.py` (skim). Closed before writing.
Evidence: none.

What it does (own words): `indus -w [NAME]` (also `indus chat -w`, `--worktree`) creates a
worktree of the repository holding the current folder and starts the new session inside it: the
session's working folder and launch record name the tree, `worktrees.json` binds it to the
session, and resuming the session moves back into it (the normal cwd restore). `--checkpoints`
turns checkpoints on for this run. `indus worktree list|new [NAME]|prune [--dry-run]` and the
slash `/worktree new [name]|list|prune [--dry-run]` (CLI only) list the repo's trees with their
keep reasons, make one, or prune. `write_file` and `patch` add `worktree_warning` to the result
when the edited path lies outside the session's worktree (e.g. the main checkout). A worktree is
never removed when its session ends; `prune` lists it like any other.

Inputs → outputs: flags / slash text → a tree on disk, a session whose cwd is inside it, printed
tables; tool results with the divergence note.

Files: `src/indus/cli/commands/worktree.py`, `src/indus/cli/app.py` (`-w`, `--checkpoints`, lazy
group `worktree`), `src/indus/cli/args.py`, `src/indus/cli/chat.py`, `src/indus/cli/session_select.py`
(launch record field), `src/indus/cli/slash/dispatch.py`, `src/indus/tools/builtin/mutation_hooks.py`,
`tests/unit/cli/commands/test_worktree.py`, `tests/contract/cli/non_tty_table.yaml` +
`tests/contract/cli/test_help_catalogue.py` (new command rows),
`tests/unit/service/commands/test_registry.py` (P11 commands now have CLI handlers; help groups).

Edge cases / failure modes:
- `-w` outside a git repository → exit 1 with a hint, no session.
- `-w` together with `-c`/`-r` → usage error 2 (a resumed session keeps its own folder).
- `-w chat` keeps `chat` as the command (a known command word is never taken as the name).
- Non-TTY `indus worktree prune` without `--dry-run` runs (it only removes trees that lose
  nothing).

Our design differences / decisions:
- No exit question (keep/archive/discard): the tree is always kept at session end.
- `/worktree new` creates and prints the tree; it does not move the running session.

Tests we will write:
- `-w` creates a tree and the session cwd is inside it; resume re-enters; divergence warning on a
  main-checkout edit; `/worktree prune --dry-run` lists without deleting; `indus worktree list`.

## Decisions for the lead
- Top-level `indus worktree` is added to the command registry after `checkpoints` (help order and
  non-TTY table rows updated).

Compare checklist (filled after the rewrite; reopened `hermes_cli/worktree_cmd.py`):
- [x] list/prune/dry-run output (test: `tests/unit/cli/commands/test_worktree.py::test_slash_prune_dry_run_lists_only`)
- [x] `-w` session inside the tree, resume re-enters (test: `tests/unit/cli/commands/test_worktree.py::test_dash_w_starts_inside_a_new_tree`)
- [x] divergence warning (test: `tests/unit/cli/commands/test_worktree.py::test_edit_outside_the_tree_warns`)
- Deliberate differences: no JSON mode, no restore of archives (no archives yet).
- Not taken: exit question, `--older-than`.

## Status

Done (2026-10-18). Shared: `cli/app.py`, `cli/args.py` (`-w`/`--worktree` is a value flag that
`rewrite_session_flags` always completes, so it joins `FALLBACK_VALUE_FLAGS`), `cli/chat.py`,
`cli/session_select.py` (`LaunchPolicy.worktree`), `cli/slash/dispatch.py`, the command registry
(`/worktree` offered on `cli` only), CLI contract goldens.

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- Recorded: `/worktree new` creates a tree but does not move the running session into it (only `indus -w` starts a session inside one).
- Recorded: `/worktree` handlers for TUI, desktop and gateway come with P12, P20 and P15 (CLI only now).
