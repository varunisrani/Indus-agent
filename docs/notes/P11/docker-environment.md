# Task: Docker environment lifecycle (P11-05) Phase: 11 Owner: P11 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/docker.py`
(`__init__`, `_container_identity`, `_sanitize_label_value`, `_reuse_environment_fingerprint`,
`_attach_existing_container`, `_find_reusable_container`, `_start_container`, `_run_command`,
`_docker_run`, `_run_bash`, `cleanup`, `wait_for_all_teardowns`), `website/docs/user-guide/docker.md`
(skimmed; its numbers are stale, R02.23), research `02-tools.md` §3.8–§3.10, traps R02.8, R02.23.
Closed before writing.
Evidence: none.

What it does (own words): one long-lived container per environment key runs `sleep infinity`; every
command is a fresh `docker exec [-i] <id> bash -c <wrapper>` through the P05 shared flow
(`SpawnedEnvironment`: snapshot, wrapper, cwd marker, capture, wait, backstop). Containers carry
labels (`indus-agent=1`, `indus-task-id`, `indus-profile`, `indus-environment=<fingerprint>`); the
fingerprint hashes the image, the mounts and the profile home unless `docker_shared_key` is set, so
changed settings never attach to a stale container. With `docker_persist_across_processes` (default
on) a new process first looks for a labelled container and attaches (starting it when stopped).
A persistent container is left running at cleanup for the next process (the orphan reaper, P11-07,
handles leftovers); a non-persistent one gets `stop -t 10` then `rm -f`. A daemon that disappears
turns into P05's degraded result and the environment is evicted, so the next call builds a new one.

Keys (P05 rule): `terminal.container_persistent` on → one key per profile (`EnvKey(kind="profile")`,
not ended by a session's release); off → the session's key.

Inputs → outputs: `open_docker(request)` (the registry factory) → started `DockerEnvironment`;
`DockerEnvironment.execute(...)` → `ExecResult`; `cleanup()`.

Files: `src/indus/backends/docker/environment.py`, `tests/unit/backends/docker/test_environment.py`;
shared: `src/indus/backends/registry.py` (docker row, `profile` key kind, `profile_scoped` hook on
`BackendEntry`), `tests/contract/backends/conftest.py` (keeps the contract suite on local + fake
until P11-21 adds docker with the fake CLI), `tests/unit/cli/setup/test_wizard.py` (the
"unavailable backend" case pins the list of ready backends).

Edge cases / failure modes:
- `docker run` fails (exit 125, timeout) → the half-made container is removed by name, the error
  text is redacted of every env value and raised as a backend error.
- several labelled matches → prefer a running one, else the first.
- `start` of a stopped match fails → create a fresh one.
- `docker_network: false` and the matched container's network mode is not `none` → removed and
  recreated.
- killing a command: the local docker client tree is stopped (P05 ordered kill) and the command's
  shell inside the container gets TERM, then KILL, through a pid file the script writes first.
- client errors that mean the daemon is gone ("Cannot connect to the Docker daemon", "error during
  connect") → `EnvironmentConnectionError`.

Our design differences / decisions:
- Our labels/names (`indus-*`, `indus-<hex8>`); no egress label (D15 not built).
- `docker exec` is given no `-w`: the wrapper `cd`s itself, and a `-w` to a removed directory would
  fail the exec before bash starts. The container gets `-w <cwd>` at `run`.
- Container kill reaches inside the container (pid file) — the reference only stops the client.
- Cleanup of a non-persistent container is awaited inside `cleanup()`; the registry already bounds
  every cleanup at 15 s and abandons a slow one (that is our "detached and drained" worker).

Tests we will write: fake CLI state machine: second process attaches to the labelled container
instead of creating; a stopped one is started; a different image → new fingerprint → new
container; non-persistent cleanup issues stop then rm; persistent cleanup leaves it; daemon gone
mid-call → degraded result through the registry and the next call recreates; run failure removes
the named container and keeps values out of the error.

## Decisions for the lead
- Registry change: `BackendEntry.profile_scoped(scope) -> bool` and key kind `profile`; `release()`
  leaves profile keys alone (idle reaper and shutdown still clean them).
- Docker is now in `default_entries()`, so `indus setup` offers it as ready. The contract suite is
  filtered to `local` (+ fake) until P11-21 adds the docker row with the fake CLI.
- Background-process "lost" wiring: `recovery.register_lost_handler` is the hook; the host (or
  slice B's `remote_processes`) registers `lambda key: mark_lost(processes, key)`. Remote pollers
  can also compare `DockerEnvironment.recreations`.
- Reaper liveness: the host calls `reaper.set_liveness(session_alive=..., jobs_alive=...)`.
- Default container cwd is `/workspace` (the reference's is `/root`); the host cwd is used only
  when it is bound, or when a per-task override gives an absolute path.
- `Availability` runs `docker version` (10 s bound) synchronously inside the registry/tool probe,
  cached 30 s; a follow-up could make it async-fed.

Compare checklist (filled after the rewrite; reopened `tools/environments/docker.py`):
- [x] labels + fingerprint (image, mounts, home; shared key opts out) (test: `tests/unit/backends/docker/test_environment.py::test_different_image_makes_a_new_container`, `::test_shared_key_has_no_fingerprint`)
- [x] attach running / start stopped (tests: `::test_second_process_attaches`, `::test_stopped_container_is_started`)
- [x] network lockdown guard on attach (test: `::test_networked_container_replaced_under_lockdown`)
- [x] failed run removes the named container (test: `::test_failed_run_removes_the_container_and_hides_values`)
- [x] persistent left running, non-persistent stop + rm (tests: `::test_persistent_cleanup_leaves_it_running`, `::test_non_persistent_cleanup_stops_then_removes`)
- [x] the P05 execution-contract suite passes for docker over the fake client (scratch run of
  `tests/contract/backends` with a docker row: 12/12; the row itself is P11-21's)
- Deliberate differences: no `-w` on exec; in-container kill; default cwd `/workspace`; the image-mismatch "keep the old
  sandbox when the default image changes" rule is not taken — the fingerprint includes the image,
  so a new image means a new container (the old one stays until the reaper's 24 h rule).
- Not taken: egress label and proxy; `force_remove` teardown hook (unused in the reference).

## Status

Done (2026-10-18).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- Decisions accepted: profile-wide container label `shared`, `profile_scoped` key kind, default cwd `/workspace` (D-16.122), new image = new container (D-16.125).
- Host wiring: `TerminalRuntime.create` registers `recovery.register_lost_handler` with a weak handler that calls `mark_lost(processes, key)`; `shutdown` unregisters it. `cli/runtime.build_service` calls `service.terminal_host.install(store, processes)`, which sets `reaper.set_liveness` (session over = the newest segment of its compression chain has ended; unknown or store error = alive) and the receipts lineage resolver.
- Sudo: `open_docker` installs `sudo.backend_sudo(scope, backend="docker", target="docker:<key>")`, which is None unless `docker_sudo_password` is stored (security item, D-16.134).
- The contract suite has a `docker` row over the fake client (P11-21).
