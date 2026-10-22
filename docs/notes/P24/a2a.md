# Task: A2A adapter, both directions (P24-10) Phase: 24 Owner: wave-37 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/a2a/adapter.py`
(whole: routes, auth order, method table, task preparation and waiting, streaming, task queries,
push configs, send / clarify resolution), `plugins/platforms/a2a/protocol.py` (task states, roles,
markers, error codes, card, parts, task store, turn tracker), `plugins/platforms/a2a/security.py`
(auth, bind safety, trusted peers, injection framing, outbound redaction, blocked prefixes),
`plugins/platforms/a2a/tools.py` (peer config, `a2a_call`, discover/list/history/orchestrate),
`plugins/platforms/a2a/__init__.py` (registration, platform hint). Research 06 §3.3 (A2A row),
R06.24.
Evidence: none.

Protocol pin: A2A (Agent2Agent) protocol v1.0 (spec tag 1.0.x): JSON-RPC 2.0 over HTTP; agent card
at `/.well-known/agent-card.json`; methods `SendMessage`, `SendStreamingMessage`, `GetTask`,
`CancelTask`, `ListTasks` (the 0.3 slash names `message/send`, `message/stream`, `tasks/get`,
`tasks/cancel`, `tasks/list` accepted too); task states `TASK_STATE_SUBMITTED`, `_WORKING`,
`_INPUT_REQUIRED`, `_COMPLETED`, `_FAILED`, `_CANCELED`, `_REJECTED`; roles `ROLE_USER`,
`ROLE_AGENT`; parts discriminated by member (`text`, `url`, `raw`, `data`); errors −32700,
−32600, −32601, −32602, −32603, −32001 (task not found), −32002 (not cancelable), −32003 (push
notifications not supported), −32004 (unsupported operation). Header `A2A-Version: 1.0`.
No A2A library: our own JSON-RPC over the P16-01 listener kit (uvicorn, base) and httpx (base).

What it does (own words): inbound, the gateway serves an agent card and the JSON-RPC endpoint on
its own listener (127.0.0.1 by default; another host needs `a2a.public: true`). Every JSON-RPC call
needs `Authorization: Bearer <token>`; tokens are per trusted peer (`a2a.inbound_peers`, each
token in the secret store as `INDUS_A2A_TOKEN_<PEER>`), the matching token names the peer, and
with no token configured every call is refused (401) — fail closed. A `SendMessage` becomes one
inbound event in a session keyed per peer and context (`<peer>/<contextId>`), marked as coming
from an untrusted remote agent and never allowed to run slash commands; the call waits for the
turn's final reply and answers with a completed task (artifact + status message). When the agent
asks a clarify question the task ends `input-required` with the question, and the peer answers in
the same context. Streaming answers the same task as server-sent events (submitted, working, then
the artifact and the final status, and the stream closes). Peers report chat type `dm` but the
platform's DMs are not private (R06.24): pairing codes and login links are never sent there.
Outbound, the `a2a_call` tool (bundle `indus-a2a`) sends a message to a configured peer
(`a2a.peers.<name>.url`, key `INDUS_A2A_PEER_<NAME>_KEY`) and returns its reply, state and context
id; a privacy note is put in front of the text and credentials are masked before it leaves.
Peer URLs that are link-local, loopback or private are refused unless the peer is marked
`trusted: true` (SSRF; the shared URL policy and its connect-time transport).

Inputs → outputs: HTTP GET card / POST JSON-RPC → inbound events → final reply / clarify →
JSON-RPC result or SSE; tool args `{agent, message, context_id?}` → peer reply text.

Files: `src/indus/gateway/adapters/a2a/__init__.py`, `.../adapter.py`, `.../protocol.py`,
`.../tools.py`, `tests/unit/gateway/adapters/a2a/test_adapter.py`,
`tests/fixtures/a2a/schema/a2a-v1-subset.json`; shared: `src/indus/tools/toolsets.py`
(`a2a`, `indus-a2a`), `src/indus/foundation/config/schema.py` (`a2a.*`, `platforms.a2a`) and its
test.

Edge cases / failure modes:
- body over 1 MiB → 413; not JSON → −32700; not an object / wrong `jsonrpc` → −32600; unknown
  method → −32601; bad params (no text, bad context id) → −32602; `A2A-Version` other than 1.0.x →
  −32602.
- no token configured, missing header, wrong scheme or wrong token → HTTP 401 with
  `WWW-Authenticate: Bearer`, nothing read into a turn; comparisons constant-time.
- an empty message → task rejected; more than `a2a.max_turns_per_context` turns in one context →
  rejected (two agents ping-ponging forever).
- the turn does not answer within `a2a.reply_timeout_seconds` → task failed (fake clock).
- a peer sees and cancels only its own tasks (another peer's id → task not found).
- cancel of a finished task → −32002; cancel of a running one marks it canceled and answers the
  waiting call; the turn's later reply is dropped.
- interim sends (progress, notices) never resolve a task; only the final reply does, oldest task of
  the context first.
- disconnect fails every waiting task (`failed`, "gateway shutting down").
- outbound: unknown peer, refused URL, HTTP 401/403/429/5xx, JSON-RPC error, non-JSON answer —
  each a plain tool error naming the peer, never the key.

Our design differences / decisions:
- Names: `A2AAdapter`, `A2ATaskStore`, `a2a_call`; sessions `<peer>/<contextId>`.
- Fail closed without tokens even on loopback (the reference let loopback callers in without a
  token); a peer's identity comes only from its token.
- Push notifications, `SubscribeToTask` and multi-agent served routes / tenants are not built:
  the card says `pushNotifications: false`; those methods answer −32003 / −32004 (recorded
  "later"). `a2a_discover`, `a2a_list`, `a2a_history`, `a2a_orchestrate` and the on-disk
  conversation log are not built (recorded "later").
- Streaming is a buffered SSE answer (all events written when the turn ends) on the listener kit;
  true incremental streaming would need a streaming reply in the kit.
- Approval cards are not delivered to peers (a remote agent must not approve the operator's
  commands); the approval waits out its normal timeout.
- The vendor JSON schema could not be fetched (no network in this build): requests are validated
  against our own subset schema of the v1.0 shapes we produce and accept; replace it with the
  vendor file at the pinned tag at landing.

Tests we will write:
- no token configured → 401; wrong token → 401; right token → peer identity.
- card served without auth; shape validates.
- SendMessage round-trip: event in session `<peer>/<ctx>`, untrusted framing, no commands, final
  reply → completed task with artifact; requests and answers validate against the schema.
- clarify → `input-required`; answer in the same context continues.
- streaming SSE events in order; GetTask / ListTasks / CancelTask scoped per peer; errors.
- timeout → failed on the fake clock; disconnect fails waiters; lifecycle idempotent; bind
  refusal of a public host without `public: true`.
- `dm_is_private` false (R06.24); a2a_call: privacy note + redaction, 10.0.0.5 refused unless
  trusted, trusted peer reached through an injected transport, input-required hint, errors.

## Decisions for the lead
- Registry wiring (not in my files): add `A2A_ENTRY` from `indus.gateway.adapters.a2a` to
  `builtin_entries` / `BUILTIN_NAMES` (no credential; turned on by `platforms.a2a.enabled: true`),
  a NON_CHAT row in `tests/contract/gateway/test_adapter_matrix.py` pointing at
  `tests/unit/gateway/adapters/a2a/test_adapter.py` (A2A cannot post unsolicited messages, so the
  chat suite's send checks do not fit), a setup-wizard step, a target-parser rule, and
  `BuiltinModule("indus.gateway.adapters.a2a.tools")` in `indus.tools.builtin.BUILTIN_MODULES`.
- No `a2a` extra is needed (stdlib + base dependencies); the phase file's `a2a` extra row can be
  dropped.
- Vendor schema follow-up: fetch the A2A v1.0.x JSON schema (Apache-2.0) into
  `tests/fixtures/a2a/schema/` and point the test at it.

Compare checklist (filled after the rewrite; reopened the five reference files):
- [x] methods: SendMessage / message/send (test: `tests/unit/gateway/adapters/a2a/test_adapter.py::test_a_message_becomes_one_untrusted_turn_and_the_final_reply_completes_the_task`, `::test_legacy_method_names_answer_a_bare_task`), SendStreamingMessage (`::test_streaming_answers_server_sent_events_in_order`), GetTask / ListTasks / CancelTask (`::test_tasks_are_visible_and_cancelable_only_by_their_peer`, `::test_cancel_answers_the_waiting_call_as_canceled`), push-config methods → −32003 and SubscribeToTask → −32004 (`::test_protocol_errors`)
- [x] task states: submitted, working, input-required (clarify), completed, failed (timeout, shutdown), canceled, rejected (empty, anti-loop) (tests: `::test_a_clarify_question_surfaces_as_input_required`, `::test_no_reply_in_time_fails_the_task`, `::test_disconnect_fails_waiting_tasks`, `::test_an_empty_message_and_a_runaway_context_are_rejected`)
- [x] auth: no token → 401, wrong token → 401, token names the peer, constant-time (tests: `::test_no_token_configured_refuses_every_call_with_401`, `::test_missing_or_wrong_credentials_are_401`, `::test_the_token_names_the_peer`)
- [x] loopback bind by default; a public host needs `a2a.public` (test: `::test_lifecycle_is_idempotent_and_a_public_host_needs_the_flag`)
- [x] R06.24 DMs not private; approvals withheld (tests: `::test_the_registry_entry_marks_dms_not_private`, `::test_approval_cards_are_never_offered_to_peers`)
- [x] outbound SSRF: private refused unless trusted, metadata never (tests: `::test_outbound_to_a_private_address_is_refused_unless_trusted`, `::test_trust_never_opens_cloud_metadata`); privacy note + masking (`::test_outbound_carries_the_privacy_note_masks_secrets_and_conforms`)
- [x] gap found: the reference rate-limits each peer (60 calls a minute) — added (429, −32051), pinned by `::test_a_peer_over_the_rate_is_answered_429`
- [x] gap found: a cancel racing the final reply left the stale "working" record as the answer — fixed in `_finish` (the stored outcome stands), pinned by `::test_cancel_answers_the_waiting_call_as_canceled`
- Deliberate differences: fail closed without tokens even on loopback (the reference allowed loopback callers without a token); input-required comes from the clarify card, not a `[INPUT_REQUIRED]` text marker the model must remember to write; inbound text is framed as untrusted and never runs commands (`allow_control` false) instead of regex-defanging chat-template markers; outbound masking covers credential shapes and the profile's own secrets but not e-mail addresses; a peer's own tasks only (no agent-slug / tenant routing); sessions keyed `<peer>/<context>` so two peers reusing one context id never share a session.
- Not taken: push notifications (and their HMAC signing and callback SSRF checks), SubscribeToTask, served-agent routes and tenants, forwarding to other profiles, message-id retry dedupe, on-disk conversation log and audit file, `/metrics`, `a2a_discover`, `a2a_list`, `a2a_history`, `a2a_orchestrate`, the setup wizard step — recorded "later".

## Status

Done (2026-10-21). Server adapter, protocol, `a2a_call` and the subset schema written and tested
(`tests/unit/gateway/adapters/a2a/test_adapter.py`, 35 tests). Shared files changed:
`src/indus/tools/toolsets.py` (`a2a`, `indus-a2a`), `src/indus/foundation/config/schema.py`
(`a2a.*`, `platforms.a2a`) and its test. Registry, matrix, wizard, target and builtin-module wiring
are the lead's (see Decisions).

## Integration (P24 integrator, 2026-10-21)

Wired: `A2A_ENTRY` is a built-in platform (non-chat row in the adapter matrix, setup-wizard text, no
target rule: nothing can be pushed to a peer). `a2a_call` is not a built-in tool module: built-in
modules may not import the gateway (R02.26, `test_router.py::test_no_model_tool_wraps_the_router`),
so its registration from the gateway host is owed (D-16.791).

## Wiring (wave 42, WB — D-16.799)

Wired: `a2a_call` is registered by the daemon (`gateway_tool_registry`; built-in modules may not import
the gateway, R02.26), offered while `a2a.peers` is configured (its probe). Registry entry, adapter
matrix row, wizard text and "not a target" were done at P24 integration and are unchanged.
Test: `tests/unit/gateway/daemon/test_host_tools.py::test_the_daemon_registers_message_agent_and_a2a_call`.
