# Task: SSH environment (P11-09) Phase: 11 Owner: P11 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/ssh.py` (all),
`tools/environments/remote_common.py` (passthrough resolution, client env, capture runner). Spec:
research `02-tools.md` §3.8–§3.11, §3.30; phase file P11-09. Closed before writing.
Evidence: none.

What it does (own words): runs the shared execution contract (P05: fresh `bash -c` per call,
snapshot, cwd marker, timeout backstop, interrupts, piped stdin) on a remote host through the
OpenSSH client. Opening an environment: one connect probe (`echo`, 15 s), then remote home
detection (`echo $HOME`, 10 s), then the remote work dirs (`~/.indus/{skills,credentials,cache}`,
10 s), then the first file push (P11-10) and the snapshot. Commands start in `~` by default.
Every command is `ssh <opts> target bash -c <wrapper>` with stdin piped; passthrough variables
ride the client environment with `SendEnv` names only. When the remote home could not be
detected, a guess is used and detection is retried before later commands, never sooner than
30 s after the last failure. Cleanup closes every control master this environment used
(`ssh -O exit`, 5 s each) and removes the socket files. SSH never syncs files back.

Connection-class failures — ssh exiting 255 with a refused/timed-out/unresolvable/closed
connection, a host key mismatch, or an authentication failure — become
`EnvironmentConnectionError`, so the registry answers `degraded` with a retry hint and evicts the
environment. A changed host key is never accepted: the message says so and how to clear the old
key. An authentication failure names the key file (or "your ssh agent / default keys") but never
prints key material. The remote host is not isolated: approval prompts stay on.

Inputs → outputs: scope config (`terminal.ssh_*`, `terminal.cwd`, `terminal.timeout`,
`terminal.ssh_persistent_shell`), the command → `ExecResult` or `EnvironmentConnectionError`.

Files: `src/indus/backends/ssh/environment.py`, `tests/unit/backends/ssh/test_environment.py`.
Shared: `src/indus/backends/registry.py` (the `ssh` row), `src/indus/cli/doctor/checks.py` (SSH
lines in the backend check).

Edge cases / failure modes:
- `ssh` missing on PATH → `BackendUnavailable` (status `disabled`), no host → same.
- probe timeout → degraded; probe refused → degraded with the host/port named.
- home detection fails → `/root` for root, else `/home/<user>`; retry spacing 30 s on the clock.
- a remote command that itself exits 255 but prints nothing ssh-like is a normal result.
- cleanup twice, cleanup while sockets are already gone → quiet.
- the snapshot sees the passthrough names excluded, so one profile's value is never frozen into a
  snapshot another session sources.

Our design differences / decisions:
- built on `SpawnedEnvironment` (the local backend's base), with the local ssh client as the
  process: interrupts and timeouts stop the local client with the same ordered kill. (Like the
  reference, a killed client does not guarantee the remote command stops.)
- the async subprocess runner is injectable for tests; the shipped one uses `subprocess.run` on
  the blocking pool.
- no sync-back (upload-oriented, R02.31); the reference syncs back on cleanup.

Tests we will write:
- fake ssh CLI (`tests/fakes/backend/ssh_cli.py`) running commands locally under a fake remote
  home: run a command, cwd persists, stdin piped, passthrough value reaches the remote shell but
  not the argv.
- probe timeout → degraded through the registry; host-key-changed output → degraded with the
  explicit message; auth failure names the key; home detection retry spacing (fake clock);
  cleanup sends `-O exit`; `flags.isolated` is False.

## Decisions for the lead
- Retry hint texts are ours; the registry already turns the error into `degraded`.

Compare checklist (filled after the rewrite; reopened `tools/environments/ssh.py`,
`tools/environments/remote_common.py`):
- [x] probe 15 s, home 10 s (guess `/root` or `/home/<user>`), dirs 10 s, `-O exit` 5 s + unlink (tests: `tests/unit/backends/ssh/test_environment.py::test_probe_timeout_is_degraded`, `::test_cleanup_closes_every_master`)
- [x] passthrough via SendEnv names + client env; names kept out of the snapshot (test: `tests/unit/backends/ssh/test_environment.py::test_passthrough_value_rides_the_client_env_only`)
- [x] connection failures (refused, host key, auth) → degraded, evicted, rebuilt next call (tests: `::test_refused_connection_is_degraded`, `::test_host_key_change_is_degraded_never_accepted`, `::test_auth_failure_names_the_key_not_its_contents`)
- [x] first sync forced at open, rate-limited before commands (test: `::test_open_pushes_skills_and_credential_files`)
- [x] gap found: a command's own exit 255 must not count as a lost link — only ssh's own messages do (test: `::test_runs_commands_and_keeps_cwd_and_exports`)
- [x] doctor rows without connecting (test: `::test_doctor_rows`)
- Deliberate differences: home detection is retried (≥ 30 s apart) instead of guessed once; no sync-back at cleanup (upload-oriented, R02.31); the client env is the scope's child env (blocked names gone) instead of the whole process env; the remote snapshot file is removed at cleanup.
- Not taken: sudo over SSH (the reference probes passwordless sudo remotely). The P05 password cache is keyed by profile and session, not by host, so feeding it to a remote host could hand the local password to another machine; left off until the cache is keyed per host (follow-up).

## Status

Done (2026-10-18). `src/indus/backends/ssh/environment.py`, `tests/unit/backends/ssh/test_environment.py`.
Shared: `src/indus/backends/registry.py` (`ssh` row, flags not isolated), `src/indus/cli/doctor/checks.py`
(ssh branch of the backend check), `tests/unit/backends/test_registry.py` (default rows now include
`ssh`), `tests/contract/backends/conftest.py` (ssh row left out of the P05 suite until P11-21 adds it
with the fake CLI). Known limit: an interrupted or timed-out command stops the local ssh client; the
remote command may keep running (as in the reference).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- SECURITY (lead item): the sudo password cache and lookup are keyed by backend environment (`SudoSupport(target=...)`, cache key `(home, session, target)`). A host-local hook refuses to rewrite on an environment that is not host-local. SSH and Docker get a hook only through `sudo.backend_sudo` when `ssh_sudo_password` / `docker_sudo_password` is stored; that hook reads only its own secret and never prompts. Without it no rewrite happens on those backends: the local password never reaches a remote host or a container. Tests: `tests/unit/backends/test_sudo.py` (`test_host_hook_never_rewrites_on_a_remote_environment`, `test_cache_is_keyed_by_backend_environment`, `test_no_backend_hook_without_a_backend_password`, `test_backend_password_is_the_only_source_and_never_prompts`, `test_backend_hook_without_answer_does_not_prompt`), `tests/unit/backends/ssh/test_environment.py::test_local_sudo_password_never_reaches_the_remote_host`, `tests/unit/backends/docker/test_recovery.py::test_host_sudo_password_never_enters_the_container`. D-16.134.
- Interrupt and timeout now also stop the remote command: each script writes its remote shell's pid to `<temp>/indus-exec-<hex>.pid`; a stop sends TERM to that process group over the connection (5 s bound), then the ordered kill of the local client, then KILL (test: `test_timeout_also_stops_the_remote_command`). A link that is down gets no remote signal, so the remote command can then keep running until it ends on its own (recorded).
- Tests no longer write into the shared `/tmp`: `tests/unit/backends/ssh/conftest.py` points the remote temp dir at the test's directory (`tests.fakes.backend.ssh_cli.redirect_remote_tmp`); before, an environment evicted while the fake link was down left its snapshot in `/tmp`.
- D-16.127, D-16.128, D-16.129.
