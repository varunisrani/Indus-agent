# Task: session worktrees, core (P11-19) Phase: 11 Owner: P11 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/worktree_ops.py` (base
resolution, add and cleanup, dirty/unpushed checks, prune phases), `hermes_cli/worktree_retention.py`
(skim: removal blockers), `hermes_cli/worktree_gc.py` (protected branches, prune),
`hermes_cli/worktree_registry.py` (whole). Closed before writing.
Evidence: none.

What it does (own words): `create(repo, name?)` makes `<repo>/.worktrees/<name>` on a new branch
`indus/<name>` (name default `indus-<YYYYmmdd-HHMMSS>-<hex4>`; given names are cleaned to
`[A-Za-z0-9._-]`, at most 40 chars). The base is the freshest ref we can get cheaply: the current
branch's upstream (fetched, 5 s), else the remote default branch `origin/HEAD` (fetched), else
local `HEAD`; a failed fetch falls back to the cached remote ref, a missing one to `HEAD`.
`.worktrees/` is added to the repo's `.gitignore` once; entries of `.worktreeinclude` (e.g. `.env`)
are copied in (only paths that stay inside both trees). A failed `worktree add` removes the half
made tree, its admin entry and the branch, then retries once from `HEAD` when the base was remote.
`<home>/worktrees.json` records `{path, repo, branch, base, session_id, created_at}`.

Removal is allowed only when nothing unrecoverable is lost; otherwise the tree is kept with a
reason: changes to tracked files, untracked files that are not ignored, commits not reachable from
any other branch/tag/remote ref, an embedded repository anywhere below the tree (ignored folders
too, bounded walk), per-worktree refs (`refs/worktree/`, `refs/bisect/`, `refs/rewritten/`), or any
git failure while checking (fail safe toward keeping). Branches `main`, `master`, `develop`, `dev`,
`trunk` are never deleted. `prune(dry_run)` removes clean trees older than 24 h whose commits are
all on other refs ("merged") and reports every other tree with its reason. Every git call is
bounded (10 s default) with a non-interactive env (`GIT_TERMINAL_PROMPT=0`, stdin closed).

Inputs → outputs: repo root, name, scope (home, clock, env) → `WorktreeInfo`; prune →
`PruneReport{removed, kept: [(path, reason)]}`; registry file.

Files: `src/indus/backends/worktrees.py`, `src/indus/foundation/config/schema.py`
(`worktrees.*`), `tests/unit/backends/test_worktrees.py`.

Edge cases / failure modes:
- Not inside a git repo → typed error "not a git repository".
- Name already exists → error, nothing created.
- `.gitignore` without a trailing newline / with a BOM → line appended correctly, never twice.
- `.worktreeinclude` entry escaping the repo (`../x`, absolute, symlink out) → skipped.
- A tree registered but deleted by hand → dropped from the registry by `prune` (and `git worktree
  prune` cleans git's admin entry).

Our design differences / decisions:
- No squash/rebase-merge detection through GitHub or patch ids; "merged" = every commit is on
  another ref. Unpushed-but-merged-locally work therefore counts as safe.
- No worktree lock/live-process checks, no archive refs, no repack maintenance (later phases).
- No `git remote show` network fallback for the default branch.
- Directories listed in `.worktreeinclude` are copied, not symlinked.

Tests we will write:
- create from a fetched upstream; `.gitignore` updated once; `.worktreeinclude` copied; escaping
  entry skipped; dirty tree kept with reason; untracked kept; tree with an unpushed commit kept;
  embedded repo kept; clean merged tree older than 24 h pruned (fake clock), younger kept;
  `main` never deleted; failed add leaves no branch and no folder; dry run deletes nothing.

## Decisions for the lead
- Config rows `worktrees.prune_after_hours` (24), `worktrees.fetch_timeout_s` (5),
  `worktrees.sync_base` (true).

Compare checklist (filled after the rewrite; reopened the four files above):
- [x] base resolution order and fetch fallback (test: `tests/unit/backends/test_worktrees.py::test_create_from_fetched_upstream`)
- [x] `.gitignore` once, `.worktreeinclude` with traversal guard (test: `tests/unit/backends/test_worktrees.py::test_gitignore_and_include`)
- [x] failed add cleanup (test: `tests/unit/backends/test_worktrees.py::test_failed_add_leaves_nothing`)
- [x] blockers: dirty, untracked, unpushed, embedded repo, per-worktree refs (test: `tests/unit/backends/test_worktrees.py::test_blockers_keep_trees`)
- [x] protected branches (test: `tests/unit/backends/test_worktrees.py::test_protected_branch_never_deleted`)
- [x] prune age + dry run (test: `tests/unit/backends/test_worktrees.py::test_prune_removes_old_clean_merged`)
- Deliberate differences: see above.
- Not taken: archives/sweep tiers, kanban trees, merge cache, GitHub PR checks, `git worktree
  lock` on creation, the skip-worktree/assume-unchanged hidden-edit check, the 3x age tier for
  named trees.

## Status

Done (2026-10-18).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- Config rows accepted. The registry `<home>/worktrees.json` lives in the profile home; tests use temp homes and temp repos (nothing under `/root` or in the repo after a full run).
