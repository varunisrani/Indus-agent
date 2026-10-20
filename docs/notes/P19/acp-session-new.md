# Task: `session/new` (P19-05) Phase: 19 Owner: wave-29 builder A1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/gateway_server.py`
(`new_session`, `_bypass_route`, `_session_fields`, `_after_attach`), `acp_adapter/session.py`
(cwd translation, toolset expansion), `acp_adapter/provenance.py` (all),
`acp_adapter/gateway_session_meta.py` (modes, model state, usage update, commands update),
`hermes_constants.py` (`windows_path_to_wsl`, `wsl_unc_path_to_posix`,
`translate_cwd_for_wsl_backend`). Our `service/creation_policy.py`, `platform/facts.is_wsl`,
`state/lineage`. Closed before writing.
Evidence: none.

What it does (own words): the editor opens a new conversation in a folder.

1. The cwd is translated: under WSL a Windows drive path (`E:\Projects\x`) becomes
   `/mnt/e/Projects/x` and a `\\wsl.localhost\<distro>\…` (or `\\wsl$\…`) UNC path becomes the
   POSIX path inside the distro; elsewhere nothing changes.
2. The launch flags this process was started with (`INDUS_YOLO_MODE`, safe mode, ignore-user-config,
   …, via `creation_policy.launch_flags_from_env`) are frozen into the session's policy; a safe-mode
   or ignore-config launch must name an explicit model **and** provider (`INDUS_MODEL` /
   `INDUS_INFERENCE_MODEL`, `INDUS_PROVIDER` / `INDUS_INFERENCE_PROVIDER`), else creation is refused
   with a message saying how to launch.
3. The backend creates the session with a fresh random request id, source `acp`, editor policy
   `{mcp_servers, edit_approval_policy: "ask"}`. The editor's MCP specs (commands, env, headers) are
   held in memory only — never written to the store or a log.
4. Live reasoning is asked for (`show_reasoning`, 10 s budget, a failure ignored).
5. The answer is `{sessionId, modes, configOptions, _meta: {indus: {sessionProvenance}}}`; after the
   response frame is queued (one loop yield), `available_commands_update` and `usage_update` are
   sent (R09.9): an editor routes updates only for a session id it already knows.

Provenance (`_meta.indus.sessionProvenance`): `acpSessionId` (the id the editor holds),
`currentSessionId`, `rootSessionId`, `parentSessionId`, `sessionKind` (`root` / `continuation`),
`compressionDepth` — derived from the store's compression lineage, read-only; omitted when the store
cannot be read.

Inputs → outputs: `session/new {cwd, mcpServers}` → response + two notifications.

Files: `src/indus/acp/sessions.py`, `src/indus/acp/paths.py`, `tests/contract/acp/test_session_new.py`,
`tests/unit/acp/test_paths.py`.

Vendor names used (agent-client-protocol 0.12.1, schema v1.19.0): `session/new` (stable);
`NewSessionRequest{cwd, mcpServers[], _meta}`, `McpServerStdio{name, command, args, env[]}`,
`HttpMcpServer{type:"http", name, url, headers[]}`, `SseMcpServer{type:"sse", …}`;
`NewSessionResponse{sessionId, modes, configOptions, _meta}`, `SessionModeState{currentModeId,
availableModes[{id,name,description}]}`, `SessionConfigOptionSelect{type:"select", id, name,
category:"model", currentValue, options[{value,name}]}`; notifications `session/update` with
`available_commands_update{availableCommands[{name, description, input{hint}}]}` and
`usage_update{size, used}`.

Edge cases / failure modes:
- Safe mode without provider → refused, nothing created.
- `usage_update` only when the context window is known (> 0).
- A failed post-response update is logged at DEBUG only.
- Relative or empty cwd: kept as given (the editor sends absolute paths).

Our design differences / decisions:
- **Vendor change:** schema v1.19.0 has no `models` field on session responses (the model picker
  moved to `configOptions` with `category: "model"`), so the model picker is sent as one select
  config option `model` (current value `provider:model`; the full catalogue is P19-12's).
- Provenance key names are ours (`currentSessionId` etc.) under `_meta.indus`.

Tests we will write:
- recorded client order: response frame precedes both updates;
- `E:\Projects\x` → `/mnt/e/Projects/x` with the WSL flag; UNC forms; no-op off WSL;
- safe mode without provider refused; with both → created;
- MCP env never written to the store (store file scan for the env value).

## Decisions for the lead
- `models` → `configOptions` (vendor schema change in the pinned SDK).

Compare checklist (filled after the rewrite; reopened `gateway_server.new_session`, `session.py`,
`provenance.py`):
- [x] cwd translation
- [x] launch flags frozen; bypass needs model + provider
- [x] post-response updates after the response
- Deliberate differences: see above.

## Status

Done (2026-10-20), wave-29 builder A1. Tests: see the task's Verify line; all green with the gate checks. Trap/ACC ledger rows for R09.9/R09.13/R09.14/R09.16 and ACC-M1/M2/M8 left to the lead (follow-up).
