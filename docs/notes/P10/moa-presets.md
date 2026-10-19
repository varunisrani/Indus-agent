# Task: MoA presets and CLI (P10-22) Phase: 10 Owner: P10 slice K builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/moa_config.py`
(validation, presets, normalisation), `hermes_cli/moa_cmd.py` (verbs). Closed before writing.
Evidence: none.

What it does (own words): named presets live in `moa.presets.<name>` with a list of reference
models (`provider`, `model`, `enabled`, optional `reasoning_effort`), an optional `aggregator`, an
optional `reference_timeout` and `fanout`. `moa.default_preset` names the one `/moa` uses when no
preset is given. Validation lists every problem at once (empty preset, no enabled reference,
unknown provider, a slot that names a MoA preset or the `moa` pseudo provider, bad timeout or
fanout). `indus moa list` shows presets; `configure [name]` builds one with a picker over the
providers that can be used here (a key or a configured endpoint), or from flags without a
terminal; `delete <name>` removes one but refuses the default preset. `/moa` completes preset names.

Inputs → outputs: config `moa.*` → `MoaSettings` (presets, default, timeout, privacy filter) and a
problem list; CLI verbs → config writes through `foundation.config.save` (comments kept).

Files: `src/indus/core/moa_presets.py`, `src/indus/cli/commands/moa.py`,
`tests/unit/core/test_moa_presets.py`, `tests/unit/cli/commands/test_moa.py`. Shared:
`src/indus/cli/app.py` (`moa` group), `src/indus/foundation/config/schema.py` (`moa` section) and
its test, `tests/contract/cli/{test_help_catalogue.py,non_tty_table.yaml}`,
`src/indus/cli/repl/slash_bridge.py` (preset completion after `/moa `).

Edge cases / failure modes:
- Read time is tolerant (a broken preset is skipped with its problems kept for `list`); write time
  (`configure`) refuses to save anything with a problem and prints all of them.
- Unknown preset named in `/moa name prompt`: the first word is taken as prompt text unless it is a
  known preset name (prompts may start with any word).
- `delete` of the default preset → refused with a hint to change `moa.default_preset` first;
  unknown name → exit 1.
- No terminal: `configure` needs `--reference provider:model` (repeatable); else exit 1 with the
  flags to type.

Our design differences / decisions:
- No built-in default preset with vendor model names: an empty config has no presets and `/moa`
  says how to make one (we do not ship third-party model ids as defaults).
- Delete of the default preset refused (row decision) instead of moving the default.
- No `active_preset` / "MoA as a provider" in the model picker: a session flag (`/moa on`) covers it.

Tests we will write:
- validation: empty preset, unknown provider, nested MoA slot, all problems listed together;
- `configure` via flags writes through the round-trip writer and keeps a comment;
- `delete` of the default preset refused; unknown preset exit 1;
- CliRunner `list` output; non-TTY `configure` without flags exits 1.

## Decisions for the lead
- Known providers = `providers.registry.Registry(scope).lookup`; "usable here" for the picker =
  `providers.route.resolve` succeeds for the provider.
- Intended differences: no default preset shipped; delete of the default refused; `--reference`
  has no `-r` short form (`-r` is the global resume flag).

Compare checklist (filled after the rewrite; reopened `hermes_cli/moa_config.py`, `hermes_cli/moa_cmd.py`):
- [x] Validation lists every problem at once; read time tolerant, write time strict (`tests/unit/core/test_moa_presets.py::test_every_problem_is_listed_at_once`, `test_section_validation_and_tolerant_reading`)
- [x] Empty preset, unknown provider, nested MoA slot (`test_empty_preset`, `test_unknown_provider`, `test_nested_moa_slots_are_refused`)
- [x] `configure` by flags writes through the round-trip writer, comments kept (`tests/unit/cli/commands/test_moa.py::test_configure_by_flags_keeps_comments`)
- [x] Picker over usable providers only (`test_configure_with_the_picker`, `test_usable_providers_are_the_configured_ones`)
- [x] Delete of the default preset refused (`test_delete_refuses_the_default_preset`); list output (`test_list_output`); non-TTY rows in `tests/contract/cli/non_tty_table.yaml`
- [x] `/moa` preset completion (`tests/unit/cli/slash/test_automation_cmds.py::test_moa_completes_preset_names`)
- Deliberate differences: no shipped default preset with vendor model ids; delete refuses the default instead of moving it; fan-out default `per_iteration` (the row's "before each model iteration"; the reference defaults to once per user turn); `every_n:1` normalises to `per_iteration`; no legacy flat-config form; no `enabled` per preset and no `active_preset` (the `/moa on` session flag replaces both).
- Not taken: reference / aggregator temperatures, `degraded_reference_policy`, the model-picker "MoA as a provider" entry, the billing-seat notice for the aggregator.

## Status

Done (2026-10-19). Shared: `src/indus/cli/app.py` (`moa` group), `src/indus/foundation/config/schema.py` + `tests/unit/foundation/config/test_schema.py` (`moa`, `goals`, `loops`, `auxiliary.goal_judge`, `auxiliary.side_question`), `tests/contract/cli/test_help_catalogue.py` + `non_tty_table.yaml`, `src/indus/cli/repl/slash_bridge.py` (preset completion). Note: `-r` is the global `--resume` flag, so `--reference` has no short form.
