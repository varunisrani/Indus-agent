# Task: Profile routes and delivery identity (P24-03) Phase: 24 Owner: wave-37 builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/profile_routing.py`
(all), `gateway/authz_mixin.py` (intake/delivery adapter resolution, transport owner, restored
source). Research 06 §3.7 (profile routing), §3.4 (identity first), §3.26. Closed before writing.
Evidence: none.

What it does (own words): under multiplexing a message can be handed to another profile than the
bot that received it. `gateway.profile_routes` is a list of routes `{name, platform, profile, bot?,
guild?, chat?, thread?, user?, enabled}`. A route matches only when every discriminator it names
matches (AND); among the matches the most specific wins by additive weights (user 16, thread 8,
chat 4, guild 2; no discriminator = 0). A route applies only to messages received by its `bot`
profile (unset = the default profile's bot), so a chat-id route meant for the shared bot can
never re-home a DM with a secondary's own bot. A chat route also matches threads under that chat
through the thread's parent id. No match: the receiving bot's own profile runs it.

Identity is settled once, first, before any key is built: which bot received the message
(transport profile) and which profile runs it (runtime profile). Exactly two functions answer the
adapter questions: `intake_adapter` (the bot that received it; only live provenance counts) and
`delivery_adapter` (who answers: the receiving bot when known; else, with no live provenance, the
unique owner of (platform, runtime profile) — the runtime profile's own bot, or the single bot
whose routes target it — or nobody). The receiving bot is persisted beside the route key so
delivery after a restart uses the same bot. A route naming a profile this gateway does not serve,
or a message from a bot nobody registered, is unresolved: it is dropped with one warning, and if
the platform has already acknowledged the update the target gets one "please resend" notice.
Session keys use the runtime profile's namespace (`@default` or the profile id; P15-07, format
unchanged).

Inputs → outputs:
- `ProfileRoute`, `parse_routes(raw)` → (routes most-specific first, problems);
  `match_route(routes, query)`.
- `AdapterTable`: `register(profile, platform, adapter)`, `unregister(..., release=)`,
  `for_profile`, `owner_of(adapter)`, `claims(profile)` (platforms with a bot of its own).
- `RoutingIdentity{transport_profile, runtime_profile, live}`.
- `ProfileRouter(routes, adapters, served=, bots=)`: `identify(source, adapter)` →
  `RoutingIdentity | Unresolved`; `intake_adapter(source, identity)`;
  `delivery_adapter(platform, identity)`; `restored_identity(session_key, runtime_profile)`;
  `session_key(source, identity, **opts)`; `drop_unresolved(...)`.
- `ReceivingBots(path)`: `remember(key, bot)`, `get(key)`, `forget(key)` (JSON file, capped).
- `session_keys.profile_of_key(key)`.
- config row `gateway.profile_routes` (list of mappings, empty).

Files: `src/indus/gateway/multiplex/routes.py`, `tests/unit/gateway/multiplex/test_routes.py`;
shared: `src/indus/gateway/core/session_keys.py`, `src/indus/foundation/config/schema.py` + its test.

Edge cases / failure modes:
- YAML numbers as ids → strings; floats, bools, lists → warned and the route skipped; missing
  platform or profile, an invalid profile id, an empty user id → skipped with a problem line.
- disabled routes never match.
- a thread message: `chat` matches the thread's own chat id or its parent id.
- the receiving adapter unknown (not registered) → unresolved.
- restored source with a persisted receiving bot whose adapter is offline → no delivery (never
  another bot); without a persisted bot → unique owner, else none, logged once per target.
- "please resend" is sent at most once per target, only when the platform acknowledged the update.

Our design differences / decisions:
- The bot is named by profile (`bot:`), our spelling; `default` and unset mean the default
  profile's bot.
- No WhatsApp identity aliasing in matching (exact ids; WhatsApp folding is P16's identity
  helper and can be added to `match` later).
- The receiving bot is persisted in a small JSON sidecar keyed by the session key
  (`<launch home>/gateway/receiving_bots.json`, at most 20,000 rows, oldest dropped) instead of a
  new routing-index column: the index row's origin feeds `Source.model_validate`, which forbids
  extra keys, and the routing schema is not this task's file.
- A satellite profile (no bot of its own) delivers through a bot only when exactly one live bot's
  routes target it.

Tests we will write:
- the specificity table (user > thread > chat > guild > default) and conjunctive matching.
- a route for bot A ignored for a message received by bot B.
- channel route matches a thread under it via the parent id.
- parse problems: numeric ids coerced, floats/empty user skipped, invalid profile skipped.
- intake/delivery: receiving bot answers; restored source with two possible owners delivers
  nowhere and logs once; a secondary without a bot never borrows the default bot.
- unresolved identity dropped with one warning; resend notice once and only when acknowledged.
- session key namespace from the runtime profile; receiving bot survives a reload of the sidecar.

## Decisions for the lead
- Wiring owed: every adapter's intake calls `ProfileRouter.identify` before the admission
  pipeline builds a key; the pipeline uses `router.session_key` and `delivery_adapter` for every
  send (replies, cron, notices); `ReceivingBots.remember` when a route row is created.
- Move the receiving bot into a `transport_profile` column of `gateway_routes` when the routing
  schema is next changed (the sidecar is a stop-gap).

Compare checklist (filled after the rewrite; reopened `gateway/profile_routing.py`,
`gateway/authz_mixin.py`):
- [x] weights and AND rule (test: `tests/unit/gateway/multiplex/test_routes.py::test_specificity_table`)
- [x] bot rule (test: `test_routes.py::test_a_route_for_bot_a_is_ignored_for_bot_b`)
- [x] parent id (test: `test_routes.py::test_a_chat_route_matches_threads_under_it`)
- [x] id coercion and skipped rows (test: `test_routes.py::test_parse_coerces_numbers_and_skips_bad_rows`)
- [x] pinned bot offline → no delivery (test: `test_routes.py::test_a_pinned_bot_that_is_offline_delivers_nothing`)
- [x] unique owner for restored sources (test:
  `test_routes.py::test_a_restored_source_with_two_possible_owners_delivers_nowhere`)
- [x] gap found: a profile with a bot of its own on any platform (or one that is offline) is its
  own credential boundary and never borrows a route bot — fixed by `AdapterTable` claims (kept
  until the profile is released at unserve), pinned by
  `test_routes.py::test_a_profile_with_its_own_bot_never_borrows_a_route_bot`
- Deliberate differences: no WhatsApp alias matching; no relay special case (P24-09 adds its
  adapter to the table when it lands); the receiving bot lives in a sidecar file; a float, bool
  or empty id skips the whole route with a problem line (the reference keeps a float route that
  can never match).
- Not taken: "transport inferred" identities for hand-built sources (we always resolve through
  the table or fail closed).

## Wiring (wave 42, builder WA, 2026-10-21)

Read: no reference file (wiring). Every adapter of every served profile is registered in the
host's `AdapterTable` (platform up / down); its sink is `MultiplexServing.intake`:
`ProfileRouter.identify` first (unresolved → `drop_unresolved`, one "please resend" notice through
the receiving bot), then the routed profile's admission pipeline (session key in its namespace),
with `ReceivingBots.remember` for the key. Replies of every served profile pick their sender with
`PlatformReplies.sender_for_reply` → `delivery_adapter` (the receiving bot, live or restored from
the sidecar; never another bot by guess). Test:
`tests/integration/gateway/multiplex/test_two_profiles_one_gateway.py::test_a_routed_message_runs_in_its_profile_and_is_answered_by_the_bot_that_received_it`.

Decisions for the lead: routes come from the launch profile's `gateway.profile_routes` only (one
route table per host); cron and `indus send` deliveries keep their per-profile delivery router
(they name a platform, not a received chat).

## Status

Done (2026-10-21), wired in wave 42.
