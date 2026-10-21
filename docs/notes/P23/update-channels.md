# Task: Update — install kinds, channels and plan (P23-17) Phase: 23 Owner: wave-36 builder U1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/update_cmd.py`
(`_source_update_channel`, `_cmd_update_check`, `_tip_shas`, `_landed_off_target` and the channel
helpers around them), `website/docs/getting-started/updating.md` (install table, channels, passive
notices, rolling back). Research 04 §3.25, research 10 §3.17. Closed before writing.
Evidence: none.

What it does (own words): `indus update` only ever moves a source checkout (a git clone the
installer made, or a developer's clone). Every other install kind is refused with the one
instruction that does work for it: Docker pulls a new image, Nix rebuilds the flake, the desktop
app updates its bundled runtime from inside the app, a wheel/package install has no updater.
A checkout follows one channel: `stable` (the newest published `vX.Y.Z` tag, checked out at its
exact commit), `canary` (the newest `vX.Y.Z-canary.<stamp>` tag, or a newer stable when one exists,
so a canary user is never left behind on an old line) or `main` (the branch tip). Official
checkouts default to stable, forks to main. `--set-channel C` stores the choice per install
(config `updates.installs.<id>.channel`, id = first 16 hex of SHA-256 of the checkout's real
path); `--channel C` is for one run; `--install-id` prints the id and path. With no explicit
choice the checkout never moves backwards: a checkout whose HEAD is not an ancestor of the target
stays where it is. A target whose store schema (`SCHEMA_VERSION` in its `state/schema.py`) is
older than the live store's is refused before anything moves. `--check` fetches and reports (exit 0
up to date / ahead, 10 available) and never installs; `--plan` prints JSON: install kind, from/to,
this profile's running gateway and how it would be restarted.

Inputs → outputs: package dir + checkout + config + git remote/tags → `InstallKind`, `Target`
(`channel`, `sha`, `version`, `tag`), `Decision` (`update` / `up_to_date` / `ahead` / `none` /
`rollback`), check exit code, plan JSON; `<root>/cache/update-check.json` written by `--check` for
the version line (P23-19).

Files: `src/indus/cli/update/__init__.py`, `src/indus/cli/update/channels.py`,
`src/indus/foundation/versions.py` (the shared tag grammar, P23-21),
`src/indus/foundation/config/schema.py` (`updates.*`), `tests/unit/cli/update/test_channels.py`,
`tests/fixtures/update/make_repo.py`.

Edge cases / failure modes:
- no release tag yet on stable/canary → "no release published yet" (exit 0, nothing to do), hint
  `--channel main`;
- malformed tags (`v1.2`, `v1.2.3-rc1`, `v01.2.3`, 4-digit CalVer majors, a canary with an
  impossible date) are ignored, never picked;
- fetch fails (offline) → exit 1 with git's last line; never a half state (fetch writes nothing in
  the tree);
- detached checkouts, checkouts on another branch: the target is still a commit; ancestry decides
  forward/ahead;
- the store schema of the target cannot be read (file missing in the target) → treated as schema 1;
- `updates.check: false` only switches off passive notices; `--check` always runs.

Our design differences / decisions:
- install kinds from `cli.version.install_method` plus a desktop marker; a bare source tree without
  `.git` (`dev`) is refused too (nothing to move it with);
- the channel record (P23-21) is not read by the updater: tags on the checkout's own remote are the
  source of truth, so forks and mirrors work without our release host;
- the official repository for the stable-vs-main default is one constant
  (`indus.foundation.versions.RELEASE_REPOSITORY`, D-16.690 pending);
- one-off/commit builds, dynamic channels and the Windows venv-holder listing are left out
  (D-16.57; Windows CLI is WSL2, D-16.55).

Tests we will write: real `git` in temp repos (an "origin" bare repo with tagged releases and a
clone): stable picks the newest tag; checkout ahead of the release is not moved; canary user offered
a newer stable; malformed tags ignored; Docker/Nix/desktop markers refused; schema-older target
refused; saved vs one-run channel; fork defaults to main; `--plan` JSON shape; `--check` exit codes.

## Decisions for the lead
- Config key is `updates.installs.<id>.channel` (one `updates` section, as the schema row says)
  rather than `update.installs` in the P23-17 row; the installer (P23-04) must write the same key.
  Optional `updates.installs.<id>.extras` (list) is read by the venv rebuild (P23-18).
- `indus update` with stdin/stdout not a terminal and no `-y` refuses with guidance (exit 1) before
  any fetch, so scripts must say `-y` (desktop) and the P07-26 non-TTY contract row is `update: 1`.

Compare checklist (filled after the rewrite; reopened `hermes_cli/update_cmd.py` channel/check
functions and `updating.md`):
- [x] explicit branch/channel beats the stored choice, stored beats the default
  (test: `tests/unit/cli/update/test_channels.py::test_saved_channel_and_one_run_channel`)
- [x] stable = newest published `vX.Y.Z`, exact commit (`::test_stable_picks_the_newest_release_tag`)
- [x] never backwards without a choice (`::test_checkout_ahead_of_the_release_is_not_moved`),
  explicit stable may roll back (`::test_explicit_channel_may_move_back_and_says_so`)
- [x] forks track main (`::test_fork_defaults_to_main`)
- [x] refuse an older store (`::test_target_with_an_older_store_schema_is_refused`)
- [x] managed installs refused with their instruction (`::test_managed_installs_are_refused`)
- [x] `--check` reports and installs nothing
  (`tests/unit/cli/commands/test_update.py::test_check_exit_codes_and_cache`)
- Deliberate differences: the reference fetches with `--depth 1` on shallow checkouts and reports
  presence only; we fetch tags and compare commits (installer clones are blob-filtered, not
  shallow, P23-04). The reference compares commit counts for main; we report the target commit
  and how many commits it is ahead.
- Not taken: shallow-graft repair, partial-clone promisor repair, CA-bundle mapping for git,
  parked-branch strategies, multi-profile fleet listing in `--plan` (this profile's gateway only,
  per the row).

## Status

Done (2026-10-21). Shared file changed: `src/indus/foundation/config/schema.py` (`updates`
section). Wired through `src/indus/cli/commands/update.py` (P23-19).

## Integration (wave 36)

Integration: both installers now write the same keys this module reads, `updates.installs.<id>.channel` and `.extras` (D-16.748); the Nix refusal text is "Nix owns updates: run `nix flake update` and rebuild." (D-16.759).
