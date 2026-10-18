# Task: Docker discovery and probes (P11-01) Phase: 11 Owner: P11 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/docker.py`
(`find_docker`, `docker_runtime_name`, `docker_runtime_start_hint`, `_ensure_docker_available`,
`_cgroup_limits_available`, `_storage_opt_supported`, `_image_uses_init_entrypoint`), research
`02-tools.md` §3.10 (Docker), traps R02.13, R02.20. Closed before writing.
Evidence: none.

What it does (own words): before Indus can run anything in a container it has to find a container
client (Docker or Podman), know whether its daemon answers, and learn a few host facts that change
the `docker run` flags: are cgroup resource limits usable, does the storage driver take a per-container
disk quota, and does the sandbox image bring its own `/init` (s6-style) as PID 1. Every probe has its
own short timeout and never pulls an image (no network). The terminal tool's availability check
must not flap: a "no" within 60 s of a "yes" keeps serving "yes" (R02.13). `indus doctor` shows one
row with the client, its version, the daemon state and whether the image is present.

Inputs → outputs:
- `find_client(scope)`: `terminal.docker_binary` → `INDUS_DOCKER_BINARY` (scope env) → `docker` on
  the scope's PATH → `podman` on PATH → `ClientInfo(path, runtime)` or None.
- `DockerCli(client, env, runner)`: one place every CLI call goes through (`call(*args, timeout,
  extra_env)` → `CliResult(returncode, stdout, stderr, timed_out)`); the runner is injectable so
  tests feed a fake in process.
- `ensure_daemon(cli)` (`version`, 10 s) → version text, or `DockerDaemonDown` (an
  `EnvironmentConnectionError`, so the registry answers "degraded") with a per-OS start hint.
- `host_facts(cli, image, os_name)` → `HostFacts(cgroups, storage_quota, s6_init)`; one `info`
  call (JSON) answers cgroups and the storage driver; the quota is confirmed with a throwaway
  `create --pull=never --storage-opt size=1m` only on overlay2-on-XFS; macOS is always false.
  Only definite answers are cached per process.
- `Availability(clock, check)` callable used as the backend row's probe: TTL cache plus the 60 s
  flake grace.
- `doctor_findings(scope)` → doctor rows.

Files: `src/indus/backends/docker/__init__.py`, `src/indus/backends/docker/probe.py`,
`src/indus/backends/docker/settings.py` (new: every `terminal.docker_*` / `container_*` value read
once into a dataclass), `tests/unit/backends/docker/test_probe.py`, `tests/fakes/backend/docker_cli.py`
(fake client with JSON state, argv/env recorder, executable shim). Shared: schema rows in
`src/indus/foundation/config/schema.py`, the docker branch of `check_backend` in
`src/indus/cli/doctor/checks.py`, the layout tree in `00-layout-and-conventions.md`.

Edge cases / failure modes:
- configured binary path that is not executable → fall through to PATH lookup, with a warning.
- `version` times out or exits non-zero → typed daemon-down error; the hint names the runtime
  (`podman machine start` vs starting Docker / Docker Desktop) and the OS.
- `info` unreadable or not JSON → no cgroup flags, no quota (safe side), not cached.
- image not pulled yet → entrypoint unknown → treated as non-s6 (hardened defaults kept).
- Podman's `info` JSON has a different shape (`host.cgroupControllers`, `store.graphDriverName`).

Our design differences / decisions:
- The reference probes cgroups by starting a throwaway container (may pull an image); we read
  `docker info` (MemoryLimit/PidsLimit/CpuCfsQuota, or Podman's controller list) and never pull.
- Storage quota confirmation uses `--pull=never` with our own image instead of `hello-world`.
- Default image is ours (`ghcr.io/indus-agent/indus-sandbox:slim`, D-16.64); `docker_image_pinned`
  requires an `@sha256:` digest (refused otherwise).
- No macOS Docker Desktop fallback paths: PATH lookup only (spec).

Tests we will write: discovery order (config, env, docker, podman fallback, none); daemon down →
typed error and hint per OS/runtime; version timeout; flake grace with a fake clock (served within
60 s, honoured after); cache TTL; s6 entrypoint detection and null entrypoint; storage false on
macOS facts, false on ext4, true on xfs with a passing create; cgroup facts from docker and podman
info; pinned image without digest refused; doctor rows.

## Decisions for the lead
- Default image registry is not chosen (17-naming says `<registry>`): we use
  `ghcr.io/indus-agent/indus-sandbox:slim` as the schema default. Change it in one place
  (`terminal.docker_image` default in `schema.py`).
- Proposed D-id: "cgroup and storage probes read `docker info` and never pull an image".

Compare checklist (filled after the rewrite; reopened `tools/environments/docker.py`):
- [x] client order: override → docker → podman (test: `tests/unit/backends/docker/test_probe.py::test_podman_when_docker_is_missing`, `::test_configured_binary_wins`)
- [x] runtime name and start hint per runtime (test: `::test_daemon_down_is_typed_with_a_hint`, `::test_start_hint_per_os_and_runtime`)
- [x] only definite cgroup/storage answers cached (test: `::test_unreadable_info_is_not_cached`)
- [x] s6 entrypoint `/init` and the s6-overlay command path (test: `::test_s6_entrypoint_detected`)
- [x] storage quota never on macOS (test: `::test_storage_quota_false_on_macos`)
- Deliberate differences: no throwaway container for cgroups; `--pull=never` everywhere; version
  probe 10 s (reference 5 s, spec says 10); no Docker Desktop install-path fallback.
- Not taken: the reference's `HERMES_DOCKER_BINARY` name (ours `INDUS_DOCKER_BINARY`).

## Status

Done (2026-10-18). Doctor row and schema rows are shared edits (listed in the slice report).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- Default image `ghcr.io/indus-agent/indus-sandbox:slim` accepted (D-16.64); the GHCR owner is still owner-open (17-naming `<registry>`), change it in one place (`terminal.docker_image` default).
- Probes never pull: D-16.121.
