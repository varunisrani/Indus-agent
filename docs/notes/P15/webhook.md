# Task: Webhook receiver and `indus webhook` (P15-32) Phase: 15 Owner: wave-20 builder W5

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/webhook.py`
(route validation, the request handler, signature checks, prompt rendering),
`gateway/platforms/webhook_ingress.py` (admission scope), `webhook_filters.py` (filter
operators), `webhook_coalesce.py` (debounce and supersede), `webhook_delivery.py` (destination
checks), `hermes_cli/webhook.py` (subscribe/list/remove/test). Evidence: none.
Interop: GitHub webhook docs (`X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the body>`,
`X-GitHub-Event`, `X-GitHub-Delivery`), GitLab webhook docs (`X-Gitlab-Token` = the secret,
`X-Gitlab-Event`, `X-Gitlab-Event-UUID`).

What it does (own words): the daemon listens on a second port (8644 by default, loopback by
default) for HTTP POSTs from other services: `POST /webhooks/<route>`. Each route has its own
secret and says how the sender proves it: GitHub's body HMAC, GitLab's token, a plain body HMAC,
or a timestamped HMAC whose timestamp must be within 300 s. The signature is checked on the raw
bytes before anything parses the body. A route may filter events (event names, payload
conditions), renders a prompt from a template with `{dot.path}` placeholders (a missing key is
empty text, never an error), and then either sends the rendered text straight to a delivery
target (no agent, "direct delivery"), or runs an agent turn on a fresh session whose tools are
the untrusted floor only — web search/extract, vision, clarify (R02.27) — and delivers its reply
to the route's target. Bursts about one entity can be coalesced: only the newest event of a key
runs, after a quiet window, never later than a max wait after the first. Each route takes at most
30 requests a minute; bodies over 1 MiB are refused; a delivery id seen in the last hour is
answered `duplicate` without a second turn. A route marked `insecure: true` skips the signature,
and only a receiver bound to loopback may load one.

Inputs → outputs:
- Config `gateway.webhook`: `enabled` (false), `host` 127.0.0.1, `port` 8644, `rate_limit` 30,
  `max_body_bytes` 1,048,576, `routes: {name: route}`.
- Route: `secret` (the *name* of a secret in the profile's store or env; never the value),
  `auth` (`auto` | `github` | `gitlab` | `hmac` | `hmac_ts`), `insecure` (bool), `enabled`,
  `events` (allowed event names), `filters` (list of `{field, equals|contains|exists}`, with
  `all`/`any`/`not`), `prompt` (template), `deliver` (P14 target grammar; empty = keep the reply in
  the session only), `deliver_only` (bool), `coalesce {key, window_seconds 30,
  max_wait_seconds 300}`.
- Subscriptions file `<home>/gateway/webhook_subscriptions.json` (0600, no secret values; config
  routes win on a name clash), written by `indus webhook subscribe NAME [--events] [--prompt]
  [--deliver] [--deliver-only] [--auth] [--secret-name]`; `list`, `remove NAME`, `test NAME
  [--payload JSON]` (signs and POSTs to the local receiver). A generated secret is stored as
  `INDUS_WEBHOOK_<NAME>` and printed once.
- Answers: 202 `{"status": "accepted"|"coalesced", "route", "delivery_id"}`, 200
  `{"status": "ignored"|"duplicate"|"delivered"}`, 401 bad signature, 404 unknown route, 403
  disabled route, 413 too large, 429 rate limited, 400 unparseable body, 502 direct delivery
  failed, 503 no session service.

Files: `src/indus/gateway/http/webhook.py`, `src/indus/gateway/http/webhook_rules.py`,
`src/indus/state/webhook_subscriptions.py` (new: the subscriptions file, shared by the CLI and the
receiver — D-16.246), `src/indus/cli/commands/webhook.py`, `tests/contract/gateway/test_webhook.py`,
`tests/unit/cli/commands/test_webhook.py`, `tests/unit/gateway/http/test_webhook_rules.py`,
`tests/fixtures/gateway/webhook_payloads/` (GitHub, GitLab and generic samples, ours). Shared:
`cli/app.py` (`webhook` row), the help catalogue and non-TTY table.

Edge cases / failure modes:
- signature header missing or wrong → 401; a timestamped request older or newer than 300 s → 401;
  a timestamped header present commits to that scheme (no fallback to the plain HMAC).
- a route whose secret is missing or blank and not `insecure` → refused at load (the receiver does
  not start with it); `insecure` on a non-loopback host → refused at load.
- body over the cap by `Content-Length` → 413 before reading; a chunked body is cut at the cap.
- delivery id: `X-GitHub-Delivery`, `X-Gitlab-Event-UUID`, `X-Request-Id`, `X-Indus-Delivery`;
  without one, the SHA-256 of the body (so a replayed body is caught too).
- JSON body, else form-encoded; neither → 400.
- coalescing: a key that does not resolve → the event runs at once; at shutdown held events are
  dropped (logged) and their delivery ids forgotten, so the sender's retry after the restart is
  taken (the reference flushes them into its durable admission; we have none to flush into).
- the agent session's tool list is checked against the floor before the turn; anything wider →
  the turn is refused (fail closed).
- a reply that is a silence token is not delivered.

Our design differences / decisions:
- Secrets are names into the secret store (the reference keeps them in the config file and the
  subscriptions JSON).
- `auth` can be pinned per route; `auto` picks by the headers present (GitHub, GitLab, timestamped,
  plain) like the reference. Our generic headers: `X-Indus-Signature: sha256=<hex>` (body HMAC) and
  `X-Indus-Timestamp` + `X-Indus-Signature-Ts: sha256=<hex of "<ts>.<body>">`.
- Missing template keys render empty (the reference leaves `{key}` in the text).
- No route scripts, no skills injection, no cron-job triggers, no GitHub comment delivery, no
  Svix/Linear schemes, no `/p/<profile>/` prefixes (multi-profile serving is P24) — later phases
  may add them.
- Rate limit and idempotency run after authentication, so unauthenticated floods neither use a
  route's budget nor poison its delivery ids.
- Direct delivery and agent replies go through P14's deliverer registry
  (`cron.delivery.deliverer_for`), where P15-20 registers the platform deliverers; a `DirectDeliver`
  port lets the daemon substitute the router.

Tests we will write:
- bad signature 401 for each scheme; good one 202; stale timestamp 401.
- replayed delivery id → `duplicate`, the fake model sees one request.
- 31st request in 60 s → 429; after the window → accepted again (fake clock).
- insecure route on 0.0.0.0 refused at load; on 127.0.0.1 accepted.
- coalescing with the fake clock: three events, one turn with the newest payload; max wait bounds
  a steady stream.
- filters: equals/contains/exists/all/any/not; templates: dot paths, list indexes, missing → "".
- direct delivery goes to the delivery port; agent reply delivered; silence not delivered.
- webhook session tool list equals the floor.
- CLI: subscribe (secret stored, not in the file, printed once), list, remove, test (signed POST,
  mocked transport), non-TTY exit codes.

## Decisions for the lead
- Config under `gateway.webhook` (reference: `platforms.webhook.extra`).
- `indus webhook` is registered in `cli/app.py` by this slice (the phase file gives that row to
  P15-28; drop one copy when merging with W4).

Compare checklist (filled after the rewrite; reopened the six files above):
- [x] secret required unless `insecure`, `insecure` only on loopback, checked before start
  (`test_webhook.py::test_an_insecure_route_loads_only_on_loopback`)
- [x] size checked by length before reading and while reading (`::test_size_route_and_secret_checks`)
- [x] signatures on the raw body in constant time: GitHub, GitLab token, plain HMAC, timestamped
  HMAC with a 300 s window; a timestamped header commits to that scheme
  (`::test_a_bad_signature_is_401_and_a_good_one_runs_the_agent`,
  `::test_the_timestamped_scheme_refuses_stale_requests_and_never_falls_back`)
- [x] rate limit after authentication, 30 per 60 s per route (`::test_the_31st_request_in_60_s_is_429`)
- [x] delivery-id memory for an hour, per route (`::test_a_replayed_delivery_is_answered_without_a_second_turn`)
- [x] event allow list, then payload filters, answered `ignored` (`::test_events_filters_and_direct_delivery`)
- [x] direct delivery with no agent, 502 on a refused send (`::test_events_filters_and_direct_delivery`)
- [x] coalescing: newest event per key, quiet window bounded by max wait, unresolved key runs at
  once, a note on the prompt (`::test_coalescing_runs_the_newest_event_once_after_the_quiet_window`,
  `::test_an_unresolved_coalesce_key_runs_at_once`)
- [x] subscriptions hot-reloaded by file identity, config wins (`::test_subscribed_routes_are_picked_up_without_a_restart`)
- [x] silence tokens are not delivered (`::test_a_silent_reply_is_not_delivered`)
- [x] CLI subscribe/list/remove/test (`tests/unit/cli/commands/test_webhook.py`)
- Gap found and fixed: the coalescing timer measured its window from when its task first ran, not
  from when the event arrived — deadlines are now fixed at arrival (the same for the API desk's
  idle close).
- Deliberate differences: secrets are names into the secret store; missing template keys render
  empty; a failed direct delivery forgets its delivery id (a retry is taken, the reference answers
  it as a duplicate); a body hash stands in for a missing delivery id; no `GET /health` on the
  webhook port; the agent prompt starts with a line saying the content is outside data.
- Not taken: route scripts, skills injection, cron-job triggers, GitHub comment delivery,
  `mirror_to_session`, Svix / Standard Webhooks / Linear schemes, per-route profiles and
  `/p/<profile>/` prefixes (P24 multi-profile serving), per-route toolsets (the floor is fixed).

## Status

Done (2026-10-19). Shared files touched: `cli/app.py` (`webhook` row), the help catalogue and the
non-TTY table, `foundation/config/schema.py` (+ test: `gateway.webhook` rows),
`acceptance-map.md` (ACC-K36-1), `traps-ledger.md` (R02.27).
