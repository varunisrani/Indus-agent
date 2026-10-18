# Task: Credential files registry (P11-11) Phase: 11 Owner: P11 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/credential_files.py` (all).
Spec: research `02-tools.md` §3.10a (credential files), §3.11, §6 R02.31; phase file P11-11.
Closed before writing.
Evidence: none.

What it does (own words): lists the host files a sandbox needs to have (a cloud CLI token file, a
service account key a skill declares) and where they land remotely. Sources: the config list
`terminal.credential_files` (host paths, `~` allowed) and files a skill declares for the session.
Each entry becomes `{host_path, remote_rel, mode}` with `remote_rel` under
`.indus/credentials/` (a path inside the user's home keeps its home-relative shape, anything else
its absolute shape below `abs/`). Refused: files inside Indus's own secret stores and control
files (the same rule `read_file` uses), and files not owned by the current user. A missing file is
skipped with one warning per session. Docker mounts the entries read-only (P11-03 consumes
`mount_specs`); SSH pushes them through the file sync as upload-only items that a later sync-back
must never pull (R02.31).

Inputs → outputs: scope (config + home) and session id → `CredentialFile` list, Docker `-v` specs,
`SyncItem`s.

Files: `src/indus/backends/credential_files.py`, `tests/unit/backends/test_credential_files.py`.

Edge cases / failure modes:
- relative paths in config resolve against the user's home (`HOME` of the scope env).
- symlinks resolve before the checks (a link into the secret store is refused).
- directories are not credential files → refused with a warning.
- two entries with the same host path → one entry.
- skill registrations are per (profile home, session) and cleared at session end.

Our design differences / decisions:
- host paths anywhere the user owns, not only inside the Indus home (the reference only takes
  home-relative paths); the secret-store and ownership checks replace its containment rule.
- mode defaults to 0600 on the remote side.

Tests we will write:
- secret-store path refused; file owned by another user refused (ownership check injected);
  missing file warned once per session; remap to `.indus/credentials/…`; Docker mount spec
  read-only; sync items marked upload-only; skill registration cleared per session.

Compare checklist (filled after the rewrite; reopened `tools/credential_files.py`):
- [x] sources: config list + skill-declared per session (test: `tests/unit/backends/test_credential_files.py::test_skill_declared_files_are_per_session`)
- [x] secret stores refused, links into them too (test: `::test_secret_stores_are_refused`)
- [x] missing skipped with one warning per session (test: `::test_missing_file_warned_once_per_session`)
- [x] other owner and directories refused (test: `::test_other_owner_and_directories_refused`)
- [x] remap under `.indus/credentials/`, Docker read-only specs, upload-only sync items (tests: `::test_registry_table`, `::test_docker_mounts_read_only_and_sync_upload_only`)
- Deliberate differences: host paths anywhere the user owns (the reference: only paths inside its home, with containment checks); config read per call (the reference caches per home); skill registrations keyed by profile and session (the reference: a context variable).
- Not taken: skills/cache dir mounts for Docker (P11-03 owns mounts); the symlink-free skills copy is Docker's concern.

## Status

Done (2026-10-18). `src/indus/backends/credential_files.py`, `tests/unit/backends/test_credential_files.py`.
P11-03 (Docker mounts) consumes `resolve(...)` + `mount_specs(files, "/root/.indus")`.

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- Docker now consumes `resolve(scope, session_id)` + `mount_specs(files, "/root/.indus")` directly (see docker-mounts). D-16.132.
