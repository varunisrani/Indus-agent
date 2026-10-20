# Task: Adapter matrix gate and doctor gateway checks (P16-36) Phase: 16 Owner: wave-28 builder E17

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/doctor.py` (the check list and
run order), `hermes_cli/doctor_platform.py` (skimmed: gateway supervision and linger checks),
`hermes_cli/doctor_state.py` (skimmed: host gateway note, duplicate platform credentials across
profiles), `hermes_cli/doctor_tools.py` (skimmed: the WhatsApp bridge package audit),
`hermes_cli/doctor_connectivity.py` and `doctor_config.py` (skimmed: no per-platform gateway rows).
Evidence: none.

What it does (own words): two guards for the messaging gateway.

1. **Adapter matrix** (`tests/contract/gateway/test_adapter_matrix.py`): every platform the
   registry ships must either run the shared contract suite over its own wire fake, or be one of
   the named non-chat surfaces with its own contract tests. The test lists every built-in registry
   entry, builds every contract case once to learn which platform it drives (the adapter's
   `platform`) and which wire fake serves it (the env's wire object, which must come from a
   `tests/fakes/platform/<name>_wire.py` file that exists), and fails with the platform named when
   one has no case, a case module did not import (for example because its wire fake was removed), a
   case uses no wire fake, or a case drives a platform the registry does not know. The case modules
   are now found by file name (`*_case.py`) and imported one by one, so one broken case module no
   longer stops the whole contract directory from loading: the failure is kept and the matrix test
   reports it. Non-chat surfaces (API server, webhook receiver, Graph change notifications, the Raft
   wake bridge) cannot send chat replies, so the shared suite does not fit them; the test names
   each one's own contract test files and fails if they are missing. Photon is being built in
   parallel: it is allowed as pending until its case lands (the integrator removes the entry; the
   test fails if a pending platform gains a case).
   CI: a dedicated `adapters` job runs the contract directory (matrix included) and is required by
   the gate.
2. **`indus doctor` gateway section** (`src/indus/cli/doctor/gateway_checks.py`, title "Messaging
   gateway"): reads the profile's gateway settings (no SDK import, no gateway import at module load:
   the gateway modules are imported by name, like the setup wizard) and reports
   - the daemon: running (pid), stopped, stale, or never started;
   - which platforms are on; for each one on: whether its library is installed (install hint),
     whether every credential is present (names only, never values; a platform switched on in config
     with credentials missing is an error), and for a running daemon the platform's live state;
     platforms with some but not all credentials are reported as partly set up;
   - sidecar and daemon reachability for the platforms that depend on a process the user runs:
     signal-cli (`GET <SIGNAL_HTTP_URL>/api/v1/check`), the SimpleX CLI (a TCP connect to the
     WebSocket URL's host and port), the BlueBubbles server (`GET <url>/api/v1/ping` with no
     password: any HTTP answer means reachable), the WhatsApp bridge (Node.js on PATH, the bridge
     script present, a provider module configured — the adapter starts the bridge itself);
     bounded by a short timeout, failures are warnings;
   - port conflicts among the inbound listeners: each enabled platform's webhook port (its
     `webhook_port`, else the adapter's default: SMS 8080, WhatsApp Cloud 8090, WeCom callback and
     BlueBubbles 8645, LINE and Graph 8646, Google Chat (HTTP mode) 8647, Feishu (webhook mode)
     8765, Teams 3978, Telegram (webhook mode) 8443) plus the API server (8642) and webhook receiver
     (8644) when on; two on one port is an error naming both and the setting that moves one;
   - Discord voice readiness when Discord is on: the `discord-voice` extra, ffmpeg and libopus
     (information only: voice is optional);
   - the access mode for each platform that is on (what a stranger's DM gets, and why).

Inputs → outputs: registry + contract cases + wire fake files → pass / named failures; profile
config, env, secret store, daemon status file, the user's local daemons → doctor findings.

Files: `tests/contract/gateway/test_adapter_matrix.py`, `src/indus/cli/doctor/gateway_checks.py`,
`tests/unit/cli/test_doctor_gateway.py` (new). Shared: `tests/contract/gateway/conftest.py` (case
discovery), `src/indus/cli/doctor/checks.py` (one import registers the section, as the cloud
backends check does), `tests/unit/cli/commands/test_doctor.py` (the check order list),
`.github/workflows/ci.yml` (the `adapters` job and the gate's needs).

Edge cases / failure modes:
- a case module that fails to import (missing wire fake, syntax error): named, with the error.
- a platform covered only by a variant case (`feishu_webhook`): found through the adapter's
  `platform`, not the case name.
- a pending platform that is not registered yet: fine; one that has a case: fail (stale entry).
- doctor with no gateway configured: one info line, nothing else (no network).
- a reachability probe that hangs: bounded (2 s); never a credential in the request.
- unreadable settings or a gateway module that fails to import: one warning, the run goes on.
- ports: `0` (any free port) never clashes; a port that cannot be read is ignored.

Our design differences / decisions:
- the reference doctor has no per-platform gateway section; ours follows the phase row.
- not taken from the reference doctor: systemd linger / s6 supervision rows (P17/P23 service
  install territory), duplicate credentials across profiles (P16-31 wizard and R04.26 already
  refuse cloned bot tokens), the npm audit of the bridge (a CI concern).
- reachability probes run on every `indus doctor`, not only with `--live` (they only touch
  addresses the user configured, usually loopback).

Tests we will write:
- matrix: the real registry passes; a copy of the wire fake directory without one file fails naming
  that platform; a case-module import error is named; a registered platform with no case is named;
  a case with no wire fake is named; an unknown platform's case is named; a pending platform with a
  case is named; non-chat surfaces need their contract files.
- doctor: nothing configured → one info row; a platform on with its credential → ok rows with no
  value printed; enabled in config with a missing credential → error; partly set up → warning;
  library missing → warning with the install hint; two platforms on one port → error naming both;
  `webhook_port` moves one → no clash; reachability ok / refused through injected probes;
  daemon state rows; the access mode row; Discord voice readiness rows; the section is registered
  in order.

## Decisions for the lead
- Case modules are discovered (`*_case.py`) instead of one import line each; new adapters no longer
  edit the conftest (E15's `photon_case.py` is picked up as is).
- Non-chat surfaces stay out of the shared chat suite (api_server, webhook, msgraph, raft) with
  their own contract files listed in the matrix test.
- Reachability probes always on (not behind `--live`).
- The `adapters` CI job installs no extras (the drift rows of `test_vendor_names.py` stay in the
  extras lane the lead runs at re-lock).

Compare checklist (filled after the rewrite; reopened the doctor files listed under Read):
- [x] doctor never fails the run on one broken section (`test_doctor_gateway.py::test_the_section_runs_last_and_never_fails_the_run`)
- [x] fixes are commands to run, never values (`::test_switched_on_without_credentials_is_an_error`,
  `::test_a_platform_that_is_on_shows_names_never_values`, `::test_local_daemons_are_probed_without_credentials`)
- [x] gateway process state shown (the reference's host-gateway note) (`::test_a_running_gateway_reports_each_platforms_live_state`,
  `::test_a_stopped_or_stale_gateway`)
- [x] row's Verify: removing one wire fake fails the matrix with the platform named
  (`test_adapter_matrix.py::test_a_removed_wire_fake_fails_with_the_platform_named`,
  `::test_removing_a_wire_fake_fails_the_real_gate_with_the_platform_named`); two adapters on one port
  reported (`test_doctor_gateway.py::test_two_listeners_on_one_port_are_reported`)
- Deliberate differences: a per-platform gateway section the reference does not have (phase row);
  reachability probes on every run.
- Not taken: systemd linger / s6 rows, duplicate credentials across profiles, npm audit of the bridge
  (see "Our design differences").

## Status

Done (2026-10-20). Shared files changed: `tests/contract/gateway/conftest.py` (case discovery),
`src/indus/cli/doctor/checks.py` (registration import + docstring), `tests/unit/cli/commands/test_doctor.py`
(check order), `.github/workflows/ci.yml` (`adapters` job, gate needs), acceptance map (ACC-K8-1 ref).
Live vendor lanes (`INDUS_LIVE=1`) stay owner work.

Integration (2026-10-20): Photon landed with its case, so `PENDING` is empty (the stale-entry test
now uses a made-up pending platform); the doctor's Node-sidecar rows cover the Photon sidecar too
(Node.js, script, provider, its two project credentials) and the port check its loopback port
(`platforms.photon.sidecar_port`, 8789). D-16.562, D-16.563.
