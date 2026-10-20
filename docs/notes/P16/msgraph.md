# Task: Microsoft Graph change-notification receiver (P16-12) Phase: 16 Owner: wave-25 builder E6

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/msgraph_webhook.py`
(whole file: settings, source CIDR gate, validation handshake, notification batch handling,
clientState check, receipts, event building, prompt template), `plugins/teams_pipeline/subscriptions.py`
(whole file: the subscription upkeep the reference runs from its Teams pipeline CLI), the env names
in `tools/microsoft_graph_auth.py` and `gateway/config_env.py` (lines 130–150), test names in
`tests/gateway/test_msgraph_webhook.py`. Research 06 §3.3 (Graph row).
Evidence: none.

Vendor API (interop facts, checked against the Microsoft Graph v1.0 change-notification
documentation): `POST https://graph.microsoft.com/v1.0/subscriptions` with `changeType`,
`notificationUrl`, `lifecycleNotificationUrl`, `resource`, `expirationDateTime`, `clientState`
(≤ 128 characters), `latestSupportedTlsVersion`; `PATCH /subscriptions/{id}` with a new
`expirationDateTime`; `DELETE` not used. Validation: Graph calls the notification URL with
`?validationToken=<token>` and expects `200`, `text/plain`, the token echoed, within 10 s.
Notifications: `POST` JSON `{"value": [ {subscriptionId, clientState, changeType, resource,
resourceData, subscriptionExpirationDateTime, tenantId, lifecycleEvent?} ]}`, answered with 2xx
within 3 s (`202 Accepted`). Lifecycle events: `reauthorizationRequired`, `subscriptionRemoved`,
`missed`. Tokens: the same client-credentials grant as Teams with scope
`https://graph.microsoft.com/.default`. No library (httpx only), nothing to pin.

Capability flags: `max_len` 100,000, `splits_natively` true (nothing is chunked: replies are not
posted anywhere), `supports_edit` / `supports_buttons` false, `owns_access_policy` false,
`auth_upstream` true (clientState), `dm_is_private` **false** (R06.24), `push_capable` false.

What it does (own words): an ingress, not a chat. The operator points Graph subscriptions at our
listener (loopback by default behind a tunnel, or a public bind that must name the source CIDRs it
accepts). Graph's validation call is answered by echoing the token as plain text, capped in length
and refused when it holds control characters. Every notification must carry the configured
`clientState` secret (compared in constant time); others are dropped and a batch of only forged
items gets 403. Accepted notifications are deduplicated by id (or a digest of their content) for one
hour and become internal events for one configured conversation (or one per subscription): the
text is a fixed, sanitised summary of a few known fields, framed as data from an outside service,
never as instructions, and the event can never run a command (`allow_control` false). Replies the
agent writes are not posted back to Graph: they are handed to a configured delivery target when the
daemon wires one, else logged (truncated, scrubbed). When client credentials and subscription specs
are configured, the adapter creates the subscriptions after its listener is up, stores their ids,
and renews each when 80 % of its lifetime has passed (a 404 on renewal recreates it; failures retry
with a growing pause and never past expiry); `reauthorizationRequired` renews at once and
`subscriptionRemoved` recreates.

Inputs → outputs:
- Secrets: `MSGRAPH_WEBHOOK_CLIENT_STATE` (required, 16–128 printable characters); optional
  `MSGRAPH_TENANT_ID`, `MSGRAPH_CLIENT_ID`, `MSGRAPH_CLIENT_SECRET` (subscription upkeep).
- `platforms.msgraph` keys: kit `webhook_*` (port 8646, path `/msgraph/webhook`, body 1 MiB,
  `webhook_public_url` = the notification URL), `allowed_source_cidrs`, `accepted_resources`
  (prefixes, trailing `*` allowed), `session` (one conversation name), `deliver_to` (a send target),
  `prompt` (template with `{change_type}`, `{resource}`, `{subscription_id}`, `{tenant_id}`,
  `{resource_id}`, `{lifecycle_event}` only), `subscriptions` (list of `{resource, change_type,
  lifetime_minutes}`).
- `GET|POST <path>?validationToken=…` → 200 text/plain echo / 400; `POST <path>` body → 202 / 400 /
  403 / 413; `<home>/gateway/msgraph_subscriptions.json` (0600).

Files: `src/indus/gateway/adapters/msgraph/{__init__,adapter,subscriptions}.py`,
`tests/fakes/platform/msgraph_wire.py`, `tests/unit/gateway/adapters/msgraph/{__init__,test_adapter}.py`;
uses `adapters/_entra.py` (P16-11). Shared: `platforms.msgraph` schema row (+ test), trap R06.24 rows.

Edge cases / failure modes:
- no clientState configured, or one outside 16–128 printable characters → fatal at connect.
- public bind without `allowed_source_cidrs` → fatal at connect; a source outside the CIDRs → 403.
- validation token: missing → 400; over 1,024 characters or with control characters → 400.
- body not JSON, `value` not a list, an item not an object → 400 (nothing accepted).
- clientState missing, wrong or non-ASCII → item dropped (no exception).
- a resource outside `accepted_resources` → item ignored.
- duplicates within an hour → counted, not emitted; still answered 202.
- template placeholders other than the six names are left as written; values have control
  characters removed and are cut at 512 characters.
- subscription upkeep: no credentials or no public URL → upkeep off (warning); create failing →
  retried; renewal 404 → recreate; restart → stored ids reused (renewed when due).

Our design differences / decisions:
- Our names (`GraphReceiver`, `SubscriptionKeeper`, `SubscriptionSpec`).
- Subscription upkeep lives in the receiver (the reference has it in a separate pipeline CLI that
  renews within 24 h of expiry); we renew at 80 % of each lifetime as the phase row says.
- Loopback by default (the kit); the reference binds every interface.
- Events carry a sanitised field summary, not the raw JSON dump (content is untrusted).
- `allow_control` false on every event; `internal` true (no pairing, no busy acks).
- Platform name `msgraph` (the reference's enum says `msgraph_webhook`).
- Not taken: rich notifications with resource data (encrypted content is ignored), the plugin
  notification scheduler, health counters in `/health` (the kit's `/health` stays minimal).

Tests we will write:
- handshake echo on GET and POST (plain text, exact), missing/oversized/control-character tokens.
- wrong / missing / non-ASCII clientState dropped; all-forged batch 403; mixed batch 202.
- dedupe by id within an hour, again after; digest dedupe without an id.
- resource filter; CIDR gate; public bind refused without CIDRs.
- event shape: internal, `allow_control` false, sanitised text, template, `dm_is_private` false.
- subscription create at start, renewal at 80 % on the fake clock, 404 recreate, lifecycle events,
  stored ids reused.
- replies forwarded to the delivery port or logged.

## Decisions for the lead
- Platform name `msgraph`, credential `MSGRAPH_WEBHOOK_CLIENT_STATE` (the only one in
  `BUILTIN_CREDENTIALS`); the registry row is a `PlatformEntry` like `webhook` with
  `trusted_inbound=True`, `dm_is_private=False`.
- Not in the shared chat contract suite (it cannot send, like `api_server` / `webhook`); its own
  tests cover the ingress.
- The reply port (`forward`) is an `Adapter` attribute the daemon can set
  (`receiver.set_forward(async (target, text) -> SendResult)`); until wired, replies are logged.
- clientState minimum length 16 (Graph allows any length up to 128).

Compare checklist (filled after the rewrite; reopened `gateway/platforms/msgraph_webhook.py`,
`plugins/teams_pipeline/subscriptions.py`, the test names in `tests/gateway/test_msgraph_webhook.py`):
- [x] refuse to start without clientState (test: `test_adapter.py::test_create_adapter_needs_a_strong_client_state`,
  `::test_unsafe_setups_are_refused_at_connect`)
- [x] public bind without source CIDRs fails closed; loopback needs none; a source outside the
  CIDRs → 403 (tests: `::test_unsafe_setups_are_refused_at_connect`,
  `::test_source_networks_gate_every_request`)
- [x] validation: GET echoes `validationToken` as text/plain; bare GET 400; a POST carrying the
  token echoes too (tests: `::test_the_validation_handshake_echoes_the_token_as_plain_text`,
  `::test_bad_validation_requests_are_refused_without_echo`)
- [x] body: declared size over the cap 413 (kit), not JSON / `value` not a list → 400 (test:
  `::test_malformed_batches_are_400`)
- [x] clientState compared in constant time, non-ASCII without raising; all-forged batch 403;
  accepted or duplicate → 202 with an empty body (test: `::test_a_wrong_client_state_is_dropped`)
- [x] accepted resources: prefixes, trailing `*`, leading slash tolerated (test:
  `::test_resources_outside_the_accepted_prefixes_are_ignored`)
- [x] receipts by id, else a content digest; bounded memory (test:
  `::test_notifications_are_deduplicated_for_an_hour`)
- [x] source `msgraph:<subscription id>`, user `msgraph`, internal event; replies logged, never
  posted (tests: `::test_events_are_internal_never_commands_and_sanitised`,
  `::test_replies_are_logged_or_forwarded_never_posted_to_graph`)
- [x] prompt template with placeholders; unknown placeholders left as written (test:
  `::test_the_prompt_template_fills_known_fields_only`)
- [x] subscription upkeep: expired ones not renewed but recreated, renewal extends from now,
  stored records, remote 404 handled (tests: `::test_renewal_fires_at_eighty_percent_of_the_lifetime`,
  `::test_a_restart_reuses_the_stored_subscription_and_a_404_recreates_it`,
  `::test_lifecycle_events_renew_or_recreate_at_once`, `::test_failed_calls_retry_with_a_growing_pause`)
- [x] Verify: handshake echo; wrong clientState dropped; renewal at 80 % on the fake clock (tests
  above).
- Deliberate differences: the reference binds every interface by default (CIDRs required); we
  bind loopback by default (the kit) and require CIDRs only for a public bind. The reference checks
  the resource before the clientState (a forged item with a foreign resource counts as "other");
  we check the secret first. The reference renders dotted template keys from the whole raw
  notification (and dumps the JSON by default); we render six sanitised fields only and frame
  them as outside data, and every event has `allow_control` false. The reference's subscription
  upkeep is a separate CLI renewing within 24 h of expiry by 24 h and flagging subscriptions
  missing remotely; ours runs inside the receiver for the configured specs and renews at 80 % of
  each lifetime. The reference's `/health` reports counters and is CIDR-gated; the kit's `/health`
  says only `ok` / `stopping` (counts stay in `GraphReceiver.counts`). Env-only settings
  (`MSGRAPH_WEBHOOK_PORT`, `…_ACCEPTED_RESOURCES`, `…_ALLOWED_SOURCE_CIDRS`, `…_ENABLED`) are config
  keys here; the clientState stays a secret.
- Not taken: the plugin notification scheduler (P13 plugins can hook the core instead), the
  per-profile shared listener, the dry-run / list views of the upkeep CLI.

## Status

Done (2026-10-20). New files only, plus the shared rows listed in the report (`platforms.msgraph`
schema row and its test, trap R06.24 row). No new package.

## Landing (integrator, 2026-10-20)

Lead ruling applied: a reply with no route is no longer logged and reported delivered; it is a typed
non-delivered result (`not_delivered()`, `extra["undelivered"] = "not_configured"`) with one warning
per receiver. The daemon's `RouterPart` installs the forward (`delivery.parts.graph_forward`) for
`deliver_to`; `deliver_to: msgraph:…` is refused; `msgraph` is never a send target (D-16.443).
