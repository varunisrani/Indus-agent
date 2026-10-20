# Task: Microsoft Teams adapter (P16-11) Phase: 16 Owner: wave-25 builder E6

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/teams/adapter.py`
(whole file: credentials, service-URL allow-list, token request, inbound message handling, mention
gate, attachments, card actions, approval card, send/typing/media, setup, registration),
`plugins/platforms/teams/plugin.yaml` (env names), test names and one test body
(`test_aad_user_route_survives_conversation_changes`) in `tests/gateway/test_teams.py`. Research 06
§3.3 (Teams row), §3.5 (mention gating). Not read: `summary_writer.py` (meeting pipeline, D-16.87).
Evidence: none.

Vendor API (interop facts, checked against Microsoft's Bot Framework REST v3 and authentication
documentation, not against the reference): inbound activities are POSTed to the bot's endpoint
(`/api/messages`, port 3978 by convention) with `Authorization: Bearer <JWT>`; the JWT is RS256,
issuer `https://api.botframework.com`, audience = the bot's app id, signed by a key published at the
`jwks_uri` of `https://login.botframework.com/v1/.well-known/openidconfiguration`; each key may carry
`endorsements` (channel ids it may sign for), and the token may carry a `serviceUrl` claim that must
equal the activity's. Outbound: a token from the Microsoft identity platform client-credentials
grant (`POST https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`, scope
`https://api.botframework.com/.default`), then `POST {serviceUrl}v3/conversations/{id}/activities`
(send), `POST …/activities/{replyToId}` (reply), `PUT …/activities/{id}` (update), activity types
`message` (text, `textFormat: markdown`, attachments), `typing`; Adaptive Cards as
`application/vnd.microsoft.card.adaptive` attachments (schema 1.4, `Action.Execute`, `fallbackText`);
a card press arrives as an `invoke` activity named `adaptiveCard/action` and is answered in the HTTP
response (`statusCode` / `type` / `value`); edits arrive as `messageUpdate`; channel reply chains are
addressed as `<conversation id>;messageid=<root id>`.

No library: the reference uses `microsoft-teams-apps` (MIT); we talk REST over httpx (a base
dependency) and verify the JWT ourselves (RS256 per RFC 7515/7518, PKCS#1 v1.5 verification by
re-encoding and comparing, RFC 8017 §8.2.2), so there is no `teams` extra, nothing to pin and no new
package. Recorded under decisions.

Capability flags: `max_len` 28,000 code points, `splits_natively` true, `supports_edit` true,
`supports_delete` false, `supports_buttons` true (Adaptive Cards), `owns_access_policy` false,
`auth_upstream` false, `dm_is_private` true, `push_capable` true (stored conversation references),
`markup` markdown, `reply_opens_thread` false.

What it does (own words): Teams pushes every message for the bot to an HTTPS endpoint the operator
exposes (a tunnel or reverse proxy in front of our loopback listener). Each POST carries a signed
token from the Bot Connector; we refuse the request unless the token is a well-formed RS256 JWT
signed by a current Bot Framework key, issued by the Bot Framework, meant for our app id and not
expired (five minutes of clock skew). Only then is the body parsed. A message becomes an
`InboundEvent` (personal → dm, groupChat → group, channel → channel, a channel reply chain → thread),
the bot's `<at>` mention is taken out of the text and recorded, attachments become media references
(fetched later by the intake through a downloader that only talks to Microsoft hosts), and the
conversation's reference (service URL, type, tenant) is stored on disk so the agent can write into
that conversation later, from cron or `indus send`, even after a restart. Replies use a bot token
from the client-credentials grant, cached until five minutes before it expires, and go only to
service URLs on the Microsoft allow-list over HTTPS (no SSRF, the token never leaves Microsoft
hosts). Approvals are Adaptive Cards whose buttons carry our callback data; a press comes back as an
`invoke` that we acknowledge at once and hand to the core as a button event (the core decides who
may answer). A card the service refuses falls back to the text card with the /approve hint.

Inputs → outputs:
- Credentials (secret store, then env; vendor names, D-16.22): `TEAMS_CLIENT_ID`,
  `TEAMS_CLIENT_SECRET`, `TEAMS_TENANT_ID` (all three required).
- `platforms.teams` keys: the kit's `webhook_host` / `webhook_port` (3978) / `webhook_path`
  (`/api/messages`) / `webhook_public` / `webhook_max_body_bytes` (1 MiB); `service_url` (default
  `https://smba.trafficmanager.net/teams/`, allow-listed); `extra_service_hosts` (more exact HTTPS
  hosts, for sovereign clouds); `openid_metadata_url` (the Bot Framework default; must be HTTPS).
- `POST /api/messages` → 401 (missing/bad token), 400 (bad body, service URL off the list), 200
  (accepted, ignored or duplicate), 200 + invoke body for card presses.
- Outbound: `send` / `edit` / `typing_start` / `send_approval` / `send_clarify` → Bot Connector calls
  → `SendResult`; `register_fetchers` → a downloader for attachment URLs.
- Files: `<home>/gateway/teams_conversations.json` (0600, at most 2,000 conversations, oldest
  dropped).

Files: `src/indus/gateway/adapters/teams/{__init__,adapter,botauth,conversations}.py`,
`src/indus/gateway/adapters/_entra.py` (client-credentials tokens, shared with P16-12),
`tests/fakes/platform/teams_wire.py`, `tests/contract/gateway/teams_case.py`,
`tests/unit/gateway/adapters/teams/{__init__,test_adapter,test_botauth}.py`; shared: the contract
`conftest.py` import line, `platforms.teams` schema row (+ test).

Edge cases / failure modes:
- token: missing header, not `Bearer`, not three base64url segments, over 8 KiB, header not JSON,
  `alg` other than RS256 (`none`, HS256, PS256, …), `crit` present, no `kid`, unknown `kid` (one key
  refresh at most every 5 minutes, then refuse), wrong issuer, audience not our app id (string or
  list), `exp` missing or passed (beyond skew), `nbf` in the future, signature of the wrong length or
  not verifying, a key smaller than 2,048 bits → 401, nothing parsed. Header URLs (`jku`, `x5u`,
  `jwk`) are never fetched.
- keys: metadata or key set unreachable → keep the cached keys and retry later; first fetch failing
  → 401 for that request (the Bot Connector retries); `jwks_uri` not HTTPS or on another host than
  the metadata → refused.
- body: over 1 MiB → 413 before reading; not JSON → 400; endorsements not naming the activity's
  channel or a `serviceUrl` claim that differs from the activity's → 401.
- service URL: not HTTPS, a port other than 443, a host not on the list, user info in the URL → the
  activity is dropped (400) and no reference stored; outbound to such a URL is refused.
- conversation ids: only `[A-Za-z0-9:@._-]` plus an optional `;messageid=<digits>` (path traversal
  impossible); activity ids digits/letters only.
- our own messages (`28:<app id>` or the recipient id) dropped; duplicates (Bot Connector retries
  reuse the activity id) dropped for an hour.
- token endpoint: 400/401 (`invalid_client`, `unauthorized_client`) → fatal at connect; network →
  retryable; never logs the secret or the token (errors carry the status and the OAuth error code).
- send: 404 `ConversationNotFound` → chat unreachable; other 404 → message/thread not found; 403 →
  forbidden; 413 → too long; 429 → `Retry-After`; timeouts per R06.4 (kit); a reply that cannot be
  threaded is not retried flat (the core's plain-text/ledger rules apply).
- proactive send to a conversation never seen → the configured service URL.
- attachment downloads: HTTPS only, default port, no redirects; the bot token only to Bot Connector
  hosts and `*.asm.skype.com`; SharePoint download URLs (pre-authorised) without a token; any other
  host refused; size capped by the intake.

Our design differences / decisions:
- Our names (`TeamsAdapter`, `BotTokenVerifier`, `BotKeys`, `ConversationBook`, `ClientCredentials`).
- Own REST client and JWT check instead of the vendor SDK (no new package; the SDK also loads a
  `.env` from the current directory at import, which the reference has to suppress).
- Loopback bind by default through the P16-01 kit (the reference binds every interface).
- Edits supported (`PUT` activity) so streaming and final edits work; the reference sends only.
- Card presses are acknowledged and handed to the core's prompt module as `metadata["button"]`; the
  core applies the requester/admin rule (the reference checks its own allowlist in the adapter).
- Mention gating stays in the core (P15 mention policy); the adapter records `mentioned`.
- Not taken: the meeting-summary pipeline (D-16.87, K12 gap), proactive DM to a user who never wrote
  (needs `POST /v3/conversations` with a member list), outbound files other than images (Teams
  needs a consent card or a SharePoint upload; images go inline up to 1 MiB, other kinds are
  refused cleanly), multiplex shared listener.

Tests we will write:
- contract suite over the wire fake (Bot Connector + token endpoint + OpenID keys + a signer).
- Verify: JWT with wrong audience 401; `serviceUrl` outside the allow-list refused for inbound and
  outbound (SSRF guard); proactive send uses the stored reference after a restart.
- JWT unit cases: alg none/HS256 refused, unsigned, tampered payload, expired/skew, nbf, unknown kid
  with one refresh, key rotation, small key, endorsements, serviceUrl claim, oversized token.
- token cache and refresh, secret and token never in logs/errors/reprs.
- card press → button event + invoke answer; card refused → text fallback; typing; edit.
- `indus send` over the real builder does no inbound work (no listener).
- fetcher host rules.

## Decisions for the lead
- No `teams` extra: own Bot Framework client over httpx and our own RS256 check (stdlib only). If
  the lead prefers a library, PyJWT (MIT, already locked through `mcp[crypto]`) can replace
  `botauth._rsa_verify` behind the same interface.
- Platform name `teams`; credentials `TEAMS_CLIENT_ID`, `TEAMS_CLIENT_SECRET`, `TEAMS_TENANT_ID`
  (all three in `BUILTIN_CREDENTIALS`: the token lock keys on the app).
- Conversation references in `<home>/gateway/teams_conversations.json`, 2,000 entries.
- Only the Bot Framework issuer is accepted (no Emulator / Entra v1/v2 issuers); a local Emulator
  would need its own opt-in.
- Clock skew 300 s; keys cached 24 h; unknown-`kid` refresh at most every 5 minutes.
- Any tenant the app is installed in is accepted (no tenant allow-list); a `allowed_tenants` key
  would be a small follow-up.
- The tenant id goes into `Source.scope_id` (session keys are per tenant).
- The allow-list is exact hosts `smba.trafficmanager.net`, `smba.infra.gov.teams.microsoft.us`,
  `smba.infra.dod.teams.microsoft.us` plus `extra_service_hosts`.

Compare checklist (filled after the rewrite; reopened `plugins/platforms/teams/adapter.py`,
`plugins/platforms/teams/plugin.yaml`, the test names in `tests/gateway/test_teams.py`):
- [x] credentials all three required; missing → refused before any call (test:
  `test_adapter.py::test_create_adapter_reads_the_secrets_and_refuses_without_them`)
- [x] service URL: https, exact Microsoft hosts, default port, trailing slash, regional default
  `…/teams/`, `TEAMS_SERVICE_URL` override (tests: `::test_service_url_allow_list_refuses_lookalikes`,
  `::test_service_url_allow_list_accepts_microsoft_hosts_and_operator_hosts`,
  `::test_create_adapter_reads_the_secrets_and_refuses_without_them`)
- [x] conversation id and tenant charset (no path traversal) (test:
  `::test_conversation_ids_cannot_escape_the_url_path`)
- [x] client-credentials token for `https://api.botframework.com/.default`, cached until five
  minutes before expiry, one request at a time (tests: `::test_the_token_is_cached_and_refreshed_once_after_a_401`,
  `::test_concurrent_sends_share_one_token_request`)
- [x] standalone (out-of-process) send: token + `POST …/activities` with `textFormat: markdown`
  (tests: `::test_indus_send_over_the_wire_does_no_inbound_work`,
  `::test_proactive_send_uses_the_stored_reference_after_a_restart`)
- [x] own messages (`28:<app id>` and the recipient id) dropped; dedupe by activity id (test:
  `::test_our_own_messages_duplicates_and_other_activity_types_are_dropped`)
- [x] conversation reference cached per conversation for proactive cards/sends (test:
  `::test_proactive_send_uses_the_stored_reference_after_a_restart`)
- [x] `<at>` tags stripped; mention entities matched against both bot id spellings (test:
  `::test_inbound_shapes_mentions_threads_and_identity`)
- [x] reply to one of our own messages counts as addressed (the reference's require-mention
  exemption; we set `reply_to_is_own` for the core's mention gate) (test:
  `::test_a_reply_to_our_message_is_marked_as_addressed_to_us`)
- [x] user id = AAD object id, else the channel account id; chat types personal/groupChat/channel
  (test: `::test_inbound_shapes_mentions_threads_and_identity`)
- [x] attachments: card and html mirror skipped; `file.download.info` → document via the
  pre-authorised download URL; Bot Framework image URLs fetched with the bot token; document wins
  over picture (tests: `::test_attachments_become_media_on_microsoft_hosts_only`,
  `::test_the_fetcher_sends_the_bot_token_only_to_bot_framework_hosts`)
- [x] approval Adaptive Card 1.4 with `Action.Execute`, positive/destructive styles, callback data
  in the button (tests: `::test_approval_cards_carry_our_callbacks_and_fall_back_to_text`,
  contract `approvals`)
- [x] card press (invoke `adaptiveCard/action`) answered with an invoke response (test:
  `::test_a_card_press_is_acknowledged_and_handed_to_the_core`)
- [x] long text split at 28,000; typing activity (test: `::test_edits_typing_and_long_replies`,
  contract `split`, `typing-refresh`)
- [x] outbound media: images inline as a data URI (test:
  `::test_images_go_inline_and_other_files_are_refused`)
- [x] Verify: wrong audience 401 (`::test_a_token_for_another_app_is_refused_with_401_and_nothing_is_parsed`);
  `serviceUrl` off the list refused (`::test_a_service_url_off_the_allow_list_is_never_stored_or_called`);
  proactive send uses the stored reference (`::test_proactive_send_uses_the_stored_reference_after_a_restart`);
  contract suite (`tests/contract/gateway/test_adapter_contract.py` case `teams`).
- Deliberate differences: the reference trusts the SDK's token validation and binds every
  interface; we check the JWT ourselves (RS256 only, issuer, audience, expiry, endorsements,
  `serviceUrl` claim) and bind loopback. The reference checks card presses against
  `TEAMS_ALLOWED_USERS` in the adapter (default deny) and answers with an updated card; we hand the
  press to the core, whose requester/admin rule decides, and answer "Received.". The reference
  replies threaded through the reply endpoint with a flat fallback; we post to the conversation
  (`;messageid=` for channel chains) with `replyToId` in the body, which never needs a fallback.
  The reference fetches non-Bot-Framework image URLs from any public host; we only fetch from
  Microsoft hosts (Bot Connector, `*.asm.skype.com` with the token; SharePoint without). Non-image
  files are not sent (Teams needs the file-consent flow; the reference's data URI for documents is
  not rendered by Teams). The reference reads `platforms.teams.extra.client_id` etc. from config;
  credentials here come only from the secret store / environment (config holds no secrets).
  `TEAMS_PORT` / `TEAMS_HOST` are the kit's `webhook_port` / `webhook_host`. A bad port is a
  configuration error, not a silent fallback to 3978.
- Not taken: the meeting-summary pipeline and its webhook/Graph delivery (D-16.87, K12 gap);
  `interactive_setup` (P16-31); the dotenv suppression (no SDK); edits of the approval card after
  a press (the core edits cards through `edit` when it wants to); the require-mention env switch
  (the core's mention policy).

## Status

Done (2026-10-20). New files only, plus the shared rows listed in the report (contract
`conftest.py` import line, `platforms.teams` schema row and its test). No new package.

## Landing (integrator, 2026-10-20)

Lead ruling applied: the RS256 check no longer re-encodes and compares by hand; `botauth.rsa_sha256_verify`
keeps its interface and the claim checks but verifies through `cryptography` via the shared
`adapters/_jws.py` (D-16.441, D-16.451). Registry row, credentials and lock key (`TEAMS_CLIENT_ID` +
`TEAMS_TENANT_ID`) added (D-16.453, D-16.454); `indus send` tested through the real row
(`tests/unit/gateway/delivery/test_send_p16_platforms.py`).
