# Task: `indus profile` (P07-16) Phase: 07 Owner: P07 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/profiles.py` (constants,
alias wrapper functions, clone/create/staging, delete, active profile, rename head, profile meta),
`hermes_cli/profile_channels.py` (module notes, `.env` and config strip functions),
`hermes_cli/profile_cmd.py` (list/delete/describe/show/alias/rename handlers),
`website/docs/reference/profile-commands.md` (headings and examples). Research
`04-cli-config.md` §3.10; traps R04.25, R04.26. Closed before writing.
Evidence: none.

What it does (own words): manages the profiles under one root (the root itself is `default`, named
ones live in `<root>/profiles/<id>/`). Ids, reserved names, liveness markers, tombstones and the
sticky active file all come from P01 `foundation.profiles`.
- `list` (also bare `indus profile`): every live profile with its model, alias and description; the
  sticky one and the one this process runs as are marked.
- `use NAME`: makes NAME sticky (`use default` removes the file).
- `show NAME`: path, model/provider, whether `.env`/`PERSONA.md` exist, skill count, alias,
  description.
- `create NAME [--clone | --clone-all] [--clone-from SRC] [--clone-channels] [--no-alias]
  [--description TEXT]`: builds the new home in a hidden staging dir beside it and publishes it with
  one rename. `--clone` copies `config.yaml`, `.env`, `PERSONA.md`, the memory files and the skills;
  `--clone-all` copies the whole home except history and runtime state (store and sidecars,
  sessions, backups, snapshots, checkpoints, logs, caches, cron jobs, nested profiles, lock/pid
  files). Files are copied by content, so a symlinked `config.yaml`/`.env` in the source becomes a
  private file and the clone can never write through to the source. Unless `--clone-channels`,
  messaging channel settings are stripped: channel env names (`TELEGRAM_*`, `DISCORD_*`, …,
  `GATEWAY_ALLOW*`) from `.env` through P01's env writer, platform sections from `config.yaml`
  through P01's config writer, the matching secret store entries, and channel state dirs on
  `--clone-all` (R04.26). The source's secret store entries are copied into the new profile's
  namespace after publishing. Unless `--no-alias`, an alias script is written.
- `delete NAME [-y]`: writes the tombstone beside the profile dirs first, removes the profile's
  secret store entries and its alias, then removes the dir only when it carries an identity marker
  (a marker-less dir is never removed, R04.25); the sticky file falls back to `default`. Without
  `-y` a terminal asks for the name typed back; without a terminal it refuses (exit 1).
- `rename OLD NEW`: moves the dir, moves the secret store entries to the new namespace, rewrites an
  alias of ours and retargets the sticky file.
- `describe [NAME] [--text T]`: reads or writes the description in `profile.yaml`.
- `alias NAME [--remove] [--name ALIAS]`: `<alias dir>/<alias>` (default `~/.local/bin`) holding
  `exec '<abs indus>' -p <profile> "$@"` (chmod +x; a `.bat` on Windows); the alias name passes the
  id rule and may not shadow an `indus` command or a file that is not ours; warns when the dir is not
  on `PATH`.

Inputs → outputs: each subcommand prints a short report; errors on stderr with exit 1.

Files: `src/indus/cli/commands/profile.py`, `tests/unit/cli/commands/test_profile.py`.

Edge cases / failure modes:
- `create` over a live profile → refused; over a marker-less dir → refused and the dir is named
  (never removed); over a tombstoned name whose dir is gone → tombstone cleared and created.
- A failed copy leaves no half profile (staging removed) and no stray tombstone change.
- `delete default`, `rename default …`, `create default` → refused.
- `delete` of the profile this process runs as is allowed (the next run falls back).
- The alias dir comes from the scope's `HOME` (tests point it into a temp dir).

Our design differences / decisions:
- `PERSONA.md` instead of the reference's persona file name; secret store entries (keychain) are
  copied by name into the new namespace instead of copying a credential file.
- Rename of `default` is refused (the reference sets a display name); no display names yet.
- No gateway/service steps (P15), no distributions, export/import, identity purge commands.
- Channel names: a fixed prefix list until P15's platform registry can provide it
  (`CHANNEL_ENV_PREFIXES`).

Tests we will write:
- clone without `TELEGRAM_BOT_TOKEN` (env, config section and secret store) and with
  `--clone-channels`; clone through a symlinked `.env` leaves the source untouched; clone-all skips
  the store and history; alias script content golden; delete → tombstone, `-p` then refuses;
  marker-less dir untouched; list/use/show/describe/rename round trips; non-TTY delete without `-y`
  exits 1; alias name collisions refused.

## Decisions for the lead
- Channel prefixes are a list in `profile.py` until P15 provides the platform registry.
- Alias dir: `<HOME>/.local/bin` (POSIX) or `%LOCALAPPDATA%\indus\bin` on Windows, read from the
  scope's env so tests never touch the real home.

Compare checklist (filled after the rewrite; reopened the files above):
- [x] staging dir + one rename (test: `test_profile.py::test_failed_clone_leaves_nothing`)
- [x] clone strips channel env keys and config sections unless `--clone-channels` (tests:
  `::test_clone_strips_channels`, `::test_clone_channels_keeps_them`)
- [x] symlinked `.env` materialised, source untouched (test: `::test_clone_through_symlinked_env`)
- [x] clone-all excludes history (test: `::test_clone_all_skips_history`)
- [x] wrapper content and chmod (test: `::test_alias_script_golden`)
- [x] alias name validated, collisions refused (test: `::test_alias_collisions_refused`)
- [x] delete: tombstone first, marker-less dir never removed (tests:
  `::test_delete_tombstones_and_profile_flag_refuses`, `::test_marker_less_dir_untouched`)
- [x] sticky active retargeted on delete/rename (test: `::test_rename_moves_everything`)
- Deliberate differences: keychain entries copied by name; rename of default refused; no
  gateway/service handling yet; `create` over any existing non-profile dir is refused (the
  reference replaces an empty shell left behind by a deleted profile; we never remove a dir we did
  not prove is a profile); the default profile's `create`/`delete`/`rename` are refused.
- Not taken: `--no-skills`, `--sync-imports`, distributions, export/import, identity commands,
  `--auto` descriptions (needs an auxiliary model, P08).

## Status

Done (2026-10-18).

## Integration (lead)

- Clones never copy credential-pool secrets (`AUTH_<PROVIDER>_<ID>`): `auth.json` is not cloned,
  so its sign-ins and pool keys stay with their profile (D-16.99). The `profile delete`
  confirmation uses `cli.prompts.ask_text`.
