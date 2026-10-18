# Task: Docker mounts (P11-03) Phase: 11 Owner: P11 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/docker.py`
(`_mount_args`, `_split_volume_spec`, `_container_mount_taken`, `_existing_host_mount`,
`_free_host_cwd_mount`, `_readonly_skill_mount_args`, `_abs_host_cwd`),
`tools/environments/path_utils.py` (`sanitize_task_id_for_path`, behaviour only), research
`02-tools.md` §3.10, traps R02.6, R02.20. Closed before writing.
Evidence: none.

What it does (own words): decides what the container sees of the host. User volumes come first
(bad entries skipped with a warning). The host working directory is bound only when the user asked
for it (or it is a Windows drive path): at `/workspace`, or at `/host-cwd`, `-2`, `-3` when a
volume already owns `/workspace`; when a volume already binds that very directory it is reused.
Skills dirs and credential files go in read-only. A non-persistent container keeps its working
space in tmpfs; a persistent one binds owner-only dirs under `<home>/sandboxes/docker/<task>/`.
The plan reports whether any host path is bound, which decides the isolation flag (R02.6).

Inputs → outputs: `plan_mounts(settings, *, home, task_key, host_cwd, readonly=[...])` →
`MountPlan(volume_args, writable_args, cwd_target, host_paths_mounted, home_dir, workspace_dir)`.
`ReadonlyMount(host_path, container_path)`; `register_readonly_source(fn)` lets the credential-file
registry (P11-11, slice B) add its files without this module importing it.

Files: `src/indus/backends/docker/mounts.py`, `tests/unit/backends/docker/test_mounts.py`.

Edge cases / failure modes:
- a volume without a container path, or with a relative container path → skipped, one warning;
- `~` in a host path expanded with the scope's HOME;
- a task key with colons (session keys) → sanitised dir name with a digest so `a:b` and `a_b`
  never share a dir (R02.20);
- a read-only source that is missing or the wrong kind (file vs dir) → skipped with a warning
  (Docker would otherwise create a directory in its place);
- `host_paths_mounted` is true for a writable bind of the user's files (a volume not marked
  `ro`, or the host cwd). Indus's own persistent sandbox dirs do not count (scratch space, not the
  user's files), nor do read-only skill/credential mounts (nothing can be destroyed through them).

Our design differences / decisions:
- The isolation rule counts writable binds of user paths only; `:ro` volumes and named volumes
  do not count (decision below).
- Credential files come from `indus.backends.credential_files` (slice B, P11-11) through its
  `resolve(scope)` and `mount_specs(...)`; `credential_mounts(scope)` imports it lazily and accepts
  `host:container[:ro]` text, (host, container) pairs or objects with `host_path` and
  `container_path`/`remote_rel`; a relative container path lands under `/root` (so
  `.indus/credentials/…` → `/root/.indus/credentials/…`). Without the module: no credential mounts.
- Persistent dirs created with 0700.

Tests we will write: table: cwd mount taken → `/host-cwd`; duplicate dir reused; malformed volume
skipped; colon task key sanitised; tmpfs for non-persistent; persistent bind dirs 0700; read-only
skill mount present and skipped when missing; `host_paths_mounted` only for host binds.

## Decisions for the lead
- At integration, check that slice B's `mount_specs` output matches one of the shapes
  `_as_mount` accepts (the call is `mount_specs(resolve(scope))`).
- `host_paths_mounted` counts user volumes (unless `:ro`) and the host cwd bind; it does not count
  Indus's own sandbox dirs or read-only skill/credential mounts. Change in `MountPlan` if the
  lead wants every bind to count.

Compare checklist (filled after the rewrite; reopened `tools/environments/docker.py`):
- [x] `/workspace` → `/host-cwd` → `-2` → `-3` (test: `tests/unit/backends/docker/test_mounts.py::test_cwd_goes_to_host_cwd_when_workspace_is_taken`)
- [x] existing bind of the same dir reused (test: `::test_existing_mount_of_the_cwd_is_reused`)
- [x] malformed volume skipped (test: `::test_malformed_volumes_are_skipped`)
- [x] colon in task key (test: `::test_task_key_with_colons_is_sanitised`)
- [x] tmpfs sizes for non-persistent (test: `::test_non_persistent_uses_tmpfs`)
- [x] read-only mounts and missing sources (test: `::test_readonly_mounts`)
- [x] credential files through the registry hook (tests: `::test_credential_files_come_from_the_registry`, `::test_no_credential_module_means_no_mounts`)
- Deliberate differences: the reference marks prompts on for any host bind; we skip read-only ones
  (see decision). The reference's volatile-tempdir fingerprint canonicalisation is not needed: our
  skills mount is the real dir, never a per-process copy.
- Not taken: egress CA mount (D15).

## Status

Done (2026-10-18).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- Credential files: the `importlib` indirection (a stand-in while slice B's module did not exist) is gone. `credential_mounts(scope, session_id)` imports `credential_files.resolve` and calls `mount_specs(files, "/root/.indus")` directly. The old call `mount_specs(resolve(scope))` passed one argument, so it raised and nothing was ever mounted (caught and logged). Now a profile's credential files are mounted read-only below `/root/.indus/credentials/...` (test: `test_credential_files_come_from_the_registry`, real registry). A profile-wide container gets the files of the session that created it; skill-declared files of later sessions reach it only after the container is recreated.
- `host_paths_mounted` decision accepted: read-only binds keep the container isolated (D-16.126).
