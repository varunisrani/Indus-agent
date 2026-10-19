# Task: Plugin install, update, remove (P13-21) Phase: 13 Owner: builder N3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/plugins_cmd_install.py`
(clone → scan → publish order, refusals, metadata record), `hermes_cli/plugins_cmd_git.py`
(exact-revision checkout and HEAD check, credential scrubbing, subdirectory sparse checkout, autostash
pull).
Evidence: none.

What it does (own words): `indus plugins install <id>` puts a plugin under `<home>/plugins/<name>/`
only after it was fetched into a staging folder, its manifest read, and the P13-20 scanner judged it.

Identifiers:
- `owner/repo[/sub/dir]` → `https://github.com/owner/repo.git` (+ subdirectory), source kind `github`;
- a git URL (`https://`, `http://`, `ssh://`, `git@host:path`, `file://`) → kind `git`, printed as a
  custom, unreviewed source;
- an existing local folder (absolute, `./`, `../`, `~`) → kind `local`, copied (no `.git`).

Pins: `--ref` must be a full 40-hex commit SHA (tags, branches, short SHAs refused). The clone is
`--no-checkout`, the commit fetched at depth 1 and checked out detached, then `rev-parse HEAD` must
equal the pin or the install stops. The install record keeps `pinned: true`; `update` of a pinned
plugin is refused, and a reinstall of the same source without `--ref` stays on the recorded pin
(move the pin with `install --force --ref <sha>`). A subdirectory install clones
blobless and sparse-checks-out only that folder, then publishes only that folder.

Credentials: every network git command runs anonymously first with prompts off
(`GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, ssh `BatchMode=yes`). If the remote refuses
(authentication / not-found wording), the same command is run once more with a token from, in order,
`GITHUB_TOKEN` / `GH_TOKEN` (scope env, then secret store), `gh auth token`, `git credential fill`.
The token goes in as a one-shot `http.<origin>/.extraHeader` for that single command, through
`GIT_CONFIG_COUNT/KEY/VALUE` environment entries — never on argv (other users can read argv) and
never written to `.git/config` or any other git config. The recorded origin URL is scrubbed of any
userinfo.

Scan before activation (P13-20 `scan_tree(kind="plugin")`): `safe` → install; `caution` → findings
printed, then a confirm (or `--force`); `dangerous` → blocked, `--force` never overrides. A dangerous
update disables the plugin (it is added to `plugins.disabled`) and the old revision is restored.

Publish: staging folder `<home>/plugins/.staging-<random>/` (same file system), then one
`os.replace`-style rename into `<home>/plugins/<name>/`; a replacement moves the old folder aside
(`.old-<random>`) first and deletes it after. A crash leaves only dot-folders, which discovery never
reads and the next install sweeps. Install metadata `<home>/plugins/.install-metadata.json`
`{name: {source, kind, identifier, subdir, revision, pinned, installed_at, update_url, scan}}` under
a file lock; it never holds config values, secrets or grants.

Update (unpinned git installs): record the old HEAD, autostash local edits (`stash push
--include-untracked`), `pull --ff-only` (credential fallback as above), scan, decide, then re-apply
the stash (a conflict leaves the edits in the stash and says so). `--force` reinstall of a plugin
with local edits to tracked files copies those files to `<home>/plugins-backup/<name>-<sha7>/`
first. `check_updates` compares the recorded revision with `ls-remote` of the origin; the check runs
at most every `plugins.auto_update_check_hours` (24; 0 = off) and applies updates unattended only
with `plugins.auto_apply: true`. A manifest whose `update_url` changed from the recorded one is
reported and its update is refused until trusted explicitly (`update --trust-update-url`).

Remove: delete the folder, its metadata record, its names in `plugins.enabled` / `plugins.disabled`,
its `plugins.entries.<name>` block, and reset `memory.provider` when it named this plugin.

Files: `src/indus/plugins/install.py`, `tests/unit/plugins/test_install.py`; shared:
`src/indus/foundation/config/schema.py` (`plugins.clone_timeout_seconds`,
`plugins.auto_update_check_hours`, `plugins.auto_apply`) and its test.

Edge cases / failure modes:
- git missing → clear error; clone timeout (`plugins.clone_timeout_seconds`, 300, max 3,600) kills
  the whole git process group;
- plugin name from the manifest must be a single safe folder name (no `/`, `..`, leading dot);
- target exists without `--force` → refused (`update` or `--force`); a symlinked target → refused;
- subdirectory missing in the repo or escaping it → refused;
- metadata file unreadable → refuse to write it (never replace with an empty one).

Our design differences / decisions:
- Token via `GIT_CONFIG_*` env, never argv (the plan says "one-shot `http.extraHeader`"; this is
  that, through the environment so `ps` cannot show it). Scoped to the remote's origin URL.
- No catalog, kill list or reviewed pins (D-16.45).
- Dangerous update: restore the previous revision and disable (the plan says disable; restoring as
  well keeps the folder at code that passed a scan).

Tests we will write (local bare repos made by a fixture with real git): short SHA and tag refused;
pin installs the exact commit and records `pinned`; pin mismatch after checkout refused (patched
rev-parse); pinned `update` refused; token only in the one command's env (git runner recorder), absent
from `.git/config`; dangerous plugin blocked even with `--force`; caution needs confirm, `--force`
installs; crash between staging and rename leaves no half plugin and no record; subdirectory install
publishes only that folder; update pulls a new commit, re-applies local edits; dangerous update
disables and restores; remove clears config; local folder install.

## Decisions for the lead
- See "Our design differences". The update check is a function (`check_updates` /
  `maybe_check_updates`); nothing schedules it yet (CLI start or the service maintenance timer would).

Compare checklist (filled after the rewrite; reopened `hermes_cli/plugins_cmd_install.py`,
`hermes_cli/plugins_cmd_git.py`):
- [x] full 40-hex SHA only, lowercased (`test_install.py::test_ref_must_be_full_sha`)
- [x] HEAD verified after detached checkout (`::test_pin_mismatch_refused`)
- [x] reinstall of a pinned source without `--ref` keeps the pin (`::test_pinned_update_refused`) —
  gap found at Compare (we refused it), fixed by `_kept_pin`
- [x] origin URL scrubbed of credentials (`::test_scrub_url`)
- [x] sparse subdirectory install (`::test_subdirectory_install_publishes_only_that_folder`)
- [x] autostash pull keeps local edits (`::test_update_reapplies_local_edits`)
- [x] tracked edits copied aside on forced reinstall (`::test_force_reinstall_backs_up_edits`)
- [x] scan before anything is moved into place (`::test_dangerous_blocked_even_with_force`)
- [x] existing `GIT_CONFIG_COUNT` entries in the environment are kept; ours is appended after them
  (`::test_token_only_in_one_command_env`; found while testing on a machine that sets them)
- Deliberate differences: the reference passes the credential through a helper that may persist it in
  the user's credential store; ours is env-only and one-shot. The reference refuses manifest
  versions above its own at install; ours inherits P13-13 (warn) and the loader gate.
- Not taken: catalog, kill list, Node sidecar deps, after-install.md panel (printed as plain text
  instead when present), example-file copying.

## Status

Done (2026-10-19). Shared: config schema rows and test.
