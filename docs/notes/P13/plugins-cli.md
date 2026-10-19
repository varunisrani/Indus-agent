# Task: `indus plugins` CLI (P13-23) Phase: 13 Owner: builder N3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/plugins_cmd.py` (skim:
the verb list), `hermes_cli/plugins_cmd_listing.py` (skim: list columns, pin annotation).
Evidence: none.

What it does (own words): one command group over P13-14 discovery, P13-21 install and P13-22 consent.

- `list`: every discovered plugin: name, state (enabled / disabled / not-enabled / broken), kind,
  source (bundled, user, project, entrypoint; `git pinned@<sha8>` / `git@<sha8>` / `local` for
  installs), and "update available" from the last update check.
- `install <id> [--ref SHA] [--enable/--no-enable] [--force]`: P13-21 then P13-22 in order: env
  prompts, enable question (default no), dependency consent when enabling, capability consent.
- `update [name|--all] [--force] [--trust-update-url]`, `remove <name> [-y]`.
- `enable <name>` (capability consent asked here too on a terminal) / `disable <name>`.
- `info <name>`: manifest fields, declared capabilities with risk notes, grants, provenance (install
  record), state; never prints secret values (`requires_env` shows "set" / "missing").
- `capabilities <name> [--grant X]… [--revoke X]…`.
- `check-updates`: runs the update check now and prints what is behind.
- `validate <dir>`: manifest + scan for authors; exit 1 on a `dangerous` verdict or a broken
  manifest, 0 otherwise (caution findings printed).

Non-TTY rules (P07): questions are never asked; `install` without `--enable` leaves the plugin
disabled; a caution scan without `--force` exits 1; `remove` needs `-y` (exit 1 with guidance).

`/plugins` in chat: list + enable/disable for the current profile. Not added here: the slash command
registry is shared with N1 (`/reload-mcp`, `/mcp`) and N4 (`/skills`); `plugin_lines` and
`toggle_plugin` are the functions a `/plugins` handler calls (see Decisions).

Files: `src/indus/cli/commands/plugins.py`, `tests/unit/cli/commands/test_plugins.py`; shared:
`src/indus/cli/app.py` (`plugins` group), `tests/contract/cli/non_tty_table.yaml`,
`tests/contract/cli/test_help_catalogue.py`.

Edge cases: unknown plugin name → exit 1 naming `indus plugins list`; enable of a broken plugin →
exit 1 with its error; disable of a memory provider → also clears `memory.provider` when it names it.

Tests we will write: CliRunner per verb against fixture repos; `validate` exits 1 on a dangerous
fixture; `info` never prints secret values; non-TTY install of a caution plugin without `--force`
exits 1; list shows the pin.

## Decisions for the lead
- `/plugins` slash command: add one row to `service/commands/registry.py` at merge, handler calling
  `indus.cli.commands.plugins.plugin_lines(scope)` and `toggle_plugin(scope, name, on)`.

Compare checklist (filled after the rewrite; reopened `hermes_cli/plugins_cmd.py`,
`hermes_cli/plugins_cmd_listing.py`):
- [x] list shows pin / source annotation (`test_plugins.py::test_install_list_info_remove`)
- [x] install without enabling prints how to enable (same test)
- [x] remove clears config entries (`::test_install_list_info_remove`)
- Not taken: catalog browse/search, packs, JSON/plain list modes (`--json` kept), dashboard install.

## Status

Done (2026-10-19), `/plugins` left for the merge (see Decisions).
