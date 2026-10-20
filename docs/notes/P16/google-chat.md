# Task: Google Chat adapter (P16-13) Phase: 16 Owner: wave-25 builder E7

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/google_chat/adapter.py`
(all: inbound modes, envelope formats, HTTP bearer check, Pub/Sub supervisor, send / edit / delete,
thread resolution, typing card, attachments and the trusted-host guard, standalone send, registry
entry), `google_chat/cards.py` (skim: text dialect, cards v2 shape), `google_chat/plugin.yaml`;
the platform-event route in `gateway/platforms/api_server.py` (lines 1520–1570); test names only in
`tests/gateway/test_google_chat.py` and `tests/gateway/test_parked_route_refusal_google_chat.py`.
Not read: `google_chat/oauth.py`, `setup_files.py` (per-user OAuth for native file uploads: not built,
see below). Evidence: none.

What it does (own words): connects Indus Agent to Google Chat as a Chat app. Outbound everything
goes through the Chat REST API v1 (`chat.googleapis.com`) with a service-account access token: the
adapter signs a short JWT assertion with the service account's private key (RS256) and trades it at
Google's OAuth token endpoint for a bearer token (scopes `chat.bot` and `pubsub`), cached until a
minute before it expires. Inbound comes one of two ways:

- **HTTP endpoint** (default): Google POSTs each interaction event to our callback listener (P16-01
  kit, loopback by default, body ≤ 1 MiB). Every request must carry `Authorization: Bearer <token>`
  where the token is a Google-signed JWT. Two flavours, picked by the configured audience: the
  app's **project number** (issuer `chat@system.gserviceaccount.com`, keys from that account's JWK
  set) or the endpoint **URL** (an OIDC ID token, issuer `https://accounts.google.com`, keys from
  Google's OAuth JWK set, `email` = `chat@system.gserviceaccount.com` and `email_verified`). The
  check runs before the body is parsed: RS256 only (never `none`, never HMAC or another alg), `kid`
  must name a key from the injectable key fetcher (cached 300 s; one forced refresh for an unknown
  kid, at most once a minute), signature, issuer, audience, expiry (and `iat`/`nbf` with 60 s skew).
  A failure is 401 with no detail. While the gateway drains, a verified event is answered 503 with
  `Retry-After` so Google redelivers it later instead of it being lost.
- **Pub/Sub pull**: the app publishes events to a topic; we pull the configured subscription over
  the Pub/Sub REST API (`:pull`, `:acknowledge`), decode each message's base64 data as the same
  event JSON, hand it on and acknowledge. Pulling pauses while the gateway drains (the messages stay
  in the subscription). A failed pull backs off 2 s doubling to 120 s; 401/403/404 or ten failures in
  a row stop the loop as fatal.

Events: `MESSAGE` becomes one `InboundEvent` (classic Chat event shape and the Workspace add-on
`chat.messagePayload` shape); `CARD_CLICKED` (or `chat.buttonClickedPayload`) becomes a button
event whose `metadata["button"]` is the callback data we put on the button; `ADDED_TO_SPACE` /
`REMOVED_FROM_SPACE` are logged only. Messages are deduped by resource name (both modes are at
least once). Space type `DIRECT_MESSAGE` (or legacy `DM`) → `dm`, anything else → `group`; a
message with `threadReply: true` is in a thread (`chat_type` `thread`, `thread_id` = the thread's
resource name); a top-level message keeps the space's session. The sender's `users/<id>` is the
`user_id` (stable) and the lower-cased email the `user_id_alt` (allowlists may list emails).
`argumentText` (the text without the app's @mention) is the text; a mention annotation of the app
marks `mentioned`. Attachments with `attachmentDataRef.resourceName` become `gchat-media:<name>`
refs downloaded through the Chat media endpoint; a `downloadUri` is used only when it is https on one
of a few exact Google hosts (never the token to any other host); Drive-only files are skipped.

Outbound: text cut at 4,000 characters with the shared chunker, Markdown converted to Chat's
`*bold*` / `_italic_` / `~strike~` / `<url|text>` dialect (Slack mrkdwn is the same dialect);
replies into a thread send `thread.name` with `messageReplyOption=
REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` (a vanished thread still delivers); edits patch
`text` and drop cards (`updateMask=text,cardsV2`); deletes delete; approvals and clarify prompts are
cards v2 with one button per choice (`onClick.action.function` `indus.button`, parameter `data` =
our callback data) and the text card as fallback text. Chat has no typing API: no typing marker.

Inputs → outputs: `AdapterConfig` (`GOOGLE_CHAT_SERVICE_ACCOUNT_JSON` secret: the key file's JSON;
`platforms.google_chat.*` / vendor env names for mode, subscription, audience) → a connected adapter;
signed HTTP callbacks or pulled Pub/Sub messages → `InboundEvent`s; sends / edits / deletes / cards
→ Chat REST calls → `SendResult`s; `gchat-media:` / Google URLs → bytes for the intake.

Files: `src/indus/gateway/adapters/google_chat/__init__.py`, `.../google_chat/adapter.py`,
`.../google_chat/auth.py` (service-account token source, Google ID-token verifier, JWK cache),
`.../google_chat/client.py` (Chat + Pub/Sub REST over httpx, error readings), `.../google_chat/
events.py` (event normalisation), `tests/fakes/platform/google_chat_wire.py`,
`tests/contract/gateway/google_chat_case.py`, `tests/unit/gateway/adapters/google_chat/
test_adapter.py`, `.../google_chat/test_auth.py`; shared: `pyproject.toml` (extra `google-chat`),
`THIRD_PARTY_NOTICES.md` (cryptography row names the extra), `tests/contract/gateway/conftest.py`
(one import line), `src/indus/foundation/config/schema.py` + its test (row `platforms.google_chat`).

Vendor API: Google Chat API v1 (`spaces.messages.create/patch/delete`, `spaces.get`,
`media.download`), Cloud Pub/Sub v1 REST (`projects.subscriptions.get/pull/acknowledge`), OAuth 2.0
JWT bearer grant (`urn:ietf:params:oauth:grant-type:jwt-bearer`, RFC 7523) at
`https://oauth2.googleapis.com/token`, Google JWK sets
`https://www.googleapis.com/oauth2/v3/certs` and
`https://www.googleapis.com/service_accounts/v1/jwk/chat@system.gserviceaccount.com`. No Google SDK:
our own client over httpx; RSA signing and verification through `cryptography` 50.0.2
(Apache-2.0 OR BSD-3-Clause; already in the base install via keyring/SecretStorage and mcp's
pyjwt[crypto]), imported lazily and listed in the new `google-chat` extra.

Capability flags (research 06 Q4): `dm_is_private` true (a DIRECT_MESSAGE space is the user and the
app), `owns_access_policy` false, `auth_upstream` false (the token proves Google sent it, not who
may talk to the agent), `supports_edit` true, `supports_delete` true, `supports_buttons` true
(cards v2), `push_capable` true (proactive sends to a space work with the app's token),
`splits_natively` true, cap 4,000 code points, markup `mrkdwn`.

Edge cases / failure modes:
- Token: missing header, not `Bearer`, not three base64url parts, bad JSON, `alg` other than RS256
  (incl. `none`, `HS256`), missing/unknown `kid`, bad signature, wrong `iss`, wrong `aud` (string or
  list), expired, `iat`/`nbf` in the future beyond 60 s, URL flavour without the expected verified
  email → 401, nothing parsed, nothing emitted.
- Key fetch failure → 401 for that request (fail closed), the cache keeps old keys until expiry.
- Service account JSON: not JSON, not `type: service_account`, no `private_key` / `client_email`,
  a `token_uri` that is not Google's https token endpoint → fatal config error at connect (the key
  never leaves for another host); messages never contain the key or the token.
- Token endpoint 400/401 (`invalid_grant`) → fatal; 5xx / connect errors → retryable connect error.
- Send: 429 → rate limited with Retry-After; 403 → forbidden; 404 → chat not found unless the
  error names a thread (thread not found) or the call was an edit/delete (message not found);
  400 text "too long" → too long; read timeout → maybe delivered (never retried); 401 → refresh the
  token once and retry, then a final failure (never a dead chat).
- Drain: HTTP → 503 + `Retry-After` after the token check, the dedupe claim is not taken; Pub/Sub →
  no pull while draining. Listener stopping → the kit's 503.
- Duplicate message name → dropped; bot senders → `is_bot` (the core's bot policy decides).
- Attachment from a non-Google or http host → refused by the fetcher, no request made.
- Send-only open (`indus send`): token only; no listener, no pulling.

Our design differences / decisions:
- Our names (`GoogleChatAdapter`, `ServiceAccountTokens`, `GoogleTokenVerifier`, `JwkCache`,
  `ChatClient`, `PubSubClient`), our own HTTP clients instead of the Google SDKs (no gRPC, no
  discovery documents); Pub/Sub over REST pull.
- Credentials only from the secret store (then env, vendor name): inline key JSON only, no key file
  path and no Application Default Credentials (no metadata-server lookups).
- The HTTP callback has its own listener (P16-01 kit) rather than a route on the API server.
- Thread placement from the message's own `threadReply` flag instead of a persisted per-thread
  counter file; groups keep a top-level message in the space session.
- No "thinking…" typing card (Chat has no typing API; the card/patch dance needs runner hooks we do
  not have), no per-user OAuth `/setup-files` flow: media uploads need a person's own credentials,
  so `send_*` media return a clean refusal (the sender then posts its "could not send" notice).
- No relay/flat envelope format, no bot-id cache file, no bootstrap spaces.
- Drain → 503 needs the daemon to install the drain probe (`set_drain_probe`): wiring for the lead.

Tests we will write:
- verifier: good token both flavours; `none`/HS256/unknown kid/forged signature/wrong iss/aud/expired
  /future iat/missing email → refused; key cache TTL and the one forced refresh.
- token source: assertion claims and signature checked by the fake token endpoint; cache reuse;
  refresh before expiry; bad key JSON / foreign `token_uri` refused; secrets never in errors/reprs.
- adapter: forged token 401 (Verify), draining → 503 (Verify), untrusted attachment host not fetched
  (Verify), duplicate dropped, thread/dm/group shapes, card click, Pub/Sub ack and backoff and fatal,
  send-only open, fetcher registration, 401 refresh-and-retry, create_adapter from config.
- contract: the shared suite in both modes (`google_chat` HTTP, `google_chat_pubsub`).

## Decisions for the lead
- Registry row (P15 core, not edited): `google_chat`, label "Google Chat", factory
  `indus.gateway.adapters.google_chat:create_adapter`, probe `modules_installed("cryptography")`,
  extra `google-chat`, `required_env=("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON",)`, cap 4,000.
  `BUILTIN_CREDENTIALS["google_chat"] = ("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON",)`.
- Drain probe wiring: the daemon calls `adapter.set_drain_probe(lifecycle.notice)` (duck-typed) when
  the platform comes up; a base-class seam would be cleaner (P15 follow-up).
- Default listener port 8647, path `/google-chat` (P16-36 port table).
- `cryptography` goes in the `google-chat` extra although the base install already brings it.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] bearer verified before the body is used, audience and service-account identity checked, keys
  cached 300 s (tests: `test_auth.py::test_claims_are_checked`,
  `::test_keys_are_cached_for_300_seconds_and_unknown_kids_refetch_once_a_minute`,
  `test_adapter.py::test_a_forged_token_is_refused_with_401_before_the_body_is_read` — Verify)
- [x] parked/draining delivery answered retryably (503) and the redelivery admitted once (Verify;
  test: `test_adapter.py::test_while_the_gateway_drains_callbacks_get_503_and_the_redelivery_is_admitted`)
- [x] envelope formats: classic and add-on; membership events logged only; dedupe by message name
  (tests: `::test_card_clicks_add_on_events_and_space_membership`, `::test_pubsub_pulls_hands_on_and_acknowledges`)
- [x] DM vs group, threads, email as an allowlist identity, slash command text, bot senders
  (tests: `::test_message_shapes_threads_mentions_and_identities`, `::test_bad_payloads_no_sink_and_bot_senders`)
- [x] attachments: resourceName via media download, downloadUri only from trusted Google hosts,
  Drive-picker files skipped (Verify; tests: `::test_an_attachment_from_an_untrusted_host_is_never_fetched`,
  `::test_attachments_become_refs_and_the_fetcher_downloads_them`)
- [x] Pub/Sub: subscription checked at connect (404/403 fatal), backoff 2 s doubling, fatal after 10
  failures, 401/403 fatal (tests: `::test_pubsub_*`)
- [x] send: chunks ≤ 4,000, `thread.name` + `REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD`, edits by patch,
  deletes, `spaces.get`, cards v2 with buttons (test: `::test_threads_dm_targets_cards_edits_and_deletes`)
- [x] service-account JSON parsing and token exchange (tests: `test_auth.py::test_the_key_json_is_checked_and_never_echoed`,
  `::test_the_assertion_is_signed_and_the_token_cached_until_a_minute_before_expiry`)
- [x] gap found at compare: our trusted-host rule matched by suffix (`googleapis.com`,
  `googleusercontent.com`), which also covers hosts serving other people's content; now an exact host
  list (the reference's list, with chat/www googleapis), pinned by
  `test_adapter.py::test_an_attachment_from_an_untrusted_host_is_never_fetched`.
- [x] gap found while testing: a 401 on a send carried the "chat not found" reading (a dead chat);
  the 404 reading is now applied to 404s only (`::test_a_401_refreshes_the_token_once_then_is_final`).
- Deliberate differences: the reference marks the platform fatal on any 403 from a send; ours reports
  the chat forbidden (other spaces keep working). The reference retries 429/5xx three times inline;
  ours leaves retries to the core policy (R06.4). The reference only acknowledges card clicks; ours
  hands them on so approval buttons work. The reference requires the expected service-account email
  to be configured for HTTP mode; ours defaults to Chat's own account
  (`chat@system.gserviceaccount.com`) and also supports the project-number audience. Over-long edits
  are refused (the reference truncates with an ellipsis). The reference's ID-token check goes through
  google-auth; ours is our own verifier over `cryptography` with the algorithm pinned to RS256.
- Not taken: per-user OAuth `/setup-files` and native file uploads, the "thinking…" typing card,
  persisted thread counts, bot-id cache file and bootstrap spaces, relay/flat envelopes,
  `slashCommand.commandId` → `/cmd_<id>` rewriting (we keep the command name the annotation gives),
  ADC credentials, the out-of-process standalone sender (`indus send` uses the send-only open).

## Status

Done (2026-10-20). Shared files: `pyproject.toml` (extra `google-chat`), `THIRD_PARTY_NOTICES.md`
(cryptography / cffi / pycparser rows), `tests/contract/gateway/conftest.py` (one import),
`src/indus/foundation/config/schema.py` + `tests/unit/foundation/config/test_schema.py`
(`platforms.google_chat`). Registry row, `BUILTIN_CREDENTIALS` row and the drain-probe wiring left
to the lead (above).

## Landing (integrator, 2026-10-20)

The drain probe is now the base adapter seam (`Adapter.set_drain_probe` / `draining()`), installed by
the daemon on every platform up (D-16.452). Signature checks go through the shared `adapters/_jws.py`
(D-16.451). The PEM-shaped test literal is built at run time, and a real throwaway EC key covers the
non-RSA case (lead ruling).
