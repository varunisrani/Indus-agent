# Task: Update — finish, restart, receipts, CLI (P23-19) Phase: 23 Owner: wave-36 builder U1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/update_cmd.py`
(`_settle_windows_resume`, `_resume_paused_gateways_at_exit`, `_record_update_step`,
`_record_update_skip`, `_record_pre_update_backup_outcome`, `_source_completion_request`),
`website/docs/getting-started/updating.md` (phases 5–7, "Gateway downtime during an update", "Why
the gateway restart can take a while"). Research 04 §3.25. Closed before writing. Evidence: none.

What it does (own words): `indus update --finish <request>` runs in the *new* code. It (1) runs the
config migration for this profile, asking first (`-y` accepts; without a terminal and without `-y`
it is skipped and owed); (2) rebuilds the web and TUI bundles a checkout already had, by building
into a staging dir and renaming it over the old one only when the build produced its entry file —
a failed build keeps the old bundle and becomes an owed follow-up, never a failure (exit 0);
(3) restarts this profile's gateway: through its systemd/launchd service when one is installed,
else a running manual gateway is asked over the control socket to stop for a restart (never
killed) and started again detached on the new code; `--no-gateway-restart` defers it. Every run
writes one receipt `<root>/logs/update_receipts/<stamp>.json` with each phase's outcome (`ok`,
`failed`, `skipped`, `owed`, `deferred`) and the owed follow-ups, and mirrors its output to
`<root>/logs/update.log`. `indus --version` names the install kind and, when a check ran in the
last 24 h, whether an update is available.

Inputs → outputs: request JSON (schema 1: checkout, channel, from/to, flags, receipt so far, stash
label) → migrated config, rebuilt bundles, restarted gateway, receipt JSON, exit 0 (or 1 when the
stash could not be re-applied); CLI flags `--check`, `--plan`, `--channel`, `--set-channel`,
`--install-id`, `--backup`, `--no-backup`, `-y`, `--no-gateway-restart`, hidden `--finish`.

Files: `src/indus/cli/update/finish.py`, `src/indus/cli/commands/update.py`,
`src/indus/cli/version.py`, `src/indus/cli/app.py` (registry), `tests/unit/cli/update/test_finish.py`,
`tests/unit/cli/commands/test_update.py`, `tests/contract/cli/*` (golden order, non-TTY row).

Edge cases / failure modes:
- the request file is missing/unreadable or of another schema → exit 1, nothing changed;
- no gateway running and no service → restart phase `skipped` ("not running");
- the service manager is installed but its tool is missing → owed follow-up;
- a manual gateway that does not stop within the wait → owed follow-up (it was asked, not killed);
- the receipt directory is unwritable → a warning; the receipt never fails an update;
- `updates.check: false` hides the availability line (recorded in the cache as `notices`).

Our design differences / decisions:
- restart covers this profile's gateway only (D-16.57); no fleet restart, no multiplex migration;
- the desktop bundle is not rebuilt (the desktop app owns its own update, P20);
- the update-availability line reads a small cache file so `--version` stays stdlib-only and fast
  (D-04.4); nothing goes to the network on `--version`.

Tests we will write: fake service manager (systemd runner recorder) restarted; manual gateway asked
via a fake control client and started again; `--no-gateway-restart` defers; dist build failure →
exit 0 with the follow-up in the receipt and the old bundle kept; receipt lists every phase;
`--version` shows kind and availability; CLI flags (`--install-id`, `--set-channel`, refusal
without a terminal).

## Decisions for the lead
- A manual gateway is restarted by `stop {restart: true, reason: "update"}` over the control socket
  then a detached `indus gateway start` from the new venv; the daemon has no in-place restart verb.

Compare checklist (filled after the rewrite; reopened `update_cmd.py` resume/receipt functions and
the doc's restart sections):
- [x] post-commit failures are owed follow-ups with exit 0
  (test: `tests/unit/cli/update/test_finish.py::test_dist_build_failure_keeps_the_old_bundle_and_exits_0`)
- [x] service-managed gateways restart through their manager
  (`::test_systemd_service_is_restarted_through_its_manager`)
- [x] manual gateways are asked, not killed (`::test_manual_gateway_is_asked_over_the_control_socket`)
- [x] `--no-gateway-restart` defers (`::test_no_gateway_restart_defers`)
- [x] a deliberate backup opt-out is a skip with its reason, a failed snapshot a failed phase
  (`::test_receipt_lists_every_phase`,
  `tests/integration/update/test_update_apply.py::test_failed_snapshot_warns_and_the_update_goes_on`)
- [x] `--check` exits 2 in the reference for a managed install; ours exits 1 like every other
  refusal (the P07-26 non-TTY contract uses 1 for guidance)
- Deliberate differences: no atexit/Windows resume dance (no gateway is paused before the move).
- Not taken: macOS app copy, desktop rebuild, chat `/update` gateway-mode prompts over files.

## Status

Done (2026-10-21). Shared files changed: `src/indus/cli/app.py` (registry row),
`src/indus/cli/version.py`, `tests/contract/cli/test_help_catalogue.py`,
`tests/contract/cli/non_tty_table.yaml`.

## Integration (wave 36)

Integration: `--finish` removes the update marker its pid claimed before the hand-off, whatever the outcome (D-16.761).
