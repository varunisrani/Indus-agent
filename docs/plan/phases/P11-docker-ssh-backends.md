# P11 · Docker and SSH backends

**Weeks:** 1.5 (runs beside P08–P09 on the parallel backend track) · **Track:** B (backend, Python) · **Depends on:** P07 (`CheckpointStore` protocol in `src/indus/backends/checkpoint_api.py` and the `indus checkpoints` CLI, P07-24), P05 (execution contract, local backend, env registry and idle reaper, process registry, `process_manage`, approvals with the isolation flag, env builder, redaction, contract suite) · **Status:** planned

## Goal
Shell commands can run in a hardened Docker container or on a remote host over SSH with the same
contract as the local backend: fresh `bash -c` per call, snapshot, cwd marker, timeout backstop,
interrupts, background processes that notify when they finish, and credentials passed by name only.
Files the agent needs (skills, credential files) are pushed to remote hosts. Every turn that is
about to change files takes a checkpoint first (when enabled) and `/rollback` restores it, and a
session can run in its own git worktree (`indus -w`). This proves the M2 items "a Docker sandbox
run" with "Docker passes values by name only (cmdline check)" and "background process notification
arrives".

Not in this phase: Singularity, Modal, Daytona, Vercel Sandbox and the sync-back of sandbox files
(P21, which extends `remote_sync.py` from here); the egress credential proxy (D15) and Linux memory
caps for background processes (D16), both P3 and proposed for P24; gateway rendering of completion
notices (P15).

Interop note: Docker CLI flags (`run`, `exec`, `inspect`, `ps --filter label=…`, `--cap-drop`,
`--security-opt no-new-privileges`, `--pids-limit`, `--tmpfs`, `--storage-opt size=`, `--shm-size`,
`--network=none`) and OpenSSH client options (`ControlMaster`, `ControlPersist`, `ControlPath`,
`BatchMode`, `StrictHostKeyChecking=accept-new`, `ConnectTimeout`, `SendEnv`) are third-party
interfaces. Check each against the Docker Engine CLI reference and the OpenSSH `ssh_config(5)` man
page for the versions in CI (Docker 27+, Podman 5+, OpenSSH 9+). Read the reference only for
behaviour and edge cases.

## Tasks (21)
| ID | Task | Read in reference | New files | Verify |
|---|---|---|---|---|
| P11-01 | **Docker discovery and probes.** Find the client: `terminal.docker_binary` / `INDUS_DOCKER_BINARY`, else `docker`, else `podman` on PATH; record the runtime name. Probes (each with its own timeout and no network): `version` (10 s) → daemon reachable, else a typed "daemon not running" error with a start hint per OS; cgroup controllers available (resource flags only when present); overlay2-on-XFS-with-pquota check for `--storage-opt` (skipped on macOS); image entrypoint is an s6-style `/init` (`inspect`). Availability probe for the `terminal` tool honours P05's flake grace (a probe failing within 60 s of a success serves last-good; R02.13). Default image: our own sandbox image `<registry>/indus-sandbox:<tag>` (D-16.64), overridable by `terminal.docker_image`; `docker_image_pinned` requires a digest. `indus doctor` row: client, version, daemon, image present. | `tools/environments/docker.py` [1332: `find_docker`, `_ensure_docker_available`, `_cgroup_limits_available`, `_image_uses_init_entrypoint`] | `src/indus/backends/docker/__init__.py`, `src/indus/backends/docker/probe.py`, `tests/unit/backends/docker/test_probe.py` | Fake docker CLI: missing binary → podman fallback; daemon down → typed error + hint; flake grace serves last-good within 60 s (fake clock), honours failure after; s6 image detected; storage probe false on macOS facts. |
| P11-02 | **Docker run arguments (security and resources).** Always: `--cap-drop ALL`, add back `DAC_OVERRIDE`, `CHOWN`, `FOWNER` (plus `SETUID`, `SETGID` unless running as the host user); `--security-opt no-new-privileges` (dropped under `docker_snap_compat`); tmpfs `/tmp` 512 MB `nosuid,exec`, `/var/tmp` 256 MB `noexec,nosuid`, `/run` 64 MB `noexec` (exec and no `--init` when the image uses an s6 `/init`). Resources only when cgroups are available: `--cpus <cpu>`, `--memory <MB>m`, `--pids-limit 2048` (R02.20, D-16.24). `--shm-size 1g` unless set in extra args or configured empty/"0". `--storage-opt size=<MB>m` only when the storage probe passed, else one warning and no quota. Network: `docker_network: false` → `--network=none`; an extra-args network flag that contradicts it → refuse to start with a clear error (repeated `--network` exits 125, R02.20). Container defaults cpu 1, memory 5,120 MB, disk 51,200 MB, persistent true. `docker_run_as_host_user` passes `uid:gid`. Pure function from config + probe facts → argv list. | `tools/environments/docker.py` [1332: `_build_security_args`, `_resource_args`, `_extra_args_*`, `_host_user_args`] | `src/indus/backends/docker/args.py`, `tests/unit/backends/docker/test_args.py` | Argv golden per config combination; pids limit 2048; no `--cpus` without cgroups; contradicting network → error; s6 image → exec `/run`, no `--init`; snap compat drops `no-new-privileges`; shm omitted for "0". |
| P11-03 | **Docker mounts.** User volumes `host:container[:opts]` (malformed entries skipped with a warning; host paths expanded). Host cwd bound at `/workspace` when `docker_mount_cwd_to_workspace` is on or the cwd is a Windows drive path; if `/workspace` is taken use `/host-cwd`, then `/host-cwd-2`, `-3`; reuse an existing mount of the same dir. Skills dirs and configured credential files mounted read-only. Non-persistent mode: tmpfs `/workspace` 10 GB, `/home` 1 GB, `/root` 1 GB. Persistent mode: bind dirs `<home>/sandboxes/docker/<task>/{home,workspace}` (0700). Session keys with colons sanitised before use in `-v` specs (R02.20). Reports `host_paths_mounted: bool` for the isolation flag (P11-06). | `tools/environments/docker.py` [1332: `_mount_args`, `_split_volume_spec`, `_free_host_cwd_mount`, `_readonly_skill_mount_args`] | `src/indus/backends/docker/mounts.py`, `tests/unit/backends/docker/test_mounts.py` | Table: cwd mount taken → `/host-cwd`; duplicate dir reused; malformed volume skipped; colon in task key sanitised; tmpfs set for non-persistent; read-only skill mount present; `host_paths_mounted` true only when a host path is bound. |
| P11-04 | **Docker environment variables by name only.** Forwarded names (`terminal.docker_forward_env`), explicit `terminal.docker_env` and skill-declared passthrough vars go to `docker run`/`exec` as name-only `-e NAME` flags; values live only in the docker client's own process environment, built by the P05 env builder (R02.7). Blocklisted names can never be re-allowed (R02.22). Profile-scoped passthrough values are re-injected on each `exec` because one persistent container may serve several sessions of the profile. Values never appear in argv, logs or error text. | `tools/environments/docker.py` [1332: `_name_only_env_args`, `_normalize_forward_env_names`, `_egress_and_env_args` (env half only)] | `src/indus/backends/docker/env.py`, `tests/unit/backends/docker/test_env.py` | Fake CLI records argv and env: `-e OPENAI_API_KEY` never present (blocklisted); `-e MY_TOKEN` present without `=`; value present only in the client env; argv scan for every secret value returns nothing; exec re-injects updated passthrough value. |
| P11-05 | **Docker environment lifecycle.** Create one long-lived container (`run -d … sleep infinity`, name `indus-<hex8>`), labels `indus-agent=1`, `indus-task-id`, `indus-profile`, `indus-environment=<fingerprint>` where the fingerprint hashes image + mounts + home unless `docker_shared_key` is set. With `docker_persist_across_processes` (default true), first look for a running or stopped container with matching labels and attach (start it if stopped). Exec transport `docker exec [-i] -w <cwd> <id> bash -c <wrapper>` with stdin piped. Snapshot at init per the P05 contract. Env key rules from P05: persistent → one key per profile; non-persistent → per-session key. Cleanup: persistent → leave running for reuse (reaped at next startup by P11-07); otherwise `stop -t 10` then `rm -f` on a worker, detached and drained at exit (15 s). Connection-class failures (daemon gone) → P05 degraded result and eviction. | `tools/environments/docker.py` [1332: `__init__`, `_attach_existing_container`, `_start_container`, `_run_command`, `cleanup`], `website/docs/user-guide/docker.md` [skim; numbers stale per R02.23] | `src/indus/backends/docker/environment.py`, `tests/unit/backends/docker/test_environment.py` | Fake CLI state machine: second process attaches to the labelled container instead of creating; stopped container started; different image → new fingerprint → new container; non-persistent cleanup issues stop then rm; persistent cleanup leaves it; daemon vanishing mid-call → degraded result and next call recreates. |
| P11-06 | **Docker recovery and isolation flag.** Output containing "No such container" or "is not running" → find a reusable labelled container or create a fresh one, re-run the snapshot, retry the command once, and mark the result `environment_recreated` with the P05 warning that background processes and unpersisted files may be gone (R02.19); only with persist-across-processes. Background processes owned by the vanished container are marked `lost`. The backend sets P05's `isolated` flag = (no host path mounted) so dangerous-command prompts are skipped only then; deny globs still apply (R02.6, R02.4). | `tools/environments/docker.py` [1332: recovery path] | `src/indus/backends/docker/recovery.py`, `tests/unit/backends/docker/test_recovery.py` | Fake container removed between calls → one recreate + retry + `environment_recreated`; second failure surfaces the error; background process marked lost; mounted cwd → `isolated=False` and an `rm -rf /workspace` command goes to the approval flow; unmounted → skipped prompt but deny glob still blocks. |
| P11-07 | **Docker orphan reaper.** Once per process at startup (when `docker_orphan_reaper` is on, default true): list containers labelled `indus-agent=1` for this profile, remove those stopped longer than 24 h and running ones whose owning session no longer exists and whose `indus-task-id` has no live process registry entry; never touch containers without our label; run off the main loop with a 30 s budget. | `tools/environments/docker.py` [1332: `reap_orphan_containers`, `_container_finished_at`] | `src/indus/backends/docker/reaper.py`, `tests/unit/backends/docker/test_reaper.py` | Fake CLI with mixed containers: only stale labelled ones removed; unlabelled untouched; other profile's untouched; budget exceeded → stops and logs. |
| P11-08 | **SSH client arguments and control sockets.** Base options: `ControlMaster=auto`, `ControlPersist=300`, `BatchMode=yes`, `StrictHostKeyChecking=accept-new`, `ConnectTimeout=10`, `-p <port>`, `-i <key>` when set. Control socket path `<runtime dir>/indus-ssh/<sha256(user@host:port)[:16]>[-<sha8 of sorted SendEnv names>]` so paths stay under the macOS 104-byte limit; one master per distinct SendEnv name set (a mux master forwards only the names it started with). No multiplexing on Windows. Passthrough values ride the ssh client's environment with `SendEnv NAME` only (R02.7). Pure argv builder + socket path function. | `tools/environments/ssh.py` [302: client flags, socket naming] | `src/indus/backends/ssh/__init__.py`, `src/indus/backends/ssh/client.py`, `tests/unit/backends/ssh/test_client.py` | Argv golden; socket path ≤ 104 bytes for a long host name; different SendEnv sets → different sockets; Windows facts → no Control* options; secret value absent from argv. |
| P11-09 | **SSH environment.** Connect probe `echo ok` (15 s), remote home detection `echo $HOME` (10 s, retried no sooner than every 30 s after a failure), work dir creation (10 s); default cwd `~`; persistent-shell knob separate from local. Exec `ssh … bash -c <wrapper>` with stdin piped and the P05 wrapper/snapshot. Connection-class failures (refused, timeout, host key change, auth failure) → P05 degraded result with a retry hint and eviction; auth failures name the key/agent being used, never print key material. Cleanup closes control sockets (`-O exit`, 5 s). Host key change is never auto-accepted. Approval prompts are not skipped (remote host is not isolated). | `tools/environments/ssh.py` [302: all], `tools/environments/remote_common.py` [99] | `src/indus/backends/ssh/environment.py`, `tests/unit/backends/ssh/test_environment.py` | Fake ssh CLI: probe timeout → degraded; home detection retry spacing 30 s (fake clock); host-key-changed output → degraded with explicit message; cleanup sends `-O exit`; `isolated` is False. |
| P11-10 | **Remote file sync (push).** Transport interface `SyncTransport {upload_one(path, dest) (30 s), upload_bulk(tar) (120 s), download_bulk(src) (120 s), delete(paths) (15 s)}` implemented here for SSH (scp single file, tar over ssh stdin for bulk) and extended by P21 for SDK backends. What syncs: credential files (P11-11), skills dirs, selected cache dirs; paths remapped to the remote home's `.indus/`. Before each command at most every 5 s (forceable by `INDUS_FORCE_FILE_SYNC`): detect changes by (mtime_ns, size) with a racy-mtime sentinel that forces a content hash for files modified within 2 s of the last scan; upload changed, delete removed; manifest updated only after the transport succeeds (transactional). Records what was pushed (path → sha256) for P21's sync-back. SSH never syncs back (upload-oriented). | `tools/environments/file_sync.py` [500: all], `tools/environments/remote_common.py` [99] | `src/indus/backends/remote_sync.py`, `tests/unit/backends/test_remote_sync.py` | Fake transport: first call pushes all; unchanged tree within 5 s → no call; file touched within 2 s with same size and mtime granularity → hashed and pushed; deleted file → delete call; transport failure → manifest unchanged and retried next command. |
| P11-11 | **Credential files registry.** Sources: `terminal.credential_files` (host paths) and skill-declared files; each entry `{host_path, remote_rel, mode}`; refuse paths inside the Indus secret stores and anything not owned by the user; files mounted read-only in Docker (P11-03) and pushed upload-only over SSH (never pulled back, R02.31). Missing files skipped with one warning per session. | `tools/credential_files.py` [409: all] | `src/indus/backends/credential_files.py`, `tests/unit/backends/test_credential_files.py` | Registry table: secret-store path refused; missing file warned once; remap to `.indus/credentials/…`; Docker mount spec read-only; sync marks entries upload-only. |
| P11-12 | **Background processes on non-local backends.** Spawn wrapper through `env.execute` (10 s): `mkdir -p <tmp> && ( nohup bash -lc <cmd> > <log> 2>&1; echo $? > <exit> ) & echo $! > <pid>` with files `indus_bg_<proc>.{log,pid,exit}` in the backend temp dir; no PID → `failed_start`. Poll, log and wait read those files through `env.execute` with **unbounded** capture (R02.2); kill sends TERM then KILL to the remote PID group with the P05 grace. Container recreated or host unreachable → `lost`. Notifications, watch patterns and heartbeats reuse P05's machinery by feeding it the polled output (one poll loop per backend every 2 s while any remote process runs). PTY not available (note in result). | `tools/process_registry.py` [2766: `spawn_via_env` and the non-local poll path] | `src/indus/backends/remote_processes.py`, `tests/unit/backends/test_remote_processes.py` | Fake backend: spawn returns pid; poll reads log tail; exit file → completion event once; 300 kB log read intact; missing pid → `failed_start`; container gone → `lost`; `pty=true` → note. |
| P11-13 | **Retained process receipts.** On completion write an atomic, force-redacted receipt `<home>/logs/process-results/<proc_id>.json` `{id, command (redacted), exit_code, completion_reason, started_at, exited_at, tail (≤ 200,000 chars), owner_session}` **before** the completion event fires; keep the newest 64 and ≤ 7 days; readable through `process_manage poll/log` only from the owning conversation or its compression continuation (a bare id is not authority); no re-run or replay. | `tools/process_registry_results.py` [119: all] | `src/indus/backends/process_receipts.py`, `tests/unit/backends/test_process_receipts.py` | Receipt exists when the completion handler runs (ordering test); 65th receipt prunes the oldest; 8-day-old pruned (fake clock); other session's poll refused; secret in output redacted even with live redaction off. |
| P11-14 | **Process crash recovery.** A checkpoint `<home>/processes.json` (atomic, 0600) lists running background processes `{id, pid, pid_start_time, backend, env_key, log paths, owner_session, notify flags}`. On start, adopt only entries whose PID exists **and** whose start time matches (PID-reuse guard); adopted ones are `detached` (no in-memory output history; poll says so and reads the log file); others become `lost`. `persist_on_release` entries survive session end, not host exit. Used by the gateway (P15) and by the CLI on resume. | `tools/process_registry_checkpoint.py` [159: all] | `src/indus/backends/process_checkpoint.py`, `tests/unit/backends/test_process_checkpoint.py` | Real sleeping child process: write checkpoint, new registry adopts it as detached; recycled PID with a different start time → lost; corrupt checkpoint → ignored with a warning; notify-on-complete still fires for an adopted process. |
| P11-15 | **Checkpoint store.** One shared shadow git store `<home>/checkpoints/store/` with per-project refs `refs/indus/<sha16(workdir)>`, per-project index files `indexes/<hash16>`, project metadata `projects/<hash16>.json` `{workdir, created_at, last_touch}`, default excludes in `info/exclude` (`.git`, `node_modules`, `.venv`, `__pycache__`, build outputs, files over `checkpoints.max_file_size_mb` 10). Every git call runs with `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` pointing at the store so nothing touches the user's repo; bounded timeouts (30 s per call). `snapshot(workdir, reason) -> commit | Skipped(reason)`: skip projects over 50,000 files; nested repos noted as not captured. Ledger per project ≤ 2,000 entries. Commit hashes and file paths validated before use. Local backend only (container and remote backends skip with a reason). The store **implements P07's `CheckpointStore` protocol** (`backends/checkpoint_api.py`); it does not redefine it. If the protocol lacks a method this phase needs (for example `restore_file` or `prune(options)`), extend that file in this task and record the change in the note. | `tools/checkpoint_manager.py` [1673: header docstring (the spec), `_git_env`, `_init_store`, `_register_project`, snapshot function, validators] | `src/indus/backends/checkpoints/__init__.py`, `src/indus/backends/checkpoints/store.py`, `tests/unit/backends/checkpoints/test_store.py` | Temp project: snapshot creates a ref, user's `.git` untouched (status and refs unchanged); 50,001 files → skipped; excludes honoured; two projects share objects (store size grows less than 2×); invalid hash rejected. |
| P11-16 | **Checkpoint trigger.** Enabled by `checkpoints.enabled` (default false) or `--checkpoints` / `/checkpoints on`. Before the first file-mutating operation in a turn — `write_file`, `patch`, or a `terminal` command the P05 detector flags as destructive (rm, mv over, truncating redirect, `git reset/checkout/clean`) — take one snapshot of the session's working dir; at most once per turn; `checkpoints.min_interval_hours` (24) governs auto-prune, not snapshots; `max_snapshots` 20 per project (oldest dropped). Failure to snapshot never blocks the tool (warning in the result once per turn). Hook point: a pre-mutation callback in the P05 file tools and terminal plan. | `tools/checkpoint_manager.py` [1673: trigger call sites], `website/docs/user-guide/checkpoints-and-rollback.md` [skim] | `src/indus/backends/checkpoints/trigger.py`, `tests/unit/backends/checkpoints/test_trigger.py` | Two writes in one turn → one snapshot; next turn → another; non-destructive command → none; disabled → none; snapshot error → tool still runs, one warning. |
| P11-17 | **Rollback.** `list(workdir)` (newest first, with turn/session and changed-file count), `diff(n)` (unified diff from checkpoint n to the working tree), `restore(n)` (takes a safety snapshot first, then restores tracked files and removes files created since, never touching excluded paths), `restore_file(n, path)` (one file). Slash `/rollback [N] [--all]`, `/rollback diff N`, `/rollback N <file>` (shared registry; CLI, TUI, desktop, gateway with confirmation when `approvals.destructive_slash_confirm`). | `tools/checkpoint_manager.py` [1673: restore and diff functions], `hermes_cli/checkpoints.py` [221] | `src/indus/backends/checkpoints/rollback.py`, `tests/unit/backends/checkpoints/test_rollback.py` | Edit → snapshot → edit → `restore(1)` returns bytes; created file removed; safety snapshot listed after restore; `restore_file` touches one file only; diff output matches `git diff` of the two trees. |
| P11-18 | **Checkpoint pruning.** Prune: delete projects whose workdir is gone (unless `--keep-orphans`) or untouched longer than `retention_days` (7), then `git gc --prune=now`, then drop oldest checkpoints until the store is under `max_total_size_mb` (500); auto-prune at most once per `min_interval_hours` (24) via a `.last_prune` marker; explicit `prune` ignores the marker. Exposed through the `CheckpointStore` protocol methods (`status`, `list`, `prune`, `clear`) so the P07-24 `indus checkpoints` CLI, written against the fake, works unchanged on the real store; this task wires the real store into the CLI's store factory and runs P07's CLI tests against it. No legacy-store commands (D-16.3). | `tools/checkpoint_pruning.py` [149], `tools/checkpoint_maintenance.py` [614: prune order, marker], `hermes_cli/checkpoints.py` [221] | `src/indus/backends/checkpoints/prune.py`, `tests/unit/backends/checkpoints/test_prune.py`, `tests/integration/backends/test_checkpoints_cli_real_store.py` | Orphan project removed; stale (8 days, fake clock) removed; size cap drops oldest; marker prevents a second auto-prune within 24 h; P07's CLI cases rerun against the real store (status, list, prune, clear) all pass. |
| P11-19 | **Session worktrees (core).** `create(repo_root, name?)`: base ref = current branch's upstream (fetched, 5 s timeout) else the remote default branch else `HEAD`; path `<repo>/.worktrees/<name>` (name default `indus-<YYYYmmdd-HHMMSS>-<hex4>`), branch `indus/<name>`; ensure `.worktrees/` is in the repo `.gitignore`; copy files listed in a `.worktreeinclude` file (e.g. `.env`) into the new tree; failed add cleans up the half-made tree and branch. Registry `<home>/worktrees.json` `{path, repo, branch, session_id, created_at}`. Removal is allowed only when nothing unrecoverable would be lost: uncommitted tracked changes, untracked non-ignored files, commits not on any other ref, embedded repositories below the tree, or per-worktree refs → keep (fail safe toward keeping); protected branches `main`, `master`, `develop`, `dev`, `trunk` never deleted. `prune(dry_run)` removes clean, merged trees older than 24 h and reports the rest with reasons. All git calls bounded (10 s default) with a non-interactive git env. | `hermes_cli/worktree_ops.py` [1191: base resolution, add, cleanup], `hermes_cli/worktree_retention.py` [skim: removal blockers], `hermes_cli/worktree_gc.py` [434: protected branches, prune], `hermes_cli/worktree_registry.py` [70] | `src/indus/backends/worktrees.py`, `tests/unit/backends/test_worktrees.py` | Temp repos: create from upstream; `.gitignore` updated once; `.worktreeinclude` file copied; dirty tree kept with reason; tree with an unpushed commit kept; clean merged tree older than 24 h pruned; `main` never deleted; failed add leaves no branch. |
| P11-20 | **Worktree surfaces.** `indus -w [name]` / `indus chat --worktree` starts the session inside a new worktree (session row records the worktree; resume re-enters it); `/worktree new [name]\|list\|prune [--dry-run]` (CLI only). File tools attach a warning when an edited path lies outside the session's worktree (e.g. the main checkout). On session end the worktree is kept (it is the user's work) and listed by `prune`. | `hermes_cli/worktree_cmd.py` [202], `hermes_cli/worktree_exit.py` [skim] | `src/indus/cli/commands/worktree.py`, `tests/unit/cli/commands/test_worktree.py` | CliRunner `-w` creates a tree and the session cwd is inside it; resume re-enters; editing a path in the main checkout returns the divergence warning; `/worktree prune --dry-run` lists without deleting. |
| P11-21 | **Contract suite for Docker and SSH + live lanes.** Add `docker` and `ssh` parameters to the P05 execution-contract suite (`tests/contract/backends/`) using the fake CLIs: snapshot and cwd marker (odd output, no trailing newline, binary noise), per-session variables never leaking through the snapshot (R02.1), timeout backstop rc 124 + flag (R02.3), interrupt rc 130, stdin, bounded vs unbounded capture, `environment_recreated` warning, background start/poll/kill, degraded result on connection failure, values never in argv (R02.7 cmdline check). Gated live lanes: marker `docker` (real Docker in CI) and `ssh` (a throwaway `sshd` container); same suite runs against the real thing. | `tests/tools/` [docker args and process registry tests: edge-case lists only] | `tests/contract/backends/test_docker_ssh_contract.py`, `tests/integration/backends/test_docker_live.py`, `tests/integration/backends/test_ssh_live.py` | All contract cases green for `local`, `docker`, `ssh` with fakes; live lanes green on the CI runner with Docker; `/proc/<pid>/cmdline` of the docker client contains no secret value (live lane). |

## Files to create (50)
| Path (under `indus-agent/`) | What it holds | Task |
|---|---|---|
| `src/indus/backends/docker/__init__.py` | package, backend registration | P11-01 |
| `src/indus/backends/docker/probe.py` | client discovery and probes | P11-01 |
| `tests/unit/backends/docker/test_probe.py` | probe cases | P11-01 |
| `src/indus/backends/docker/args.py` | security/resource argv | P11-02 |
| `tests/unit/backends/docker/test_args.py` | argv goldens | P11-02 |
| `src/indus/backends/docker/mounts.py` | mounts | P11-03 |
| `tests/unit/backends/docker/test_mounts.py` | mount table | P11-03 |
| `src/indus/backends/docker/env.py` | names-only env | P11-04 |
| `tests/unit/backends/docker/test_env.py` | env cases | P11-04 |
| `src/indus/backends/docker/environment.py` | Docker environment | P11-05 |
| `tests/unit/backends/docker/test_environment.py` | lifecycle cases | P11-05 |
| `src/indus/backends/docker/recovery.py` | recreate + isolation flag | P11-06 |
| `tests/unit/backends/docker/test_recovery.py` | recovery cases | P11-06 |
| `src/indus/backends/docker/reaper.py` | orphan reaper | P11-07 |
| `tests/unit/backends/docker/test_reaper.py` | reaper cases | P11-07 |
| `src/indus/backends/ssh/__init__.py` | package, backend registration | P11-08 |
| `src/indus/backends/ssh/client.py` | ssh argv, control sockets | P11-08 |
| `tests/unit/backends/ssh/test_client.py` | argv cases | P11-08 |
| `src/indus/backends/ssh/environment.py` | SSH environment | P11-09 |
| `tests/unit/backends/ssh/test_environment.py` | environment cases | P11-09 |
| `src/indus/backends/remote_sync.py` | push sync + transport interface | P11-10 |
| `tests/unit/backends/test_remote_sync.py` | sync cases | P11-10 |
| `src/indus/backends/credential_files.py` | credential file registry | P11-11 |
| `tests/unit/backends/test_credential_files.py` | registry cases | P11-11 |
| `src/indus/backends/remote_processes.py` | non-local background spawn/poll | P11-12 |
| `tests/unit/backends/test_remote_processes.py` | remote process cases | P11-12 |
| `src/indus/backends/process_receipts.py` | retained receipts | P11-13 |
| `tests/unit/backends/test_process_receipts.py` | receipt cases | P11-13 |
| `src/indus/backends/process_checkpoint.py` | crash recovery | P11-14 |
| `tests/unit/backends/test_process_checkpoint.py` | adoption cases | P11-14 |
| `src/indus/backends/checkpoints/__init__.py` | package | P11-15 |
| `src/indus/backends/checkpoints/store.py` | shadow git store | P11-15 |
| `tests/unit/backends/checkpoints/test_store.py` | store cases | P11-15 |
| `src/indus/backends/checkpoints/trigger.py` | once-per-turn trigger | P11-16 |
| `tests/unit/backends/checkpoints/test_trigger.py` | trigger cases | P11-16 |
| `src/indus/backends/checkpoints/rollback.py` | list/diff/restore | P11-17 |
| `tests/unit/backends/checkpoints/test_rollback.py` | rollback cases | P11-17 |
| `src/indus/backends/checkpoints/prune.py` | pruning | P11-18 |
| `tests/unit/backends/checkpoints/test_prune.py` | prune cases | P11-18 |
| `tests/integration/backends/test_checkpoints_cli_real_store.py` | P07 CLI against the real store | P11-18 |
| `src/indus/backends/worktrees.py` | worktree create/classify/prune | P11-19 |
| `tests/unit/backends/test_worktrees.py` | worktree cases | P11-19 |
| `src/indus/cli/commands/worktree.py` | `-w`, `/worktree` | P11-20 |
| `tests/unit/cli/commands/test_worktree.py` | CLI cases | P11-20 |
| `tests/contract/backends/test_docker_ssh_contract.py` | contract params for docker/ssh | P11-21 |
| `tests/integration/backends/test_docker_live.py` | live Docker lane (marker `docker`) | P11-21 |
| `tests/integration/backends/test_ssh_live.py` | live SSH lane (marker `ssh`) | P11-21 |
| `tests/fakes/backend/docker_cli.py` | fake `docker` executable with container state in a JSON file, argv/env recorder | P11-01 |
| `tests/fakes/backend/ssh_cli.py` | fake `ssh`/`scp` executables running commands locally under a fake remote home | P11-08 |
| `tests/fixtures/backends/sshd/Dockerfile` | throwaway sshd image for the live lane | P11-21 |

## Files to update (11)
| Path (under `indus-agent/`) | Change | Task |
|---|---|---|
| `src/indus/backends/registry.py` (P05) | register `docker` and `ssh` factories with flags (isolated-by-mounts, container paths, remote sync, stdin pipe, persistence) | P11-05, P11-09 |
| `src/indus/backends/processes.py` (P05) | route non-local spawns to `remote_processes`; write receipts before completion events; load the crash checkpoint at start | P11-12, P11-13, P11-14 |
| `src/indus/tools/builtin/process_manage.py` (P05) | receipts for exited processes; `detached` note | P11-13, P11-14 |
| `src/indus/tools/builtin/write_file.py`, `src/indus/tools/builtin/patch.py`, `src/indus/tools/builtin/terminal.py` (P05) | pre-mutation checkpoint callback; worktree divergence warning | P11-16, P11-20 |
| `src/indus/cli/app.py` (P06) | `-w/--worktree`, `--checkpoints` flags; lazy group `worktree` | P11-16, P11-20 |
| `src/indus/backends/checkpoint_api.py` (P07) | only if the protocol needs extending (say so in the note); otherwise untouched | P11-15 |
| `src/indus/backends/checkpoint_api.py` (P07-24) | return the real shadow-git store instead of the fake when available | P11-18 |
| `src/indus/service/commands/registry.py` (P06-08) | `/rollback`, `/worktree`, `/checkpoints on\|off` | P11-17, P11-20 |
| `src/indus/foundation/config/schema.py` (P01) | `terminal.docker_*`, `terminal.ssh_*`, `terminal.credential_files`, `terminal.container_*`, `checkpoints.*`, `worktrees.*` with the defaults above | P11-02, P11-08, P11-15 |
| `src/indus/cli/commands/doctor.py` (P07) | Docker and SSH rows | P11-01, P11-09 |
| `docs/plan/phases/00-layout-and-conventions.md` | add `backends/docker/`, `backends/ssh/`, `backends/checkpoints/`, `backends/worktrees.py` to the tree | P11-01 |

## Fixtures and fakes (3)
Listed in the files table: `tests/fakes/backend/docker_cli.py`, `tests/fakes/backend/ssh_cli.py`,
`tests/fixtures/backends/sshd/Dockerfile`. Fake CLIs are installed on PATH by a fixture in
`tests/contract/backends/conftest.py` (P05 file, extended).

## Task notes (21)
`docs/notes/P11/<slug>.md`: `docker-probe` (01), `docker-args` (02), `docker-mounts` (03),
`docker-env` (04), `docker-environment` (05), `docker-recovery` (06), `docker-reaper` (07),
`ssh-client` (08), `ssh-environment` (09), `remote-sync` (10), `credential-files` (11),
`remote-processes` (12), `process-receipts` (13), `process-checkpoint` (14), `checkpoint-store` (15),
`checkpoint-trigger` (16), `rollback` (17), `checkpoint-prune` (18), `worktrees` (19),
`worktree-surfaces` (20), `backend-contract-docker-ssh` (21).

## Run until green
```bash
cd indus-agent
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run pyright && uv run lint-imports
uv run pytest -q tests/unit/backends tests/unit/cli/commands/test_worktree.py tests/contract/backends \
  tests/integration/backends/test_checkpoints_cli_real_store.py
uv run pytest -q -m docker tests/integration/backends/test_docker_live.py   # gated: needs Docker
uv run pytest -q -m ssh tests/integration/backends/test_ssh_live.py         # gated: needs Docker for sshd
uv run pytest -q
uv run python -m tooling.checks --require-ref
```
On any failure, fix the cause and rerun from `uv sync`. Never skip a contract case for one backend,
loosen a timeout to pass, or hand-edit a fake's recorded state.

## Compare checklist (reopen the reference after writing)
- [ ] Docker flags: caps, no-new-privileges, tmpfs sizes and options, pids 2048, shm, storage probe, network conflict refusal (R02.20).
- [ ] Env by name only; profile passthrough re-injected per exec (R02.7, R02.22).
- [ ] Labels, fingerprint, attach/start, persistent containers left running and reaped next start (D-16.24, R02.23).
- [ ] Recovery retries once and flags `environment_recreated`; isolation decided by mounts (R02.6, R02.19).
- [ ] SSH options, socket naming under 104 bytes, one master per SendEnv set, timeouts 15/10/10/30/120.
- [ ] Sync every ≤ 5 s, racy-mtime sentinel, transactional manifest; credential files upload-only.
- [ ] Remote background via files, unbounded capture, lost on vanish; receipts written before completion; PID + start-time adoption.
- [ ] Checkpoints: shared store, git env isolation, 50,000 files, once per turn, 7 d / 500 MB / 24 h marker.
- [ ] Worktrees fail safe toward keeping; protected branches; `.worktrees/` ignored.
- [ ] Intended differences (our labels, image, socket dir, no legacy checkpoint cleanup) have D-ids.

## Traps covered
| Trap | Pinned by test in |
|---|---|
| R02.1 per-session vars through the snapshot | P11-21 |
| R02.2 bounded vs full capture (remote logs) | P11-12 |
| R02.3 timeout backstop | P11-21 |
| R02.4 floors before bypass on containers (deny globs) | P11-06 |
| R02.6 isolation by mounts | P11-03, P11-06 |
| R02.7 credentials on the command line | P11-04, P11-08, P11-21 |
| R02.8 profile scoping of env keys and containers | P11-05, P11-07 |
| R02.12 kill order for remote process groups | P11-12 |
| R02.13 flapping availability probes | P11-01 |
| R02.17 completions only where deliverable (remote) | P11-12 |
| R02.19 container recreated silently | P11-06 |
| R02.20 Docker self-inconsistencies | P11-02, P11-03 |
| R02.22 secrets in child processes (docker/ssh clients) | P11-04 |
| R02.23 docs vs code (persistent containers, pids) | P11-05, P11-02 |
| R02.31 credential files upload-only (sync-back itself in P21-10) | P11-11 |

## Acceptance
| Feature | Planned ids |
|---|---|
| D3 Docker backend | ACC-D3-1 (hardened run, names-only env), ACC-D3-2 (reuse by label), ACC-D3-3 (recovery flag), ACC-D3-4 (orphan reaper) |
| D4 SSH backend | ACC-D4-1 (multiplexed run), ACC-D4-2 (home detection + sync) |
| D12 remote file sync | ACC-D12-1 |
| D13 degraded backend | ACC-D13-1 (Docker), ACC-D13-2 (SSH) |
| C6 background processes (non-local, receipts, recovery) | ACC-C6-3 (remote notify), ACC-C6-4 (adoption after restart) |
| C9 retained receipts | ACC-C9-1 |
| C16 checkpoints and rollback | ACC-C16-1 (once per turn), ACC-C16-2 (restore + per-file) |
| I32 `indus checkpoints` | ACC-I32-1 is P07-24's; P11-18 adds the real-store run of the same cases |
| B29 session worktrees | ACC-B29-1 (`-w`), ACC-B29-2 (prune keeps dirty) |

## Done when
- Unit and contract suites green for `local`, `docker`, `ssh`; both live lanes green on the CI
  runner; `tooling.checks --require-ref` clean.
- Demo (M2 slice): `indus` with `terminal.backend: docker` runs "create hello.py and run it" in the
  hardened container (approval prompt skipped only because nothing is mounted), a background
  `sleep 5 && echo done` notifies on completion with a new turn, `ps`/`cmdline` inspection shows no
  secret values; the same with `terminal.backend: ssh` against the throwaway sshd; with
  `--checkpoints`, an edit followed by `/rollback 1` restores the file; `indus -w` opens a session in
  a fresh worktree.
</content>
</invoke>
