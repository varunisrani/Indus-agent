# Task: install.sh repository stage (P23-04) Phase: 23 Owner: wave-36 builder I1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `scripts/install.sh` (repository stage
functions: saved-channel reader, release lookup, stable target, fresh clone, existing-checkout
update). Trap R10.14. Closed before writing.
Evidence: none.

What it does (own words): the riskiest stage — it must never leave a half-cloned or overwritten
tree where the user's install is.

Fresh install: the target must not exist, or be an empty directory (taken over); anything else is
refused (`dir_not_checkout`). The clone goes into a hidden sibling staging directory
(`--filter=blob:none`), up to 3 attempts with 5 s and 10 s pauses (sleeper injectable), then one
`--no-checkout` attempt; a `--commit` must be an ancestor of the branch; the checkout of the pin or
release tag gets 2 attempts; only a complete tree is **renamed into place**. Any failure removes the
staging directory, so no target directory appears.

Existing directory: a `.git` with no commits (an interrupted clone) is moved aside to
`<dir>.broken-<UTC stamp>` (never deleted) and a fresh clone follows. Otherwise the checkout is
updated: its channel is the explicit `--channel`, else the saved one (`update.installs.<id>.channel`
in `<home>/config.yaml`, id = first 16 hex of SHA-256 of the real path), else the default (stable
for our repository's main line, main for forks or other branches).

Channels: stable resolves the newest `vMAJOR.MINOR.PATCH` (regex `^v[0-9]{1,3}\.[0-9]+\.[0-9]+$`)
through the GitHub API JSON, then the `releases/latest` redirect header, then `git ls-remote --tags`
sorted by version; canary takes the newest of canary (`vX.Y.Z-canary.<YYYYMMDDTHHMMSSZ>`) and stable
tags by SemVer order (ls-remote). Unreachable: an existing checkout is kept (unless the channel was
named explicitly) — a fresh one fails with a hint to use `--channel main`. A release only moves a
checkout forward (never backwards). Main fetches the branch and fast-forwards; local edits are
auto-stashed first (label `indus-install-autostash-<stamp>`, unmerged index entries cleared before
the stash); a non-fast-forward reset happens only **after** commits missing from the remote are saved
under `refs/indus/install-backups/<stamp>`.

Inputs → outputs: flags + `INDUS_INSTALL_REPO_URL` (forks, tests) → a checkout at `<dir>`.

Files: `scripts/install/sh/10-repository.sh`, `tests/integration/install/test_install_sh_repository.py`.

Edge cases / failure modes:
- clone fails on every attempt → `git_clone_failed`, no target, no staging dir left.
- pin not on the branch → `commit_not_on_branch`, nothing published.
- symlink at the target → refused (never followed).
- stash fails → `local_changes_blocked`; backup ref fails → `git_reset_failed`, no reset.
- saved channel value outside `[a-z0-9-]` → ignored.

Our design differences / decisions:
- An explicit `--channel` or a saved one decides the move; we do not leave a channel-owned checkout
  untouched (the reference hands those to its updater). Forward-only for releases either way.
- Backup refs under `refs/indus/install-backups/<stamp>` (the row's namespace).
- Canary lookup is ls-remote only (the API's "latest" excludes prereleases).

Tests we will write (real git over a local bare origin, fake git/curl wrappers for failures):
- clone failure mid-way leaves no target dir; retries with injected sleeps 5, 10; deferred attempt.
- broken checkout moved aside; non-checkout dir refused; empty dir taken over.
- tag lookup falls back API → redirect → ls-remote in order; fresh install fails with the hint.
- diverged commit reachable from the backup ref after reset; stash label present; saved channel
  honoured; pin outside the branch refused.

## Decisions for the lead
- Config key read: `update.installs.<id16>.channel` (P23-17's spelling); U1 owns the schema row.

Compare checklist (filled after the rewrite; reopened the repository functions):
- [x] staging clone + rename, 3 attempts (5 s, 10 s), deferred attempt, nothing published on failure (tests: `tests/integration/install/test_install_sh_repository.py::test_clone_failure_midway_leaves_no_target`, `::test_clone_retries_then_publishes`, `::test_deferred_checkout_after_three_failed_clones`)
- [x] pin must be on the branch (tests: `::test_pin_must_be_on_the_branch`, `::test_pin_on_the_branch_is_checked_out`)
- [x] broken checkout moved aside; non-checkout dir and symlink refused; empty dir taken over (tests: `::test_broken_checkout_is_moved_aside`, `::test_non_checkout_dir_is_refused`, `::test_symlink_target_is_refused`, `::test_empty_dir_is_taken_over`)
- [x] API → redirect → ls-remote, regex, numeric sort (tests: `::test_tag_lookup_falls_back_in_order`, `::test_tag_lookup_uses_the_redirect_when_the_api_fails`, `::test_tag_lookup_ends_with_the_remote_tags`)
- [x] unreachable release: fresh fails with the `--channel main` hint, existing kept unless named (tests: `::test_no_release_fails_with_the_main_hint`, `::test_unreachable_release_keeps_an_existing_checkout`)
- [x] autostash label, unmerged index cleared first, backup ref before reset (tests: `::test_diverged_commit_is_saved_before_the_reset`, `::test_unmerged_index_is_cleared_before_the_stash`)
- [x] saved channel, explicit choice wins, forward only (tests: `::test_saved_channel_is_honoured`, `::test_explicit_channel_beats_the_saved_one`, `::test_a_release_never_moves_a_checkout_backwards`, `::test_default_channel_moves_a_release_checkout_forward`)
- [x] canary ordering (tests: `::test_canary_takes_the_newest_by_semver`, `::test_canary_offers_a_newer_stable`)
- Deliberate differences: a saved or named channel moves the checkout (the reference leaves a
  channel-owned checkout to its updater); API/redirect lookups only for our own repository URL;
  one backup ref per run named by stamp.
- Not taken: the reference's repairs for partial-clone pack markers, treeless refetch and
  commit-graph settings (they heal checkouts its older installers made; ours start blobless).

## Status

Done (2026-10-21).

## Integration (wave 36)

The saved channel is read from `updates.installs.<id16>.channel` (the schema key `indus update` uses; D-16.748). The settings reader also returns lists (block `- item` lines or `[a, b]`) as space-separated items.
