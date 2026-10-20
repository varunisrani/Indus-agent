# Task: `indus acp` entry and stdio purity (P19-01) Phase: 19 Owner: wave-29 builder A1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/entry.py` (all: flag
handling before the server, stderr logging, probe-noise filter, setup/browser setup, Windows warm
import, `run_agent` + `aclose` order), `website/docs/developer-guide/acp-internals.md` (boot flow,
stdout rule). Our P12-28 `tui_bridge/stdio_server.claim_stdio` (descriptor takeover), P06/P07
`cli/entry.py` (`STDIO_COMMANDS` → logging in `stdio` mode), `foundation/log.py`. Closed before
writing.
Evidence: none.

What it does (own words): an editor starts `indus acp` and talks JSON-RPC over the child's stdin and
stdout. Stdout is the protocol: one stray `print` or library banner corrupts the stream and the
editor drops the agent. So:

- The informational flags run first and exit without starting anything: `--version` prints the
  version line, `--check` imports the SDK (`agent-client-protocol`, the `acp` extra) and the
  adapter's own modules and prints OK (exit 1 with an install hint when the SDK is missing),
  `--setup` runs `indus setup` as a child process (the ACP package may not import the CLI; the child
  inherits the terminal) and then, on a TTY, offers the browser toolchain, `--setup-browser [--yes]`
  installs it (through the existing `indus` CLI as a child as well).
- Then, before any heavy import, the stdout guard takes the protocol channel: private copies of
  descriptors 0 and 1 are kept for the protocol, descriptor 1 is pointed at stderr, descriptor 0 at
  the null device, and `sys.stdout` becomes a writer that drops anything written to it and logs one
  error per stray write (length only, never the text). Logging is the process's `stdio` mode
  (stderr at WARNING, files, redaction, HTTP client loggers at WARNING) because `acp` is added to the
  CLI's `STDIO_COMMANDS`.
- Liveness probes `ping`, `health`, `healthcheck` are not ACP methods: they are answered `-32601`
  like any unknown method, and logged at DEBUG only (no traceback). Unknown other methods are
  logged at INFO without traceback too.
- The server is the SDK's JSON-RPC `Connection` over asyncio streams on the private descriptors,
  with our method dispatcher (`agent.AcpAgent.dispatch`) as its handler.
- Exit (end of input, SIGTERM, Ctrl-C): the backend is closed while its loop still runs. The
  adapter never ends sessions: in attached mode the daemon ends an `acp` session once no viewer is
  left and nothing is queued (D-16.421); in local mode closing detaches only.
- Windows: the heavy native imports that deadlocked in the reference (memory provider, keyring
  backend) are imported on the main thread before the stdin reader starts; building a local service
  happens off the loop thread (`run_blocking`).
- Secrets: the profile's `SecretStore` is the scope's (bootstrap); TLS uses the OS trust store
  where the process already does (the provider HTTP clients); nothing extra here.

Inputs → outputs: `indus acp [--local] [--version] [--check] [--setup] [--setup-browser [--yes]]`
→ stdout NDJSON frames only; stderr diagnostics; exit 0 on end of input, 1 on a crash or a failed
check.

Files: `src/indus/acp/entry.py`, `src/indus/acp/stdout_guard.py`, `src/indus/acp/command.py`
(the click command, see decisions), `src/indus/acp/__init__.py`, `tests/unit/acp/test_entry.py`;
shared: `src/indus/cli/app.py` (lazy row), `src/indus/cli/entry.py` (`acp` in `STDIO_COMMANDS`),
`pyproject.toml` (`acp` extra).

Edge cases / failure modes:
- SDK missing: `--check` exit 1 with "install the acp extra"; a plain run prints the same hint on
  stderr and exits 1 (stdout untouched).
- A handler `print()`s: nothing reaches stdout; one error line on stderr.
- Probes: `ping` → `-32601`, stderr has no traceback.
- `--setup` with no `indus` executable: falls back to `python -m indus` (same interpreter).
- Broken pipe on stdout: the connection ends, exit 0.

Our design differences / decisions:
- Our names: `indus acp`, setup method id `indus-setup` (P19-02). No `indus-acp` console shim (no
  editor registry here needs a second binary, D-16.3).
- Descriptor takeover (not only a `sys.stdout` swap), like the TUI bridge: child processes that
  inherit fd 1 cannot write into the frame stream either.
- No `.env` loading here: the CLI bootstrap already loaded the profile's env and secrets.

Tests we will write:
- subprocess: `--version`, `--check` (ok and SDK hidden → exit 1 + hint);
- subprocess: a server whose handler prints — stdout parses as JSON frames only;
- subprocess: `ping` → -32601 and stderr has no traceback;
- `--setup` spawns `indus setup` (fake executable on PATH records argv).

## Decisions for the lead
- The click command lives in `src/indus/acp/command.py` and is registered by name in
  `cli/app.py` (as `tui-bridge` and `send` are), not in `src/indus/cli/commands/acp.py`: the layer
  contract makes `indus.cli` and `indus.acp` independent siblings, and a command module under
  `cli/` would have to import the adapter. Move it if the lead prefers a name-loaded shim there.
- The SDK (`agent-client-protocol==0.12.1`, Apache-2.0, schema v1.19.0, `PROTOCOL_VERSION` 1) is
  pinned in the `acp` extra **and** in the dev group, so the gate (no extras) runs the ACP tests.
  0.12.1 is the newest non-pre-release uploaded ≥14 days before exclude-newer (2026-08-16; the
  1.0.0 release candidates are pre-releases). `uv.lock` was regenerated in the worktree only.

Compare checklist (filled after the rewrite; reopened `acp_adapter/entry.py`):
- [x] flags before server code
- [x] stderr logging, probe noise
- [x] backend closed while its loop is alive; no session ended by the adapter
- Deliberate differences: see above.
- Not taken: update-recovery and import-path hardening at startup (installer concerns of the
  reference's checkout layout; ours is a wheel).

## Status

Done (2026-10-20), wave-29 builder A1. Tests: see the task's Verify line; all green with the gate checks. Trap/ACC ledger rows for R09.9/R09.13/R09.14/R09.16 and ACC-M1/M2/M8 left to the lead (follow-up).
