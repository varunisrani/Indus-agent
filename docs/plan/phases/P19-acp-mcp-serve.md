# P19 · ACP + MCP server mode

**Weeks:** 1.5 · **Track:** B · **Depends on:** P17 (daemon as shared backend: owner-only control socket, single-use tickets, WS endpoint, turn lease, generation-bound controls, event replay since cursor, server-request re-delivery, admission receipts and the "unknown" turn state), P15 (delivery library and live platform connections for `messages_send`; gateway session routing index), P13 (MCP SDK pin, MCP client for editor-provided servers, toolset resolver extras), P09 (slash registry incl. skill commands), P05 (approvals), P04 (`SessionService`, protocol) · **Status:** planned

## Goal

Editors that speak the Agent Client Protocol (Zed, VS Code and JetBrains ACP clients) run Indus as an
external agent: `indus acp` opens, loads, lists, forks and cancels sessions, streams message, thought,
tool, plan and usage updates, and shows permission cards with diffs. It has two explicit modes behind
one interface (D-16.29): **attached** (default, a viewer of the profile daemon from P17, restart-safe,
never resends a turn whose outcome is unknown) and **`--local`** (in-process `SessionService`).
Both advertise the same single "ask before edits" mode. `indus mcp serve` lets another agent list
messaging conversations, read history, long-poll new messages and send through the daemon's
platforms; the two approval tools appear only once wired to the daemon's approval queue (D-16.30).

Interop note: ACP method names, notification kinds, tool kinds, permission option kinds, stop
reasons and capability fields are a third-party protocol (they are not ours to rename, D-16 §1).
Pin the official ACP Python SDK `agent-client-protocol` to an exact version at P19 start (the
reference used 0.9.0; take the newest release that is at least 14 days old, D-16.52) and record its
`PROTOCOL_VERSION`. Check every name used in this phase against the vendor JSON schema
(`schema/schema.json` in the agentclientprotocol GitHub repository) at the tag matching that SDK
version, including which methods are unstable-tier (`session/fork`, `session/list`,
`session/resume`, `session/set_model` were unstable in the reference's pin; run the SDK with its
unstable switch only for those). A snapshot of that schema is kept as a test fixture and the drift
check of P19-18 fails when our constants disagree with it. MCP is likewise third-party: use the
MCP Python SDK version P13 pinned, stdio transport, protocol version negotiated by the SDK
(`2025-06-18` or newer). Read the reference's code for behaviour and edge cases only.

## Tasks (18)

| ID | Task | Read in reference | New files | Verify |
|---|---|---|---|---|
| P19-01 | **`indus acp` entry and stdio purity.** Lazy click subcommand `indus acp [--local] [--version] [--check] [--setup] [--setup-browser [--yes]]` plus an `indus-acp` console shim only if an editor registry needs a separate binary (D-16.3). Flags handled before any server code: `--version` prints and exits; `--check` imports the SDK and the adapter and prints OK (exit 1 with a hint when the `acp` extra is missing); `--setup` runs `indus setup` as a child process (no import of `indus.cli`, layer rule), then with a TTY offers the browser toolchain; `--setup-browser` installs it. From the first import: all logging to stderr with redaction, HTTP client loggers quietened, a stdout guard that turns any stray write into a logged error (R09.14). Liveness probes `ping`, `health`, `healthcheck` get `-32601` without a traceback. Load the profile's secrets through `SecretStore`, use the OS trust store for TLS. On Windows pre-import heavy native modules (memory provider, keyring backend) on the main thread before the stdin reader starts, and build agents off the event loop (R09.15). Exit closes the backend while its loop is alive; the adapter never marks sessions ended (closing an ACP session is the daemon's job: it does so once no viewer remains attached and nothing is queued). | `acp_adapter/entry.py` [225], `website/docs/developer-guide/acp-internals.md` [208] | `src/indus/acp/entry.py`, `src/indus/acp/stdout_guard.py`, `src/indus/cli/commands/acp.py`, `tests/unit/acp/test_entry.py` | Subprocess run: `--version`, `--check` exit codes; a `print()` in a handler never reaches stdout (stdout parses as frames only); `ping` → -32601 and stderr has no traceback; `--setup` spawns `indus setup` (fake binary records argv) |
| P19-02 | **`initialize` and `authenticate`.** Respond with the SDK's protocol version, agent info `{ name: "indus-agent", version }`, capabilities `loadSession: true`, `promptCapabilities.image: true`, session `fork`/`list`/`resume`. Auth methods: when credentials resolve, an agent-managed method whose id is the active provider name; always a `terminal` method id `indus-setup` with `args: ["--setup"]` so a fresh editor install can configure first. Read the raw `initialize` params to detect client `elicitation.form` support (the SDK's typed model may drop it). `authenticate(methodId)`: `indus-setup` succeeds only once a provider with credentials resolves; otherwise the id must equal the resolved provider name; a callable credential (cloud identity) counts; anything else fails. | `acp_adapter/gateway_server.py` [962: `initialize`, `authenticate`], `acp_adapter/auth.py` [51] | `src/indus/acp/agent.py`, `src/indus/acp/auth.py`, `tests/contract/acp/test_initialize.py` | Fake ACP client: capabilities exact (checked against the schema snapshot); no credentials → only `indus-setup` listed and authenticate with it fails until a fake key is added; elicitation flag detected from raw params |
| P19-03 | **Backend interface and `--local` host.** `AcpBackend` protocol used by every handler: create/resume/fork/list sessions, submit (with `input_id`), cancel(admission), mutate (model, workspace, rename, rewind, reset, compress), subscribe → event stream with `seq` and `replay_epoch`, answer approval/clarify by `prompt_id` + `generation`, receipts. `LocalHost` implements it over an in-process `SessionService` (P04) in this process; it creates sessions with source `acp` and the same editor policy as attached mode; receipts and "unknown" never occur in local mode except after this process restarts (then sessions resume from the store with no open turn). Mode selection is explicit: `--local` or no daemon available and `acp.allow_local_fallback: true` (default false, D-16.83; with false and no daemon the adapter answers `initialize` and then fails `session/new` with a clear "start the daemon or use --local" message). | `acp_adapter/server.py` [1,099: initialize, sessions, list paging, modes — to see what the in-process path did differently] | `src/indus/acp/backend.py`, `src/indus/acp/local_host.py`, `tests/unit/acp/test_local_host.py` | Same handler test-suite run against `LocalHost` and a fake attached host (parametrised fixture); no daemon + fallback off → session/new error text; fallback on → local |
| P19-04 | **Attached host (daemon viewer).** Connect lazily to the profile daemon: owner-only control socket → single-use ticket → WS (P17, D-16.25); call the describe method and require its profile id to equal this process's profile (else `profile_mismatch`); start an event pump task. If the pump dies or the transport closes, the next call tears down and reconnects; on reconnect resubscribe every held session from its last `seq`, re-attach editor MCP servers, re-request live reasoning, re-offer pending approval/clarify prompts, and announce lost "unknown" turns. Every waiter on the old transport fails with "outcome unknown, do not resend" (R09.11). Requires the daemon to list `acp` among creatable sources and advertise the editor-policy capability (else `acp_policy_unavailable`). | `acp_adapter/gateway_server.py` [962: connection, pump, reconnect], `acp_adapter/gateway_reply.py` [135] | `src/indus/acp/attached_host.py`, `tests/contract/acp/test_attached_host.py` | Fake daemon (P17 fake): profile mismatch refused; kill transport mid-turn → waiter gets outcome-unknown, reconnect resubscribes from last seq, pending approval re-offered once |
| P19-05 | **`session/new`.** Translate cwd (Windows `E:\x` and `\\wsl.localhost\…` forms → POSIX under WSL); freeze launch flags (yolo, safe mode) into the session policy; safe-mode or ignore-config launches must name an explicit model **and** provider or creation is refused; create with a random `request_id`, source `acp`, editor policy `{ mcp_servers, edit_approval_policy: ask }`; keep editor MCP specs (commands, env, headers) in memory only; ask for live reasoning (`show_reasoning`, 10 s timeout, failure ignored); respond `{ sessionId, models, modes, _meta.indus.sessionProvenance }`; **after the response is queued** (yield once) send `available_commands_update` and `usage_update` (R09.9). | `acp_adapter/gateway_server.py` [962: `new_session`], `acp_adapter/session.py` [582: cwd translation, toolset expansion], `acp_adapter/provenance.py` [86] | `src/indus/acp/sessions.py`, `src/indus/acp/paths.py`, `tests/contract/acp/test_session_new.py`, `tests/unit/acp/test_paths.py` | Recorded client order: response frame precedes both updates; `E:\Projects\x` → `/mnt/e/Projects/x` when WSL flag set; safe mode without provider refused; MCP env never written to the store (store scan) |
| P19-06 | **`session/load`, `session/resume`, `session/fork`.** Load and resume behave the same: map the editor's id to the logical session (ids may rotate through compression; keep aliases so updates use the id the editor knows); if the session cwd differs from the editor's, apply a move-workspace mutation (idle sessions only), and if it has no cwd say so; non-empty `mcpServers` = the editor's selection (policy conflict → rebind mutation then retry), empty list keeps existing; subscribe; **stream the full history as `session/update` notifications before responding** (user/agent/thought chunks and reconstructed tool start/complete) (R09.9); re-offer pending prompts; announce lost turns; respond with models/modes/meta. Fork: branch mutation on the parent, child gets the supplied MCP servers or a copy of the parent's, move workspace if cwd differs, subscribe, respond with the child id. | `acp_adapter/gateway_server.py` [962: load/resume/fork], `acp_adapter/gateway_session_meta.py` [127] | `src/indus/acp/history.py`, `tests/contract/acp/test_session_load.py` | Recorded frames: all history updates before the load response; compressed session loaded by old id → updates addressed to old id; fork returns child with parent's MCP servers |
| P19-07 | **`session/list`.** ACP-source rows only, at most 1,000 read; filter by cwd normalised with realpath (macOS `/var` vs `/private/var`, `/tmp` vs `/private/tmp`) and Windows drive → WSL mount mapping (R09.16); page size 50; `cursor` = last session id of the previous page; unknown cursor → empty page (never the first page); `nextCursor` only when more remain. | `acp_adapter/server.py` [1,099: list paging], `acp_adapter/session.py` [582: normalisation] | `src/indus/acp/listing.py`, `tests/unit/acp/test_listing.py` | 120 rows → pages 50/50/20 with cursors; unknown cursor → `[]`; `/private/var/x` matches `/var/x`; non-ACP rows excluded |
| P19-08 | **Prompt content conversion.** ACP content blocks → user content: text; base64 images → image parts; resource links to local files read up to 512 KiB (an oversize image becomes a notice; text truncated with a note); embedded resources decoded the same way; remote images fetched through the SSRF policy (P05) and staged under the home; attachment count capped by the backend's limit. Content fingerprint (stable hash of normalised blocks) for idempotent retries. | `acp_adapter/content.py` [282], `acp_adapter/gateway_media.py` [111] | `src/indus/acp/content.py`, `tests/unit/acp/test_content.py` | 600 KiB text file truncated with note; private-IP image URL refused by SSRF policy; same blocks → same fingerprint, reordered → different |
| P19-09 | **`session/prompt` and `session/cancel`.** Prompt: unknown session → `not_found`; a turn in state "unknown" (lost in a daemon restart) → refuse with `unknown_execution` text telling the user to resolve it in a terminal (resume in the classic chat and discard the lost admission) — never resubmit (R09.11, R06.19, R03.21). A registered slash command (or alias) with no attachments runs via P19-12; `/etc/hosts` or unknown `/words` are prompt text. Submit with `input_id`; if the previous submit for this session was never acknowledged and the fingerprint matches, reuse its staged content and `input_id` so the daemon returns the same admission; any other prompt retires the retained identity; a cancel during staging ends with `cancelled` without sending. Receipt `terminal` (retry of a settled turn) → replay that turn's final text once; `unknown` → refuse. Wait for this admission's terminal result, pump failure, transport replacement (outcome-unknown error) or blocking behind an unknown turn. Outcome `failed` → JSON-RPC error `admitted_turn_failed`; `cancelled` → `stopReason: cancelled`; else `end_turn`, with usage from the last usage event. Cancel: pending submit → remember and apply once the admission id is known; otherwise cancel this editor's own admission; on a stale-generation reply read the receipt and interrupt only if that exact admission is running (never a successor turn from another surface). | `acp_adapter/gateway_server.py` [962: `prompt`, `cancel`], `acp_adapter/gateway_reply.py` [135] | `src/indus/acp/prompts.py`, `tests/contract/acp/test_prompt.py` | Fake daemon: lost ACK + same prompt → same admission, one user row; different prompt retires identity; unknown turn → refusal text, no submit frame sent; cancel after successor started from CLI does not interrupt it; failed → error code |
| P19-10 | **Event projection.** Drop events with `seq` ≤ the snapshot's last seq in the same replay epoch (dedupe after reattach); `message.delta` → `agent_message_chunk` with a stable message id per assistant message; interim commentary not already streamed → its own chunk segment; `message.complete` finishes the reply (unless a retry receipt delivered it) and records the terminal; `reasoning.delta` → `agent_thought_chunk`; `todo.updated` → `plan`; `tool.start`/`tool.complete` → `tool_call`/`tool_call_update`, and every tool call reaches a terminal status (still open at turn end → `failed`); usage → `usage_update { size: context window, used }`; title change → `session_info_update` only when changed; tools changed → text notice that editor MCP tools change from the next turn; replay gap → transport failure. Tool kind map: read (file read, skill view/list, browser snapshot, vision), edit (write file, patch, skill manage), search (file search), execute (terminal, process, execute_code, browser actions, delegate, image generation, TTS), fetch (web search/extract, browser navigate), think, other (todo, unknown). Edits render as diffs; terminal as the command; reads/searches as previews; outputs truncated at 5,000 chars. | `acp_adapter/events.py` [297], `acp_adapter/tools.py` [853: kind map, truncation, diff building] | `src/indus/acp/projection.py`, `src/indus/acp/tool_kinds.py`, `tests/unit/acp/test_projection.py`, `tests/unit/acp/test_tool_kinds.py` | Replayed event fixture → golden list of ACP updates (validated against the schema snapshot); duplicate seq dropped; open tool at turn end → failed; 6,000-char output cut to 5,000; every tool in our registry maps to a kind (registry-driven test) |
| P19-11 | **Permission bridge and clarify.** Approval request → `session/request_permission` with a pending `execute` tool call titled with the command and description (or a diff card for file edits). Options by what the backend allows: "Allow once" (`allow_once`), "Allow for session" (option id `allow_session`, kind `allow_always` because ACP has no session kind), "Allow always" (only if permanent allowed), "Deny" (`reject_once`), "Deny always" (`reject_always`, only when the SDK has the kind and not once-only). Map answers **by option id**, never by kind (R09.12). Close the card's tool call `completed` (allowed) or `failed`. A timed-out approval reports "timeout" distinctly from deny; timeout = configured approvals timeout (default 300 s). Attached mode: a cancelled card, transport loss or unknown option is **not** a denial — the waiter stays answerable by another viewer; release the card key so a later snapshot re-shows it; an accepted answer keeps the key until the matching settled event. Clarify: client with form elicitation → `elicitation/create` with `{ answer: string or enum or array of enum }`; accept → answer (multi-select as JSON array string); decline/cancel → empty (skip); otherwise a permission card with one `allow_once` option per choice plus "Skip" (`reject_once`); dismissal = skip. | `acp_adapter/permissions.py` [153], `acp_adapter/edit_approval.py` [241], `acp_adapter/elicitation.py` [71] | `src/indus/acp/permissions.py`, `src/indus/acp/elicitation.py`, `tests/contract/acp/test_permissions.py`, `tests/contract/acp/test_elicitation.py` | Option-id mapping table; editor dismisses card (attached) → daemon prompt still open and answerable from fake TUI; injected clock timeout → "timeout" decision; multi-select elicitation answer is a JSON array string |
| P19-12 | **Slash commands, model and mode.** Advertise via `available_commands_update` all registered commands that make sense in an editor (built-ins minus terminal-only ones, plus skill and quick commands). Local: `help`, `branch` (refused: use the editor's fork), `steer` (inject into a running turn; idle → resubmit a just-cancelled prompt with the steer as correction, else run as a prompt), `queue`, `title` (rename; bare form forwarded), `undo`/`retry` (rewind mutation; retry resubmits), `tools`, `model` (mutation with an argument, info without), `compress`, `new`, `clear`. Everything else → the backend's slash executor; a refusal is the command's reply, never a failed prompt; usage text when an argument is missing. `session/set_model(modelId)`: ids `provider:model` or `custom:<name>:<model>`; guarded targets ask with a permission card "switch anyway" (`allow_once`) / "cancel" (`reject_once`); declined → error `model_switch_cancelled`; mid-turn → deferred to the next turn with a notice, and a deferred pick refused at the boundary is announced; never changes the profile default. `session/set_mode` and `session/set_config_option`: one mode `default` (ask before edits) in both host modes (D-16.29); other modes or a non-ask edit policy → `acp_edit_policy_mutation_unavailable`; other config writes accepted as no-ops. | `acp_adapter/gateway_commands.py` [271], `acp_adapter/commands.py` [312: route table], `acp_adapter/model_catalog.py` [312] | `src/indus/acp/commands.py`, `src/indus/acp/model_mode.py`, `tests/contract/acp/test_commands.py`, `tests/contract/acp/test_model_mode.py` | `/branch` refused text; unknown `/foo` sent as prompt; guarded model declined → error and no mutation; mid-turn switch deferred notice; set_mode `accept-edits` refused in both modes |
| P19-13 | **Toolset policy and editor MCP servers.** ACP sessions use the `indus-acp` platform toolset (files, terminal/process, web/browser, memory, todo, session search, skills, code execution, delegation, vision; no messaging delivery, no cron), plus resolver extras (enabled plugin toolsets, admitted MCP servers by server name); the global disabled list removes entries; `no_mcp` drops config MCP servers; editor-sent MCP servers are always added through the P13 client. Durable policy keeps only a fingerprint and frozen tool schemas; after a daemon restart the editor must resend identical specs or execution is refused; schemas stay frozen for the conversation. Docs note (user guide stub): hosts that auto-answer permission requests make terminal execution unattended; removing `terminal`/`execute_code` from `indus-acp` is the mitigation. | `acp_adapter/session.py` [582: toolset expansion], `acp_adapter/catalog.py` [79] | `src/indus/acp/toolset.py`, `tests/unit/acp/test_toolset.py`, `docs/user/acp.md` | Resolved tool list golden; disabled toolset removed; spec with changed env after restart → refused; schema frozen across a tools-changed event |
| P19-14 | **MCP serve: server and read tools.** `indus mcp serve [--verbose]` (subcommand under P13's `mcp` group); stdio only; logs to stderr at WARNING (DEBUG with `--verbose`); missing MCP SDK → hint and exit 1; server name `indus`, instructions text (our own words) saying it bridges the connected messaging platforms. Tools in this order: `conversations_list { platform?, limit=50 (1..200), search? }` newest first, search over display name, chat name or key, case-insensitive; `conversation_get { session_key }` with token totals or `{error}`; `messages_read { session_key, limit=50 (1..200) }` user/assistant text only, last N in chronological order, content ≤ 2,000 chars, `total_in_session`; `attachments_fetch { session_key, message_id }` from `MEDIA:` tags, paths checked by the media path policy (R06.22); `channels_list { platform? }` from the channel directory else the routing index. Every tool returns JSON with an `error` field instead of raising; integers clamped, non-numeric replaced by defaults. Reads use a private bounded read-only connection (R03.16, R03.17) and work without the daemon. | `mcp_serve.py` [763: tool registration, read tools], `website/docs/user-guide/features/mcp.md` [§ running as an MCP server] | `src/indus/mcp_serve/server.py`, `src/indus/mcp_serve/read_tools.py`, `src/indus/cli/commands/mcp_serve.py`, `tests/contract/mcp_serve/test_read_tools.py` | MCP SDK in-memory client: tool list order exact; limit 999 → 200; `limit="x"` → 50; media path outside allowed roots refused; store opened read-only (write attempt raises); stdout carries only MCP frames |
| P19-15 | **MCP serve: event bridge and long-poll.** Background task (injected clock): at start record each conversation's latest message id as a baseline so history is not replayed; every 200 ms read the store's change counter (`PRAGMA data_version` on the private read-only connection) and do nothing if unchanged; on change reload the routing index (gateway session rows with session key, origin, display name) and enqueue one `message` event per new user/assistant text message after the per-conversation last-seen id (content ≤ 500 chars; ordered by row id, R03.14). Queue capped at 1,000 (oldest dropped, and the next poll reports `dropped: n`); cursors are monotonically increasing integers. `events_poll { after_cursor=0, session_key?, limit=20 (1..200) }` → `{ events, next_cursor }`; `events_wait { after_cursor=0, session_key?, timeout_ms=30000 (0..300000) }` re-checks at most every 200 ms until its deadline → `{event}` or `{event: null, reason: "timeout"}` (long-poll in 1.0, D-16.67). | `mcp_serve.py` [763: event bridge class, `events_poll`, `events_wait`] | `src/indus/mcp_serve/event_bridge.py`, `tests/contract/mcp_serve/test_event_bridge.py` | Fake clock + store writes: baseline excludes history; one new message → one event; unchanged counter → no index reload (spy); 1,001 events → oldest dropped with count; `events_wait` returns at timeout exactly; ordering by id when timestamps regress |
| P19-16 | **MCP serve: `messages_send`.** `messages_send { target, message }`: target `platform:chat_id` or a friendly channel name resolved through the channel directory; text only; goes through the daemon's delivery library over the control socket (P15, P17), which writes the delivery-ledger row before the platform send; returns the delivery result or `{error}`; no daemon → `{ error: "daemon not running" }`. Never a model tool (D-16.37). | `mcp_serve.py` [763: `messages_send`] | `src/indus/mcp_serve/send.py`, `tests/contract/mcp_serve/test_send.py` | Fake daemon records one ledger-backed send; ambiguous friendly name → error listing candidates; daemon absent → error JSON |
| P19-17 | **MCP serve: approval tools wired to the daemon (M11, D-16.30).** `permissions_list_open {}` and `permissions_respond { id, decision: allow-once / allow-session / allow-always / deny }` are registered **only** when the daemon is reachable and advertises the approval-queue capability at server start; otherwise they are absent from the tool list. List reads open approval prompts from the daemon (all sessions of the profile, with session key, command redacted, choices); respond answers through the same generation-bound control every other surface uses (P17), so the first answer wins and others see `settled`; enqueue `approval_requested` / `approval_resolved` events into the bridge queue from the daemon's event stream. | `mcp_serve.py` [763: permissions tools — note they are hollow, R09.10] | `src/indus/mcp_serve/approvals.py`, `tests/contract/mcp_serve/test_approvals.py` | No daemon → tools not listed; fake daemon with an open prompt → listed with redacted command; respond → daemon receives control with generation; second respond → `{resolved: false, reason: settled}`; events appear in `events_poll` |
| P19-18 | **Protocol conformance and drift checks.** ACP: a schema snapshot (`tests/fixtures/acp/schema-<version>.json`, vendor file, licence recorded in `THIRD_PARTY_NOTICES.md`) and a test that every method, notification kind, tool kind, permission option kind, stop reason and capability field our code uses exists in it with the same tier; a replay test drives the adapter with a recorded Zed-style transcript we write ourselves (initialize → session/new → prompt with tool + approval → load → list → cancel) in both host modes and compares the output frames to a golden. MCP: tool list snapshot with input schemas. A `live` marker test runs Zed headless if available (skipped otherwise, reported). | `website/docs/user-guide/features/acp.md` [454: client setup only] | `tests/contract/acp/test_schema_drift.py`, `tests/contract/acp/test_transcript_replay.py`, `tests/contract/mcp_serve/test_tool_list.py`, `tests/fixtures/acp/schema-<version>.json`, `tests/fixtures/acp/zed_session.jsonl` | Removing a kind from the snapshot fails the drift test; transcript replay identical for `--local` and attached; MCP tool list matches snapshot |

## Files to create (54)

| Paths | Task |
|---|---|
| `src/indus/acp/entry.py`, `stdout_guard.py`, `src/indus/cli/commands/acp.py`, `tests/unit/acp/test_entry.py` | P19-01 |
| `src/indus/acp/agent.py`, `auth.py`, `tests/contract/acp/test_initialize.py` | P19-02 |
| `src/indus/acp/backend.py`, `local_host.py`, `tests/unit/acp/test_local_host.py` | P19-03 |
| `src/indus/acp/attached_host.py`, `tests/contract/acp/test_attached_host.py` | P19-04 |
| `src/indus/acp/sessions.py`, `paths.py`, `tests/contract/acp/test_session_new.py`, `tests/unit/acp/test_paths.py` | P19-05 |
| `src/indus/acp/history.py`, `tests/contract/acp/test_session_load.py` | P19-06 |
| `src/indus/acp/listing.py`, `tests/unit/acp/test_listing.py` | P19-07 |
| `src/indus/acp/content.py`, `tests/unit/acp/test_content.py` | P19-08 |
| `src/indus/acp/prompts.py`, `tests/contract/acp/test_prompt.py` | P19-09 |
| `src/indus/acp/projection.py`, `tool_kinds.py`, `tests/unit/acp/test_projection.py`, `test_tool_kinds.py` | P19-10 |
| `src/indus/acp/permissions.py`, `elicitation.py`, `tests/contract/acp/test_permissions.py`, `test_elicitation.py` | P19-11 |
| `src/indus/acp/commands.py`, `model_mode.py`, `tests/contract/acp/test_commands.py`, `test_model_mode.py` | P19-12 |
| `src/indus/acp/toolset.py`, `tests/unit/acp/test_toolset.py`, `docs/user/acp.md` | P19-13 |
| `src/indus/mcp_serve/server.py`, `read_tools.py`, `src/indus/cli/commands/mcp_serve.py`, `tests/contract/mcp_serve/test_read_tools.py` | P19-14 |
| `src/indus/mcp_serve/event_bridge.py`, `tests/contract/mcp_serve/test_event_bridge.py` | P19-15 |
| `src/indus/mcp_serve/send.py`, `tests/contract/mcp_serve/test_send.py` | P19-16 |
| `src/indus/mcp_serve/approvals.py`, `tests/contract/mcp_serve/test_approvals.py` | P19-17 |
| `tests/contract/acp/test_schema_drift.py`, `test_transcript_replay.py`, `tests/contract/mcp_serve/test_tool_list.py`, `tests/fixtures/acp/schema-<version>.json`, `tests/fixtures/acp/zed_session.jsonl` | P19-18 |

## Files to update (10)

| Path | Change | Task |
|---|---|---|
| `src/indus/cli/app.py` (P06/P07 group) and P13's `mcp` group | register `acp` and `mcp serve` lazily | P19-01, P19-14 |
| `src/indus/tools/toolsets.py` (P04/P05) | `indus-acp` platform toolset definition | P19-13 |
| `src/indus/foundation/config/schema.py` | `acp.allow_local_fallback` (default false), `platform_toolsets.acp` | P19-03, P19-13 |
| `src/indus/protocol/` (P17 daemon contract) | editor-policy capability, `acp` creatable source, approval-queue capability for MCP serve, list-open-prompts method if P17 lacks it | P19-04, P19-17 |
| `pyproject.toml` | extra `acp = ["agent-client-protocol==<pinned>"]`; console shim `indus-acp` only if needed. PyPI has no licence metadata for this SDK: the builder reads the source repository's LICENSE at the pinned tag and records it in 00-layout §4; if it is not on the allow-list, add a written exception or stop and ask the lead | P19-01 |
| `THIRD_PARTY_NOTICES.md` | ACP schema snapshot licence entry | P19-18 |
| `docs/plan/16-what-we-change.md` §2.7 | D-16.29 and D-16.30 "Decided in" cells point at the P19 notes; default `acp.allow_local_fallback: false` is D-16.83 | P19-03, P19-17 |
| `docs/internals/traps-ledger.md`, `docs/internals/acceptance-map.md` | R09.9–R09.16 and M* rows to tests | P19-18 |
| `src/indus/acp/__init__.py` (P00-10 placeholder) | package exports | P19-01 |
| `src/indus/mcp_serve/__init__.py` (P00-10 placeholder) | package exports | P19-14 |

## Fixtures and fakes (4)

| Path | What it holds | Task |
|---|---|---|
| `tests/fakes/acp_client.py` | minimal ACP client over a pipe pair that records frames in order and answers permission/elicitation requests by script | P19-02 to P19-12 |
| `tests/fakes/daemon.py` (P17) | describe, tickets, sessions, receipts incl. `unknown`, approval queue, restart simulation | P19-04, P19-09, P19-16, P19-17 |
| `tests/fixtures/acp/zed_session.jsonl` | our own recorded editor transcript | P19-18 |
| `tests/fixtures/store/gateway_sessions.py` | store builder with gateway sessions, routing index, `MEDIA:` messages | P19-14, P19-15 |

## Task notes (18)

`docs/notes/P19/<slug>.md`: `acp-entry` (01), `acp-initialize` (02), `acp-local-host` (03),
`acp-attached-host` (04), `acp-session-new` (05), `acp-load-fork` (06), `acp-list` (07),
`acp-content` (08), `acp-prompt-cancel` (09), `acp-projection` (10), `acp-permissions` (11),
`acp-commands-model` (12), `acp-toolset` (13), `mcp-serve-read` (14), `mcp-serve-events` (15),
`mcp-serve-send` (16), `mcp-serve-approvals` (17), `protocol-drift` (18). Each ACP note lists every
vendor name it uses with the schema line it was checked against.

## Run until green

```bash
cd indus-agent
uv sync --extra acp --extra mcp
uv run ruff check && uv run ruff format --check
uv run pytest tests/unit/acp tests/contract/acp tests/contract/mcp_serve
uv run pytest -m live tests/contract/acp   # optional lane; skipped and reported when no editor is present
uv run python -m tooling.checks --require-ref
```
On any failure fix the cause and rerun from `uv sync`. Never edit the schema snapshot or a recorded
transcript to make a test pass; a vendor change means a new pinned version and a new snapshot.

## Compare checklist (reopen the reference after writing)

- [ ] Every ACP method and update kind the reference handles has handle / ignore / later in the P19 notes.
- [ ] History replay before the load response; post-new updates after the response (R09.9).
- [ ] No path resubmits a turn whose outcome is unknown; cancel never hits a successor turn (R09.11).
- [ ] "Allow for session" mapped by option id; timeout distinct from deny; dismissed card is not a denial in attached mode (R09.12).
- [ ] Both host modes advertise the same single mode (D-16.29; the reference's in-process three-mode set is not built).
- [ ] MCP serve: the event bridge uses the store change counter, not file times; approval tools absent until wired (R09.10).
- [ ] Stdout purity in both stdio servers from the first import (R09.14).

## Traps covered

| Trap | Pinned by |
|---|---|
| R09.9 history before load response; post-new updates after | P19-05, P19-06 |
| R09.10 hollow MCP approval tools | P19-17 |
| R09.11 never resend unknown-outcome turns; idempotent retries; own-admission cancel | P19-04, P19-09 |
| R09.12 allow-for-session by option id; timeout ≠ deny; dismiss ≠ deny | P19-11 |
| R09.13 two explicit implementations, consistent modes | P19-03, P19-12 |
| R09.14 stdout purity, probe noise | P19-01, P19-14 |
| R09.15 Windows import deadlock | P19-01 |
| R09.16 path normalisation; unknown cursor → empty page | P19-05, P19-07 |
| R06.19 unknown turns pause, never replay | P19-09 |
| R06.22 media path policy | P19-14 |
| R03.14 order by id | P19-15 |
| R03.16 read-only browsing | P19-14 |
| R03.17 bounded read connections | P19-14 |
| R03.21 never replay started work | P19-09 |

## Acceptance

| Feature | Planned ACC ids |
|---|---|
| M1 `indus acp` + initialize/authenticate | ACC-M1-1 (setup method), ACC-M1-2 (`--check`) |
| M2 sessions new/load/resume/list/fork/cancel | ACC-M2-1 (history before reply), ACC-M2-2 (list paging), ACC-M2-3 (fork) |
| M3 prompts and streamed updates | ACC-M3-1 (message/thought/tool/plan/usage), ACC-M3-2 (diffs) |
| M4 permission cards | ACC-M4-1 (five options), ACC-M4-2 (timeout distinct) |
| M5 clarify via elicitation | ACC-M5-1 |
| M6 slash commands | ACC-M6-1 |
| M7 model switch and single mode | ACC-M7-1 |
| M8 attached + `--local` modes | ACC-M8-1 (local), ACC-M8-2 (attached reattach), ACC-M8-3 (lost-turn refusal) |
| M9 editor MCP servers + toolset | ACC-M9-1 |
| M10 `indus mcp serve` | ACC-M10-1 (read tools), ACC-M10-2 (events_wait), ACC-M10-3 (send) |
| M11 MCP approval tools wired | ACC-M11-1 |
| Q7 WSL path translation (ACP part) | ACC-Q7-2 |

## Done when

All suites pass; the drift test is green against the pinned schema; R09.9–R09.16 point at tests.

**Demo (M7 part one):** Zed configured with `indus acp` opens a session in a project, asks for a file
edit and shows an approval card with a diff, then a terminal command with Allow once / for session /
always / Deny; the presenter restarts the daemon mid-turn and Zed shows the "outcome unknown, resolve
in a terminal" message instead of a duplicate turn; `indus acp --local` runs the same flow without a
daemon. An MCP client running `indus mcp serve` lists conversations, waits for a new Telegram message
and replies through `messages_send`.
