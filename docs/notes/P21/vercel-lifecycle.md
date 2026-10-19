# Task: Vercel Sandbox, create and lifecycle (P21-08) Phase: 21 Owner: P21 builder B2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/vercel_sandbox.py`
(`_create_sandbox`, `_attach_fresh_sandbox`, `_wait_for_running`, `_snapshot_sandbox`,
`_stop_sandbox`, `_ensure_sandbox_ready`, `cleanup`), `tools/terminal_tool_backends.py`
(`_check_vercel`, `_is_supported_vercel_runtime`). Closed before writing.
Vendor surface: the sandbox client now ships as `vercel-sandbox` (MIT; 0.7.0 is the newest before
2026-10-04, pulled in by `vercel` 0.11.4, MIT). Its synchronous API differs from what the reference
used: `vercel.sandbox.sync.SyncSandboxClient.create(options=SandboxServiceOptions(
credentials_factory=…))`, `client.create_sandbox(name, image, source=SnapshotSource(snapshot_id),
execution_time_limit, resources=SandboxResources(vcpus, memory), env)`, `client.get_sandbox(name)`,
sandbox `status` (`SandboxStatus`: pending, running, stopping, stopped, failed, aborted,
snapshotting), `cwd`, `name`, `create_process(cmd, args, cwd, env)` → process `wait()`, `kill()`,
`stdout.read()`, `fs.write_bytes(path, data, mode)`, `fs.read_bytes(path)`, `snapshot()` → `.id`,
`stop()`, `client.close()`. There is no `runtime` argument any more.
Evidence: none.

What it does (own words): auth is a token + project id + team id (all three), or a short-lived OIDC
token; anything else is a requirement reason. `container_disk_mb` must be 51200 (default) or 0:
any other value is refused before any API call. A configured `terminal.vercel_runtime` (one of the
vendor's documented names) replaces the image; otherwise the vendor's default image is used. A
persistent profile creates from its recorded snapshot; a failed restore drops that record and
creates from the image (recreated flag). Create is retried up to 3× on transient errors through the
wrapper. Then wait until running: ≤ 30 s, polling every 500 ms. The sandbox time limit is at least
5 min. Before each command a sandbox found stopped/failed/aborted is replaced (counted in
`recreations`, flagged once). Cleanup: sync back → snapshot when persistent (record its id; a
snapshot holds no processes) → stop (≤ 15 s, polling 500 ms) → close the client.

Files: `src/indus/backends/vercel/__init__.py`, `src/indus/backends/vercel/lifecycle.py`,
`tests/unit/backends/vercel/test_lifecycle.py`, `src/indus/foundation/config/schema.py` (shared:
`vercel_runtime`).

Edge cases: partial token trio → reason names what is missing; terminal state during the wait →
error at once; snapshot failure at cleanup → logged, still stopped.

Our design differences / decisions:
- credentials are handed to the SDK through its credentials factory (no process env). The OIDC
  path uses the vendor's own lookup of `VERCEL_OIDC_TOKEN` in the process env (the SDK decodes the
  project/team from it).
- a runtime name is sent in the image slot: the pinned SDK has no runtime parameter (docs-vs-code
  item for D-16.24; check in the live lane).

Tests: bad disk refused with zero calls; 429 then success on create; wait gives up at 30 s on a fake
clock; persistent cleanup order sync-back → snapshot → stop; unknown runtime rejected by config
validation; restore failure → record dropped, recreated.

## Decisions for the lead
- pin `vercel-sandbox==0.7.0` directly in the `vercel` extra (it is the package with the API);
  `vercel==0.11.4` pulls a large set of unrelated packages.
- runtime-in-image-slot (above).

Compare checklist (filled after the rewrite; reopened `tools/environments/vercel_sandbox.py`,
`tools/terminal_tool_backends.py`):
- [x] disk other than 51200/0 refused before any call (`tests/unit/backends/vercel/test_lifecycle.py::test_bad_disk_is_refused_with_zero_api_calls`)
- [x] create retried on transient errors only (`::test_429_then_success_on_create`, `::test_400_on_create_is_not_retried`)
- [x] wait for running ≤ 30 s; terminal state fails at once (`::test_wait_for_running_gives_up_at_30s`,
  `::test_terminal_state_while_starting_fails_at_once`)
- [x] restore from the recorded snapshot, fallback drops the record (`::test_restore_from_record_and_fallback`)
- [x] cleanup order sync-back → snapshot → stop → close (`::test_persistent_cleanup_order_is_sync_back_snapshot_stop`)
- [x] runtime names validated; runtime replaces the image (`::test_unknown_runtime_is_rejected_by_config_validation`,
  `::test_runtime_replaces_the_image`)
- [x] auth: whole trio or OIDC; partial trio named (`::test_auth_needs_the_whole_trio_or_an_oidc_token`)
- Deliberate differences: the running wait polls every 500 ms (phase) where the reference polls
  every 250 ms — needs a D-id or a phase fix (lead); the SDK's API changed (no `runtime` argument,
  `create_sandbox`/`create_process`/`fs.write_bytes`), so a runtime goes in the image slot
  (D-16.24 docs-vs-code item); credentials go through the SDK's credentials factory.

## Status

Done (2026-10-19). Shared: schema row `vercel_runtime` (+ test).

Integrated (lead, 2026-10-19): ports from `backends/sdk_ports.py` (records kind `vercel`), registry
row `vercel_sandbox` in `backends/sdk_entries.py`; tests on B1's fake Vercel SDK (which gained a
configurable workspace, a status script and a `create_process` that returns while the command runs).
