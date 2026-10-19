# Task: Singularity/Apptainer backend (P21-04) Phase: 21 Owner: P21 slice B1 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/singularity.py`
(all), `tools/environments/remote_common.py` (`bash_argv`, `run_capture`). Closed before writing.
Evidence: none.

What it does (own words): on HPC machines without Docker, `terminal.backend: singularity` runs the
agent's commands in an Apptainer (or SingularityCE) instance. The CLI is found on the profile's
PATH (`apptainer` first, then `singularity`). A `docker://` image is converted once into a SIF file
in a cache under the scratch dir; one long-lived instance is started per environment with
`--containall --no-home` (no host home, private /tmp, clean env), read-only binds for the profile's
credential files and skills, CPU/memory limits when the host's cgroups allow them, and either a
throwaway writable tmpfs or, when persistent, an overlay directory that survives restarts. Every
command is a fresh `apptainer exec instance://<name> bash -c <wrapper>` with stdin piped, through
the shared P05 shell flow (snapshot, cwd marker, capture, wait, backstop, interrupt). Cleanup stops
the instance and records the overlay in the P21-01 store.

Inputs → outputs:
- `singularity_entry()` → registry row `singularity` (factory `open_singularity`, flags
  `isolated=True, container_paths=True, stdin_mode="pipe"`, probe, profile-scoped when persistent).
  Registration in `default_entries` is P21-12's.
- `find_cli(scope)` → path of `apptainer`, else `singularity`, on the scope's PATH (P01 lookup);
  `singularity_probe(scope)` → `"Apptainer or Singularity not found"` or None.
- `settings_for(scope)` → `SingularitySettings(image, scratch_dir, disk_warning_gb, cpu, memory_mb,
  persistent)`: image = `terminal.singularity_image` or `docker://<terminal.docker_image>`; scratch
  = `terminal.scratch_dir` or `<home>/sandboxes/singularity`.
- `ensure_sif(cli, image, scratch)` → local SIF path: URIs (`docker://`, `oras://`, …) build into
  `<scratch>/sif-cache/<sha256(image)[:16]>.sif` with `build --force` under a file lock (one build
  per image across processes; 600 s limit; built to a temp name then renamed); a local path is used
  as is. Failure or timeout → `SingularityBuildError` with at most 500 chars of stderr.
- `open_singularity(request, *, runner=None, cgroups=None, clock=None)` → started
  `SingularityEnvironment`; `start_argv(...)` builds the `instance start` argv (pure).
- Exec env: passthrough values travel as `APPTAINERENV_<NAME>` (or `SINGULARITYENV_` for the
  singularity CLI) in the client's environment, never in argv (R02.7); names without a value are
  unset in the script; passthrough names are kept out of the snapshot (R02.1).

Files: `src/indus/backends/singularity.py`, `tests/unit/backends/test_singularity.py`; config rows
`terminal.singularity_image`, `terminal.scratch_dir`, `terminal.disk_warning_gb` (schema, shared);
contract row `singularity` in `tests/contract/backends/conftest.py` (shared).

Edge cases / failure modes:
- No CLI → `BackendUnavailable("Apptainer or Singularity not found")` (registry answers disabled).
- `version` fails → `BackendUnavailable` with the CLI's first error line.
- Two tasks with the same image → one build (lock + re-check); a second process waits on the lock.
- Build timeout (600 s) → typed error with the partial stderr tail; the partial file is removed.
- `instance start` fails because cgroup limits are not usable → one retry without `--cpus/--memory`
  and a warning; any other failure → `BackendError` with the CLI's message (120 s limit).
- Instance vanished (stopped outside Indus): exec exits 255 "no instance found" →
  `EnvironmentConnectionError`; the registry evicts and the next call starts a fresh instance.
- Scratch usage above `terminal.disk_warning_gb` → one warning per scratch dir per process.
- Persistent overlays: one directory per task key (`<scratch>/overlays/<safe key>`), reused from the
  record store; two processes cannot mount the same overlay at once (Apptainer refuses the second);
  the second process gets the CLI's error.
- Snapshot files live in the instance's private /tmp; removed before stopping a persistent instance
  (the overlay keeps /tmp).

Our design differences / decisions:
- A failed SIF build is an error (the reference falls back to running the `docker://` URL, which
  silently pulls on every start).
- SIF cache names are a digest of the image reference, not the mangled reference.
- No `/scratch` auto-detection: the scratch dir comes from config or the profile home.
- Sudo inside the instance gets no password hook (`--containall` instances run as the user; there
  is no stored per-backend sudo secret for singularity).
- Stopping a command: the exec client runs in its own process group; the P05 ordered kill of that
  group reaches the command (Apptainer runs exec'd processes as descendants of the client, unlike
  `docker exec`).

Tests we will write (fake `apptainer` on PATH, `tests/fakes/backend/fake_apptainer.py`):
- start flags (`--containall --no-home`, cpu/memory when cgroups allow, none when not), read-only
  binds for credential files and skills
- overlay vs `--writable-tmpfs` by persistence; overlay recorded in the P21-01 store and reused
- `singularity` fallback when only it is on PATH; missing CLI → requirement reason
- SIF built once for two environments with the same image; build failure and timeout → typed
  error with stderr ≤ 500 chars
- stop on cleanup; vanished instance → connection error
- passthrough values in `APPTAINERENV_*`, never in argv
- disk warning once
- the shared execution-contract suite rows (`tests/contract/backends`)

## Decisions for the lead
- SIF build failure raises instead of falling back to the URL (see above).
- cgroup support is guessed as "root, or cgroup v2 present"; a start that still fails on cgroups
  is retried once without limits.
- The singularity row is profile-scoped when `terminal.container_persistent` is on (like Docker).

Compare checklist (filled after the rewrite; reopened `tools/environments/singularity.py`):
- [x] `apptainer` before `singularity` (test: `test_singularity.py::test_singularity_fallback`)
- [x] `instance start --containall --no-home`, `--overlay` vs `--writable-tmpfs`, ro binds,
  `--memory <MB>M`, `--cpus` (test: `::test_start_flags_tmpfs_binds_and_a_command`,
  `::test_persistent_overlay_recorded_and_reused`)
- [x] 600 s build, 120 s start, 30 s stop (constants; test: `::test_build_timeout_is_typed`)
- [x] instance name `indus_<12 hex>` (test: `::test_start_flags_tmpfs_binds_and_a_command`)
- [x] overlay path recorded at cleanup (test: `::test_persistent_overlay_recorded_and_reused`)
- Deliberate differences: build failure is an error; `build --force` to a temp name (the reference
  built straight to the cache name without `--force`); digest-named SIF cache; no `/scratch`
  auto-detect; the recorded overlay path is reused at start (the reference only wrote it); values via `APPTAINERENV_*` instead of the reference's host env passthrough;
  vanished instance → connection error and a fresh instance.
- Not taken: the reference's `APPTAINER_CACHEDIR` from the host env (we set our own under scratch).

## Status

Done (2026-10-19). Registration in `default_entries` and the doctor row are P21-12/13.

Integrated (lead, 2026-10-19): `singularity_entry()` is in `registry.default_entries` (after `ssh`);
the doctor row stays with P21-13.
