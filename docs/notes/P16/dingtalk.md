# Task: DingTalk adapter (P16-16) Phase: 16 Owner: wave-25 builder E8

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/dingtalk/adapter.py`
(all: credentials, Stream Mode start loop with backoff and circuit breaker, SDK log guard, mention and
allowlist gating, session-webhook cache and expiry margin, markdown send and renderer quirks, AI
cards, emotion reactions, media download codes, standalone static-webhook send),
`dingtalk/inbound.py` (text, rich text, card, audio, file and picture shapes), `dingtalk/gateway_env.py`,
`dingtalk/plugin.yaml`; tests `tests/gateway/test_dingtalk_reconnect_circuit_breaker.py` (both cases)
and the test names in `tests/gateway/test_dingtalk.py`. Research 06 §3.3 (row DingTalk). Evidence: none.
Interop (vendor, not the reference): the `dingtalk-stream` SDK 0.24.3 sources on GitHub
(`frames.py`, `stream.py`, `handlers.py`, `chatbot.py`: frame and ack field names, the
`/v1.0/gateway/connections/open` body, the `/v1.0/oauth2/accessToken` body, the chatbot message keys
and topic `/v1.0/im/bot/messages/get`), DingTalk open-platform pages for
`/v1.0/robot/oToMessages/batchSend` and `/v1.0/robot/groupMessages/send` (`robotCode`, `userIds`,
`openConversationId`, `msgKey` `sampleMarkdown`, `msgParam` as a JSON string, header
`x-acs-dingtalk-access-token`, answer `processQueryKey`) and `/v1.0/robot/messageFiles/download`
(`downloadCode`, `robotCode` → `downloadUrl`). PyPI metadata of `dingtalk-stream` (MIT; needs
`websockets`, `requests`, `aiohttp`).

What it does (own words): DingTalk bots run in "Stream Mode": the app opens an outbound WebSocket to
DingTalk and receives every chat message the bot sees as a `CALLBACK` frame, which it must
acknowledge so DingTalk does not push it again. Each inbound message carries a short-lived
"session webhook" URL that posts a reply into that conversation. The adapter keeps the newest
webhook per conversation (at most 500, expiry honoured with a safety margin) and replies through
it; when there is none (cron, `indus send`, an expired one, or a webhook that refuses) it falls back
to the robot REST API with an app access token (group: `groupMessages/send`; one-to-one:
`oToMessages/batchSend`). Replies are markdown, cut at 20,000 characters. A stream that keeps
failing the same way backs off 2, 5, 10, 30, 60 s; the fifth identical failure in a row trips a
breaker that waits 300 s between attempts (and stops logging each one) until the error changes;
repeated identical log lines are collapsed within 300 s.

Library decision: `dingtalk-stream` is MIT (allowed) but would add `websockets`, `requests` and
`aiohttp` (and requests' own dependencies) to an extra, and its retry loop swallows errors (the
reference needs a log filter and an incompatibility probe to see them). The protocol is small, so we
write our own minimal Stream Mode client over httpx (REST, a base dependency) and httpx-ws (the
WebSocket, already locked for Mattermost): no new package. Extra `dingtalk = ["httpx-ws==0.9.0"]`,
imported lazily when the stream opens.

Inputs → outputs: `AdapterConfig` (`DINGTALK_CLIENT_ID`, `DINGTALK_CLIENT_SECRET` from the secret
store or env; `platforms.dingtalk.robot_code`, `require_mention`, `allowed_chats`,
`free_response_chats`) → a connected adapter; `CALLBACK` frames → acks + `InboundEvent`s; `SYSTEM`
`ping` → an ack echoing its data; `SYSTEM` `disconnect` → ack, close, reopen; sends → webhook or REST
→ `SendResult`; attachments (`dingtalk://download/<code>`) → bytes through `messageFiles/download`.

Files: `src/indus/gateway/adapters/dingtalk/__init__.py`, `.../dingtalk/adapter.py`,
`.../dingtalk/stream.py` (our Stream Mode client pieces: connection open, frames, acks, the opener),
`tests/fakes/platform/dingtalk_wire.py`, `tests/contract/gateway/dingtalk_case.py`,
`tests/unit/gateway/adapters/dingtalk/test_adapter.py`; shared: `pyproject.toml` (extra),
`THIRD_PARTY_NOTICES.md` (httpx-ws / wsproto rows name `dingtalk`), config schema row
`platforms.dingtalk` + its test, contract `conftest.py` (one import line).

Edge cases / failure modes:
- missing client id or secret → fatal connect error naming the variable;
- the token request refused (400/401/403) → fatal (`auth_failed`); a connect failure → retryable;
- `connections/open` refused later (credentials revoked) → `report_fatal("dingtalk_auth")`;
  httpx-ws missing → `report_fatal("missing_dependency")` with the install hint;
- an endpoint outside `*.dingtalk.com` or not `wss://` → refused (the ticket never leaves);
- a session webhook not on `https://oapi.dingtalk.com/` or `https://api.dingtalk.com/` → ignored;
- duplicate message ids (DingTalk redelivers an un-acked frame) → dropped (1,000 recent ids);
- group messages without a mention when `require_mention` is on, or outside `allowed_chats` → dropped
  (DMs always pass); empty messages → dropped;
- an unknown frame type or topic → acked "not implemented" (404) so DingTalk does not retry it forever;
- a reply over 20,000 characters → split into several messages, ids for each;
- REST errors classified: 429 / throttling codes → rate limited with `Retry-After`, 403 → forbidden,
  404 → chat not found (message not found when the code names a message), 400 parameter codes →
  bad format, length codes → too long; transport timeouts through the P16-01 kit (R06.4).

Our design differences / decisions:
- our names (`DingTalkAdapter`, `StreamFrame`, `Breaker`, `LogCollapse`, `SessionWebhooks`);
- own Stream Mode client (above); the app secret travels only in request bodies, the access token only
  in the `x-acs-dingtalk-access-token` header, and the connection ticket and session-webhook query are
  added inside the transport (the Signal pattern, D-16.394), so httpx's request log, `response.request`
  and error texts never carry them;
- REST fallback when no live session webhook exists (the reference fails such sends);
- user id = `senderStaffId` (what `oToMessages` and allowlists use), `user_id_alt` = `senderId`;
  the DM chat id is the conversation id; a chat id `user:<staffId>` addresses a person directly;
- AI cards, emotion reactions and the static-webhook standalone sender are not built (no editing:
  `supports_edit=false`; streaming falls back to segments);
- no typing indicator (DingTalk has none);
- `report_fatal` instead of a private fatal flag; the breaker and log collapse are ours (no SDK filter).

Tests we will write: the shared contract over the wire fake; expired session webhook falls back to
REST; a refusing webhook falls back and is evicted; the breaker trips on the 5th identical failure and
waits 300 s (fake clock), resets when the error changes; log lines collapsed within 300 s; ping acked
with its data; disconnect frame reopens; duplicate message ignored; mention and chat gating; ticket,
secret, token and webhook query never in logs (httpx log on); media fetch through the download API;
`indus send` does no inbound work; non-dingtalk endpoint and webhook hosts refused.

## Decisions for the lead
- Own Stream Mode client instead of `dingtalk-stream` (licence fine, but four new packages and a
  swallowing retry loop); swap only by replacing `stream.py`.
- REST fallback uses `sampleMarkdown`; a DM fallback needs the person's staff id: a chat seen before is
  remembered (bounded), a cold DM can be addressed as `user:<staffId>`; any other cold chat id is sent
  as a group `openConversationId`.
- Session webhook safety margin 300 s before its stated expiry (the reference's value).
- Registry row (P15 core, for the lead): `_chat_entry("dingtalk", "DingTalk", sdk=("httpx_ws",),
  extra_name="dingtalk", required_env=("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"), max_len=20000,
  hint=...)`; `BUILTIN_CREDENTIALS["dingtalk"] = ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET")`.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] backoff 2/5/10/30/60 and the breaker after 5 identical failures, 300 s while tripped (tests:
  `dingtalk/test_adapter.py::test_the_breaker_trips_on_the_fifth_identical_failure`,
  `::test_breaker_counts_only_identical_failures_in_a_row`)
- [x] repeated log lines collapsed within 300 s; the tripped breaker logs once, then stays quiet
  until the failure changes (`::test_repeated_log_lines_are_collapsed_within_300_seconds`,
  `::test_log_collapse_counts_what_it_held_back`)
- [x] session webhook cache ≤500, oldest evicted, host allow-list, 300 s expiry margin; expired →
  REST (`::test_an_expired_session_webhook_falls_back_to_the_robot_api`,
  `::test_a_refusing_webhook_is_dropped_and_the_api_takes_over`,
  `::test_webhooks_on_other_hosts_are_ignored`)
- [x] mention gating, allowed chats, free-response chats; DMs never gated
  (`::test_group_gating_by_mention_and_chat`)
- [x] text, rich text, picture, audio (recognition text + the voice file), file, video
  (`::test_message_shapes_become_events_with_attachments`)
- [x] dedupe of redelivered message ids; every frame acked at once, work in the background
  (`::test_redelivered_messages_and_empty_ones_are_dropped`,
  `::test_pings_are_acked_with_their_data_and_callbacks_at_once`)
- [x] markdown renderer quirks: blank line before a numbered list, dedented fences
  (`::test_long_replies_are_split_and_markdown_is_fixed_for_the_renderer`)
- [x] shared contract over the wire fake (`tests/contract/gateway/dingtalk_case.py`)
- Deliberate differences: our own Stream Mode client instead of `dingtalk-stream` (no SDK incompat
  probe or SDK log filter needed: our loop sees every failure); replies split instead of cut at
  20,000; the REST fallback replaces the reference's static robot-webhook standalone sender;
  attachments resolved lazily by the intake instead of eagerly for every message; the user id is the
  staff id (sender id as the alternate); allowlists are the core's (`INDUS_DINGTALK_ALLOWED_USERS`),
  not an adapter list; a `disconnect` frame reopens at once; a refused stream is fatal.
- Not taken: AI cards (and so editing / streaming), the Thinking/Done emotion reactions, wake-word
  `mention_patterns`, the card / interactive-card / file-name text fallbacks (such messages carry no
  text and are dropped unless they have an attachment), remote image URLs as markdown images
  (`send_image` takes local files only and DingTalk webhooks cannot upload them: refused), QR setup
  (P16-31).

## Status

Done (2026-10-20). New files: adapter package (`__init__.py`, `adapter.py`, `stream.py`), wire fake,
contract case, unit tests. Shared: `pyproject.toml` (extra `dingtalk`), `THIRD_PARTY_NOTICES.md`,
00-layout §4 pin row, config schema row `platforms.dingtalk` + its test, contract `conftest.py`
(import line). Registry row and `BUILTIN_CREDENTIALS` row are for the lead (above).
