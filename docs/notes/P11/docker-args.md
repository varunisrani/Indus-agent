# Task: Docker run arguments (P11-02) Phase: 11 Owner: P11 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/docker.py`
(`_BASE_SECURITY_ARGS`, `_build_security_args`, `_resource_args`, `_extra_args_set_shm_size`,
`_extra_args_network_mode`, `_host_user_args`, `_run_command`), research `02-tools.md` §3.10,
traps R02.20, R02.23. Closed before writing.
Evidence: none.

What it does (own words): turns the Docker settings plus the host facts from the probes into the
security and resource part of `docker run`. It is a pure function, so every combination can be
pinned by a golden argv list. The container is the security boundary: all capabilities are dropped
and only the few a root user needs to work on files are added back, privilege gain is off,
temporary dirs are size-capped tmpfs mounts, and resource limits apply when the host's cgroups
allow them.

Inputs → outputs: `run_args(settings, facts, host_user=None)` → `RunArgs(args, init)`:
- always `--cap-drop ALL`, `--cap-add DAC_OVERRIDE CHOWN FOWNER`, plus `SETUID SETGID` unless the
  container runs as the host user; `--security-opt no-new-privileges` unless `docker_snap_compat`;
  tmpfs `/tmp` 512m (nosuid, exec), `/var/tmp` 256m (noexec, nosuid), `/run` 64m noexec — exec when
  the image boots its own s6 `/init`;
- `--user uid:gid` with `docker_run_as_host_user` (when POSIX ids exist);
- with cgroups: `--cpus`, `--memory <MB>m`, `--pids-limit 2048`;
- `--shm-size 1g` unless the extra args set it or the setting is empty / "0";
- `--storage-opt size=<MB>m` only when the storage probe passed, else one warning per process;
- `docker_network: false` → `--network=none`; an extra-args network flag that asks for anything
  else → `DockerConfigError` (Docker exits 125 on a repeated `--network`, R02.20); an extra
  `--network none` is the same intent and is kept once;
- `init` is True unless the image has an s6 `/init` or snap compatibility is on (no `--init`).

Files: `src/indus/backends/docker/args.py`, `tests/unit/backends/docker/test_args.py`.

Edge cases / failure modes: memory/cpu of 0 → flag omitted; non-string extra args dropped with a
warning; `--net` spelling and `--network=x` form both recognised; disk 0 → no storage flag and
no warning.

Our design differences / decisions:
- PID limit 2048 (code value, R02.23 / D-16.24), not the docs' 256.
- Defaults: cpu 1, memory 5,120 MB, disk 51,200 MB (schema `terminal.container_*`).
- The extra args go last so they can still override a default (reference order kept as behaviour).

Tests we will write: golden argv for default, no cgroups, host user, snap compat, s6 image,
network off, storage on/off; shm omitted for "0" and when extra args set it; contradicting network
raises; matching `--network none` not doubled; pids 2048 present only with cgroups.

Compare checklist (filled after the rewrite; reopened `tools/environments/docker.py`):
- [x] caps and tmpfs sizes/options (test: `tests/unit/backends/docker/test_args.py::test_default_golden`)
- [x] SETUID/SETGID dropped for the host user (test: `::test_host_user_drops_privdrop_caps`)
- [x] snap compat drops no-new-privileges and `--init` (test: `::test_snap_compat`)
- [x] s6 image: exec `/run`, no `--init` (test: `::test_s6_image_runs_exec_run_without_init`)
- [x] network conflict refused, same intent kept once (tests: `::test_contradicting_network_is_refused`, `::test_extra_network_none_is_not_doubled`)
- [x] storage only with the probe, one warning otherwise (test: `::test_storage_quota_only_when_probed`)
- Deliberate differences: none in behaviour; structure is a pure function returning a dataclass.
- Not taken: egress proxy args (D15, proposed for P24).

## Status

Done (2026-10-18).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- No change at integration.
