# Task: Feishu / Lark adapter (P16-14) Phase: 16 Owner: wave-25 builder E9

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/feishu/adapter.py`
(module header and constants; settings loading; connect / disconnect; send, edit, delete, approval
card, media sends; inbound message handler, reaction and card-action routing; inbound processing,
text and media batching; webhook request handler and signature check; admission, mention and
group-policy gates; bot identity hydration; dedupe; outbound payload choice; connect and send
retry loops; request builders; `register`), `feishu/__init__.py`, `feishu/plugin.yaml`,
`feishu/gateway_env.py`, `feishu/send_targets.py`, `feishu/adapter_hint.py`. Research 06 §3.3 (row
Feishu/Lark), §3.5. Evidence: none.
Interop (vendor facts, checked against the vendor's own documentation and an independent tool, not
the reference): Feishu Open Platform server API (`/open-apis/auth/v3/tenant_access_token/internal`,
`/open-apis/bot/v3/info`, `/open-apis/im/v1/messages` create / reply / update / patch / delete /
get, `/im/v1/chats/{id}`, `/im/v1/images`, `/im/v1/files`, `/im/v1/messages/{id}/resources/{key}`,
`/im/v1/messages/{id}/reactions`, `/contact/v3/users/{id}`), event schema 2.0 (`header.event_type`,
`header.token`, `header.event_id`), the long-connection endpoint `POST /callback/ws/endpoint`
(`AppID`/`AppSecret` → `URL` + `ClientConfig`) and its protobuf `Frame` / `Header` messages (fields
1–9, method 0 control / 1 data, header keys `type`, `message_id`, `sum`, `seq`, `trace_id`,
`biz_rt`), webhook encryption (AES-256-CBC, key = SHA-256 of the encrypt key, IV = the first 16
bytes of the base64 cipher text, PKCS#7) and the request signature (SHA-256 hex of timestamp +
nonce + encrypt key + body, headers `X-Lark-Request-Timestamp`, `X-Lark-Request-Nonce`,
`X-Lark-Signature`). The vendor's decryption example (`"test key"`,
`P37w+VZImNgPEO1RBhJ6RtKl7n6zymIbEG1pReEzghk=` → `hello world`) was checked with `openssl enc`.

What it does (own words): connects Indus Agent to a Feishu (China, `open.feishu.cn`) or Lark
(international, `open.larksuite.com`) workspace as a custom app bot, chosen by
`platforms.feishu.extra.domain` / `FEISHU_DOMAIN`. Connecting proves the app id and secret by
fetching a tenant access token (cached, refreshed five minutes before it expires, and once more
when the API says it is invalid), then reads the bot's own open id (`bot/v3/info`) for mention
gating. Inbound events come over one of two transports:

- **long connection** (default): the adapter asks the endpoint for a WebSocket URL, opens it with
  httpx-ws (the `feishu` extra) and reads binary protobuf frames: data frames carry event JSON
  (possibly split in parts, reassembled by message id), control frames are pings and pongs. Every
  data frame is answered with the same frame and a `{"code": 200}` payload; a ping goes out at the
  server's `PingInterval` (120 s). The first link gets three attempts inside connect (1 s, 2 s
  apart); a dropped link reopens after 2 s doubling to 60 s.
- **webhook**: a callback listener from the P16-01 kit (loopback, port 8765, `/feishu/webhook`,
  1 MiB). The signature is checked on the raw body in constant time before anything is parsed, an
  encrypted body is decrypted with the encrypt key (`cryptography`, no hand-written AES), the
  verification token is compared in constant time, the URL-verification challenge is echoed only
  after that, and events are deduplicated by event id.

`im.message.receive_v1` messages become `InboundEvent`s: `text` (mention placeholders resolved,
the bot's own mention removed and recorded as `mentioned`), `post` (rich text flattened to
Markdown-ish text; its images and files become attachments), `image`, `file`, `audio`, `media`
(video), `sticker`, and readable placeholders for cards, forwarded bundles and shared chats.
`chat_type` p2p → dm, group → group, a message with a `thread_id` → thread; the sender's open id
is `user_id` and the union id `user_id_alt`; `sender_type` app/bot marks `is_bot`. Our own
messages and repeats (by message id) are dropped. Rapid plain-text messages from one sender in one
chat are merged (0.6 s quiet window, at most 8 messages and 4,000 characters per batch); images,
files and videos are merged per kind (0.8 s). Commands, voice notes and button presses never wait.
`card.action.trigger` (a button on one of our cards) becomes an event whose metadata carries the
button's callback data, as the shared approval flow expects.

Outbound: text goes as `text`, or as a `post` with `md` rows when it carries Markdown (one choice
for all pieces of a split message); pieces of at most 8,000 characters; a reply uses the reply API
(in the thread when there is one) and falls back to a new message when the replied-to message was
withdrawn; `meta["plain"]` sends plain text. Edits update text/post messages and patch cards
(approval cards lose their buttons that way). Approvals and clarify questions are interactive
cards with buttons. Images and files are uploaded and sent by key (a caption makes a post).
Processing reactions: "Typing" while a turn runs, removed at the end, "CrossMark" on failure.

Inputs → outputs: `AdapterConfig` (`FEISHU_APP_ID`, `FEISHU_APP_SECRET`, optional
`FEISHU_ENCRYPT_KEY`, `FEISHU_VERIFICATION_TOKEN` from the secret store or environment;
`platforms.feishu.extra`: `domain`, `connection_mode`, `webhook_*`, `reactions`, `user_names`,
`max_media_bytes`, batching settings) → a connected adapter; frames / callbacks → `InboundEvent`s;
sends, edits, deletes, media, cards → REST calls → `SendResult`s.

Files: `src/indus/gateway/adapters/feishu/__init__.py`, `.../feishu/adapter.py`, new
`.../feishu/client.py` (REST client over httpx, token cache, error readings, route table, socket
opener), `.../feishu/frames.py` (the long connection's frame codec and part assembly),
`.../feishu/crypto.py` (webhook decryption and signature), `.../feishu/content.py` (message content
in and out, cards), `.../feishu/batching.py` (text and media batches),
`tests/fakes/platform/feishu_wire.py`, `tests/contract/gateway/feishu_case.py`,
`tests/unit/gateway/adapters/feishu/__init__.py`, `.../feishu/test_adapter.py`,
`.../feishu/test_frames.py`, `tests/fixtures/gateway/crypto_vectors/feishu.json`. Shared:
`pyproject.toml` (extra `feishu = ["httpx-ws==0.9.0"]`), `THIRD_PARTY_NOTICES.md` (httpx-ws and
wsproto rows name `feishu`), `tests/contract/gateway/conftest.py` (one import line),
`foundation/config/schema.py` + `tests/unit/foundation/config/test_schema.py` (row
`platforms.feishu`).

Library decision: the plan names `lark-oapi` (MIT; 1.7.5 is current). It is **not used**: it pulls
`requests`, `requests-toolbelt`, `pycryptodome`, `websockets` and (for the long connection)
`protobuf` into the gateway, runs its WebSocket client on its own thread and event loop with
module-level globals, and would be a second HTTP stack beside httpx. What we need is a dozen REST
routes, one WebSocket and a nine-field protobuf message, so the adapter has its own client over
httpx (base dependency), the long connection over httpx-ws 0.9.0 (MIT; with wsproto 1.3.2, MIT;
both already locked through the `mattermost` extra, so the new extra adds no package), its own
protobuf wire codec for the one message (varints and length-delimited fields only; this is
serialisation, not cryptography) and `cryptography` (base dependency) for AES-CBC.

Vendor API version: Feishu / Lark Open Platform server API v1/v3 as listed above, event schema 2.0,
long-connection protocol `pbbp2` (2026). No SDK pin.

Capability flags (research 06 Q4): `dm_is_private` true (a p2p chat has two members),
`owns_access_policy` false (the core chain decides allowlists, pairing and mention gating; the
adapter only drops its own echoes), `auth_upstream` false, `supports_edit` true,
`supports_delete` true, `supports_buttons` true (interactive cards), `push_capable` true,
`splits_natively` true, cap 8,000 code points, markup `markdown`, `reply_opens_thread` false.

Edge cases / failure modes:
- Token: refused app id/secret (codes 10003, 10014, or HTTP 400/401 on the token route) → connect
  fails as fatal (`auth_failed`); later in the socket loop → `report_fatal("feishu_auth")`.
  Token invalid on a call (99991661, 99991663) → one refresh and one retry of that call (the call
  never reached the API handler).
- Send errors read from the API's `code`: 99991400 / 230020 / HTTP 429 → rate limited with
  `x-ogw-ratelimit-reset` or `Retry-After`; 230002, 230006, 230013, 230027, 99991672 / HTTP 403 →
  forbidden; 230001 naming the receive id → chat not found, otherwise bad format; 230011, 231003
  → message not found (a reply falls back to a new message); 230025 → too long; a rejected post
  ("content format of the post type is incorrect") → bad format, so the sender retries as plain
  text; connect errors retry, read timeouts never (R06.4).
- Long connection: an endpoint error or a closed socket reopens with backoff; frames that are not
  valid protobuf, unknown types and over-size or stale part sets are dropped; parts are capped (64
  parts, 4 MiB, 30 s).
- Webhook: missing or bad signature when an encrypt key is set → 401 (only the challenge may come
  unsigned, and it is answered only after the token check); a plaintext body when an encrypt key
  is set → 400; bad base64 / padding / JSON → 400; wrong token → 401; repeated event id → 200 and
  dropped; stale timestamp (±300 s) → 401.
- Inbound: empty text after removing the bot mention → dropped; unknown message types → a short
  placeholder; batches never mix threads, replies, senders or mention state; a batch over 8
  messages or 4,000 characters starts a new batch; disconnect emits what is pending.
- Media: attachments fetched on demand by the intake from the resources API (cap 100 MiB, type
  `image` or `file`; audio and video retried as `file`).
- Send-only open (`indus send`): token only; no socket, no listener.

Our design differences / decisions:
- Own client instead of lark-oapi (above). The WebSocket URL (it carries a connection ticket) is
  added inside the transport, so httpx's request log line never shows it, and it is masked like a
  credential.
- `user_id` is the open id (always present, what event payloads and allowlists use); the reference
  prefers the tenant `user_id` when the tenant exposes it. `union_id` is `user_id_alt` in both.
- Access policy, allowlists, pairing, bot admission and mention gating are the core's (P15-08,
  `platforms.feishu.extra.require_mention`); the reference's own group rules, admins and
  blacklists, its comment rule file and its persisted dedupe file are not taken (in-memory dedupe
  of 2,048 ids for 24 h).
- Send attempts 3 = the core retry policy (one send plus two retries, R06.4 rules); the adapter
  does not loop sends itself. Connect attempts 3 = the first long-connection link inside connect.
- Webhook mode requires the verification token (the reference accepts the encrypt key alone) and
  decrypts encrypted bodies (the reference refuses them).
- Replies into a thread reply to the newest message we saw in that thread
  (`reply_in_thread: true`) instead of creating with an undocumented `thread_id` receive type.
- Pending batches are emitted at disconnect (the reference drops them).
- No QR app-registration onboarding (P16-31 setup), no meeting-invite handling, no reaction or
  recall events as synthetic messages, no read receipts (not in the row).

Tests we will write:
- the shared contract suite over the wire fake (long connection), plus a webhook-mode case run;
- nine rapid messages → two events (8 + 1); limits by characters; threads/replies/senders kept
  apart; media batch per kind; commands bypass and keep order;
- webhook: vendor vector decrypt, signed + encrypted event accepted, bad signature 401, unsigned
  event 401, wrong token 401, challenge echoed only with the token, replayed event id dropped;
- Lark domain selected by config (requests go to `open.larksuite.com`);
- frame codec known answers, part reassembly and caps, ping and ack frames;
- token refresh before expiry and after an invalid-token code; auth refusal fatal at connect and
  `report_fatal` in the socket loop;
- reply fallback after a withdrawn message; post → plain retry; card edit by patch;
- secrets and the socket URL never in logs, reprs or errors; `indus send` opens nothing inbound.

## Decisions for the lead
- Registry row and `BUILTIN_CREDENTIALS` row (P15 core, not edited): see the report.
- `feishu` extra = `httpx-ws==0.9.0` only (the same pin as `mattermost`).
- Webhook mode refuses to start without `FEISHU_VERIFICATION_TOKEN`; drop the check in
  `FeishuOptions.problem` to accept the encrypt key alone.
- The webhook listener keeps the kit defaults (loopback); no per-IP rate limiter (the kit has
  none; the reference's 120/min limiter is left out).

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] connect: credentials checked first, unknown mode refused, webhook needs its secret (ours: the
  verification token), three connect attempts 1 s / 2 s apart (tests:
  `test_adapter.py::test_connect_tries_the_first_link_three_times`,
  `::test_an_unknown_domain_or_mode_is_a_configuration_error`,
  `::test_webhook_mode_needs_the_verification_token`)
- [x] bot identity from `bot/v3/info` for mention gating; the bot's own messages dropped
  (`::test_mentions_are_resolved_and_the_bots_own_messages_dropped`)
- [x] dead long connection rebuilt with capped backoff (`::test_a_lost_link_reopens_with_backoff`);
  refused credentials while running are fatal (`::test_refused_credentials_are_fatal_at_connect_and_while_running`)
- [x] events before the loop / sink is ready are queued (cap 1,000) and replayed
  (`::test_events_before_the_sink_wait_for_it`)
- [x] text batching 0.6 s / 8 messages / 4,000 characters, debounced, only identical reply and
  thread context merged; media batching 0.8 s per kind (Verify "9 rapid messages → two batches":
  `::test_nine_rapid_messages_make_two_batches`, `::test_the_quiet_window_restarts_with_each_message_and_respects_the_char_cap`,
  `::test_batches_keep_senders_threads_and_order_apart`, `::test_photos_within_the_media_window_become_one_event`)
- [x] message normalisation: text with mention placeholders, posts (styles, links, code blocks,
  images), image / file / audio / media, `@_all`; empty text after the self mention dropped; native
  `thread_id` only marks a topic; parent message text fetched for replies (cache 512)
  (`::test_rich_text_posts_become_markdown_and_attachments`, `::test_a_reply_carries_the_text_it_answers`,
  `::test_thread_replies_stay_in_the_thread`)
- [x] outbound: one text/post decision for all pieces of a split reply; rejected post retried as
  plain text (core retry on bad format; edits fall back in the adapter); reply → new message when
  the target was withdrawn, never outside a thread
  (`::test_markdown_goes_as_a_post_in_every_piece_and_plain_as_text`,
  `::test_a_reply_to_a_withdrawn_message_becomes_a_new_message`)
- [x] approval card with orange header and buttons; the press routed with its callback data; the
  card closed in place (`::test_an_approval_card_press_and_close`; contract `approvals`, `inbound`)
- [x] file routing by extension (opus → audio, mp4/mov/m4v/avi → media, pdf/doc/xls/ppt, else
  stream); image with caption as a post (`::test_media_uploads_and_processing_reactions`)
- [x] processing reactions: Typing at start, removed at the end, CrossMark on failure (same test)
- [x] webhook: JSON only (415), token compared in constant time before the challenge is echoed,
  signature `sha256(timestamp + nonce + key + body)` compared in constant time; encrypted bodies
  decrypted (Verify "webhook decrypt with the encrypt key": `test_frames.py::test_the_vendor_example_decrypts`,
  `::test_an_openssl_made_body_decrypts`, `test_adapter.py::test_webhook_mode_decrypts_and_checks_every_layer`,
  `::test_the_url_challenge_is_echoed_only_with_the_token`, `::test_the_webhook_takes_json_only`)
- [x] both domains (Verify "Lark domain selected by config": `::test_the_lark_domain_is_selected_by_config`)
- [x] dedupe of message ids (2,048, 24 h) and event ids; contract suite in both transports
  (`tests/contract/gateway/feishu_case.py`, cases `feishu` and `feishu_webhook`)
- Deliberate differences: no lark-oapi (own client, own frame codec, httpx-ws); sends are never
  re-sent by the adapter after a timeout (the reference retries any exception three times, R06.4);
  encrypted webhook bodies are accepted (the reference refuses them) and the verification token is
  required in webhook mode; per-group rules, admins, blacklists, `allow_bots` and the DM allowlist
  are the core's; dedupe is in memory (the reference persists a file); `user_id` is the open id;
  pending batches are emitted at disconnect; thread replies answer the newest message seen in the
  thread instead of a `thread_id` receive type.
- Not taken: the mention hint line listing other people's open ids, reaction and card-click
  events as synthetic messages (our cards only carry our callback data), the update-prompt card,
  voice transcoding to Opus with ffmpeg (non-Opus voice goes as a file), OGG duration, the
  audio-in-thread retry for code 99992402, the long-message continuation delay (2 s when a piece is
  near Feishu's client split), meeting invites, read receipts, recalls, QR onboarding (P16-31),
  the per-IP webhook rate limiter and anomaly counters (the kit binds loopback by default), the
  `FEISHU_BOT_*` environment fallbacks for the bot identity.

## Status

Done (2026-10-20). Contract suite green in both transports; Verify cases pinned above. Shared files:
`pyproject.toml` (extra `feishu`), `THIRD_PARTY_NOTICES.md` (httpx-ws / wsproto rows),
`docs/plan/phases/00-layout-and-conventions.md` (pin row), `foundation/config/schema.py` +
`tests/unit/foundation/config/test_schema.py` (row `platforms.feishu`),
`tests/contract/gateway/conftest.py` (one import). Registry row and `BUILTIN_CREDENTIALS` row are
for the lead (P15 core); `uv.lock` needs a re-lock for the new extra (metadata only, checked:
no package added or moved).
