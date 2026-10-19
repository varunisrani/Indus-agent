# Task: Modal direct, exec and file transfer (P21-06) Phase: 21 Owner: P21 builder B2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/modal.py`
(`_exec`, `_modal_upload`, `_modal_bulk_upload`, `_modal_bulk_download`, `_modal_delete`,
`_run_bash`). Vendor surface: modal 1.6.1 (`Sandbox.exec(*args, timeout, workdir, env, text)`
returns a process with `stdout` (iterable reader), `stdin.write/drain/write_eof`, `wait()`,
`poll()`, `returncode`; no per-process kill). Closed before writing.
Evidence: none.

What it does (own words): commands run through the shared spawn-per-command flow (snapshot, cwd
marker, capture, backstop) with the SDK's exec in place of a local process. Every script runs as
`bash -c "timeout -k 2 <N> bash -c <script>"` (shell guard) and first writes its shell pid to a file
in the sandbox temp dir and folds stderr into stdout. stdout is streamed while the command runs.
Stdin is streamed in 64 KiB chunks then closed; an encode failure becomes `stdin_error` and the
command gets no stdin. Interrupt: the process group is signalled through a second exec (TERM, then
KILL), the result is rc 130 + `interrupted`. Deadline: rc 124 + `timed_out` with the output so far;
the vendor call itself is bounded at timeout + 10 s by the wrapper. The init snapshot gets 60 s.
File transport (P11 `SyncTransport`): one file = base64 on stdin into `base64 -d` (30 s) with the
written byte count read back and compared; many = a tar on stdin into `tar -xf -` (120 s);
download = `tar cf - <app dir>` read in full (120 s, unbounded, R02.2); delete = `rm -f` (15 s).

Inputs → outputs: command/timeout/cwd/stdin → `ExecResult` (same fields as local).

Files: `src/indus/backends/modal/exec.py`, `tests/unit/backends/modal/test_exec.py`.

Edge cases / failure modes:
- vendor exec raises (link lost) → `EnvironmentConnectionError` (registry answers degraded).
- the exec never returns → our wait loop stops at the timeout (rc 124), the kill is sent once.
- output with no trailing newline before the marker; binary bytes decoded with replacement.
- 20 MiB download: written to the local tar file as it streams, never truncated.

Our design differences / decisions:
- interrupt kills the command's process group, not the whole sandbox (the reference terminates the
  sandbox, which loses its state); the shell guard still stops a command the kill missed.
- bulk upload sends the raw tar bytes on stdin (no base64/gzip layer).

Tests we will write (fake Modal SDK running local bash in a tmp root; scripted fakes on a fake
clock): commands + cwd persistence; interrupt → rc 130 and a kill exec; never-returning exec → rc
124 at the deadline; 20 MiB download intact; upload byte count checked (mismatch → error); op
deadlines 30/120/120/15 s passed to the wrapper; stdin streamed in 64 KiB chunks.

## Decisions for the lead
- kill via pid file + second exec (Modal has no per-process kill at 1.6.1).

Compare checklist (filled after the rewrite; reopened `tools/environments/modal.py`):
- [x] exec through the SDK with stdin streamed and closed, output collected
  (`tests/unit/backends/modal/test_exec.py::test_commands_keep_cwd_and_exports`,
  `::test_stdin_is_streamed_in_64k_chunks`)
- [x] interrupt → rc 130 (`::test_interrupt_signals_the_command_and_returns_130`)
- [x] deadline → rc 124 even when the vendor call never returns (`::test_never_returning_exec_gives_124_at_the_deadline`)
- [x] single upload by base64 on stdin, bulk by tar on stdin, download `tar cf -` with `*.sock`
  excluded, delete `rm -f`; 30/120/120/15 s (`::test_transport_round_trip_and_limits`,
  `::test_twenty_mib_download_is_not_truncated`, `::test_hung_bulk_upload_gives_up_at_120s`)
- [x] byte count of a single upload verified (`::test_upload_byte_count_mismatch_is_an_error`)
- Deliberate differences: interrupt signals the command's process group (pid file + second exec)
  instead of terminating the whole sandbox; stdin chunks are 64 KiB (phase) where the reference
  used 1 MiB; bulk upload sends the raw tar (no gzip + base64 layer); the exec also gets a
  vendor-side timeout just past the shell guard.

## Status

Done (2026-10-19). Shared base `src/indus/backends/sdk_shell.py` (new) carries the exec/stop/stdin
flow for all three SDK backends.

Integrated (lead, 2026-10-19): tests on B1's fake Modal SDK (its stdout reader is iterable now, as
the SDK's is); forwarded variables go through `Sandbox.exec(env=…)` (P21-11).
