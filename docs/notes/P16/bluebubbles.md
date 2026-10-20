# Task: BlueBubbles adapter (P16-24) Phase: 16 Owner: wave-25 builder E10

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/bluebubbles/adapter.py`
(whole file: settings, REST helpers, connect and webhook registration, chat GUID resolution, text
and attachment sends, typing/read receipts, webhook parsing and dispatch, standalone send),
`plugins/platforms/bluebubbles/gateway_env.py`, `plugins/platforms/bluebubbles/plugin.yaml`,
`website/docs/user-guide/messaging/bluebubbles.md`. Research 06 §3.3 (BlueBubbles row), §3.5
(mention gating). Kit: `docs/notes/P16/http-kit.md`.
Evidence: none.

Vendor API: BlueBubbles Server REST API v1 (server ≥ 1.9, the plan's floor; the v1.9 webhook shape
nests the chat under `data.chats[0]`). Routes we call: `GET /api/v1/ping`, `GET /api/v1/server/info`
(`private_api`, `helper_connected`), `GET|POST /api/v1/webhook`, `DELETE /api/v1/webhook/{id}`,
`POST /api/v1/chat/query`, `POST /api/v1/chat/new`, `GET /api/v1/chat/{guid}`,
`POST|DELETE /api/v1/chat/{guid}/typing`, `POST /api/v1/chat/{guid}/read`,
`POST /api/v1/message/text`, `POST /api/v1/message/attachment` (multipart),
`POST /api/v1/message/react`, `GET /api/v1/attachment/{guid}/download`. Every REST call carries the
server password as the `password` query parameter (the server's only auth). Webhook events used:
`new-message`, `updated-message`. No Python library (httpx, a core dependency): no extra, nothing
to pin.

Capability flags: `max_len` 4,000 code points, `splits_natively` true, `markup` plain,
`supports_edit` false, `supports_delete` false, `supports_buttons` false, `owns_access_policy`
false, `auth_upstream` false, `dm_is_private` true, `push_capable` true.

What it does (own words): a Mac runs the BlueBubbles server, which bridges iMessage. The adapter
checks the server (`ping`, then `server/info` to learn whether the Private API helper is there),
starts a callback listener (P16-01 kit, port 8645, path `/bluebubbles-webhook`, body ≤1 MiB,
loopback by default) and registers that URL with the server (reusing an existing registration of
the same URL, so a crash leaves no duplicates; removed again on disconnect). The server cannot set
headers on its webhook calls, so the registered URL carries a secret in its query string; the
listener compares it in constant time before the body is parsed. New messages become events (chat
id = the chat GUID, user id = the sender's phone number or email); edits (`updated-message` with
`dateEdited`) become edited events; our own messages, tapbacks and delivery/read-status updates are
dropped; a replayed delivery (same message GUID, or the same edit) is answered 200 and dropped.
Replies go out over REST: each paragraph is its own bubble, a long paragraph is cut at 4,000 code
points (no `(i/n)` markers: bubbles flow), a chat GUID is passed through and a bare address is
resolved to its chat GUID (`chat/query`, exact `chatIdentifier` match, LRU cache of 500); an address
with no chat gets a new one through `chat/new` when the Private API is there. Typing, read
receipts, quoted replies and tapback reactions (processing state) need the Private API and are
skipped without it. Attachments are fetched lazily by the media intake (`register_fetchers`), so
nothing is downloaded for a message the gateway refuses.

Inputs → outputs:
- Credentials (secret store, then env; vendor names D-16.22): `BLUEBUBBLES_SERVER_URL` (or
  `platforms.bluebubbles.server_url`), `BLUEBUBBLES_PASSWORD`.
- `platforms.bluebubbles` keys: `webhook_host/port/path/public/public_url/max_body_bytes` (kit),
  `send_read_receipts` (true), `mention_patterns` (list or comma/newline string of regexes; default
  our wake words `indus agent`, `indus`), `split_paragraphs` (true), `require_mention` (read by the
  core's mention gate, P15-11).
- Webhook `POST <path>?token=<secret>` JSON `{type, data}` → `InboundEvent` (text, kind from the
  first attachment's MIME, `media` = `bluebubbles-attachment:<guid>` refs, `reply_to_id` =
  `threadOriginatorGuid`, metadata `mentioned` when a wake word matched, `edited` for edits).
- `send(chat, text)` → `SendResult` with one id per bubble (the message GUIDs).

Files: `src/indus/gateway/adapters/bluebubbles/__init__.py`, `.../bluebubbles/adapter.py`,
`tests/fakes/platform/bluebubbles_wire.py`, `tests/unit/gateway/adapters/bluebubbles/__init__.py`,
`tests/unit/gateway/adapters/bluebubbles/test_adapter.py`,
`tests/contract/gateway/bluebubbles_case.py`; shared: the contract `conftest.py` import line,
`platforms.bluebubbles` schema row + its test line.

Edge cases / failure modes:
- missing URL or password → fatal config error at connect; a URL without a scheme gets `http://`;
  a URL other than http(s) refused.
- callback without the token, or a wrong one → 401 before any parsing (compare spied in a test);
  a token of another length never raises; body over 1 MiB → 413 (kit).
- events other than messages, `isFromMe`, tapbacks (`associatedMessageType` set), status-only
  `updated-message` → 200, no event.
- replay of a `new-message` with a seen GUID, or of an edit with a seen `dateEdited` → 200
  `{"status":"duplicate"}`, no event (ids kept 24 h, at most 5,000).
- payload without a chat or a sender → 400.
- the password must never appear in a URL httpx logs, in `response.request`, in an error text or in
  a repr: it is added inside the transport, and the webhook carries a derived token, not the
  password.
- a send that fails part way returns the bubbles that landed and the remainder (R06.4); a read
  timeout is "maybe delivered", never retried.
- GUID cache full → least recently used address evicted.
- no Private API → typing, read receipts, quoted replies, reactions and `chat/new` skipped (send to
  an unknown address = "chat not found").

Our design differences / decisions:
- Webhook secret: a token derived from the password (`HMAC-SHA256(password, "indus bluebubbles
  webhook v1")`, hex) instead of the password itself, so a logged or leaked callback URL never
  reveals the server password; same value every start, so re-registration still finds an old row.
- Password added to REST URLs inside our transport (`_PasswordQuery`), never visible to httpx's log
  line or error texts (same idea as Signal's account, D-16.394).
- Group mention gating follows the core (P15-11 `require_mention`, default true); the adapter only
  says whether a wake word matched (`mentioned`) and strips a leading wake word. The reference
  answers every group message by default.
- Attachments fetched lazily by the intake (only for admitted messages), capped at 100 MiB, no
  redirects; the reference downloads every attachment before dispatch.
- Webhook bodies are JSON only (the server sends JSON); the reference also parses form bodies.
- No text batching, no `/p/<profile>` shared listener (multiplexing is P24).

Tests we will write:
- contract suite through the wire fake (lacks `thread`, `bot`).
- webhook: token check (constant-time compare spied), replay ignored, own/tapback/status updates
  dropped, edits, bad payload 400, password never in callback URL or logs.
- GUID resolution: pass-through, exact match only, cache hit, LRU eviction at 500, `chat/new`.
- send: paragraph bubbles, no markers, partial failure remainder, private-API reply fields.
- connect: missing credentials fatal, webhook registered once and reused, removed at close,
  send-only registers nothing, password masked in httpx's log line and errors.
- media fetcher: download with cap, wrong ref refused.

## Decisions for the lead
- Webhook auth uses the derived token in `?token=` (not `?password=`); to accept the reference's
  form, compare `password` too in `_callback` (one line).
- Registry row (P15 core, not edited here): name `bluebubbles`, label `BlueBubbles (iMessage)`, probe
  `httpx` (no extra), `required_env` `BLUEBUBBLES_SERVER_URL` + `BLUEBUBBLES_PASSWORD`, max_len
  4,000, install hint "pip install indus-agent (BlueBubbles needs no extra); run BlueBubbles Server
  on a Mac"; `BUILTIN_CREDENTIALS["bluebubbles"] = ("BLUEBUBBLES_PASSWORD",)` (URL left out like the
  other hosts).
- Tapback for processing state: started → `emphasize`, finished → remove it and add `like` (ok) or
  `dislike` (failed). Change in `PROCESSING_REACTIONS`.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] ping + server info at connect; Private API and helper flags read (test:
  `test_adapter.py::test_replies_quote_only_with_the_private_api`)
- [x] webhook registered for `new-message` and `updated-message`, an existing row reused, every
  row for our URL removed at close (test: `::test_the_webhook_is_registered_once_and_removed_at_close`)
- [x] callback auth before parsing — the reference compares with `!=`; ours in constant time,
  compare spied (test: `::test_callback_without_the_right_token_is_refused_before_parsing`)
- [x] own messages and tapbacks dropped (numeric and named `associatedMessageType`); v1.9 nested
  chat shape (test: `::test_own_messages_tapbacks_and_other_events_are_dropped`)
- [x] GUID resolution by exact `chatIdentifier` only, LRU 500 (tests:
  `::test_addresses_resolve_by_exact_identifier_and_are_cached`,
  `::test_guid_cache_evicts_the_least_recently_used_address`)
- [x] paragraph bubbles, no `(i/n)` markers; `chat/new` for an unknown address only with the Private
  API (tests: `::test_paragraphs_become_bubbles_without_markers`,
  `::test_an_unknown_address_needs_the_private_api_for_a_new_chat`)
- [x] quoted reply via `selectedMessageGuid` only with the Private API and helper
- [x] attachments by multipart, voice as an audio message, caption as a following text (test:
  `::test_files_go_as_attachments_with_the_caption_after`)
- [x] typing and read receipts only with the Private API (tests:
  `::test_replies_quote_only_with_the_private_api`, `::test_read_receipts_follow_the_setting`)
- [x] wake words: a leading one stripped only (test: `::test_group_wake_words_set_mentioned_and_are_stripped`)
- [x] gap found: the reference treats every `updated-message` as a new message (a delivery or read
  status update would be answered again) — ours answers only edits (`dateEdited`), once per edit
  (test: `::test_edits_arrive_once_each_and_status_updates_are_dropped`)
- [x] Verify column: contract suite; GUID cache eviction; webhook replay ignored
  (`::test_a_replayed_webhook_is_ignored`)
- Deliberate differences: derived webhook token instead of the password in the callback URL; the
  password added inside the transport (test: `::test_the_password_never_reaches_a_url_log_or_error`);
  group mention gating by the core's `require_mention` (default on) with our wake words; lazy
  attachment fetch (≤100 MiB, no redirects); JSON webhook bodies only; tapbacks for processing state.
- Not taken: the `/p/<profile>` shared listener (multiplexing, P24), form-encoded webhook bodies,
  token in `x-password`/`guid` headers or query, chat participants in `chat_info`, the setup wizard
  (P16-31), the cron standalone sender (`indus send` uses the send-only open).

## Status

Done (2026-10-20). Files: the adapter package, the wire fake, its unit tests and contract case.
Shared: `tests/contract/gateway/conftest.py` (one import line), `platforms.bluebubbles` schema row
and its `test_schema.py` line, ACC-K12-1 in `docs/internals/acceptance-map.md`. Not done here (P15
core, for the lead): the registry row and `BUILTIN_CREDENTIALS` entry above, so `indus send
bluebubbles:…` and the daemon can build it.
