# Task: optional git worktree per subagent (P10-07) Phase: 10 Owner: P10 slice G builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/subagent_worktree.py` (all).
Closed before writing.
Evidence: none.

What it does (own words): with trusted config `delegation.worktree: true` each child works in a
fresh worktree of the parent's repository (P11 `backends.worktrees.create`, branch
`indus/subagent-<id>`), on the local backend only; other backends ignore it with a note in the
result. The child's terminal and file cwd is the worktree. The result names the worktree path and
branch. On completion the P11 retention rule decides: a tree with commits or changes is kept and
reported; a clean one is removed.

Inputs → outputs: parent cwd + config → `ChildWorktree(path, branch)` or a note; end → kept/removed.

Files: `src/indus/core/subagents/worktree.py`, `tests/unit/core/subagents/test_worktree.py`.

Edge cases / failure modes:
- Parent cwd outside a git repository → no worktree, note.
- Worktree creation fails → note, child runs in the parent's cwd.

Our design differences / decisions:
- No archive refs; P11's keep-or-remove rule applies.

Tests we will write:
- temp repo: two children, two trees and branches; clean one removed, dirty one kept; Docker
  backend → note, no tree.

Compare checklist (reopened `tools/subagent_worktree.py`):
- [x] two children, two trees and branches; clean removed, dirty kept (tests: `tests/unit/core/subagents/test_worktree.py::test_two_children_get_two_trees_clean_removed_dirty_kept`, `::test_delegated_children_report_their_worktrees`)
- [x] non-local backend → note, no tree (tests: `::test_other_backends_and_plain_folders_get_a_note`, `::test_docker_backend_delegation_notes_and_skips_the_tree`)
- Deliberate differences: no archive refs (P11 keep/remove rule); branch `indus/subagent-<id>`.

## Decisions for the lead (added)
- Base is the parent's `HEAD` (P11 `worktrees.sync_base` forced off for children) so children see
  unpushed local commits. The child's scope overlays `terminal.cwd` with the tree, which anchors
  both terminal and file tools there.

## Status

Done (2026-10-19).
