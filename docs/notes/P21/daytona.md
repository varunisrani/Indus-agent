# Task: Daytona backend (P21-07) Phase: 21 Owner: P21 builder B2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/daytona.py`
(all). Vendor surface: daytona 0.220.0 (Apache-2.0): `Daytona(DaytonaConfig(api_key=…))`,
`get(name)`, `create(CreateSandboxFromImageParams(image, name, auto_stop_interval, resources=
Resources(cpu, memory, disk)), timeout=…)`, `delete(sandbox)`; sandbox `state`, `refresh_data()`,
`start(timeout)`, `stop(timeout)`, `process.exec(command, cwd, env, timeout) -> (exit_code,
result)`, `fs.upload_file(bytes|path, remote)`, `fs.download_file(remote, local)`,
`fs.get_file_info(path).size`, `fs.delete_file(path)`; `DaytonaNotFoundError`. Closed before writing.
Evidence: none.

What it does (own words): `terminal.backend: daytona` runs commands in a Daytona sandbox named
`indus-<task key>`. Start: look the name up; a stopped (or archived) one is started, a running one is
used, none is created with auto-stop off. Non-persistent profiles never reuse: a leftover sandbox of
that name is deleted first. Resources: CPU from `container_cpu`, memory GiB = ceil(MB/1024) (min 1),
disk GiB = ceil(MB/1024) clamped to 1..10 with one warning when cut. The home is read with
`echo $HOME` (10 s); a cwd of `~` or the vendor default follows it. Before each command the sandbox
state is refreshed and a stopped sandbox is started again. Commands: the shared shell flow over
`process.exec` with the shell timeout guard kept; stdin is staged as a file (P21-03); cancel signals
the process group via a second exec. Cleanup: sync back (P21-10), then stop (persistent: files
kept) or delete. File transport: one file = mkdir + upload + size check; many = upload the tar to
the sandbox temp dir, `tar -xf` it there, remove it; download = `tar cf` to a temp file, download,
remove; delete = `rm -f`.

Files: `src/indus/backends/daytona.py`, `tests/unit/backends/test_daytona.py`,
`src/indus/foundation/config/schema.py` (shared: `daytona_image`).

Edge cases: API key missing → requirement reason; name lookup fails with anything but not-found →
`EnvironmentConnectionError`; home detection fails → `/root` kept; a sandbox stopped behind our
back (auto-stop elsewhere) is started before the next command.

Our design differences / decisions:
- no legacy label lookup (D-16.3); the key comes from the secret store or `DAYTONA_API_KEY` and is
  handed to the SDK config object (never env/argv).
- cancel stops the process group instead of stopping the whole sandbox.
- image: `terminal.daytona_image`, else `terminal.docker_image`.

Tests: stopped sandbox started not recreated; resource table (512 MB → 1, 5120 → 5, disk 51200 →
10 + one warning); persistent stop vs delete; home drives cwd; guard in the exec command string;
stdin staged and removed before the command; transport round trip.

## Decisions for the lead
- `terminal.daytona_image` row added (empty = docker_image).

Compare checklist (filled after the rewrite; reopened `tools/environments/daytona.py`):
- [x] get-by-name, start when stopped/archived, else create with auto-stop off
  (`tests/unit/backends/test_daytona.py::test_existing_stopped_sandbox_is_started_not_recreated`,
  `::test_missing_sandbox_is_created_with_auto_stop_off`)
- [x] resource math and the 10 GiB disk cap with one warning (`::test_resource_math`, `::test_disk_clamp_warns_once`)
- [x] home via `echo $HOME`, cwd follows for `~` and the vendor default (`::test_home_detection_drives_the_cwd`)
- [x] stopped sandbox restarted before a command (`::test_sandbox_stopped_elsewhere_is_started_before_the_next_command`)
- [x] shell timeout guard kept (`::test_exec_keeps_the_shell_timeout_guard`)
- [x] staged stdin removed before user code, and scrubbed when the command never ran
  (`::test_stdin_is_staged_and_removed_before_the_command`, `::test_staged_stdin_is_scrubbed_when_the_command_never_ran`)
- [x] cleanup: sync back, then stop (persistent) or delete (`::test_transport_round_trip_and_sync_back`,
  `::test_persistent_cleanup_stops_and_keeps_the_sandbox`, `::test_non_persistent_cleanup_deletes_and_leftovers_are_not_reused`)
- Deliberate differences: no label lookup (D-16.3); interrupt kills the process group, not the
  sandbox; a non-persistent profile deletes a leftover sandbox of its name instead of reusing it;
  CPU is whole CPUs, at least 1.

## Status

Done (2026-10-19).

Integrated (lead, 2026-10-19): ports from `backends/sdk_ports.py`, registry row `daytona` in
`backends/sdk_entries.py`; tests on B1's fake Daytona SDK (which gained the SDK's
`download_file(remote, local)` form). Forwarded variables go through `process.exec(env=…)` (P21-11).
