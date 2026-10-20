# Task: LINE adapter (P16-23) Phase: 16 Owner: wave-24 builder E5

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/line/adapter.py`
(whole file: webhook, signature, reply/push, slow-reply button, media serving, chunking),
`plugins/platforms/line/plugin.yaml` (env names), `tests/gateway/test_line_plugin.py` (test names
only, for edge cases). Evidence: none.
Interop (vendor facts, LINE Messaging API v2 as documented by LINE): `POST /v2/bot/message/reply`
(`replyToken`, `messages` ≤5), `POST /v2/bot/message/push` (`to`, `messages` ≤5), both answering
`{"sentMessages": [{"id", "quoteToken"}]}`; `GET /v2/bot/info` (`userId`, `displayName`);
`POST /v2/bot/chat/loading/start` (`chatId`, `loadingSeconds` 5–60 in steps of 5, user chats only);
`GET https://api-data.line.me/v2/bot/message/{messageId}/content`; webhook body
`{destination, events[]}` signed by `X-Line-Signature` = base64(HMAC-SHA256(channel secret, raw
body)); event fields `type`, `mode` (`active`/`standby`), `timestamp`, `source` (`user`/`group`/
`room` with `userId`/`groupId`/`roomId`), `webhookEventId`, `deliveryContext.isRedelivery`,
`replyToken`, `message` (`id`, `type` text/image/video/audio/file/location/sticker, `text`,
`mention.mentionees[]` with `index`, `length`, `isSelf`, `quoteToken`, `quotedMessageId`,
`fileName`, `duration`), `postback.data`; message objects `text` (≤5,000 characters,
`quoteToken`), `image` (`originalContentUrl`, `previewImageUrl`, HTTPS, JPEG/PNG ≤10 MB), `audio`
(`originalContentUrl`, `duration` ms), `video` (`originalContentUrl`, `previewImageUrl`), `template`
buttons (`altText` ≤400, `text` ≤160, ≤4 `postback` actions with `label` ≤20 and `data` ≤300).
No SDK: our own client over httpx (a base dependency); API version: Messaging API v2.

What it does (own words): LINE pushes every chat event to an HTTPS webhook we expose through the
P16-01 listener kit (port 8646, `/line/webhook`, body ≤1 MiB). The signature is checked on the raw
body in constant time before anything is parsed; a bad one is 401. Each event becomes an
`InboundEvent` (user chats are DMs, groups and rooms are groups; a mention of the bot sets
`mentioned` and is cut out of the text; media become `line:<message id>` attachments that the
media intake downloads through our fetcher). Replies prefer the free, single-use reply token of
the chat's latest event while it is younger than 50 s; otherwise (or when LINE rejects the token)
they go out as metered pushes. LINE takes at most five message objects per call and 5,000
characters per text bubble, so a reply is cut into bubbles of at most 4,500 units and sent five per
call: 12 bubbles take three calls (5, 5, 2), only the first using the reply token. A call is all or
nothing, so a failure after earlier calls reports those bubbles as delivered and the rest as the
remainder that may be sent again. Approval and clarify prompts use template buttons whose
postback data is our callback string; a press comes back as a postback event. When a turn is
still running 45 s after the user's message (configurable, 0 = off), the still-valid reply token
is spent on a "still working" bubble with a postback button; the answer is then held until the
user taps it (a fresh, free reply token) or, if nobody taps within 10 minutes, pushed. Media we
send are served to LINE from the same listener through unguessable short-lived URLs
(`/line/media?t=<token>`, token TTL 1,800 s) under the configured public HTTPS base URL.

Inputs → outputs:
- `create_adapter(AdapterConfig)`: `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET` (settings
  credentials, else secret store, else env); `platforms.line` keys: the kit's `webhook_*` keys
  (port 8646, path `/line/webhook`, cap 1 MiB), `webhook_public_url` (the HTTPS URL LINE calls;
  its origin is also the media base), `slow_reply_seconds` (45), `slow_reply_hold_seconds` (600),
  `api_base_url` / `data_base_url` (defaults `https://api.line.me`, `https://api-data.line.me`).
- Webhook → 200 at once (events handled in the background), 401 bad signature, 400 bad JSON.
- `send` → reply/push calls, `SendResult` with one id per bubble; `send_image/voice/video` →
  media message + caption bubble; `send_file/document` refused (LINE bots cannot send files);
  `typing_start` → loading animation (user chats only); `register_fetchers` → `line:` downloads.

Files: `src/indus/gateway/adapters/line/__init__.py`, `line/adapter.py`, `line/api.py` (our client),
`tests/fakes/platform/line_wire.py`, `tests/unit/gateway/adapters/line/test_adapter.py`,
`tests/contract/gateway/line_case.py`, `tests/fixtures/gateway/crypto_vectors/line_signature.json`.
Shared: `tests/contract/gateway/conftest.py` (`AdapterCase.lacks`, case import),
`tests/contract/gateway/test_adapter_contract.py` (inbound check honours `lacks`).

Edge cases / failure modes:
- signature header missing, wrong, re-encoded JSON (signature is over the raw bytes), non-ASCII
  body → 401 / verified on bytes; body over 1 MiB → 413 before reading (kit).
- the console's "Verify" call (empty `events`) → 200; `mode: standby` events and events from our
  own bot id ignored; redelivered events (same `webhookEventId`) ignored (dedupe 5,000 ids, 24 h).
- reply token older than 50 s, already used, or refused by LINE (400 "Invalid reply token") →
  push; a reply that timed out on read is never followed by a push (it may have landed, R06.4).
- 429 with a long `Retry-After` is handed back (inline wait cap 5 s, R06.3); a 429 for the monthly
  message quota is not retried.
- unsupported media type or size, no public HTTPS base → clear failure, nothing sent; a media URL
  after its TTL → 410; unknown token → 404.
- unknown chat id shapes on `send` (not `U`/`C`/`R` + 32 hex) are still sent (LINE decides).
- `send_only` (`indus send`): token check only, no listener.

Our design differences / decisions:
- Our names (`LineAdapter`, `LineApi`, `LineError`); our own client over httpx (no aiohttp).
- Every bubble is delivered (12 bubbles → three calls), where the reference stops at five bubbles
  and ellipsises the rest. A send carries up to 20,000 UTF-16 units (`Capabilities.max_len`), so a
  normal reply fits one call and uses the free reply token for all of it.
- Length is counted in UTF-16 units (conservative for emoji).
- Access lists come from the gateway's authorization chain (`INDUS_LINE_ALLOWED_USERS`, group chat
  lists); the reference's three LINE-only lists are not repeated in the adapter.
- The held answer is pushed after `slow_reply_hold_seconds` if the user never taps (the reference
  holds it forever). Held answers only take sends marked `final`; interim sends pass through.
- The still-working timer starts when the runner shows typing (a turn is running), not from a
  typing heartbeat wrapper.
- Media URLs use a query token on one fixed route; served files are read whole (the kit's replies
  are buffered), so audio and video are capped at 50 MiB here (LINE allows 200 MB).
- Listener binds 127.0.0.1 unless `webhook_host` + `webhook_public` (kit rule); the reference binds
  all interfaces by default.

Tests we will write:
- signature known-answer vectors (fixture, derived with `openssl dgst`), constant-time compare spied.
- reply token at 49 s used, at 51 s falls back to push; refused token → push; read timeout → no push.
- 12 bubbles → three calls (5, 5, 2), only the first with the reply token; failure in the second
  call → partial with the first five ids and the right remainder.
- media URL served, then 410 after 1,800 s; wrong type/size refused; no public URL refused.
- slow-reply button fires at 45 s, holds the final answer, a tap delivers it with the new token;
  0 disables; no tap → pushed after the hold.
- inbound: mention cut and `mentioned`, standby ignored, duplicate event id ignored, postback →
  button metadata, own bot ignored, quoted message → `reply_to_is_own`.
- `indus send` (send_only) starts no listener; credentials never in errors or logs.
- contract suite through `line_case.py`.

## Decisions for the lead
- Registry row (P15 core, not edited): `_chat_entry("line", "LINE", sdk=("httpx",),
  extra_name="line", required_env=("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET"),
  max_len=20000, hint=...)` with the install hint "no extra"; `BUILTIN_CREDENTIALS["line"] =
  ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET")` (token lock = both).
- Port 8646 shares the kit; `indus doctor` (P16-36) should list it.
- The 50 MiB serve cap goes away when the kit can stream a reply body.
- `X-Line-Retry-Key` on pushes would make a read-timed-out push safely retryable; not used yet.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] signature over the raw body, constant time, 401 before parsing (test:
  `test_adapter.py::test_signature_vectors_from_the_spec`,
  `::test_a_bad_or_missing_signature_is_refused_before_parsing`)
- [x] reply token used while fresh, 51 s → push (Verify column; test:
  `::test_reply_token_at_49_seconds_is_used`, `::test_reply_token_at_51_seconds_falls_back_to_push`)
- [x] a refused token falls back to push; a token is single use (test:
  `::test_a_token_is_single_use_and_a_refused_token_falls_back_to_push`)
- [x] ≤5 message objects per call, 12 bubbles in 3 calls (Verify column; test:
  `::test_twelve_bubbles_go_out_in_three_calls`)
- [x] media URL served, 410 after the TTL, 404 unknown (Verify column; test:
  `::test_media_url_expires_after_its_ttl`)
- [x] video preview generated, unsupported types/sizes refused, files refused, public HTTPS URL
  required (test: `::test_video_gets_a_generated_preview_and_files_are_refused`,
  `::test_media_needs_a_public_https_url`)
- [x] slow-turn postback card at 45 s, held answer delivered on the tap, interim sends bypass it,
  0 disables, stale tap answered (test: `::test_a_slow_turn_offers_the_still_working_button`,
  `::test_a_held_answer_is_pushed_when_nobody_taps_and_a_fast_turn_gets_no_card`,
  `::test_zero_turns_the_card_off_and_a_stale_tap_is_answered`)
- [x] self-message filter, redelivery dedupe, sticker keywords, location text, rooms as groups,
  lifecycle events ignored (test: `::test_inbound_normalisation_and_drops`)
- [x] inbound media downloaded through the content endpoint (test:
  `::test_the_media_fetcher_feeds_the_intake`)
- [x] loading animation for user chats only (contract `typing-refresh` on a group: none sent)
- [x] gap found at compare: the reference's standalone sender cannot deliver media; our send-only
  adapter would have handed LINE a URL nobody serves — fixed by refusing media when
  `send_only`, pinned by `::test_indus_send_opens_line_send_only`
- Deliberate differences: the reference stops at five bubbles and ellipsises the rest; we deliver
  all of them in batches of five. It falls back to push after *any* reply failure; we push only
  when the token is refused or the reply never reached LINE (a timed-out reply may have landed,
  R06.4). It keeps its own three LINE allowlists; we use the gateway's authorization chain. It
  emits "[unsupported message type]" text for unknown message types; we drop them. It holds a slow
  answer until a tap forever; we push it after `slow_reply_hold_seconds`. It binds every interface
  by default; we bind loopback (kit rule). It serves files with an allowed-roots check at serve
  time; ours serves only paths the sender's media policy already validated (R06.22), by token.
  Unknown audio duration: 60 s (the reference says 1 s).
- Not taken: the reference's `/line/webhook/health` path (the kit's `/health` covers it), the
  interrupt hook that marks a pending answer as failed (no such base seam here), the shared
  multi-profile ingress prefix, the interactive setup wizard (P16-31).

## Status

Done (2026-10-20). New files only, plus the shared contract changes (`AdapterCase.lacks` and the
inbound check honouring it, linear `_long_text`, one case import line) and one R06.3 trap row.
No new dependency, no config schema rows, no pyproject extra (nothing to install: httpx is a base
dependency). Registry and credential rows for the lead are under "Decisions for the lead".
