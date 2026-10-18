# Task: Remote file sync, push side (P11-10) Phase: 11 Owner: P11 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/file_sync.py`
(all), `tools/environments/remote_common.py`, `tools/environments/ssh.py` (scp, tar upload, rm).
Spec: research `02-tools.md` §3.11, §6 R02.31; phase file P11-10. Closed before writing.
Evidence: none.

What it does (own words): a remote sandbox starts with none of the host files the agent relies on
(credential files, skills, some cache dirs). Before each command — at most every 5 s unless
`INDUS_FORCE_FILE_SYNC` is set in the scope env — the sync looks at every file it should mirror,
uploads the ones that changed, deletes remote copies of files that went away, and only then
records the new state. A failed transport call leaves the recorded state as it was, so the next
command tries everything again. Change detection is `(mtime_ns, size)`; a file modified within 2 s
of the scan cannot be trusted that way (a same-size rewrite in the same timestamp tick looks
unchanged), so it is recorded with a sentinel that forces a content hash next time, and pushed
again only when the hash differs from what was pushed. What was pushed is kept as remote path →
SHA-256 for the later sync-back of SDK backends (P21).

Transport interface `SyncTransport`: `upload_one(path, dest)` (30 s), `upload_bulk(files)`
(120 s, one tar stream), `download_bulk(src, dest)` (120 s; unused by SSH), `delete(paths)`
(15 s). The SSH transport: `scp` for one file, a tar archive written by Python's `tarfile` and fed
to `ssh … tar xf - -C <base>` for many, `rm -f` over ssh for deletes.

Inputs → outputs: a list of `SyncItem(host_path, remote_path, upload_only, mode)` from a source
callable → transport calls; `pushed` map.

Files: `src/indus/backends/remote_sync.py`, `tests/unit/backends/test_remote_sync.py`.

Edge cases / failure modes:
- file vanishes between listing and upload → the cycle fails, state rolls back, next command
  retries.
- a file grows or shrinks while being archived → the archive write fails the same way.
- remote paths outside the sync base are refused (no `..`).
- remote path with spaces/quotes → shell-quoted in every remote command.
- the first sync after start is forced.
- clock: the 5 s gate uses the monotonic clock of the environment (fake in tests).

Our design differences / decisions:
- files are streamed into the tar while being hashed (no staging copy, no symlink farm), and the
  archive is a temp file fed to the ssh stdin.
- the change key is `(mtime_ns, size)` as the plan says (no inode/ctime).
- no sync-back here (P21 extends this module).

Tests we will write (fake transport, fake clock):
- first call pushes all; unchanged tree within 5 s → no call; after 5 s unchanged → no call.
- file touched within 2 s with the same size and mtime → hashed and pushed next time when its
  bytes changed, not pushed when they did not.
- deleted file → one delete call; transport failure → state unchanged and retried next command.
- force env flag bypasses the 5 s gate; SSH transport argv (fake ssh CLI): bulk tar lands under
  the remote base, single scp, delete.

## Decisions for the lead
- `download_bulk(src, dest)` takes a local destination path too (the plan names only `src`).

Compare checklist (filled after the rewrite; reopened `tools/environments/file_sync.py`):
- [x] 5 s gate, first round forced, force flag from the scope env (tests: `tests/unit/backends/test_remote_sync.py::test_first_call_pushes_all_then_rate_limited`, `::test_force_flag_skips_the_gate`)
- [x] racy sentinel → content hash next round, pushed only when bytes differ (tests: `::test_racy_mtime_is_hashed_and_pushed_when_bytes_differ`, `::test_racy_file_with_same_bytes_is_not_pushed_again`)
- [x] transactional: failure keeps state, the rate-limit clock is not advanced (test: `::test_transport_failure_keeps_state_and_retries`)
- [x] deletes for removed files; pushed hash dropped (test: `::test_deleted_file_is_deleted_remotely`)
- [x] hash of exactly the bytes sent; paths outside the base refused; no dir entries in the archive (the reference uses `--no-overwrite-dir` for the same reason) (tests: `::test_archive_hashes_exactly_what_it_packs`, `::test_archive_refuses_paths_outside_the_base`)
- [x] SSH transport: scp, tar over ssh stdin, rm -f (test: `::test_ssh_transport_moves_files`)
- Deliberate differences: change key `(mtime_ns, size)` per the plan (the reference adds inode and ctime); archive streamed by `tarfile` with hashing on the way (no staging copies or symlink farm); delete timeout 15 s per the plan (the reference uses 10 s); a single changed file is hashed before `scp` (a write between hash and copy is caught on the next round only through the stat key).
- Not taken: sync-back, its size cap, retries, SIGINT deferral and lock (P21-10); the download excludes for live sockets belong there too.

## Status

Done (2026-10-18). `src/indus/backends/remote_sync.py`, `tests/unit/backends/test_remote_sync.py`. What is
mirrored: credential files, `<home>/skills` (minus `.git`, `node_modules`, `__pycache__`, `.venv`, `.hub`,
`.archive`, symlinks) and `<home>/cache/{documents,images,audio,screenshots,attachments}` when present.

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- `download_bulk(src, dest)` signature accepted. No sync-back over SSH: D-16.129.
