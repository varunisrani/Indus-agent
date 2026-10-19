# P21 remaining backends — slice reports (base 730cf11)

## B2 — w19-p21b2.patch — DONE (P21-05..10 Modal create/lifecycle + exec/files, Daytona, Vercel create/lifecycle + exec/files, sync-back)
- New: backends/{sdk_shell (shared SDK base; SdkSupport + SandboxRecords protocols), sync_back}.py,
  backends/modal/{__init__,lifecycle,exec}.py, backends/daytona.py, backends/vercel/{__init__,lifecycle,
  exec}.py, tests (unit/backends/modal, vercel, test_daytona, test_sync_back; _stubs_b2.py STAND-IN —
  delete), notes ×6.
- Shared: config terminal.modal_mode (validator refuses `managed`, D-16.16), modal_image,
  daytona_image (empty = terminal.docker_image), vercel_runtime (""|node24|node22|python3.13),
  sync_back_max_bytes 2 GiB (+test); traps R02.2, R02.19, R02.31; ACC-D12-1.
- Wiring: one adapter from B1's sdk_call / stdin_payload / sandbox_records onto SdkSupport
  (call(op, fn, *, timeout, idempotent, on_deadline), guard, stage_stdin, stdin_chunks) and
  SandboxRecords (load, save, drop); tests switch to B1's fakes; entry points open_modal / open_daytona /
  open_vercel(scope, *, support, records?, sdk=None, session_id, task_key, overrides, clock, temp_dir)
  raise BackendUnavailable / EnvironmentConnectionError; requirement_reason never imports the SDK.
  MUST FIX in P21-12: registry CLEANUP_BOUND_S 15 s < sync-back 120 s + snapshot 60 s — SDK backends
  need a longer cleanup bound. Records store calls are blocking on the loop — run_blocking once B1 lands.
- SDK pins (optional extras, newest before 2026-10-04): modal 1.6.1 (Apache-2.0), daytona 0.220.0
  (Apache-2.0), vercel-sandbox 0.7.0 (MIT; pin directly in the `vercel` extra).
- Decisions (need D-ids): Vercel SDK no longer matches the reference (create_sandbox / create_process /
  fs.write_bytes, no runtime arg; runtime sent in the image slot — unverified, docs-vs-code entry +
  live-lane check; OIDC-only path relies on VERCEL_OIDC_TOKEN in the process env); interrupts signal
  the command's process group (reference stopped the sandbox), Vercel also uses vendor process kill;
  guard `timeout -k 2 <ceil(t)+2>`, vendor exec timeout 2 s later; image config rows; Daytona
  non-persistent deletes a same-name leftover. LEAD RULINGS: Vercel running-wait poll 500 ms (phase
  value, reference 250 ms); stale sync-back temp entries removed only when owner dead AND > 30 min
  (phase value).
- Follow-ups: confirm Vercel SDK import sends no telemetry; R02.30 rows for deadline/guard tests (B1).

## B1 — w19-p21b1.patch — DONE (P21-01 records store, P21-02 SDK call wrapper, P21-03 stdin staging, P21-04 Singularity/Apptainer, P21-11 vendor credentials/env hygiene, P21-15 fake SDKs + fake CLI)
- New: backends/{sandbox_records,sdk_call,stdin_payload,singularity,vendor_env}.py; fakes
  tests/fakes/backend/{sdk_kit,fake_modal,fake_daytona,fake_vercel,fake_apptainer}.py +
  sdk_surfaces.json (scripts/dev/sdk_surfaces.py); golden tests/golden/backends/snapshot_script.sh; tests;
  notes ×7 (sdk-versions, fake-sdks hold the vendor API shapes).
- Shared: pyproject extras modal==1.6.1, daytona==0.220.0, vercel = vercel-sandbox==0.7.0 — RE-LOCK and
  MANUAL LICENCE CHECK of new transitive packages (httpx2, vercel-internal-core, vercel-oidc,
  grpc/protobuf under modal…; the licence guard only walks [project].dependencies); config
  terminal.singularity_image, scratch_dir, disk_warning_gb; test_schema filter; safety/child_env.py
  vendor floor + DAYTONA_JWT_TOKEN, MODAL_OAUTH_CLIENT_SECRET, MODAL_OAUTH_REFRESH_TOKEN (test pins floor ⊇
  declarations); contract conftest singularity row; traps R02.1, 6, 7, 8, 16, 22, 30; ACC-D5-1, D5-2.
- Wiring: register singularity_entry() in registry.default_entries (P21-12); call
  vendor_env.register_vendor_names() at service start beside register_blocked_names("providers", …);
  ERROR_CODES backend.deadline, backend.sandbox_records_busy, backend.singularity.build; B2's SdkSupport /
  SandboxRecords adapter over sdk_call / stdin_payload / sandbox_records.
- Decisions (need D-ids): records lock timeout raises (reference wrote unlocked); failed SIF build is an
  error (no docker:// fallback); cgroup guess root or cgroup v2, one retry without limits; singularity
  profile-scoped with container_persistent; vercel extra pins vercel-sandbox; GET/DELETE budgets 30 s /
  15 s; child_env floor mirrors vendor declarations (layer rule).
- Follow-ups: remote-only contract cases for singularity (P21-16); ACC-D5-2 overlay restart needs the
  live lane; phase file mentions an `all` extra that pyproject does not have.
