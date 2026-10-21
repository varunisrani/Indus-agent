# Task: install.sh core (P23-03) Phase: 23 Owner: wave-36 builder I1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `scripts/install.sh` (flag parser,
logging and the quiet runner, receipt writer, signal traps, JSON frame and manifest, stage table,
stage runner, main guard and ladder). P20-09 contract: `apps/desktop/electron/installer/stageProtocol.ts`
and `apps/desktop/testing/fakeInstaller.mjs` (ours). Closed before writing.
Evidence: none.

What it does (own words): one bash script installs Indus Agent on Linux and macOS. It is built by
`scripts/install/build.py` from fragments in `scripts/install/sh/` (core, repository, runtime) plus
two generated sections (pins from `pins.json`, the stage table from `stages.json`) and a trailer
that runs `ins_main "$@"` only when the file is executed — sourcing it defines functions and runs
nothing. Termux is refused (a later channel); other systems are pointed to the PowerShell script.

Flags: `--channel stable|canary|main` (env `INDUS_INSTALL_CHANNEL`), `--branch`, `--commit`,
`--dir`, `--indus-home`, `--manifest`, `--stage NAME`, `--json`, `--non-interactive` (alias
`--skip-setup`), `--include-desktop`, `--skip-browser`, `--skip-computer-use`, `--verbose`, `-h`;
the PowerShell spellings (`-Channel`, `-IndusHome`, …) too, and `--flag=value`. A flag without its
value exits 2, an unknown flag exits 1, a channel outside the three exits 2.

The ladder: `prerequisites, repository, runtime, dependencies, config, products, setup, gateway,
complete`. `--manifest` prints one JSON line `{protocol_version: 1, stages: [{name, title,
category, needs_user_input}]}`. `--stage X --json` runs one stage and prints one result frame
`{ok, stage, skipped, reason?}` as the last stdout line. With `--non-interactive`, `setup` and
`gateway` are skipped with a reason. Child output goes to `<home>/logs/install.log`; the terminal
gets one status line per stage (`--verbose` streams the children too); colour only on a TTY without
`NO_COLOR`; `UV_NO_CONFIG=1` always.

Signals (R10.15): INT/TERM/HUP are only noted while a stage runs in the foreground; the stage's own
exit decides. A stage that succeeds after an INT lets the ladder continue; after TERM/HUP the run
stops before the next stage. A stage that fails after a signal is recorded `interrupted` and the
script re-raises the signal so its exit status is the signal's (130 for INT). A full-ladder run
writes one receipt `<home>/logs/install_receipts/<id>.json` `{id, installer: "sh", outcome,
failed_stage?, failure_class?, started_at, finished_at}` through a temp file and an atomic no-clobber
link: the first writer wins and every later write is a no-op. Nothing is uploaded (D-16.49).

Inputs → outputs: argv/env → exit status, stdout frames/manifest, log file, receipt.

Files: `scripts/install/sh/00-core.sh`, `scripts/install/build.py`, `scripts/install/stages.json`
(the shared stage table; P23-06 reads the same file), `scripts/install.sh` (generated),
`tests/integration/install/test_install_sh_core.py`, `tests/integration/install/stage_protocol.py`,
`tests/fakes/install/bin/{git,curl}`, `tests/fakes/install/fake_tools.py`.

Edge cases / failure modes:
- value flags followed by another flag (`--dir --json`) → "needs a value", exit 2.
- `--stage nope --json` → frame `ok: false` naming the unknown stage, exit 2.
- receipt dir unwritable → receipt silently skipped; never fails the install.
- a stage killed outright (no class recorded) → receipt `failed` with class `other`.
- `curl | bash`: `BASH_SOURCE` is empty; the trailer still runs main.
- bash 3.2 (macOS): no `${x,,}`, no associative arrays, guarded empty-array expansions.

Our design differences / decisions:
- Our stage names (`runtime`, `dependencies`) replace the reference's venv/deps pair; receipts live
  under `<home>/logs/install_receipts/`, never a telemetry dir; timestamps are UTC ISO strings.
- First-writer-wins is atomic (`ln` of a finished temp file), not check-then-move.
- Only the ladder parent writes receipts; a stage subshell hands its failure class back through a
  run directory, so an interrupt seen only by the parent still records `interrupted`.
- Receipts are written for full-ladder runs only (the desktop drives single stages and writes its
  own marker).
- `--channel canary` added (D-16.56).

Tests we will write:
- manifest JSON (also through the P20-09 frame rules, and the real TS parser when node can strip
  types); flag errors 1/2; `--stage config --json` frame; non-interactive skip frame; unknown stage;
  SIGINT during a stage that exits 0 continues; SIGINT then failure → receipt `interrupted`, exit by
  SIGINT; TERM after a passing stage stops before the next; second receipt write is a no-op;
  `install.sh` equals the build; sourcing runs nothing.

## Decisions for the lead
- `scripts/install/stages.json` is created here (P23-03 needs it first); P23-06 (I2) must read the
  same file — at integration keep one copy (this one, unless I2's titles are agreed).
- Receipt outcome words: `success`, `failed`, `interrupted`.

Compare checklist (filled after the rewrite; reopened `scripts/install.sh`):
- [x] flags, PowerShell spellings, missing value → 2, unknown → 1, bad channel → 2 (test: `tests/integration/install/test_install_sh_core.py::test_flag_errors`)
- [x] ladder order and manifest shape; desktop parser reads it (tests: `::test_manifest_json`, `::test_the_desktop_parser_reads_the_real_manifest_and_frames`, `::test_same_shapes_as_the_p20_fake_installer`)
- [x] frame shape, non-interactive skip, unknown stage (tests: `::test_stage_config_json_frame`, `::test_powershell_spellings_and_help`, `::test_unknown_stage`)
- [x] signals: INT during a passing stage continues; INT then failure → interrupted + re-raise; TERM stops before the next stage (tests: `::test_sigint_during_a_stage_that_succeeds_continues`, `::test_sigint_then_failure_is_interrupted_and_dies_by_the_signal`, `::test_sigterm_after_a_passing_stage_stops_before_the_next`)
- [x] receipts local, one per ladder run, first writer wins (tests: `::test_second_receipt_write_is_a_no_op`, `::test_a_full_ladder_writes_one_success_receipt`, `::test_a_stage_run_writes_no_receipt`)
- [x] child output to the log, the failing command's cause in the reason (tests: `::test_child_output_goes_to_the_log`, `::test_a_failed_command_names_its_cause`)
- [x] gap found: the reference streams child output in CI transcripts; added (CI / GITHUB_ACTIONS
  imply `--verbose`), pinned by `::test_child_output_goes_to_the_log`
- [x] sourcing defines functions only (test: `::test_sourcing_runs_nothing`); built script fresh (test: `::test_install_sh_equals_the_built_output`)
- Deliberate differences: receipts under `<home>/logs/install_receipts/` with ISO timestamps and
  outcome words success/failed/interrupted, written only by the ladder parent and atomically; our
  stage names; `--channel canary`; no banner; on a terminal the children's output is not redrawn
  into a live status line (one line per stage instead).
- Not taken: a dispatchable-but-unlisted `desktop` stage (`--include-desktop` changes the products
  title only; the desktop app is not built by install.sh).

## Status

Done (2026-10-21). `scripts/install/stages.json` is shared with P23-06 (see Decisions).

## Integration (wave 36)

`build.py` also builds `install.ps1` (the PowerShell half moved in from I2's stand-in) and renders `INS_EXTRAS_COMMON` / `INS_EXTRAS_BROWSER` / `INS_EXTRAS_COMPUTER_USE` from `stages.json` `extras` into the generated stages section (D-16.751).
