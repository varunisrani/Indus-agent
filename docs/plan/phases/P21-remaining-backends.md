# P21 · Remaining backends

**Weeks:** 33–35 (1.5 weeks, beside P19–P20) · **Track:** B · **Depends on:** P11 (and through it P05) · **Status:** planned

## Goal
A user can set `terminal.backend` to `singularity`, `modal`, `daytona` or `vercel_sandbox` and the
agent's `terminal`, file tools and background processes work there exactly as they do on Docker:
same execution contract (snapshot, cwd marker, timeout backstop, interrupt), same result shape,
same approval behaviour for isolated sandboxes. Persistent sandboxes keep their filesystem across
sessions through each vendor's snapshot or stop/start mechanism, and files the agent changed in the
sandbox's app dir come back to the host only under the sync-back rules (R02.31). Every vendor SDK
call is wrapped in our own deadline, so a vendor that ignores its own timeout can no longer hang a
tool call (R02.30). The vendor-hosted managed Modal gateway is not built (D-16.16): Modal runs only
in direct mode with the user's own credentials. The phase proves the P05 contract really is
backend-neutral: the shared contract suite runs green over all seven backends.

Interop note: the Modal Python SDK, the Daytona Python SDK, the Vercel Sandbox Python SDK and the
Apptainer/Singularity command line are third-party interfaces. Check every class, method, argument
and status name against the vendor's own docs and type stubs for the version we pin, not against
the reference. Pin each SDK exactly per D-16.52 (a release at least 14 days old at lock time, recorded
in `docs/notes/P21/sdk-versions.md` with its licence). For orientation only, the reference pinned
modal 1.5.5, daytona 0.155.0 and vercel 0.7.2 (licence of the last one unknown: confirm before it
enters the allow-list). Apptainer ≥ 1.2 and SingularityCE ≥ 4.0 are the CLI versions we test
against; record the exact flags used per version in the note. Read the reference's code only for
behaviour and edge cases. Each SDK lives in its own extra (`modal`, `daytona`, `vercel`), imported
lazily inside the backend module, never at package import.

## Tasks (16)
| ID | Task | Read in reference | New files | Verify |
|---|---|---|---|---|
| P21-01 | **Sandbox records store.** One small JSON store per backend kind at `<profile home>/sandboxes/records/<kind>.json` mapping a namespaced key `<profile id>:<task key>` to `{snapshot_id?, overlay_path?, sandbox_name?, created_at, updated_at}`. Reads and writes go through one function that takes an exclusive file lock (`<file>.lock`, 10 s acquire deadline, then a typed `SandboxRecordBusy` error), re-reads inside the lock, applies a caller mutation and writes via temp file + fsync + atomic rename with mode 0600 in a 0700 dir. Keys are always profile-prefixed (R02.8); a record written under profile A is invisible to profile B. A corrupt file is moved aside to `<kind>.json.corrupt-<UTC stamp>` and treated as empty with one warning. No legacy-key migration (D-16.3). Scope is passed in; nothing reads the home at import. | `tools/environments/modal.py` [282: `_snapshot_store`, `_load_snapshots`, `_update_snapshots`, `_store_direct_snapshot`, `_delete_direct_snapshot`], `tools/environments/singularity.py` [215: `_snapshot_store`, `_update_snapshots`] | `src/indus/backends/sandbox_records.py`, `tests/unit/backends/test_sandbox_records.py` | Round-trip; two processes mutating the same file 200× each lose no update (multiprocessing test); profile isolation; corrupt file quarantined; lock timeout raises `SandboxRecordBusy` with an injected clock; file mode 0600 (POSIX-only assertion under the layout's host-marker rule; the OS marker itself lands in P23-01). |
| P21-02 | **SDK call wrapper.** One helper every SDK backend uses for every vendor call: runs the blocking SDK call on the bounded thread pool helper from P04 with a client-side deadline (`deadline = command timeout + 10 s` for exec; per-operation constants for create 300 s, snapshot 60 s, terminate/stop 15 s, file ops 30 s single / 120 s bulk); on deadline the call is abandoned (never joined), a vendor cancel/terminate is fired once best-effort, and a typed `BackendDeadline` is raised (R02.30). Transient classification: HTTP 408, 425, 429, 5xx and connection resets are transient; retries up to 3 attempts with jittered backoff (base 0.5 s, factor 2, cap 4 s) only for idempotent operations (create-by-name, get, stop, snapshot read), never for exec. Also builds the shell-level guard: every exec command string is wrapped in `timeout -k 2 <N> bash -c …` inside the sandbox so a vendor that ignores its own timeout still stops the process. Clock and sleeper injected. | `tools/environments/vercel_sandbox.py` [421: `_exception_chain`, `_extract_status_code`, `_is_transient_vercel_error`, `_retry_vercel_call`], `tools/environments/modal.py` [282: `_AsyncWorker`], `tools/environments/daytona.py` [207: `_run_bash`] | `src/indus/backends/sdk_call.py`, `tests/unit/backends/test_sdk_call.py` | Fake SDK that never returns: wrapper returns `BackendDeadline` at timeout + 10 s on a fake clock and fires cancel once; 503 twice then success retries (idempotent op); exec never retried; 400 not retried; exception chains inspected for status codes; generated wrapper string quotes a command containing `'`, `$()`, newlines. |
| P21-03 | **Payload stdin staging.** For backends without a stdin pipe (Daytona, Vercel) stage stdin as a uniquely named file under the sandbox temp dir (written through the backend's file API, verified by byte count, not argv: payloads can exceed the 128 KiB argv limit), then the contract wrapper prefixes `exec 0<"<file>"; rm -f "<file>";` so the file is gone before user code runs. For Modal, stream stdin in 64 KiB chunks then close. An encode failure surfaces as `stdin_error` in `ExecResult`, never an exception. | `tools/environments/modal.py` [282: `_stream_stdin`], `tools/environments/vercel_sandbox.py` [421: `_run_bash`], `tools/environments/base.py` [804: stdin mode handling] | `src/indus/backends/stdin_payload.py`, `tests/unit/backends/test_stdin_payload.py` | 1 MiB payload staged and byte-count verified; file removed before command (fake sandbox records order); chunking at 64 KiB boundaries; non-UTF-8 payload → `stdin_error`; empty stdin stages nothing. |
| P21-04 | **Singularity/Apptainer backend.** Executable discovery via the P01 command-lookup tables: `apptainer` first, then `singularity`; absent → requirement reason "Apptainer or Singularity not found". Image from `terminal.singularity_image` or `docker://<container image>`; SIF cached under `<scratch>/sif-cache/<sha256(image)[:16]>.sif`, built with `build --force` at most once per image under a file lock, 600 s limit. Scratch dir: `terminal.scratch_dir`, else `<home>/sandboxes/singularity`; warn once when its usage exceeds `terminal.disk_warning_gb` (default 500). Start `instance start --containall --no-home` with CPU/memory flags when cgroups allow (120 s), instance name `indus_<12 hex>`; persistent → overlay dir per task under scratch (path recorded in P21-01 store), else `--writable-tmpfs`; credential files and skills dirs bound read-only. Exec `exec instance://<name> bash -c …` with stdin pipe; prompts skipped (isolated, no host-path binds) unless a host path is bound (R02.6). Cleanup `instance stop` (30 s), record overlay path. | `tools/environments/singularity.py` [215: all] | `src/indus/backends/singularity.py`, `tests/unit/backends/test_singularity.py` | Fake `apptainer` script on PATH records argv: start flags, read-only binds, overlay vs tmpfs by persistence, `singularity` fallback, SIF built once for two tasks with the same image, build timeout → typed error with partial stderr ≤ 500 chars, stop on cleanup, disk-warning once. |
| P21-05 | **Modal direct: create and lifecycle.** Config `terminal.modal_mode` accepts `auto` or `direct` (both mean direct); `managed` is rejected at config validation with a message that the managed gateway is not offered (D-16.16). Credentials: the user's Modal token pair from `SecretStore` or the vendor-standard env names (D-16.22); missing → requirement reason, never a crash. Create a sandbox with timeout 3600 s (create deadline 300 s) from the restore candidate in the P21-01 store if one exists; restore failure → delete that record, create from the base image, mark `environment_recreated` (R02.19). Image resolution: our sandbox image (D-16.64) unless configured; CPU/memory from container defaults (1 CPU, 5120 MB). Cleanup: when persistent take a filesystem snapshot (≤ 60 s) and store its id; then terminate (≤ 15 s). | `tools/environments/modal.py` [282: `ModalEnvironment.__init__`, `_get_snapshot_restore_candidate`, `_resolve_modal_image`, `cleanup`], `tools/terminal_tool_backends.py` [373: `_get_modal_backend_state`, `_modal_unavailable_reason`, `_build_modal_env`] | `src/indus/backends/modal/lifecycle.py`, `tests/unit/backends/modal/test_lifecycle.py` | Fake Modal SDK: restore path used when a record exists; bad snapshot → record deleted, base image used, recreated flag set; persistent cleanup snapshots then terminates; non-persistent only terminates; `managed` rejected by config validation; missing token → probe reason. |
| P21-06 | **Modal direct: exec and file transfer.** Exec through the SDK on the P21-02 wrapper; stdin streamed (P21-03); cancel on interrupt = terminate the process, return rc 130 + interrupted flag; deadline → partial output, rc 124 + `timed_out`. Snapshot of env at init within 60 s (Modal's longer budget). File ops: single upload via base64 on stdin (30 s), bulk upload as tar on stdin (120 s), bulk download `tar cf - <app dir>` (120 s, unbounded capture: R02.2), deletes (15 s). These ops implement the P11 remote-sync transport interface. | `tools/environments/modal.py` [282: `_exec`, `_modal_upload`, `_modal_bulk_upload`, `_modal_bulk_download`, `_modal_delete`, `_run_bash`] | `src/indus/backends/modal/exec.py`, `tests/unit/backends/modal/test_exec.py` | Fake SDK: interrupt mid-command terminates and returns rc 130; never-returning exec returns rc 124 at deadline; 20 MiB download not truncated; upload byte counts verified; bulk ops respect their limits on a fake clock. |
| P21-07 | **Daytona backend.** Sandbox name `indus-<task key>`; get-by-name then start if stopped, else create with auto-stop disabled (no legacy label lookup, D-16.3). Resources: CPU from config, memory GiB = ceil(MB / 1024) min 1, disk GiB min 1 max 10 (a larger request is clamped with one warning). Home detected with `echo $HOME` (10 s); default cwd follows home. Exec via the SDK process API on the P21-02 wrapper with the shell timeout guard kept; cancel = stop the process; stdin staged (P21-03). Cleanup: sync back (P21-10), then stop when persistent (filesystem kept) or delete when not. File ops implement the remote-sync transport. | `tools/environments/daytona.py` [207: all] | `src/indus/backends/daytona.py`, `tests/unit/backends/test_daytona.py` | Fake Daytona SDK: existing stopped sandbox is started not recreated; resource math table (512 MB → 1 GiB, 5120 → 5, disk 51200 MB → clamped 10); persistent stop vs delete; home detection drives cwd; exec timeout guard present in the command string. |
| P21-08 | **Vercel Sandbox: create and lifecycle.** Auth: access token + project id + team id, or a short-lived OIDC token when present (vendor-standard env names, D-16.22); missing → requirement reason. `container_disk` must be the default (51200 MB) or 0, else refuse before any API call with a clear message. A configured `vercel_runtime` (only the vendor's documented runtime names) overrides the image; default image is the vendor's universal image. Create from the stored snapshot if any, else image; create retried 3× on transient errors (P21-02); wait for running ≤ 30 s polling every 500 ms; sandbox timeout ≥ 5 min. Cleanup: sync back (P21-10), snapshot when persistent (stored in P21-01; snapshots hold no live processes), stop ≤ 15 s polling 500 ms. | `tools/environments/vercel_sandbox.py` [421: `_create_sandbox`, `_attach_fresh_sandbox`, `_wait_for_running`, `_snapshot_sandbox`, `_stop_sandbox`, `_ensure_sandbox_ready`, `cleanup`], `tools/terminal_tool_backends.py` [373: `_check_vercel`, `_is_supported_vercel_runtime`] | `src/indus/backends/vercel/lifecycle.py`, `tests/unit/backends/vercel/test_lifecycle.py` | Fake Vercel SDK: bad disk value refused with zero API calls; 429 then success on create; wait-running gives up at 30 s on a fake clock; persistent cleanup order sync-back → snapshot → stop; unknown runtime rejected by config validation. |
| P21-09 | **Vercel Sandbox: exec and files.** Workspace root `/vercel/sandbox`; exec via SDK with staged stdin (P21-03) and shell timeout guard; file writes retried 3× with a 100 ms backoff step; bulk upload/download/delete implement the remote-sync transport. Background commands use the generic non-local background flow from P11; after a sandbox stop or process restart there is no recovery of detached processes, and the process tool reports them as lost with a one-line reason. | `tools/environments/vercel_sandbox.py` [421: `_run_checked`, `_vercel_bulk_upload`, `_vercel_delete`, `_vercel_bulk_download`, `_run_bash`] | `src/indus/backends/vercel/exec.py`, `tests/unit/backends/vercel/test_exec.py` | Fake SDK: write fails twice then succeeds (100/200 ms on fake clock); download unbounded; background process after restart reported lost; cwd marker parsed from output with no trailing newline. |
| P21-10 | **Sync-back (R02.31).** On cleanup of Modal, Daytona and Vercel sandboxes: skip entirely when nothing was ever pushed; download the remote app dir as a tar through the backend transport into a private temp dir under `<home>/cache/sync-back/`; refuse when the archive exceeds `terminal.sync_back_max_bytes` (default 2 GiB); apply only files whose SHA-256 differs from what this session pushed, never credential files (upload-only), never paths outside the mapped dirs (reject `..`, absolute and symlink members); 3 attempts with 2/4/8 s backoff; SIGINT deferred until the apply finishes; serialised across processes with a file lock in the profile home; temp entries older than 30 min whose owner PID is dead are removed at start. | `tools/environments/file_sync.py` [500: `FileSyncManager.sync_back` through `_apply_staged_file`, `_cleanup_stale_sync_back_temp`, `_credential_host_paths`] | `src/indus/backends/sync_back.py`, `tests/unit/backends/test_sync_back.py` | Archive with a changed file, an unchanged file, a credential file and a `../escape` member → only the changed file written; 2 GiB + 1 refused; two concurrent sync-backs serialise (lock); SIGINT during apply finishes the apply then raises; stale temp from a dead PID removed, live one kept; no-push session does nothing. |
| P21-11 | **Vendor credentials and env hygiene.** Declare each backend's vendor token names (Modal token pair, Daytona API key, Vercel token and OIDC token) to the P05 child-env builder so they are stripped from every child process, MCP server and sandbox env (R02.22). Forwarded env (`terminal.env_passthrough`) reaches SDK sandboxes only through the SDK's env parameter, never in command text (R02.7). Per-session bridged variables are unset in the snapshot step for these backends exactly as for local (R02.1). | `tools/environments/remote_common.py` [99: `resolve_passthrough_env`, `prepend_unset`, `client_env_with`], `tools/environments/base_session_env.py` [190: `_export_dump_excluding_session_vars`] | `src/indus/backends/vendor_env.py`, `tests/unit/backends/test_vendor_env.py` | Env builder output for a local child has no vendor token; fake SDK receives passthrough values in its env argument and the command text contains none of the values; snapshot script unsets session vars (golden script). |
| P21-12 | **Registration and requirement probes.** Register the four kinds in the P05/P11 backend registry with their flags: isolated (skip dangerous-command prompts only when no host path is mounted, R02.6), container paths, remote sync on/off, stdin mode, persistence default true. Probes never touch the network: SDK importable, credentials present, CLI found; each failure records a human reason shown by `doctor` and at startup. Flapping guard: a probe that succeeded within the last 120 s serves last-good on one failure, but two consecutive failures 30 s apart mark it unavailable (R02.13). A probe run before any scope exists logs quietly and re-probes on the first scoped turn (R02.29). Missing extra → reason names the extra (`indus` was installed without `modal`), never an ImportError. | `tools/terminal_tool_backends.py` [373: `_create_environment`, `_check_requirements`, `_modal_pre`, `_daytona_post`, `terminal_backend_unavailable_reason`] | `src/indus/backends/remote_probes.py`, `tests/unit/backends/test_remote_probes.py` | Table test of probe outcomes per backend; flapping grace on a fake clock; unscoped probe does not cache "unavailable"; missing SDK reports the extra name; registry lists seven built-ins. |
| P21-13 | **Doctor and setup entries.** `indus doctor` gains one check per backend (binary/SDK, credentials present, scratch dir writable, for Singularity the scratch usage warning); `indus setup` terminal step lists the four backends with one-line descriptions (our words) and asks only for the fields each needs, writing secrets to `SecretStore` (P4). Non-TTY: `setup --non-interactive` accepts `--terminal-backend` plus env-supplied credentials. | `hermes_cli/doctor_tools.py` [`_check_daytona_backend`, `_check_vercel_backend`, `_check_terminal_backend`], `hermes_cli/setup_terminal.py` [328: backend picker] | `src/indus/cli/doctor/remote_backends.py`, `src/indus/cli/setup/remote_backends.py`, `tests/unit/cli/doctor/test_remote_backends.py`, `tests/unit/cli/setup/test_remote_backends.py` | CliRunner: doctor output per state (ok, missing extra, missing token); setup writes the token to a fake keychain, not `.env`; non-interactive setup with env credentials. |
| P21-14 | **Per-task overrides.** A small registry, keyed by task id under the current scope, holding image / backend / cwd overrides for one task (used by the P22 batch runner and by delegation): same image name for Docker, Modal and Daytona, `docker://<image>` for Singularity, ignored for Vercel with one warning. Overrides produce their own env key (02 §3.8 rule 1) so they never reuse the default sandbox. | `batch_runner.py` [1020: `_prepare_container_image`], `tools/terminal_tool.py` [1653: `_resolve_container_task_id`, `_get_env_config`] | `src/indus/backends/task_overrides.py`, `tests/unit/backends/test_task_overrides.py` | Override mapping table per backend; env key differs from default; cleared after task end; profile scoping. |
| P21-15 | **Fake SDKs and fake CLI.** Deterministic in-process fakes for the three SDKs (same method surface we call, recorded call log, scriptable latency on an injected clock, scriptable errors by status code, a toy filesystem with tar import/export) and a fake `apptainer` script that records argv and simulates instance state. Used by every task above and by the contract suite. | — (our own fakes; surface from the vendor docs at the pinned version) | `tests/fakes/backend/fake_modal.py`, `tests/fakes/backend/fake_daytona.py`, `tests/fakes/backend/fake_vercel.py`, `tests/fakes/backend/fake_apptainer.py`, `tests/unit/fakes/test_fake_sdks.py` | Self-tests: each fake's method names match a checked-in list taken from the pinned SDK's public API (`tests/fakes/backend/sdk_surfaces.json`, regenerated by a script when the pin changes); latency and errors honour the fake clock. |
| P21-16 | **Contract suite over seven backends + live lane.** Extend the shared execution-contract suite from P05/P11 with a parameter per new backend (using the fakes): snapshot and cwd marker, odd output (no trailing newline, binary noise), timeout backstop rc 124 + flag, interrupt rc 130, stdin, bounded vs unbounded capture, `environment_recreated` warning, background process start/poll/kill, file read/write/patch through the backend. Add a `network`-marked integration module per vendor (skipped without credentials) that runs "echo, write file, read back, persist across recreate" against the real service. | `tests/tools/` [reference docker/remote backend tests: edge-case lists only] | `tests/contract/backends/test_sdk_backends_contract.py`, `tests/integration/backends/test_remote_backends_live.py` | `uv run pytest tests/contract/backends` green for local, docker, ssh, singularity, modal, daytona, vercel_sandbox; live module skips cleanly with no credentials. |

## Files to create (40)
| Path | What it holds | Task |
|---|---|---|
| `src/indus/backends/sandbox_records.py` | per-kind locked JSON records of snapshots/overlays | P21-01 |
| `tests/unit/backends/test_sandbox_records.py` | round trip, concurrency, quarantine | P21-01 |
| `src/indus/backends/sdk_call.py` | deadline + retry wrapper, shell timeout guard | P21-02 |
| `tests/unit/backends/test_sdk_call.py` | deadline, retry classes | P21-02 |
| `src/indus/backends/stdin_payload.py` | staged and streamed stdin | P21-03 |
| `tests/unit/backends/test_stdin_payload.py` | staging order, chunking | P21-03 |
| `src/indus/backends/singularity.py` | Apptainer/Singularity backend | P21-04 |
| `tests/unit/backends/test_singularity.py` | argv and lifecycle cases | P21-04 |
| `src/indus/backends/modal/__init__.py` | package marker, lazy import guard | P21-05 |
| `src/indus/backends/modal/lifecycle.py` | create, restore, snapshot, terminate | P21-05 |
| `tests/unit/backends/modal/test_lifecycle.py` | restore fallback, managed rejected | P21-05 |
| `src/indus/backends/modal/exec.py` | exec, interrupt, file transport | P21-06 |
| `tests/unit/backends/modal/test_exec.py` | rc 124/130, transfer limits | P21-06 |
| `src/indus/backends/daytona.py` | Daytona backend | P21-07 |
| `tests/unit/backends/test_daytona.py` | resources, stop vs delete | P21-07 |
| `src/indus/backends/vercel/__init__.py` | package marker, lazy import guard | P21-08 |
| `src/indus/backends/vercel/lifecycle.py` | create, wait, snapshot, stop | P21-08 |
| `tests/unit/backends/vercel/test_lifecycle.py` | disk refusal, retries, order | P21-08 |
| `src/indus/backends/vercel/exec.py` | exec, file transport, write retries | P21-09 |
| `tests/unit/backends/vercel/test_exec.py` | retries, lost background | P21-09 |
| `src/indus/backends/sync_back.py` | hash-diff sync-back with lock | P21-10 |
| `tests/unit/backends/test_sync_back.py` | R02.31 cases | P21-10 |
| `src/indus/backends/vendor_env.py` | vendor token declarations, env passing | P21-11 |
| `tests/unit/backends/test_vendor_env.py` | no token leaks, no values in argv | P21-11 |
| `src/indus/backends/remote_probes.py` | probes, flags, flapping guard | P21-12 |
| `tests/unit/backends/test_remote_probes.py` | probe table, grace | P21-12 |
| `src/indus/cli/doctor/remote_backends.py` | doctor checks | P21-13 |
| `src/indus/cli/setup/remote_backends.py` | setup picker step | P21-13 |
| `tests/unit/cli/doctor/test_remote_backends.py` | doctor states | P21-13 |
| `tests/unit/cli/setup/test_remote_backends.py` | setup writes to keychain | P21-13 |
| `src/indus/backends/task_overrides.py` | per-task image/backend/cwd overrides | P21-14 |
| `tests/unit/backends/test_task_overrides.py` | mapping and env keys | P21-14 |
| `tests/fakes/backend/fake_modal.py`, `fake_daytona.py`, `fake_vercel.py`, `fake_apptainer.py` | fakes (four files) | P21-15 |
| `tests/fakes/backend/sdk_surfaces.json`, `tests/unit/fakes/test_fake_sdks.py` | pinned API surface list, fake self-tests | P21-15 |
| `tests/contract/backends/test_sdk_backends_contract.py` | contract suite parameters for four backends | P21-16 |
| `tests/integration/backends/test_remote_backends_live.py` | live lane per vendor | P21-16 |

## Files to update (7)
| Path | Change | Task |
|---|---|---|
| `src/indus/backends/registry.py` (P05/P11) | register `singularity`, `modal`, `daytona`, `vercel_sandbox` factories and flags | P21-12 |
| `src/indus/backends/remote_sync.py` (P11) | accept the SDK file transports from P21-06/07/09; call `sync_back` on cleanup | P21-10 |
| `src/indus/safety/child_env.py` (P05) | consume vendor token declarations from `vendor_env` | P21-11 |
| `src/indus/foundation/config/schema.py` (P01) | terminal keys: `singularity_image`, `scratch_dir`, `disk_warning_gb`, `modal_mode` (`auto`/`direct`), `vercel_runtime`, `sync_back_max_bytes`, container cpu/memory/disk/persistent | P21-05, P21-08, P21-10 |
| `pyproject.toml` | extras `modal`, `daytona`, `vercel` with exact pins; add to `all` | P21-15 |
| `tests/contract/backends/conftest.py` (P05) | backend parameter list grows to seven | P21-16 |
| `docs/internals/traps-ledger.md` | swap R02.30, R02.31 and the other rows below from `Review:` to test references | P21-16 |

## Fixtures and fakes
| Path | What it holds | Task |
|---|---|---|
| `tests/fakes/backend/fake_*.py` | the SDK and CLI fakes above | P21-15 |
| `tests/fixtures/backends/sync_back/*.tar` | archives with changed, unchanged, credential, escaping and symlink members (generated by a script in the test, not committed binaries) | P21-10 |
| `tests/golden/backends/snapshot_script.sh` | expected snapshot bootstrap for SDK backends | P21-11 |

## Task notes (17)
`docs/notes/P21/<slug>.md`, written before the code:
- `sandbox-records.md` (P21-01) · `sdk-call.md` (P21-02) · `stdin-payload.md` (P21-03) ·
  `singularity.md` (P21-04) · `modal-lifecycle.md` (P21-05) · `modal-exec.md` (P21-06) ·
  `daytona.md` (P21-07) · `vercel-lifecycle.md` (P21-08) · `vercel-exec.md` (P21-09) ·
  `sync-back.md` (P21-10) · `vendor-env.md` (P21-11) · `remote-probes.md` (P21-12) ·
  `doctor-setup.md` (P21-13) · `task-overrides.md` (P21-14) · `fake-sdks.md` (P21-15) ·
  `contract-suite.md` (P21-16)
- plus `sdk-versions.md` (interop record: pinned SDK and CLI versions, licences, method list)

## Run until green
```bash
uv sync --locked --all-extras
uv run ruff check && uv run ruff format --check
uv run pyright src/indus/backends
uv run pytest tests/unit/backends tests/unit/cli/doctor/test_remote_backends.py \
  tests/unit/cli/setup/test_remote_backends.py tests/unit/fakes tests/contract/backends
uv run python -m tooling.checks --require-ref
# gated, needs real vendor credentials; never in the default loop:
uv run pytest -m network tests/integration/backends/test_remote_backends_live.py
```
On any failure fix the cause and rerun from `uv sync`. Never skip a test, never widen the licence
allow-list without a written reason in the note, never hand-edit `sdk_surfaces.json`.

## Compare checklist (reopen the reference after writing)
- [ ] Every timeout and limit in the Task cells matches the reference's numbers or has a D-id (600 s SIF build, 120 s instance start, 30 s stop, 3600/300/60/15 s Modal, 30 s/500 ms Vercel wait, 2 GiB, 2/4/8 s).
- [ ] Managed Modal paths in the reference (`managed_modal.py`) have no counterpart; the config refusal cites D-16.16.
- [ ] Legacy Daytona label lookup and Modal legacy snapshot keys are absent on purpose (D-16.3).
- [ ] Sync-back never writes a credential file and never a path outside the mapped dirs; compare against the reference's upload-only and mapping rules.
- [ ] Stdin modes per backend match the reference table (pipe: Singularity; streamed: Modal; staged file: Daytona, Vercel).
- [ ] Isolation decision is by mounts, not by backend name (R02.6) for all four.
- [ ] Every exec path returns the same `ExecResult` fields as local, including `environment_recreated`.
- [ ] Docs-vs-code differences found while reading go into D-16.24 with the note's name.

## Traps covered
| Trap | Pinned by |
|---|---|
| R02.1 per-session vars in snapshots | P21-11 |
| R02.2 bounded vs full capture (downloads unbounded) | P21-06, P21-09 |
| R02.3 timeout backstop on every backend | P21-16 |
| R02.6 isolation by mounts | P21-04, P21-12 |
| R02.7 credentials never on command lines | P21-11 |
| R02.8 profile scoping of records and keys | P21-01, P21-14 |
| R02.10 cwd only from this command's marker | P21-16 |
| R02.13 flapping probes | P21-12 |
| R02.16 remote copies verified by byte count | P21-03 |
| R02.19 recreated sandbox flagged | P21-05, P21-16 |
| R02.22 vendor tokens out of child envs | P21-11 |
| R02.23 follow code, not docs | P21 compare checklist |
| R02.29 unscoped probes at boot | P21-12 |
| R02.30 SDK timeouts | P21-02 |
| R02.31 sync-back rules | P21-10 |

## Acceptance
| Feature | Planned ids |
|---|---|
| D5 Singularity/Apptainer backend | ACC-D5-1 (command runs in an instance and cwd persists), ACC-D5-2 (persistent overlay survives restart) |
| D6 Modal direct with snapshots | ACC-D6-1 (command runs), ACC-D6-2 (filesystem restored from snapshot), ACC-D6-3 (bad snapshot falls back and warns) |
| D7 managed Modal gateway (dropped) | ACC-D7-1 (config `managed` refused with message; cites D-16.16) |
| D8 Daytona backend | ACC-D8-1 (command runs), ACC-D8-2 (stopped sandbox restarted, files kept) |
| D9 Vercel Sandbox backend | ACC-D9-1 (command runs), ACC-D9-2 (persistent snapshot restored), ACC-D9-3 (invalid disk refused before API) |
| D12 remote file sync (sync-back part) | ACC-D12-3 (changed files come back), ACC-D12-4 (credential files never pulled back) |
| D14 terminal settings (new keys) | ACC-D14-4 (sync-back limit and backend keys validated) |

## Done when
- The contract suite is green for all seven backends with fakes, and the trap rows above point at tests.
- `indus doctor` shows one line per backend with a reason when unavailable, and no SDK is imported
  when its backend is not selected (import-time test).
- Live smoke (owner or lead with credentials, recorded in the note with date and SDK versions): one
  Modal, one Daytona, one Vercel and one Apptainer run each create a file, recreate the sandbox and
  read the file back; the M8 demo row "a Modal/Daytona/Vercel/Singularity sandbox run each" is shown.
