# Task: MCP serve — messages_send (P19-16) Phase: 19 Owner: wave-30 builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `mcp_serve.py` (the
`messages_send` handler and its target examples), `tests/test_mcp_serve.py` (missing-argument case).
Research 09 §3.12; R02.26 / D-16.37 (delivery is a library, not a model tool).
Evidence: none.

What it does (own words): `messages_send {target, message}` lets the MCP client post a text into a
chat on one of the platforms the profile's running gateway is connected to. The target is
`platform:chat_id` (optionally `platform:chat_id:thread_id`), or a platform plus a friendly chat
name (`telegram:Family`, `discord:#general`) looked up in the gateway's channel directory. The MCP
server never talks to a platform itself: it asks the daemon over its owner-only control socket,
and the daemon sends through its delivery router and the platform's outbound sender, which records
the delivery-ledger row before the platform send (so a crash mid-send is redelivered once, never
lost or doubled). Text only. Without a daemon the answer is `{error: "daemon not running"}`.

Inputs → outputs: `{target, message}` → `{sent: true, target, platform, chat_id, thread_id?,
message_id?, verified}` or `{error}`; an ambiguous friendly name → `{error, candidates[]}`.

Files: `src/indus/mcp_serve/send.py`, `src/indus/service/messaging_bridge.py` (control-socket wire
for the bridge verbs, below both surfaces), `src/indus/gateway/daemon/bridge_verbs.py` (the
daemon's `deliver-message` verb), `tests/contract/mcp_serve/test_send.py`.
Shared (minimal): `src/indus/gateway/daemon/side_services.py` (register the bridge verbs),
`src/indus/gateway/control_socket.py` (verb names known and owner-only),
`src/indus/gateway/delivery/router.py` (`send(..., session_key=, message_ref=)` so the sender
records a ledger row).

Edge cases / failure modes:
- Empty target or message, a target without a platform, `all`/`origin`/a bare platform (home
  channel broadcast) → `{error}`: the bridge sends to one named chat only.
- Friendly name: exact chat id first, then the name without `#` and case, then `guild/name`, then a
  unique prefix; two or more matches → error with the candidates (`platform:id (name)`); no match →
  the text after the platform goes to the daemon as written (it may be a chat id the directory has
  not seen), and the daemon's router answers `unknown_channel` for an unknown `#name`.
- Message longer than the cap (16,000 characters) → `{error}` (the control socket's request limit
  is 64 KiB).
- Daemon not running / unsafe home / timeout → `{error: "daemon not running"}` or the code;
  a daemon that answers but lacks the verb (older build) → `{error}` naming the missing verb.
- Platform not connected, dead chat, send failure → the daemon's receipt error.
- The delivery-ledger row uses a session key of its own (`mcp-send:<platform>:<chat>`), never the
  chat's conversation key: crash recovery dedupes a turn's owed reply on rows of that key.

Our design differences / decisions:
- The reference called the model's send tool in-process (live connections needed in the same
  process); ours goes through the daemon that holds the connections (D-16.37, P15/P17).
- The verb is owner-only (the OS must confirm the peer is this user).
- Mirror notes: the router writes the usual "delivered" note into the chat's conversation.

Tests we will write (`tests/contract/mcp_serve/test_send.py`):
- a real control socket in the profile home serving the daemon's `deliver-message` verb over a real
  `DeliveryRouter`, `OutboundSender` with a `DeliveryLedger` and a recording adapter: one call →
  one adapter send and one ledger row `delivered`, the chat key not used.
- friendly name resolved through the directory; ambiguous name → error with candidates.
- no daemon → `{error: "daemon not running"}`; missing arguments; broadcast targets refused.

## Decisions for the lead
- A target the daemon's grammar refuses (`slack:C1`) answers `bad_target: <the grammar's message>`.
- Message cap 16,000 characters (`send.MAX_MESSAGE_CHARS`); longer text is refused, not cut.
- Ledger key `mcp-send:<platform>:<chat_id>` and message ref `mcp-send:<random>`; one row per call
  (a client retry is a new message).
- Verb name `deliver-message` (owner-only), wire constants in `indus.service.messaging_bridge`.

Compare checklist (filled after the rewrite; reopened `mcp_serve.py`):
- [x] missing arguments, blank message, bare platform / `all` / `origin` / empty chat refused (test: `tests/contract/mcp_serve/test_send.py::test_without_a_daemon_or_arguments_the_answer_is_an_error`)
- [x] `platform:chat_id` through the daemon, one ledger row `delivered`, chat key unused (test: `tests/contract/mcp_serve/test_send.py::test_one_send_is_one_ledger_backed_platform_send`)
- [x] friendly names, `#name`, ambiguous name with candidates (test: `tests/contract/mcp_serve/test_send.py::test_friendly_names_resolve_through_the_directory`)
- [x] daemon refusals: platform not connected, unknown `#name`, a malformed platform address (the daemon's grammar message) (test: `tests/contract/mcp_serve/test_send.py::test_the_daemons_refusals_come_back_as_errors`)
- Deliberate differences: the reference called the model's send tool in its own process; ours asks the daemon (D-16.37) and records a ledger row; ambiguous names are an error with candidates instead of the send tool's own resolution.
- Not taken: media attachments (text only, as the row says).

## Status

Done (2026-10-20). `messages_send` and the daemon's `deliver-message` verb built and tested (4 tests). Shared: `src/indus/gateway/delivery/router.py` (`send(..., session_key=, message_ref=)`), `src/indus/gateway/daemon/side_services.py` (bridge verbs registered), `src/indus/gateway/control_socket.py` (verbs known, owner-only); new `src/indus/service/messaging_bridge.py`, `src/indus/gateway/daemon/bridge_verbs.py`. ACC-M10-3.
