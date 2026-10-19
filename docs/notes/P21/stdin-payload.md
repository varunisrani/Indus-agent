# Task: Payload stdin staging (P21-03) Phase: 21 Owner: P21 slice B1 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/modal.py`
(`_stream_stdin`), `tools/environments/vercel_sandbox.py` (`_run_bash`), `tools/environments/daytona.py`
(`_run_bash`), `tools/environments/base.py` (stdin mode handling, `_staged_stdin_path`,
`_redirect_stdin_from_file`). Closed before writing. Evidence: none.

What it does (own words): a command's stdin (a file body written through `terminal`, a sudo
password, a heredoc-sized payload) must reach the sandboxed process byte for byte without ever
being put on a command line (argv is limited to about 128 KiB and readable by other users). Daytona
and Vercel have no stdin pipe for exec, so the payload is uploaded as a uniquely named file in the
sandbox temp dir through the backend's file API, its size is checked, and the script first points
its stdin at the file and deletes it, so the file is gone before user code runs. Modal has a stdin
stream: the payload is written in 64 KiB chunks, drained after each, then closed.

Inputs → outputs (API B2 builds on):
- `encode_stdin(data) -> bytes | StdinFailure` — UTF-8 with `surrogateescape` (same as the local
  backend); a failure is a value whose `.message` goes into `ExecResult.stdin_error`, never raised.
- `staged_stdin_path(temp_dir) -> str` = `<temp_dir>/.indus-stdin-<32 hex>`.
- `await stage_stdin(data, *, temp_dir, write, size_of, remove=None) -> StagedStdin`:
  `data` None or "" stages nothing (`StagedStdin(path=None)`); otherwise encode → `await write(path,
  raw)` → `await size_of(path)` must equal `len(raw)`; on an encode failure or a size mismatch the
  result has `error` set (the file is removed best-effort with `remove`) and no path. Errors from
  `write` itself propagate (they are backend failures, e.g. `BackendDeadline`).
- `StagedStdin(path, size, error)`; `.prefix()` → `exec 0<'<path>' || exit 1; rm -f '<path>'\n` ("" when
  nothing was staged; a missing file ends the script before the command runs); `with_stdin(script, staged) -> str` puts the prefix in
  front of the wrapped script.
- Modal: `stdin_chunks(raw, size=STREAM_CHUNK)` and `stream_stdin(writer, raw, *, chunk=STREAM_CHUNK)`
  where `writer` has `write(bytes)`, `drain()`, `write_eof()` (blocking, as the SDK's sync API); the
  last call is always `write_eof()` then `drain()`, also for an empty payload.

Files: `src/indus/backends/stdin_payload.py`, `tests/unit/backends/test_stdin_payload.py`.

Edge cases / failure modes:
- 1 MiB payloads and more: staged through the file API, never argv; chunk boundaries exactly at
  64 KiB multiples, a last partial chunk, an exact multiple (no empty trailing chunk).
- Lone surrogates that `surrogateescape` cannot carry → `stdin_error`, the command still runs with
  empty stdin.
- Size mismatch after upload (a truncated copy, R02.16) → `stdin_error` naming both sizes, the partial
  file removed.
- Cancel between upload and dispatch: the backend (B2) removes the staged file; once the script runs
  it deletes the file itself before the user command.
- Paths are shell-quoted; the name is random so two commands never share a file.

Our design differences / decisions:
- Staging is a backend-neutral helper taking async `write`/`size_of` callables, so Daytona and Vercel
  share it; the reference duplicated it per backend.
- 64 KiB stream chunks for Modal (the reference used 1 MiB).
- Size verification after every upload (R02.16); the reference trusted the upload.

Tests we will write:
- 1 MiB payload staged, byte count verified, order: write → size check → (command) with the prefix
  removing the file before the command (fake sandbox records the order; prefix run through real bash)
- chunking at 64 KiB boundaries; empty payload sends only EOF
- non-UTF-8 payload → `stdin_error`, nothing staged
- empty / None stdin stages nothing
- size mismatch → error and the file removed

## Decisions for the lead
- An empty string stdin stages nothing (the command sees an empty stdin either way).

Compare checklist (filled after the rewrite; reopened `tools/environments/base.py`,
`tools/environments/modal.py`):
- [x] Staged file is opened and unlinked before the command (test:
  `test_stdin_payload.py::test_prefix_removes_file_before_command`)
- [x] Stream writes then EOF, drained (test: `::test_chunks_at_64k_boundaries`)
- [x] Empty stdin means no stdin (test: `::test_empty_stdin_stages_nothing`)
- [x] A staged file that is missing stops the script before the command (test:
  `::test_missing_staged_file_stops_the_script`)
- Deliberate differences: byte-count verification after upload; 64 KiB chunks (the reference used
  1 MiB); one shared helper for both pipe-less backends.
- Not taken: the reference's in-file chmod step (our file is created 0600 by the backend's write,
  and lives only until the script starts).

## Status

Done (2026-10-19).
