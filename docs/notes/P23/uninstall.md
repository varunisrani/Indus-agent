# Task: `indus uninstall` finished (P23-20) Phase: 23 Owner: wave-36 builder I2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/uninstall.py` (all: prompts,
ownership refusal, rc cleanup, wrapper removal, gateway service removal per OS, Windows registry
helpers, desktop leftovers, profiles, `run_data_uninstall`, `run_gui_uninstall`, `run_uninstall`,
dry run, `_perform_uninstall`, module entry). Research 04 §3.26, 08 §3.29; P24-34 row (consumer of
the levels table). Closed before writing.
Evidence: none.

What it does (own words): removes what the installer added, at one of four levels, and never
anything it did not add.

- Ownership: code removal is allowed only for a git checkout the installer made. When another
  manager owns the install (Nix store, a container image, the desktop app's bundled runtime, or a
  copy with no `.git`) every level that removes code is refused with that manager's instruction;
  `--data` still works.
- Levels: **keep data** (default; remove code, launcher, PATH block, gateway service, installer
  tools; keep the home), **full** (keep data + the home, this home's keychain entries and the
  desktop app's user data; from the default profile, named profiles only when the person says so),
  **gui** (desktop app only: built artefacts, Start-menu shortcut, desktop user data; the agent
  stays), **data** (this home's data only; code and other profiles stay; allowed on managed
  installs). `--gui-summary` prints what the desktop app has installed as JSON. `--dry-run` prints
  the plan of the chosen level and changes nothing.
- Interactive (terminal on stdin and stdout, no `-y`): shows code dir, config, secrets (keychain
  entry names), data dirs, desktop user-data dir, other profiles (with a "gateway running"
  marker); options 1 keep data (default), 2 full, 3 cancel; full from the default profile asks
  whether to remove the named profiles too (default no); the final confirmation needs `yes`
  typed; Ctrl-C or end of input prints "Cancelled." and changes nothing.
- `-y`: no prompts; keep data unless `--full`; named profiles are never removed unattended.
  Without a terminal and without `-y` the command prints how to run it and exits 1.
- Steps (in order): gateway service of each home being removed (systemd user unit / launchd
  agent; a running gateway gets a planned-stop record first; a manually started one is asked to
  stop by SIGTERM to its recorded pid, never by pattern; Windows has no native service in 1.0,
  D-16.55); the marked PATH block (`# >>> indus >>>` … `# <<< indus <<<`, exact lines) from every
  rc file that holds one; the Windows user PATH entry `<home>\bin`; the launcher (only when it
  points into the code dir); Start-menu shortcut; the code dir; `<home>/tools`; on full the
  keychain entries of each removed home (before its files, the index lives there), the desktop
  user data, then the home.

Inputs → outputs: flags, answers → removed files/entries, printed plan, JSON summary, exit 0
(done, dry run, cancelled), 1 (refused, no terminal, a step failed).

Files: `src/indus/cli/uninstall.py`, `tests/unit/cli/test_uninstall.py`,
`docs/internals/uninstall-levels.md`; registration in `src/indus/cli/app.py` and the CLI contract
tables (`tests/contract/cli/non_tty_table.yaml`, `test_help_catalogue.py` golden order).

Edge cases / failure modes:
- rc file with similar user lines (`export PATH=...local/bin`, a comment naming indus): kept; only
  the exact marked block goes. An opening marker without its closing one: the file is left alone
  with a note. A symlinked rc: the target is rewritten in place (atomic, mode kept).
- Home inside the code dir: keep data refuses to remove the code (it would delete the data).
- Code dir is the user's home or a filesystem root: refused.
- A launcher that is not ours (points elsewhere): kept with a note.
- `--data` while this profile's gateway runs: refused with "stop it first".
- Windows: files of the running interpreter inside the code dir are locked; whatever cannot be
  removed is reported as a failed step.

Our design differences / decisions:
- PATH cleanup is by exact marked block, never by a pattern over lines (reference removes any line
  that mentions the product and `PATH`).
- Declining named profiles keeps `<root>/profiles` on a full uninstall from the default profile
  (reference: the home rmtree sweeps them anyway).
- Gateway processes are never killed by pattern; only the pid the control socket or status file
  names gets SIGTERM, after the planned-stop record.
- No legacy runtime trees, node symlinks, Electron cache sweeps or dashboard launchd jobs (we never
  installed them); no `python -m … --mode` entry (the desktop calls `indus uninstall` with the
  table's flags).
- Cancel exits 0.
- Install kind comes from `indus.cli.version.install_method` plus a `.git` check (U1's install-kind
  detector can replace it at integration).

Tests we will write:
- Temp home + temp user home: rc files with the block and similar user lines → only the block
  removed; unterminated block untouched; dry run removes nothing (files, services, keychain);
  interactive keep-data / full with typed `yes`; wrong word cancels; EOF → "Cancelled."; `-y` never
  removes named profiles; full with "y" to profiles removes them; fake keychain emptied on full
  only; Nix / Docker / desktop / no-git refused except `--data`; `--data` keeps code and other
  profiles; `--gui` removes only desktop pieces; `--gui-summary` JSON shape; launcher ownership;
  fake systemctl service removed; Windows PATH entry removal through a fake registry; non-TTY
  without `-y` exits 1.

## Decisions for the lead
- Registration: `uninstall` added to `REGISTRY` just before `acp` (research 04 §3.2.4 order:
  update, uninstall, acp), golden order and the non-TTY table updated (`uninstall: 1`). U1's
  `update` goes right before it.
- `--data` keeps the installer's state in the home (`tools`, `bin`, `.indus-bootstrap-complete`,
  the checkout when it lives there) so the runtime stays usable.
- Launcher ownership: a symlink into the checkout, or a file with the installers' launcher comment
  (`# indus launcher` from install.sh, `rem indus launcher` from install.ps1) that names the checkout.
- Skip choices / install kinds owned by U1: uninstall does not read them.

Compare checklist (filled after the rewrite; reopened `hermes_cli/uninstall.py`):
- [x] Options, typed confirmation, Ctrl-C/EOF, unattended rules (tests in `tests/unit/cli/test_uninstall.py`:
  `test_interactive_default_is_keep_data_after_typed_yes`, `test_anything_but_yes_cancels`,
  `test_end_of_input_cancels`, `test_unattended_full_never_removes_named_profiles`,
  `test_interactive_full_with_profiles_removed_on_request`, `test_no_terminal_without_yes_is_guidance`)
- [x] Ownership refusal and `--data` exception (`test_managed_installs_refuse_code_levels`,
  `test_tree_without_git_is_refused`, `test_data_level_works_on_a_managed_install`)
- [x] Gateway services per OS (`test_real_systemd_unit_is_disabled_and_removed`,
  `test_manual_gateway_gets_a_planned_stop_then_sigterm_by_pid`; launchd goes through the same manager)
- [x] PATH block removal (`test_keep_data_removes_only_the_marked_blocks`, `test_unterminated_rc_is_untouched_and_noted`,
  `test_symlinked_rc_target_is_rewritten_and_the_link_kept`, `test_windows_user_path_entry_is_removed`)
- [x] GUI level and summary (`test_gui_removes_only_the_desktop_pieces`, `test_gui_summary_shape`)
- [x] Data level (`test_data_level_refuses_while_the_gateway_runs`)
- [x] Gap found: named profiles' alias commands — removed with their profile
  (`test_interactive_full_removes_the_aliases_of_removed_profiles`)
- Deliberate differences: exact marked block instead of a pattern over lines; declining named profiles
  keeps `<root>/profiles`; gateways stopped by recorded pid only; keep data removes `<root>/tools`
  but only `indus.cmd` from `<root>\bin` (vendored helpers live there); Start menu entry and
  launcher removed only when they name the checkout; one "Cancelled." for every cancel, exit 0.
- Not taken: `/usr/local/bin` wrappers, node symlinks, legacy runtime trees, system-scope units
  (we never install one), macOS Library cache sweeps, dashboard launchd jobs, Windows user env vars
  (our installer sets none), the `python -m … --mode` entry, a reinstall hint with a URL.

## Status

Done (2026-10-21). Changed outside the task's files: `src/indus/cli/app.py` (REGISTRY row
`uninstall` before `acp`), `tests/contract/cli/test_help_catalogue.py` (golden order),
`tests/contract/cli/non_tty_table.yaml` (`uninstall: 1`), `docs/internals/acceptance-map.md`
(ACC-I42-1..3).

## Integration (wave 36)

The install kind now comes from `indus.cli.update.detect_kind` (D-16.752): it is a drop-in for the levels here and also recognises the desktop's bundled runtime (`INDUS_DESKTOP_BUNDLED`) and a git clone inside a dev container (a source checkout).
