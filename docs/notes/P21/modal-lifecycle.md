# Task: Modal direct, create and lifecycle (P21-05) Phase: 21 Owner: P21 builder B2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/modal.py`
(`ModalEnvironment.__init__`, `_get_snapshot_restore_candidate`, `_resolve_modal_image`, `cleanup`),
`tools/terminal_tool_backends.py` (`_get_modal_backend_state`, `_modal_unavailable_reason`,
`_build_modal_env`). Vendor surface checked against the modal 1.6.1 wheel (Apache-2.0; newest
release before the 2026-10-04 exclude-newer date). Closed before writing.
Evidence: none.

What it does (own words): `terminal.backend: modal` runs commands in a Modal sandbox created with
the user's own Modal token pair. Only direct mode exists: `terminal.modal_mode` takes `auto` or
`direct` (same meaning); `managed` is refused when the config is read, citing D-16.16 (the vendor
gateway is not offered). The token pair comes from the profile's secret store, else from the
vendor's standard environment names; when either half is missing the backend reports a
requirement reason instead of failing at import or crashing. A sandbox lives up to 3600 s and is
created within 300 s. A persistent profile first tries the filesystem snapshot recorded for its
task key; when that restore fails the record is dropped, the sandbox is created from the base
image, and the first command result says `environment_recreated` (R02.19). Cleanup of a persistent
sandbox takes a filesystem snapshot (60 s), records its id, then terminates (15 s); a
non-persistent one only terminates.

Inputs → outputs: scope config (`terminal.modal_mode`, `modal_image`, `container_cpu`,
`container_memory_mb`, `container_persistent`, `timeout`), secrets `MODAL_TOKEN_ID` /
`MODAL_TOKEN_SECRET` → a sandbox handle + `recreated` flag; snapshot id in the records store.

Files: `src/indus/backends/modal/__init__.py`, `src/indus/backends/modal/lifecycle.py`,
`src/indus/backends/sdk_shell.py` (shared SDK shell base, new), `tests/unit/backends/modal/test_lifecycle.py`,
`src/indus/foundation/config/schema.py` (shared: `modal_mode`, `modal_image`).

Edge cases / failure modes:
- record exists but the snapshot is gone → drop only that record (the id must still match), base
  image, recreated flag.
- snapshot at cleanup fails → logged, sandbox still terminated, record left as it was.
- terminate fails/hangs → bounded at 15 s by the SDK call wrapper, logged.
- `modal` extra not installed → `BackendUnavailable` naming the extra, never ImportError.
- tokens never on argv/logs; the SDK client is built from the pair in memory.

Our design differences / decisions:
- the client is built with the SDK's from-credentials constructor, so the token pair never goes
  through `os.environ`.
- base image: `terminal.modal_image`, else `terminal.docker_image` (our sandbox image, D-16.64);
  a value starting `im-` is an image id. No pip repair / python install layers (the reference
  adds them for arbitrary registry images; our image already has what we need).
- no legacy bare-task-id snapshot keys (D-16.3), no managed path (D-16.16).
- every vendor call goes through the injected `SdkSupport.call` (one call site per backend, B1's
  wrapper plugs in there).

Tests we will write (fake Modal SDK, stub support/records):
- restore path used when a record exists; bad snapshot → record deleted, base image, recreated.
- persistent cleanup = snapshot then terminate, id recorded; non-persistent = terminate only.
- `managed` refused by config validation; missing token → requirement reason.

## Decisions for the lead
- `terminal.modal_image` row added (empty = `terminal.docker_image`); the phase lists no image key.
- the SDK call wrapper, records store and stdin helpers are B1's; this slice codes against the
  `SdkSupport` / `SandboxRecords` protocols in `sdk_shell.py` and tests with
  `tests/unit/backends/_stubs_b2.py`. Integration: implement those two protocols with B1's
  `sdk_call` / `stdin_payload` / `sandbox_records` (one adapter) and drop the stubs.
- registry wiring (P21-12) passes `support` and `records`; `open_modal` takes them as arguments.

Compare checklist (filled after the rewrite; reopened `tools/environments/modal.py`,
`tools/terminal_tool_backends.py`):
- [x] 3600 s sandbox, 300 s create, 60 s snapshot, 15 s terminate (tests:
  `tests/unit/backends/modal/test_lifecycle.py::test_restore_path_is_used_when_a_record_exists`,
  `::test_persistent_cleanup_snapshots_then_terminates`)
- [x] restore candidate first, failed restore → record dropped, base image, recreated warning
  (`::test_bad_snapshot_falls_back_to_the_base_image_and_flags_recreated`)
- [x] snapshot kept indefinitely (`ttl=None`), snapshot failure non-fatal, terminate always
  (`::test_snapshot_failure_still_terminates_and_keeps_the_record`)
- [x] non-persistent: no restore, no snapshot (`::test_non_persistent_only_terminates_and_never_restores`)
- [x] managed refused at config validation and at open, citing D-16.16
  (`::test_managed_mode_is_refused_by_config_validation`, `::test_managed_mode_is_refused_again_before_any_sdk_call`)
- [x] missing token pair → requirement reason, no SDK call (`::test_missing_token_is_a_requirement_reason_not_a_crash`)
- Deliberate differences: no legacy bare-task-id keys (D-16.3); no managed gateway (D-16.16); the
  client comes from the token pair in memory (the reference relies on the SDK's own env/config
  lookup); no pip-repair layers on registry images; a record is dropped only while it still names
  the failed snapshot (same as the reference's id check).
- Not taken: the reference's `ephemeral_disk` probe through `inspect.signature` (the disk setting is
  not sent to Modal; container_disk_mb stays a Docker/Daytona setting).

## Status

Done (2026-10-19). Shared: `src/indus/foundation/config/schema.py` (+ `modal_mode` with the D-16.16
validator, `modal_image`) and its test. Integration: replace `tests/unit/backends/_stubs_b2.py`
with B1's `sdk_call` / `stdin_payload` / `sandbox_records` behind the `SdkSupport` /
`SandboxRecords` protocols; registry wiring is P21-12.

Integrated (lead, 2026-10-19): `backends/sdk_ports.py` implements the ports (`VendorSupport` over
`sdk_call` / `stdin_payload`, `ProfileRecords` over `sandbox_records`, whose calls now run on the
`io` pool, so `SandboxRecords` is async); the stubs are gone and the tests run on B1's fake SDKs
(`tests/unit/backends/sdk_rig.py`). Registry row `modal` in `backends/sdk_entries.py` (P21-12
wiring).
