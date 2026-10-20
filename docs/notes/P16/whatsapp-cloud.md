# Task: WhatsApp Business Cloud adapter (P16-04) Phase: 16 Owner: wave-24 builder E4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/whatsapp_cloud/adapter.py`
(whole file: settings, webhook handshake and POST, signature, dispatch walk, dedupe, interactive
buttons and lists, media upload/download, typing + read, registration), its `gateway_env.py` and
`plugin.yaml` (env names), `gateway/platforms/whatsapp_common.py` lines 20–100 and 247–290 (the
shared formatting and the DM gate it calls). Evidence: none.
Interop (checked against Meta's WhatsApp Cloud API reference, not the reference code): Graph API
`POST /<ver>/<phone-number-id>/messages` (`messaging_product: "whatsapp"`, `to`, `type`, `text.body`
≤4,096 characters, `context.message_id`, `interactive` `button` ≤3 reply buttons with title ≤20 and
id ≤256, `list` ≤10 rows with title ≤24 and description ≤72, body text ≤1,024, action `button` ≤20;
media objects by `id` with `caption` (image, video, document) and `filename` (document); the read
receipt with `typing_indicator: {type: "text"}`), `POST /<ver>/<phone-number-id>/media` (multipart
`file`, `type`, `messaging_product`), `GET /<ver>/<media-id>` (`url`, `mime_type`, `file_size`;
the URL needs the same bearer token), `GET /<ver>/<phone-number-id>` (credential check), webhooks
(`hub.mode`/`hub.verify_token`/`hub.challenge` GET handshake; POST signed with
`X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the raw body keyed by the app secret>`; payload
`object: whatsapp_business_account`, `entry[].id` = WABA id, `changes[].field == "messages"`,
`value.metadata.{phone_number_id, display_phone_number}`, `value.contacts[].{wa_id, profile.name}`,
`value.messages[]`, `value.statuses[]`), Graph error JSON `{error: {message, type, code,
error_data.details, fbtrace_id}}` and the codes we read: 190 (token), 131047 (outside the 24-hour
customer-service window), 130429 / 131056 / 131048 / 80007 / 4 / 613 (throughput, pair and spam
rate limits), 131026 (message undeliverable), 131009 / 100 (parameter). Graph API version pinned
to `v23.0` (configurable, `api_version`).

What it does (own words): the official WhatsApp Business route. People write to the business
number; Meta calls our webhook with signed JSON; replies go out through the Graph API with the
business's access token. There is no local bridge and no QR pairing: the operator creates the app
in Meta's dashboard, points its webhook at our URL (port 8090 by default, behind their own TLS
proxy or tunnel) and gives us the phone-number id, the access token, the app secret (the HMAC key
for the webhook signature) and the verify token (the GET handshake). Messages are 1:1 only. Meta
retries webhooks it did not get a 200 for (for days), so every message id (`wamid`) is remembered
(5,000) and a repeat is dropped; the vendor gets its 200 as soon as the signature and JSON are
good and the work runs after. Approvals and clarify questions use interactive messages: up to three
choices as reply buttons, four to ten as a list, more as the text card. Meta only lets a business
send free-form messages within 24 hours of the person's last message; outside it Graph refuses
with 131047, which we classify as `forbidden` with that reason (never retried, no "delivery
failed" notice into a window that is shut).

Inputs → outputs:
- `create_adapter(AdapterConfig)`: credentials `WHATSAPP_CLOUD_ACCESS_TOKEN`,
  `WHATSAPP_CLOUD_PHONE_NUMBER_ID` (both required), `WHATSAPP_CLOUD_APP_SECRET` (required to
  receive), `WHATSAPP_CLOUD_VERIFY_TOKEN` (handshake), optional `WHATSAPP_CLOUD_WABA_ID`; config
  `platforms.whatsapp_cloud.{webhook_host, webhook_port (8090), webhook_path (/whatsapp/webhook),
  webhook_public, webhook_max_body_bytes (3 MiB), api_version, base_url, waba_id}` → adapter.
- connect: `GET /<ver>/<phone-id>` (401 / code 190 → fatal `whatsapp_cloud_auth`; other 4xx → fatal
  config; 5xx / network → retryable), then the listener (not in send-only mode).
- webhook GET → `hub.challenge` echoed (verify token compared in constant time; 503 when none is
  set, 403 on mismatch, 400 on a bad mode or challenge).
- webhook POST → signature first (401), JSON (400), then per message: dedupe, identity check
  (phone-number id, WABA id when configured), normalise, `emit` in the background → 200.
- message kinds: text; image → photo; audio (`voice: true` → voice, else audio); video; document;
  sticker; location (text line); contacts (names); interactive `button_reply` / `list_reply` →
  `metadata["button"]` = the button id (our callback data); template quick-reply `button` → its
  text; reaction / system / unsupported / unknown / group-shaped → no event.
- outbound: text split at 4,096 code points (Markdown → WhatsApp markup per piece), reply quote
  on the first piece; media upload then send by id; approvals/clarify as above; typing = read
  receipt + typing indicator on the chat's last inbound message.
- `register_fetchers`: `wac:<media id>` refs → two-step Graph download (bearer token only to Meta
  hosts or the configured Graph host, size capped).

Files: `src/indus/gateway/adapters/whatsapp_cloud/__init__.py`, `.../whatsapp_cloud/adapter.py`,
`.../whatsapp_cloud/markup.py`, `tests/fakes/platform/whatsapp_cloud_wire.py`,
`tests/contract/gateway/whatsapp_cloud_case.py`,
`tests/unit/gateway/adapters/whatsapp_cloud/test_adapter.py`; shared: contract `conftest.py` (case
import + `AdapterCase.lacks`, identical to the parked w23-e3 hunk), `test_adapter_contract.py`
(the `lacks` handling, identical to w23-e3), `foundation/config/schema.py` row
`platforms.whatsapp_cloud` (+ its test), traps R06.4 rows.

Edge cases / failure modes:
- body over 3 MiB → 413 before reading it all (kit); bad / missing signature → 401, nothing parsed;
  no app secret → connect refused (fatal) unless send-only.
- duplicate wamid (Meta retry, or two entries in one batch) → one event.
- a signed callback for another phone-number id or WABA (a shared Meta app) → ignored.
- statuses (sent/delivered/read/failed) → ignored (debug log, masked).
- malformed message entries (not dicts, missing `from`) → skipped, the rest of the batch kept.
- a tap whose button id is not one of the referenced prompt's ids → dropped.
- send outside the 24-hour window → `forbidden`, error text names the window; final (no retry, no
  plain resend, no notice).
- Graph 401 / code 190 on a send → final failure + `report_fatal("whatsapp_cloud_auth")`.
- media: unknown local file / over Meta's cap → refused locally; upload failure → classified.
- inbound media id with path characters → refused by the fetcher; download URL on a foreign host
  → refused (the token never leaves Meta).
- phone numbers in logs and error texts are masked (last four digits kept).

Our design differences / decisions:
- Our names (`WhatsAppCloudAdapter`, `CloudSettings`, `to_whatsapp`, `wac:` refs).
- Built on the P16-01 kit: loopback bind by default (the reference binds every interface).
- No app secret = no start (fatal, clear reason) instead of a listener that answers 503.
- Graph `v23.0` default (the reference defaults to v20.0, past its support window).
- DMs only, like the reference; groups dropped with a masked warning.
- Approvals: ≤3 actions as buttons, 4–10 as a list (all four choices kept), the text card when the
  prompt body would not fit 1,024 characters (never a cut-off command). The reference shows only
  approve / deny buttons.
- Taps go through the core prompt pipeline as `metadata["button"]` (the core checks the request id
  and who pressed); the adapter keeps a 1,000-entry map of our interactive messages so a tap
  naming an id that prompt never offered is dropped.
- Markdown → WhatsApp markup is our own converter in the adapter package (the core formatter has no
  WhatsApp markup; see "Decisions for the lead").
- No document-text inlining in the adapter (the P15 intake already inlines small text files), no
  MP3 → Opus transcoding, no rich sent store (the core sent index does reply context).
- `user_id` = the `wa_id` digits; `user_id_alt` = `+<digits>` so an allowlist entry matches either
  spelling.

Tests we will write:
- contract suite through the wire fake (every check, no real network).
- duplicate wamid ignored; bad signature 401 (compare function spied); GET handshake cases.
- out-of-window send classified forbidden, attempted once, no notice; token refusal fatal.
- 4 approval choices render as a list, 3 as buttons, an oversized prompt as text; clarify.
- inbound kinds (text, voice, image with caption, document, location, interactive, reaction,
  statuses, foreign phone id) and the tap check.
- media upload + send by id; fetcher two-step download and host check; send-only opens no
  listener and consumes nothing; create_adapter reads credentials; logs carry no token or number.
- markup conversion cases.

## Decisions for the lead
- WhatsApp markup: `Capabilities.markup = "markdown"` (the sender applies the Discord-style
  pass: tables to code blocks) and the adapter turns each piece into WhatsApp syntax before
  posting (`markup.to_whatsapp`; plain sends are left alone). Proposed core change (P15-15 follow-up):
  add `"whatsapp"` to `Markup` with a `_Style` subclass in `outbound/formatting.py` (bold `*`,
  italic `_`, strike `~`, headers bold, links `label (url)`) and drop the adapter-side pass.
- Missing app secret is fatal at connect (code `whatsapp_cloud_app_secret_missing`); to allow a
  send-mostly setup instead, start the listener and answer POSTs 503.
- Graph version default `v23.0`; bump in `adapter.DEFAULT_API_VERSION`.
- Registry row and credentials (not edited; to add in P15 core):
  `_chat_entry("whatsapp_cloud", "WhatsApp Business (Cloud API)", sdk=("httpx",), extra_name="whatsapp",
  required_env=("WHATSAPP_CLOUD_ACCESS_TOKEN", "WHATSAPP_CLOUD_PHONE_NUMBER_ID"), max_len=4096, hint=...)`
  with `install_hint="pip install indus-agent (WhatsApp Cloud needs no extra)"`;
  `BUILTIN_CREDENTIALS["whatsapp_cloud"] = ("WHATSAPP_CLOUD_ACCESS_TOKEN", "WHATSAPP_CLOUD_PHONE_NUMBER_ID")`.
- Capability flags (research 06 Q4): `dm_is_private=True`, `owns_access_policy=False` (the core
  chain with `INDUS_WHATSAPP_CLOUD_ALLOWED_USERS`), `auth_upstream=False` (the signature proves
  Meta, not the person), `supports_edit=False`, `push_capable=True` (inside the 24-hour window).

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] phone-number id + token required; refused token / unknown number fatal at connect (test:
  `tests/unit/gateway/adapters/whatsapp_cloud/test_adapter.py::test_connect_refusals_are_fatal_with_a_reason`)
- [x] GET handshake: no verify token 503, mode 400, token mismatch 403 (constant time), missing or
  unsafe challenge 400, challenge echoed as text (test: `::test_the_subscription_handshake`)
- [x] POST: body cap 3 MiB before reading, signature over the raw body before JSON, non-object
  400, 200 after a good request (tests: `::test_an_oversized_body_is_refused_before_it_is_read`,
  `::test_a_bad_signature_is_401_before_parsing_and_compared_in_constant_time`)
- [x] dispatch walk: `object`, `field == messages`, phone-number id and WABA bound, contacts'
  profile names, statuses only logged (test: `::test_only_our_number_and_account_and_message_changes_count`)
- [x] wamid dedupe (5,000) before the event is built; a build error drops one message, not the
  batch (tests: `::test_a_duplicate_wamid_is_ignored`, `::test_message_kinds_become_events`)
- [x] contentless kinds (reaction, system, unsupported, unknown) and group-shaped payloads give no
  event; text / button / interactive / media caption bodies (test: `::test_message_kinds_become_events`)
- [x] media ids checked against a safe charset before they reach a URL; two-step download with
  the bearer token on the second hop (test:
  `::test_the_fetcher_downloads_in_two_steps_and_keeps_the_token_on_meta_hosts`)
- [x] reply context: quoted id, "own" when the quote's author is our display number (test:
  `::test_a_reply_to_our_message_is_marked_own`)
- [x] last inbound wamid per chat (bounded) for read + typing (test:
  `::test_typing_marks_the_last_inbound_message_read`)
- [x] text: Markdown → WhatsApp markup, 4,096 pieces, quote on the first piece only (test:
  `::test_long_text_is_split_converted_and_quotes_only_the_first_piece`,
  `::test_markdown_becomes_whatsapp_markup`, `::test_the_sender_formats_tables_and_markup_for_whatsapp`)
- [x] interactive: ≤3 reply buttons (title 20), list (title 24, description 72, button label 20),
  state remembered (1,000) only after a successful send (tests:
  `::test_four_approval_choices_render_as_a_list_and_three_as_buttons`,
  `::test_clarify_choices_are_numbered_buttons_with_the_full_text_in_the_body`,
  `::test_a_tap_must_name_an_option_its_prompt_offered`)
- [x] media out: local file checked (exists, per-type cap) before upload, `/media` multipart,
  send by id, caption on image/video/document, filename on documents (test:
  `::test_media_is_uploaded_then_sent_by_id`)
- [x] Graph error body read into the failure text with its code (tests:
  `::test_a_send_outside_the_24_hour_window_is_forbidden_final_and_tried_once`,
  `::test_numbers_and_tokens_are_masked_in_errors_and_logs`, contract `error-classification`)
- [x] Verify column: contract suite (`tests/contract/gateway/test_adapter_contract.py`, case
  `whatsapp_cloud`, every rule + no real network); duplicate wamid ignored; bad signature 401;
  out-of-window send forbidden, not retried; 4 approval choices render as a list.
- Deliberate differences: loopback bind by default (the reference binds all interfaces); no app
  secret = connect refused (the reference starts and answers 503); Graph v23.0 default (v20.0);
  out-of-window, token and throughput errors typed (the reference returns raw text); all approval
  choices offered as a list (the reference shows approve / deny only); prompts over the 1,024
  body go as the text card (the reference cuts the body); more than ten clarify choices go as the
  text card (the reference silently keeps the first ten); taps are core button events checked
  against the prompt's own ids (the reference resolves them in the adapter and falls back to text
  for stale taps); non-voice audio is `audio`, stickers `sticker`, locations `location` (the
  reference maps them to voice / photo / text); the DM gate is the core authorization chain, not
  an adapter-owned policy; `preview_url` only when the piece holds a URL.
- Not taken: the health page counters and ffmpeg probe (the kit's `/health` is minimal), MP3 →
  Opus voice transcoding, sending media by public link, the rich sent store and quoted-media
  lookup (core sent index covers reply text), the clarify "Other" row (a typed reply answers a
  clarify), slash-confirm buttons, the shared-ingress profile prefix, the reply prefix setting,
  zero-width / odd-space clean-up of outbound text.

## Status

Done (2026-10-20). New files only, plus the shared edits listed under Files (contract `lacks`
identical to the parked w23-e3 hunk; schema row `platforms.whatsapp_cloud`). Registry row and
`BUILTIN_CREDENTIALS` entry are for the lead (see Decisions). No new dependency.
