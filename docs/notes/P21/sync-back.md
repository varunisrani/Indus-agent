# Task: Sync-back (P21-10, R02.31) Phase: 21 Owner: P21 builder B2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/file_sync.py`
(`FileSyncManager.sync_back` through `_apply_staged_file`, `_cleanup_stale_sync_back_temp`,
`_credential_host_paths`). Closed before writing.
Evidence: none.

What it does (own words): when a Modal, Daytona or Vercel sandbox is cleaned up, files the agent
changed inside the mirrored dirs of the sandbox's `.indus` dir (skills, the synced cache dirs) are
copied back to the profile. Nothing happens when this session never pushed anything. The remote
app dir comes down as one tar (through the backend transport) into a private dir under
`<home>/cache/sync-back/`; an archive larger than `terminal.sync_back_max_bytes` (2 GiB) is refused
whole. Each member is checked before anything is written: only regular files; no absolute names,
no `..`, no symlinks or hard links; it must map into one of the mirrored dirs (exact pushed path
first, else a mirrored root); credential files are upload-only and never written back. A file is
written only when its SHA-256 differs from what this session pushed (and from what the host has),
via a temp file + rename in the target dir; the target's real parent must stay inside the real
mirrored root (symlinked dirs on the host cannot redirect a write) and an existing symlink at the
target is refused. Download + apply get 3 attempts with 2/4/8 s backoff. The apply phase is
serialised across processes by a file lock in the profile home, and a Ctrl+C that arrives during
the apply is held until the apply finishes, then delivered. Leftover temp entries older than 30 min
whose owner pid is dead are removed at start.

Inputs → outputs: the push-side `FileSync` (pushed hashes, upload-only paths), the transport, the
mirrored roots, the profile home → `SyncBackReport` (status, written, refused with reasons).

Files: `src/indus/backends/sync_back.py`, `tests/unit/backends/test_sync_back.py`,
`src/indus/foundation/config/schema.py` (shared: `sync_back_max_bytes`).

Edge cases: archive exactly at the limit is accepted, one byte over refused; member that is a
directory → skipped silently; host file edited since push and remote changed → remote wins, one
warning; empty or unchanged archive → no write at all; transport failure on every attempt → status
`failed`, logged, never raised to the cleanup caller.

Our design differences / decisions:
- members are streamed one by one from the tar (no `extractall` into a staging tree).
- the lock covers the apply only (downloads go to private temp dirs and may overlap).
- the stale sweep follows the phase rule (dead owner AND older than 30 min).
- `remote_sync.py` is not edited here: the SDK environments call `sync_back` from their own
  cleanup (the registry wiring of P21-12 decides the rest).

Tests (archives built by the test with `tarfile`): changed/unchanged/credential/`../escape`/symlink/
absolute members → only the changed file written; new file in a mirrored dir written; symlinked host
dir refused; 2 GiB + 1 refused (limit lowered in the test, same comparison); two concurrent apply
phases serialise; SIGINT during apply finishes then raises; stale dead-pid temp removed, live kept;
no-push session does nothing; retries 2/4/8 s on a fake clock.

Compare checklist (filled after the rewrite; reopened `tools/environments/file_sync.py`):
- [x] skip without prior push (`tests/unit/backends/test_sync_back.py::test_nothing_pushed_means_no_download`)
- [x] 2 GiB cap configurable (`::test_archive_over_the_limit_is_refused_whole`)
- [x] hash diff against the pushed hash; host copy equal → no write (`::test_only_the_changed_and_new_files_are_written`,
  `::test_unchanged_archive_writes_nothing`)
- [x] upload-only credential files never written, by remote path and by resolved host path
  (`::test_only_the_changed_and_new_files_are_written`, `::test_credential_is_never_written_even_inside_a_mirrored_dir`)
- [x] `..`, absolute, symlink and hard-link members refused; symlinked host dirs cannot redirect
  (`::test_symlinked_host_dir_cannot_redirect_a_write`)
- [x] 3 attempts, 2 s then 4 s (`::test_failed_downloads_retry_after_2_then_4_seconds`)
- [x] SIGINT held until the apply ends (`::test_ctrl_c_during_apply_finishes_the_apply_then_raises`)
- [x] cross-process lock (`::test_the_apply_waits_for_the_profile_lock`, `::test_two_sync_backs_both_apply`)
- [x] stale temp sweep (`::test_stale_temp_of_a_dead_owner_is_removed_live_kept`)
- Deliberate differences: the reference extracts the whole archive with the `data` filter and maps
  new files by the parent dir of any mapped file; we stream members and map by explicit mirrored
  roots (skills, synced cache dirs), so a new file lands only inside those. The reference's stale
  sweep removes a dead owner's entry at once and a live one after 30 min; the phase (and we) remove
  only dead AND older than 30 min — lead to confirm. The lock covers the apply only.

## Status

Done (2026-10-19). The SDK environments call `sync_back` from `SdkEnvironment.cleanup`;
`remote_sync.py` is unchanged (the phase's "accept the SDK transports" needs nothing: they already
implement `SyncTransport`). The registry's 15 s cleanup bound (`CLEANUP_BOUND_S`) is shorter than
sync-back (120 s download) + snapshot (60 s): P21-12 must give SDK backends a longer bound.

Integrated (lead, 2026-10-19): the SDK rows carry `cleanup_bound_s = 195` (bulk 120 s + snapshot 60
s + stop 15 s; `sdk_entries.SDK_CLEANUP_BOUND_S`), used by `EnvironmentRegistry.cleanup_bound_for`.
