# Task: Vercel Sandbox, exec and files (P21-09) Phase: 21 Owner: P21 builder B2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/vercel_sandbox.py`
(`_run_checked`, `_vercel_bulk_upload`, `_vercel_delete`, `_vercel_bulk_download`, `_run_bash`).
Closed before writing.
Evidence: none.

What it does (own words): the workspace root is `/vercel/sandbox` (or the sandbox's reported cwd
when absolute). Commands go through the shared shell flow with `create_process("bash", ["-c",
<guarded script>])`; stdin is staged as a file first (P21-03); interrupt kills the process
(vendor kill) and signals its group. File writes (stdin staging, uploads) are retried 3× with a
100 ms step (100 ms, then 200 ms). Transport: one file = a retried write; many = the tar written to
the sandbox temp dir and unpacked there; download = `tar cf` into a temp file, read back in full
(unbounded); delete = `rm -f`. Background commands use P11's remote flow; a sandbox that was
replaced bumps `recreations`, so jobs started before are reported lost (no recovery of detached
processes after a stop or restart).

Files: `src/indus/backends/vercel/exec.py`, `tests/unit/backends/vercel/test_exec.py`.

Tests: write fails twice then succeeds (sleeps 0.1, 0.2 on a fake clock); download unbounded;
background job after a sandbox replacement reported lost; cwd marker read from output with no
trailing newline; stdin staged then removed before the command.

Compare checklist (filled after the rewrite; reopened `tools/environments/vercel_sandbox.py`):
- [x] writes retried 3× with a 100 ms step (`tests/unit/backends/vercel/test_exec.py::test_write_fails_twice_then_succeeds_with_100ms_steps`)
- [x] download in full, temp tar removed (`::test_download_is_unbounded`)
- [x] staged stdin, removed before user code (`::test_stdin_is_staged_then_removed`)
- [x] interrupt kills the vendor process (`::test_interrupt_kills_the_vendor_process`)
- [x] replaced sandbox: jobs of the old one are lost, the warning shows once (`::test_replaced_sandbox_loses_background_jobs`)
- [x] marker read from output without a trailing newline (`::test_commands_start_in_the_workspace_and_parse_the_marker`)
- Deliberate differences: bulk upload goes as one tar unpacked in the sandbox (the reference wrote
  each file through the API); single uploads are size-checked.

## Status

Done (2026-10-19).

Integrated (lead, 2026-10-19): tests on B1's fake Vercel SDK; forwarded variables go through
`create_process(env=…)` (P21-11).
