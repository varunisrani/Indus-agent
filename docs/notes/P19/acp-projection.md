# Task: Event projection (P19-10) Phase: 19 Owner: wave-30 builder A2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/events.py` (message id
allocator, plan from todos, tool start/complete/flush), `acp_adapter/tools.py` (kind map, titles,
start content, completion formatters, diff content, truncation, abandoned calls),
`acp_adapter/gateway_reply.py` (segments, final remainder exactly once, tool bubbles closed),
`acp_adapter/gateway_server.py` (`_events`, `_project`, `_project_session_state`, usage, title,
tools-changed notice, replay gap). Our `protocol/events.py`, `service/daemon_host.py`
(`message.complete` + `session.usage` after `turn.done`), `service/remote/host.py` (what reaches a
renderer), `tools/toolsets.py`, `tools/builtin/todo.py` (result shape), tool parameter schemas.
Closed before writing.
Evidence: none.

What it does (own words): every event of a held session becomes zero or more `session/update`
notifications, sent in event order by one sender task per agent connection (so a prompt can wait
until everything before its answer is on the wire).

- Dedupe: per session the projection keeps the snapshot's last `seq`; an event at or below it is
  dropped (a reattach replays from the cursor; the snapshot already covered those).
- `message.delta` → `agent_message_chunk{content:text, messageId}`; one UUID message id per
  assistant message: a tool start ends the current message, so text after a tool opens a new id.
  What was streamed per turn is remembered.
- `message.complete` finishes the reply: only the part the editor has not seen yet is sent (the
  whole text when nothing streamed, the tail when the stream was a prefix, nothing when equal; a
  different text is sent after a blank line); unless a retry receipt already showed that turn. The
  turn's terminal is recorded for the prompt waiter.
- `reasoning.delta` → `agent_thought_chunk` (same message id).
- `todo.updated`, and a `todo_list` tool result → `plan{entries[{content, priority:"medium",
  status}]}` (cancelled items stay as completed entries marked "[cancelled]").
- `tool.started` → `tool_call{toolCallId, title, kind, status:"in_progress", content, locations,
  rawInput}`; `tool.completed` → `tool_call_update{status: completed|failed, content, rawOutput?}`.
  `turn.done` closes every call of that turn still open as `failed` ("ended without a result").
- `session.usage` → `usage_update{size: context window, used: context tokens}` (only with a known
  window); the numbers are kept for the prompt's `usage`.
- `session.title` / `session.info` title → `session_info_update{title}` only when it changed.
- `notice` → a short agent text segment (its own message id).
- A tools-changed notice (MCP reload) → "Editor MCP tools changed …; the new tool list applies
  from the next turn."
- `on_gap` (replay gap) → the session's waiters fail as a transport failure.

Tool kinds (`tool_kinds.py`): read (`read_file`, `skill_view`, `skills_list`, `browser_snapshot`,
`browser_vision`, `browser_get_images`, `browser_console`, `vision_analyze`), edit (`write_file`,
`patch`, `skill_manage`), search (`search_files`, `session_search`), execute (`terminal`,
`process_manage`, `execute_code`, `browser_click`/`type`/`scroll`/`press`/`back`/`dialog`/`cdp`,
`delegate_task`, `image_generate`, `text_to_speech`, `video_*`, `computer_use`), fetch
(`web_search`, `web_extract`, `browser_navigate`), think (`_thinking`), other (`todo_list`,
`memory`, `clarify`, `cronjob_manage`, MCP and unknown tools). Every built-in tool is listed
explicitly; a registry-driven test fails when a new tool has no entry.

Content: `write_file` start → a diff `{path, oldText:null, newText:content}`; `patch` → diff
`{path, oldText: old_string, newText: new_string}`; `terminal` → `$ command`; `execute_code` → the
code in a fence (cut at 1,200 chars); read/search starts have no content (title + location say it);
other tools with arguments → their JSON (cut). Completion text: the result cut at 5,000 chars with a
"(N characters in all)" tail; `read_file` output fenced so editors keep the line numbers literal.
Titles: `name: <preview>` (command, path, pattern, query, URL; 80 chars).

Inputs → outputs: `EventBase` → `session/update` payloads.

Files: `src/indus/acp/projection.py`, `src/indus/acp/tool_kinds.py`,
`tests/unit/acp/test_projection.py`, `tests/unit/acp/test_tool_kinds.py`; shared: `acp/agent.py`
(the sink hands events to the projection), `acp/history.py` uses `tool_kinds` for replay (through
the agent's `tool_kinds` attribute it already reads).

Vendor names used (schema v1.19.0): `session/update`; updates `agent_message_chunk`,
`agent_thought_chunk` (`ContentChunk{content, messageId}`), `tool_call` (`ToolCallStart`),
`tool_call_update` (`ToolCallProgress`), `plan` (`AgentPlanUpdate{entries[PlanEntry{content,
priority, status}]}`), `usage_update{size, used}`, `session_info_update{title}`; tool kinds `read`,
`edit`, `delete`, `move`, `search`, `execute`, `think`, `fetch`, `switch_mode`, `other`; statuses
`pending`, `in_progress`, `completed`, `failed`; tool content `content` / `diff{path, oldText,
newText}`; `ToolCallLocation{path, line}`.

Edge cases / failure modes:
- a duplicate event after reattach → dropped; a delta of empty text → nothing;
- `tool.completed` for an unknown call id (started before the snapshot) → still an update;
- a result that is JSON with `success:false` / `error` → `failed` only when `ok` is false (the
  executor's verdict wins);
- a failed send to the editor is logged and the next update still goes.

Our design differences / decisions:
- No `message.interim` event exists in our protocol (commentary is streamed as deltas and the
  tool start closes the message), so there is no separate interim path.
- Formatters are deliberately fewer than the reference's (one generic text body, edits as diffs,
  terminal as the command, read output fenced, todo as plan).

Tests we will write:
- replayed event fixture → golden list of updates, each validated against the vendor models;
- duplicate seq dropped; open tool at turn end → failed; 6,000-char output cut to 5,000;
- message id changes after a tool; final remainder exactly once; title only when changed;
- every built-in tool maps to a kind (registry-driven).

## Decisions for the lead
- Notices (`notice` events) are shown as agent text segments.

Compare checklist (filled after the rewrite; reopened `events.py`, `tools.py`, `gateway_reply.py`, `gateway_server.py` `_project`):
- [x] kind map covers every reference kind group with our names (test: `tests/unit/acp/test_tool_kinds.py::test_kind_groups`, `::test_every_tool_in_our_registry_and_toolsets_has_an_explicit_kind`)
- [x] message ids: new id after a tool start; thought chunks share the id (test: `tests/unit/acp/test_projection.py::test_a_replayed_turn_projects_to_the_golden_updates`)
- [x] final remainder exactly once; retry receipt suppresses the second showing (test: `test_projection.py::test_the_final_text_is_shown_exactly_once`, `::test_a_retry_receipt_shows_the_reply_once`)
- [x] open tools at turn end → failed (golden test)
- [x] usage / title / tools-changed notice / plan from todos (test: `test_projection.py::test_titles_only_when_changed_and_notices_as_text`)
- [x] duplicate and snapshot seqs dropped (test: `test_projection.py::test_duplicate_and_snapshot_events_are_dropped`); gap → waiters fail (`Projection.on_gap`)
- [x] 6,000-char output cut to 5,000 (test: `test_tool_kinds.py::test_output_is_cut_at_five_thousand_characters`)
- Deliberate differences: a call's status is the executor's verdict (`ok`) only — no result-text
  heuristic; edits show their diff at start (the reference showed it only for auto-approved edits);
  far fewer per-tool formatters; no `rawOutput`; open calls close at `turn.done` (the reference at
  the reply's end).
- Not taken: `message.interim` (no such event in our protocol).

## Status

Done (2026-10-20), wave-30 builder A2. Shared: `acp/agent.py` (the sink feeds the projection; the
agent's `tool_kinds` drives the history replay's kinds), `acp/sessions.py` (`attach` sets the
seq floor and known title).
