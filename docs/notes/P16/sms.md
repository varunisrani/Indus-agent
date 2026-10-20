# Task: SMS adapter over Twilio (P16-10) Phase: 16 Owner: wave-24 builder E4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/sms/adapter.py`
(whole file: settings, connect refusals, REST send, signature check with the port variant, webhook
handler, standalone send, registration), `plugins/platforms/sms/gateway_env.py` and `plugin.yaml`
(env names). Evidence: none.
Interop (checked against Twilio's documentation, not the reference code): Programmable Messaging
REST `2010-04-01`: `POST /2010-04-01/Accounts/{AccountSid}/Messages.json` (form `To`, `From`,
`Body`; HTTP Basic auth with the account SID and auth token; answers 201 with `sid`), `GET
/2010-04-01/Accounts/{AccountSid}.json` (credential check), error JSON `{code, message,
more_info, status}` and the codes we read: 20003 (authentication), 20429 (too many requests),
21211 / 21614 (invalid or non-mobile `To`), 21610 (recipient unsubscribed with STOP), 21408
(region not enabled), 21617 (body over 1,600 characters); incoming-message webhook (form
`MessageSid`, `AccountSid`, `From`, `To`, `Body`, `NumMedia`, `MediaUrl{N}`, `MediaContentType{N}`,
`OptOutType`); request validation `X-Twilio-Signature` = base64 HMAC-SHA1 (key: auth token) of the
full URL Twilio called (query included) followed by every POST parameter name and value sorted by
name, where an explicit :443 / :80 in the signed URL is optional on Twilio's side (both spellings
have to verify); the empty TwiML answer
`<Response/>`; the standard opt-out / opt-in / help keywords handled by Twilio itself (STOP,
STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT, START, YES, UNSTOP, HELP, INFO).

What it does (own words): text messages to and from a Twilio phone number. People text the
number; Twilio posts a signed form to our webhook (port 8080 by default, body ≤64 KiB) and we
answer at once with an empty TwiML document (replies never go inline); the agent's reply is sent
through the REST API from the configured number. SMS is plain text: the sender strips Markdown
with the P15 plain formatter; a reply over 1,600 characters (about ten segments, Twilio's cap) is
split by the adapter. No edits, no typing, no buttons (approvals use the `/approve` text card).
Twilio itself answers STOP / HELP and keeps the opt-out list, so those keywords never reach the
agent, and a later send to someone who opted out comes back as `forbidden`.

Inputs → outputs:
- `create_adapter(AdapterConfig)`: credentials `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
  `TWILIO_PHONE_NUMBER` (E.164); config `platforms.sms.{webhook_host, webhook_port (8080),
  webhook_path (/webhooks/twilio), webhook_public, webhook_public_url (required to receive),
  webhook_max_body_bytes (64 KiB), base_url}` → adapter.
- connect: `GET /2010-04-01/Accounts/{sid}.json` (401 → fatal `sms_auth`, 404 → fatal config,
  5xx / network → retryable); then the listener unless send-only; no public URL → fatal (the
  signature cannot be checked without it).
- webhook POST → signature over the public URL + the request's query + sorted form fields (403
  when wrong or missing) → dedupe by `MessageSid` → echo, keyword and foreign-number drops →
  event (text, MMS media as attachments) emitted in the background → 200 empty TwiML.
- outbound: pieces of ≤1,600 UTF-16 units, one REST call each; classified failures.
- `register_fetchers`: MMS media URLs (on the configured Twilio API host only) downloaded with the
  account's Basic auth, size capped.

Files: `src/indus/gateway/adapters/sms/__init__.py`, `src/indus/gateway/adapters/sms/adapter.py`,
`tests/fakes/platform/sms_wire.py`, `tests/contract/gateway/sms_case.py`,
`tests/unit/gateway/adapters/sms/test_adapter.py`; shared: contract `conftest.py` (case import),
`foundation/config/schema.py` row `platforms.sms` (+ its test).

Edge cases / failure modes:
- the URL Twilio signed carries a query string → signed with it; a configured public URL with its
  own query is reduced to scheme/host/path first so the query is never doubled.
- signed with `:443` while we were configured without (or the reverse) → accepted.
- empty auth token → never verifies (a blank HMAC key would be public).
- duplicate `MessageSid` (a Twilio retry) → one event.
- `From` equal to our own number (a loop) → dropped; `To` not our number (another number on the
  account pointed at this webhook) → dropped, replies would come from the wrong number.
- a body that is only STOP/HELP/START-style keyword, or any `OptOutType` → no event.
- an empty message with no media → no event.
- 1,601 characters → two messages; a failure after the first piece resumes with the rest only.
- Twilio 401 on a send → final failure + `report_fatal("sms_auth")`.
- phone numbers masked in every log line and error text (last four digits kept).

Our design differences / decisions:
- Our names (`SmsAdapter`, `TwilioSettings`).
- No "insecure, no signature" switch (D-16.25: no insecure flags): a webhook without a public URL
  refuses to start.
- MMS media arrive as attachments (the reference drops media and needs a body).
- Opt-out / help keywords never start a turn (the reference passes them to the agent while Twilio
  also answers).
- Twilio's error codes are read into typed results (unsubscribed → forbidden, invalid number →
  chat not found, 20429 → rate limited, 21617 → too long); the reference returns raw text.
- Splitting counts UTF-16 units (conservative for emoji); the reference counts characters.
- No shared-ingress profile prefix (one listener per adapter port, as the kit does).

Tests we will write:
- contract suite through the wire fake.
- signature over a URL with a query string; default-port variant; forged / missing signature 403
  (compare spied); configured URL with its own query.
- 1,601 chars → two messages; markdown removed through the P15 sender.
- keywords, echo, foreign `To`, duplicate sid, MMS kinds, empty message.
- error codes → kinds; 401 → fatal; no notice for an unsubscribed number.
- send-only opens no listener; missing public URL fatal; create_adapter reads credentials; logs and
  errors carry no token and no unmasked number; fetcher host check.

## Decisions for the lead
- Webhook URL comes from config `platforms.sms.webhook_public_url` (product setting, not an env
  credential); the reference's `SMS_WEBHOOK_*` env names are not read. Add an `INDUS_SMS_*` env
  bridge later if wanted.
- Inbound whose `To` is not our number is dropped (logged masked); relax by deleting the check in
  `SmsAdapter._accept`.
- Registry row and credentials (not edited; to add in P15 core):
  `_chat_entry("sms", "SMS (Twilio)", sdk=("httpx",), extra_name="sms",
  required_env=("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"), max_len=1600, hint=...)`
  with `install_hint="pip install indus-agent (SMS needs no extra)"`;
  `BUILTIN_CREDENTIALS["sms"] = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")`
  (the number listed so it is masked like a credential).
- Capability flags (research 06 Q4): `dm_is_private=True`, `owns_access_policy=False`,
  `auth_upstream=False`, `supports_edit=False`, `push_capable=True`.

Compare checklist (filled after the rewrite; reopened the files on the Read line):
- [x] from-number required (E.164 checked at build); a missing public URL refuses the start with a
  reason (test: `tests/unit/gateway/adapters/sms/test_adapter.py::test_connect_refusals_are_fatal_with_a_reason`)
- [x] body cap 64 KiB before reading (test: `::test_an_oversized_body_is_refused`)
- [x] signature: empty token never verifies, URL with and without the default port, HMAC-SHA1 over
  URL + sorted fields, compared in constant time, 403 on a missing or wrong header (tests:
  `::test_the_signature_covers_a_url_with_a_query_string`,
  `::test_a_signature_with_or_without_the_default_port_is_accepted`,
  `::test_forged_or_missing_signatures_are_403_and_compared_in_constant_time`)
- [x] `From`/`To`/`Body`/`MessageSid` read; empty sender or empty message ignored; own number
  ignored (echo) (test: `::test_keywords_echoes_foreign_numbers_and_repeats_start_no_turn`)
- [x] event emitted after the answer; the answer is always empty TwiML (tests above)
- [x] REST send: Basic auth, form `To`/`From`/`Body`, sid as message id, pieces ≤1,600 (test:
  `::test_1601_characters_become_two_messages`)
- [x] markdown stripped (the P15 plain formatter through the sender) (test:
  `::test_markdown_is_removed_by_the_plain_formatter`)
- [x] Twilio's error message carried into the failure (tests:
  `::test_twilio_codes_become_typed_results`, `::test_an_unsubscribed_number_is_forbidden_final_and_tried_once`)
- [x] chat info: the number as name, a DM (adapter `_chat_info`)
- [x] Verify column: contract suite (case `sms`, every rule + no real network); signature over a
  URL with a query string; 1,601 chars → two messages; markdown removed.
- Deliberate differences: no insecure no-signature mode (D-16.25); signature always checked (the
  reference skips it when no URL is configured with the insecure flag); MMS media kept as
  attachments (the reference ignores media and drops caption-less MMS); keyword messages and
  `OptOutType` never start a turn; repeated `MessageSid` dropped; messages to another number of
  the account dropped; Twilio codes typed (unsubscribed and region final + forbidden, invalid
  number = chat not found, 20429 rate limited, 21617 too long, 20003 final + fatal state); UTF-16
  lengths; the inbound text is never logged (the reference logs its first 80 characters); no
  `chat_name` (the reference repeats the number).
- Not taken: the shared-ingress profile prefix, the standalone sender's own looser markdown
  stripper (`indus send` goes through the same adapter and P15 sender), `SMS_WEBHOOK_*` /
  `SMS_INSECURE_NO_SIGNATURE` env names (config keys instead), outbound MMS (needs a public media
  URL; refused cleanly by the base).

## Status

Done (2026-10-20). New files only, plus the shared edits listed under Files (contract case
import; schema row `platforms.sms`) and one R06.4 test reference. Registry row and
`BUILTIN_CREDENTIALS` entry are for the lead (see Decisions). No new dependency.
