# Task: Contract suite over seven backends + live lane (P21-16) Phase: 21 Owner: P21 slice B3 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tests/tools/` (names of the
Daytona, Vercel Sandbox, Modal and base-environment tests only, for the edge-case list). Closed
before writing. Evidence: none.

What it does (own words): the P05 execution contract (snapshot, cwd marker, odd output, timeout
backstop rc 124 + flag, interrupt rc 130, stdin, bounded vs unbounded capture) already runs over
local, docker, ssh, singularity and the scripted fake. This task adds the three SDK sandboxes,
each over its fake SDK (`tests/fakes/backend/fake_*.py`, P21-15) through the real backend modules,
so the shared cases run unchanged for all seven backends. A second module adds what a sandbox in
someone's cloud must also keep: per-session values never frozen in the snapshot (R02.1), forwarded
values only in the SDK's env parameter (R02.7), a replaced sandbox flagged once with
`environment_recreated` (R02.19), background jobs started, polled and killed inside the sandbox,
file read/write/patch through the backend's file operations, and a lost vendor link answered as
`degraded`. A `network`-marked live module runs "echo, write a file, read it back, recreate the
sandbox, read it again" against each real vendor, and skips cleanly without credentials, the SDK
extra or the CLI.

Inputs → outputs:
- `tests/contract/backends/conftest.py` (shared, P21-16 row): `SDK_ROWS`; `contract_entries()`
  adds the three SDK rows; the rig installs a fake vendor account (`Cloud`) and the fake SDK for
  the row under test, hands the factory the fake module, the real `VendorSupport`, the profile's
  real records store and the cloud's temp dir; vendor credentials are built at runtime (never a
  literal) and merged with any test secrets; the row's workdir lives inside the fake cloud (the
  fake maps sandbox paths there). New fixture `sdk_backend` (the three rows).
- `tests/contract/backends/test_sdk_backends_contract.py`: the SDK-only cases above.
- `tests/integration/backends/test_remote_backends_live.py`: markers `network` (all) and `live`;
  one test per vendor (Modal, Daytona, Vercel Sandbox, Apptainer); skip reasons name what is
  missing (`INDUS_LIVE_<VENDOR>=1` opt-in, credentials, extra, CLI).

Edge cases:
- CI installs no extras: the fakes stand in for the SDK, so the rig's rows carry no probe (the
  real probe would report the missing extra); the backends never import the SDK when a module is
  handed in.
- The fake sandbox runs real bash on this machine: every row's temp dir is the test's own
  directory, so nothing lands in `/tmp`.
- The live lane never runs in the default loop (`network` marker deselected) and needs an explicit
  opt-in variable as well as credentials, since it creates billable sandboxes.

Our design differences / decisions:
- The live lane's "persist across recreate" uses a persistent sandbox (snapshot / stop-start) and
  checks the file after a full cleanup and reopen; on Apptainer it uses the persistent overlay.
- Decision for the lead: the live lane needs `INDUS_LIVE_<VENDOR>=1` besides credentials (a stray
  token in a developer's environment must not start paid sandboxes).

Tests planned: `uv run pytest tests/contract/backends` green for local, docker, ssh, singularity,
modal, daytona, vercel_sandbox (and the fake); the live module skips cleanly here.

Compare checklist (after writing):
- [x] edge cases from the reference names covered: home-relative default cwd (unit tests, B2),
  staged stdin not in argv (P21-03 unit + contract stdin case), interrupt rc 130, recreated sandbox
  before a command (contract `environment_recreated`), cleanup idempotent (contract protocol case).
