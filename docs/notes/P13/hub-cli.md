# Task: Hub CLI and deep link (P13-27) Phase: 13 Owner: wave-2 builder N4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/skills_hub.py`
(command surface: browse, search, inspect, install, check, update, audit, uninstall, tap, slash
handler). Closed before writing.

What it does (own words): `indus.cli.commands.skills_hub` adds hub verbs to the P09-24 `skills`
group: `browse [--source]`, `search <q>`, `inspect <id>` (fetch to quarantine, scan, print, remove
the quarantine copy — nothing installed), `install <id> [--category] [--name] [--force] [-y]`,
`check-updates`, `update [name|--all] [--force]`, `audit`, `uninstall <name> [-y]`, `tap add|remove|list`,
`trust|untrust <tap>` (only within the fixed trusted set) and `trust-project [path]` (P13-28).
`deep_link_install(url, ...)` handles `indus://skill/install?identifier=…`: same pipeline, always a
confirm step, any `profile=` or scan-skipping parameter ignored. Chat: `/skills search <q>` and
`/skills install <id>` (install asks through the surface's confirm; without one it refuses).

Exit codes: 0 ok; 1 refused/blocked/failed; 2 needs a terminal (confirmation without `-y`).
A non-TTY install of a caution skill without `--force` exits 1 (blocked by policy).

Tests planned (`tests/unit/cli/commands/test_skills_hub.py`, CliRunner with MockTransport
sources): each verb; `inspect` leaves nothing installed; deep link with `profile=` ignores it;
non-TTY caution install exits 1.

## Decisions for the lead
- **`check` clash:** P09-24 already owns `indus skills check` (file validation + lint). The hub's
  update check is `indus skills check-updates`; the P13-27 row says `check` — rename if the lead
  prefers (e.g. move P09's to `validate`).
- The hub verbs are added to the existing `skills` group by `skills_hub.register` (one import and
  one call at the end of `skills.py`); `trust-project` lives there too.
- `install` follows the repo's non-TTY rule: it asks on a terminal; without one it needs `-y`
  (exit 2 otherwise). Scanning and the policy run first, so a blocked skill exits 1 either way.
- `/skills install` in chat asks through `ctx.confirm`; a surface that cannot ask
  (`interactive=False`) refuses with a pointer to the CLI. Gateway buttons are P15 (follow-up).
- `deep_link_install(scope, url, confirm_install=...)` is the desktop entry (P20): only
  `identifier`, `category`, `name` are read; `profile`, `force` and anything else are ignored.

Compare checklist (after writing; reopened the file above):
- [x] inspect never installs; uninstall confirms; update pinned to recorded source.
- [x] tap add/remove/list; trust limited to a fixed set.
