# Task: Native Windows gateway daemon and control channel (owner request) Phase: 25 Owner: Claude Code

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none. The reference is not on this
machine, and this is a port of our own daemon to another OS, not new behaviour taken from it.
Evidence: none.

What it does (own words): on native Windows the desktop app could find a checkout's `indus` but
never got past boot: `gateway ensure` refused (`unsupported`, "the gateway control socket needs
POSIX"), and the daemon itself did not even import (`fcntl`). The owner asked for the desktop app
to run fully natively, without WSL2, which amends D-16.55 for the gateway. After this task:

- The control channel is a named pipe on Windows. The daemon serves `\\.\pipe\indus-gw-<random>`
  through the Proactor loop and writes the name into the existing pointer file
  `<home>/gateway.sock.path`; the Python client and the desktop's `control.ts` follow the pointer.
  Same wire (one JSON line each way), same limits and timeouts.
- Peer checks use Windows user SIDs instead of uids: the server reads the client's pid from the
  pipe and compares that process's token user with its own (owner-only verbs answer only a match);
  the Python client checks the pipe server's process the same way before writing a byte
  (`foreign_listener` otherwise). A pid whose token cannot be read is "unknown", exactly like an
  OS that reports no peer credential on POSIX.
- Process probes stop using `os.kill(pid, 0)`, which on Windows terminates the process it probes.
  `indus.platform.winproc` answers liveness (`GetExitCodeProcess`), a start fingerprint (creation
  time from `GetProcessTimes`) and the owner SID.
- Locks: the remaining `fcntl.flock` users that the daemon imports (the instance lock, the
  maintenance ownership lock, the multiplex host lock, the Matrix store lock) take the repo's
  one-byte `msvcrt.locking` range at offset 0 on Windows, like `session_lock` and `fts` already do.
- Signals: Windows has no SIGUSR1/SIGUSR2/SIGKILL. The daemon installs only what exists (SIGINT,
  SIGTERM, SIGBREAK through `signal.signal` when the loop cannot add handlers). Stop and the
  version-skew restart go through the owner-only `stop` control verb (`restart: true` for a
  restart); `--force` and the takeover fall back to `TerminateProcess` (what `os.kill` does there).
- Detached starts (`gateway ensure`, `serve ensure`): no double fork on Windows. The child is
  started directly in a new process group with no console window, breaking away from the
  caller's job object when the job allows it, stdout/stderr appended to the stdio log as before.

Inputs → outputs: `indus gateway run` / `ensure` / `stop` / `status`, `indus serve ensure`, the
desktop's `controlQuery` → the same JSON verdicts and answers as on Linux.

Files: `src/indus/platform/winproc.py` (new), `src/indus/service/gateway_control.py`,
`src/indus/gateway/control_socket.py`, `src/indus/service/remote/profile_guard.py`,
`src/indus/state/gateway_runtime.py`, `src/indus/gateway/daemon/liveness.py`,
`src/indus/gateway/daemon/lifecycle.py`, `src/indus/service/gateway_ensure.py`,
`src/indus/service/maintenance_gate.py`, `src/indus/gateway/multiplex/host.py`,
`src/indus/gateway/adapters/matrix/crypto.py`, `src/indus/web/serve_runtime.py`,
`src/indus/cli/commands/gateway.py`, the other `os.kill(pid, 0)` probes that run on Windows,
`apps/desktop/electron/backend/control.ts`, tests beside them,
`docs/user/getting-started/windows.md`.

Edge cases / failure modes:
- A stale pointer after a crash: the pipe no longer exists, so a client gets `no_socket`; the next
  daemon overwrites the pointer after checking nobody answers on the old name.
- Pipe squatting: the first pipe instance is created with `FILE_FLAG_FIRST_PIPE_INSTANCE`
  (asyncio does this), and the name is random, so another user cannot pre-create ours; a client
  still refuses a server process owned by another user.
- A pointer that does not name a pipe (`\\.\pipe\indus-gw-` + hex) is ignored: a client never
  dials a path an attacker wrote into the home.
- Busy pipe (all instances in use): the client waits for one within its deadline.
- Elevated vs. normal process of the same user: the token may not be readable across integrity
  levels; that is "unknown" (owner-only verbs refused, plain verbs answered).
- Home safety: Windows has no mode bits. The home must be a directory; its protection is the
  user profile's ACL. Documented as best effort.
- The desktop closing: the daemon is not in the desktop's job (silent breakaway), so it outlives
  the app and idles out like on POSIX.

Our design differences / decisions:
- Named pipe, not loopback TCP: keeps "local only, never TCP" and gives a peer pid to check.
- The pipe name is random and published through the pointer file (the same file the long-home
  fallback already uses), so no hashing rule has to match between Python and Node.
- Restart and stop through the control verb, not emulated signals.

Tests we will write:
- `winproc`: liveness of ourselves and of an exited child, start time stable, own SID readable
  (Windows only).
- Control channel: a `ControlServer` on a pipe answers `identify` to the Python client; owner-only
  verbs answer the same user; a stale pointer is `no_socket`; a bad pointer is ignored (Windows only).
- `pid_alive` never terminates the probed process (Windows only).
- Desktop: `socketPathFor` accepts only a well-formed pipe name on win32 (runs everywhere with the
  platform passed in).

## Decisions for the lead
- D-16.55 amended by the owner (2026-10-25): the gateway daemon also runs natively on Windows, best
  effort; WSL2 stays the recommended setup for messaging platforms.

Compare checklist (filled after the rewrite): no reference compare (none read; see above).
- [x] Windows: `indus gateway ensure --json` starts a detached daemon in about 4 s and answers its
  `ws_url`; `gateway status` reads it over the pipe; `gateway stop` drains it (exit 0) and removes
  the pointer (checked by hand on Windows 11, Python 3.13).
- [x] Windows: `indus serve ensure` starts and rediscovers the headless backend.
- [x] Windows: the packed desktop app (`pack:unsigned`) boots against this checkout: it starts the
  daemon, replaces a serve holding another token, mints a ticket over the pipe from Node and its
  WebSocket attaches (daemon log).
- [x] Pipe server and client (`tests/unit/service/test_control_pipe.py`), Win32 probes
  (`tests/unit/platform/test_winproc.py`), the desktop client
  (`apps/desktop/electron/backend/control.test.ts`): green on Windows.
- [ ] Linux: the existing control-socket, ensure and liveness tests (CI). Not run here: the POSIX
  paths are unchanged apart from moving code into helpers, and pyright `--pythonplatform Linux` is
  clean on every changed file.
- Found on the way: a Windows venv's `python.exe` is a redirector that runs the real interpreter
  as its child, so a detached start's pid is not the server's. `serve ensure` waited for a record
  with its own pid and timed out; on Windows it now accepts a record written after its spawn
  (`serve.lock` serialises callers). `gateway ensure` only watches the child's liveness, which the
  redirector mirrors.
- Not taken: a Windows service (no `gateway install`), messaging platforms natively, ACL checks on
  the home.

## Status

Done, best effort (2026-10-25). Owed: the Linux CI run. Outside the task's files: the other
`os.kill(pid, 0)` probes that run on Windows (`backends/docker/egress.py`, `backends/sync_back.py`,
`board/dispatcher.py`, `state/fts.py`, `tools/builtin/browser/engines.py`,
`tools/builtin/browser/session.py`) now ask `winproc` there instead of terminating the process.

## Follow-up: first run without a model provider (owner request, 2026-10-25)

Before, the daemon refused to start without a model provider on every OS (`NoUsableProvider` out
of `_session_service`), so the desktop never booted far enough to show its provider onboarding.
Now `gateway.daemon.process.MainRoute` holds the default route: resolved at start as before; when
no provider is set up the daemon logs it and starts, and every agent start tries again with the
config read from disk (`scope.with_config(None)`), keeping the first route that resolves (a later
change of provider still needs a restart, as before). While pending, reading the route raises
`NoUsableProvider`, which clients receive as `PROVIDER_UNAVAILABLE`. `SessionService` takes a
route or a callable (as `AuxClient` already did); `gateway/core/commands.py` shows "no model set up
yet" instead of failing. On the desktop, onboarding announces a saved key or model
(`requests.announceProviderSaved`) and the status bar's readiness asks again at once, so "Backend
needs setup" clears when the first run finishes.

Checked in the packed app on Windows against a fresh home: the daemon starts, the first-run overlay
shows, "Use a model running on this computer" tests a local OpenAI-compatible fake, "Start
chatting" saves it, and the first message is answered by that model with the daemon never
restarted. Tests: `tests/unit/gateway/daemon/test_main_route.py`,
`apps/desktop/src/onboarding/useProviderReady.test.tsx`, two cases in `Onboarding.test.tsx`.

## Follow-up: sign-ins too long for the Windows keychain (2026-10-25)

"Signed in, but the credential could not be saved" after the ChatGPT sign-in: Windows Credential
Manager keeps at most 2560 bytes (1280 UTF-16 code units) per entry (`CredWrite` error 1783 past
that), and the OAuth token JSON is several KB. `foundation.secrets.KeychainBackend` now takes
`max_units` (set to `WINDOWS_MAX_UNITS` on Windows): a longer value is kept in parts
`<profile_id>:<NAME>#1..n` behind a head `indus-chunked:v1:<n>:<digest>`; reads check the digest,
a shrink or delete removes the parts, values that fit stay one entry, and other OSes are unchanged.
The sign-in route now logs why a save failed. Checked against the real Credential Manager (a
4.5 KB bundle round-trips in 4 parts); tests: `tests/unit/foundation/test_keychain_parts.py`.

## Follow-up: one home for the desktop app and the CLI on Windows (2026-10-25)

`indus.foundation.home` and `scripts/install.ps1` default to `%LOCALAPPDATA%\indus` on Windows,
but the desktop's `electron/paths.ts` used `~/.indus` everywhere, so the app and terminal commands
saw different settings and sessions. `resolveIndusHome` now follows the CLI: `INDUS_HOME`, else
`%LOCALAPPDATA%\indus` (with `INDUS_DATA_DIR_SUFFIX`) on Windows, `~/.indus` elsewhere as before.
`legacyWindowsHome` lets main log where an earlier build's settings are when the new home has
none; nothing is moved automatically. Keychain entries are keyed by profile id (`default`), not
the path, so a moved home keeps its sign-ins. Tests: `apps/desktop/electron/paths.test.ts`.
