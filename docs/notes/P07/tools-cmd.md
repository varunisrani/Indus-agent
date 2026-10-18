# Task: `indus tools` (P07-17) Phase: 07 Owner: P07 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/subcommands/tools.py`
(whole), `hermes_cli/tools_config.py` (function list; `_get_platform_tools`,
`_save_platform_tools`, the disabled-toolset pruning). Research `04-cli-config.md` §3.19.
Closed before writing.
Evidence: none.

What it does (own words): per-platform toolset choice, stored in `platform_toolsets.<platform>` as
a list of toolset names. With no list saved the platform uses its bundle (`indus-<platform>`).
- `list [--platform cli]`: one row per configurable toolset (the named sets plus toolsets that
  tools registered themselves into): enabled or not, and whether its tools are ready here with the
  reason when not (the registry probe's answer; for web, which provider is missing).
- `enable NAME… [--platform]`: adds the names to the saved list (starting from the bundle when
  nothing was saved yet). `disable NAME…`: the saved list is first spelled out as the configurable
  toolsets the bundle covers, then NAME and every listed set that only repeats NAME's tools are
  removed; disabling a bundle (`indus-*`) or a composite keeps the shared core and removes only what
  it adds (the toolset resolver's rule). Unknown names → did-you-mean, exit 1, nothing written.
- `--summary`: the enabled toolsets for every platform that has a saved list, plus `cli`.
- bare `indus tools`: with a terminal, a numbered checklist (type numbers to toggle, Enter to
  save), then, when web search is on and no web provider is ready, a provider choice and a masked
  key prompt (key saved in the secret store, provider in `web.backend`); without a terminal, the
  same as `list`.
Every write goes through the P01 config writer.

Inputs → outputs: rows on stdout; saves `platform_toolsets.<platform>`; exit 1 on unknown names or
platforms.

Files: `src/indus/cli/commands/tools.py`, `tests/unit/cli/commands/test_tools.py`.

Edge cases / failure modes:
- Enabling something already on, disabling something already off → "nothing changed", exit 0.
- An empty saved list means no toolsets (kept as the user's choice).
- Unknown `--platform` → exit 1 with the known ones.
- A probe that raises counts as unavailable (registry rule).

Our design differences / decisions:
- Our toolset names (`indus-cli`, named sets from `tools.toolsets`).
- The checklist is a small numbered toggle written here; P07-01's prompt kit (slice A) can replace
  it at integration.
- `post-setup` installers are not taken (no optional backends to install yet).

Tests we will write:
- enable/disable persist through the writer and survive a reload; disabling `web` from the default
  bundle removes web_search/web_extract tools from the selection; unknown toolset → did-you-mean
  exit 1; availability reason shown for a failing probe; `--summary`; non-TTY bare → list; a
  scripted checklist toggles and saves.

## Decisions for the lead
- The classic CLI still starts with `runtime.DEFAULT_TOOLSETS` when no `-t` is given; nothing reads
  `platform_toolsets.cli` yet. Follow-up for the lead: `runtime.toolset_selection` should use
  `commands.tools.saved_selection(scope, "cli")` when the policy has no toolsets (a P06 file, so not
  changed here).
- P01 writer, cosmetic: appending an item to a list that is the last thing before the commented
  example block (`platform_toolsets.cli` in a file the writer created) lands the new `- item` line
  after the example comments. YAML still reads it as part of the list (`config get` and the tests
  confirm), but the file looks odd; the round-trip apply in `foundation/config/writer.py` should
  insert before the trailing comment block. Not changed here (P01 file).
- `platform_toolsets` is not in the P01 schema yet (slice A owns the schema rows); top-level extra
  keys are kept, so the writer stores it fine.

Compare checklist (filled after the rewrite; reopened `hermes_cli/tools_config.py`):
- [x] saved list vs platform default (test: `test_tools.py::test_enable_disable_persist`)
- [x] disabling removes the tools for real, not just the name (test:
  `::test_disable_web_from_default_bundle`)
- [x] unknown name refused (test: `::test_unknown_toolset_did_you_mean`)
- [x] reasons for unavailable toolsets (test: `::test_failing_probe_reason_shown`)
- Deliberate differences: no MCP server passthrough handling (P13), no "recently shipped" toolset
  bookkeeping, no `agent.disabled_toolsets` reconciliation (not in our schema).
- Not taken: `post-setup`, per-tool backend setup flows other than the web provider.

## Status

Done (2026-10-18).

## Integration (lead)

- The checklist and the web-provider questions use `cli.prompts` (checklist, choice with "Skip",
  masked key); the module-level `ask` hook is gone (tests script `prompts.io_factory`).
- Chat now reads the saved list: `runtime.toolset_selection(policy, scope)` uses
  `saved_selection(scope, "cli")` when no `-t` was given (not with `--safe-mode` /
  `--ignore-user-config`), so `indus tools disable` affects the next chat.
- `platform_toolsets` is now a schema row (`dict[str, list[str]]`, default empty).
- The P01 writer now moves a list's trailing comment block to the new last item when it appends, so
  `- item` lines no longer land after the commented example.
