# Task: `session/load`, `session/resume`, `session/fork` (P19-06) Phase: 19 Owner: wave-29 builder A1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/gateway_server.py`
(`_resume`, `_moved`, `_move_workspace`, `_rebind_editor_mcp`, `load_session`, `resume_session`,
`fork_session`, `_editor_id`), `acp_adapter/gateway_session_meta.py`, `acp_adapter/server.py`
(`_history_replay_updates` and its helpers), `acp_adapter/catalog.py` (`logical_session_id`). Our
`state/lineage.py`, `core/transcript.TranscriptRow`, `state/messages.get_in`. Closed before writing.
Evidence: none.

What it does (own words): the editor reopens a conversation it knows by id.

- The id may be an old one: compression rotates the stored id. We map it to the logical session
  (the root of its compression lineage) and keep an alias, so every update for that session is
  addressed to the id **this editor** used on its latest load.
- If the session's cwd differs from the editor's (after translation and normalisation), a
  `workspace` mutation moves it (idle sessions only); a session with no cwd gets a short message
  instead ("this session has no working folder; the editor's folder is not applied").
- `mcpServers`: a non-empty list is the editor's selection for this session (`editor_mcp`
  mutation; on a policy conflict the rebind runs and the resume is retried once); an empty list
  keeps what the session has.
- The session is subscribed, then its whole stored history is streamed as `session/update`
  notifications **before** the response (R09.9): user chunks, agent thought chunks, agent message
  chunks, and for each tool call a `tool_call` start plus a `tool_call_update` with the result.
- Pending approval/clarify prompts are offered again (through the subscription sink); lost turns
  are announced; the response carries modes, config options and provenance `_meta`.
- `session/resume` is the same as load (the vendor marks it unstable; the editor gets the same
  replay).
- Fork: a `branch` mutation on the parent (the editor's MCP servers for the child, or a copy of
  the parent's), a workspace move on the child when the cwd differs, subscribe, respond with the
  child id (no history replay: the editor shows the fork as a new thread).

Inputs → outputs: `session/load|resume {sessionId, cwd, mcpServers}` → N notifications + response;
`session/fork {sessionId, cwd, mcpServers}` → `{sessionId: child, modes, configOptions, _meta}`.

Files: `src/indus/acp/history.py`, `tests/contract/acp/test_session_load.py`.

Vendor names used (0.12.1 / v1.19.0): `session/load` (stable), `session/resume`, `session/fork`
(unstable tier in the SDK router); `user_message_chunk`, `agent_message_chunk`,
`agent_thought_chunk`, `tool_call{toolCallId,title,kind,status,rawInput,content}`,
`tool_call_update{toolCallId,status,content,rawOutput}`; `LoadSessionResponse`,
`ResumeSessionResponse`, `ForkSessionResponse{sessionId,…}`.

Edge cases / failure modes:
- Unknown id → `not_found` (-32002).
- A tool result whose call is not in the replayed history: skipped.
- Empty history: no notifications, plain response.
- A failed replay update stops the replay (logged) but the load still answers.

Our design differences / decisions:
- Tool kinds in the replay use P19-10's kind map once it lands; until then a provisional mapping
  hook (`history.ToolKinds`, default `other`) with outputs cut at 5,000 characters.
- Compaction-summary `_meta` flags are not set (our summaries are not marked in rows yet).

Tests we will write:
- recorded frames: every history update before the load response;
- a compressed session loaded by its old id → updates addressed to the old id;
- fork returns a child that has the parent's MCP servers (local: in memory; attached: the
  mutation payload);
- cwd differs → workspace moved; no cwd → message.

## Decisions for the lead
- Fork does not replay the child's history (the editor treats it as a new thread).

Compare checklist (filled after the rewrite; reopened `gateway_server._resume`/`fork_session`,
`server._history_replay_updates`):
- [x] alias handling
- [x] workspace move / no-cwd message
- [x] MCP selection rules
- [x] history before response
- Deliberate differences: see above.

## Status

Done (2026-10-20), wave-29 builder A1. Tests: see the task's Verify line; all green with the gate checks. Trap/ACC ledger rows for R09.9/R09.13/R09.14/R09.16 and ACC-M1/M2/M8 left to the lead (follow-up).
