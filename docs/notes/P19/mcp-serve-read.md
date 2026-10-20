# Task: MCP serve — server and read tools (P19-14) Phase: 19 Owner: wave-30 builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `mcp_serve.py` (module helpers,
routing-index loading, content and attachment extraction, the read tool handlers, server creation
and `run_mcp_server`), `tests/test_mcp_serve.py` (helper, extraction, end-to-end read tool and
coercion cases), `website/docs/user-guide/features/mcp.md` (searched for a "running as an MCP
server" section: the pinned page has none, it covers only the client side). Research 09 §3.12,
R09.10, R09.14, R03.14, R03.16, R03.17, R06.22.
Evidence: none.

What it does (own words): `indus mcp serve` turns Indus into a stdio MCP server that another agent
(an editor assistant, a coding agent) starts as a child process. Over it the other agent can see the
messaging conversations the gateway has had: list them, look at one, read its recent user and
assistant text, and list a message's attachments. Reads go straight to the profile's session store
through one private read-only connection, so they work when no gateway runs. Every tool answers a
JSON text; a problem is a JSON object with an `error` field, never an MCP error or a traceback.
Stdout carries MCP frames only; logs go to stderr (WARNING, DEBUG with `--verbose`).

Inputs → outputs:
- `conversations_list {platform?, limit=50 (1..200), search?}` → `{count, conversations[{session_key,
  session_id, platform, chat_type, display_name, chat_name, user_name, updated_at}]}`, newest
  first; `search` is a case-insensitive substring of display name, chat name or key.
- `conversation_get {session_key}` → the row with chat/thread ids, created/updated times and the
  session's input/output/total tokens, or `{error}`.
- `messages_read {session_key, limit=50 (1..200)}` → `{session_key, count, total_in_session,
  messages[{id, role, content ≤ 2000 chars, timestamp}]}`: live user/assistant rows with text,
  in row-id order, the last `limit`.
- `attachments_fetch {session_key, message_id}` → `{message_id, count, attachments[], refused[]}`:
  image parts (`{type: image, url}`), other non-text parts (`{type, data}`), and `MEDIA:` paths
  from the text that pass the media path policy (`{type: media, path}`); a path the policy refuses
  is listed in `refused` with the reason, never as an attachment.
- `channels_list {platform?}` → `{count, channels[{target, platform, name, chat_type}]}` from the
  gateway's saved channel directory, else derived from the routing index.

Files: `src/indus/mcp_serve/{__init__,server,read_tools,store}.py`,
`src/indus/cli/commands/mcp_serve.py`, `tests/contract/mcp_serve/{conftest,test_read_tools}.py`.
Shared: `src/indus/cli/commands/mcp.py` (the `serve` row of the `mcp` group),
`src/indus/cli/entry.py` (`mcp serve` starts in stdio logging mode),
`src/indus/safety/media_policy.py` (the media policy moved below the surfaces; the gateway module
re-exports it), contract tables.

Edge cases / failure modes:
- `limit` 999 → 200, 0 → 1, `"x"` / null / a float string → the default; the input schema accepts
  an integer or a string so the SDK never rejects the call before we clamp it.
- No store yet (fresh profile) → every read answers `{error: ...}` that names the missing store;
  the store is never created. A store from a newer build → `{error}` too.
- Unknown session key, a route whose session row is gone, an unknown message id → `{error}`.
- Content stored as parts (lists) → only `text` parts count as text; tool rows, system rows and
  empty assistant rows (tool calls only) are skipped; rewound (inactive) rows are not shown.
- `MEDIA:` inside code spans/fences is not a directive (the shared extractor); `~/.ssh/id_rsa`,
  `/etc/passwd`, a symlink from an allowed folder into a denied place → refused.
- Timestamps that go backwards: order is the row id (R03.14).
- A home path with `#`, `?` or `%`: the read-only URI is percent-encoded (shared helper).
- A tool runs while another reads: one connection, guarded by a lock; threads never share it at
  once. A broken connection is closed and reopened on the next call.

Our design differences / decisions:
- Server name `indus`, our own instructions text. Tools registered in a fixed order; the two
  approval tools only when wired to the daemon (P19-17, D-16.30).
- One read-only connection (`state.browse.open_readonly`, mode=ro URI, no schema setup) per
  server process, shared by the read tools and the event bridge: bounded by construction (R03.17)
  and a write through it raises (R03.16).
- The routing index is our `gateway_routes` table (no legacy JSON fallback: we never had one).
- Times are ISO-8601 UTC strings; token totals come from the session row.
- `attachments_fetch` applies the gateway's media path policy (R06.22) to `MEDIA:` paths; the
  reference listed any path written in the text.
- The `mcp` CLI group gains `serve`; its code lives in `indus.mcp_serve` and is loaded by name
  (layer rule D-16.246, like `tui-bridge` / `gateway run`).

Tests we will write (`tests/contract/mcp_serve/test_read_tools.py`, MCP SDK in-memory client):
- tool list order exact (without the daemon: no approval tools); instructions and server name.
- conversations_list: newest first, platform filter case-insensitive, search on name/chat/key,
  limit clamp 999 → 200 and `"x"` → 50.
- conversation_get: tokens, unknown key error.
- messages_read: user/assistant text only, chronological by id when timestamps regress, last N,
  2,000-char cut, total_in_session, `limit="x"`.
- attachments_fetch: image part, allowed cached file, `~/.ssh` and `/etc` refused, unknown message.
- channels_list: from the directory file, from routes when there is none, platform filter.
- missing store → error JSON and no file created; the connection refuses writes.
- subprocess `indus mcp serve`: stdout parses as JSON-RPC frames only; a missing SDK → hint, exit 1.

## Decisions for the lead
- `attachments_fetch` returns paths, not file bytes (as the reference); refused paths are listed in
  a separate `refused` array with the policy's reason so a client can tell the user why.
- An inline `data:` image URL is not echoed back (it can be megabytes): the part reads
  `{type: image, inline: true, media_type, bytes}`. Change in `read_tools.attachments_of`.
- The media policy moved to `indus.safety.media_policy` (unchanged code; `gateway_section` read
  inline from the profile config) because `indus.mcp_serve` may not import `indus.gateway`.
  `indus.gateway.outbound.media_policy` re-exports every name.

Compare checklist (filled after the rewrite; reopened `mcp_serve.py`, `tests/test_mcp_serve.py`):
- [x] coercion of bad and out-of-range integers (test: `tests/contract/mcp_serve/test_read_tools.py::test_integers_are_clamped_and_non_numbers_take_the_default`, `tests/contract/mcp_serve/test_read_tools.py::test_conversations_are_listed_newest_first_with_filters`)
- [x] conversation fields, platform filter (any case), search on name / chat name / key, newest first (test: `tests/contract/mcp_serve/test_read_tools.py::test_conversations_are_listed_newest_first_with_filters`, `tests/contract/mcp_serve/test_read_tools.py::test_conversation_get_has_ids_times_and_token_totals`)
- [x] messages_read: user/assistant text only, multi-part text, last N, 2,000-char cut, total, id order (test: `tests/contract/mcp_serve/test_read_tools.py::test_messages_read_shows_user_and_assistant_text_in_id_order`, `tests/contract/mcp_serve/test_read_tools.py::test_messages_read_caps_the_limit_at_200`)
- [x] attachment extraction: image_url / image parts, other non-text parts, MEDIA tags — plus the media path policy (test: `tests/contract/mcp_serve/test_read_tools.py::test_attachments_follow_the_media_path_policy`)
- [x] channels_list from the directory, from the routing index when there is none or it is corrupt, platform filter (test: `tests/contract/mcp_serve/test_read_tools.py::test_channels_come_from_the_directory_else_from_the_routes`)
- [x] every tool has a description; registration order is the list order (test: `tests/contract/mcp_serve/test_read_tools.py::test_tool_list_order_is_exact_and_approval_tools_are_absent_without_a_daemon`)
- [x] missing SDK: hint on stderr, exit 1 (test: `tests/contract/mcp_serve/test_read_tools.py::test_a_missing_sdk_prints_a_hint_and_exits_1`); the CLI routes `mcp serve` and stdout stays frames only (test: `tests/contract/mcp_serve/test_read_tools.py::test_stdout_carries_only_mcp_frames`)
- [x] no store / read-only store (test: `tests/contract/mcp_serve/test_read_tools.py::test_a_missing_store_is_an_error_answer_and_is_never_created`, `tests/contract/mcp_serve/test_read_tools.py::test_the_store_is_opened_read_only`)
- Deliberate differences: the reference read a legacy `sessions.json` when the store had no routing rows and its gateway sessions were filtered to "active"; ours reads `gateway_routes` only (we never had the JSON index; a route is current by construction). `"3.9"` coerces to 3 and a boolean takes the default (the reference gave the default and 1). `MEDIA:` tags are found by the gateway's extractor (absolute or `~/` paths, not inside code) and checked by the media path policy, instead of any `MEDIA:\S+` text. Times are ISO-8601 UTC; the reference used local time. Display name falls back to the user name for DMs. Handoff (compression summary) rows are not shown as messages.
- Not taken: the legacy JSON index fallback (nothing of ours writes one).

## Status

Done (2026-10-20). Read tools, server, `indus mcp serve` command and stdio purity are built and tested (12 tests). Shared changes: `src/indus/cli/commands/mcp.py` (serve added to the group), `src/indus/cli/entry.py` (`STDIO_SUBCOMMANDS`), the media policy moved to `src/indus/safety/media_policy.py` with `src/indus/gateway/outbound/media_policy.py` re-exporting it, `tests/contract/cli/non_tty_table.yaml` (`mcp serve` long-running), traps R03.14/R03.16/R03.17/R06.22/R09.14 and ACC-M10-1.
