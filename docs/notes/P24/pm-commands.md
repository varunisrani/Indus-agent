# Task: `indus pm` commands (P24-24) Phase: 24 Owner: wave-37 builder X1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `pm/cli.py` (subcommand set,
flags, output lines, exit codes: `lock`, `install`, `env`, `doctor`, `repair`, `gc`, `bundle`,
`status`, `update`); research 10 §3.9. Ours: P07 non-TTY rules (`cli/prompts.confirm`,
`tests/contract/cli/non_tty_table.yaml`), `cli/cmdkit.group`, our P24-23 core. Closed before
writing.
Evidence: none.

What it does (own words): the command surface over the helper-binary manager.

- `lock [NAMES] [--yes]`: resolve every target of our package list from the definitions and pin
  url + sha256 (an unchanged URL keeps its hash, a URL shared by every target collapses to
  `any`); downloads to hash, so without a terminal it needs `--yes`.
- `install [NAMES] [--without PKG]... [--from DIR]`: no names → every required package plus the
  default ones not declined; `--without` declines a default package now and later (recorded in
  the facts file; naming it again opts back in); a default that fails warns and does not fail the
  run; `--from` adds a bundle folder as the first (offline) origin.
- `env [NAMES]`: the exports of installed packages as JSON (`PATH` folders), never the caller's
  own environment (it can hold credentials and gets pasted into reports).
- `doctor`: facts against lock and store — ok, not installed, outdated, missing executable,
  tampered (tree digest differs); exit 1 when a required package is not usable or any entry is
  tampered.
- `repair`: re-realise every recorded package at its recorded version and hash (re-fetch through
  the blob cache, origins, mirror) when its entry is missing or tampered; the set of packages and
  the lock are not changed.
- `gc`: remove store entries, staging folders, partial downloads and cached archives the facts do
  not reference; keeps everything referenced.
- `bundle --out DIR`: stage lock, facts, the referenced store entries and their archives
  (`blobs/<sha256>`) plus a `bundle.json` manifest; `install --from DIR` in another root installs
  from it without the network.
- `status [--json]`: the last operation receipt (`{schema, operation, started_at, finished_at,
  outcome, exit_code, steps: [{name, ok, detail}]}`), or `receipt: null` before any.
- `update [NAMES] [--check] [--yes]`: newest upstream release at least 14 days old per package
  (GitHub releases or the Node release index; packages tied to another pin do not move), re-pin
  and install what changed; `--check` writes nothing and exits 1 when updates exist.

Every network step has a timeout (`pm.timeout_s`) and downloads resume with HTTP ranges.
Without a terminal nothing asks: commands that would download a whole lock need `--yes`, the
others print and exit by the P07 rules (R04.19).

Inputs → outputs: argv → stdout lines / JSON; exit 0 ok, 1 failure or guidance, 2 usage.

Files: `src/indus/pm/commands.py` (operations and receipts, no click), `src/indus/cli/commands/pm.py`
(click group), `tests/unit/pm/test_commands.py`, `tests/unit/cli/commands/test_pm.py`; shared rows
in `cli/app.py`, `tests/contract/cli/non_tty_table.yaml`, `tests/contract/cli/test_help_catalogue.py`.

Edge cases / failure modes:
- No lock yet: `install` / `update` say "run `indus pm lock`" and exit 1, no network.
- `--without` naming a required or unknown package: usage error, exit 2.
- `bundle --out` into a non-empty folder: refused; a missing archive is fetched (verified) first.
- `update` resolver failure for one package: reported, nothing applied, exit 1.
- A receipt is written for every operation that changes something, including partial failures.

Our design differences / decisions:
- No `--extra`, `--tools-only`, `--test-environment`, `--target`, `--uv`, `--npm`, `--termux`
  (no Python or Node environments are managed; cross-target staging is a follow-up).
- `bundle` stages helper binaries only (no repo snapshot, no virtualenv).
- `env` prints exports only.
- `lock` and `update` (without `--check`) ask first; `--yes` for scripts.

Tests we will write:
- `doctor` reports a tampered store entry; `gc` keeps everything the facts reference; `bundle`
  output installs offline in a second temp root; `status --json` shape snapshot; `update --check`
  with the 14-day rule (fake clock); `install --without`; non-TTY exit codes.

## Decisions for the lead
- Declined defaults live in `facts.json` (`declined`), not in config.
- Receipt at `<root>/pm/receipt.json` (last one only).

Compare checklist (filled after the rewrite; reopened `pm/cli.py`):
- [x] Subcommand set: lock, install, env, doctor, repair, gc, bundle, status, update (test:
  `tests/contract/cli/non_tty_table.yaml` rows, `tests/unit/cli/commands/test_pm.py`)
- [x] `lock`: every target, an unchanged URL keeps its hash, one URL for all targets collapses
  to `any` (test: `test_commands.py::test_lock_pins_every_target_and_collapses_shared_urls`)
- [x] `install --without` only for default packages, persisted, naming it opts back in; default
  failures warn (test: `::test_without_declines_a_default_now_and_later`,
  `::test_without_takes_only_default_packages`, `::test_a_failing_default_warns_and_does_not_fail`)
- [x] `env` prints package exports only (test: `::test_env_exports_only_installed_packages`)
- [x] `doctor`: not for this machine / not installed (soft for optional) / outdated / tampered;
  exit 1 when bad (test: `::test_doctor_reports_a_tampered_store_entry`, `::test_doctor_states`,
  `test_pm.py::test_doctor_exits_1_on_a_tampered_entry_and_repair_fixes_it`)
- [x] `gc` keeps what the facts reference, removes staging, partials and orphans (test:
  `::test_gc_keeps_everything_the_facts_reference`)
- [x] `bundle` → offline install in a second root (test:
  `::test_a_bundle_installs_offline_in_a_second_root`)
- [x] `status` shape (test: `::test_status_json_shape`)
- [x] `update`: lookup failure applies nothing, `--check` exits 1 when updates exist, 14-day rule
  (test: `::test_update_takes_the_newest_release_at_least_14_days_old`, `::test_a_failed_lookup_is_reported`,
  `test_pm.py::test_update_check_and_apply`)
- [x] Non-TTY: `lock` / `update` need `--yes`; nothing asks (test:
  `test_pm.py::test_lock_asks_first_and_needs_yes_without_a_terminal`,
  `::test_fresh_profile_commands_without_a_terminal`)
- Deliberate differences: `lock` pins our package list at its definition versions (the reference
  relocks uv.lock with no arguments and pins one tool with `--bump`); `update` reinstalls only
  re-pinned packages that are installed (the reference installs every re-pinned one); `status`
  prints a human summary unless `--json`; `repair` re-realises store entries (the reference
  recovers the Python environment); `bundle` stages helper binaries and their archives only.
- Not taken: `--extra`, `--tools-only`, `--trust-recorded`, `--test-environment`, `--target`
  (cross-target staging), `update --target/--uv/--npm/--termux`, live progress lines.

## Status

Done (2026-10-21). Shared: `src/indus/cli/app.py` (`pm` row), `tests/contract/cli/non_tty_table.yaml`,
`tests/contract/cli/test_help_catalogue.py`, `docs/internals/traps-ledger.md` (R04.19 lines).
