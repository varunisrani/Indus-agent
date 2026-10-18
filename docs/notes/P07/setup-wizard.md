# Task: Setup wizard (P07-02) Phase: 07 Owner: P07 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/setup.py` (guards,
modes, section list, navigation, summary hooks), `hermes_cli/setup_terminal.py` (backend choice
shape), `hermes_cli/setup_summary.py` (summary shape), `website/docs/reference/cli-commands.md`
(§setup row). Research `04` §3.18, §3.4.4. Closed before writing.
Evidence: none.

What it does (own words): `indus setup [model|terminal|tools|agent|gateway] [--non-interactive]
[--reset] [--quick]` gets a person from a clean home to a working chat. Guards first: a managed
install refuses; the home is created; the config is copied to a `pre-setup` backup **before**
`--reset` replaces it; without a terminal (or with `--non-interactive`) it prints how to set
things up with `indus config set …` and `indus auth add …` and exits 0. With a section name only
that section runs. Otherwise an existing install (a usable provider, or setup finished before)
runs the full wizard with every prompt defaulting to the current value, or with `--quick` only
the items still missing. A first install picks Full (every section, defaults on) or Blank slate
(only the minimum on; each capability is opted into). Sections, in order: Model & provider (the
`indus model` flow), Terminal backend (local; docker and ssh listed as not available yet), Tools
(which toolsets the classic CLI enables), Agent defaults (iteration cap; approvals `manual` by
default with `smart` offered as an opt-in, D-16.53), Gateway (one line until P15). Each section
saves through the P01 writer when it ends, so Ctrl+C mid-way leaves every later section as it
was: "Setup cancelled. Sections not reached were left unchanged." At the end: paths, the backup
path with a restore hint, a capability summary, and `onboarding.setup_completed: true`.

Inputs → outputs: flags + terminal answers → `config.yaml` (model, terminal, toolsets, agent,
approvals, onboarding), the key in the secret store (keychain by default), printed summary; exit
0 done / non-TTY guidance, 1 managed / cancelled / unknown section.

Files: `src/indus/cli/setup/__init__.py`, `src/indus/cli/setup/wizard.py`,
`src/indus/cli/setup/sections.py`, `tests/unit/cli/setup/test_wizard.py`,
`tests/integration/cli/test_setup_first_run.py`; shared: `src/indus/foundation/config/schema.py`
(`onboarding.setup_completed`), `src/indus/foundation/config/backups.py` (`pre-setup` reason),
`src/indus/cli/app.py` (registration line).

Edge cases / failure modes:
- `--reset` with no config file: no backup (nothing to copy), reset still yields a fresh file;
- `--reset` + non-TTY: the reset happens (it is a file operation), then guidance;
- unknown section → list of sections, exit 2 (usage);
- picker cancelled inside setup → "Model unchanged" and the wizard goes on;
- choosing docker/ssh → explained as not available yet, backend stays local;
- a stale `.env` value is never written by setup; keys go to the secret store (keychain, P4);
- quick mode with nothing missing → says so and records completion.

Our design differences / decisions:
- No portal Quick mode and no `--portal` (I29, P3); no OpenClaw migration offer; no TTS or
  telemetry sections (later phases add sections to `SECTIONS`).
- Agent defaults are prompted in the full wizard (the reference applies them silently on first
  install); blank slate writes nothing for them (built-in defaults already are the minimum).
- `--reset` removes the user's file and lets the writer seed the commented example (R04.4: never
  the full default tree).
- The tools section edits `platform_toolsets.cli` (a list of toolset names) itself through the
  writer; when P07-17 lands, its enable/disable functions replace `ConfigToolsModel` (one seam).

Tests we will write:
- scripted answers from a clean temp home → config with provider, model, backend; key in a fake
  keychain; summary printed; `onboarding.setup_completed` set;
- backup exists before `--reset`, and the reset file carries no default values;
- non-TTY and `--non-interactive` print guidance and exit 0;
- Ctrl+C in the tools section keeps terminal changes and leaves tools/agent unchanged;
- one section only; unknown section; managed install refused; quick on an existing install;
- blank slate leaves extras off; back navigation from terminal to the model section;
- integration (timed): clean home → setup → first fake-model answer in one scripted run.

## Decisions for the lead
- Cancel exits 1 (the reference returns 0). Changing it is one constant in `wizard.py`.
- Existing install = a usable provider (P07-03 signals) or `onboarding.setup_completed`.
- The timed manual run with a real key (Done-when bullet) is still to be recorded by a person.

Compare checklist (filled after the rewrite; reopened `hermes_cli/setup.py`,
`hermes_cli/setup_terminal.py`, `hermes_cli/setup_summary.py`):
- [x] managed install refused before anything is written (test:
  `tests/unit/cli/setup/test_wizard.py::test_managed_install_refused`)
- [x] backup taken before `--reset` (test:
  `tests/unit/cli/setup/test_wizard.py::test_backup_before_reset`)
- [x] non-TTY / `--non-interactive` guidance names `config set` lines and `auth add`, exit 0 (test:
  `tests/unit/cli/setup/test_wizard.py::test_non_tty_prints_guidance`)
- [x] section order model → terminal → tools → agent → gateway; one section by name (tests:
  `tests/unit/cli/setup/test_wizard.py::test_full_first_install`,
  `tests/unit/cli/setup/test_wizard.py::test_single_section`)
- [x] existing install: full reconfigure with current values as defaults; `--quick` only missing
  (tests: `tests/unit/cli/setup/test_wizard.py::test_reconfigure_keeps_current_values`,
  `tests/unit/cli/setup/test_wizard.py::test_quick_runs_only_missing`)
- [x] back navigation across sections (test:
  `tests/unit/cli/setup/test_wizard.py::test_back_from_terminal_returns_to_model`)
- [x] Ctrl+C message and later sections untouched (test:
  `tests/unit/cli/setup/test_wizard.py::test_cancel_mid_way_leaves_later_sections`)
- [x] backup path printed with a `cp` restore hint after a full run (test:
  `tests/unit/cli/setup/test_wizard.py::test_full_first_install`)
- [x] unknown section lists the valid ones (test:
  `tests/unit/cli/setup/test_wizard.py::test_unknown_section`)
- Deliberate differences: see above (no portal, no migration, no TTS/telemetry; agent defaults
  prompted; reset seeds the commented example).
- Not taken: gateway service install (P15), plugin terminal backends (P13), backend SDK installs.

## Status

Done (2026-10-18) except the manual timed run with a real key. Shared files changed:
`foundation/config/schema.py`, `foundation/config/backups.py`, `cli/app.py`, `cli/lazy.py` (the
help listing uses the registry's one-liner even for an imported command; with longer command
names a loaded `chat` help wrapped and broke the P06 help-order test),
`tests/unit/foundation/config/test_schema.py` (new defaults).

## Integration (lead)

- `ConfigToolsModel` replaced by `PlatformToolsModel`: the rows are `indus tools list`'s and a
  change goes through `tools.enable_names` / `disable_names`. `ToolsModel.enabled` now returns the
  rows on now (never None); a choice equal to them writes nothing. Full mode still starts with every
  row ticked; the saved list keeps P07-17's order (`["file", "terminal", "web"]`).
- The OpenRouter row asks "Paste an API key" or "Sign in" first (P07-10's key mint); test scripts
  answer it.
- Owner item: the timed three-minute run with a real key is still open.
