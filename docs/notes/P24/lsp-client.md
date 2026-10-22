# Task: LSP client and manager (P24-15) Phase: 24 Owner: P24 slice L1 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/lsp/client.py` (constants,
initialize, request/retry, document sync, diagnostics wait, shutdown), `agent/lsp/manager.py`
(spawn single-flight, broken set, idle reaper, status, release), `agent/lsp/servers.py` (catalogue
shape, root markers, custom servers, the servers that never run project code), skimmed
`agent/lsp/workspace.py` (git-worktree gate, marker walk), `agent/lsp/cli.py` (verbs) and
`agent/lsp/protocol.py` (framing limits). Closed before writing. Evidence: none.
Protocol pin: Language Server Protocol 3.17 (base protocol framing `Content-Length` + JSON-RPC 2.0;
`initialize`/`initialized`, `textDocument/didOpen|didChange|didSave|didClose`,
`textDocument/publishDiagnostics`, `textDocument/diagnostic` pull, `shutdown`/`exit`, error codes
`ContentModified` −32801 and `MethodNotFound` −32601). Names checked against the 3.17 spec.

What it does (own words): edits get a language server's opinion. The manager finds the server for
a file from a catalogue (by extension), works out the project root (git work tree, then the nearest
marker file of that server), and keeps one running server per (server, root). A server is started
only when its binary is already on the PATH (or named in `lsp.servers.<id>.command`): the catalogue
carries install hints, never an installer. The client speaks LSP over the child's stdio: initialize
within 45 s, keeps whole-document sync with a version per document (a stored diagnostic list only
counts for the version it was published for), at most 64 open documents (the oldest is closed),
retries `ContentModified` three times (0.5, 1, 2 s), answers the few server→client requests it must
(configuration, workspace folders, progress/registration → null, anything else → method not found),
keeps the last 30 stderr lines for failure messages and caps a frame at 16 MiB. Shutdown is polite
first (`shutdown` request, `exit`), then 1 s, SIGTERM to the server's process group, 1 s, SIGKILL.
Idle servers are stopped after 600 s (config floor 30 s; 0 turns reaping off). A server that fails
to start is put in a broken set and not retried until `indus lsp restart` (or `broken_retry_s`).
Children get their environment from `safety.child_env` (R02.22), so no provider key reaches a server.

Inputs → outputs: config `lsp.*` (enabled, wait_timeout, idle_timeout, broken_retry_s,
trusted_workspaces, servers) → `LspManager.client_for(path)` → a running `LspClient`;
`LspClient.sync/save/diagnostics(path, version, timeout)` → list of LSP diagnostics or `None` (no
verdict in time). `indus lsp status [--json]`, `indus lsp install-hint <server>`, `indus lsp restart`.

Files: `src/indus/tools/lsp/{__init__,client,manager,servers}.py`, `src/indus/cli/commands/lsp.py`,
`tests/fakes/lsp_server.py`, `tests/unit/tools/lsp/test_client.py`, `tests/unit/tools/lsp/test_manager.py`;
shared: `foundation/config/schema.py` (`lsp` section), `cli/app.py` (`lsp` row).

Edge cases / failure modes:
- binary missing → no client, the pair is marked broken once, status lists it with the install hint;
- initialize never answered → after 45 s the start fails, the process group is killed, the error
  carries exit status and the stderr tail;
- server dies mid-request → pending requests fail at once ("connection closed"), state `failed`;
- an oversized or malformed frame header → protocol error, the reader stops and the client fails;
- `ContentModified` four times → the fourth error is raised (three retries only);
- 65th document → the least recently touched one gets `didClose` and is forgotten;
- two edits racing for a cold server → one spawn (single flight), both get the same client;
- a file outside any git work tree → LSP is skipped (no daemon for a home-dir chat);
- a server that runs project code (rust-analyzer, gopls, …) in a workspace that is not trusted →
  skipped and listed under "untrusted" in status (trusted = the session's own work tree or
  `lsp.trusted_workspaces`).
- Windows is out of scope (D-16.55); process-group signals are POSIX.

Our design differences / decisions:
- One asyncio manager on the caller's loop (our tools are async) instead of a private loop thread.
- Timeouts and backoff sleep on the injected `Clock`, so tests run on a fake clock.
- No auto-install at all (row: install hints only); `indus lsp install-hint` prints the command.
- Multi-root sharing of one pyright process across worktrees is not taken (one process per root;
  simpler, bounded by the idle reaper).
- A frame body cap of 16 MiB (the row's stream limit) for stdout frames as well as stderr lines.

Tests we will write: fake server script (`tests/fakes/lsp_server.py`) driven by a JSON script: init
timeout on a fake clock; ContentModified retry delays and give-up; 64-file eviction with didClose;
push/pull diagnostics versioning; server→client requests; stderr tail in failure text; shutdown
escalation order (polite, SIGTERM, SIGKILL); manager single flight, broken set, idle reap at 600 s
(fake clock), floor of 30 s, restart clears broken set, env from the child-env builder (no provider
key reaches the server), git gate, untrusted gate, custom servers from config; CLI status/hint/restart.

## Decisions for the lead
- `indus lsp restart` acts on the language servers of the process it runs in; servers inside a
  running daemon are reached only when the daemon exposes a verb (follow-up, not built here).
- Default `lsp.enabled: true`, but nothing starts unless a host installs the manager
  (`tools.lsp.install_active`) and the binary is already present.

Compare checklist (filled after the rewrite; reopened `agent/lsp/client.py`, `manager.py`, `servers.py`):
- [x] 45 s initialize, 16 MiB stream, 3 × ContentModified from 0.5 s doubling, 1 s shutdown graces,
  30 stderr lines, 64 documents, 600 s idle, 30 s floor (tests: `test_client.py::test_initialize_times_out_after_45_seconds`,
  `::test_content_modified_retried_three_times_with_doubling_delay`, `::test_sixty_fifth_document_closes_the_oldest`,
  `test_manager.py::test_idle_server_reaped_after_600_seconds`, `::test_idle_floor_is_30_seconds`)
- [x] shutdown escalation (`test_client.py::test_shutdown_escalates_polite_then_term_then_kill`)
- [x] versioned freshness: a push for an older version never satisfies a wait (`::test_stale_push_is_not_a_verdict`)
- [x] method-not-found on pull disables pulling for that client (`::test_pull_unsupported_falls_back_to_push`)
- [x] server→client requests answered; unknown method → −32601 (`::test_server_requests_are_answered`)
- [x] single flight + broken set (`test_manager.py::test_concurrent_callers_share_one_spawn`, `::test_failed_spawn_is_not_retried_until_restart`)
- [x] child env from the builder (`test_manager.py::test_server_env_comes_from_child_env_builder`)
- [x] gap found: the reference gives the server didChangeWatchedFiles on every sync — taken (some
  servers re-scan only on it), pinned by `test_client.py::test_sync_sends_open_then_change_with_versions`.
- [x] gap found: servers of a removed worktree (or a deleted root) must be released — added
  `LspManager.release(folder)` and deleted-root reaping, pinned by
  `test_manager.py::test_release_and_deleted_roots`.
- Deliberate differences: no private loop thread; no auto-install; no multi-root sharing; status
  is a dataclass rendered by the CLI; diagnostics are pulled only from servers that advertise
  `diagnosticProvider` (LSP 3.17), not tried on every server until MethodNotFound; the
  escalation follows the row (polite, 1 s, SIGTERM, 1 s, SIGKILL).
- Not taken: Vue/PowerShell bespoke spawners and the TypeScript SDK pinning for untrusted trees
  (the TypeScript server is in the trusted-only set instead); the event log file.

## Status

Done (2026-10-21). Shared files touched: `foundation/config/schema.py` (`lsp` section + test),
`cli/app.py` (`lsp` row), `tests/contract/cli/test_help_catalogue.py` (golden order) and
`tests/contract/cli/non_tty_table.yaml` (rows). Wiring for the lead: one `LspManager(scope=…)`
per agent host process, wrapped in `EditDiagnostics` and installed with
`tools.lsp.diagnostics.install_active`; call `manager.trust(<session work tree>)` per session and
`manager.release(path)` before a worktree is removed; `await manager.close()` at host exit.
Follow-ups: `lsp.exclude_roots`; a daemon verb for `indus lsp restart` against a running daemon.

## Wiring (wave 42, WB — D-16.799)

Wired: `service.lsp_host.LspUpkeep` — one language-server tier per process (`LspManager(scope=…)`
in `EditDiagnostics`, `install_active`), joined by every session service of the process (CLI
runtime, TUI bridge, ACP local host, gateway daemon chat and surface services:
`SessionService(lsp=…)`); built when the first session is attached (nothing spawns until an edited
file needs a server), each attached session's folder trusted by its git work tree
(`manager.trust`), the last service out closes the manager and uninstalls the checker. Tests:
`tests/unit/service/test_lsp_host.py`. The root `tests/conftest.py` turns hosts' own LSP settings
off (`no_language_servers`; the venv carries `pyright-langserver`) — a test wanting the tier passes
`LspUpkeep(scope, settings=…)`.

Decisions for the lead (wiring): the tier is per process, not per profile: a multiplexed gateway's
servers get the child environment of the profile that started the tier (provider keys never reach a
server either way, R02.22); `LspUpkeep.release(folder)` exists but no worktree-removal path calls it
yet (follow-up with P11's worktree remove); `indus lsp restart` against a running daemon is still a
follow-up.
