# Task: SSH client arguments and control sockets (P11-08) Phase: 11 Owner: P11 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/ssh.py` (client
flags, control socket naming, per-SendEnv sockets, Windows switch). Spec: research `02-tools.md`
§3.10 (SSH), §6 R02.7; phase file P11-08. Closed before writing.
Evidence: none.

What it does (own words): turns an SSH target (host, user, port, key) into the exact argv for the
OpenSSH client, and decides where the multiplexing control socket lives. Every connection to the
same target reuses one master connection (`ControlMaster=auto`, kept 300 s after the last use), so
a command costs one round trip instead of a new handshake. A master only forwards the environment
names it was itself started with, so each distinct set of `SendEnv` names gets its own master and
socket. Values of passthrough variables never appear on the command line: the argv carries
`-o SendEnv=NAME` and the value sits in the ssh client's own environment (R02.7).

Inputs → outputs: `SshTarget(host, user, port, key_path)`, the sorted SendEnv names, platform
facts, the runtime dir → argv lists for `ssh`, `scp`, `ssh -O exit`; the control socket path.

Files: `src/indus/backends/ssh/__init__.py`, `src/indus/backends/ssh/client.py`,
`tests/unit/backends/ssh/test_client.py`, `tests/fakes/backend/ssh_cli.py`.
Shared: `src/indus/foundation/config/schema.py` (`terminal.ssh_host`, `ssh_user`, `ssh_port`,
`ssh_key`, `ssh_persistent_shell`).

Edge cases / failure modes:
- long host names or a deep runtime dir: the socket name is a hash (16 hex of SHA-256 of
  `user@host:port`, plus `-` and 8 hex of the sorted SendEnv names), and the dir falls back to a
  short one when `XDG_RUNTIME_DIR` is too long, so the path stays under the macOS 104-byte limit
  with room for the random suffix ssh adds while it creates the socket.
- the socket dir exists but belongs to someone else or is group/world writable → not used
  (another user could hijack the master); we fall back to no multiplexing for that target.
- Windows client: no Unix-socket multiplexing at all (no `Control*` options).
- empty user → `host` alone (ssh picks the local user); port always passed with `-p`.
- names that are not shell identifiers are dropped from SendEnv.

Our design differences / decisions:
- socket dir `<runtime dir>/indus-ssh/` where runtime dir = `XDG_RUNTIME_DIR` (from the scope env,
  absolute, ≤ 50 chars), else the profile's short socket dir (`foundation.layout.socket_dir`), the
  same rule every other Indus socket uses. Sockets for different SendEnv sets are named
  `<target16>-<names8>` (the reference glues them without a separator and shortens the target).
- `-p <port>` is always given (golden argv is stable; the reference omits it for 22).
- no `.sock` suffix: the name is never globbed, the environment remembers which sockets it used.
- pure functions only; the environment (P11-09) owns processes.

Tests we will write:
- argv golden (default and with key, port, SendEnv); scp argv uses `-P`.
- socket path ≤ 104 bytes (with ssh's temp suffix) for a 250-char host name and a long runtime dir.
- different SendEnv sets → different sockets; same set in another order → same socket.
- Windows facts → no `ControlMaster`/`ControlPath`/`ControlPersist`.
- a secret value placed in the client env never appears in the argv.

## Decisions for the lead
- Config key names: `terminal.ssh_host`, `terminal.ssh_user`, `terminal.ssh_port` (22),
  `terminal.ssh_key` (""), `terminal.ssh_persistent_shell` (true). Rename in schema.py if the plan
  prefers other names.

Compare checklist (filled after the rewrite; reopened `tools/environments/ssh.py`):
- [x] options ControlMaster=auto, ControlPersist=300, BatchMode, accept-new, ConnectTimeout=10, `-p`, `-i` (test: `tests/unit/backends/ssh/test_client.py::test_argv_golden`)
- [x] socket name = 16 hex of SHA-256 of `user@host:port`, under the 104-byte limit with ssh's temp suffix (test: `tests/unit/backends/ssh/test_client.py::test_socket_path_fits_the_macos_limit`)
- [x] one master per SendEnv name set; order of names does not matter (test: `tests/unit/backends/ssh/test_client.py::test_each_sendenv_set_gets_its_own_socket`)
- [x] no multiplexing on Windows (test: `tests/unit/backends/ssh/test_client.py::test_windows_has_no_multiplexing`)
- [x] values never in argv (test: `tests/unit/backends/ssh/test_client.py::test_secret_value_never_in_argv`)
- [x] gap found: the reference trusts a shared temp dir for sockets — we refuse a socket dir owned by someone else and tighten a loose one (test: `tests/unit/backends/ssh/test_client.py::test_socket_dir_is_private`)
- Deliberate differences: the reference names SendEnv sockets `<target[:8]><names8>.sock` and globs for them at cleanup; we name them `<target16>-<names8>` and remember the sockets we used. `-p` is always passed. Socket dir: `XDG_RUNTIME_DIR` / the profile socket dir / `/tmp` (the reference: the system temp dir).
- Not taken: the reference's per-instance probe socket (we have no probe-only environments).

## Status

Done (2026-10-18). `src/indus/backends/ssh/{__init__,client,runner}.py`, `tests/fakes/backend/ssh_cli.py`,
`tests/unit/backends/ssh/test_client.py`. Shared: `src/indus/foundation/config/schema.py` (ssh keys and
`terminal.credential_files`), `tests/unit/foundation/config/test_schema.py` (defaults table).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- D-16.127 (socket naming and private dir). Config names accepted as proposed.
